"""
Pipeline 编排 v0.3
- 多源编排:Europe PMC + OpenAlex(可配置 --sources);各源失败如实记录部分覆盖
- 完整核验链:注册(论文/证据/Gap/假设)-> 坏证据发现 -> 删除/降级 -> 级联清理(不崩溃)
  -> 受影响 Gap 重判定 -> 假设核验(配对) -> 报告 -> 字段绑定 report_check
- 密钥脱敏:RunLog.register_secret 后所有 log/log_api 自动脱敏;落盘前再全局扫描
- 失败运行也完整落盘
"""
from __future__ import annotations

import json
import os
from typing import Dict, List

from .config import (APP_VERSION, DEFAULT_DEEP_EXTRACT, DEFAULT_MAX_RETRIEVE, DEFAULT_SCREEN_KEEP,
                     DEFAULT_YEAR_FROM, RUNS_DIR, new_run_id)
from .evaluator import screen
from .extractor import build_evidences, extract_batch
from .gap_hunter import GapHunter
from .hypothesis import HypothesisArchitect
from .planner import build_plan
from .reporter import ReportGenerator
from .retriever import crossref_verify, get_openalex_key, retrieve, openalex_search
from .schemas import RunLog, redact
from .scorer import GapScorer
from .store import EvidenceStore
from .validator import Validator


def _scrub_secrets(obj, secrets):
    return redact(obj, secrets)


def run_pipeline(topic: str, out_root: str = RUNS_DIR,
                 max_retrieve: int = DEFAULT_MAX_RETRIEVE,
                 screen_keep: int = DEFAULT_SCREEN_KEEP,
                 deep_extract: int = DEFAULT_DEEP_EXTRACT,
                 year_from: int = DEFAULT_YEAR_FROM,
                 max_gaps: int = 6,
                 sources: List[str] | None = None,
                 progress=None) -> Dict:
    sources = sources or ["europepmc", "openalex"]
    run_id = new_run_id()
    run_log = RunLog(run_id=run_id)
    # 密钥注册(供脱敏;不回显)
    k = get_openalex_key()
    if k:
        run_log.register_secret(k)
    cfg = (f"max_retrieve={max_retrieve}, screen_keep={screen_keep}, "
           f"deep_extract={deep_extract}, year_from={year_from}, max_gaps={max_gaps}, "
           f"sources={sources}, version={APP_VERSION}")
    run_log.log("Pipeline", "start", "ok", f"topic='{topic}' config=[{cfg}]")

    def _p(stage: str, status: str = "running", detail: str = ""):
        """真实阶段进度回调(供后台任务接口读取;回调异常不改变流水线行为)。"""
        if progress:
            try:
                progress(stage, status, detail)
            except Exception:
                pass

    store = EvidenceStore(run_log)
    plan = None
    papers: List = []
    included: List = []
    deep: List = []
    gaps: List = []
    hyps: List = []
    ranked: List = []
    validation_summary: Dict = {}
    source_stats: Dict = {}
    failure = None

    try:
        # ---------- Node 0
        plan = build_plan(topic, year_from=year_from, run_log=run_log)

        # ---------- Module 1(多源)
        _p("retrieve", "running", f"max_retrieve={max_retrieve} sources={sources}")
        papers, source_stats = retrieve(plan, max_retrieve, run_log, sources=sources)
        _p("retrieve", "done", f"papers={len(papers)}")
        if not papers:
            failure = "检索无结果(或全部源失败):本轮不产出 Gap/假设;日志完整保存供审计"
            _p("retrieve", "empty_or_failed", failure)
            run_log.log("Pipeline", "retrieve", "empty_or_failed", failure)
        else:
            # ---------- Module 2
            _p("screen", "running", f"screen_keep={screen_keep}")
            included = screen(papers, plan, screen_keep, run_log)
            _p("screen", "done", f"included={len(included)}")
            for p in included:
                crossref_verify(p, run_log)

            # ---------- Node 1
            deep = included[:deep_extract]
            _p("extract", "running", f"deep={len(deep)}")
            extract_batch(deep, plan.concepts, run_log)
            _p("extract", "done", f"deep={len(deep)}")
            evs = []
            for i, p in enumerate(deep, 1):
                evs.extend(build_evidences(p, prefix=f"P{i:03d}"))
            store.add_papers(papers)
            store.add_evidences(evs)

        if not failure:
            # ---------- Node 2(多源反查:传入 OpenAlex 回调)
            hunter = GapHunter(store, run_log, plan, sources=sources)
            oa_fn = None
            if "openalex" in sources and get_openalex_key():
                def oa_fn(q, n):
                    recs, meta = openalex_search(q, n, run_log, year_from=None)
                    # 为判定器重建摘要字段
                    from .retriever import rebuild_abstract_from_inverted
                    for r in recs:
                        r["abstract_rebuilt"] = rebuild_abstract_from_inverted(
                            r.get("abstract_inverted_index"))
                    return recs, meta
            elif "openalex" in sources:
                run_log.log("Pipeline", "openalex", "no_key",
                            "OpenAlex 已启用但缺少密钥:反查按单源(EPMC)部分覆盖执行")
            gaps = hunter.discover(max_gaps=max_gaps)
            _p("gap_hunter", "running", f"max_gaps={max_gaps}")
            gaps = hunter.reverse_search(gaps, year_from=year_from, openalex_fn=oa_fn)
            store.add_gaps(gaps)
            _p("gap_hunter", "done", f"gaps={len(gaps)}")

            # ---------- Node 3(先全部注册)
            architect = HypothesisArchitect(store, run_log, plan)
            _p("hypothesis", "running", f"gaps={len(gaps)}")
            hyps = architect.generate(gaps, year_from=year_from, openalex_fn=oa_fn)
            store.add_hypotheses(hyps)
            _p("hypothesis", "done", f"hyps={len(hyps)}")

            # ---------- Module 4(完整核验链)
            validator = Validator(store, run_log)
            paper_stats = validator.check_papers(deep)
            loc_bad = validator.check_evidence_location()      # 含级联清理+Gap 重判定
            passed_hyps = validator.check_hypotheses(store.hypotheses.values())
            strength_notes = validator.check_conclusion_strength(gaps)
            _p("validate", "running", "")
            consistency = validator.check_consistency()
            # 一致性失败的实际影响:重新过滤 passed
            passed_hyps = [h for h in passed_hyps if h.validation.get("passed")]
            _p("validate", "done", f"passed={len(passed_hyps)} consistency_issues={len(consistency)}")
            # v0.4 修复#2:竞争假设有限重写循环(真正 HYP_MAX_REWRITES 次)
            # 对 A 通过但 B 未通过判别(或 A/B 未通过判别)的 Gap,重写 B 的预测
            # 使其与 A 结构性判别;重写后重新核查,通过者并入 passed。
            rewritten = architect.rewrite_failed_pairs(gaps, passed_hyps)
            if rewritten:
                # v0.5.1 修复#6c:重写之后重新执行 finalize 定稿,
                # 使重写产生的候选也经过模板/占位符扫描与待审标注。
                for h in rewritten:
                    g = store.gaps.get(h.gap_id)
                    if g is not None:
                        architect._finalize_system_hypothesis(h, g)
                rechecked = validator.check_hypotheses(rewritten)
                passed_hyps = passed_hyps + [h for h in rechecked
                                             if h.validation.get("passed")]
            _p("rewrite", "done", f"rewritten_total={len(passed_hyps)}")
            hyps = passed_hyps

            # ---------- 排序(清理后仍存活的 Gap)
            scorer = GapScorer(store, run_log)
            _p("score", "running", "")
            ranked = scorer.score_all(gaps)
            _p("score", "done", f"ranked={len(ranked)}")

            validation_summary = {
                "paper_stats": paper_stats,
                "evid_dropped": len(loc_bad),
                "hyp_passed": len([h for h in passed_hyps if h.origin == "system"]),
                "hyp_total": len([h for h in store.hypotheses.values() if h.origin == "system"]),
                "hyp_author_registered": len([h for h in store.hypotheses.values()
                                              if h.origin == "author"]),
                "consistency_issues": len(consistency),
                "strength_adjustments": len(strength_notes),
                "validator_actions": list(validator.actions),
            }
    except Exception as e:
        import traceback
        failure = f"运行异常:{type(e).__name__}: {str(e)[:200]}"
        _p("exception", "failed", failure)
        run_log.log("Pipeline", "exception", "error",
                    failure + " | " + traceback.format_exc()[-300:])

    # ---------- 输出(失败也落盘)
    out_dir = os.path.join(out_root, run_id)
    os.makedirs(out_dir, exist_ok=True)
    if plan:
        with open(os.path.join(out_dir, "search_plan.json"), "w", encoding="utf-8") as f:
            json.dump(_scrub_secrets(plan.to_dict(), run_log._secrets),
                      f, ensure_ascii=False, indent=2)
    _p("export_data", "running", out_dir)
    store.export(out_dir)
    _p("export_data", "done", "")

    reporter = ReportGenerator(store, run_log, plan)
    _p("report", "running", "")
    outputs = reporter.save_all(out_dir, topic, ranked, hyps,
                                validation_summary or {"paper_stats": {}})
    _p("report", "done", "")
    if source_stats:
        with open(os.path.join(out_dir, "source_stats.json"), "w", encoding="utf-8") as f:
            json.dump(_scrub_secrets(source_stats, run_log._secrets),
                      f, ensure_ascii=False, indent=2)
    if failure:
        with open(outputs["report"], "a", encoding="utf-8") as f:
            f.write(f"\n\n## ⚠️ 运行状态\n\n{failure}\n\n本目录日志与数据仍然完整保存供审计。\n")
    else:
        validator2 = Validator(store, run_log)
        # v0.5 修复#4:传入 out_dir 使图件文件存在检查真正执行
        rc = validator2.report_check(open(outputs["report"], encoding="utf-8").read(),
                                     out_dir=out_dir)
        _p("report_check", "done",
           f"checked={len(rc['checks'])} mismatched={rc['mismatched']}")
        validation_summary["report_check"] = {"checked": len(rc["checks"]),
                                              "mismatched": rc["mismatched"],
                                              "not_checked": rc.get("not_checked", 0)}
        with open(os.path.join(out_dir, "report_check.json"), "w", encoding="utf-8") as f:
            json.dump(rc, f, ensure_ascii=False, indent=2)
        # v0.4 修复#9:报告核对失败 → 交付状态降级(不静默通过)
        if rc["mismatched"] > 0:
            failure = (f"报告一致性核对失败:{rc['mismatched']}/{len(rc['checks'])} 项字段绑定不匹配;"
                       "交付状态:DEGRADED(数据完整保留供审计,正文数字不可信)")
            with open(outputs["report"], "a", encoding="utf-8") as f:
                f.write(f"\n\n## ⚠️ 交付状态:DEGRADED\n\n{failure}\n")
            run_log.log("Pipeline", "report_check", "failed", failure)
    _p("export_pdf_word", "running", "")
    exports = reporter.export_pdf_word(out_dir)
    _p("export_pdf_word", "done", str(sorted(exports)))
    outputs.update(exports)

    _p("finish", "failed" if failure else "ok", failure or "")
    run_log.log("Pipeline", "finish", "failed" if failure else "ok",
                f"outputs={sorted(outputs)} dir={out_dir}")
    # 落盘(密钥全局脱敏)
    with open(os.path.join(out_dir, "run_log.json"), "w", encoding="utf-8") as f:
        json.dump(_scrub_secrets(run_log.to_dict(), run_log._secrets),
                  f, ensure_ascii=False, indent=2)

    S = store.stats_summary()
    return {
        "run_id": run_id, "out_dir": out_dir,
        "status": "failed" if failure else "ok", "failure": failure,
        "stats": {"n_retrieved": len(papers), "n_included": len(included),
                  "n_deep": len(deep)},
        "source_stats": _scrub_secrets(source_stats, run_log._secrets),
        "summary": S, "n_ranked_gaps": len(ranked), "n_hyps_reported": len(hyps),
        "outputs": dict(outputs),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Literature Gap & Hypothesis Discovery Agent v0.3")
    ap.add_argument("topic")
    ap.add_argument("--max-retrieve", type=int, default=DEFAULT_MAX_RETRIEVE)
    ap.add_argument("--screen-keep", type=int, default=DEFAULT_SCREEN_KEEP)
    ap.add_argument("--deep-extract", type=int, default=DEFAULT_DEEP_EXTRACT)
    ap.add_argument("--year-from", type=int, default=DEFAULT_YEAR_FROM)
    ap.add_argument("--max-gaps", type=int, default=6)
    ap.add_argument("--out-root", type=str, default=RUNS_DIR)
    ap.add_argument("--sources", type=str, default="europepmc,openalex",
                    help="逗号分隔:europepmc,openalex")
    args = ap.parse_args()
    result = run_pipeline(topic=args.topic, out_root=args.out_root,
                          max_retrieve=args.max_retrieve,
                          screen_keep=args.screen_keep,
                          deep_extract=args.deep_extract,
                          year_from=args.year_from, max_gaps=args.max_gaps,
                          sources=[s.strip() for s in args.sources.split(",") if s.strip()])
    print(json.dumps({k: v for k, v in result.items() if k != "outputs"},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

"""
Node 2:Gap Hunter v0.3
修复:
- _judge_resolution 实质化:题名命中只产生待审候选;须核对研究对象/关系/具体结局/条件
  与主题一致;跨主题(不同疾病/终点/机制)不得判为解决;无摘要/无证据标待核查
- 撤销 v0.2 的错误删除逻辑:单篇可触发停止但需强证据(实体+主题结局+直接中介/必要性
  且研究对象匹配);否则为 related_unresolved(保留,不算解决)
- 收窄必须明确"已解决部分 vs 剩余具体问题",实际改写描述
- 多源反查(EPMC+OpenAlex 各自方言);检查预算跨级跨源;预算耗尽标 truncated
- 不因"已返回N条"停止:依据相关命中与覆盖不足决定是否继续放宽
- 依据结构化 RQ(实体/结局/条件)构造检索词;绝不回退旧主题;禁止 AND ()
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .config import (RS_EXAMINED_PER_LEVEL, RS_MIN_RELEVANT_FOR_STOP,
                     RS_TOTAL_EXAMINE_BUDGET, now_iso)
from .schemas import Gap, Paper, ReverseSearchRecord, RunLog
from .gap_hunter_lex import (ENTITY_SYNONYMS, RESOLUTION_REL, RESOLUTION_ENDPOINTS,
                             TOPIC_OUTCOME_SYNONYMS, DESIGN_ZH)

OPPOSITE = {"increases": "decreases", "decreases": "increases"}

# v0.5.1 修复#3:检索契约允许状态白名单。
# ok      :请求有效,按记录及覆盖信息处理
# partial :保留可用记录,明确截断/失败部分
# failed / no_key / not_executed:分别保留真实原因
# 缺失、None、空字符串、意外值或结构不合法 → 契约异常,不得默认成功。
ALLOWED_SOURCE_STATUS = ("ok", "partial", "failed", "no_key", "not_executed")
# 这些状态表示"源未给出可用结果",记录不得进入已核查/零结果/高新颖性结论
NON_USABLE_STATUS = ("failed", "no_key", "not_executed")


def _is_allowed_status(status) -> bool:
    """状态是否在允许白名单内(严格字符串匹配,None/空/意外值均 False)。"""
    return isinstance(status, str) and status in ALLOWED_SOURCE_STATUS


def _status_contract_error(status) -> str:
    """把契约异常状态转成可记录的原因(不泄露内容)。"""
    if status is None:
        return "contract_error:status_missing_or_none"
    if isinstance(status, str) and status.strip() == "":
        return "contract_error:status_empty"
    return f"contract_error:unexpected_status={str(status)[:40]}"


def _is_usable_status(status) -> bool:
    """状态对应的响应是否含可审查结果:仅 ok/partial。

    v0.5.1 修复#3 补充:failed/no_key/not_executed 在契约白名单内(合法状态),
    但表示"源未给出可用结果"——必须计源失败,其(空)记录不得进入已核查、
    零结果或高新颖性结论。契约合法 ≠ 结果可用。
    """
    return status in ("ok", "partial")


def _entity_terms(entity: str) -> List[str]:
    return ENTITY_SYNONYMS.get(entity, [])


class GapHunter:
    MIN_PAPERS = 5

    def __init__(self, store, run_log: RunLog, plan=None,
                 sources: Optional[List[str]] = None):
        self.store = store
        self.log = run_log
        self.plan = plan
        self.rq = plan.rq if plan else None
        self.sources = sources or ["europepmc"]
        # 主题结局词(来自当前主题 RQ;跨主题判定用)
        self.topic_outcomes = set()
        if self.rq:
            for o in self.rq.outcomes:
                self.topic_outcomes.add(o.lower())
            for syns in TOPIC_OUTCOME_SYNONYMS.values():
                for s in syns:
                    self.topic_outcomes.add(s.lower())

    # ================================================= 候选生成
    def discover(self, max_gaps: int = 6) -> List[Gap]:
        gaps: List[Gap] = []
        papers = self.store.analysis_set("deep")
        gaps.extend(self._contradiction_gaps(papers))
        gaps.extend(self._mechanism_gaps(papers))
        gaps.extend(self._context_gap(papers))
        gaps.extend(self._translational_gap(papers))
        gaps.extend(self._methodological_gap(papers))
        gaps.extend(self._insufficient_gap(papers))
        seen, uniq = set(), []
        for g in gaps:
            key = re.sub(r"\s", "", g.description)[:60]
            if key not in seen:
                seen.add(key)
                uniq.append(g)
        for i, g in enumerate(uniq[:max_gaps], 1):
            g.gap_id = f"GAP_{i:03d}"
        self.log.log("GapHunter", "candidate_generation", "ok",
                     f"candidates={len(uniq)} types={[g.gap_type for g in uniq]}")
        return uniq[:max_gaps]

    # ---------------- (a) 冲突:三元组+终点极性(v0.3 核心)
    def _contradiction_gaps(self, papers: List[Paper]) -> List[Gap]:
        gaps = []
        # 同主体+同具体终点+效益含义相反(improves vs worsens)才构成冲突
        by_pair = defaultdict(list)
        for e in self.store.evidences.values():
            t = e.triple or {}
            s, end = t.get("subject", ""), t.get("endpoint", "")
            eff = e.effect or ""
            if s and end and eff in ("improves", "worsens") and e.evidence_level == 1:
                by_pair[(s, end)].append(e)
        ranked = sorted(by_pair.items(), key=lambda kv: -len({e.paper_id for e in kv[1]}))
        n = 0
        for (subject, endpoint), group in ranked:
            inc = [e for e in group if e.effect == "improves"]
            dec = [e for e in group if e.effect == "worsens"]
            if not inc or not dec:
                continue
            inc_p, dec_p = {e.paper_id for e in inc}, {e.paper_id for e in dec}
            if inc_p & dec_p or not (inc_p and dec_p):
                continue
            designs = {self.store.paper(pid).study_design
                       for pid in (inc_p | dec_p)}
            gaps.append(Gap(
                gap_id="GAP_TMP", gap_type="contradiction",
                entity=subject, endpoint=endpoint,
                description=(f"在「{endpoint}」终点上,多篇 L1 证据对「{subject}」的效益方向相反:"
                             f"{len(inc_p)} 篇报道改善、{len(dec_p)} 篇报道恶化"
                             f"(证据来自 {'、'.join(DESIGN_ZH.get(d, d) for d in sorted(designs))}):"
                             f"哪些模型、剂量或亚群决定其方向?"),
                rationale=(f"三元组比对:subject={subject}, endpoint={endpoint}(极性归一后);"
                           f"improves {len(inc)} 条 vs worsens {len(dec)} 条,来自不同论文;"
                           f"终点已做效益/风险极性归一,同义方向(response↑ vs resistance↓)不判冲突"),
                supporting_evidence_ids=[e.evidence_id for e in inc[:3]],
                contradicting_evidence_ids=[e.evidence_id for e in dec[:3]],
                already_addressed=(f"现有研究已分别在各自模型中报告 {subject} 与 {endpoint} 的"
                                   f"方向性关联(见支持/反对证据);未在同一可比条件下对比"),
            ))
            n += 1
            if n >= 2:
                break
        return gaps

    # ---------------- (b) 机制
    def _mechanism_gaps(self, papers: List[Paper]) -> List[Gap]:
        """v0.4 修复#1(剩余):机制Gap按同一关系+终点聚合比较,而非实体词频。

        - 聚合键 = (subject, relation, endpoint):只有同一关系指向同一终点的推测
          才互相比较;同一实体指向不同终点/不同关系不合并;
        - 分来源类别:作者推测(L3 author_speculation)与直接结果(L1)分开计数,
          推测句不因含干预词而升级为直接验证计数;
        - 去重:按(论文, 片段, 关系)去重,同篇同句重复提及不增加独立计数;
          独立论文数按 paper_id 去重。
        """
        gaps = []
        evs = list(self.store.evidences.values())
        spec_papers, spec_ev = defaultdict(set), defaultdict(list)
        spec_frags = defaultdict(set)          # key -> {(paper_id, 片段)} 去重
        direct_papers = defaultdict(set)
        for e in evs:
            t = e.triple or {}
            s = t.get("subject", "")
            if not s:
                continue
            key = (s, t.get("relation", ""), t.get("endpoint", ""))
            if e.evidence_level == 3 and e.evidence_type == "author_speculation":
                frag = (e.location or e.claim[:60])[:60]
                if (e.paper_id, frag) in spec_frags[key]:
                    continue                    # 同篇同句重复提及不重复计数
                spec_frags[key].add((e.paper_id, frag))
                spec_papers[key].add(e.paper_id)
                spec_ev[key].append(e)
            elif e.evidence_level == 1 and e.mechanism_claimed:
                direct_papers[key].add(e.paper_id)
        n = 0
        for key, pids in sorted(spec_papers.items(), key=lambda kv: -len(kv[1])):
            entity, relation, endpoint = key
            n_spec, n_dir = len(pids), len(direct_papers.get(key, set()))
            if n_spec >= 2 and n_dir < n_spec:
                ep = endpoint or (self.rq.outcomes[0] if self.rq and self.rq.outcomes
                                  else "主题结局")
                rel_zh = {"increases": "升高", "decreases": "降低",
                          "negated": "无/否定关联"}.get(relation, relation or "关联")
                gaps.append(Gap(
                    gap_id="GAP_TMP", gap_type="mechanism", entity=entity,
                    endpoint=ep,
                    description=(f"「{entity}」{rel_zh}「{ep}」这一关系在 {n_spec} 篇独立论文中"
                                 f"以推测性表述提及(按论文/片段/关系去重),同关系同终点的"
                                 f"干预性验证仅 {n_dir} 篇:{entity} 是否为必要中介"
                                 f"(而非伴随变化)缺乏因果检验?"),
                    rationale=(f"同一关系({entity}→{ep})推测级表述 {n_spec} 篇"
                               f"(按论文去重);同关系干预验证 {n_dir} 篇;"
                               f"不同终点/不同关系的提及未合并入本 Gap"),
                    supporting_evidence_ids=[e.evidence_id for e in spec_ev.get(key, [])[:4]],
                    already_addressed=(f"{n_dir} 篇已有同关系摘要层可见的干预性实验;"
                                       f"相关性与共现已多次报道"),
                ))
                n += 1
                if n >= 2:
                    break
        return gaps

    # ---------------- (c) 情境(修正"主导人体再缺人体")
    def _context_gap(self, papers: List[Paper]) -> List[Gap]:
        non_review = [p for p in papers if p.study_design != "review"]
        if len(non_review) < self.MIN_PAPERS:
            return []
        dist = defaultdict(int)
        for p in non_review:
            dist[p.study_design] += 1
        dom = max(dist, key=dist.get)
        if dist[dom] / len(non_review) < 0.6 or dom == "unknown":
            return []
        missing = {"clinical": "前瞻性临床研究", "human": "独立人体队列验证",
                   "in_vivo": "体内动物模型", "in_vitro": "体外机制实验",
                   "bioinformatic": "独立队列数据验证"}.get(dom)
        if not missing:
            return []
        solved = "、".join(f"{DESIGN_ZH.get(d, d)}({n})" for d, n in dist.items())
        sup = [self.store.evidences_by_paper(p.paper_id)[0].evidence_id
               for p in non_review[:3] if self.store.evidences_by_paper(p.paper_id)]
        return [Gap(
            gap_id="GAP_TMP", gap_type="context",
            description=(f"非综述研究集中于「{DESIGN_ZH.get(dom, dom)}」({dist[dom]}/{len(non_review)}):"
                         f"已报道机制在 {missing} 等其他研究情境中是否成立、边界条件是什么?"),
            rationale=f"设计分布:{dict(dist)};主导占比 {dist[dom]/len(non_review):.0%}",
            supporting_evidence_ids=sup,
            already_addressed=f"现有研究已在 {solved} 中报道相关现象;本 Gap 仅针对跨情境验证缺失",
        )]

    # ---------------- (d) 转化
    def _translational_gap(self, papers: List[Paper]) -> List[Gap]:
        non_review = [p for p in papers if p.study_design != "review"]
        if len(non_review) < self.MIN_PAPERS:
            return []
        n_clin = sum(1 for p in non_review if p.study_design == "clinical")
        n_basic = sum(1 for p in non_review if p.study_design in ("in_vitro", "in_vivo", "bioinformatic"))
        if n_basic < 4 or n_basic < 2 * max(n_clin, 1):
            return []
        sup = [self.store.evidences_by_paper(p.paper_id)[0].evidence_id
               for p in non_review if p.study_design in ("in_vitro", "in_vivo")
               and self.store.evidences_by_paper(p.paper_id)][:3]
        topic = self.rq.topic_zh if self.rq else "当前主题"
        return [Gap(
            gap_id="GAP_TMP", gap_type="translational",
            description=(f"基础/生信研究({n_basic} 篇)与临床证据({n_clin} 篇)比例失衡:"
                         f"主题相关机制策略({topic})是否已在临床可评估情境中验证?"),
            rationale=f"体外/体内/生信 {n_basic} vs 临床 {n_clin}(摘要层设计分类)",
            supporting_evidence_ids=sup,
            already_addressed=f"{n_clin} 篇临床类研究已存在;基础机制已有较多报道",
        )]

    # ---------------- (e) 方法学(v0.3:类别≠单模型)
    def _methodological_gap(self, papers: List[Paper]) -> List[Gap]:
        non_review = [p for p in papers if p.study_design != "review"]
        if len(non_review) < self.MIN_PAPERS:
            return []
        # 仅统计"摘要可确认单一类别且模型证据为 identified_categories"的论文;
        # 明确区分:识别到1类模型 ≠ 原文确认仅1个模型
        single_cat = [p for p in non_review if len(p.models) == 1]
        multi = [p for p in non_review if len(p.models) >= 2]
        unreported = [p for p in non_review if not p.models]
        if not single_cat or len(single_cat) / len(non_review) < 0.6:
            return []
        topic = self.rq.topic_zh if self.rq else "当前主题"
        sup = [self.store.evidences_by_paper(p.paper_id)[0].evidence_id
               for p in single_cat[:3] if self.store.evidences_by_paper(p.paper_id)]
        return [Gap(
            gap_id="GAP_TMP", gap_type="methodological",
            description=(f"{len(single_cat)}/{len(non_review)} 篇非综述研究摘要仅可识别单一模型类别"
                         f"(注意:类别数≠原文确认的单模型数,单模型结论需全文核对):"
                         f"在多模型跨平台统一验证下,主题结论能否区分直接效应与伴随改变?"),
            rationale=(f"单一类别 {len(single_cat)} 篇;多类别 {len(multi)} 篇;"
                       f"未报告模型线索 {len(unreported)} 篇(未知,不计为缺陷)"),
            supporting_evidence_ids=sup,
            already_addressed=(f"{len(multi)} 篇已使用多模型(摘要可识别);单模型研究内部对照通常完备,缺跨模型一致性验证"),
        )]

    # ---------------- (f) 证据不足
    def _insufficient_gap(self, papers: List[Paper]) -> List[Gap]:
        non_review = [p for p in papers if p.study_design != "review"]
        if len(non_review) < self.MIN_PAPERS:
            return []
        weak = [p for p in non_review if "statistics" not in (p.quality_notes or "")]
        if len(weak) / len(non_review) < 0.6:
            return []
        topic = self.rq.topic_zh if self.rq else "当前主题"
        sup = [self.store.evidences_by_paper(p.paper_id)[0].evidence_id
               for p in weak[:3] if self.store.evidences_by_paper(p.paper_id)]
        return [Gap(
            gap_id="GAP_TMP", gap_type="insufficient",
            description=(f"{len(weak)}/{len(non_review)} 篇非综述研究摘要未报告效应量/统计细节:"
                         f"现有证据是否足以支持主题({topic})关键结论的共识?"),
            rationale="统计信号缺失比例高(摘要层信号,非统计缺失判定)",
            supporting_evidence_ids=sup,
            already_addressed=f"{len(non_review)-len(weak)} 篇有统计信号;需全文复核确认",
            limitation="摘要层统计信号检测不完全等价统计缺失,需全文复核",
        )]

    # ================================================= 反查(v0.3:多源+预算+实质判定)
    def reverse_search(self, gaps: List[Gap], year_from: int = 2015,
                       openalex_fn=None) -> List[Gap]:
        """多源反查。openalex_fn(query, n) -> (records, meta) 由 retriever 提供;
        EPMC 直连。预算跨级跨源;题名命中仅产生待审候选。"""
        from .retriever import europepmc_search
        context_terms = []
        if self.rq:
            context_terms = list(self.rq.topic_terms_en)[:4]

        for g in gaps:
            entity = g.entity or self._main_entity_from_desc(g)
            terms = _entity_terms(entity) or context_terms[:3]
            rec = ReverseSearchRecord(purpose="gap_reverse",
                                      sources=list(self.sources))
            rec.date = now_iso()
            levels = self._build_levels(terms, context_terms, g)
            examined_total = 0
            any_failed_source = False
            relevant: List[Dict] = []
            stop_looked = False

            for lv in levels:
                per_source = {}
                lv_rel = []
                for src in self.sources:
                    q = lv["query"].get(src, "")
                    if not q:
                        per_source[src] = {"request_status": "skipped",
                                           "note": "该级无此源方言"}
                        continue
                    if examined_total >= RS_TOTAL_EXAMINE_BUDGET:
                        per_source[src] = {"request_status": "skipped",
                                           "note": f"总检查预算({RS_TOTAL_EXAMINE_BUDGET})耗尽"}
                        continue
                    if src == "europepmc":
                        n_entries_before = len(self.log.entries)
                        # v0.4 修复#4:统一结构化契约 (records, meta_info)
                        recs, ep_meta = europepmc_search(q, RS_EXAMINED_PER_LEVEL, self.log, year_from)
                        # 失败检测:仅 failed/no_key/not_executed 视为源失败;
                        # partial(结果截断但记录可用)照常审查,并记录截断状态。
                        failed_in_log = any(
                            e.get("tool") == "europepmc" and e.get("status") == "failed"
                            for e in self.log.entries[n_entries_before:])
                        ep_status = ep_meta.get("status")
                        # v0.5 修复#2:未知状态不得默认成功(unknown 同样计源失败)
                        # v0.5.1 修复#3:允许状态白名单。缺失/None/空/意外值
                        # 均视为契约异常,不得默认成功。
                        # v0.5.1 修复#3 补充:failed/no_key/not_executed 虽是合法
                        # 契约状态,但表示"源未给出可用结果"——契约合法≠结果可用,
                        # 必须计源失败,不得进入已核查/零结果/高新颖性结论。
                        if (not _is_usable_status(ep_status)) or failed_in_log:
                            any_failed_source = True
                            if _is_allowed_status(ep_status):
                                ek = ep_meta.get("error_kind") or ep_status
                                ps_status = ep_status
                            else:
                                ek = ep_meta.get("error_kind") or _status_contract_error(ep_status)
                                ps_status = "failed"
                            per_source[src] = {"request_status": ps_status,
                                               "query": q,
                                               "error_kind": ek,
                                               "note": "源失败(请求失败或状态契约异常):不计为零结果"}
                            continue
                        recs = recs or []
                        last_src_status = ep_status if ep_status else "unknown"
                        last_src_truncated = bool(ep_meta.get("truncated", False))
                        last_src_trunc_reason = ep_meta.get("truncation_reason", "")
                    else:  # openalex
                        if openalex_fn is None:
                            per_source["openalex"] = {"request_status": "not_configured",
                                                      "note": "OpenAlex 回调未配置"}
                            continue
                        recs, meta = openalex_fn(q, RS_EXAMINED_PER_LEVEL)
                        # v0.5 修复#2:统一 partial 契约。仅 failed/no_key/
                        # not_executed/unknown 视为源失败;partial(结果截断但
                        # 记录可用)照常审查可用记录,并记录截断状态与原因。
                        # 未知状态不得默认成功。
                        # v0.5.1 修复#3:允许状态白名单;缺失/None/空/意外值
                        # 视为契约异常,不默认成功。failed/no_key/not_executed
                        # 合法但无可用结果:契约合法≠结果可用,同样计源失败。
                        oa_status = meta.get("status")
                        if not _is_usable_status(oa_status):
                            any_failed_source = True
                            if _is_allowed_status(oa_status):
                                ek = meta.get("error_kind") or oa_status
                                ps_status = oa_status
                            else:
                                ek = meta.get("error_kind") or _status_contract_error(oa_status)
                                ps_status = "failed"
                            per_source["openalex"] = {
                                "request_status": ps_status,
                                "error_kind": ek,
                                "note": "源失败或状态契约异常:不计为零结果"}
                            continue
                        # partial/ok:记录照常审查,partial 保留截断状态与原因
                        last_src_status = oa_status if oa_status else "unknown"
                        last_src_truncated = bool(meta.get("truncated", False))
                        last_src_trunc_reason = meta.get("truncation_reason", "")
                    examined = recs[:RS_EXAMINED_PER_LEVEL]
                    examined_total += len(examined)
                    rel_here = []
                    for r in examined:
                        judg = self._judge_resolution(r, g, entity)
                        item = {"title": (r.get("title") or "")[:160],
                                "year": ((r.get("journalInfo") or {}).get("yearOfPublication")
                                         or r.get("publication_year")),
                                "doi": (r.get("doi") or "").replace("https://doi.org/", ""),
                                "pmid": (r.get("pmid") or r.get("id") or ""),
                                "source": src,
                                "judgment": judg["verdict"],
                                "reason": judg["reason"]}
                        # v0.4 修复#3:resolves 附可定位原文(题名/DOI/PMID)+逐项匹配理由
                        if judg["verdict"] == "resolves":
                            item["locator"] = {
                                "title": (r.get("title") or "")[:160],
                                "doi": (r.get("doi") or "").replace("https://doi.org/", ""),
                                "pmid": (r.get("pmid") or r.get("id") or ""),
                            }
                            item["matched"] = judg.get("matched", [])
                        rel_here.append(item)
                        if judg["verdict"] == "resolves":
                            relevant.append(item)
                            stop_looked = True
                        elif judg["verdict"] in ("related_unresolved", "needs_manual"):
                            relevant.append(item)   # 保留"相关但未解决"
                    per_source[src] = {"request_status": last_src_status,
                                       "coverage": ("partial" if last_src_truncated else "complete"),
                                       "truncated": last_src_truncated,
                                       "truncation_reason": last_src_trunc_reason,
                                       "query": q,
                                       "examined": len(examined),
                                       "relevant": rel_here}
                rec.query_levels.append({"level": lv["level"], "strategy": lv["strategy"],
                                         "per_source": per_source})
                                # v0.4 修复#5:停止条件依据真实相关性。
                # 仅当发现直接解决研究(resolves)或预算耗尽才停;
                # 相关未解决/待人工核查不触发提前停止(避免"前几条相关就停")。
                if stop_looked:
                    break
                if examined_total >= RS_TOTAL_EXAMINE_BUDGET:
                    break
                # 不再因"相关未解决数量>=3"提前放宽停止:全无关/仅相关未解决
                # 仍需继续检索或最终标覆盖不足。

            rec.n_examined_total = examined_total
            rec.relevant_papers = relevant
            resolves = [x for x in relevant if x["judgment"] == "resolves"]
            needs_manual = [x for x in relevant if x["judgment"] == "needs_manual"]

            # ---- 状态判定(实质化)
            if any_failed_source and examined_total == 0:
                rec.final_status = "failed"
                rec.coverage = "partial_source_failure"
                rec.decision = "unresolved"
                rec.reasoning = "全部启用源请求失败:新颖性待评估,不得据此加分"
                g.reverse_search = rec.to_dict()
                g.limitation = "反查请求失败(全部源),新颖性未核查"
                continue
            if any_failed_source:
                rec.final_status = "partial"
                rec.coverage = "partial_source_failure"
            else:
                rec.final_status = "ok"
                # v0.4 修复#5:若审查了预算内全部结果仍无任何相关命中,标覆盖不足
                n_related = len(relevant)
                if n_related == 0 and examined_total > 0:
                    rec.coverage = "insufficient_relevance"
                else:
                    rec.coverage = ("truncated_budget"
                                    if examined_total >= RS_TOTAL_EXAMINE_BUDGET
                                    else "complete_in_budget")

            if resolves:
                g.status = "dropped"
                g.status_reason = (f"反查发现 {len(resolves)} 篇通过实质判定(对象/关系/结局/条件匹配)"
                                   f"的直接解决研究;首个:{resolves[0]['title'][:90]}")
                rec.decision = "dropped"
                rec.reasoning = "存在直接且充分回答该问题的研究(单篇强证据即触发停止)"
            elif needs_manual:
                g.status = "candidate"
                g.limitation = (f"反查发现 {len(needs_manual)} 篇待人工核查研究"
                                f"(题名/摘要信息不足以自动判定):新颖性判断悬置,待人工复核")
                rec.decision = "needs_manual_review"
                rec.reasoning = "存在疑似相关研究但证据不足以自动判定解决与否"
            else:
                g.status = "candidate"
                rec.decision = "unresolved"
                rec.reasoning = (f"两级检索共审查 {examined_total} 条题名,未发现直接解决研究;"
                                 f"结论限于当前检索范围(题名+摘要存在性判定),不等于'没有研究'")
                g.limitation = "反查未发现直接解决研究(当前范围);题名级筛查需人工复核"
            g.reverse_search = rec.to_dict()
            self.log.log("GapHunter", "reverse_search", "ok",
                         f"{g.gap_id} entity={entity} examined={examined_total} "
                         f"resolves={len(resolves)} needs_manual={len(needs_manual)} "
                         f"coverage={rec.coverage} status={g.status}")
        return gaps

    # ---------------- 检索级构造(双源方言;禁 AND ())
    def _build_levels(self, terms: List[str], context_terms: List[str], g: Gap) -> List[Dict]:
        if not terms:
            terms = context_terms or [self.rq.topic_zh if self.rq else "topic"]
        syn = "(" + " OR ".join(f'"{t}"' for t in terms[:4]) + ")"
        end_q = "(" + " OR ".join(RESOLUTION_ENDPOINTS) + ")"
        rel_q = "(" + " OR ".join(RESOLUTION_REL) + ")"
        ctx = "(" + " OR ".join(f'"{c}"' for c in context_terms[:3]) + ")"
        epmc = [
            {"level": 1, "strategy": "narrow:实体+解决型关系+主题结局",
             "query": {"europepmc": f"{syn} AND {rel_q} AND {end_q}"}},
            {"level": 2, "strategy": "medium:实体+结局+主题上下文",
             "query": {"europepmc": f"{syn} AND {end_q} AND {ctx}"}},
            {"level": 3, "strategy": "broad:实体+机制",
             "query": {"europepmc": f"{syn} AND (mechanism OR pathway)"}},
        ]
        # OpenAlex 方言(filter 语法)
        oa_base = "title_and_abstract.search:" + " ".join(
            f'"{t}"' if " " in t else t for t in terms[:3])
        for lv in epmc:
            lv["query"]["openalex"] = oa_base
        # 语法校验(禁空组)
        for lv in epmc:
            for src, q in lv["query"].items():
                if re.search(r"\b(AND|OR|NOT)\s*\(\s*\)", q, re.I) or q.count("(") != q.count(")"):
                    self.log.log("GapHunter", "reverse_query_syntax", "error",
                                 f"level={lv['level']} src={src} 无效检索式(已丢弃)")
                    lv["query"][src] = ""
        return epmc

    def _main_entity_from_desc(self, g: Gap) -> str:
        m = re.findall(r"「(.+?)」", g.description)
        return m[0] if m else ""

    # ---------------- 实质判定(v0.3 核心:不再"题名命中即解决")
    def _judge_resolution(self, rec: dict, g: Gap, entity: str) -> Dict:
        """核对:研究对象/关系/具体结局/条件与当前 Gap 一致才可判 resolves。
        题名命中 → 至多 needs_manual(待审候选);
        跨主题(不同疾病/不同终点/不同机制)→ related_unresolved 或 unrelated;
        无摘要且题名不足以判断 → needs_manual。"""
        title = (rec.get("title") or "")
        abstract = rec.get("abstractText") or rec.get("abstract") or ""
        # OpenAlex 记录可能已重建 abstract
        if not abstract and isinstance(rec.get("abstract_rebuilt"), str):
            abstract = rec["abstract_rebuilt"]
        text = f"{title} {abstract}".lower()
        title_l = title.lower()

        # 机制路径判据:优先用 Gap 实体;Gap 无实体时用主题实体词(RQ.entities)
        syn = [s.lower() for s in _entity_terms(entity)] if entity else []
        if not syn and self.rq:
            syn = [e.lower() for e in self.rq.entities[:4]]
        if not syn:
            syn = [entity.lower()] if entity else []
        has_entity_title = any(s in title_l for s in syn)
        has_entity_anywhere = any(s in text for s in syn)

        # 主题结局词命中:需 Gap 特有终点 + 主题结局同义词;
        # 但"response/resistance/efficacy"等通用词在任意肿瘤研究都出现,
        # 不能单独作为"结局匹配"证据——需结合主题特有结局(如缺氧免疫的
        # immune evasion/resistance to immunotherapy)或 Gap 具体终点。
        outcome_words = set()
        end = (g.endpoint or "").lower()
        if end:
            outcome_words.add(end)
        # 主题特有结局(非通用词):来自 RQ.outcomes 中非 response/resistance 的
        for o in (self.rq.outcomes if self.rq else []):
            ol = o.lower()
            if ol not in ("response", "resistance", "efficacy"):
                outcome_words.add(ol)
        # 通用词仅当 Gap 有具体终点时才作为补充
        has_outcome = any(w and w in text for w in outcome_words) if outcome_words else False
        # 若只有通用结局词且无 Gap 具体终点,则结局匹配不足(避免跨亚型误判)
        generic_only = bool(re.search(r"\b(response|resistance|efficacy)\b", text)) and not has_outcome

        # 直接解决表述(中介/必要性/因果 + 干预/试验)
        has_direct = bool(re.search(
            r"\b(mediat\w+|necessar\w+|required|causal|essential|dispensable|"
            r"we (show|demonstrate) that .{0,40}(is|are) (required|necessary))\b", text))
        has_intervention = bool(re.search(
            r"\b(randomi[sz]ed|phase [ii]+|inhibitor|blockade|knockout|knockdown|"
            r"combination (therapy|treatment)|clinical trial)\b", text))
        # ---- v0.4 修复#3:无定论/否定/不确定性作用范围守卫
        # 若原文明确表示"仍未知/无定论/建议未来检验/尚未确立/有待确定",
        # 则该文不能判为已解决当前 Gap(至多 related_unresolved 或 needs_manual)。
        uncertainty_pat = re.compile(
            r"\b(remains? (unclear|unknown|to be (determined|established|elucidated))|"
            r"not (yet )?(established|known|determined|clear)|"
            r"needs? further (investigation|study|research)|"
            r"warrants? (further|future) (investigation|study)|"
            r"future (studies|work|research) (are|is) needed|"
            r"whether .{0,60} remains? (unclear|unknown)|"
            r"尚未(明确|确定|阐明|解决)|仍(未知|不清楚|无定论)|有待(研究|确定|阐明)|"
            r"需要进一步(研究|验证|探索)|建议(未来|今后)(研究|检验|验证)|"
            r"机制(尚|仍)不(清楚|明确|确定))\b", re.I)
        has_uncertainty = bool(uncertainty_pat.search(text))
        # 否定作用范围:明确否定该机制/必要性(not required/not necessary/does not mediate)
        negation_pat = re.compile(
            r"\b(not (required|necessary|essential|mediat\w*|involv\w*)|"
            r"does not (mediate|involve|require)|"
            r"is not (required|necessary|essential)|"
            r"不(需要|必需|介导|参与|涉及))\b", re.I)
        has_negation = bool(negation_pat.search(text))

        # ---- 跨主题负例判定:不同疾病域/不同肿瘤亚型/不同终点
        # 非肿瘤疾病域
        off_topic_disease = bool(re.search(
            r"\b(reproductive|infertility|hpv|pregnan\w+|endometrio\w+|cardiac|diabetes|"
            r"alzheimer|parkinson|asthma|copd|ibd|liver fibrosis|kidney|"
            r"cardiomyocyte|ocular|choroidal|retinal)\b", text))
        # 其他肿瘤亚型(当前主题域外的癌种):若主题未命中该癌种,视为跨主题
        other_cancer = bool(re.search(
            r"\b(gastric|lung|breast|colorectal|colon|pancreatic|prostate|ovarian|"
            r"hepatocell\w+|renal cell|bladder|head and neck|esophageal|"
            r"glioblastoma|leukemia|lymphoma|sarcoma)\b", text))
        # Gap 终点 vs 该文终点(具体终点词都在文中,但方向/主题不同)
        topic_terms = {t.lower() for t in (self.rq.topic_terms_en if self.rq else [])}

        if (off_topic_disease or other_cancer) and not (has_outcome and has_direct):
            return {"verdict": "unrelated",
                    "reason": (f"跨主题负例:研究对象域不匹配(非当前主题疾病/其他肿瘤亚型),"
                               f"实体同现不足以判解决")}
        if other_cancer and generic_only:
            return {"verdict": "related_unresolved",
                    "reason": "其他肿瘤亚型研究:仅通用结局词(response/resistance)命中,"
                              "无主题特有结局,相关但未解决当前主题问题"}
        if not has_entity_anywhere:
            return {"verdict": "unrelated", "reason": "题名/摘要未检出实体同义词"}
        if not abstract:
            # 无摘要:题名命中只产生待审候选
            if has_entity_title and has_outcome and has_direct:
                return {"verdict": "needs_manual",
                        "reason": "无摘要可核;题名同时含实体+结局+直接表述,需人工全文核查"}
            return {"verdict": "needs_manual" if has_entity_title else "unrelated",
                    "reason": "无摘要且题名信息不足:待人工核查" if has_entity_title else "题名未检出实体"}
        # 主题条件词判据(如 ICB/免疫检查点):resolves 需同时满足
        # 主题实体 + 主题条件 + 主题结局 + 直接表述 + 干预
        cond_words = set()
        if self.rq:
            for c in self.rq.conditions:
                cond_words.add(c.lower())
        has_condition = any(cw and cw in text for cw in cond_words) if cond_words else True
        # 主题核心机制实体判据:若 RQ.entities 含非泛化机制词(如 macrophage polarization),
        # resolves 需命中它(避免仅凭泛化实体如 exosome 命中就判解决)
        mech_entities = []
        if self.rq:
            for e in self.rq.entities:
                el = e.lower()
                if el not in ("exosome", "exosomes", "extracellular vesicle",
                              "extracellular vesicles", "mechanism", "causal", "mediates"):
                    mech_entities.append(el)
        has_mech_entity = (any(m and m in text for m in mech_entities)
                           if mech_entities else True)
        # 有摘要:实质核对。resolves 必须同时满足:
        # 主题实体(机制路径)+主题条件+主题核心机制实体+主题结局+直接表述+干预
        if (has_entity_anywhere and has_condition and has_mech_entity
                and has_outcome and has_direct and has_intervention
                and not has_uncertainty and not has_negation):
            # v0.4 修复#3:resolves 附逐项匹配理由(每项命中的具体词/短语)
            matched = []
            for label, flag, words in (
                ("主题实体(机制路径)", has_entity_anywhere, syn),
                ("主题条件", has_condition, sorted(cond_words)),
                ("主题核心机制实体", has_mech_entity, mech_entities),
                ("主题结局", has_outcome, sorted(outcome_words)),
                ("直接中介/必要性表述", has_direct, ["mediat/necessar/required/causal/essential"]),
                ("干预/试验设计", has_intervention, ["randomized/phase/inhibitor/blockade/knockout/clinical trial"]),
            ):
                if flag:
                    matched.append(f"{label}:{','.join(w for w in words if w)}")
            return {"verdict": "resolves",
                    "reason": (f"主题实体(机制路径)+主题条件+主题核心机制实体+主题结局+"
                               f"直接中介/必要性表述+干预/试验设计均在原文中;对象域与当前主题一致"),
                    "matched": matched}
        if (has_entity_anywhere and has_condition and has_mech_entity
                and has_outcome and has_direct and has_intervention
                and (has_uncertainty or has_negation)):
            return {"verdict": "needs_manual",
                    "reason": ("实体/结局/直接表述/干预均命中,但原文含无定论或否定表述"
                               "(仍未知/建议未来检验/not required/does not mediate):"
                               "不能判为已解决,待人工核查作用范围")}
        if has_outcome and has_direct and has_intervention and not (
                has_entity_anywhere and has_condition and has_mech_entity):
            return {"verdict": "related_unresolved",
                    "reason": "结局与解决性表述匹配但未同时检出主题实体/条件/核心机制实体"
                              "(机制或干预路径不同):相关但未解决当前主题问题"}
        if has_entity_anywhere and has_outcome and (has_direct or has_intervention):
            return {"verdict": "needs_manual",
                    "reason": "实体+结局匹配且有解决性信号,但要素不全(缺直接表述或干预设计):待人工核查"}
        if has_entity_anywhere:
            return {"verdict": "related_unresolved",
                    "reason": f"含实体但未同时覆盖主题结局与解决性表述:相关但未解决(保留记录)"}
        return {"verdict": "unrelated", "reason": "仅背景级同现"}

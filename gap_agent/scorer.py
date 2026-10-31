"""
Gap 排序 v0.3
修复:
- N 维:按反查真实状态(failed/partial/needs_manual/truncated)分级;
  needs_manual/truncated 不得自动高新颖性;小样本题名审查显式降置信
- E 维:支持证据按独立论文去重+L1 计数+核验状态
- I/T:类型先验但注明(不变),F 未评估不参与(不变)
"""
from __future__ import annotations

from typing import Dict, List

from .config import GAP_WEIGHTS, SCORE_TO_100
from .schemas import Gap, RunLog

PENDING = "待评估"
WMAP = {"N_novelty": "novelty", "I_significance": "significance",
        "E_evidence": "evidence", "T_testability": "testability",
        "F_feasibility": "feasibility"}


def _clamp(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(1.0, min(5.0, v))


class GapScorer:
    def __init__(self, store, run_log: RunLog):
        self.store = store
        self.log = run_log

    def score_all(self, gaps: List[Gap]) -> List[Gap]:
        for g in gaps:
            g.scores = self.score(g)

        def sort_key(g: Gap):
            s = g.scores or {}
            pw = s.get("partial_weighted")
            return (pw if pw is not None else -1,
                    len(g.supporting_evidence_ids))
        ranked = sorted([g for g in gaps if g.status != "dropped"], key=sort_key, reverse=True)
        self.log.log("GapScorer", "weighted_score", "ok",
                     f"scored={len(gaps)} ranked={len(ranked)} "
                     f"pending_manual={sum(1 for g in gaps if (g.reverse_search or {}).get('decision')=='needs_manual_review')}")
        return ranked

    def score(self, g: Gap) -> Dict:
        n_sup = len(g.supporting_evidence_ids)
        rs = g.reverse_search or {}
        dims: Dict[str, Dict] = {}

        # ---------------- N 新颖性(v0.3:多源真实状态分级)
        n_dim = {"evaluated": False, "value": None, "note": ""}
        rs_status = rs.get("final_status", "not_executed")
        decision = rs.get("decision", "")
        examined = rs.get("n_examined_total", 0)
        coverage = rs.get("coverage", "")
        if rs_status == "failed":
            n_dim["note"] = "反查请求失败(全部源):新颖性待评估(禁止失败转高分)"
        elif rs_status == "not_executed":
            n_dim["note"] = "反查未执行:新颖性待评估"
        elif decision == "needs_manual_review":
            n_dim["evaluated"] = True
            n_dim["value"] = 2.5
            n_dim["note"] = (f"发现疑似相关研究但无法自动判定(待人工核查):"
                             f"新颖性按中低值处理,悬置不加分")
        else:
            n_resolves = sum(1 for x in rs.get("relevant_papers", [])
                             if x.get("judgment") == "resolves")
            n_similar = len(rs.get("relevant_papers", []))
            if n_resolves:
                n_dim["evaluated"] = True
                n_dim["value"] = 1.0
                n_dim["note"] = f"发现 {n_resolves} 篇直接解决研究(应已触发停止)"
            elif n_similar == 0 and examined >= 15:
                n_dim["evaluated"] = True
                n_dim["value"] = 4.0
                n_dim["note"] = (f"审查 {examined} 条题名(双源)未见直接解决;"
                                 f"题名级筛查,置信中等;范围限当前检索")
            elif n_similar == 0:
                n_dim["evaluated"] = True
                n_dim["value"] = 3.5
                n_dim["note"] = (f"仅审查 {examined} 条题名未见解决研究:小样本审查,"
                                 f"新颖性判断降置信(不得宣称充分完成核查)")
            elif n_similar <= 2:
                n_dim["evaluated"] = True
                n_dim["value"] = 3.5
                n_dim["note"] = f"发现 {n_similar} 篇相关未解决研究"
            else:
                n_dim["evaluated"] = True
                n_dim["value"] = 3.0
                n_dim["note"] = f"发现 {n_similar} 篇相关研究:新颖性为条件性增量"
            if coverage == "partial_source_failure":
                n_dim["value"] = max(1.0, (n_dim["value"] or 3.0) - 0.5)
                n_dim["note"] += ";部分源失败:覆盖不完整,再降 0.5"
            elif coverage == "truncated_budget":
                n_dim["note"] += ";预算截断:检查不完整,如实标注"
        dims["N_novelty"] = n_dim

        # ---------------- I / E / T / F(同 v0.2 逻辑,E 按独立论文)
        base = {"contradiction": 4.0, "mechanism": 4.0, "translational": 3.6,
                "methodological": 3.2, "context": 3.4, "insufficient": 3.0}.get(g.gap_type, 3.0)
        dims["I_significance"] = {
            "evaluated": True, "value": round(_clamp(base + min(n_sup, 4) * 0.15), 2),
            "note": f"类型先验 {base} + 支持证据 {n_sup} 条(规则先验,非专家评审)"}

        sup_papers, l1_cnt, verified_cnt, abstract_only = set(), 0, 0, 0
        for eid in g.supporting_evidence_ids:
            e = self.store.evidences.get(eid)
            if not e:
                continue
            sup_papers.add(e.paper_id)
            if e.evidence_level == 1:
                l1_cnt += 1
            if e.verified:
                verified_cnt += 1
            if self.store.paper(e.paper_id).analysis_basis == "abstract":
                abstract_only += 1
        if sup_papers:
            dims["E_evidence"] = {
                "evaluated": True,
                "value": round(_clamp(2.0 + min(len(sup_papers), 6) * 0.3 + l1_cnt * 0.15), 2),
                "note": (f"支持证据 {len(sup_papers)} 篇独立论文;L1 {l1_cnt} 条;"
                         f"{abstract_only} 篇仅摘要层;定位核验通过 {verified_cnt} 条")}
        else:
            dims["E_evidence"] = {"evaluated": False, "value": None,
                                  "note": "无关联支持证据(分布统计型);E 维待评估"}

        t_prior = {"contradiction": 4.0, "mechanism": 4.5, "methodological": 3.8,
                   "context": 3.5, "translational": 3.2, "insufficient": 2.8}.get(g.gap_type, 3.0)
        dims["T_testability"] = {"evaluated": True, "value": t_prior,
                                 "note": "按类型先验(非实验设计评审);配对状态见 pairing_status"}
        dims["F_feasibility"] = {"evaluated": False, "value": None,
                                 "note": "缺团队资源信息:待评估;禁止固定分参与总分"}

        evaluated = {k: v for k, v in dims.items() if v["evaluated"]}
        w_cov = sum(GAP_WEIGHTS[WMAP[k]] for k in evaluated if k in WMAP)
        out = {"dimensions": dims, "n_evaluated": len(evaluated),
               "weight_coverage": round(w_cov, 2), "complete": len(evaluated) == 5}
        if evaluated:
            partial_raw = sum(GAP_WEIGHTS[WMAP[k]] * v["value"] for k, v in evaluated.items())
            out["partial_weighted"] = round(partial_raw / w_cov * SCORE_TO_100, 1)
            out["partial_note"] = (f"部分评分:{len(evaluated)}/5 维,覆盖权重 {w_cov:.0%},"
                                   f"按已评估权重归一;非完整 S_gap")
        else:
            out["partial_weighted"] = None
            out["partial_note"] = "全部维度待评估"
        out["total_100"] = out["partial_weighted"] if out["complete"] else None
        out["total_note"] = ("完整五维(F 仍为规则先验)" if out["complete"]
                             else "未输出完整总分:F 待评估;partial_weighted 为暂定分")
        out["uncertainty"] = ("摘要层证据+规则先验;F 未评估;题名级反查"
                              f"(coverage={coverage or 'n/a'});评分为决策辅助,非创新性证明")
        return out

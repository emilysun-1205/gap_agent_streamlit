"""
Module 3:证据库 v0.2
修复:
- count_terms():每次从原始数据重算,无副作用;按"去重论文"计数(同篇论文多次提及同一术语只计 1)
- 每个统计函数显式声明分析集合(analyses set)与分母
- 术语表保留原文与映射依据
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from typing import Dict, List

from .schemas import Evidence, Gap, Hypothesis, Paper, RunLog

TERM_MAP = {
    "pd-1": "PD-1", "pd-l1": "PD-L1", "programmed death 1": "PD-1",
    "pd1": "PD-1", "pdl1": "PD-L1", "anti-pd-1": "anti-PD-1", "anti-pd-l1": "anti-PD-L1",
    "immune checkpoint blockade": "ICB(免疫检查点阻断)",
    "nivolumab": "Nivolumab(anti-PD-1)", "pembrolizumab": "Pembrolizumab(anti-PD-1)",
    "tumor hypoxia": "肿瘤缺氧", "tumour hypoxia": "肿瘤缺氧",
    "t cell exhaustion": "T细胞耗竭",
}
# v0.2 抽取器输出的实体标签本身已是标准化标签;TERM_MAP 仅兜底原始词


def normalize_term(raw: str) -> str:
    r = (raw or "").strip().lower()
    return TERM_MAP.get(r, raw.strip())


class EvidenceStore:
    def __init__(self, run_log: RunLog):
        self.run_log = run_log
        self.papers: Dict[str, Paper] = {}
        self.evidences: Dict[str, Evidence] = {}
        self.gaps: Dict[str, Gap] = {}
        self.hypotheses: Dict[str, Hypothesis] = {}

    # ---------------- 注册
    def add_papers(self, papers: List[Paper]) -> None:
        for p in papers:
            self.papers[p.paper_id] = p

    def add_evidences(self, evs: List[Evidence]) -> None:
        for e in evs:
            self.evidences[e.evidence_id] = e

    def add_gaps(self, gaps: List[Gap]) -> None:
        for g in gaps:
            self.gaps[g.gap_id] = g

    def add_hypotheses(self, hyps: List[Hypothesis]) -> None:
        for h in hyps:
            self.hypotheses[h.hypothesis_id] = h

    # ---------------- 分析集合(显式口径)
    def analysis_set(self, kind: str = "included") -> List[Paper]:
        """统计口径显式化:included=通过初筛的论文(图表与报告的默认分母)。"""
        if kind == "included":
            return [p for p in self.papers.values() if p.include]
        if kind == "deep":
            return [p for p in self.papers.values()
                    if p.include and p.extraction_status in ("complete", "partial")
                    and p.extraction]
        return list(self.papers.values())

    def evidences_by_paper(self, paper_id: str) -> List[Evidence]:
        return [e for e in self.evidences.values() if e.paper_id == paper_id]

    def paper(self, paper_id: str) -> Paper:
        return self.papers.get(paper_id, Paper(paper_id=paper_id))

    # ---------------- 统计(纯函数,无副作用)
    def count_terms(self, subset: str = "included") -> Counter:
        """按去重论文计数术语出现(每篇每术语最多 1);每次重算,不累加。"""
        c: Counter = Counter()
        for p in self.analysis_set(subset):
            terms = {normalize_term(t) for t in p.extraction.get("materials_or_targets", [])}
            for t in terms:
                c[t] += 1
        return c

    def year_distribution(self, subset: str = "included") -> Counter:
        c: Counter = Counter()
        for p in self.analysis_set(subset):
            if p.year and isinstance(p.year, int):
                c[p.year] += 1
        return c

    def design_distribution(self, subset: str = "included") -> Counter:
        c: Counter = Counter()
        for p in self.analysis_set(subset):
            c[p.study_design] += 1
        return c

    def stats_summary(self) -> Dict:
        """统一统计口径(报告与图表共用,防不一致)。v0.3:新增配对/来源/摘要统计。"""
        inc = self.analysis_set("included")
        deep = self.analysis_set("deep")
        hyps_sys = [h for h in self.hypotheses.values() if h.origin == "system"]
        hyps_pass = [h for h in hyps_sys if h.validation.get("passed")]
        # v0.5.1 修复#6c:待审假设(字段完整但存在待核查项)单独统计,
        # 不得混入完整通过或失败;待审项在统计中正确呈现。
        hyps_pending = [h for h in hyps_sys
                        if not h.validation.get("passed")
                        and (getattr(h, "pending_review", None)
                             or getattr(h, "design_completeness", "complete") == "degraded")]
        return {
            "papers_total": len(self.papers),
            "papers_included": len(inc),
            "papers_deep_extracted": len(deep),
            "papers_review_type": sum(1 for p in inc if p.study_design == "review"),
            "papers_retracted": sum(1 for p in self.papers.values() if p.retracted),
            "papers_with_abstract": sum(1 for p in inc if p.abstract),
            "evidences_total": len(self.evidences),
            "evidences_l1": sum(1 for e in self.evidences.values() if e.evidence_level == 1),
            "evidences_l3_author": sum(1 for e in self.evidences.values()
                                       if e.evidence_level == 3 and e.evidence_type == "author_speculation"),
            "evidences_verified": sum(1 for e in self.evidences.values() if e.verified),
            "gaps_candidate": sum(1 for g in self.gaps.values() if g.status == "candidate"),
            "gaps_narrowed": sum(1 for g in self.gaps.values() if g.status == "narrowed"),
            "gaps_dropped": sum(1 for g in self.gaps.values() if g.status == "dropped"),
            "gaps_paired": sum(1 for g in self.gaps.values() if g.pairing_status == "paired"),
            "gaps_single_incomplete": sum(1 for g in self.gaps.values() if g.pairing_status == "single_incomplete"),
            "hypotheses": len(self.hypotheses),
            "hypotheses_system": len(hyps_sys),
            "hypotheses_system_passed": len(hyps_pass),
            "hypotheses_system_pending_review": len(hyps_pending),
            "hypotheses_system_failed": len(hyps_sys) - len(hyps_pass) - len(hyps_pending),
            "hypotheses_author": sum(1 for h in self.hypotheses.values() if h.origin == "author"),
            "hypotheses_insufficient_evidence": sum(1 for h in hyps_pass if h.insufficient_evidence),
            "analysis_basis_counts": self._basis_counts(inc),
            "source_distribution": self._source_distribution(),
        }

    def _source_distribution(self) -> Dict[str, int]:
        """纳入文献的来源分布(v0.5 修复#4:互斥分类+可重叠覆盖+明确分母)。

        互斥分类(每篇恰好归一类,总和=纳入篇数):
          epmc_only / oa_only / both / unknown
        可重叠覆盖数(一篇可计入多个来源):
          europepmc / openalex / multi_source
        分母:来源分布基于 analysis_set("included") 的纳入文献。
        """
        excl: Counter = Counter()      # 互斥:epmc_only/oa_only/both/unknown
        overlap: Counter = Counter()   # 可重叠:europepmc/openalex/multi_source
        total = 0
        for p in self.analysis_set("included"):
            total += 1
            srcs = {r.get("source") for r in p.source_refs} - {None, ""}
            if not srcs:
                srcs = {"unknown"}
            if "unknown" in srcs:
                excl["unknown"] += 1
            elif "europepmc" in srcs and "openalex" in srcs:
                excl["both"] += 1
            elif "europepmc" in srcs:
                excl["epmc_only"] += 1
            elif "openalex" in srcs:
                excl["oa_only"] += 1
            else:
                excl["unknown"] += 1
            for s in srcs:
                if s != "unknown":
                    overlap[s] += 1
            if len(srcs - {"unknown"}) > 1:
                overlap["multi_source"] += 1
        return {**dict(excl), **{f"overlap_{k}": v for k, v in overlap.items()},
                "denominator_included": total}

    def _basis_counts(self, papers: List[Paper]) -> Dict[str, int]:
        c: Counter = Counter()
        for p in papers:
            c[p.analysis_basis] += 1
        return dict(c)

    # ---------------- 证据矩阵
    def evidence_matrix(self) -> List[dict]:
        rows = []
        deep_ids = {p.paper_id for p in self.analysis_set("deep")}
        for pid, p in sorted(self.papers.items(), key=lambda kv: -(kv[1].cited_by or 0)):
            evs = self.evidences_by_paper(pid)
            main = [e for e in evs if e.evidence_level == 1][:2]
            mech = p.extraction.get("proposed_mechanisms", {})
            rows.append({
                "paper_id": pid,
                "title": p.title[:80],
                "year": p.year,
                "journal": p.journal[:40],
                "in_deep_analysis": pid in deep_ids,
                "model_object": ";".join(p.models[:3]),
                "factors": ";".join(p.extraction.get("variables", {}).get("independent", [])[:4]),
                "main_results": " | ".join(e.claim[:120] for e in main),
                "mechanism_evidence": ("作者验证:" + ";".join(
                    m["claim"][:60] for m in mech.get("author_validated", [])[:1])
                    if mech.get("author_validated") else
                    ("作者推测:" + ";".join(m["claim"][:60]
                                            for m in mech.get("author_speculative", [])[:1])
                     if mech.get("author_speculative") else "")),
                "evidence_level": ("L1(直接)" if any(e.evidence_level == 1 for e in evs)
                                   else ("L3(作者推测)" if evs else "-")),
                "analysis_basis": p.analysis_basis,
                "limitations": ";".join(
                    (l.get("claim", "") if isinstance(l, dict) else str(l))
                    for l in p.extraction.get("author_reported_limitations", [])[:2])[:150],
                "doi": p.doi, "pmid": p.pmid,
            })
        return rows

    # ---------------- 导出
    def export(self, out_dir: str) -> Dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        paths = {}

        def dump(name: str, obj) -> str:
            path = os.path.join(out_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            paths[name] = path
            return path

        dump("papers.json", [p.to_dict() for p in self.papers.values()])
        dump("evidences.json", [e.to_dict() for e in self.evidences.values()])
        dump("evidence_matrix.json", self.evidence_matrix())
        dump("gaps.json", [g.to_dict() for g in self.gaps.values()])
        dump("hypotheses.json", [h.to_dict() for h in self.hypotheses.values()])
        dump("stats_summary.json", self.stats_summary())

        matrix = self.evidence_matrix()
        if matrix:
            with open(os.path.join(out_dir, "evidence_matrix.csv"), "w",
                      newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(matrix[0].keys()))
                w.writeheader(); w.writerows(matrix)
            paths["evidence_matrix.csv"] = os.path.join(out_dir, "evidence_matrix.csv")

        papers_rows = [{
            "paper_id": p.paper_id, "doi": p.doi, "pmid": p.pmid, "oa_id": p.oa_id,
            "title": p.title,
            "year": p.year, "journal": p.journal, "cited_by": p.cited_by,
            "retracted": p.retracted,
            "study_design": p.study_design, "models": "|".join(p.models),
            "model_evidence": p.model_evidence,
            "relevance_score": p.relevance_score, "quality_score": p.quality_score,
            "full_text_status": p.full_text_status, "ft_link_found": p.ft_link_found,
            "ft_fetched": p.ft_fetched, "ft_parsed": p.ft_parsed,
            "has_abstract": bool(p.abstract),
            "metadata_status": p.metadata_status, "included": p.include,
            "include_reason": p.include_reason,
            "sources": "+".join(sorted({r.get("source", "") for r in p.source_refs})),
            "source_queries": " | ".join(f"{r.get('source')}:{r.get('query_name')}"
                                         for r in p.source_refs)[:160],
        } for p in self.papers.values()]
        if papers_rows:
            with open(os.path.join(out_dir, "papers.csv"), "w",
                      newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(papers_rows[0].keys()))
                w.writeheader(); w.writerows(papers_rows)
            paths["papers.csv"] = os.path.join(out_dir, "papers.csv")
        self.run_log.log("EvidenceStore", "export", "ok", f"files={sorted(paths)}")
        return paths

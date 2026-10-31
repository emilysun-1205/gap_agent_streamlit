"""
Module 2:质量与相关性评估 v0.3
修复:
- 综述优先识别+配额(继承 v0.2)
- model_evidence 区分"摘要识别类别"vs"确认单模型"(配合 schemas)
- 撤稿论文(retracted)自动排除并记录理由
- 多模型信息保留
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Tuple

from .schemas import Paper, RunLog

REVIEW_PUBTYPES = {"review", "review-article", "systematic review"}

REVIEW_SIGNALS = [
    r"\b(this review|we review|we summarize|we summarise|systematic review|meta-?analysis|"
    r"literature (search|review)|narrative review|comprehensive overview|"
    r"we (describe and )?discuss (the )?(current )?(knowledge|evidence|literature)|"
    r"aims to (review|summarize|summarise)|we provide an overview)\b",
]

DESIGN_PATTERNS: List[tuple] = [
    ("clinical", r"\b(randomi[sz]ed|clinical trial|phase [iI1234]+ trial|single-?arm|"
                 r"cohort of \d+ patients|\d+ patients (were|received))\b"),
    ("human", r"\b(patient[s]?|clinical samples?|tumor specimens?|biops(y|ies)|resection specimens?|"
              r"human tissue[s]?|blood samples? from|serum samples? from)\b"),
    ("in_vivo", r"\b(mouse|mice|xenograft|syngeneic|tumor-?bearing|PDX|patient-derived xenograft|"
                r"humanized mice|in vivo|C57BL|BALB/c)\b"),
    ("in_vitro", r"\b(cell line[s]?|cells were (treated|transfected|co-?cultured)|in vitro|"
                 r"co-?culture|organoid[s]?|spheroid[s]?)\b"),
    ("bioinformatic", r"\b(TCGA|GEO dataset|single-?cell (RNA-?)?seq|scRNA-?seq|bulk RNA-?seq|"
                      r"public datasets?|database analysis|spatial transcriptomics)\b"),
]
DESIGN_RANK = {"clinical": 5, "human": 4, "in_vivo": 3, "in_vitro": 2,
               "bioinformatic": 2, "review": 1, "unknown": 0}

MODEL_PATTERNS = [
    ("细胞系/体外", r"\b([A-Z][a-z0-9]+[- ]?\d{1,3} cell[s]?|cell lines?|co-?culture|organoid|spheroid)\b"),
    ("小鼠/体内", r"\b(C57BL/6|BALB/c|nu/nu|NSG|xenograft|syngeneic|PDX|tumor-bearing mice|humanized mice)\b"),
    ("患者样本", r"\b(patient[s]?|biops(y|ies)|specimens?|clinical samples?)\b"),
    ("公共数据集", r"\b(TCGA|GEO|single-cell (RNA-)?seq|scRNA|public datasets?)\b"),
]

QUALITY_SIGNALS = {
    "sample": [r"\b(n\s?=\s?\d+|cohort of \d+|\d+ patients|\d+ mice|\d+ samples|\d+ cases)\b"],
    "statistics": [r"\b(p\s?[<=]\s?0?\.\d+|significantly|CI\b|confidence interval|hazard ratio|odds ratio|"
                   r"median .*(OS|PFS)|log-?rank)\b"],
    "validation": [r"\b(validate[sd]?|validated|independent cohort|reproduced|replicate[sd]?|confirmation)\b"],
    "mechanism_test": [r"\b(knockdown|knockout|overexpression|inhibitor|blocked|silencing|CRISPR|shRNA|siRNA|"
                       r"antibody blockade|pharmacologic(?:al)? (?:inhibition|blockade))\b"],
}


def _is_review(p: Paper) -> Tuple[bool, str]:
    pts = {str(t).lower() for t in p.pub_types}
    if pts & REVIEW_PUBTYPES:
        return True, f"pubType 包含 {pts & REVIEW_PUBTYPES}"
    text = f"{p.title} {p.abstract}"
    for pat in REVIEW_SIGNALS:
        m = re.search(pat, text, re.I)
        if m:
            return True, f"结构信号命中: '{m.group(0)[:50]}'"
    if pts & {"article", "journal-article", "unknown"} == pts and not text:
        return False, ""
    return False, ""


def detect_design(p: Paper) -> Tuple[str, List[str]]:
    text = f"{p.title} {p.abstract}"
    is_rev, why = _is_review(p)
    models = []
    for label, pat in MODEL_PATTERNS:
        if re.search(pat, text, re.I):
            models.append(label)
    if is_rev:
        return "review", models
    hits = [name for name, pat in DESIGN_PATTERNS if re.search(pat, text, re.I)]
    if not hits:
        return "unknown", models
    primary = sorted(hits, key=lambda h: -DESIGN_RANK[h])[0]
    return primary, models


def relevance(p: Paper, concept_terms: Dict[str, List[str]]) -> Tuple[float, str, Dict]:
    title_l = p.title.lower()
    abs_l = p.abstract.lower()
    score, hits = 0.0, {}
    groups = [g for g in concept_terms if g != "free"]
    for g, terms in concept_terms.items():
        th, ah = 0, 0
        for t in terms:
            tl = t.lower()
            if tl in title_l:
                th += 1
            elif tl in abs_l:
                ah += 1
        if th or ah:
            hits[g] = (th, ah)
    n_groups = len(groups) or 1
    coverage = len([g for g in groups if g in hits]) / n_groups
    title_bonus = sum(min(v[0], 2) for v in hits.values())
    score = coverage * 3.0 + min(title_bonus * 0.4, 1.5)
    if not hits:
        for t in concept_terms.get("free", []):
            if t.lower() in title_l or t.lower() in abs_l:
                score += 1.0
                break
    score = round(min(score, 5.0), 2)
    if coverage >= 0.99:
        reason = "全部核心概念均出现"
    elif coverage >= 0.66:
        reason = "多数核心概念出现"
    elif coverage > 0:
        reason = f"概念覆盖 {int(coverage*100)}%"
    else:
        reason = "仅命中背景词/自由词"
    return score, reason, hits


def quality(p: Paper) -> Tuple[float, str]:
    text = f"{p.title} {p.abstract}".lower()
    notes = []
    q = 0.0
    d = p.study_design
    q += {5: 2.0, 4: 1.7, 3: 1.5, 2: 1.2, 1: 0.8, 0: 0.5}[DESIGN_RANK.get(d, 0)]
    notes.append(f"设计={d}")
    for sig, pats in QUALITY_SIGNALS.items():
        if any(re.search(pt, text, re.I) for pt in pats):
            q += {"sample": 0.5, "statistics": 0.6, "validation": 0.7, "mechanism_test": 0.7}[sig]
            notes.append(sig)
    if not p.abstract:
        q = min(q, 1.0)
        notes.append("无摘要:设计/质量不可判(摘要未报告,非研究缺陷)")
    if p.retracted:
        q = 0.0
        notes.append("撤稿论文:质量置零并排除")
    notes.append(f"模型证据={p.model_evidence}")
    notes.append(f"分析依据={p.analysis_basis};来源={p.source_db}")
    return round(min(q, 5.0), 2), ";".join(notes)


def screen(papers: List[Paper], plan, keep: int, run_log: RunLog,
           max_review_share: float = 0.25) -> List[Paper]:
    terms = plan.concepts
    for p in papers:
        p.study_design, models = detect_design(p)
        if p.models:
            models = list(dict.fromkeys(p.models + models))
        p.models = models[:4]
        p.model_evidence = "identified_categories" if p.models else "unreported"
        rel, reason, _ = relevance(p, terms)
        p.relevance_score = rel
        p.relevance_reason = reason
        p.relevance_grade = ("高" if rel >= 3.5 else "中" if rel >= 2.0 else "低")
        p.quality_score, p.quality_notes = quality(p)
        if p.retracted:
            p.include, p.include_reason = False, "撤稿论文(OpenAlex retracted 标记):排除"
        elif p.relevance_score < 1.5:
            p.include, p.include_reason = False, f"相关性不足({rel})"
        elif not p.abstract:
            p.include, p.include_reason = False, "无摘要:无法做摘要层评估(摘要未报告,非研究缺陷)"
        else:
            p.include, p.include_reason = True, f"相关性{p.relevance_grade}({rel});{p.relevance_reason}"
    ranked = sorted(papers, key=lambda x: (x.relevance_score, x.quality_score, x.cited_by), reverse=True)
    n_review_quota = max(1, int(keep * max_review_share))
    included, n_rev = [], 0
    for p in ranked:
        if not p.include:
            continue
        if p.study_design == "review":
            if n_rev >= n_review_quota:
                p.include, p.include_reason = False, f"综述超出配额({n_review_quota}/{keep})"
                continue
            n_rev += 1
        included.append(p)
        if len(included) >= keep:
            break
    for p in included:
        p.include_reason += ";入选初筛"
    for p in ranked:
        if p.include and p not in included:
            p.include, p.include_reason = False, "超出初筛配额(排序靠后)"
    dist = defaultdict(int)
    for p in included:
        dist[p.study_design] += 1
    n_retracted = sum(1 for p in papers if p.retracted)
    run_log.log("Evaluator", "rule_based", "ok",
                f"total={len(papers)} included={len(included)} designs={dict(dist)} "
                f"综述配额={n_rev}/{len(included)} 撤稿排除={n_retracted}")
    return included

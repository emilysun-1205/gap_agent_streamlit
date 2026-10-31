"""
Node 1:结构化抽取 v0.3
修复:
- 终点极性:保留具体终点(response/resistance/OS...),effect_on_outcome 归一效益含义
  (response↑ 与 resistance↓ 同为 improves,不再判为反向冲突)
- 否定处理:negated 关系;阴性结果不作肯定方向
- 关系绑定主体+终点:不再按整句词典优先命中判方向
- model_evidence:identified_categories(摘要识别类别)≠ 原文确认单模型
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional

from .schemas import (Evidence, Paper, RunLog, clean_ws,
                      detect_endpoint, endpoint_polarity, effect_on_outcome)

SPECULATIVE_PAT = re.compile(
    r"\b(may|mights?|possibly|plausib\w+|we speculate|speculat\w+|suggest(?:s|ed|ing)? that|"
    r"appear[sd]? to|putative|presumab\w+|it is possible|could|potential\w*|hypothes[iy]\w*|"
    r"remains? to be|likely)\b", re.I)
NEGATIVE_PAT = re.compile(
    r"\b(did not|does not|failed to|no (significant |difference|effect|change|improvement)|"
    r"unable to|absence of|lacked|not associated|not improved|no increase|no decrease)\b", re.I)
REVIEW_TRANSFER_PAT = re.compile(
    r"\b(has been (reported|shown|demonstrated)|have been (reported|shown)|studies have shown|"
    r"it has been (reported|shown|demonstrated)|previous(?:ly)? (reports?|studies|work) (showed|reported|found)|"
    r"is (reported|known) to)\b", re.I)
FINDING_PAT = re.compile(
    r"\b(we (found|show|demonstrate|report|observed|identified|discovered|analyzed|measured|"
    r"treated|validated)|our (results|data|findings|analysis)|results (showed|show|revealed|demonstrated)|"
    r"here (we|, we)|in this (study|work|report)|treatment (with|of).{0,60}(resulted|led)|"
    r"patients? (received|were treated)|mice were|cells were (treated|transfected))\b", re.I)
METHOD_PAT = re.compile(
    r"\b(we (used|performed|applied)|using|were assessed by|was measured|immunohistochem\w+|"
    r"flow cytometry|quantified|sequencing)\b", re.I)
UNRESOLVED_PAT = re.compile(
    r"\b(remains? (unclear|unknown|elusive|to be determined)|little is known|poorly understood|"
    r"(has|have) not been (established|determined|studied)|further (studies|work|research) (are|is) (needed|warranted)|"
    r"future (studies|work) should)\b", re.I)

MODEL_PATTERNS = [
    ("细胞系/体外", r"\b([A-Z][a-z0-9]+[- ]?\d{1,3} cells?|cell lines?|co-?culture|organoid|spheroid)\b"),
    ("小鼠/体内", r"\b(C57BL/6|BALB/c|nu/nu|NSG|xenograft|syngeneic|PDX|tumor-bearing mice|humanized mice)\b"),
    ("患者样本", r"\b(patients?|biops(?:y|ies)|specimens?|clinical samples?)\b"),
    ("公共数据集", r"\b(TCGA|GEO|single-cell (RNA-)?seq|scRNA|public datasets?)\b"),
]

MECH_ENTITIES = {
    "HIF-1": r"\bHIF-?1(?:alpha|α)?\b",
    "HIF-2": r"\bHIF-?2(?:alpha|α)?\b",
    "VEGF": r"\bVEGF(?:A)?\b|angiogen\w+",
    "adenosine axis": r"adenosine|CD39|CD73|A2A receptor",
    "lactate": r"\blactate\b|acidosis|acidic TME",
    "TAM": r"tumor-?associated macrophage|\bTAMs?\b",
    "MDSC": r"myeloid-?derived suppressor|\bMDSCs?\b",
    "T cell exhaustion": r"T[- ]cell exhaustion|exhausted (T cells?|lymphocytes?)",
    "TIL/CD8 infiltration": r"\bTILs?\b|CD8\+?\s?(T cells?|lymphocytes?)|effector T cells?",
    "CXCR4 axis": r"\bCXCR4\b|\bCXCL12\b",
    "ECM remodeling": r"extracellular matrix|\bECM\b|collagen|cancer-?associated fibroblast|\bCAFs?\b",
    "ROS": r"reactive oxygen|\bROS\b|oxidative stress",
    "autophagy": r"autopha\w+",
    "metabolic reprogramming": r"metabolic reprogram\w+|glycolytic reprogram\w+|\bglycolysis\b|OXPHOS",
    "epigenetic": r"epigenetic\w*|histone (modification|acetylation|methylation)|DNA methylation",
    "exosome": r"exosom\w*|extracellular vesicles?|\bsEVs?\b",
    "macrophage polarization": r"macrophage (polarization|phenotype)|M1[ /-]M2|M2 (macrophages?|polarization)",
}

RELATION_WORDS = {
    "increases": r"\b(increases?|increased|promotes?|promoted|enhances?|enhanced|up-?regulates?|upregulated|induces?|induced|drives?|drove|elevates?|elevated|improves?|improved|boosts?)\b",
    "decreases": r"\b(decreases?|decreased|reduces?|reduced|inhibits?|inhibited|suppresses?|suppressed|down-?regulates?|downregulated|impairs?|impaired|attenuates?|attenuated|blocks?|blocked|limits?|limited)\b",
    "associates": r"\b(associat\w+|correlat\w+|linked to|related to)\b",
}
INTERVENTION_PAT = re.compile(
    r"\b(knockdown|knockout|inhibit\w+|blockade|blocked|silenc\w+|overexpress\w+|antibody|"
    r"pharmacolog\w+|depletion|ablation)\b", re.I)


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text or "")
    return [clean_ws(s) for s in parts if len(clean_ws(s)) > 25]


def classify_sentence(s: str) -> str:
    if UNRESOLVED_PAT.search(s):
        return "unresolved"
    if REVIEW_TRANSFER_PAT.search(s):
        return "review_transfer"
    if SPECULATIVE_PAT.search(s):
        return "speculative"
    if NEGATIVE_PAT.search(s) and FINDING_PAT.search(s):
        return "negative"
    if FINDING_PAT.search(s):
        return "finding"
    has_direction = any(re.search(p, s, re.I) for p in RELATION_WORDS.values())
    has_quant = bool(re.search(
        r"(\d+\s?%|\bp\s?[<=]\s?0?\.\d+|significan\w+|higher|lower|greater|reduced|"
        r"increased|decreased|improved|prolonged|shorter|median|survival|response rate)", s, re.I))
    if has_direction and has_quant:
        return "finding"
    if METHOD_PAT.search(s):
        return "method"
    return "background"


def independent_kind_check(sentence: str, claimed_kind: str) -> tuple:
    """独立复核句类(v0.3):不依赖对象自报 sentence_kind。
    返回 (复核结果, 说明)。推测/否定/转述标志存在时否决 finding/negative。"""
    s = sentence
    if SPECULATIVE_PAT.search(s) and claimed_kind in ("finding", "negative"):
        return False, "独立复核:句含推测标志(may/suggest等),不得为 L1 finding"
    if REVIEW_TRANSFER_PAT.search(s) and claimed_kind in ("finding", "negative"):
        return False, "独立复核:句为他人研究转述,非本文直接结果"
    if NEGATIVE_PAT.search(s) and claimed_kind == "finding":
        return True, "独立复核:句含否定但按 finding 处理时方向须为 negated"
    return True, "独立复核通过"


def _entities_in(s: str) -> List[str]:
    return [label for label, pat in MECH_ENTITIES.items() if re.search(pat, s, re.I)][:4]


def _relation_with_span(s: str) -> Optional[tuple]:
    """关系词+位置(用于就近绑定主体与终点)。"""
    best = None
    for name, pat in RELATION_WORDS.items():
        m = re.search(pat, s, re.I)
        if m:
            if best is None or m.start() < best[1]:
                best = (name, m.start())
    return best


def _bind_triple(s: str) -> Dict[str, str]:
    """构建 subject-relation-endpoint:关系词左侧最近的实体为 subject,
    句内终点词为 endpoint;否定词存在则 relation=negated。"""
    ents = _entities_in(s)
    rel_span = _relation_with_span(s)
    relation = rel_span[0] if rel_span else ""
    if NEGATIVE_PAT.search(s):
        relation = "negated"
    endpoint = detect_endpoint(s)
    subject = ""
    if rel_span and ents:
        left = [e for e in ents if re.search(MECH_ENTITIES[e], s[:rel_span[1]], re.I)]
        subject = left[-1] if left else ents[0]
    elif ents:
        subject = ents[0]
    return {"subject": subject, "relation": relation, "endpoint": endpoint or ""}


def _multi_endpoint(s: str) -> List[str]:
    """同句多终点检测(v0.4 修复#6)。

    若一句同时命中多个不同终点词(如同时含 response 与 resistance、或
    survival 与 metastasis),则无法可靠地把整句绑定到单一终点,返回命中的
    终点名列表;调用方据此标记"无法可靠抽取"而非静默取第一个。
    """
    from .schemas import BENEFIT_ENDPOINTS, RISK_ENDPOINTS
    hits = []
    for name, pat in {**BENEFIT_ENDPOINTS, **RISK_ENDPOINTS}.items():
        if re.search(pat, s, re.I):
            hits.append(name)
    # 去重并保留出现顺序
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _models_in(text_l: str) -> List[str]:
    return [label for label, pat in MODEL_PATTERNS if re.search(pat, text_l, re.I)][:4]


def extract_paper(p: Paper, concepts: Dict[str, List[str]] | None = None,
                  run_log: RunLog | None = None) -> Paper:
    text = p.abstract or ""
    if not text:
        p.extraction_status = "partial"
        p.extraction = {"error": "无摘要:字段抽取不可用(标注'摘要未报告',非研究缺陷)",
                        "basis": "none"}
        p.model_evidence = "unreported"
        if run_log:
            run_log.log("Extractor", "rule_based", "partial", f"{p.paper_id} 无摘要(来源:{p.source_db})")
        return p

    sents = _sentences(text)
    kinds = [classify_sentence(s) for s in sents]
    text_l = text.lower()
    title_l = p.title.lower()

    p.extraction["n_sentences"] = len(sents)
    p.extraction["sentence_kinds"] = {k: kinds.count(k) for k in set(kinds)}

    rq = ""
    for s, k in zip(sents, kinds):
        if k in ("background", "unresolved") and re.search(
                r"\b(whether|how|role of|mechanism|remains)\b", s, re.I):
            rq = s
            break
    p.extraction["research_question"] = rq or f"{p.title.strip('.')}"

    objs = _models_in(text_l)
    p.extraction["research_object"] = objs
    p.models = objs
    # v0.3:类别标签 ≠ 单模型确认
    p.model_evidence = "identified_categories" if objs else "unreported"
    p.extraction["research_object_note"] = (
        f"摘要识别到 {len(objs)} 类模型线索({';'.join(objs)});"
        "类别计数不等于'原文确认仅使用 N 个模型/队列',单模型结论需全文确认"
        if objs else "摘要未报告模型线索(未知,非研究缺陷)")

    materials = []
    for s, k in zip(sents, kinds):
        if k in ("method", "background"):
            continue
        for e in _entities_in(s):
            if e not in materials:
                materials.append(e)
    if not materials:
        for e in _entities_in(title_l + " " + text_l):
            if e not in materials:
                materials.append(e)
                break
    p.extraction["materials_or_targets"] = materials[:8]

    method_terms = ["flow cytometry", "RNA-seq", "scRNA-seq", "single-cell", "western blot",
                    "qPCR", "qRT-PCR", "ELISA", "immunohistochemistry", "IHC",
                    "immunofluorescence", "CyTOF", "CRISPR", "knockout", "knockdown",
                    "shRNA", "siRNA", "overexpression", "bioluminescence", "seahorse",
                    "metabolomics", "proteomics", "spatial transcriptomics"]
    p.extraction["methods"] = [m for m in method_terms if m in text_l][:6]
    p.extraction["study_design"] = p.study_design

    iv, dv = [], []
    for g, terms in (concepts or {}).items():
        for t in terms[:8]:
            if t.lower() in text_l:
                (dv if g in ("resistance", "metastasis") else iv).append(t)
                break
    p.extraction["variables"] = {
        "independent": sorted(set(iv))[:6],
        "dependent": sorted(set(dv))[:6],
        "confounders": ["摘要层未系统评估(需全文核对)"],
    }

    # ---- Main Findings:绑定三元组+终点极性
    findings = []
    for i, (s, k) in enumerate(zip(sents, kinds)):
        if k not in ("finding", "negative"):
            continue
        ok, note = independent_kind_check(s, k)
        if not ok:
            findings.append({"claim": s[:400], "evidence_location": f"摘要句#{i+1}",
                             "sentence_index": i, "sentence_kind": "speculative" if "推测" in note else k,
                             "evidence_type": "author_speculation",
                             "independent_check": note, "conditions": _models_in(s.lower())[:2],
                             "triple": _bind_triple(s), "limitations": ["句类独立复核降级"]})
            continue
        tri = _bind_triple(s)
        eff = effect_on_outcome(tri.get("relation"), tri.get("endpoint"))
        # v0.4 修复#6:同句多终点 → 不静默绑定第一个终点,标记待人工拆分
        endpoints_hit = _multi_endpoint(s)
        multi = len(endpoints_hit) > 1
        findings.append({
            "claim": s[:400], "evidence_location": f"摘要句#{i+1}", "sentence_index": i,
            "sentence_kind": k, "evidence_type": "direct", "independent_check": note,
            "conditions": _models_in(s.lower())[:2], "triple": tri,
            "effect": eff,
            "multi_endpoint": multi,
            "endpoints_hit": endpoints_hit if multi else [],
            "needs_manual": multi,
            "limitations": (["同句多终点:无法可靠绑定单一终点(待人工按终点拆分)"]
                            if multi else [])})
    p.extraction["main_findings"] = findings[:5]

    mech_verified, mech_spec = [], []
    for i, (s, k) in enumerate(zip(sents, kinds)):
        if k in ("method", "background", "review_transfer", "unresolved"):
            continue
        ents = _entities_in(s)
        if not ents:
            continue
        if k in ("finding", "negative") and INTERVENTION_PAT.search(s):
            mech_verified.append({"claim": s[:300], "location": f"摘要句#{i+1}", "entities": ents})
        elif k == "speculative":
            mech_spec.append({"claim": s[:300], "location": f"摘要句#{i+1}", "entities": ents})
    p.extraction["proposed_mechanisms"] = {
        "author_validated": mech_verified[:4], "author_speculative": mech_spec[:4]}

    p.extraction["author_reported_limitations"] = [
        {"claim": s[:250], "location": f"摘要句#{i+1}"}
        for i, (s, k) in enumerate(zip(sents, kinds))
        if k == "unresolved" or re.search(r"\b(limitation|small sample|single-?center|retrospective)\b", s, re.I)][:4]
    p.extraction["unresolved_questions"] = [
        {"claim": s[:300], "location": f"摘要句#{i+1}"}
        for i, (s, k) in enumerate(zip(sents, kinds)) if k == "unresolved"][:3]

    p.extraction["evidence_basis"] = p.analysis_basis
    n_fields = sum(1 for k in ("research_question", "research_object", "materials_or_targets",
                               "methods", "main_findings", "proposed_mechanisms")
                   if p.extraction.get(k))
    conf = min(0.45 + 0.07 * n_fields, 0.75)
    if p.analysis_basis == "abstract":
        conf -= 0.10
    p.extraction["extraction_confidence"] = round(max(conf, 0.2), 2)
    p.extraction_status = "complete" if n_fields >= 4 and findings else "partial"
    if run_log:
        run_log.log("Extractor", "rule_based", p.extraction_status,
                    f"{p.paper_id} fields={n_fields} findings={len(findings)}")
    return p


def extract_batch(papers: List[Paper], concepts=None, run_log: RunLog | None = None) -> List[Paper]:
    for p in papers:
        extract_paper(p, concepts, run_log)
    return papers


def build_evidences(p: Paper, prefix: str) -> List[Evidence]:
    evs: List[Evidence] = []
    ex = p.extraction
    seen_keys = set()

    def add(e: Evidence):
        k = e.dedup_key()
        if k in seen_keys:
            return
        seen_keys.add(k)
        evs.append(e)

    for f in ex.get("main_findings", []):
        tri = f.get("triple", {})
        kind = f.get("sentence_kind", "finding")
        is_direct = f.get("evidence_type", "direct") == "direct"
        eff = f.get("effect") or effect_on_outcome(tri.get("relation"), tri.get("endpoint"))
        add(Evidence(
            evidence_id="", paper_id=p.paper_id,
            claim=f["claim"], sentence_kind=kind,
            evidence_type="direct" if is_direct else "author_speculation",
            evidence_level=1 if is_direct else 3,
            location=f["evidence_location"], sentence_index=f.get("sentence_index", 0),
            model=";".join(f.get("conditions", [])) or ";".join(ex.get("research_object", [])[:2]),
            conditions=f.get("conditions", []), triple=tri,
            direction=tri.get("relation", ""),
            effect=eff,
            endpoint_polarity=endpoint_polarity(tri.get("endpoint")),
            mechanism_claimed=bool(INTERVENTION_PAT.search(f["claim"])),
            limitations=(["摘要层抽取,待全文核验"] if p.analysis_basis == "abstract" else []),
        ))
    # v0.4 修复#1:作者推测证据链。proposed_mechanisms.author_speculative 是作者
    # 在摘要中提出的推测性机制表述,必须构造为 Evidence(evidence_type=
    # author_speculation, evidence_level=3),保留原文定位与去重,供 _mechanism_gaps
    # 读取。同句重复提及不增加独立论文数(去重键含 paper_id+claim+subject+endpoint)。
    for m in ex.get("proposed_mechanisms", {}).get("author_speculative", []):
        claim = m.get("claim", "")
        if not claim:
            continue
        ents = m.get("entities", []) or []
        subject = ents[0] if ents else ""
        tri = {"subject": subject, "relation": "", "endpoint": ""}
        add(Evidence(
            evidence_id="", paper_id=p.paper_id,
            claim=claim, sentence_kind="speculative",
            evidence_type="author_speculation",
            evidence_level=3,
            location=m.get("location", ""),
            sentence_index=0,
            model=";".join(ex.get("research_object", [])[:2]),
            conditions=[],
            triple=tri,
            direction="",
            effect="unclear",
            endpoint_polarity="unclear",
            mechanism_claimed=bool(INTERVENTION_PAT.search(claim)),
            limitations=["作者推测性表述(原文转录,非本文直接实验验证);待全文核验"],
        ))
    for j, e in enumerate(evs, 1):
        e.evidence_id = f"{prefix}_E{j:03d}"
    return evs

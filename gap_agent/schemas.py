"""
数据结构层 v0.3
新增/修复:
- ResearchQuestion:结构化研究问题(实体/关系/结局/条件),贯穿规划->反查->假设->设计,杜绝旧主题回退
- Evidence:具体终点(endpoint)+ 效益/风险极性(effect_on_outcome),关系绑定(主体,终点)
- Paper:多来源追踪(source_ids:来源+来源ID+命中检索式)、OpenAlex 字段(oa_id/retracted)、撤稿标记
- Gap:结构化 entity/endpoint 字段(不再从中文描述重新解析)、pairing_status
- Hypothesis:pairing_status/insufficient_evidence/rewrite_count
- RunLog.log_api:保存 URL+脱敏参数+状态+响应证据(可重放 vs 可重查区分)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

REDACTED = "<REDACTED>"


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def yes(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().upper() == "Y"


def redact(obj: Any, secrets: List[str]) -> Any:
    """递归脱敏:移除任何包含密钥明文的字符串。"""
    if not secrets:
        return obj
    def _r(o):
        if isinstance(o, str):
            for s in secrets:
                if s and s in o:
                    return REDACTED
            return o
        if isinstance(o, dict):
            return {k: _r(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_r(x) for x in o]
        return o
    return _r(obj)


# ---------------------------------------------------------------- 句类
SENTENCE_KINDS = {
    "finding": "作者直接报告的研究结果(L1 直接证据)",
    "speculative": "作者推测/假设表述(记录为作者假设,不作 L1)",
    "negative": "阴性/否定结果(L1,方向为 negated)",
    "review_transfer": "转述他人研究(非本文直接结果)",
    "method": "方法描述(不作为证据)",
    "background": "背景陈述(不作为证据)",
    "unresolved": "作者明确的未解决问题陈述",
}

# 终点极性:效益终点(升高=改善)vs 风险终点(升高=恶化)
BENEFIT_ENDPOINTS = {
    "treatment response": r"\b(response|responders?|ORR|objective response|efficacy|benefit)\b",
    "overall survival": r"\b(overall survival|\bOS\b)\b",
    "progression-free survival": r"\b(progression-?free survival|\bPFS\b)\b",
    "T cell function": r"\b(T cell (function|activity|proliferation|killing)|cytotoxic\w*|IFN-?γ|interferon-?gamma)\b",
    "immune infiltration": r"\b(infiltration|infiltrat\w+|TIL density|immune cell infiltration)\b",
}
RISK_ENDPOINTS = {
    "resistance": r"\b(resistance|resistant|non-?response|nonresponder|treatment failure|relapse)\b",
    "tumor growth": r"\b(tumor growth|tumour growth|tumor burden|tumor volume|progression)\b",
    "T cell exhaustion": r"\b(T[- ]cell exhaustion|exhausted (T cells?|lymphocytes?))\b",
    "toxicity": r"\b(toxicity|adverse events?)\b",
    "metastasis": r"\b(metastasis|metastat\w+|metastases|dissemination)\b",
}


# v0.4 修复#6:复合/否定终点词(如 non-response、nonresponder、treatment
# failure)必须优先于裸词(如 response)识别,否则 "non-response" 会被
# BENEFIT_ENDPOINTS["treatment response"] 的 "response" 子串误判为效益终点。
_COMPOUND_RISK_PAT = re.compile(
    r"\b(non-?response|nonresponders?|treatment failure|no response|resistance|"
    r"resistant|relapse|refractor\w*|unresponsive)\b", re.I)


def detect_endpoint(sentence: str) -> Optional[str]:
    # 1) 优先识别复合/否定终点(风险方向),避免 "non-response" 被裸 "response" 吞掉
    for name, pat in RISK_ENDPOINTS.items():
        if name == "resistance" and _COMPOUND_RISK_PAT.search(sentence):
            return name
        if name != "resistance" and re.search(pat, sentence, re.I):
            return name
    # 2) 其余终点(含效益终点)按原顺序
    for name, pat in {**BENEFIT_ENDPOINTS, **RISK_ENDPOINTS}.items():
        if re.search(pat, sentence, re.I):
            return name
    return None


def endpoint_polarity(endpoint: Optional[str]) -> str:
    if endpoint in BENEFIT_ENDPOINTS:
        return "benefit"
    if endpoint in RISK_ENDPOINTS:
        return "risk"
    return "unclear"


def effect_on_outcome(relation: Optional[str], endpoint: Optional[str]) -> str:
    """归一化效应方向:response↑ 与 resistance↓ 均为 improves(效益含义同向)。"""
    pol = endpoint_polarity(endpoint)
    if relation == "increases":
        return "improves" if pol == "benefit" else ("worsens" if pol == "risk" else "unclear")
    if relation == "decreases":
        return "improves" if pol == "risk" else ("worsens" if pol == "benefit" else "unclear")
    if relation == "negated":
        return "negated"
    return "unclear"


@dataclass
class ResearchQuestion:
    """结构化研究问题(Node 0 输出,贯穿全流水线;v0.3 核心)。"""
    topic_zh: str = ""
    status: str = "ok"                      # ok | pending_planning
    status_note: str = ""
    entities: List[str] = field(default_factory=list)        # 实体(英文检索词组)
    relations: List[str] = field(default_factory=list)       # 关系词
    outcomes: List[str] = field(default_factory=list)        # 结局词
    conditions: List[str] = field(default_factory=list)      # 疾病/模型/条件词
    topic_terms_en: List[str] = field(default_factory=list)  # 主题英文词(检索兜底,仅本主题)
    concept_groups: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRef:
    source: str = ""            # europepmc | openalex | crossref
    source_id: str = ""         # 该源内的稳定 ID
    query_name: str = ""        # 命中的检索式名
    query_text: str = ""        # 实际执行的检索式(该源方言)
    def to_dict(self):
        return asdict(self)


@dataclass
class Paper:
    paper_id: str
    title: str = ""
    authors: str = ""
    year: Optional[int] = None
    journal: str = ""
    doi: str = ""
    pmid: str = ""
    pub_types: List[str] = field(default_factory=list)
    abstract: str = ""
    cited_by: int = 0
    retracted: bool = False                 # OpenAlex retracted_date 撤稿标记
    oa_id: str = ""                         # OpenAlex W ID
    source_refs: List[Dict[str, Any]] = field(default_factory=list)  # SourceRef.to_dict()

    ft_link_found: bool = False
    ft_fetched: bool = False
    ft_parsed: bool = False
    ft_used_for_verification: bool = False
    ft_raw: Dict[str, Any] = field(default_factory=dict)
    full_text_status: str = "unknown"

    metadata_status: str = "unverified"     # verified|partial|unverified|not_checked
    metadata_check: Dict[str, Any] = field(default_factory=dict)

    relevance_score: float = 0.0
    relevance_grade: str = ""
    relevance_reason: str = ""
    quality_score: float = 0.0
    quality_notes: str = ""
    study_design: str = "unknown"
    models: List[str] = field(default_factory=list)
    model_evidence: str = "unreported"      # identified_categories | unreported(v0.3:类别计数≠单模型确认)
    include: Optional[bool] = None
    include_reason: str = ""

    extraction: Dict[str, Any] = field(default_factory=dict)
    extraction_status: str = "pending"

    @property
    def source_db(self) -> str:
        return "+".join(sorted({r.get("source", "") for r in self.source_refs if r.get("source")})) or "unknown"

    @property
    def analysis_basis(self) -> str:
        if self.ft_parsed and self.ft_used_for_verification:
            return "verified_full_text"
        if self.ft_fetched:
            return "fetched_full_text_unverified"
        if self.abstract:
            return "abstract"
        return "none"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Evidence:
    evidence_id: str
    paper_id: str
    claim: str
    sentence_kind: str = "finding"
    evidence_type: str = "direct"           # direct|author_speculation|system_inference
    evidence_level: int = 1
    location: str = ""
    sentence_index: int = 0
    model: str = ""
    conditions: List[str] = field(default_factory=list)
    triple: Dict[str, str] = field(default_factory=dict)
    # triple: subject / relation(increases|decreases|negated|other) / endpoint(具体终点名)
    direction: str = ""                     # 原始方向(increases/decreases/...)
    effect: str = ""                        # effect_on_outcome: improves|worsens|negated|unclear
    endpoint_polarity: str = ""
    mechanism_claimed: bool = False
    limitations: List[str] = field(default_factory=list)
    verified: bool = False
    verification_note: str = ""

    def dedup_key(self) -> tuple:
        return (self.paper_id, re.sub(r"\W", "", self.claim.lower())[:120],
                self.triple.get("subject", ""), self.triple.get("endpoint", ""),
                self.direction)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReverseSearchRecord:
    purpose: str = ""
    sources: List[str] = field(default_factory=list)          # 实际查询的源
    query_levels: List[Dict[str, Any]] = field(default_factory=list)
    # 每级: {level, strategy, per_source: {source: {query, request_status, http_status,
    #        total_hits, examined, relevant[]}}, examined_total, ...}
    date: str = ""
    final_status: str = "not_executed"      # ok|failed|partial|not_executed
    coverage: str = ""                      # complete_in_budget | truncated_budget | partial_source_failure
    n_examined_total: int = 0
    relevant_papers: List[Dict[str, Any]] = field(default_factory=list)
    decision: str = ""
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Gap:
    gap_id: str
    description: str
    gap_type: str
    entity: str = ""                        # 结构化实体(v0.3:不从中文描述解析)
    endpoint: str = ""                      # 结构化终点
    rationale: str = ""
    supporting_evidence_ids: List[str] = field(default_factory=list)
    contradicting_evidence_ids: List[str] = field(default_factory=list)
    already_addressed: str = ""
    reverse_search: Dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"
    status_reason: str = ""
    scores: Dict[str, Any] = field(default_factory=dict)
    limitation: str = ""
    origin: str = "v0.3"
    pairing_status: str = "pending"         # paired | single_incomplete | not_applicable

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Hypothesis:
    hypothesis_id: str
    gap_id: str
    label: str
    statement: str = ""
    rationale: str = ""
    origin: str = "system"
    supporting_evidence_ids: List[str] = field(default_factory=list)
    contradictory_evidence_ids: List[str] = field(default_factory=list)
    proposed_mechanism: str = ""
    mechanism_evidence_status: str = ""
    predictions: List[str] = field(default_factory=list)
    experimental_design: Dict[str, Any] = field(default_factory=dict)
    controls: List[str] = field(default_factory=list)
    readouts: List[str] = field(default_factory=list)
    alternative_explanations: List[str] = field(default_factory=list)
    falsification_criteria: str = ""
    novelty_check: Dict[str, Any] = field(default_factory=dict)
    feasibility: Dict[str, Any] = field(default_factory=dict)
    evidence_level: int = 3
    validation: Dict[str, Any] = field(default_factory=dict)
    rewrite_count: int = 0
    source: Dict[str, Any] = field(default_factory=dict)
    pairing_status: str = "pending"         # paired | single_incomplete
    insufficient_evidence: bool = False     # 无直接支持证据:允许输出但必须标注
    # v0.5.1 修复#6:系统生成的实验选择一律标"拟议设计",不得冒充原文验证结果;
    # 方案中无依据的具体化项(干预手段/条件/参照治疗/模型)入 pending_review,
    # 并以 design_completeness 降级,不得用模板词伪装完整。
    proposal_status: str = ""               # 非空时如 "拟议设计(系统生成,未经原文验证)"
    design_completeness: str = "complete"   # complete | degraded
    pending_review: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunLog:
    run_id: str
    entries: List[Dict[str, Any]] = field(default_factory=list)
    api_calls: List[Dict[str, Any]] = field(default_factory=list)
    _secrets: List[str] = field(default_factory=list)

    def register_secret(self, s: str) -> None:
        if s and s not in self._secrets:
            self._secrets.append(s)

    def log(self, stage: str, tool: str, status: str, detail: str = "", **kw) -> None:
        from .config import now_iso
        entry = {"ts": now_iso(), "stage": stage, "tool": tool,
                 "status": status, "detail": str(redact(detail, self._secrets))[:500]}
        entry.update(redact(kw, self._secrets))
        self.entries.append(entry)

    def log_api(self, tool: str, url: str, request_params: Dict, http_status: int,
                ok: bool, n_results: int, snapshot: Any = None,
                replayable: bool = False, note: str = "") -> None:
        """审计记录。replayable=True 表示保存了可重放的响应快照;
        False 表示仅保存可重新查询的参数(可重查≠可重放)。"""
        from .config import now_iso
        snap = snapshot[:1] if isinstance(snapshot, list) else snapshot
        self.api_calls.append({
            "ts": now_iso(), "tool": tool, "url": url,
            "request": redact(dict(request_params or {}), self._secrets),
            "http_status": http_status, "ok": ok, "n_results": n_results,
            "snapshot": redact(snap, self._secrets),
            "replayable": replayable,
            "note": redact(note, self._secrets),
        })

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "entries": self.entries,
                "api_calls": self.api_calls}


def paper_key(doi: str, pmid: str, title: str) -> Dict[str, str]:
    doi = (doi or "").strip().lower()
    pmid = str(pmid or "").strip()
    if doi:
        primary, kind = doi, "doi"
    elif pmid:
        primary, kind = f"pmid:{pmid}", "pmid"
    else:
        primary, kind = "title:" + norm_title(title), "title"
    return {"primary": primary, "kind": kind, "doi": doi, "pmid": pmid,
            "title_key": "t:" + norm_title(title)}


def validate_items(items: List[Any], required: List[str]) -> List[str]:
    errs = []
    for it in items:
        d = it.to_dict() if hasattr(it, "to_dict") else it
        for k in required:
            if not d.get(k):
                errs.append(f"{type(it).__name__} missing {k}: {str(d)[:80]}")
    return errs

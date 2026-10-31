"""
Node 0:研究问题规划器 v0.3
修复:
- 输出结构化 ResearchQuestion(实体/关系/结局/条件,来自当前主题),贯穿 Gap 反查/假设/实验设计
- 无词库命中时:status=pending_planning 并用主题自身词,不得静默回退旧主题
- 每条查询分别编译 Europe PMC 方言与 OpenAlex 方言(字段语法不兼容,不复用)
- 检查所有检索式语法:不允许 AND () / OR () 空组
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from .schemas import ResearchQuestion, RunLog

SYNONYM_LEXICON: Dict[str, Dict[str, List[str]]] = {
    "hypoxia": {
        "concept": "A:缺氧/肿瘤微环境",
        "zh": ["缺氧", "低氧", "氧剥夺", "肿瘤微环境", "微环境"],
        "terms": ["tumor hypoxia", "tumour hypoxia", "hypoxic tumor microenvironment",
                  "hypoxic tumour microenvironment", "tumor oxygenation", "oxygen deprivation",
                  "hypoxia-inducible factor", "HIF-1", "HIF-1alpha"],
    },
    "icb": {
        "concept": "B:免疫检查点阻断",
        "zh": ["免疫检查点", "检查点阻断", "检查点抑制剂", "免疫治疗", "免疫疗法",
               "pd-1", "pd-l1", "pd1", "pdl1", "纳武利尤单抗", "帕博利珠单抗"],
        "terms": ["immune checkpoint blockade", "immune checkpoint inhibitor", "PD-1", "PD-L1",
                  "programmed death 1", "programmed death ligand 1", "anti-PD-1", "anti-PD-L1",
                  "CTLA-4", "nivolumab", "pembrolizumab", "atezolizumab", "durvalumab", "ipilimumab"],
    },
    "resistance": {
        "concept": "C:耐药/免疫逃逸",
        "zh": ["耐药", "抗药", "抵抗", "免疫逃逸", "逃逸", "无应答", "不应答",
               "耐药性", "治疗失败", "超进展"],
        "terms": ["resistance", "acquired resistance", "primary resistance", "immune evasion",
                  "immune resistance", "non-response", "treatment failure", "hyperprogression"],
    },
    "mechanism": {
        "concept": "D:机制/细胞群",
        "zh": ["机制", "因果", "介导", "耗竭", "巨噬细胞", "髓源", "代谢", "血管生成"],
        "terms": ["mechanism", "causal", "mediates", "T cell exhaustion",
                  "tumor-associated macrophage", "myeloid-derived suppressor cell", "MDSC",
                  "CD8 positive T cell", "effector T cell", "reactive oxygen species",
                  "angiogenesis", "adenosine", "VEGF"],
    },
    "exosome": {
        "concept": "E:细胞外囊泡/外泌体",
        "zh": ["外泌体", "胞外囊泡", "细胞外囊泡", "exosome", "外分泌"],
        "terms": ["exosome", "exosomes", "extracellular vesicle", "extracellular vesicles",
                  "small extracellular vesicle", "ectosome", "microvesicle"],
    },
    "macrophage": {
        "concept": "F:巨噬细胞极化",
        "zh": ["巨噬细胞", "极化", "m1", "m2", "tam"],
        "terms": ["macrophage polarization", "M1 macrophage", "M2 macrophage",
                  "tumor-associated macrophage", "macrophage", "TAM",
                  "macrophage phenotype", "alternative activation"],
    },
    "metastasis": {
        "concept": "G:转移",
        "zh": ["转移", "侵袭", "迁移", "播散", "metastasis"],
        "terms": ["metastasis", "metastatic", "metastases", "tumor dissemination",
                  "invasion", "epithelial-mesenchymal transition", "EMT", "pre-metastatic niche"],
    },
}

MECHANISM_HINTS = ["HIF-1", "HIF-2", "VEGF", "adenosine", "CD39", "CD73", "A2A receptor",
                   "lactate", "MDSC", "tumor-associated macrophage", "T cell exhaustion",
                   "CXCR4", "angiogenesis", "extracellular matrix", "acidosis"]
CONTRADICTION_TERMS = ["meta-analysis", "systematic review", "contradictory", "conflicting",
                       "inconsistent", "replication", "controversy"]

STOPWORDS = {"and", "or", "of", "the", "in", "with", "to", "for", "与", "和",
             "、", ",", ";", "×", "x", "癌", "肿瘤"}


def _pick_group(term_lower: str) -> str | None:
    for gname, ginfo in SYNONYM_LEXICON.items():
        for t in ginfo["terms"]:
            if t.lower() in term_lower or term_lower in t.lower():
                return gname
        for z in ginfo.get("zh", []):
            if z in term_lower:
                return gname
    return None


def validate_query_syntax(q: str) -> List[str]:
    """检查检索式语法:空组 / AND () / OR () / 末尾悬挂运算符。"""
    errs = []
    if re.search(r"\b(AND|OR|NOT)\s*\(\s*\)", q, re.I):
        errs.append("空布尔组 ()")
    if re.search(r"\b(AND|OR|NOT)\s*$", q.strip(), re.I):
        errs.append("末尾悬挂运算符")
    if q.count("(") != q.count(")"):
        errs.append("括号不配对")
    return errs


@dataclass
class SearchPlan:
    topic: str
    rq: ResearchQuestion = field(default_factory=ResearchQuestion)
    concepts: Dict[str, List[str]] = field(default_factory=dict)
    concept_labels: Dict[str, str] = field(default_factory=dict)
    queries: List[Dict[str, str]] = field(default_factory=list)
    # 每条: {name, strategy, query(逻辑), epmc_query, openalex_query}
    year_from: int = 2015
    stop_conditions: List[str] = field(default_factory=list)
    boundaries: Dict[str, str] = field(default_factory=dict)
    syntax_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"topic": self.topic, "research_question": self.rq.to_dict(),
                "concepts": self.concepts, "concept_labels": self.concept_labels,
                "queries": self.queries, "year_from": self.year_from,
                "boundaries": self.boundaries, "stop_conditions": self.stop_conditions,
                "syntax_errors": self.syntax_errors}


def _oa_quote(term: str) -> str:
    """OpenAlex title_and_abstract.search 用词组引号。"""
    t = term.replace('"', "")
    return f'"{t}"' if " " in t else t


def build_plan(topic: str, year_from: int = 2015, run_log: RunLog | None = None) -> SearchPlan:
    topic_l = (topic or "").lower()
    plan = SearchPlan(topic=topic, year_from=year_from)
    rq = ResearchQuestion(topic_zh=topic)
    plan.rq = rq

    matched = {g: False for g in SYNONYM_LEXICON}
    for gname, ginfo in SYNONYM_LEXICON.items():
        for t in ginfo["terms"]:
            if t.lower() in topic_l:
                matched[gname] = True
                break
        if not matched[gname]:
            for z in ginfo.get("zh", []):
                if z in topic_l:
                    matched[gname] = True
                    break

    any_matched = any(matched.values())
    for gname, hit in matched.items():
        if hit:
            plan.concepts[gname] = list(SYNONYM_LEXICON[gname]["terms"])
            plan.concept_labels[gname] = SYNONYM_LEXICON[gname]["concept"]

    # ---- 自由词(当前主题内,不回退)
    free_terms: List[str] = []
    for raw in topic_l.replace(";", " ").replace(",", " ").replace("、", " ").split():
        if raw in STOPWORDS or len(raw) < 3:
            continue
        if _pick_group(raw):
            continue
        free_terms.append(raw)
    if free_terms:
        plan.concepts["free"] = free_terms
        plan.concept_labels["free"] = "用户自定义关键词(当前主题)"

    # ---- 结构化研究问题(来自当前主题命中组)
    def _fill_rq():
        rq.concept_groups = {g: plan.concepts[g] for g in plan.concepts if g != "free"}
        rq.entities = []
        rq.relations = []
        rq.outcomes = []
        rq.conditions = []
        for g in ("hypoxia", "exosome", "macrophage", "mechanism"):
            if g in plan.concepts:
                rq.entities.extend(plan.concepts[g][:3])
        for g in ("icb",):
            if g in plan.concepts:
                rq.conditions.extend(plan.concepts[g][:3])
        for g in ("resistance", "metastasis"):
            if g in plan.concepts:
                rq.outcomes.extend(plan.concepts[g][:3])
        rq.relations = ["promotes", "inhibits", "mediates", "is necessary for"]
        # v0.4 修复#8:稳定顺序(不用无序 set),保证跨运行可复现
        _seen = set()
        _ordered = []
        for g in plan.concepts:
            if g == "free":
                continue
            for t in plan.concepts[g][:2]:
                if t not in _seen:
                    _seen.add(t)
                    _ordered.append(t)
        rq.topic_terms_en = _ordered[:8]
        if not rq.topic_terms_en and free_terms:
            rq.topic_terms_en = free_terms[:6]

    _fill_rq()
    if not any_matched:
        rq.status = "pending_planning"
        rq.status_note = (f"主题 '{topic}' 未命中任何概念词库:已用主题自身词构造检索,"
                          f"并标记待人工规划确认;系统不会静默回退到其他主题")
        if run_log:
            run_log.log("Node0_Planner", "rule_based", "pending_planning",
                        rq.status_note[:200])

    # ---------------- 查询构造(逻辑式 -> 双源方言)
    # 核心概念组:排除 mechanism 泛化组(机制词只用于 mechanism 检索式,
    # 不参与核心 AND,避免 'mechanism OR causal' 使检索式过严零命中)
    def _active_groups() -> List[str]:
        return [g for g in plan.concepts if g not in ("free", "mechanism")][:4]

    def q_core() -> str:
        groups = []
        for g in _active_groups():
            main = plan.concepts[g][:6]
            groups.append(f'"{main[0]}"' if len(main) == 1
                          else "(" + " OR ".join(f'"{t}"' for t in main) + ")")
        for ft in plan.concepts.get("free", [])[:4]:
            groups.append(f'"{ft}"')
        return " AND ".join(groups) if groups else f'"{topic}"'

    def q_broad() -> str:
        groups = []
        for g in _active_groups():
            groups.append("(" + " OR ".join(f'"{t}"' for t in plan.concepts[g]) + ")")
        for ft in plan.concepts.get("free", [])[:6]:
            groups.append(f'"{ft}"')
        return " AND ".join(groups[:4]) if groups else f'"{topic}"'

    def _mech_block() -> str:
        return "(" + " OR ".join(f'"{m}"' for m in MECHANISM_HINTS[:10]) + ")"

    def _contradiction_block() -> str:
        return "(" + " OR ".join(f'"{c}"' for c in CONTRADICTION_TERMS[:8]) + ")"

    def q_mech():
        # 机制检索:核心概念(排除 mechanism 组自身)+ 机制线索词
        return f"({q_core()}) AND {_mech_block()}"

    logical_queries = [
        {"name": "precision", "strategy": "高精度检索(排除综述,保原始研究)",
         "query": q_core() + ' NOT PUB_TYPE:"Review"'},
        {"name": "recall", "strategy": "扩展召回检索(排除综述)",
         "query": q_broad() + ' NOT PUB_TYPE:"Review"'},
        {"name": "mechanism", "strategy": "机制检索(排除综述)",
         "query": f"({q_mech()}) NOT PUB_TYPE:\"Review\""},
        {"name": "contradiction", "strategy": "冲突/反证检索(含 meta 分析)",
         "query": f"({q_core()}) AND {_contradiction_block()}"},
    ]

    # ---- 方言编译
    def to_epmc(logical: str) -> str:
        """Europe PMC:保留布尔;NOT PUB_TYPE 已是 EPMC 语法。"""
        return logical

    def to_openalex(logical: str) -> str:
        """OpenAlex:filter 语法。v0.4 修复#8:使用 logical 参数使四类策略
        有真实差异(而非四策略同查询)。
        - precision:核心概念组内 OR、组间 AND(高精度)
        - recall:扩展召回(每组取更多同义词,组间 AND)
        - mechanism:核心概念 AND 机制线索词
        - contradiction:核心概念 AND 冲突/反证词
        概念组内用 OR(避免 AND 过严零命中),组间用 AND。"""
        # 按策略选择同义词数量与附加块
        if "mechanism" in logical:
            n_terms = 2
            extra = _mech_block()
        elif "contradiction" in logical:
            n_terms = 2
            extra = _contradiction_block()
        elif "recall" in logical:
            n_terms = 4
            extra = None
        else:  # precision
            n_terms = 1
            extra = None
        groups = []
        for g in _active_groups():
            terms = plan.concepts[g][:n_terms]
            if terms:
                groups.append(" OR ".join(_oa_quote(t) for t in terms))
        for ft in plan.concepts.get("free", [])[:n_terms]:
            groups.append(_oa_quote(ft))
        if not groups:
            groups = [_oa_quote(topic)]
        search = " AND ".join(groups)
        if extra:
            search = f"({search}) AND {extra}"
        return f'title_and_abstract.search:{search}'
        # 注:OpenAlex 的 title_and_abstract.search 支持 OR/AND 布尔;组内 OR 保证召回
        # 注:OpenAlex 的 title_and_abstract.search 支持 OR/AND 布尔;组内 OR 保证召回

    plan.queries = []
    for lq in logical_queries:
        epmc_q = to_epmc(lq["query"])
        # v0.4 修复#8:传策略名(lq["name"])而非查询串,使四类策略有真实差异
        oa_q = to_openalex(lq["name"])
        errs = validate_query_syntax(epmc_q) + (validate_query_syntax(oa_q) if "(" in oa_q and ")" in oa_q else [])
        plan.syntax_errors.extend([f"{lq['name']}/{e}" for e in errs])
        plan.queries.append({**lq, "epmc_query": epmc_q, "openalex_query": oa_q})

    plan.boundaries = {
        "发表时间": f">= {year_from}",
        "文献类型": "研究论文与综述(摘要层)",
        "全文要求": "开放获取全文链接仅为线索;未获取不标已核验",
        "语言": "英文文献",
    }
    plan.stop_conditions = [
        "四类检索式在启用的各源全部执行并合并去重",
        "连续扩展检索新增相关文献 < 5%",
        "达到单源请求预算(记录截断状态)",
    ]
    if run_log:
        run_log.log("Node0_Planner", "rule_based",
                    "pending_planning" if rq.status == "pending_planning" else "ok",
                    f"groups={list(plan.concepts)} queries={len(plan.queries)} "
                    f"syntax_errors={len(plan.syntax_errors)}")
    return plan

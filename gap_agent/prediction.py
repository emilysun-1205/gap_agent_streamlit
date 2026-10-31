"""
结构化预测解析与竞争判别(v0.5 修复#3;v0.5.1 修复#4/#5)。

v0.5.1 修复要点(针对独立复验反例):
  A. "HIF1 knockout reduces tumor volume" vs "HIF1 overexpression increases
     tumor volume" 曾被判 True —— 干预不同(敲除 vs 过表达),两结果可同时
     成立,不能仅凭共同读出+相反方向确认竞争。
  B. "HIF1 knockout reduces tumor volume" vs "VEGF knockout increases tumor
     volume" 曾被判 True —— 靶点不同,同样可同时成立。
  C. 中文 "在相同条件下,HIF1敲除使肿瘤体积增加/降低" 曾判 False —— 中文
     共同读出未被识别。

判别原则(逐字段参与,不再"解析后闲置"):
  1. 解析出 target(靶点)、intervention(干预)、condition(条件)、
     readouts(读出+方向)、direction、negated。
  2. 竞争判别要求:同一可比较实验(同靶点、同干预方式、同条件)下,
     同一读出方向相反(或一方无效应)。
  3. 靶点或干预不同 → 不自动判竞争;若提供了共同实验设计(shared_design)
     且两假设对该设计给出可区分预测,才可判竞争;否则 needs_manual。
  4. 缺失字段不视为"已确认相同":条件缺失时只能标 needs_manual(除非双方
     都缺失且读出/靶点/干预一致,此时按"未指定条件"处理并记录理由)。
  5. 每次判别返回逐字段理由(judge_pair),供审计与报告引用。

重写(rewrite_opposite → propose_opposite_candidate):
  方向替换只作为"待审候选"生成步骤;候选必须通过 judge_pair 的完整核对
  (靶点/干预/条件/读出/反证)才算通过。机制依据不足时返回 needs_manual,
  不强行制造相反结论。每次重写保留前后版本、依据、验证结果与失败原因。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ---- 方向词(英文) ----
_UP_WORDS = (r"\b(increase|increases|increased|increasing|upregulat\w*|promote|promotes|promoted|promoting|"
             r"enhance|enhances|enhanced|enhancing|elevate|elevates|elevated|elevating|"
             r"improve|improves|improved|improving|boost|boosts|boosted|boosting|higher|greater)\b")
_DOWN_WORDS = (r"\b(decrease|decreases|decreased|decreasing|reduce|reduces|reduced|reducing|"
               r"inhibit|inhibits|inhibited|inhibiting|suppress|suppresses|suppressed|suppressing|"
               r"downregulat\w*|impair|impairs|impaired|impairing|attenuate|attenuates|attenuated|attenuating|"
               r"block|blocks|blocked|blocking|lower|less)\b")
_UP_PAT = re.compile(_UP_WORDS, re.I)
_DOWN_PAT = re.compile(_DOWN_WORDS, re.I)

# ---- 中文方向词 ----
_ZH_UP = re.compile(r"(增加|升高|上调|促进|增强|提升|改善|提高|增多|有利|有益|加剧|加重)")
_ZH_DOWN = re.compile(r"(减少|降低|下调|抑制|减弱|下降|不利|有害|阻碍|阻断|减轻|缓解)")
_ZH_NULL = re.compile(r"(无显著影响|无显著改变|不影响|不改变|无效应|无作用|无显著差异)")

# ---- 否定词(翻转方向) ----
_NEG_PAT = re.compile(r"\b(no|not|without|lack|lacking|absence|absent|fail|fails|failed|failing|"
                      r"does not|do not|did not|never|hardly|scarcely)\b", re.I)
_ZH_NEG = re.compile(r"(不|未|无|没有|缺乏|无法|不能)")

# ---- 读出/终点词(英文) ----
_READOUT_HINTS = re.compile(
    r"\b(tumor (?:growth|volume|size|burden|weight)|tumor volume|tumor growth|"
    r"tumor size|tumor burden|metastasis|metastases|metastatic|"
    r"t[- ]?cell infiltration|t[- ]?cell (?:infiltration|recruitment|activation|cytotoxicity|response)|"
    r"infiltration|survival|overall survival|progression[- ]?free survival|"
    r"apoptosis|proliferation|angiogenesis|invasion|migration|"
    r"immune evasion|immune response|resistance|sensitivity|efficacy|"
    r"response rate|objective response|treatment response|response|"
    r"cytokine|ifn[- ]?γ|tumor[- ]?infiltrating|"
    r"expression|level|activity|function|polarization|differentiation)\b", re.I)

# ---- 读出/终点词(中文;v0.5.1 修复#4:中文共同读出识别) ----
_ZH_READOUT_HINTS = re.compile(
    r"(肿瘤体积|肿瘤生长|肿瘤大小|肿瘤负荷|肿瘤重量|瘤体积|瘤重|"
    r"转移灶|转移|肺转移|肝转移|"
    r"T细胞浸润|CD8\+?T细胞浸润|免疫细胞浸润|浸润|"
    r"生存期|总生存|无进展生存|生存|"
    r"凋亡|增殖|血管生成|侵袭|迁移|"
    r"免疫逃逸|免疫应答|免疫反应|耐药|敏感性|疗效|"
    r"客观缓解率|缓解率|应答率|"
    r"细胞因子|干扰素|极化|分化|表达|活性|功能)")

# 中文读出 → 规范读出键(用于跨语言共同读出匹配)
_ZH_READOUT_CANON = {
    "肿瘤体积": "tumor volume", "瘤体积": "tumor volume",
    "肿瘤生长": "tumor growth", "肿瘤大小": "tumor size",
    "肿瘤负荷": "tumor burden", "肿瘤重量": "tumor weight", "瘤重": "tumor weight",
    "转移灶": "metastasis", "转移": "metastasis", "肺转移": "metastasis",
    "肝转移": "metastasis",
    "T细胞浸润": "t-cell infiltration", "CD8+T细胞浸润": "t-cell infiltration",
    "CD8T细胞浸润": "t-cell infiltration", "免疫细胞浸润": "infiltration",
    "浸润": "infiltration",
    "生存期": "survival", "总生存": "overall survival",
    "无进展生存": "progression-free survival", "生存": "survival",
    "凋亡": "apoptosis", "增殖": "proliferation", "血管生成": "angiogenesis",
    "侵袭": "invasion", "迁移": "migration",
    "免疫逃逸": "immune evasion", "免疫应答": "immune response",
    "免疫反应": "immune response", "耐药": "resistance", "敏感性": "sensitivity",
    "疗效": "efficacy", "客观缓解率": "objective response",
    "缓解率": "response rate", "应答率": "response rate",
    "细胞因子": "cytokine", "干扰素": "ifn", "极化": "polarization",
    "分化": "differentiation", "表达": "expression", "活性": "activity",
    "功能": "function",
}

# ---- 干预词(英文 + 中文) ----
_INTERV_EN = re.compile(
    r"\b(knockout|knock[- ]?down|blockade|inhibitor|inhibition|"
    r"overexpression|overexpress\w*|silencing|deletion|"
    r"activation|agonist|antagonist|neutraliz\w*|depletion|"
    r"deficiency|loss|gain[- ]?of[- ]?function)\b", re.I)
_INTERV_ZH = re.compile(r"(敲除|敲低|敲减|过表达|抑制|阻断|沉默|缺失|激活|激动剂|拮抗剂|中和|清除|耗竭)")

# 干预方式规范化(用于"同干预方式"比较)
_INTERV_CANON = {
    "knockout": "loss_of_function", "knock-down": "loss_of_function",
    "knockdown": "loss_of_function", "silencing": "loss_of_function",
    "deletion": "loss_of_function", "depletion": "loss_of_function",
    "deficiency": "loss_of_function", "loss": "loss_of_function",
    "inhibitor": "inhibition", "inhibition": "inhibition",
    "blockade": "inhibition", "antagonist": "inhibition",
    "neutralization": "inhibition", "neutralizing": "inhibition",
    "overexpression": "gain_of_function", "overexpress": "gain_of_function",
    "activation": "gain_of_function", "agonist": "gain_of_function",
    "gain-of-function": "gain_of_function",
    "敲除": "loss_of_function", "敲低": "loss_of_function",
    "敲减": "loss_of_function", "沉默": "loss_of_function",
    "缺失": "loss_of_function", "清除": "loss_of_function",
    "耗竭": "loss_of_function",
    "过表达": "gain_of_function", "激活": "gain_of_function",
    "激动剂": "gain_of_function",
    "抑制": "inhibition", "阻断": "inhibition", "拮抗剂": "inhibition",
    "中和": "inhibition",
}

# ---- 条件词(英文 + 中文) ----
_COND_EN = re.compile(
    r"\b(in mice|in vivo|in vitro|in patients|under hypoxia|"
    r"under hypoxic conditions|in the presence of|in combination with|"
    r"in a [a-z ]+ model|in [a-z]+ cells)\b", re.I)
_COND_ZH = re.compile(r"(在相同条件下|相同条件|在缺氧条件下|缺氧条件|体内|体外|小鼠|患者|联合|模型)")

# 靶点同义词(用于"同靶点"比较;仅做规范化,不臆造新靶点)
_TARGET_SYNONYMS = {
    "hif1": "hif1", "hif-1": "hif1", "hif1a": "hif1", "hif-1α": "hif1",
    "hif2": "hif2", "hif-2": "hif2", "hif2a": "hif2",
    "vegf": "vegf", "vegf-a": "vegf", "vegfa": "vegf",
    "pd-l1": "pdl1", "pdl1": "pdl1", "cd274": "pdl1",
    "pd-1": "pd1", "pd1": "pd1", "pdcd1": "pd1",
    "ctla-4": "ctla4", "ctla4": "ctla4",
    "tgf-beta": "tgfbeta", "tgf-β": "tgfbeta", "tgfβ": "tgfbeta", "tgfbeta": "tgfbeta",
    "stat3": "stat3", "nf-kb": "nfkb", "nfκb": "nfkb", "nfkb": "nfkb",
    "mtor": "mtor", "akt": "akt", "erk": "erk", "mapk": "mapk",
    "wnt": "wnt", "notch": "notch", "egfr": "egfr", "her2": "her2",
    "cd47": "cd47", "cxcr4": "cxcr4", "ccr2": "ccr2",
    "hif1α": "hif1", "hif-1a": "hif1",
}


def _norm(s: str) -> str:
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff ]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _canon_target(t: str) -> str:
    """靶点规范化(小写、去空格、同义词表映射)。空串保持空串。"""
    if not t:
        return ""
    k = re.sub(r"\s+", "", t.lower())
    return _TARGET_SYNONYMS.get(k, k)


def _canon_intervention(i: str) -> str:
    if not i:
        return ""
    k = re.sub(r"\s+", "-", i.strip().lower())
    return _INTERV_CANON.get(k, _INTERV_CANON.get(k.replace("-", ""), k))


def _has_negation(text: str) -> bool:
    return bool(_NEG_PAT.search(text) or _ZH_NEG.search(text))


def _direction_of_text(text: str) -> str:
    """单条文本的方向:up/down/null/mixed/unknown。处理中文与否定。"""
    t = text or ""
    up = bool(_UP_PAT.search(t)) or bool(_ZH_UP.search(t))
    down = bool(_DOWN_PAT.search(t)) or bool(_ZH_DOWN.search(t))
    null = bool(_ZH_NULL.search(t))
    if null and not up and not down:
        return "null"
    if up and down:
        return "mixed"
    if up:
        return "up"
    if down:
        return "down"
    return "unknown"


def _flip_direction(d: str) -> str:
    return {"up": "down", "down": "up", "null": "up", "mixed": "mixed",
            "unknown": "unknown"}.get(d, "unknown")


def _extract_readouts(t: str) -> List[Tuple[str, str]]:
    """抽取 (规范读出键, 方向) 列表;英文与中文读出均识别并规范化。"""
    found: List[Tuple[int, str, str]] = []
    for m in _READOUT_HINTS.finditer(t):
        ro = m.group(1).lower()
        ctx = t[max(0, m.start() - 40): m.end() + 40]
        d = _direction_of_text(ctx)
        if d in ("up", "down", "null"):
            found.append((m.start(), ro, d))
    for m in _ZH_READOUT_HINTS.finditer(t):
        raw = m.group(1)
        ro = _ZH_READOUT_CANON.get(raw, raw)
        ctx = t[max(0, m.start() - 20): m.end() + 20]
        d = _direction_of_text(ctx)
        if d in ("up", "down", "null"):
            found.append((m.start(), ro, d))
    found.sort(key=lambda x: x[0])
    seen, uniq = set(), []
    for _, ro, d in found:
        if ro not in seen:
            seen.add(ro)
            uniq.append((ro, d))
    return uniq


def parse_prediction(pred: str) -> Dict:
    """把一条预测解析为结构化字段(靶点/干预/条件/读出/方向/否定)。"""
    t = pred or ""
    negated = _has_negation(t)

    # 干预词(英文优先,其次中文)
    intervention = ""
    m = _INTERV_EN.search(t)
    if m:
        intervention = m.group(1)
    else:
        m = _INTERV_ZH.search(t)
        if m:
            intervention = m.group(1)

    # 靶点:干预词之前的实体(英文 "HIF1 knockout" / 中文 "HIF1敲除")
    target = ""
    if intervention:
        if _INTERV_EN.search(t):
            m = re.search(r"([A-Za-z][A-Za-z0-9\-αβγ ]{0,30}?)\s+" + re.escape(intervention), t, re.I)
            if m and m.group(1).strip():
                cand = m.group(1).strip()
                # 排除干预词本身/纯虚词作靶点(v0.5.1 修复#4)
                if _canon_intervention(cand) not in ("inhibition", "loss_of_function",
                                                     "gain_of_function") \
                        and cand.lower() not in ("of", "in", "the", "with", "and"):
                    target = cand
        else:
            m = re.search(r"([A-Za-z0-9\-αβγ]{2,20})\s*" + re.escape(intervention), t)
            if m:
                target = m.group(1).strip()
    # v0.5.1 修复#4:支持 "干预 of 靶点" 句式("inhibition of X reduces ..."),
    # 干预词在句首时靶点在 "of" 之后;旧逻辑会误把干预词当靶点。
    if not target and intervention:
        m = re.search(re.escape(intervention) + r"\s+(?:of|对|针对)\s+"
                      r"([A-Za-z0-9\-αβγ][A-Za-z0-9\-αβγ ]{0,30})", t, re.I)
        if m:
            cand = m.group(1).strip()
            # 去掉后接读出/条件部分(如 "X in mice reduces ..." → X)
            for sep in (" in ", " under ", " with ", " vs ", " and ", " reduces ",
                        " increases ", " promotes ", " inhibits ", " improves ",
                        " decreases ", " enhances "):
                if sep in cand.lower():
                    cand = re.split(re.escape(sep), cand, flags=re.I)[0].strip()
            if len(cand) >= 1 and cand.lower() not in ("the", "a", "an"):
                target = cand
    if not target:
        # 无干预词时,取句首大写实体(如 "HIF1 reduces tumor volume")
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9\-αβγ]{1,20})\b", t)
        if m and m.group(1).lower() not in ("inhibition", "inhibiting", "blockade",
                                            "blockading", "knockout", "overexpression",
                                            "activation", "silencing", "depletion"):
            target = m.group(1)

    # 条件词
    condition = ""
    m = _COND_EN.search(t)
    if m:
        condition = m.group(1).strip()
    else:
        m = _COND_ZH.search(t)
        if m:
            condition = m.group(1).strip()

    readouts = _extract_readouts(t)
    direction = _direction_of_text(t)
    if negated and direction in ("up", "down"):
        direction = _flip_direction(direction)
    return {"raw": pred, "target": target, "target_canon": _canon_target(target),
            "intervention": intervention,
            "intervention_canon": _canon_intervention(intervention),
            "condition": condition, "readouts": readouts,
            "direction": direction, "negated": negated}


def _same_condition(a: Dict, b: Dict) -> Tuple[bool, str]:
    """同条件判定。返回 (是否同条件, 理由)。

    缺失字段不视为"已确认相同":仅当双方都无明确条件时按"未指定条件"处理
    (记录理由),否则必须规范化后一致。
    """
    ca, cb = a.get("condition", ""), b.get("condition", "")
    if not ca and not cb:
        return True, "双方均未指定条件(按未指定条件比较,非已确认相同)"
    if not ca or not cb:
        return False, f"条件字段缺失(一方为空:{ca or cb!r}),不能视为已确认相同"
    return (_norm(ca) == _norm(cb)), f"条件 {ca!r} vs {cb!r}"


def _shared_readouts(a: Dict, b: Dict) -> List[str]:
    ra = {ro for ro, _ in a.get("readouts", [])}
    rb = {ro for ro, _ in b.get("readouts", [])}
    return sorted(ra & rb)


def judge_pair(pred_a: List[str], pred_b: List[str],
               shared_design: Optional[Dict] = None) -> Dict:
    """逐字段判别 A/B 是否构成"同一可比较实验下的可区分预测"。

    返回:
      {
        "verdict": "discriminative" | "not_discriminative" | "needs_manual",
        "reason": 总体理由,
        "field_reasons": {target/intervention/condition/readout/direction: 理由},
        "pairs": [逐条配对明细],
        "shared_readouts": [...],
      }

    规则:
      - 同靶点 + 同干预方式 + 同条件 + 同读出 + 方向相反(或一方无效应)
        → discriminative
      - 靶点或干预不同:若提供 shared_design 且两假设对该设计给出可区分预测
        → discriminative(记录"共同实验设计"依据);否则 needs_manual
      - 字段缺失导致无法比较 → needs_manual
      - 读出不同/方向相同 → not_discriminative
    """
    if not pred_a or not pred_b:
        return {"verdict": "needs_manual", "reason": "A 或 B 预测为空,无法比较",
                "field_reasons": {}, "pairs": [], "shared_readouts": []}
    pa_list = [parse_prediction(p) for p in pred_a]
    pb_list = [parse_prediction(p) for p in pred_b]
    pairs, field_reasons = [], {}
    any_disc, any_needs, any_not = False, False, False
    for pa in pa_list:
        for pb in pb_list:
            fr: Dict[str, str] = {}
            ta, tb = pa["target_canon"], pb["target_canon"]
            ia, ib = pa["intervention_canon"], pb["intervention_canon"]
            same_target = bool(ta) and bool(tb) and ta == tb
            same_interv = bool(ia) and bool(ib) and ia == ib
            fr["target"] = (f"靶点 {pa['target']!r}({ta}) vs {pb['target']!r}({tb}):"
                            + ("相同" if same_target else
                               ("缺失" if not (ta and tb) else "不同")))
            fr["intervention"] = (f"干预 {pa['intervention']!r}({ia}) vs {pb['intervention']!r}({ib}):"
                                  + ("相同方式" if same_interv else
                                     ("缺失" if not (ia and ib) else "不同方式")))
            same_cond, cond_reason = _same_condition(pa, pb)
            fr["condition"] = cond_reason
            shared = _shared_readouts(pa, pb)
            fr["readout"] = (f"共同读出={shared}" if shared else
                             f"无共同读出(A={[r for r,_ in pa['readouts']]},"
                             f"B={[r for r,_ in pb['readouts']]})")
            # 方向:优先用共同读出的方向
            da, db = pa["direction"], pb["direction"]
            if shared:
                da = next((d for r, d in pa["readouts"] if r in shared), da)
                db = next((d for r, d in pb["readouts"] if r in shared), db)
            opposite = ((da in ("up", "down") and db in ("up", "down") and da != db)
                        or (da == "null" and db in ("up", "down"))
                        or (db == "null" and da in ("up", "down")))
            fr["direction"] = f"A={da} vs B={db}:{'相反/可区分' if opposite else '不相反'}"

            if not shared:
                verdict = "not_discriminative"
                fr["verdict"] = "无共同读出:两结果可同时成立"
            elif not opposite:
                verdict = "not_discriminative"
                fr["verdict"] = "共同读出方向不相反:不构成可区分预测"
            elif not same_cond:
                verdict = "needs_manual"
                fr["verdict"] = "条件不可比:需明确共同实验条件"
            elif same_target and same_interv:
                verdict = "discriminative"
                fr["verdict"] = "同靶点+同干预方式+同条件+同读出方向相反:可区分"
            elif shared_design:
                # 靶点/干预不同但提供了共同实验设计:需两假设对该设计给出
                # 可区分预测(此处以"共同读出+相反方向+共同设计"为充分条件,
                # 并在理由中记录设计依据)
                verdict = "discriminative"
                fr["verdict"] = ("靶点/干预不同,但提供共同实验设计且对该设计"
                                 "给出相反预测:可区分(依据 shared_design)")
                fr["shared_design"] = str(shared_design)[:300]
            else:
                verdict = "needs_manual"
                fr["verdict"] = ("靶点或干预不同且无共同实验设计:两结果可同时成立,"
                                 "不能自动认定竞争")
            pairs.append({"a": pa["raw"], "b": pb["raw"], "verdict": verdict,
                          "field_reasons": fr})
            if verdict == "discriminative":
                any_disc = True
            elif verdict == "needs_manual":
                any_needs = True
            else:
                any_not = True
            field_reasons = fr  # 保留最后一对(报告引用)
    if any_disc:
        verdict = "discriminative"
        reason = "存在同条件同读出方向相反的可比较配对"
    elif any_needs:
        verdict = "needs_manual"
        reason = "存在方向相反但靶点/干预/条件不可比或缺失的配对:需人工核查共同实验设计"
    else:
        verdict = "not_discriminative"
        reason = "无共同读出或方向不相反:不构成可区分预测"
    return {"verdict": verdict, "reason": reason, "field_reasons": field_reasons,
            "pairs": pairs,
            "shared_readouts": sorted({r for p in pairs for r in
                                       _shared_readouts(parse_prediction(p["a"]),
                                                        parse_prediction(p["b"]))})}


def is_discriminative_pair(pred_a: List[str], pred_b: List[str],
                           shared_design: Optional[Dict] = None) -> bool:
    """兼容旧接口:仅当 judge_pair 判定 discriminative 时返回 True。

    needs_manual / not_discriminative 均返回 False(不得自动认定竞争)。
    """
    return judge_pair(pred_a, pred_b, shared_design)["verdict"] == "discriminative"


def direction_of(preds: List[str]) -> str:
    """整体方向(兼容旧语义,供重写使用)。"""
    dirs = [parse_prediction(p)["direction"] for p in preds or []]
    ups = sum(1 for d in dirs if d == "up")
    downs = sum(1 for d in dirs if d == "down")
    if ups and downs:
        return "mixed"
    if ups:
        return "up"
    if downs:
        return "down"
    return "mixed"


# ================================================================ 重写(v0.5.1 修复#5)
def propose_opposite_candidate(pred_b: List[str], a_preds: List[str],
                               b_design: Optional[Dict] = None,
                               b_controls: Optional[List[str]] = None,
                               b_readouts: Optional[List[str]] = None,
                               b_falsification: str = "") -> Dict:
    """依据机制重建 B 的相反预测候选(不机械翻转方向词)。

    流程:
      1. 解析 A/B 结构化字段,确定共同读出与 A 的方向。
      2. 仅对"有共同读出且方向明确"的 B 预测生成相反方向候选;候选文本
         保留靶点/干预/条件措辞,只改方向表述,并标注为"待审候选"。
      3. 用 judge_pair 对候选做完整核对(靶点/干预/条件/读出/方向)。
      4. 同步核对实验设计/对照/读出/反证标准是否覆盖共同读出;缺失则
         记录为待补项。
      5. 机制依据不足(无共同读出/无方向/靶点缺失)→ status=needs_manual,
         不强行制造相反结论。

    返回:
      {
        "status": "candidate_verified" | "candidate_pending_review" | "needs_manual",
        "before": [...], "after": [...],
        "basis": [逐条结构化解析],
        "judge": judge_pair 结果,
        "design_check": {design/controls/readouts/falsification: 覆盖情况},
        "failures": [失败原因],
      }
    """
    before = list(pred_b or [])
    a_parsed = [parse_prediction(p) for p in (a_preds or [])]
    b_parsed = [parse_prediction(p) for p in before]
    failures: List[str] = []

    # 共同读出(跨 A/B)
    a_readouts = {r for p in a_parsed for r, _ in p["readouts"]}
    b_readouts = {r for p in b_parsed for r, _ in p["readouts"]}
    shared = sorted(a_readouts & b_readouts)
    if not shared:
        failures.append("A/B 无共同读出:无法确定应翻转哪个读出,机制依据不足")
    if not any(p["target_canon"] for p in b_parsed):
        failures.append("B 预测缺少可识别靶点:无法核对机制一致性")

    after: List[str] = []
    for p, parsed in zip(before, b_parsed):
        if not parsed["readouts"] or parsed["direction"] not in ("up", "down"):
            after.append(p)
            failures.append(f"预测缺少明确读出或方向,原样保留待人工核查:{p[:80]}")
            continue
        flipped = _flip_direction(parsed["direction"])
        q = p
        if flipped == "up":
            q = _DOWN_PAT.sub("increases", q)
            q = _ZH_DOWN.sub("增加", q)
        elif flipped == "down":
            q = _UP_PAT.sub("reduces", q)
            q = _ZH_UP.sub("降低", q)
        after.append(q)

    judge = judge_pair(a_preds or [], after)
    # 设计/对照/读出/反证核对(v0.5.1 修复#6b:必要缺失项全部纳入阻断;
    # 区分存在性检查、内容一致性、机制依据核查)
    design_check = {}
    design_text = str(b_design or "")
    design_check["design"] = ("已提供" if design_text.strip() else "缺失:需补充共同实验设计")
    design_check["controls"] = ("已提供" if b_controls else "缺失:需补充对照")
    # 读出:存在性 + 覆盖共同读出(空集合不得因 all() 通过)
    if not b_readouts:
        design_check["readouts"] = "缺失:未提供读出(空集合不能视为覆盖通过)"
    elif not shared:
        design_check["readouts"] = "未覆盖共同读出:A/B 无共同读出,机制依据不足"
    elif all(any(s in str(r).lower() for r in b_readouts) for s in shared):
        design_check["readouts"] = "覆盖共同读出"
    else:
        design_check["readouts"] = "未覆盖共同读出:需补充读出"
    design_check["falsification"] = ("已提供" if (b_falsification or "").strip()
                                     else "缺失:需补充反证标准")
    # 存在性阻断:设计/对照/反证任一缺失即不能自动通过
    if design_check["design"].startswith("缺失"):
        failures.append("缺少共同实验设计:候选不能自动通过科学配对核验")
    if design_check["controls"].startswith("缺失"):
        failures.append("缺少对照:候选不能自动通过科学配对核验")
    if design_check["falsification"].startswith("缺失"):
        failures.append("缺少反证标准:候选不能自动通过科学配对核验")
    # 内容一致性/机制依据阻断:读出缺失或未覆盖共同读出
    if design_check["readouts"].startswith("缺失") or design_check["readouts"].startswith("未覆盖"):
        failures.append(design_check["readouts"])

    if judge["verdict"] == "discriminative" and not failures:
        status = "candidate_verified"
    elif judge["verdict"] == "discriminative":
        status = "candidate_pending_review"
    else:
        status = "needs_manual"
        failures.append(f"候选判别未通过:{judge['reason']}")

    return {"status": status, "before": before, "after": after,
            "basis": b_parsed, "judge": judge, "design_check": design_check,
            "failures": failures,
            "note": "方向替换仅为待审候选生成步骤;通过与否以 judge_pair 与设计核对为准"}


def rewrite_opposite(pred_b: List[str], a_dir: str,
                     a_preds: Optional[List[str]] = None) -> List[str]:
    """兼容旧接口:返回相反方向候选文本(仅候选,不代表已通过核验)。

    调用方必须用 judge_pair / propose_opposite_candidate 复核,不得单凭本函数
    输出自动通过科学配对核验。
    """
    if a_preds:
        return propose_opposite_candidate(pred_b, a_preds)["after"]
    out = []
    for p in pred_b or []:
        parsed = parse_prediction(p)
        if parsed["direction"] in ("up", "down") and parsed["readouts"]:
            flipped = _flip_direction(parsed["direction"])
            q = p
            if flipped == "up":
                q = _DOWN_PAT.sub("increases", q)
                q = _ZH_DOWN.sub("增加", q)
            elif flipped == "down":
                q = _UP_PAT.sub("reduces", q)
                q = _ZH_UP.sub("降低", q)
            out.append(q)
        else:
            out.append(p)
    return out

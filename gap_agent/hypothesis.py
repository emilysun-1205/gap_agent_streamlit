"""
Node 3:科学假设架构师 v0.3
修复:
- 竞争配对:B 失败时有限次数重写(HYP_MAX_REWRITES);仍失败则 Gap.pairing_status=
  single_incomplete 并在假设上标注,不冒充配对交付
- 结构化 RQ 贯穿:陈述/机制链/预测/实验设计使用当前主题实体/结局/条件;
  绝不回退旧主题(缺氧/ICB/anti-PD-1 等仅在主题命中时出现)
- 判别性由结构保证(A/B 对同一关系给出相反预测),不靠"分层/交互"关键词
- 无直接支持证据 → insufficient_evidence=True,输出时必须标注证据不足
- 新颖性核查多源;needs_manual/truncated 不给高新颖性
"""
from __future__ import annotations

import re
from typing import Dict, List

from .config import HYP_MAX_REWRITES, RS_EXAMINED_PER_LEVEL, now_iso
from .schemas import Gap, Hypothesis, ReverseSearchRecord, RunLog
from .gap_hunter_lex import ENTITY_SYNONYMS as _ENTITY_SYNONYMS_TABLE
from .gap_hunter import _is_allowed_status, _is_usable_status, _status_contract_error


def _evidence_chain(store, gap: Gap) -> Dict[str, List[str]]:
    sup, con = [], []
    for eid in gap.supporting_evidence_ids:
        e = store.evidences.get(eid)
        if e:
            sup.append(f"[{eid}] {e.claim[:140]}({e.paper_id},{e.location})")
    for eid in gap.contradicting_evidence_ids:
        e = store.evidences.get(eid)
        if e:
            con.append(f"[{eid}] {e.claim[:140]}({e.paper_id},{e.location})")
    return {"supporting": sup, "contradictory": con}


class HypothesisArchitect:
    def __init__(self, store, run_log: RunLog, plan=None):
        self.store = store
        self.log = run_log
        self.plan = plan
        self.rq = plan.rq if plan else None

    # ------------------------------------------------ 主入口
    def generate(self, gaps: List[Gap], year_from: int = 2015,
                 include_author_hypotheses: bool = True,
                 openalex_fn=None) -> List[Hypothesis]:
        hyps: List[Hypothesis] = []
        for g in gaps:
            if g.status == "dropped":
                continue
            chain = _evidence_chain(self.store, g)
            if g.gap_type == "contradiction":
                hs = self._pair_contradiction(g, chain)
            elif g.gap_type == "mechanism":
                hs = self._pair_mechanism(g, chain)
            else:
                hs = self._pair_context(g, chain)
            # 配对状态
            valid = [h for h in hs if h.label in ("A", "B")]
            if len(valid) >= 2:
                g.pairing_status = "paired"
            elif len(valid) == 1:
                g.pairing_status = "single_incomplete"
                self.log.log("HypothesisArchitect", "pairing", "incomplete",
                             f"{g.gap_id} 仅单候选({valid[0].label})通过:竞争性方案未完成,已标注")
            for h in hs:
                h.evidence_level = 3
                h.novelty_check = self._novelty_search(h, year_from, openalex_fn)
                # v0.5.1 修复#6c:全部 origin=system 假设生成后、验证前定稿
                # (proposal_status/pending_review/design_completeness 实际写入)
                self._finalize_system_hypothesis(h, g)
                hyps.append(h)
        if include_author_hypotheses:
            hyps.extend(self._collect_author_hypotheses())
        return hyps

    # ------------------------------------------------ 竞争假设有限重写(v0.4 修复#2)
    def rewrite_failed_pairs(self, gaps: List[Gap],
                             passed_hyps: List[Hypothesis]) -> List[Hypothesis]:
        """真正有限重写循环:对 A 通过但 B 未通过(或 A/B 未通过判别)的 Gap,
        用 propose_opposite_candidate 生成并完整核验 B 的相反候选,最多
        HYP_MAX_REWRITES 次,每次递增 rewrite_count。

        v0.5.1 修复#6a:主流程消费完整候选核验结果,而非只接收改写文本。
        - 传入 A/B 预测、共同实验设计、对照、读出、反证标准及机制依据;
        - candidate_pending_review / needs_manual 一律不得置 passed=True;
        - 方向相反只能证明某项预测可区分,不能替代方案完整性或机制依据核验;
        - 保留每次候选、依据、逐字段检查、失败原因及最终状态(rewrite_audit)。
        """
        from .prediction import propose_opposite_candidate
        from .validator import _is_discriminative_pair
        by_gap: Dict[str, List[Hypothesis]] = {}
        for h in passed_hyps:
            if h.origin == "system" and h.gap_id:
                by_gap.setdefault(h.gap_id, []).append(h)
        rewritten: List[Hypothesis] = []
        for g in gaps:
            if g.status == "dropped":
                continue
            hs = by_gap.get(g.gap_id, [])
            a = next((h for h in hs if h.label == "A"), None)
            b = next((h for h in hs if h.label == "B"), None)
            if not a or not b:
                continue
            # 已通过完整判别(含方案完整性)则无需重写
            if b.validation.get("passed") and _is_discriminative_pair(a.predictions, b.predictions):
                continue
            audit = {"gap_id": g.gap_id,
                     "a_predictions": list(a.predictions),
                     "b_before": list(b.predictions),
                     "attempts": []}
            final_status = None
            for attempt in range(1, HYP_MAX_REWRITES + 1):
                b_before = list(b.predictions)
                cand = propose_opposite_candidate(
                    pred_b=b_before, a_preds=a.predictions,
                    b_design=b.experimental_design,
                    b_controls=b.controls,
                    b_readouts=b.readouts,
                    b_falsification=b.falsification_criteria,
                )
                audit["attempts"].append({
                    "attempt": attempt,
                    "b_before": b_before,
                    "candidate": cand["after"],
                    "status": cand["status"],
                    "basis": cand["basis"],
                    "judge": cand["judge"],
                    "design_check": cand["design_check"],
                    "failures": cand["failures"],
                })
                if cand["status"] == "candidate_verified":
                    b.rewrite_count = attempt
                    b.predictions = cand["after"]
                    b.validation = {"passed": True, "problems": [],
                                    "action": f"竞争方案重写 {attempt} 次后通过完整候选核验",
                                    "rewrite_audit": audit}
                    rewritten.append(b)
                    self.log.log("HypothesisArchitect", "rewrite", "ok",
                                 f"{g.gap_id} B 重写 {attempt} 次后完整核验通过")
                    final_status = "candidate_verified"
                    break
                # candidate_pending_review / needs_manual:不得置 passed=True
                if cand["status"] == "candidate_pending_review":
                    b.rewrite_count = attempt
                    b.predictions = cand["after"]
                    b.validation = {"passed": False,
                                    "problems": cand["failures"],
                                    "action": "候选待人工核查:方向可区分但方案完整性/机制依据不足",
                                    "rewrite_audit": audit}
                    b.pending_review = list(getattr(b, "pending_review", []) or []) + [
                        "重写候选待人工核查:" + ";".join(cand["failures"])]
                    b.design_completeness = "degraded"
                    final_status = "candidate_pending_review"
                    break
                # needs_manual:不强行制造相反结论
                b.rewrite_count = attempt
                b.validation = {"passed": False,
                                "problems": cand["failures"],
                                "action": "机制依据不足:保留 needs_manual/single_incomplete,不强行制造相反结论",
                                "rewrite_audit": audit}
                final_status = "needs_manual"
                break
            if final_status != "candidate_verified":
                g.pairing_status = "single_incomplete"
                self.log.log("HypothesisArchitect", "rewrite", "failed",
                             f"{g.gap_id} B 重写未通过完整核验(status={final_status})")
        return rewritten

    def _opposite_predictions(self, a_dir: str, pred_b: List[str]) -> List[str]:
        """将 B 的预测改写为与 A 相反方向(v0.5 修复#3)。

        依据机制重建预测与实验设计,而非机械翻转方向词:
        解析 B 每条预测的结构化字段(机制/干预/条件/读出/方向),把每个读出的
        方向翻转为相反方向,保留机制/干预/条件措辞。若某条预测无法可靠判断
        (无读出/无方向),则原样保留,由调用方判定失败并标 needs_manual/
        single_incomplete。
        """
        from .prediction import rewrite_opposite
        return rewrite_opposite(pred_b, a_dir)

    # ------------------------------------------------ 作者假设
    def _collect_author_hypotheses(self) -> List[Hypothesis]:
        out = []
        idx = 0
        for pid, p in self.store.papers.items():
            if not p.include:
                continue
            for m in p.extraction.get("proposed_mechanisms", {}).get("author_speculative", [])[:1]:
                idx += 1
                out.append(Hypothesis(
                    hypothesis_id=f"HYP_AUTHOR_{idx:03d}", gap_id="", label="author",
                    statement=m["claim"][:400],
                    rationale="论文作者在摘要中提出的推测性表述(原文转录,非系统生成)",
                    origin="author",
                    proposed_mechanism=(m.get("entities") or [""])[0],
                    mechanism_evidence_status="作者推测;未经本文实验直接验证",
                    source={"paper_id": pid, "location": m.get("location", "")},
                ))
        self.log.log("HypothesisArchitect", "author_hypotheses", "ok", f"n={len(out)}")
        return out

    # ------------------------------------------------ 主题词(结构化,无伪造占位)
    def _topic_words(self) -> Dict[str, str]:
        rq = self.rq
        ent = rq.entities[0] if rq and rq.entities else ""
        outcome = rq.outcomes[0] if rq and rq.outcomes else ""
        cond = rq.conditions[0] if rq and rq.conditions else ""
        # v0.5.1 修复#6:主题未指定的字段返回空串,由调用方显式标"待人工核查",
        # 不再伪造"主题条件/主题关键因素/主题结局"占位词冒充已具体化。
        return {"entity": ent, "outcome": outcome, "condition": cond,
                "topic": rq.topic_zh if rq else ""}

    # --------------------------------- 实体可用的干预手段(v0.5.1 修复#6)
    # 外泌体(细胞外囊泡)与细胞群体(TAM/T 细胞等)不是单一基因,
    # 不能直接"条件性敲除";干预手段须按实体类型生成,无依据时标待设计。
    _NON_GENE_VESICLE = re.compile(
        r"exosome|外泌体|extracellular vesicle|\bEVs?\b|微囊泡", re.I)
    _NON_GENE_CELL = re.compile(
        r"\bTAMs?\b|tumor[- ]associated macrophage|巨噬细胞|macrophage|"
        r"\bT cells?\b|T细胞|MDSC|髓源性抑制细胞|fibroblast|成纤维细胞|"
        r"neutrophil|中性粒|dendritic cell|树突状", re.I)

    def _intervention_options(self, ent: str) -> Dict[str, str]:
        """按实体类型给出可用干预手段与待核查说明(不编造具体分子)。"""
        e = ent or ""
        if self._NON_GENE_VESICLE.search(e):
            return {"text": (f"{e} 干预手段待设计(不能对{e}直接条件性敲除:"
                             "非单一基因靶点;可选项如来源细胞分泌抑制/"
                             "囊泡摄取/清除策略——具体分子靶点待人工核查)"),
                    "non_gene": True,
                    "pending": f"「{e}」为细胞外囊泡/细胞群体,非单一基因:不能直接条件性敲除;"
                               "具体干预分子靶点待设计并人工核查"}
        if self._NON_GENE_CELL.search(e):
            return {"text": (f"{e} 干预手段待设计(不能对{e}细胞群体直接条件性敲除:"
                             "可选项如亚群清除/极化调节/关键分子条件性敲除——"
                             "具体靶点待人工核查)"),
                    "non_gene": True,
                    "pending": f"「{e}」为细胞群体,非单一基因:不能整体条件性敲除;"
                               "具体干预靶点(亚群/极化/关键分子)待设计并人工核查"}
        # 分子靶点(基因/蛋白,如 VEGF/HIF1):抑制剂/条件性敲除可用,
        # 但具体试剂/cre 系仍属系统拟议,未经原文验证。
        return {"text": f"{e} 抑制剂/条件性敲除(具体试剂与 cre 系统待人工核查)",
                "non_gene": False,
                "pending": f"「{e}」干预具体试剂/敲除策略为系统拟议设计,未经原文验证,待人工核查"}

    # ---------------- 系统假设定稿:拟议设计标注+未完成占位扫描(v0.5.1 修复#6)
    _PLACEHOLDER_SCANS = [
        (re.compile(r"主题条件|主题关键因素|主题结局"), "主题词占位符未具体化"),
        (re.compile(r"平行\s*ent\s*干预臂|(?<![A-Za-z0-9])ent(?![A-Za-z0-9])"),
         "实体占位符 ent 未替换"),
    ]

    def _finalize_system_hypothesis(self, h: Hypothesis, g: Gap) -> None:
        """所有 origin=system 假设定稿:
        1) proposal_status 标"拟议设计"(系统生成的实验选择,未经原文验证);
        2) 扫描陈述/预测/设计/对照/读出/反证中的未完成占位符与无依据模板词,
           记入 pending_review 并把 design_completeness 降为 degraded;
        3) 裸"标准治疗/相关模型"(未附待核查限定)同样记待人工核查。
        """
        pending: List[str] = list(getattr(h, "pending_review", []) or [])
        texts = ([h.statement, h.rationale, h.proposed_mechanism,
                  h.falsification_criteria, h.mechanism_evidence_status]
                 + list(h.predictions) + list(h.controls) + list(h.readouts)
                 + list(h.alternative_explanations)
                 + [f"{k}:{v}" for k, v in (h.experimental_design or {}).items()]
                 + [f"{k}:{v}" for k, v in (h.feasibility or {}).items()])
        blob = "\n".join(t for t in texts if t)
        for pat, label in self._PLACEHOLDER_SCANS:
            m = pat.search(blob)
            if m:
                pending.append(f"{label}(出现于:{m.group(0)[:30]})——需具体化后人工核查")
        # 裸"标准治疗参照/标准治疗/相关模型":只有给出具体方案/模型名称才算具体化;
        # 后跟"待人工核查/待设计/未编造"仍属未具体化(未给出方案),记待设计并降级。
        for m in re.finditer(r"标准治疗参照|标准治疗", blob):
            tail = blob[m.end(): m.end() + 20]
            if ("待人工核查" in tail or "待设计" in tail or "未编造" in tail
                    or "(" not in tail):
                pending.append("「标准治疗参照」未指明具体方案:待设计并人工核查(不得编造一线方案)")
                break
        for m in re.finditer(r"相关模型", blob):
            tail = blob[m.end(): m.end() + 20]
            if ("待人工核查" in tail or "待设计" in tail or "未编造" in tail
                    or "(" not in tail):
                pending.append("「相关模型」未指明具体模型:待设计并人工核查(不得编造模型体系)")
                break
        # 非基因实体出现"条件性敲除"且未标注不可行说明
        if h.proposed_mechanism:
            pass
        ent = (g.entity or (self.rq.entities[0] if self.rq and self.rq.entities else "") or "")
        if ent:
            iv = self._intervention_options(ent)
            if iv["non_gene"] and "条件性敲除" in blob and "不能" not in blob:
                pending.append(iv["pending"])
        h.proposal_status = "拟议设计(系统生成,未经原文实验验证)"
        h.pending_review = pending
        h.design_completeness = "degraded" if pending else "complete"

    # ------------------------------------------------ 冲突型配对
    def _pair_contradiction(self, g: Gap, chain) -> List[Hypothesis]:
        w = self._topic_words()
        ent = g.entity or w["entity"]
        endpoint = g.endpoint or w["outcome"]
        base = g.gap_id.replace("GAP_", "")
        sup = g.supporting_evidence_ids
        A = Hypothesis(
            hypothesis_id=f"HYP_{base}_A", gap_id=g.gap_id, label="A", origin="system",
            statement=(f"「{ent}」对「{endpoint}」的作用方向是条件依赖的:在特定条件/模型或亚群中"
                       f"为负向,在对照条件下为正向或无效应。"),
            rationale=("现有 L1 证据的效益方向分歧可由未测量的条件变量解释:"
                       "各研究在模型、剂量或人群上存在系统差异。"),
            supporting_evidence_ids=sup,
            contradictory_evidence_ids=g.contradicting_evidence_ids,
            proposed_mechanism=f"{ent} 的效应符号由条件变量(模型/剂量/亚群)调节",
            mechanism_evidence_status="分歧现象:多篇 L1 支持;调节机制:推论边(待检验)",
            predictions=[
                f"P1(判别 A vs B):在同一研究内按条件变量分层,{ent} 与 {endpoint} 的关联方向在层间相反;若校正共同原因后关联消失,则支持 B 而非 A",
                f"P2(判别 A vs B):条件变量×{ent} 交互项显著(A);或中介比例高且工具变量不支持因果(B)——两个结果模式互斥",
                "P3:统一平台重测后跨研究异质性显著下降",
            ],
            experimental_design={
                "设计": "同一队列分层分析 + 跨研究元回归(条件变量为调节项)",
                "模型": f"{w['topic']} 相关已有数据再分析 + 前瞻验证队列",
                "主要分析": "随机效应元分析+元回归;亚组交互检验;中介/敏感性分析",
                "组别": f"按 {w['condition']} 高/低分层",
            },
            controls=["同一检测平台", "已知阴性调节变量对照", "阴性暴露对照(供 B 判别)"],
            readouts=["各层效应方向与大小", "异质性统计量(I²/Q)", "交互项效应量", "中介比例与 E-value"],
            alternative_explanations=["分歧由检测平台差异造成", "发表偏倚致负向结果未报告"],
            falsification_criteria=("若统一条件后分歧消失且无交互项,同时校正共同原因后关联稳健,"
                                    "则 A 被削弱而 B 获支持;反之亦然。"),
            feasibility={"note": "数据可行性取决于原始研究可得性;未评估团队资源(待评估)"},
        )
        B = Hypothesis(
            hypothesis_id=f"HYP_{base}_B", gap_id=g.gap_id, label="B", origin="system",
            statement=(f"「{ent}」与「{endpoint}」的表面关联由共同原因(如肿瘤免疫原性/突变负荷)驱动,"
                       f"{ent} 本身无因果作用;方向分歧反映各研究人群基线差异。"),
            rationale="相关性证据无法排除共同原因结构;冲突常见于效应实为混杂的情形。",
            supporting_evidence_ids=sup[:1],
            contradictory_evidence_ids=g.contradicting_evidence_ids,
            proposed_mechanism=f"共同原因同时驱动 {ent} 表达与 {endpoint}(三角结构)",
            mechanism_evidence_status="共同原因结构:推论边(需统计中介/敏感性验证)",
            predictions=[
                f"P1(判别 B vs A):校正共同原因指标后,{ent} 与 {endpoint} 的关联显著衰减(中介比例高);若分层后层间方向仍相反,则支持 A 而非 B",
                "P2(判别 B vs A):工具变量/孟德尔随机化不支持因果方向(B);或层间方向相反(A)——互斥结果模式",
                f"P3:共同原因指标同时预测 {ent} 水平与 {endpoint}(三角验证)",
            ],
            experimental_design={
                "设计": "统计建模再分析:中介分析 + 敏感性分析(E-value)",
                "模型": f"{w['topic']} 公共队列 + 纳入研究原始数据",
                "主要分析": "多变量校正;中介效应分解;E-value;工具变量分析",
                "组别": "按共同原因指标分层",
            },
            controls=["阴性暴露对照", "多套协变量设定下的系数稳定性", "未测量混杂定量敏感性"],
            readouts=["效应衰减比例", "E-value", "工具变量因果方向估计"],
            alternative_explanations=["真实因果效应被模型错误设定掩盖"],
            falsification_criteria="若校正后关联稳健、工具变量支持因果且中介比例低,则 B 被反驳。",
            feasibility={"note": "依赖公共数据可得性;未评估团队资源(待评估)"},
        )
        # 无直接支持证据标注
        if not sup:
            A.insufficient_evidence = True
            B.insufficient_evidence = True
        return [A, B]

    # ------------------------------------------------ 机制型配对
    def _pair_mechanism(self, g: Gap, chain) -> List[Hypothesis]:
        w = self._topic_words()
        ent = g.entity or w["entity"]
        outcome = g.endpoint or w["outcome"]
        base = g.gap_id.replace("GAP_", "")
        sup = g.supporting_evidence_ids
        cond = w["condition"]
        cond_in = f"在 {cond} 条件下" if cond else "在关键条件(主题未指定具体条件,待人工核查)下"
        cond_np = f"{cond} 标记" if cond else "关键条件标记(待人工核查)"
        cond_f = cond or "关键条件(待人工核查)"
        iv = self._intervention_options(ent)
        std = "标准治疗(具体方案待人工核查)"
        topic_m = (f"{w['topic']}相关模型体系(具体模型待人工核查)" if w["topic"]
                   else "相关模型体系(具体模型待人工核查)")
        A = Hypothesis(
            hypothesis_id=f"HYP_{base}_A", gap_id=g.gap_id, label="A", origin="system",
            statement=((f"条件「{cond}」经「{ent}」通路改变「{outcome}」:"
                        f"选择性干预 {ent} 可{cond_in}部分逆转结局。") if cond
                       else (f"关键条件(主题未指定具体条件,待人工核查)经「{ent}」通路"
                             f"改变「{outcome}」:选择性干预 {ent} 可{cond_in}部分逆转结局。")),
            rationale=f"{ent} 与结局的共现在多篇独立论文中被推测,但因果必要性缺乏干预验证。",
            supporting_evidence_ids=sup,
            proposed_mechanism=(f"{w['condition']} →(文献支持)→ {ent} 活性改变 →(推论边)→"
                                f"效应细胞功能变化 →(推论边)→ {outcome} 改变"),
            mechanism_evidence_status=(f"{w['condition']}→{ent}:文献支持;{ent}→功能变化:推论;"
                                       f"干预→结局逆转:待验证"),
            predictions=[
                f"P1(判别 A vs B):{cond_in}选择性干预 {ent} 使 {outcome} 指标部分逆转,而干预无关通路无此效应(A);若仅改善伴随病理(如灌注)即见效,则支持 B",
                f"P2(判别 A vs B):{ent} 干预+{std}的增益随 {ent} 依赖性分层升高(A);干预在不改变伴随病理时无效果(B)——互斥模式",
                f"P3(关联):{ent} 活性与 {cond_np} 正相关、与 {outcome} 相关(人体验证)",
            ],
            experimental_design={
                "设计": f"2×2×2 析因({cond_f} × {ent} 干预 × {std})",
                "模型": f"{topic_m}(体外共培养+体内)+ 人体样本",
                "干预": f"{iv['text']} × {cond_f} × {std}/对照",
                "组别": "各因素 2 水平完全交叉;含伴随病理同步监测(供 B 判别)",
                "时间点": "基线、治疗第 7/14/21 天",
            },
            controls=[f"{ent} 干预效率验证", "脱靶效应评估", "载体/同型对照", "无关通路干预对照(判别 B)"],
            readouts=[f"{outcome} 相关终点", f"{ent} 活性指标", "效应细胞功能",
                      "伴随病理指标(灌注/屏障,判别 B)"],
            alternative_explanations=[
                "干预效应来自伴随病理间接重塑而非通路本身(即 B)",
                "其他平行通路主导结局(ent 非必要)",
            ],
            falsification_criteria=(f"若有效干预 {ent} 后{cond_in}的 {outcome} 无改善,"
                                    f"或改善与通路活性解耦而与伴随病理耦合,则 A 被反驳(B 获支持)。"),
            feasibility={"note": f"需要 {topic_m};未评估团队资源(待评估)"},
        )
        B = Hypothesis(
            hypothesis_id=f"HYP_{base}_B", gap_id=g.gap_id, label="B", origin="system",
            statement=(f"「{ent}」仅为伴随标志;其与 {outcome} 的关联由伴随病理改变"
                       f"(如屏障/灌注/代谢微环境)驱动,干预 {ent} 不改变结局。"),
            rationale="竞争性解释:结局可由伴随病理独立解释,无需该实体因果参与。",
            supporting_evidence_ids=sup[:1],
            proposed_mechanism=f"{cond_f} → 伴随病理改变 → 结局变化({ent} 仅相关)",
            mechanism_evidence_status="伴随病理路径:文献部分支持;实体冗余性:推论",
            predictions=[
                f"P1(判别 B vs A):仅改善伴随病理即可部分逆转 {outcome},不依赖 {ent} 干预(B);若须 {ent} 干预才见效,则支持 A",
                f"P2(判别 B vs A):{ent} 干预在不改变伴随病理时对 {outcome} 无显著影响(B);反之为 A",
            ],
            experimental_design={
                "设计": "双臂判别(通路干预 vs 伴随病理调节)+ 联合臂",
                "模型": f"{topic_m}",
                "干预": f"伴随病理调节剂 × {std};平行 {ent} 干预臂({iv['text']})",
                "组别": "四组:联合/各单药/对照",
            },
            controls=["伴随病理改善的客观确证", "各臂等量载体", "测量标化"],
            readouts=["伴随病理指标", "结局终点", f"{ent} 活性(应与结局解耦若 B 成立)"],
            alternative_explanations=["两机制并行贡献(非互斥)"],
            falsification_criteria=f"若改善伴随病理后 {outcome} 无变化而 {ent} 干预有效,则 B 被反驳。",
            feasibility={"note": "需要伴随病理评价平台;未评估团队资源(待评估)"},
        )
        if not sup:
            A.insufficient_evidence = True
            B.insufficient_evidence = True
        return [A, B]

    # ------------------------------------------------ 情境/转化/方法学/证据不足
    def _pair_context(self, g: Gap, chain) -> List[Hypothesis]:
        w = self._topic_words()
        base = g.gap_id.replace("GAP_", "")
        topic = re.sub(r"^\[收窄后\]\s*", "", g.description)[:80]
        sup = g.supporting_evidence_ids
        A = Hypothesis(
            hypothesis_id=f"HYP_{base}_A", gap_id=g.gap_id, label="A", origin="system",
            statement=(f"已报道的 {w['condition']}→{w['outcome']} 机制在目标研究情境中同样成立,"
                       f"并可转化为分层或联合策略。({topic})"),
            rationale="跨情境外推需前瞻验证;若成立直接支持转化路径。",
            supporting_evidence_ids=sup,
            proposed_mechanism="模型机制在目标情境保守(推论边,需跨情境一致性验证)",
            mechanism_evidence_status="原情境机制:文献支持;跨情境保守性:待验证",
            predictions=[
                f"P1(判别 A vs B):目标情境中关键分子({w['entity']})与 {w['outcome']} 的关联方向与原情境一致且量级可预估(A);若仅在特定条件窗口成立,则支持 B",
                "P2:基于该机制的分层在回顾队列中显示结局差异",
            ],
            experimental_design={
                "设计": "多中心人体样本检测 + 回顾队列验证",
                "模型": f"{w['topic']} 人体样本与配对数据",
                "组别": "机制标记阳性/阴性分层 × 治疗方案",
            },
            controls=["检测平台间一致性", "批次/中心效应校正",
                      "标准治疗参照(具体方案待人工核查,未编造)"],
            readouts=[f"{w['outcome']} 差异(ORR/PFS 等)", "机制标记与功能读出相关性"],
            alternative_explanations=["情境差异由模型特异性补偿通路造成(即 B)"],
            falsification_criteria="若目标情境中关键分子-结局关联缺失或反向,则 A 被反驳。",
            feasibility={"note": "需要样本库与合作网络;未评估团队资源(待评估)"},
        )
        B = Hypothesis(
            hypothesis_id=f"HYP_{base}_B", gap_id=g.gap_id, label="B", origin="system",
            statement=(f"该机制仅在特定条件窗口(模型、剂量、时机、亚群)内成立;"
                       f"系统定义条件边界后原分歧将收敛。({topic})"),
            rationale="证据不均衡提示条件依赖而非普遍失败;边界定义具方法学价值。",
            supporting_evidence_ids=sup,
            proposed_mechanism="条件开关模型:效应受调节变量控制",
            mechanism_evidence_status="调节结构:由分歧模式间接支持的推论",
            predictions=[
                f"P1(判别 B vs A):按候选调节变量分层后,原冲突结果在层内方向一致(B);若全条件一致外推成立,则支持 A",
                "P2:效应大小随调节变量单调变化(跨模型矩阵)",
            ],
            experimental_design={
                "设计": "跨模型矩阵统一方案",
                "模型": f"{w['topic']} 相关:细胞-类器官-人源化-临床样本",
                "组别": "按调节变量分层的析因设计",
            },
            controls=["跨平台标准化质控", "同一模型内阳/阴性对照"],
            readouts=["各层效应方向与大小", "调节变量交互项"],
            alternative_explanations=["分歧纯由检测噪声/发表偏倚造成(无系统边界)"],
            falsification_criteria="若分层后层内仍随机分歧(无异质性结构),则 B 被反驳。",
            feasibility={"note": "需要多平台协作;未评估团队资源(待评估)"},
        )
        if not sup:
            A.insufficient_evidence = True
            B.insufficient_evidence = True
        return [A, B]

    # ------------------------------------------------ 新颖性(多源+诚实状态)
    def _novelty_search(self, h: Hypothesis, year_from: int, openalex_fn=None) -> Dict:
        from .retriever import europepmc_search, EUROPEPMC_SEARCH, _get_json
        rq = self.rq
        syns: List[str] = []
        # 结构化实体(同义词表内英文词);中文标签不进检索式
        for label in ([h.proposed_mechanism] if h.proposed_mechanism else []):
            for m in re.findall(r"「(.+?)」", h.proposed_mechanism or ""):
                if m in _ENTITY_SYNONYMS_TABLE:
                    syns.extend(_ENTITY_SYNONYMS_TABLE[m][:3])
        if rq:
            syns.extend(rq.topic_terms_en[:4])
        if not syns:
            syns = ["tumor microenvironment", "mechanism"]   # 通用生物医词,非旧主题
        syn_q = "(" + " OR ".join(f'"{s}"' for s in syns[:6]) + ")"

        rec = ReverseSearchRecord(purpose="hypothesis_novelty",
                                  sources=["europepmc"] + (["openalex"] if openalex_fn else []))
        rec.date = now_iso()
        levels = [
            {"level": 1, "strategy": "实体+干预+试验",
             "query": f'{syn_q} AND (blockade OR inhibitor OR knockout) AND ("clinical trial" OR "preclinical")'},
            {"level": 2, "strategy": "实体+机制检验",
             "query": f"{syn_q} AND (mechanism OR necessary OR mediates)"},
        ]
        relevant, examined_n, failed_src = [], 0, []
        for lv in levels:
            per_source = {}
            q_epmc = f'({lv["query"]}) AND (PUB_YEAR:[{year_from} TO 3000])'
            # v0.4 修复#4:统一结构化契约 (records, meta_info)
            recs_epmc, ep_meta = europepmc_search(q_epmc, RS_EXAMINED_PER_LEVEL // 2, self.log)
            # v0.4 修复#4 修正:partial(结果截断但记录可用)≠ failed。
            # 仅 failed/no_key/not_executed 视为源失败;partial 照常审查可用记录,
            # 并在 per_source 记录截断状态(不丢弃可用结果)。
            ep_status = ep_meta.get("status")
            # v0.5 修复#2:未知状态不得默认成功。failed/no_key/not_executed/
            # unknown 均视为源失败;partial 照常审查可用记录并如实记录状态。
            # v0.5.1 修复#3:允许状态白名单;缺失/None/空/意外值视为契约异常。
            # v0.5.1 修复#3 补充:failed/no_key/not_executed 合法但无可用结果
            # (契约合法≠结果可用),必须计源失败,不得进入已核查/高新颖性结论。
            if not _is_usable_status(ep_status):
                failed_src.append("europepmc")
                if _is_allowed_status(ep_status):
                    ek = ep_meta.get("error_kind") or ep_status
                    ps_status = ep_status
                else:
                    ek = ep_meta.get("error_kind") or _status_contract_error(ep_status)
                    ps_status = "failed"
                per_source["europepmc"] = {"request_status": ps_status,
                                           "error_kind": ek,
                                           "note": "源失败或状态契约异常:不计为零结果"}
            else:
                examined = recs_epmc[:RS_EXAMINED_PER_LEVEL // 2]
                examined_n += len(examined)
                rel = []
                for r in examined:
                    t = (r.get("title") or "").lower()
                    judg = "similar" if any(s.lower() in t for s in syns) else "unrelated"
                    if judg == "similar":
                        rel.append({"title": (r.get("title") or "")[:150],
                                    "year": ((r.get("journalInfo") or {}).get("yearOfPublication")),
                                    "doi": r.get("doi", ""), "source": "europepmc",
                                    "judgment": judg})
                per_source["europepmc"] = {
                    "request_status": ep_status if ep_status else "unknown",
                    "coverage": ("partial" if ep_meta.get("truncated") else "complete"),
                    "truncated": ep_meta.get("truncated", False),
                    "truncation_reason": ep_meta.get("truncation_reason", ""),
                    "query": q_epmc,
                    "examined": len(examined), "relevant": rel}
                relevant.extend(rel)
            if openalex_fn:
                oa_q = "title_and_abstract.search:" + " ".join(
                    f'"{s}"' if " " in s else s for s in syns[:3])
                recs_oa, meta = openalex_fn(oa_q, RS_EXAMINED_PER_LEVEL // 2)
                oa_status = meta.get("status")
                # v0.5 修复#2:未知状态不得默认成功。failed/no_key/not_executed/
                # unknown 均视为源失败;partial 照常审查可用记录并如实记录状态。
                # v0.5.1 修复#3:允许状态白名单;缺失/None/空/意外值视为契约异常。
                # v0.5.1 修复#3 补充:契约合法≠结果可用,failed/no_key/not_executed
                # 必须计源失败,不得进入已核查/高新颖性结论。
                if not _is_usable_status(oa_status):
                    failed_src.append("openalex")
                    if _is_allowed_status(oa_status):
                        ek = meta.get("error_kind") or oa_status
                        ps_status = oa_status
                    else:
                        ek = meta.get("error_kind") or _status_contract_error(oa_status)
                        ps_status = "failed"
                    per_source["openalex"] = {"request_status": ps_status,
                                              "error_kind": ek,
                                              "note": "源失败或状态契约异常:不计为零结果"}
                else:
                    examined = recs_oa[:RS_EXAMINED_PER_LEVEL // 2]
                    examined_n += len(examined)
                    rel = []
                    for r in examined:
                        t = (r.get("title") or "").lower()
                        judg = "similar" if any(s.lower() in t for s in syns) else "unrelated"
                        if judg == "similar":
                            rel.append({"title": (r.get("title") or "")[:150],
                                        "year": r.get("publication_year"),
                                        "doi": (r.get("doi") or "").replace("https://doi.org/", ""),
                                        "source": "openalex", "judgment": judg})
                    per_source["openalex"] = {
                        "request_status": oa_status if oa_status else "unknown",
                        "coverage": ("partial" if meta.get("truncated") else "complete"),
                        "truncated": meta.get("truncated", False),
                        "truncation_reason": meta.get("truncation_reason", ""),
                        "query": oa_q,
                        "examined": len(examined), "relevant": rel}
                    relevant.extend(rel)
            rec.query_levels.append({"level": lv["level"], "strategy": lv["strategy"],
                                     "per_source": per_source})
        rec.n_examined_total = examined_n
        rec.relevant_papers = relevant
        if failed_src and examined_n == 0:
            rec.final_status = "failed"
            rec.decision = "novelty_unverified"
            rec.reasoning = "全部启用源失败:不得宣称新颖;标'待评估'"
        elif failed_src:
            rec.final_status = "partial"
            rec.decision = "novelty_limited" if relevant else "novelty_plausible_in_scope_partial"
            rec.reasoning = (f"部分源失败({failed_src}):覆盖不完整,结论限于成功源;"
                             f"审查 {examined_n} 条,相似 {len(relevant)}")
        elif relevant:
            rec.final_status = "ok"
            rec.decision = "novelty_limited"
            rec.reasoning = f"发现 {len(relevant)} 条相似研究(题名级):新颖性限于条件增量,需全文复核"
        else:
            rec.final_status = "ok"
            rec.decision = "novelty_plausible_in_scope"
            rec.reasoning = (f"两级共审查 {examined_n} 条题名未见相似假设;"
                             f"限于当前检索范围与题名级筛查,不代表不存在")
        self.log.log("HypothesisArchitect", "novelty_search",
                     "ok" if rec.final_status == "ok" else rec.final_status,
                     f"{h.hypothesis_id} examined={examined_n} similar={len(relevant)} "
                     f"sources_failed={failed_src}")
        return rec.to_dict()

"""
Module 4:核验器 v0.3
修复:
- _cascade_clean_evidence_refs:字段名统一为 contradictory_evidence_ids(修复 AttributeError 崩溃)
- 空 claim 不再验证通过:claim 非空+有实际内容才进入原文匹配
- novelty_check 状态缺失/未知/未执行/失败均不能冒充已核验
- 实验设计/预测/对照/读出/替代解释/反证:检查实际内容(含嵌套空格/占位),非仅非空
- 无直接支持证据:insufficient_evidence 强制标注,不得冒充证据充分
- 一致性问题影响最终接纳:缺 Gap/缺引用的假设不通过(不进正文),非仅告警
- 证据分级独立复核(independent_kind_check),不只信对象自报 sentence_kind
- 清理后 Gap 失去依据 → 重新判定状态(撤回),不只删引用继续评分
- report_check:字段绑定(章节+字段+数字),非全文任意位置数字匹配
"""
from __future__ import annotations

import os
import re
from typing import Dict, List

from .schemas import Gap, Hypothesis, Paper, RunLog
from .extractor import _sentences, independent_kind_check, SPECULATIVE_PAT, REVIEW_TRANSFER_PAT

PLACEHOLDERS = ("待评估", "待补充", "TODO", "N/A", "placeholder", "占位", "…", "none", "tbd", "待核查")


def _meaningful(s, min_len: int = 20) -> bool:
    if not s or not str(s).strip():
        return False
    t = str(s).strip()
    if len(t) < min_len:
        return False
    if re.fullmatch(r"[\s\W_]+", t):
        return False
    if t.lower().rstrip(".") in [p.lower() for p in PLACEHOLDERS]:
        return False
    if len(set(t)) <= 3 and len(t) > 6:
        return False
    return True


def _dict_has_real_content(d) -> bool:
    """实验设计字典:至少 2 个键有实际内容(拒嵌套空格/占位)。
    v0.4 修复#9:递归核验叶子值——嵌套 dict/list 需其叶子有实际内容,
    而非 str(dict) 的字符串表示通过 _meaningful。"""
    if not isinstance(d, dict) or not d:
        return False

    def _leaf_meaningful(v) -> bool:
        if isinstance(v, dict):
            # 递归:嵌套 dict 需至少 1 个叶子有实际内容
            return any(_leaf_meaningful(x) for x in v.values())
        if isinstance(v, (list, tuple)):
            return any(_leaf_meaningful(x) for x in v)
        return _meaningful(v, 4)

    n_real = sum(1 for v in d.values() if _leaf_meaningful(v))
    return n_real >= 2


_UP_WORDS = (r"\b(increase|increases|increased|increasing|upregulat\w*|promote|promotes|promoted|promoting|"
             r"enhance|enhances|enhanced|enhancing|elevate|elevates|elevated|elevating|"
             r"improve|improves|improved|improving|boost|boosts|boosted|boosting|higher|greater)\b")
_DOWN_WORDS = (r"\b(decrease|decreases|decreased|decreasing|reduce|reduces|reduced|reducing|"
               r"inhibit|inhibits|inhibited|inhibiting|suppress|suppresses|suppressed|suppressing|"
               r"downregulat\w*|impair|impairs|impaired|impairing|attenuate|attenuates|attenuated|attenuating|"
               r"block|blocks|blocked|blocking|lower|less)\b")
_UP_PAT = re.compile(_UP_WORDS, re.I)
_DOWN_PAT = re.compile(_DOWN_WORDS, re.I)


def _direction_of(preds: List[str]) -> str:
    """单条假设预测的整体方向(up/down/mixed)。

    v0.5 修复#3:复用结构化预测模块 direction_of(处理中文/否定/多读出)。
    保留本函数名以兼容既有调用方。
    """
    from .prediction import direction_of
    return direction_of(preds)


def _is_discriminative_pair(pred_a: List[str], pred_b: List[str]) -> bool:
    """A/B 预测结构性可区分(v0.5 修复#3)。

    升级为结构化判别:仅当 A/B 在"同条件、同读出"下方向相反(或一方无效应)
    才判为可区分;不同读出/不同条件/不同机制的可同时成立,不判为竞争。
    处理中文、否定、多读出。未知状态不得默认成功。
    """
    from .prediction import is_discriminative_pair
    return is_discriminative_pair(pred_a, pred_b)


class Validator:
    def __init__(self, store, run_log: RunLog):
        self.store = store
        self.log = run_log
        self.errors: List[str] = []
        self.actions: List[str] = []

    # ---------------- 文献真实性
    def check_papers(self, papers: List[Paper]) -> Dict[str, int]:
        stats = {"verified": 0, "partial": 0, "unverified": 0, "not_checked": 0}
        for p in papers:
            st = p.metadata_status
            stats[st] = stats.get(st, 0) + 1
        self.log.log("Validator", "paper_authenticity", "ok", str(stats))
        return stats

    # ---------------- 证据定位(空 claim 拦截 + 独立分级复核)
    def check_evidence_location(self) -> List[str]:
        bad = []
        abs_cache: Dict[str, List[str]] = {}
        dropped = []
        for e in self.store.evidences.values():
            p = self.store.papers.get(e.paper_id)
            if p is None:
                bad.append(f"{e.evidence_id}: 引用不存在的 paper_id={e.paper_id}(剔除)")
                dropped.append(e.evidence_id)
                continue
            # v0.3:空 claim 先拦截(空串属于任意子串的漏洞)
            if not _meaningful(e.claim, 10):
                bad.append(f"{e.evidence_id}: claim 为空/无实际内容(不得验证通过)")
                e.verification_note = "claim 无实际内容,拒绝核验"
                dropped.append(e.evidence_id)
                continue
            if e.paper_id not in abs_cache:
                abs_cache[e.paper_id] = _sentences(p.abstract or "")
            sents = abs_cache[e.paper_id]
            m = re.match(r"摘要句#(\d+)", e.location or "")
            if not m:
                bad.append(f"{e.evidence_id}: 位置格式无效 '{e.location}'")
                e.verification_note = "位置格式无效"
                dropped.append(e.evidence_id)
                continue
            idx = int(m.group(1))
            if idx < 1 or idx > len(sents):
                bad.append(f"{e.evidence_id}: 虚构位置 '{e.location}'(摘要仅 {len(sents)} 句)")
                e.verification_note = f"位置越界:#{idx} > {len(sents)}"
                dropped.append(e.evidence_id)
                continue
            src = sents[idx - 1]
            norm_claim = re.sub(r"\s+", " ", e.claim.strip())[:200]
            if norm_claim not in src:
                bad.append(f"{e.evidence_id}: 原文不匹配")
                e.verification_note = "claim 与定位句不一致"
                dropped.append(e.evidence_id)
                continue
            # v0.3:独立复核句类(不信自报)
            ok_kind, note = independent_kind_check(src, e.sentence_kind)
            if not ok_kind:
                if e.evidence_level == 1:
                    bad.append(f"{e.evidence_id}: {note}(降级)")
                    e.evidence_level = 3
                    e.evidence_type = "author_speculation"
                    e.verification_note = note + ";已降级 L3"
                    self.actions.append(f"{e.evidence_id} 独立复核降级 L3")
                else:
                    e.verification_note = note + "(L3 本就非事实,保留)"
            else:
                e.verified = True
                e.verification_note = f"位置#{idx}/{len(sents)}存在;原文匹配;{note}"
        if dropped:
            for eid in dropped:
                self.store.evidences.pop(eid, None)
            self.actions.append(f"证据核验:剔除 {len(dropped)} 条(位置虚构/原文不匹配/空claim/引用缺失)")
            self._cascade_clean_evidence_refs(set(dropped))
        self.log.log("Validator", "evidence_location",
                     "ok" if not bad else f"dropped={len(dropped)}",
                     f"checked={len(self.store.evidences)+len(dropped)} issues={len(bad)}")
        self.errors.extend(bad)
        return bad

    # ---------------- 级联清理(修复字段名崩溃;含 Gap 重判定)
    def _cascade_clean_evidence_refs(self, dead: set) -> None:
        affected_gaps = []
        for g in self.store.gaps.values():
            before_sup = len(g.supporting_evidence_ids)
            g.supporting_evidence_ids = [i for i in g.supporting_evidence_ids if i not in dead]
            g.contradicting_evidence_ids = [i for i in g.contradicting_evidence_ids if i not in dead]
            if len(g.supporting_evidence_ids) != before_sup:
                affected_gaps.append(g)
                g.limitation = (g.limitation + ";部分支持证据未通过核验,已移除").strip(";")
        for h in self.store.hypotheses.values():   # v0.3:正确字段 contradictory_evidence_ids
            # 同时清理:被删除的(dead)与本来就指向不存在证据的引用
            valid = set(self.store.evidences.keys())
            h.supporting_evidence_ids = [i for i in h.supporting_evidence_ids
                                         if i not in dead and i in valid]
            h.contradictory_evidence_ids = [i for i in h.contradictory_evidence_ids
                                            if i not in dead and i in valid]
        # v0.3:清理后失去依据的 Gap 重新判定(撤回),不只删引用继续评分
        for g in affected_gaps:
            if g.gap_type in ("contradiction", "mechanism") and not g.supporting_evidence_ids:
                g.status = "dropped"
                g.status_reason = ("核验清理后无任何通过核验的支持证据:"
                                   "依据不足,撤回(不再进入评分与正文)")
                self.actions.append(f"{g.gap_id} 清理后失去依据,已撤回")
                self.log.log("Validator", "gap_recheck", "dropped",
                             f"{g.gap_id} {g.status_reason[:100]}")
            elif not g.supporting_evidence_ids and g.gap_type in ("context", "translational",
                                                                  "methodological", "insufficient"):
                g.limitation = (g.limitation + ";支持证据已被清理,依据降为分布统计型").strip(";")

    # ---------------- 假设核验(实质内容+新颖性状态+配对)
    def check_hypotheses(self, hyps: List[Hypothesis]) -> List[Hypothesis]:
        passed = []
        for h in hyps:
            if h.origin == "author":
                # 作者假设:验证来源与原文,不要求完整设计
                ok_src = bool(h.source and h.source.get("paper_id")
                              and h.source.get("paper_id") in self.store.papers)
                loc = h.source.get("location", "") if h.source else ""
                m = re.match(r"摘要句#(\d+)", loc or "")
                p = self.store.papers.get(h.source.get("paper_id")) if h.source else None
                ok_loc = bool(m and p and 1 <= int(m.group(1)) <= len(_sentences(p.abstract or "")))
                if ok_src and ok_loc and _meaningful(h.statement, 10):
                    h.validation = {"passed": True, "problems": [],
                                    "action": "作者假设:来源与定位核验通过(登记,非系统配对)"}
                    passed.append(h)
                else:
                    h.validation = {"passed": False,
                                    "problems": [f"来源核验失败(src={ok_src},loc={ok_loc})"],
                                    "action": "作者假设来源/定位无效:剔除"}
                    self.actions.append(f"{h.hypothesis_id} 作者假设来源核验失败,剔除")
                continue

            problems = []
            if not _meaningful(h.statement, 20):
                problems.append("陈述为空/过短/占位")
            if not _meaningful(h.proposed_mechanism, 8):
                problems.append("机制依据缺失或占位")
            if not h.predictions or any(not _meaningful(p, 10) for p in h.predictions):
                problems.append("预测为空/占位")
            if not h.controls or any(not _meaningful(c, 3) for c in h.controls):
                problems.append("对照为空/占位")
            if not h.readouts or any(not _meaningful(r, 3) for r in h.readouts):
                problems.append("读出为空/占位")
            if not _meaningful(h.falsification_criteria, 15):
                problems.append("反证标准缺失/占位")
            if not h.alternative_explanations or any(not _meaningful(a, 6) for a in h.alternative_explanations):
                problems.append("替代解释缺失/占位")
            if not _dict_has_real_content(h.experimental_design):
                problems.append("实验设计无实际内容(嵌套空格/占位)")
            # 新颖性状态:缺失/未知/未执行/失败均不合格
            nc = h.novelty_check or {}
            nstat = nc.get("final_status", "not_executed")
            if nstat not in ("ok", "partial"):
                problems.append(f"新颖性核查状态无效({nstat}):不能冒充已核验")
            elif nstat == "partial":
                h.limitation_note = "新颖性核查部分源失败:结论限于成功源"
            # 引用存在性(缺失即不通过,而非仅告警)
            missing = [eid for eid in h.supporting_evidence_ids if eid not in self.store.evidences]
            if missing:
                problems.append(f"支持证据引用不存在:{missing}")
                h.supporting_evidence_ids = [i for i in h.supporting_evidence_ids
                                             if i in self.store.evidences]
            for eid in list(h.contradictory_evidence_ids):
                if eid not in self.store.evidences:
                    h.contradictory_evidence_ids.remove(eid)
                    problems.append(f"反对证据引用不存在:{eid}(已剔除)")
            # Gap 存在性(缺 Gap 的 system 假设不通过)
            # v0.4 修复#9:system 假设 gap_id 必填;空字符串不能绕过核验
            if not str(h.gap_id or "").strip():
                problems.append("system 假设缺少 gap_id(必填):空值不能绕过核验")
            elif h.gap_id not in self.store.gaps:
                problems.append(f"引用不存在的 Gap {h.gap_id}")
            g = self.store.gaps.get(h.gap_id)
            if g is not None and g.status == "dropped":
                problems.append(f"其 Gap({h.gap_id})已被撤回/剔除")
            # 证据不足标注(v0.3:允许输出但必须标注)
            if not h.supporting_evidence_ids:
                h.insufficient_evidence = True

            # ---- v0.5.1 修复#6c:待审状态不得被无条件覆盖为整体通过 ----
            # 区分并分别保存:预测可区分性 / 字段及实验设计完整性 / 可检验性 /
            # 尚未解决的机制或设计待核查项 / 整体接纳状态 / 专家评审状态。
            # 单项检查通过(字段非空、方向可区分)不能清除尚未解决的待审问题;
            # candidate_pending_review / needs_manual / design_completeness=degraded
            # 不得被无条件覆盖为整体通过。
            pending = list(getattr(h, "pending_review", []) or [])
            design = getattr(h, "design_completeness", "complete") or "complete"
            proposal = getattr(h, "proposal_status", "") or ""
            # 待核查项:显式 pending_review 或 design 降级或拟议设计未定稿
            has_pending = bool(pending) or design == "degraded"
            # 专家评审状态:未进行(默认)不得冒充已评审
            expert_reviewed = bool(getattr(h, "expert_review", None))
            if problems:
                h.validation = {"passed": False, "problems": problems,
                                "action": "退回;不进入报告正文"}
                self.errors.append(f"{h.hypothesis_id}: {'; '.join(problems)}")
                self.log.log("Validator", "hypothesis_testability", "fail",
                             f"{h.hypothesis_id} {problems}")
            elif has_pending:
                # 字段/设计完整性检查通过,但仍有待核查项:保留待审状态,不置整体通过
                h.validation = {"passed": False,
                                "problems": ["待人工核查:" + ";".join(pending)] if pending
                                             else ["设计完整性降级(degraded):待人工核查"],
                                "action": "字段与可检验性检查通过,但存在待核查项:"
                                          "保留待审状态,不进入完整通过方案",
                                "design_completeness": design,
                                "proposal_status": proposal,
                                "pending_review": pending,
                                "expert_reviewed": expert_reviewed}
                self.actions.append(f"{h.hypothesis_id} 待审状态保留(待人工核查,未置整体通过)")
                self.log.log("Validator", "hypothesis_testability", "pending_review",
                             f"{h.hypothesis_id} pending={pending}")
            else:
                h.validation = {"passed": True, "problems": [], "action": "通过;Level 3 待验证假设",
                                "design_completeness": design,
                                "proposal_status": proposal,
                                "expert_reviewed": expert_reviewed}
                passed.append(h)
        # 竞争配对判定
        self._check_pairing(hyps)
        n_fail = sum(1 for h in hyps if not h.validation.get("passed") and h.origin == "system")
        self.log.log("Validator", "hypothesis_testability", "ok",
                     f"passed={len(passed)}/{len(hyps)} failed_system={n_fail}")
        return passed

    # ---------------- 配对完整性
    def _check_pairing(self, hyps: List[Hypothesis]) -> None:
        by_gap: Dict[str, List[Hypothesis]] = {}
        for h in hyps:
            if h.origin == "system" and h.gap_id:
                by_gap.setdefault(h.gap_id, []).append(h)
        for gap_id, hs in by_gap.items():
            g = self.store.gaps.get(gap_id)
            if not g or g.status == "dropped":
                continue
            passed = [h for h in hs if h.validation.get("passed")]
            labels = {h.label for h in passed}
            if {"A", "B"} <= labels:
                a = next(h for h in passed if h.label == "A")
                b = next(h for h in passed if h.label == "B")
                if _is_discriminative_pair(a.predictions, b.predictions):
                    g.pairing_status = "paired"
                    a.pairing_status = b.pairing_status = "paired"
                else:
                    g.pairing_status = "single_incomplete"
                    a.pairing_status = b.pairing_status = "single_incomplete"
                    g.limitation = (g.limitation + ";A/B 预测未通过结构性判别检查:配对标未完成").strip(";")
            else:
                g.pairing_status = "single_incomplete"
                for h in passed:
                    h.pairing_status = "single_incomplete"
                self.actions.append(
                    f"{gap_id} 竞争配对未完成(通过标签 {sorted(labels)}):"
                    f"正文将明确标注'单候选解释,竞争性方案未完成'")
                self.log.log("Validator", "pairing", "incomplete",
                             f"{gap_id} labels_passed={sorted(labels)}")

    # ---------------- 结论强度
    def check_conclusion_strength(self, gaps: List[Gap]) -> List[str]:
        notes = []
        strong = re.compile(r"\b(proves?|conclusively|definitive(ly)?)\b", re.I)
        for g in gaps:
            if strong.search(g.description):
                g.description = strong.sub("提示…可能", g.description)
                notes.append(f"{g.gap_id} 措辞降级")
            if not g.limitation:
                g.limitation = "证据基于摘要层;检索覆盖有限"
                notes.append(f"{g.gap_id} 补默认限制")
        for h in self.store.hypotheses.values():
            if h.evidence_level != 3:
                h.evidence_level = 3
        self.log.log("Validator", "conclusion_strength", "ok", f"adjustments={len(notes)}")
        return notes

    # ---------------- 一致性(影响接纳)
    def check_consistency(self) -> List[str]:
        issues = []
        valid_ev = set(self.store.evidences.keys())
        for g in self.store.gaps.values():
            missing = [eid for eid in g.supporting_evidence_ids + g.contradicting_evidence_ids
                       if eid not in valid_ev]
            if missing:
                g.supporting_evidence_ids = [i for i in g.supporting_evidence_ids if i in valid_ev]
                g.contradicting_evidence_ids = [i for i in g.contradicting_evidence_ids if i in valid_ev]
                issues.append(f"{g.gap_id} 引用缺失证据 {missing}(已剔除)")
        for h in self.store.hypotheses.values():
            if h.origin == "system" and h.gap_id and h.gap_id not in self.store.gaps:
                issues.append(f"{h.hypothesis_id} 引用缺失 Gap {h.gap_id}")
                h.validation = {"passed": False, "problems": [f"缺 Gap {h.gap_id}"],
                                "action": "一致性失败:不进入正文"}
        self.log.log("Validator", "report_consistency", "ok" if not issues else "warn",
                     f"issues={len(issues)}(均影响接纳)")
        self.errors.extend(issues)
        return issues

    # ---------------- 报告核对(v0.3:字段绑定,非全文数字搜索)
    def report_check(self, report_text: str, out_dir: str | None = None) -> Dict:
        """字段绑定检查:在指定章节片段中定位'字段名+数字'组合。
        '附录恰好包含正确数字'不能通过——检查限定在对应章节行内。

        v0.5 修复#4:
          - Gap 标题核对限定正文章节(第5节 Research Gap Analysis),
            不被 References/附录中的 "### Gap N" 绕过;
          - 按真实 Gap ID 精确核对集合/数量/重复;
          - 来源统计逐键核对整数(任一键数字错误即不通过);
          - 图件四项分开:引用/文件存在/统计一致/视觉检查,未执行不写通过。
        """
        S = self.store.stats_summary()
        checks = []

        def find_in_section(field_label: str, expected, section_header: str) -> None:
            """在 section_header 之后的段落内找 field_label 行,核对其中数字。"""
            ok = False
            lines = report_text.splitlines()
            in_sec = False
            for ln in lines:
                if ln.strip().startswith("#"):
                    in_sec = section_header in ln
                    continue
                if not in_sec:
                    continue
                if field_label in ln or any(k in ln for k in FIELD_ALIASES.get(field_label, [])):
                    # v0.3:数字必须出现在字段标签之后(行首其他数字不干扰)
                    pos = ln.find(field_label)
                    if pos < 0:
                        pos = min((ln.find(k) for k in FIELD_ALIASES.get(field_label, [])
                                   if ln.find(k) >= 0), default=-1)
                    if pos >= 0:
                        m = re.search(r"\*{0,2}(\d+)\*{0,2}", ln[pos:])
                        if m and int(m.group(1)) == int(expected):
                            ok = True
                    break
            checks.append({"field": field_label, "section": section_header,
                           "expected": expected, "bound_match": ok})

        FIELD_ALIASES = {
            "初筛纳入": ["初筛纳入"],
            "深度抽取": ["深度抽取"],
            "结构化证据": ["结构化证据"],
        }
        find_in_section("初筛纳入", S["papers_included"], "Executive Summary")
        find_in_section("深度抽取", S["papers_deep_extracted"], "Executive Summary")
        find_in_section("结构化证据", S["evidences_total"], "Executive Summary")
        # v0.5 修复#4:Gap 核对限定正文章节(第5节 Research Gap Analysis),
        # 不被 References/附录中的 "### Gap N" 绕过。
        gap_n = S["gaps_candidate"] + S["gaps_narrowed"]
        # 提取正文第5节(从 "## 5." 到下一个 "## " 二级标题)
        body_gap_section = ""
        lines = report_text.splitlines()
        in_gap = False
        for ln in lines:
            if ln.startswith("## "):
                if re.search(r"##\s*5\..*Gap", ln) or "Research Gap Analysis" in ln:
                    in_gap = True
                    body_gap_section += ln + "\n"
                    continue
                if in_gap:
                    break
            if in_gap:
                body_gap_section += ln + "\n"
        body_gap_headers = re.findall(r"^###\s*Gap\s+(\d+)", body_gap_section, re.M)
        # 真实 Gap ID 集合核对:store 中 candidate+narrowed 的 gap_id
        expected_gap_ids = sorted(g.gap_id for g in self.store.gaps.values()
                                  if g.status in ("candidate", "narrowed"))
        body_gap_ids = re.findall(r"^###\s*Gap\s+\d+\s+\[([A-Za-z0-9_\-]+)\]",
                                  body_gap_section, re.M)
        checks.append({"field": "候选Gap数(正文第5节计数)", "section": "Gap Analysis(正文第5节)",
                       "expected": gap_n, "bound_match": len(body_gap_headers) == gap_n})
        checks.append({"field": "真实GapID集合(正文第5节,含重复核对)",
                       "section": "Gap Analysis(正文第5节)",
                       "expected": expected_gap_ids,
                       "bound_match": (sorted(body_gap_ids) == expected_gap_ids
                                       and len(body_gap_ids) == len(set(body_gap_ids)))})
        # References/附录中的 Gap 标题不计入:由 body_gap_section 提取天然保证,
        # 无需恒真占位检查(v0.5:删除假检查)。

        # v0.4 修复#9:假设数/配对/来源统计/分母/图表 绑定核对
        # 假设数(系统生成)在 Executive Summary 的"假设:系统生成 N 组"
        find_in_section("系统生成", S["hypotheses_system"], "Executive Summary")
        # 配对:paired 数在 Executive Summary 的"竞争假设配对:paired N"
        find_in_section("paired", S.get("gaps_paired", 0), "Executive Summary")
        # v0.5 修复#4:来源统计逐键核对整数(任一键数字错误即不通过)
        # 互斥:EPMC-only/OA-only/both/unknown;可重叠:overlap_*;分母 denominator_included
        src_dist = S.get("source_distribution", {})
        src_line = ""
        for ln in report_text.splitlines():
            if "来源分布" in ln and "互斥" in ln:
                src_line = ln
                break
        key_errors = []
        excl_keys = [("EPMC-only", src_dist.get("epmc_only", 0)),
                     ("OA-only", src_dist.get("oa_only", 0)),
                     ("both", src_dist.get("both", 0)),
                     ("unknown", src_dist.get("unknown", 0))]
        for label, val in excl_keys:
            pat = rf"{re.escape(label)}\s*\**(\d+)\**"
            m = re.search(pat, src_line)
            if not m or int(m.group(1)) != val:
                key_errors.append(f"{label}期望{val}实际{m.group(1) if m else '缺失'}")
        ov_keys = [("EPMC 命中", src_dist.get("overlap_europepmc", 0)),
                   ("OpenAlex 命中", src_dist.get("overlap_openalex", 0)),
                   ("多源命中", src_dist.get("overlap_multi_source", 0))]
        for label, val in ov_keys:
            pat = rf"{re.escape(label)}\s+(\d+)\s*篇"
            m = re.search(pat, src_line)
            if not m or int(m.group(1)) != val:
                key_errors.append(f"{label}期望{val}实际{m.group(1) if m else '缺失'}")
        # 分母:分母=纳入 N 篇
        m_den = re.search(r"分母\s*=\s*纳入\s*(\d+)\s*篇", src_line)
        den_expected = src_dist.get("denominator_included", S["papers_included"])
        if not m_den or int(m_den.group(1)) != den_expected:
            key_errors.append(f"分母期望{den_expected}实际{m_den.group(1) if m_den else '缺失'}")
        # 互斥分类总和必须等于分母(内部一致性)
        excl_sum = sum(v for k, v in src_dist.items()
                       if k in ("epmc_only", "oa_only", "both", "unknown"))
        if excl_sum != den_expected:
            key_errors.append(f"互斥总和{excl_sum}≠分母{den_expected}(store内部不一致)")
        checks.append({"field": "来源分布(互斥+可重叠,逐键整数核对)",
                       "section": "Executive Summary",
                       "expected": src_dist,
                       "bound_match": bool(src_line) and not key_errors,
                       "errors": key_errors})
        # 分母:纳入文献中有摘要可用 papers_with_abstract/papers_included
        den_ok = False
        for ln in report_text.splitlines():
            if "有摘要可用" in ln:
                den_ok = (f"{S.get('papers_with_abstract', '?')}/{S['papers_included']}"
                          in ln)
                break
        checks.append({"field": "摘要可用分母", "section": "Executive Summary",
                       "expected": f"{S.get('papers_with_abstract','?')}/{S['papers_included']}",
                       "bound_match": den_ok})
        # v0.5 修复#4:图件四项分开——引用/文件存在/统计一致/视觉检查
        fig_names = ("year_dist.png", "design_dist.png",
                     "mech_terms.png", "gap_scores.png")
        # (a) 引用:报告中存在 figures/<name> 引用
        fig_ref_ok = all(f"figures/{name}" in report_text for name in fig_names)
        checks.append({"field": "图件引用(附录引用存在)", "section": "附录",
                       "expected": "4 图引用", "bound_match": fig_ref_ok})
        # (b) 文件存在:仅当提供 out_dir 时才执行;未提供 → not_checked(不写通过)
        if out_dir is not None:
            fig_dir = os.path.join(out_dir, "figures")
            missing = [n for n in fig_names
                       if not os.path.isfile(os.path.join(fig_dir, n))]
            checks.append({"field": "图件文件存在(磁盘)", "section": f"{out_dir}/figures",
                           "expected": "4 个 PNG",
                           "bound_match": not missing,
                           "missing": missing})
        else:
            checks.append({"field": "图件文件存在(磁盘)", "section": "(未执行)",
                           "expected": "out_dir 未提供 → 不判定",
                           "bound_match": False, "status": "not_checked",
                           "note": "not_checked:未提供 out_dir,不判定通过/失败"})
        # (c) 统计一致:报告引用的图件集合与 render_figures 产物集合精确相等
        #     (无缺失、无多余;render_figures 由同一 store 分布渲染)
        referenced = set(re.findall(r"!\[[^\]]*\]\(figures/([^)]+\.png)\)", report_text))
        expected_figs = set(fig_names)
        checks.append({"field": "图件引用集合与渲染产物一致(无缺失/多余)",
                       "section": "附录",
                       "expected": sorted(expected_figs),
                       "bound_match": referenced == expected_figs,
                       "actual": sorted(referenced)})
        # (d) 视觉检查:本自动核验不执行,显式标 not_checked,不得写通过
        checks.append({"field": "图件视觉检查", "section": "(未执行)",
                       "expected": "需人工/视觉复核,自动核验不判定",
                       "bound_match": False, "status": "not_checked",
                       "note": "not_checked:视觉检查未执行,不写通过"})
        # v0.5 修复#4:三态归一 —— pass/fail/not_checked
        # not_checked(未执行):不计入 mismatched,也不得视为通过
        for c in checks:
            if "status" not in c:
                c["status"] = "pass" if c["bound_match"] else "fail"
            elif c["status"] == "not_checked":
                c["bound_match"] = False  # 未执行绝不写通过
        n_bad = sum(1 for c in checks if c["status"] == "fail")
        n_nc = sum(1 for c in checks if c["status"] == "not_checked")
        self.log.log("Validator", "report_check", "ok" if n_bad == 0 else "failed",
                     f"bound_checks={len(checks)} mismatched={n_bad} not_checked={n_nc}")
        return {"checks": checks, "mismatched": n_bad, "not_checked": n_nc}

"""
Module 5:报告生成 v0.2
修复:
- 数据源状态由 run_log 实际记录生成(未调用的适配器写"未启用/未执行",不硬编码额度/IP原因)
- 统计口径:报告与图表共用 store 的同一统计函数(analysis_set),数值一致;年份用整数离散柱图
- 图表标题注明分析集合与分母(如"初筛纳入 N=40")
- 评分展示:部分评分(已评估维度+权重覆盖率+不确定性),不冒充完整总分
- 反向检索/新颖性:如实展示请求状态、审查数、判断理由;失败/未执行单独说明
"""
from __future__ import annotations

import os
from collections import Counter
from typing import Dict, List

from .config import APP_NAME, APP_VERSION, DISCLAIMER, GAP_TYPE_ZH
from .schemas import RunLog

DESIGN_ZH = {"clinical": "临床研究", "human": "人体样本研究", "in_vivo": "动物/体内研究",
             "in_vitro": "体外/细胞研究", "bioinformatic": "生物信息分析", "review": "综述",
             "unknown": "设计未识别"}


class ReportGenerator:
    def __init__(self, store, run_log: RunLog, plan=None):
        self.store = store
        self.log = run_log
        self.plan = plan

    # ------------------------------------------------------------ 数据源状态(日志驱动)
    def data_source_status(self) -> List[str]:
        lines = []
        called = {}
        for c in self.log.api_calls:
            called.setdefault(c["tool"], {"n": 0, "ok": 0, "failed": 0})
            called[c["tool"]]["n"] += 1
            called[c["tool"]]["ok" if c["ok"] else "failed"] += 1
        not_impl = [e for e in self.log.entries
                    if e.get("status") == "not_implemented" and e.get("tool")]
        for tool, name in (("europepmc", "Europe PMC"), ("crossref", "Crossref"),
                           ("openalex", "OpenAlex")):
            if tool in called:
                c = called[tool]
                line = (f"- **{name}**:已调用 {c['n']} 次(成功 {c['ok']},失败 {c['failed']});"
                        f"审计见 run_log.json 的 api_calls(url+脱敏参数+状态+快照)")
                # OpenAlex 附加状态(限额头/截断)
                oa_logs = [e for e in self.log.entries
                           if e.get("tool") == "openalex" and "truncated" in e.get("detail", "")]
                if tool == "openalex" and oa_logs:
                    line += ";存在分页/预算截断记录(详见日志)"
                lines.append(line)
            else:
                # 未调用的原因分类(密钥缺失 vs 未配置)
                no_key = [e for e in self.log.entries
                          if e.get("tool") == tool and e.get("status") == "no_key"]
                if tool == "openalex" and no_key:
                    lines.append("- **OpenAlex**:未执行——缺少 OPENALEX_API_KEY(如实记录,不虚构额度/网络原因)")
                elif tool == "openalex":
                    lines.append("- **OpenAlex**:本轮未调用(未启用)")
                else:
                    lines.append(f"- **{name}**:本轮未调用")
        if not_impl:
            ni = sorted({e["tool"] for e in not_impl})
            lines.append(f"- **{'、'.join(ni)}**:未实现/未调用——本轮未向其发送任何请求")
        # 部分覆盖披露
        failed_tools = {t for t, c in called.items() if c["failed"] > 0 and c["ok"] == 0}
        ok_tools = {t for t, c in called.items() if c["ok"] > 0}
        if failed_tools:
            lines.append(f"- **部分覆盖**:源 {sorted(failed_tools)} 全部请求失败;"
                         f"成功源 {sorted(ok_tools)};多源检索未充分完成,相关新颖性结论已按部分覆盖降级")
        return lines

    # ------------------------------------------------------------ 主报告
    def render_markdown(self, topic: str, ranked_gaps, hyps,
                        validation_summary: Dict) -> str:
        S = self.store.stats_summary()
        papers = self.store.analysis_set("included")
        deep = self.store.analysis_set("deep")
        L: List[str] = []
        w = L.append

        w("# 科研盲区挖掘与新假设论证报告(v0.2 修订版)")
        w("")
        w(f"- **主题**:{topic}")
        w(f"- **生成工具**:{APP_NAME} v{APP_VERSION}")
        w(f"- **运行 ID**:{self.log.run_id}")
        w(f"- **统计口径**:图表与正文统计均基于 `analysis_set` 统计函数(同一口径)")
        w("")
        w(f"> **声明**:{DISCLAIMER}")
        w("")
        w(f"> **诚实性说明(v{APP_VERSION})**:所有分析基于**摘要层**"
          "(未实际获取与核验全文);反向检索为**题名级筛查**;"
          "评分为**部分评分**(F 可行性维度待评估);"
          "证据分级与解决判定由规则引擎给出,需人工复核(needs_manual 项见附录)。")
        w("")

        # ---------------- Executive Summary
        w("## 1. Executive Summary(执行摘要)")
        w("")
        w(f"- 检索合并去重后 **{S['papers_total']}** 条记录;初筛纳入 **{S['papers_included']}** 篇"
          f"(含综述 {S['papers_review_type']} 篇);深度抽取 **{S['papers_deep_extracted']}** 篇。")
        w(f"- 结构化证据 **{S['evidences_total']}** 条:L1 直接证据 {S['evidences_l1']} 条,"
          f"L3 作者推测 {S['evidences_l3_author']} 条(分级依据句子类别,同句不双计)。")
        w(f"- 候选 Gap:{S['gaps_candidate']+S['gaps_narrowed']} 项"
          f"(candidate {S['gaps_candidate']},narrowed {S['gaps_narrowed']});"
          f"按停止条件剔除 {S['gaps_dropped']} 项(保留记录备查)。")
        w(f"- 假设:系统生成 {S['hypotheses_system']} 组(竞争性配对),"
          f"作者原文假设 {S['hypotheses_author']} 条(单独登记,origin=author);"
          f"全部 Level 3 待验证。")
        basis = S["analysis_basis_counts"]
        w(f"- **分析依据分布**:{basis}(全部为摘要层;未获取/解析/核验任何全文)。")
        src_dist = S.get("source_distribution", {})
        # v0.5 修复#4:互斥来源分类(EPMC-only/OA-only/both/unknown,总和=纳入数)
        # 与可重叠覆盖数(overlap_*)分开渲染,明确分母。
        epmc_only = src_dist.get("epmc_only", 0)
        oa_only = src_dist.get("oa_only", 0)
        both = src_dist.get("both", 0)
        unknown_src = src_dist.get("unknown", 0)
        ov_epmc = src_dist.get("overlap_europepmc", 0)
        ov_oa = src_dist.get("overlap_openalex", 0)
        ov_multi = src_dist.get("overlap_multi_source", 0)
        denom = src_dist.get("denominator_included", S['papers_included'])
        w(f"- **来源分布(互斥,分母=纳入 {denom} 篇)**:"
          f"EPMC-only {epmc_only} / OA-only {oa_only} / both {both} / unknown {unknown_src};"
          f"**可重叠覆盖**:EPMC 命中 {ov_epmc} 篇,OpenAlex 命中 {ov_oa} 篇,"
          f"多源命中 {ov_multi} 篇(一篇可计入多个来源,故覆盖数之和可超过分母);"
          f"纳入文献中 {S.get('papers_with_abstract', '?')}/{S['papers_included']} 篇有摘要可用;"
          f"撤稿标记 {S.get('papers_retracted', 0)} 篇(已排除)。")
        w(f"- **竞争假设配对**:paired {S.get('gaps_paired', 0)} 个 Gap,"
          f"single_incomplete {S.get('gaps_single_incomplete', 0)} 个"
          f"(系统假设:生成 {S['hypotheses_system']},通过 {S.get('hypotheses_system_passed', '?')},"
          f"失败 {S.get('hypotheses_system_failed', '?')});"
          f"证据不足标注假设 {S.get('hypotheses_insufficient_evidence', 0)} 条。")
        val = validation_summary
        w(f"- 核验:元数据 {val.get('paper_stats')};证据定位核验剔除 {val.get('evid_dropped', 0)} 条;"
          f"假设可检验性通过 {val.get('hyp_passed', 0)}/{val.get('hyp_total', 0)};"
          f"一致性 {val.get('consistency_issues', 0)} 项(已处理)。")
        w("")

        # ---------------- 数据源状态(日志驱动)
        w("## 2. Data Sources & Retrieval(数据源与检索)")
        w("")
        w("### 2.1 数据源实际状态(由运行日志生成)")
        w("")
        for line in self.data_source_status():
            w(line)
        w("")
        w("### 2.2 检索式(Node 0 规划)")
        w("")
        if self.plan:
            for q in self.plan.queries:
                w(f"- **{q['strategy']}**(`{q['name']}`):`{q['query'][:180]}`")
        w("")
        errs = [e for e in self.log.entries if e["status"] not in ("ok", "info", "not_implemented")]
        w(f"### 2.3 异常与降级(共 {len(errs)} 条,详见 run_log.json)")
        w("")
        for e in errs[:8]:
            w(f"- [{e['stage']}/{e['tool']}] {e['status']}:{e['detail'][:100]}")
        if len(errs) > 8:
            w(f"- …另有 {len(errs)-8} 条")
        w("")

        # ---------------- Literature Landscape
        w("## 3. Literature Landscape(文献图谱)")
        w("")
        years = self.store.year_distribution("included")
        w(f"### 3.1 年份分布(初筛纳入 N={sum(years.values())})")
        w("")
        w("| 年份 | 篇数 |")
        w("| --- | --- |")
        for y in sorted(years):
            w(f"| {y} | {years[y]} |")
        w("")
        designs = self.store.design_distribution("included")
        w(f"### 3.2 研究设计分布(初筛纳入;综述单列,不与原始研究混计)")
        w("")
        w("| 设计类型 | 篇数 |")
        w("| --- | --- |")
        for d, n in designs.most_common():
            w(f"| {DESIGN_ZH.get(d, d)} | {n} |")
        w("")
        n_deep = S["papers_deep_extracted"]
        terms = self.store.count_terms("deep").most_common(12)
        w(f"### 3.3 高频机制/靶点术语(基于**已抽取集合** N={n_deep},非全部纳入文献;"
          f"未抽取论文不计为未出现)")
        w("")
        w("| 术语 | 独立论文数(已抽取集合) |")
        w("| --- | --- |")
        for t, n in terms:
            w(f"| {t} | {n} |")
        w("")

        # ---------------- Evidence Matrix
        w("## 4. Evidence Matrix(证据矩阵)")
        w("")
        w(f"> 分析集合:深度抽取 {S['papers_deep_extracted']} 篇;完整矩阵见 evidence_matrix.csv;"
          f"本节展示高被引代表文献。")
        w("")
        matrix = self.store.evidence_matrix()
        deep_rows = [r for r in matrix if r["in_deep_analysis"]]
        w("| 论文 | 年份 | 模型(摘要可识别) | 主要结果(L1 节选) | 机制证据 | 级别 | 分析依据 |")
        w("| --- | --- | --- | --- | --- | --- | --- |")
        for r in deep_rows[:12]:
            w(f"| {r['title'][:50]} | {r['year']} | {r['model_object'][:28] or '未报告'} | "
              f"{r['main_results'][:90] or '-'} | {r['mechanism_evidence'][:55]} | "
              f"{r['evidence_level']} | {r['analysis_basis']} |")
        w("")
        n_abs = S["analysis_basis_counts"].get("abstract", 0)
        w(f"**证据边界**:{n_abs}/{S['papers_included']} 篇分析依据为 abstract;"
          f"未获取任何全文(ft_fetched=0)。关键数字与统计细节需全文复核(行动清单 P1)。")
        w("")

        # ---------------- Gap Analysis
        w("## 5. Research Gap Analysis(研究空白分析)")
        w("")
        w("> 说明:评分列为**部分评分**(已评估维度按权重归一);F 可行性待评估,"
          "不输出冒充完整的五维总分。")
        w("")
        n_shown = 0
        for g in ranked_gaps:
            n_shown += 1
            sc = g.scores or {}
            dims = sc.get("dimensions", {})
            partial = sc.get("partial_weighted")
            pairing = g.pairing_status
            pairing_txt = {"paired": "竞争配对完成(A/B)",
                           "single_incomplete": "单候选解释,竞争性方案未完成",
                           "pending": "配对未评估"}.get(pairing, pairing)
            w(f"### Gap {n_shown} [{g.gap_id}] [{GAP_TYPE_ZH.get(g.gap_type, g.gap_type)}] "
              f"状态={g.status}|配对={pairing_txt} 部分评分={partial if partial is not None else '待评估'}"
              f"(覆盖权重 {sc.get('weight_coverage', 0):.0%})")
            w("")
            w(f"**问题**:{g.description}")
            w("")
            w(f"- **识别依据**:{g.rationale}")
            w(f"- **现有研究已解决部分**:{g.already_addressed or '(未填写——本 Gap 应被撤回)'}")
            sup = ", ".join(g.supporting_evidence_ids[:5]) or "—(分布统计型)"
            con = ", ".join(g.contradicting_evidence_ids[:5]) or "—"
            w(f"- **支持证据**:{sup}")
            w(f"- **反对/冲突证据**:{con}")
            rs = g.reverse_search or {}
            if rs:
                srcs = "、".join(rs.get("sources", [])) or "未记录"
                w(f"- **反向检索**(源:{srcs},{rs.get('date','')[:10]},状态 {rs.get('final_status')},"
                  f"覆盖 {rs.get('coverage','n/a')}):")
                for lv in rs.get("query_levels", []):
                    per = lv.get("per_source", {}) or {}
                    for sname, ps in per.items():
                        q = ps.get("query", "")
                        w(f"  - L{lv['level']}({lv['strategy']})[{sname}]"
                          f"`{q[:110]}` → {ps.get('request_status')}|"
                          f"审查 {ps.get('examined', 0)}|相关 {len(ps.get('relevant', []))}"
                          f"{('|' + ps.get('note','')[:40]) if ps.get('note') else ''}")
                w(f"  - 判断:{rs.get('decision')}——{rs.get('reasoning','')[:180]}")
                for rp in (rs.get("relevant_papers") or [])[:3]:
                    w(f"    - 相关文献:{rp.get('title','')[:90]}({rp.get('year')},{rp.get('judgment')},{rp.get('source','')})")
            else:
                w("- **反向检索**:未执行(不得视为零结果)")
            w(f"- **分维评分**:")
            for dk, dv in dims.items():
                v = dv.get("value")
                w(f"  - {dk}:**{v if v is not None else '待评估'}**——{dv.get('note','')[:110]}")
            w(f"- **不确定性**:{sc.get('uncertainty','')}")
            w(f"- **限制**:{g.limitation}")
            w("")
        dropped = [g for g in self.store.gaps.values() if g.status == "dropped"]
        if dropped:
            w(f"### 按停止条件剔除的候选({len(dropped)} 项,保留备查)")
            w("")
            for g in dropped:
                w(f"- {g.gap_id}:{g.description[:90]}…")
                w(f"  - 剔除原因:{g.status_reason[:160]}")
            w("")

        # ---------------- Hypotheses
        w("## 6. Hypothesis Discovery(假设发现)")
        w("")
        w("> 系统假设全部 Level 3 待验证;每 Gap 配 2 个竞争性假设供判别实验区分。"
          "作者原文假设(origin=author)单独列于 6.2,不参与竞争性配对。")
        w("")
        w("### 6.1 系统生成的竞争性假设")
        w("")
        from collections import defaultdict
        by_gap = defaultdict(list)
        for h in hyps:
            if h.origin == "system":
                by_gap[h.gap_id].append(h)
        for gap_id, hs in by_gap.items():
            g = self.store.gaps.get(gap_id)
            w(f"#### 针对 {gap_id}:{(g.description[:66] + '…') if g else ''}")
            w("")
            for h in hs:
                pair_note = "" if h.pairing_status == "paired" else (
                    " ⚠️单候选解释(竞争方案未完成)" if h.pairing_status == "single_incomplete" else "")
                ev_note = " ⚠️证据不足(无直接支持证据,仅为候选解释)" if h.insufficient_evidence else ""
                # v0.5.1 修复#6c:待审候选必须明确待审原因,不得混入完整通过方案
                pending = list(getattr(h, "pending_review", []) or [])
                design = getattr(h, "design_completeness", "complete") or "complete"
                proposal = getattr(h, "proposal_status", "") or ""
                pending_note = ""
                if pending or design == "degraded":
                    pending_note = " ⚠️待人工核查(未置完整通过)"
                w(f"**假设 {h.label}({h.hypothesis_id}){pair_note}{ev_note}{pending_note}**")
                w("")
                if proposal:
                    w(f"- **方案状态**:{proposal}")
                if design == "degraded":
                    w(f"- **设计完整性**:降级(degraded)——存在待核查项,未通过完整方案检查")
                if pending:
                    w(f"- **待人工核查项**:")
                    for p in pending:
                        w(f"  - {p}")
                w(f"- **陈述**:{h.statement}")
                w(f"- **机制链**:{h.proposed_mechanism}")
                w(f"  - 证据状态:{h.mechanism_evidence_status}")
                w(f"- **判别性预测**:")
                for pr in h.predictions:
                    w(f"  - {pr}")
                w(f"- **实验设计**:{h.experimental_design}")
                w(f"- **对照**:{'; '.join(h.controls)}")
                w(f"- **读出**:{'; '.join(h.readouts)}")
                w(f"- **替代解释**:{'; '.join(h.alternative_explanations)}")
                w(f"- **反证标准**:{h.falsification_criteria}")
                nc = h.novelty_check or {}
                if nc:
                    w(f"- **新颖性核查**(状态 {nc.get('final_status')}):{nc.get('reasoning','')[:170]}")
                    for rp in (nc.get("relevant_papers") or [])[:2]:
                        w(f"  - {rp.get('title','')[:88]}({rp.get('judgment')})")
                else:
                    w("- **新颖性核查**:未执行(不得视为零结果)")
                w(f"- **可行性**:{h.feasibility.get('note','待评估') if h.feasibility else '待评估'}")
                w("")
        author_hyps = [h for h in hyps if h.origin == "author"]
        if author_hyps:
            w("### 6.2 作者原文假设(登记,非系统生成)")
            w("")
            for h in author_hyps[:10]:
                w(f"- **{h.hypothesis_id}**(来源 {h.source.get('paper_id','')},"
                  f"{h.source.get('location','')}):{h.statement[:170]}")
            w("")

        # ---------------- 核验与审计
        w("## 7. Validation & Audit Trail(核验与审计)")
        w("")
        w("### 7.1 核验结果")
        w("")
        val = validation_summary
        w(f"- 文献真实性:{val.get('paper_stats')}")
        w(f"- 证据定位核验:位置存在+原文匹配+级别一致;剔除 {val.get('evid_dropped',0)} 条不合格")
        w(f"- 假设可检验性:{val.get('hyp_passed',0)}/{val.get('hyp_total',0)} 通过;"
          f"未通过者已降级(degraded)不进正文")
        w(f"- 引用一致性:{val.get('consistency_issues',0)} 项问题(已实际剔除/修复)")
        rc = val.get("report_check", {})
        if rc:
            w(f"- 报告数字核对:{rc.get('checked',0)} 项统计中 {rc.get('mismatched',0)} 项不一致,"
              f"{rc.get('not_checked',0)} 项未执行(不视为通过)")
        w("")
        w("### 7.2 纳入文献(按相关度)")
        w("")
        w("| # | 题名 | 年份 | 设计 | 元数据 | 分析依据 | DOI/PMID |")
        w("| --- | --- | --- | --- | --- | --- | --- |")
        for i, p in enumerate(sorted(papers, key=lambda x: -x.relevance_score)[:20], 1):
            w(f"| {i} | {p.title[:52]} | {p.year} | {DESIGN_ZH.get(p.study_design,p.study_design)} | "
              f"{p.metadata_status} | {p.analysis_basis} | {p.doi or 'PMID:'+p.pmid} |")
        w("")
        w("### 7.3 数据文件")
        w("")
        for name, desc in [("papers.json/papers.csv", "文献+纳排理由+全文四态+来源检索式"),
                           ("evidences.json", "逐条证据(句类/三元组/定位/核验状态)"),
                           ("evidence_matrix.json/.csv", "跨文献矩阵"),
                           ("gaps.json", "Gap+反向检索全记录+部分评分"),
                           ("hypotheses.json", "系统+作者假设"),
                           ("stats_summary.json", "统一统计口径"),
                           ("run_log.json", "全部日志+api_calls 快照"),
                           ("figures/*.png", "统计图(口径同正文)")]:
            w(f"- {name}:{desc}")
        w("")
        w("### 7.4 已知局限(诚实披露)")
        w("")
        w("- 分析全部基于摘要;未获取/解析/核验任何全文(ft_fetched=ft_parsed=0)。")
        w("- 反向检索为题名级筛查(每级审查 5 条);不等于系统综述级别的查重。")
        w("- 评分为规则先验+摘要信号的部分评分;F 维度待评估;非专家评审。")
        w("- Gap/假设质量未经领域专家复核(见人工复核样本清单 manual_review_sample.md)。")
        # v0.4 修复#10:降级边界声明(区分四类证据/结论来源,无法可靠判断的显式待人工)
        w("- **降级边界(v0.4)**:本报告的证据与结论分为四类来源——"
          "① 原文直接结果(L1,论文摘要中报告的实验/临床结果);"
          "② 作者推测(L3 author_speculation,原文中作者以 speculative/may/might 等表述的机制解释);"
          "③ 系统综合(system_inference,跨多篇论文由规则综合得到);"
          "④ 系统待验证(system 假设,Level 3 待验证,非事实)。"
          "当规则引擎无法可靠进行语义判断时(如复合终点、否定范围、机制比较条件),"
          "系统不做猜测,输出待人工核查(needs_manual)并保留原文定位;不存在静默降级。")
        return "\n".join(L)

    # ------------------------------------------------------------ 行动清单
    def render_action_items(self, ranked_gaps, hyps) -> str:
        L = []
        w = L.append
        w("# 下一步科研行动清单(Action Items)")
        w("")
        cand = [g for g in ranked_gaps if g.status != "dropped"]
        w("| 优先级 | 行动 | 预期产出 | 依据 |")
        w("| --- | --- | --- | --- |")
        if cand:
            w(f"| P1 | 对候选 Gap({cand[0].gap_id} 等 {len(cand)} 项)执行人工反向检索与专家核查 | 确认空白是否成立 | 题名级筛查覆盖有限 |")
        w("| P1 | 获取关键论文全文并复核证据位置与统计 | 降低摘要推断误差 | 全部证据为摘要层 |")
        w("| P2 | 设计区分竞争性假设 A/B 的判别实验 | 关键对照与判别结果 | 见报告 6.1 |")
        w("| P2 | 补充团队资源信息后评估 F 维度,得到完整五维评分 | 完整优先级排序 | F 当前待评估 |")
        w("| P3 | 按新证据迭代 Gap/假设(版本化) | 版本化研究方案 | 框架 §14 Phase 6 |")
        w("| P3 | 人体/动物实验前完成伦理与法规核查 | 合规启动 | 系统边界要求 |")
        w("")
        w("> 清单由规则引擎生成;排序为建议,执行前请结合专业判断调整。")
        return "\n".join(L)

    # ------------------------------------------------------------ 人工复核样本
    def render_manual_review_sample(self, ranked_gaps, hyps, n_per_kind=3) -> str:
        """抽取人工复核样本:反向检索判断与科学内容各若干条。"""
        L = []
        w = L.append
        w("# 人工复核样本(Manual Review Sample)")
        w("")
        w("> 目的:对规则引擎的关键判断做人工抽检。工程测试通过 ≠ 科研质量已获专家确认。")
        w("")
        w("## A. 反向检索判断复核(题名级判断是否正确)")
        w("")
        w("| Gap | 文献 | 规则判断 | 需人工确认 |")
        w("| --- | --- | --- | --- |")
        n = 0
        for g in self.store.gaps.values():
            rs = g.reverse_search or {}
            for rp in (rs.get("relevant_papers") or [])[:2]:
                n += 1
                w(f"| {g.gap_id} | {rp.get('title','')[:70]} | {rp.get('judgment')} | "
                  f"该研究是否直接/部分解决该 Gap?({rp.get('reason','')[:60]}) |")
        w("")
        w("## B. 证据分级复核(句类判定是否正确)")
        w("")
        w("| 证据 | 论文 | 位置 | 句类/级别 | 原文(节选) | 需人工确认 |")
        w("| --- | --- | --- | --- | --- | --- |")
        sample = list(self.store.evidences.values())[:n_per_kind * 2]
        for e in sample:
            w(f"| {e.evidence_id} | {e.paper_id[:24]} | {e.location} | {e.sentence_kind}/L{e.evidence_level} | "
              f"{e.claim[:80]} | 分级与归属是否正确 |")
        w("")
        w("## C. Gap 科学内容复核")
        w("")
        for g in list(self.store.gaps.values())[:6]:
            w(f"- **{g.gap_id}({g.gap_type},{g.status})**:{g.description[:120]}")
            w(f"  - 复核点:问题是否真实未被解决?支持证据是否支撑?已解决部分描述是否准确?")
        w("")
        w("## D. 假设备注")
        w("")
        for h in hyps[:6]:
            if h.origin == "system":
                w(f"- **{h.hypothesis_id}**:{h.statement[:110]}(复核:竞争性是否真实、预测是否可区分)")
        return "\n".join(L)

    # ------------------------------------------------------------ 图表(口径同正文)
    def render_figures(self, out_dir: str) -> List[str]:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        # ---- 中文字体保障:注册系统 CJK 字体并选择可用字体 ----
        # 背景:Noto CJK 以 .ttc 字体集合提供,matplotlib 默认只读取第一个 face(JP),
        # 导致配置 "Noto Sans CJK SC" 时找不到,回退到 DejaVu Sans 渲染方框。
        # 注册 .ttc 的默认 face,再动态挑选系统实际可用的 CJK 字体。
        try:
            from matplotlib import font_manager
            # 1) 注册 Noto CJK 字体集合的默认 face
            _noto_sc_candidates = [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
            ]
            for _p in _noto_sc_candidates:
                if os.path.exists(_p):
                    try:
                        font_manager.fontManager.addfont(_p)
                    except Exception:
                        pass
            # 2) 动态挑选系统实际可用的 CJK 字体(按优先级)
            _avail = {f.name for f in font_manager.fontManager.ttflist}
            _pref = ["Noto Sans CJK SC", "Noto Sans CJK JP", "WenQuanYi Micro Hei",
                     "WenQuanYi Zen Hei", "SimHei", "Arial Unicode MS"]
            _chosen = [f for f in _pref if f in _avail]
            if not _chosen:
                _chosen = ["sans-serif"]
            plt.rcParams["font.sans-serif"] = _chosen
        except Exception:
            # 兜底:保持原配置
            plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Micro Hei",
                                               "SimHei", "Arial Unicode MS"]
        plt.rcParams["axes.unicode_minus"] = False
        fig_dir = os.path.join(out_dir, "figures")
        os.makedirs(fig_dir, exist_ok=True)
        paths = []
        S = self.store.stats_summary()
        n_inc = S["papers_included"]

        # 图1 年份:整数年份离散柱图
        years = self.store.year_distribution("included")
        if years:
            ys = sorted(years)
            fig, ax = plt.subplots(figsize=(7, 3.2))
            ax.bar([str(y) for y in ys], [years[y] for y in ys],
                   color="#4C72B0", edgecolor="white")
            ax.set_xlabel("发表年份(整数)")
            ax.set_ylabel("纳入文献数")
            ax.set_title(f"纳入文献年份分布(初筛纳入 N={n_inc})")
            fig.tight_layout()
            p1 = os.path.join(fig_dir, "year_dist.png")
            fig.savefig(p1, dpi=150); plt.close(fig); paths.append(p1)

        # 图2 设计分布(综述单列)
        designs = self.store.design_distribution("included")
        if designs:
            fig, ax = plt.subplots(figsize=(6.5, 3.2))
            ks = [DESIGN_ZH.get(k, k) for k in designs]
            ax.bar(ks, list(designs.values()), color="#55A868", edgecolor="white")
            ax.set_ylabel("篇数")
            ax.set_title(f"研究设计分布(初筛纳入 N={n_inc};综述单列)")
            ax.tick_params(axis="x", labelrotation=20)
            fig.tight_layout()
            p2 = os.path.join(fig_dir, "design_dist.png")
            fig.savefig(p2, dpi=150); plt.close(fig); paths.append(p2)

        # 图3 机制术语(独立论文数;口径=已抽取集合,与正文 3.3 一致)
        n_deep_fig = S["papers_deep_extracted"]
        terms = self.store.count_terms("deep").most_common(12)
        if terms:
            fig, ax = plt.subplots(figsize=(7, 4))
            names = [t for t, _ in terms][::-1]
            vals = [n for _, n in terms][::-1]
            ax.barh(names, vals, color="#C44E52", edgecolor="white")
            ax.set_xlabel("独立论文数(每篇每术语计 1)")
            ax.set_title(f"高频机制/靶点术语(已抽取集合 N={n_deep_fig};未抽取论文不计)")
            fig.tight_layout()
            p3 = os.path.join(fig_dir, "mech_terms.png")
            fig.savefig(p3, dpi=150); plt.close(fig); paths.append(p3)

        # 图4 Gap 部分评分(标注覆盖权重)
        cand = [g for g in self.store.gaps.values()
                if g.status != "dropped" and g.scores]
        fig, ax = plt.subplots(figsize=(7.5, 3.8))
        if cand:
            ids = [g.gap_id for g in cand]
            vals = [g.scores.get("partial_weighted") or 0 for g in cand]
            covs = [g.scores.get("weight_coverage", 0) for g in cand]
            bars = ax.bar(ids, vals, color="#8172B2", edgecolor="white")
            for b, c in zip(bars, covs):
                ax.text(b.get_x() + b.get_width()/2, b.get_height() + 1,
                        f"{c:.0%}", ha="center", fontsize=8)
            ax.set_ylabel("部分评分(已评估维度归一)")
            ax.set_ylim(0, 100)
            ax.set_title("候选 Gap 部分评分(柱上为权重覆盖率;F 待评估,非完整总分)")
            ax.tick_params(axis="x", labelrotation=30)
        else:
            ax.text(0.5, 0.5, "本主题未产生可评分的候选 Gap\n(无配对/单候选均未通过完整性核验)",
                    ha="center", va="center", fontsize=12, transform=ax.transAxes)
            ax.set_xticks([]); ax.set_yticks([])
            ax.set_title("候选 Gap 部分评分(无候选:未产生可评分 Gap)")
        fig.tight_layout()
        p4 = os.path.join(fig_dir, "gap_scores.png")
        fig.savefig(p4, dpi=150); plt.close(fig); paths.append(p4)

        self.log.log("Reporter", "figures", "ok",
                     f"figures={len(paths)} 口径=included(N={n_inc})")
        return paths

    # ------------------------------------------------------------ 导出 PDF/Word
    def export_pdf_word(self, out_dir: str) -> Dict[str, str]:
        """可复现导出:markdown->HTML->weasyprint PDF(base_url 解析图件相对路径);
        markdown->python-docx Word(附录图片嵌入)。"""
        outs = {}
        md_path = os.path.join(out_dir, "report.md")
        if not os.path.exists(md_path):
            return outs
        # ---- PDF(weasyprint;base_url 使 figures/xx.png 可解析嵌入)
        try:
            import markdown as md_lib
            from weasyprint import HTML
            html_body = md_lib.markdown(
                open(md_path, encoding="utf-8").read(),
                extensions=["tables", "fenced_code"])
            html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<style>body{font-family:'Noto Sans CJK SC','WenQuanYi Zen Hei',sans-serif;"
                    "font-size:10pt;line-height:1.5;margin:24px}"
                    "table{border-collapse:collapse;width:100%;font-size:8pt}"
                    "th,td{border:1px solid #999;padding:3px 5px;word-break:break-word}"
                    "code{background:#f4f4f4;font-size:8pt}"
                    "h1{font-size:16pt}h2{font-size:13pt;border-bottom:1px solid #ccc}"
                    "img{max-width:100%}</style></head><body>" + html_body + "</body></html>")
            pdf_path = os.path.join(out_dir, "report.pdf")
            HTML(string=html, base_url=out_dir).write_pdf(pdf_path)
            outs["report.pdf"] = pdf_path
            self.log.log("Reporter", "export_pdf", "ok", "weasyprint(base_url 嵌入图件)")
        except Exception as e:
            self.log.log("Reporter", "export_pdf", "failed", f"{type(e).__name__}: {str(e)[:150]}")
        # ---- Word(python-docx;附录 markdown 图片行 -> 嵌入图片)
        try:
            from docx import Document
            from docx.shared import Pt, Inches
            import re as _re
            doc = Document()
            style = doc.styles["Normal"]
            style.font.name = "Noto Sans CJK SC"
            style.font.size = Pt(10)
            n_imgs = 0
            for line in open(md_path, encoding="utf-8").read().splitlines():
                line = line.rstrip()
                if not line.strip():
                    continue
                m = _re.match(r"^!\[([^\]]*)\]\(([^)]+)\)", line)
                if m:  # markdown 图片 -> 嵌入
                    img_rel = m.group(2)
                    img_path = img_rel if os.path.isabs(img_rel) else os.path.join(out_dir, img_rel)
                    if os.path.exists(img_path):
                        try:
                            doc.add_picture(img_path, width=Inches(5.5))
                            n_imgs += 1
                            continue
                        except Exception:
                            pass  # 回退为文本行
                if line.startswith("# "):
                    doc.add_heading(line[2:], level=1)
                elif line.startswith("## "):
                    doc.add_heading(line[3:], level=2)
                elif line.startswith("### "):
                    doc.add_heading(line[4:], level=3)
                elif line.startswith("- "):
                    doc.add_paragraph(line[2:], style="List Bullet")
                else:
                    doc.add_paragraph(line)
            docx_path = os.path.join(out_dir, "report.docx")
            doc.save(docx_path)
            outs["report.docx"] = docx_path
            self.log.log("Reporter", "export_docx", "ok", f"python-docx(嵌入图 {n_imgs} 张)")
        except Exception as e:
            self.log.log("Reporter", "export_docx", "failed", f"{type(e).__name__}: {str(e)[:150]}")
        return outs

    # ------------------------------------------------------------ 保存全部
    def save_all(self, out_dir: str, topic: str, ranked_gaps, hyps,
                 validation_summary: Dict) -> Dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        md = self.render_markdown(topic, ranked_gaps, hyps, validation_summary)
        md_path = os.path.join(out_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        act = self.render_action_items(ranked_gaps, hyps)
        with open(os.path.join(out_dir, "action_items.md"), "w", encoding="utf-8") as f:
            f.write(act)
        mrs = self.render_manual_review_sample(ranked_gaps, hyps)
        with open(os.path.join(out_dir, "manual_review_sample.md"), "w", encoding="utf-8") as f:
            f.write(mrs)
        figs = self.render_figures(out_dir)
        with open(md_path, "a", encoding="utf-8") as f:
            f.write("\n\n## 附录:统计图\n\n")
            for g in figs:
                f.write(f"![{os.path.basename(g)}](figures/{os.path.basename(g)})\n\n")
        self.log.log("Reporter", "save_all", "ok", f"dir={out_dir}")
        return {"report": md_path, "figures": figs,
                "action_items": os.path.join(out_dir, "action_items.md"),
                "manual_review_sample": os.path.join(out_dir, "manual_review_sample.md")}

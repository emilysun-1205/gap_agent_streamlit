# -*- coding: utf-8 -*-
"""
Literature Gap & Hypothesis Discovery Agent — Streamlit 单体应用入口
====================================================================

架构迁移说明(Netlify 前端 + Render HTTP 后端 → Streamlit 单体应用):
  - 本文件是整个应用的唯一入口(app.py),直接调用原有科研核心
    gap_agent.pipeline.run_pipeline()(科研核心零改动)。
  - 不再依赖 Netlify 前端,不再通过 POST /api/tasks 与独立 HTTP API 通信。
  - gap_agent/ 全部模块、参数定义、数据结构、结果文件格式、引用信息全部保留。

错误处理原则(与旧架构"笼统提交失败"不同):
  1. run_pipeline() 内部自捕获科研流水线异常并返回 status="failed" + failure
     说明 —— 本应用如实展示,绝不伪装成功;
  2. 报告生成/导出阶段的异常可能向外抛出 —— 外层 try/except 捕获并记录 traceback;
  3. 按「文献检索失败 / 网络请求失败 / 分析流水线失败 / 报告生成失败」分类展示;
  4. 详细错误日志(traceback + run_log.json)可在 expander 中查看。

免费云端部署(Streamlit Community Cloud):
  - 主入口 app.py;依赖 requirements.txt;系统依赖 packages.txt(WeasyPrint/CJK 字体)。
  - Community Cloud 无持久化磁盘:任务产物写入系统临时目录,重启/休眠/重新部署会
    丢失,请在会话内及时下载。不引入任何付费数据库/磁盘/服务。
"""

import os
import io
import json
import time
import zipfile
import datetime
import tempfile
import threading
import traceback
from typing import Dict, List, Optional

import streamlit as st

# ---- 科研核心导入(零改动保留;只导入不修改) --------------------------------
from gap_agent.pipeline import run_pipeline
from gap_agent.config import APP_NAME, APP_VERSION
from gap_agent.retriever import get_openalex_key
from gap_agent.qwen_report import analyze_report, QwenError, ENDPOINTS

# ---------------------------------------------------------------------------
# 密钥桥接(仅 UI 层;科研核心零改动)
# Streamlit Community Cloud 的 Secrets 以 st.secrets 提供,不会自动注入 os.environ;
# 核心代码 gap_agent.retriever.get_openalex_key() 读取的是 OPENALEX_API_KEY 环境变量,
# 故在此把 st.secrets 桥接到环境变量(不回显、不写入日志)。
# ---------------------------------------------------------------------------
try:
    if "OPENALEX_API_KEY" in st.secrets:
        os.environ.setdefault("OPENALEX_API_KEY", str(st.secrets["OPENALEX_API_KEY"]))
except Exception:
    pass  # 本地无 secrets 文件时正常(可用环境变量或 ~/.openalex_key)

def _qwen_setting(name, default=""):
    try:
        return str(st.secrets.get(name, os.environ.get(name, default)))
    except Exception:
        return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# 页面与全局配置
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title=f"{APP_NAME} · v{APP_VERSION}",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 参数边界(与 webapp/backend.py 的校验边界一致,不放宽不收紧)
PARAM_BOUNDS = {
    "year_from": (1990, 2026, 2015),
    "max_retrieve": (10, 300, 60),
    "screen_keep": (5, 100, 20),
    "deep_extract": (2, 30, 10),
    "max_gaps": (1, 10, 6),
}

# 真实阶段 → 中文名称(与 pipeline._p() 的实际 stage 名一一对应)
STAGE_ZH = {
    "retrieve": "文献检索(Europe PMC / OpenAlex)",
    "screen": "文献筛选(初筛+Crossref 核验)",
    "extract": "深度文献分析(证据抽取)",
    "gap_hunter": "研究空白识别(含反向检索)",
    "hypothesis": "科研假设生成",
    "validate": "证据核验与审计",
    "rewrite": "竞争性假设重写",
    "score": "Gap 排序评分",
    "export_data": "结构化数据导出",
    "report": "报告生成(Markdown)",
    "report_check": "报告一致性核对",
    "export_pdf_word": "PDF / Word 导出",
    "finish": "完成",
    "exception": "异常",
}

# 阶段 → 进度权重(粗粒度,仅用于进度条示意;真实状态以事件流为准)
STAGE_WEIGHT = {
    "retrieve": 0.12, "screen": 0.28, "extract": 0.45, "gap_hunter": 0.62,
    "hypothesis": 0.72, "validate": 0.80, "rewrite": 0.85, "score": 0.88,
    "export_data": 0.92, "report": 0.95, "report_check": 0.97,
    "export_pdf_word": 0.99, "finish": 1.0,
}

# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------
for _k, _v in {
    "run_result": None,      # run_pipeline() 完整返回 dict
    "events": [],            # progress 回调记录的真实事件流
    "error_log": None,       # 外层异常 traceback 文本
    "error_stage": None,     # 错误分类标签
    "error_brief": None,     # 简明错误原因
    "last_topic": "",
}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _read_json(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_text(path: str, limit: int = 60000) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(limit)
    except Exception:
        return ""


def _classify_failure(result: Optional[Dict], events: List[Dict]) -> str:
    """按真实失败信息分类错误类型(文献检索/网络/流水线/报告)。"""
    if not result:
        return "未知"
    failure = result.get("failure") or ""
    # 内部已知的失败语义(来自 pipeline.py 源码)
    if "检索无结果" in failure or "全部源失败" in failure:
        return "文献检索失败(无结果或全部数据源失败)"
    if "报告一致性核对失败" in failure:
        return "报告核对失败(交付状态降级 DEGRADED,数据完整保留供审计)"
    if "运行异常" in failure:
        # 定位异常发生前最后执行到的阶段
        last = ""
        for e in reversed(events):
            if e.get("stage") in STAGE_ZH:
                last = STAGE_ZH[e["stage"]]
                break
        return f"分析流水线失败(最后阶段:{last})"
    if failure:
        return f"流水线报告失败:{failure[:80]}"
    return "未知"


def _write_error_log(stage: str, brief: str, exc: BaseException) -> str:
    """记录完整 traceback,返回文本(同时写入临时文件便于留存)。"""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    text = (
        f"[{datetime.datetime.now().isoformat()}] stage={stage}\n"
        f"brief={brief}\n" + "=" * 60 + "\n" + tb
    )
    path = os.path.join(tempfile.gettempdir(), f"gap_agent_error_{int(time.time())}.log")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    return text


def _out_dir() -> Optional[str]:
    r = st.session_state.get("run_result")
    return (r or {}).get("out_dir")


def _zip_run_outputs() -> Optional[bytes]:
    """把本次运行产物目录打包为 zip(真实文件),供一键下载。"""
    d = _out_dir()
    if not d or not os.path.isdir(d):
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(d):
            for fn in sorted(files):
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, d))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(f"## 🔬 {APP_NAME}")
    st.caption(f"v{APP_VERSION} · Streamlit 单体应用架构")
    st.divider()
    st.markdown("**科研核心(零改动保留)**")
    st.caption(
        "文献检索 Europe PMC / OpenAlex · Crossref 核验 · 文献筛选 · 深度证据提取 · "
        "Research Gap Discovery(三级反向检索) · Hypothesis Discovery(系统+作者假设) · "
        "证据核验 · PDF/Word/Markdown 报告"
    )
    st.divider()
    if get_openalex_key():
        st.success("OpenAlex 密钥:已配置(仅后台使用,不回显)")
    else:
        st.warning(
            "OpenAlex 密钥未配置:反查按单源(Europe PMC)部分覆盖执行,其余功能不受影响。"
            "可通过环境变量 OPENALEX_API_KEY 或 ~/.openalex_key 提供。"
        )
    st.divider()
    st.caption(
        "⚠️ 存储说明:Streamlit Community Cloud 无持久化磁盘,任务产物为临时文件,"
        "应用休眠/重启/重新部署后会丢失,请在会话内下载。"
    )

# ---------------------------------------------------------------------------
# 标题 + 8 个页签
# ---------------------------------------------------------------------------
st.title("🔬 Literature Gap & Hypothesis Discovery Agent")
st.caption(
    "科研文献空白挖掘与科学假设发现工作台 —— 输入研究主题,直接调用原有科研分析流水线"
    " run_pipeline(),获取真实文献检索、研究空白与竞争性科研假设(无模拟数据)。"
)

(tab_workspace, tab_search, tab_screen, tab_deep, tab_gap, tab_hyp,
 tab_evidence, tab_export) = st.tabs(
    ["Research Workspace", "Literature Search", "Literature Screening",
     "Deep Literature Analysis", "Research Gap Discovery", "Hypothesis Discovery",
     "Evidence & References", "Export Reports"]
)

# ===========================================================================
# Tab 1 — Research Workspace:主题输入 + 参数 + 启动(真实进度)
# ===========================================================================
with tab_workspace:
    st.subheader("研究主题与研究问题")

    topic = st.text_area(
        "研究主题(中文或英文)",
        value=st.session_state.get("last_topic", ""),
        height=90,
        placeholder="例如:肿瘤缺氧、免疫检查点阻断、耐药",
        help="作为 run_pipeline(topic=...) 的输入,由规划器(planner)生成检索式。",
        key="topic_input",
    )

    st.markdown("#### 科研分析参数(与原流水线参数一一对应)")
    def _num(key: str, label: str, help_txt: str):
        lo, hi, default = PARAM_BOUNDS[key]
        return st.number_input(label, min_value=lo, max_value=hi,
                               value=default, step=1, help=help_txt, key=key)

    c1, c2, c3 = st.columns(3)
    with c1:
        max_retrieve = _num("max_retrieve", "max_retrieve(检索上限)", "每源最多检索文献数")
        year_from = _num("year_from", "year_from(起始年份)", "检索起始年份")
    with c2:
        screen_keep = _num("screen_keep", "screen_keep(初筛保留)", "初筛后保留的文献数")
        max_gaps = _num("max_gaps", "max_gaps(空白上限)", "最多生成的研究空白数")
    with c3:
        deep_extract = _num("deep_extract", "deep_extract(深度抽取)", "进入深度文献分析的文献数")
        sources = st.multiselect(
            "数据源 sources", options=["europepmc", "openalex"],
            default=["europepmc", "openalex"], key="sources_input",
            help="OpenAlex 需密钥;缺密钥时反查按单源部分覆盖执行。",
        )

    st.caption(
        "提示:首次测试建议用小参数(如 max_retrieve=10 / screen_keep=5 / deep_extract=2),"
        "完整分析耗时数分钟(含真实网络检索与反向核验)。"
    )
    st.divider()

    run_clicked = st.button(
        "▶ 启动科研分析流水线", type="primary", width="stretch",
        disabled=not topic.strip(), key="run_btn",
    )

    if run_clicked and not topic.strip():
        st.error("请先输入研究主题。")
    elif run_clicked and not sources:
        st.error("请至少选择一个数据源(europepmc / openalex)。")
    elif run_clicked and topic.strip():
        # 重置状态
        for k in ("run_result", "error_log", "error_stage", "error_brief"):
            st.session_state[k] = None
        st.session_state["events"] = []
        st.session_state["last_topic"] = topic.strip()

        params = {
            "max_retrieve": int(max_retrieve), "screen_keep": int(screen_keep),
            "deep_extract": int(deep_extract), "year_from": int(year_from),
            "max_gaps": int(max_gaps), "sources": list(sources) or ["europepmc"],
        }
        out_root = tempfile.mkdtemp(prefix="gap_agent_runs_")  # 临时目录(无持久盘)

        events: List[Dict] = st.session_state["events"]
        holder: Dict = {"result": None, "exc": None}

        def _worker():
            try:
                holder["result"] = run_pipeline(
                    topic=topic.strip(), out_root=out_root,
                    max_retrieve=params["max_retrieve"],
                    screen_keep=params["screen_keep"],
                    deep_extract=params["deep_extract"],
                    year_from=params["year_from"],
                    max_gaps=params["max_gaps"],
                    sources=params["sources"],
                    progress=lambda s, st_, d="": events.append(
                        {"ts": _now(), "stage": s, "status": st_, "detail": d}),
                )
            except BaseException as e:          # 报告生成/导出等外抛异常在此捕获
                holder["exc"] = e
                events.append({"ts": _now(), "stage": "exception",
                               "status": "failed", "detail": f"{type(e).__name__}: {e}"})

        thread = threading.Thread(target=_worker, daemon=True)
        with st.status("科研分析流水线执行中(真实网络检索,请勿关闭页面)…",
                       expanded=True) as status:
            st.write(f"调用 run_pipeline()  topic='{topic.strip()}'  参数={params}")
            prog = st.progress(0.02, text="排队启动…")
            latest = st.empty()
            thread.start()
            while thread.is_alive():
                time.sleep(0.5)
                if events:
                    e = events[-1]
                    w = STAGE_WEIGHT.get(e["stage"])
                    if w is not None:
                        prog.progress(min(w, 0.995),
                                      text=f"{STAGE_ZH.get(e['stage'], e['stage'])} · {e['status']}")
                    latest.write(
                        f"`[{e['ts']}]` **{STAGE_ZH.get(e['stage'], e['stage'])}**"
                        f" · {e['status']} · {e.get('detail', '')[:120]}"
                    )
            thread.join()

        result = holder["result"]
        exc = holder["exc"]
        if exc is not None:
            status.update(label="任务失败(外层异常)", state="error", expanded=True)
        elif result is not None and result.get("status") == "failed":
            status.update(label=f"任务失败/降级:{_classify_failure(result, events)}",
                          state="error", expanded=True)
        else:
            status.update(label="科研分析流水线执行完成", state="complete", expanded=False)

        if exc is not None:
            # 外层失败(如报告生成/文件导出抛出):真实 traceback,不伪装成功
            brief = f"{type(exc).__name__}: {exc}"
            stage_label = "报告生成 / 结果导出阶段(流水线外层异常)"
            st.session_state["error_stage"] = stage_label
            st.session_state["error_brief"] = brief
            st.session_state["error_log"] = _write_error_log("outer", brief, exc)
            st.error(f"❌ 任务失败 · {stage_label}:{brief}")
            st.info("这是未预期异常,完整 traceback 见下方日志;流水线日志见 run_log.json。")
        elif result is None:
            st.session_state["error_stage"] = "未知(线程无结果)"
            st.session_state["error_brief"] = "流水线线程结束但未返回结果"
            st.error("❌ 任务失败:流水线未返回结果(未知错误,请查看事件流)。")
        else:
            st.session_state["run_result"] = result
            if result.get("status") == "failed":
                kind = _classify_failure(result, events)
                st.session_state["error_stage"] = kind
                st.session_state["error_brief"] = result.get("failure", "")
                st.error(f"❌ 任务失败/降级 · {kind}")
                st.markdown(f"**失败原因(流水线原文)**:{result.get('failure', '')}")
                st.info(
                    "流水线内部异常已被捕获并如实落盘(out_dir 日志与数据完整保留供审计);"
                    "这不是成功状态,结果页签中仅展示已真实生成的部分。"
                )
            else:
                s = result.get("stats", {})
                st.success(
                    f"✅ 分析完成 · run_id=`{result.get('run_id')}` · "
                    f"检索 {s.get('n_retrieved', 0)} 篇 → 纳入 {s.get('n_included', 0)} 篇 → "
                    f"深抽 {s.get('n_deep', 0)} 篇 · Gap {result.get('n_ranked_gaps', 0)} 条 · "
                    f"报告假设 {result.get('n_hyps_reported', 0)} 条"
                )

    # 错误详情(简明原因 + 详细日志)
    if st.session_state.get("error_brief"):
        with st.expander("🧾 查看详细错误日志(traceback / run_log)", expanded=False):
            st.markdown(f"**错误分类**:{st.session_state.get('error_stage')}")
            if st.session_state.get("error_log"):
                st.code(st.session_state["error_log"], language="text")
            rl = _read_json(os.path.join(_out_dir() or "", "run_log.json"))
            if rl:
                errs = [e for e in rl.get("events", [])
                        if e.get("status") in ("error", "failed", "empty_or_failed")]
                st.markdown(f"**run_log.json 中的失败/降级事件({len(errs)} 条)**")
                st.json(errs[:50] if errs else rl.get("events", [])[-10:])

    # 真实事件流
    if st.session_state.get("events"):
        with st.expander("📋 本次运行真实进度事件流(progress 回调)", expanded=False):
            st.dataframe(
                [{"时间": e["ts"], "阶段": STAGE_ZH.get(e["stage"], e["stage"]),
                  "状态": e["status"], "详情": e.get("detail", "")}
                 for e in st.session_state["events"]],
                width="stretch", hide_index=True,
            )

# ===========================================================================
# Tab 2 — Literature Search:检索与数据源状态
# ===========================================================================
with tab_search:
    st.subheader("文献检索")
    r = st.session_state.get("run_result")
    if not r:
        st.info("尚未运行分析。请在 Research Workspace 输入主题并启动流水线。")
    else:
        stats = r.get("stats", {})
        c1, c2, c3 = st.columns(3)
        c1.metric("检索文献数 n_retrieved", stats.get("n_retrieved", 0))
        c2.metric("初筛纳入 n_included", stats.get("n_included", 0))
        c3.metric("深度抽取 n_deep", stats.get("n_deep", 0))

        src = r.get("source_stats") or _read_json(os.path.join(_out_dir() or "", "source_stats.json")) or {}
        if src:
            st.markdown("#### 数据源实际状态(source_stats,流水线原文)")
            st.json(src)

        papers = _read_json(os.path.join(_out_dir() or "", "papers.json")) or []
        st.markdown(f"#### 检索到的文献({len(papers)} 篇,来自 papers.json)")
        if papers:
            st.dataframe(
                [{"paper_id": p.get("paper_id", ""), "标题": p.get("title", ""),
                  "年份": p.get("year", ""), "期刊": p.get("journal", ""),
                  "引用": p.get("cited_by", 0), "撤稿": p.get("retracted", False),
                  "DOI": p.get("doi", ""),
                  "来源": "+".join(sorted({s.get("source", "")
                                           for s in p.get("source_refs", [])}))}
                 for p in papers],
                width="stretch", hide_index=True,
            )
        else:
            st.warning("本轮无检索结果(或全部源失败)——流水线诚实行为,不产出 Gap/假设。")

# ===========================================================================
# Tab 3 — Literature Screening
# ===========================================================================
with tab_screen:
    st.subheader("文献筛选")
    r = st.session_state.get("run_result")
    if not r:
        st.info("尚未运行分析。")
    else:
        papers = _read_json(os.path.join(_out_dir() or "", "papers.json")) or []
        included = [p for p in papers if p.get("include")]
        st.markdown(f"#### 初筛纳入({len(included)} 篇 / 共 {len(papers)} 篇)")
        if included:
            st.dataframe(
                [{"paper_id": p.get("paper_id", ""), "标题": p.get("title", ""),
                  "相关度": round(float(p.get("relevance_score", 0) or 0), 3),
                  "评级": p.get("relevance_grade", ""),
                  "设计": p.get("study_design", ""),
                  "模型": "|".join(p.get("models", []) or []),
                  "纳入理由": p.get("include_reason", ""),
                  "Crossref 元数据": p.get("metadata_status", "")}
                 for p in included],
                width="stretch", hide_index=True,
            )
            n_rev = sum(1 for p in included if "review" in (p.get("study_design") or "").lower())
            st.caption(f"其中综述类约 {n_rev} 篇(综述初筛配额 ≤25%,单列不与原始研究混计)。")
        else:
            st.warning("无纳入文献。")

# ===========================================================================
# Tab 4 — Deep Literature Analysis
# ===========================================================================
with tab_deep:
    st.subheader("深度文献分析")
    r = st.session_state.get("run_result")
    if not r:
        st.info("尚未运行分析。")
    else:
        papers = _read_json(os.path.join(_out_dir() or "", "papers.json")) or []
        deep = [p for p in papers
                if (p.get("extraction") or {}) or p.get("extraction_status") not in ("", "pending", None)]
        st.markdown(f"#### 深度抽取文献({len(deep)} 篇;deep_extract 上限 {r.get('stats', {}).get('n_deep', 0)})")
        if deep:
            for p in deep:
                with st.expander(f"{p.get('title', '')[:90]} · {p.get('paper_id', '')}"):
                    st.markdown(f"**全文状态**:{p.get('full_text_status', '')}"
                                f"(ft_link_found={p.get('ft_link_found', False)},"
                                f"ft_parsed={p.get('ft_parsed', False)})")
                    ext = p.get("extraction") or {}
                    st.markdown(f"**抽取字段**:{list(ext.keys()) if ext else '(空)'}")
                    st.json(ext)
        else:
            st.warning("无深度抽取结果。")

# ===========================================================================
# Tab 5 — Research Gap Discovery
# ===========================================================================
with tab_gap:
    st.subheader("研究空白识别(Research Gap Discovery)")
    r = st.session_state.get("run_result")
    if not r:
        st.info("尚未运行分析。")
    else:
        gaps = _read_json(os.path.join(_out_dir() or "", "gaps.json")) or []
        st.markdown(f"#### 识别到的研究空白({len(gaps)} 条;排序后上报 {r.get('n_ranked_gaps', 0)} 条)")
        if gaps:
            for g in sorted(gaps, key=lambda x: str(x.get("gap_id", ""))):
                rs = g.get("reverse_search") or {}
                title = f"[{g.get('gap_id', '')}] {g.get('description', '')[:88]}"
                with st.expander(title):
                    st.markdown(f"**类型**:{g.get('gap_type', '')} · "
                                f"**状态**:{g.get('status', '')}"
                                f"({g.get('status_reason', '')}) · "
                                f"**配对**:{g.get('pairing_status', '')}")
                    st.markdown(f"**描述**:{g.get('description', '')}")
                    st.markdown(f"**依据**:{g.get('rationale', '')}")
                    if g.get("already_addressed"):
                        st.markdown(f"**已有研究**:{g.get('already_addressed', '')}")
                    if g.get("limitation"):
                        st.markdown(f"**局限**:{g.get('limitation', '')}")
                    if rs:
                        st.markdown(
                            f"**反向检索**:final_status={rs.get('final_status', '')} · "
                            f"coverage={rs.get('coverage', '')} · "
                            f"n_examined_total={rs.get('n_examined_total', 0)} · "
                            f"decision={rs.get('decision', '')}"
                        )
                    if g.get("scores"):
                        st.json(g["scores"])
        else:
            st.warning("本轮未识别到研究空白(可能因检索无结果或未达 MIN_PAPERS=5 门槛)——诚实行为。")

# ===========================================================================
# Tab 6 — Hypothesis Discovery
# ===========================================================================
with tab_hyp:
    st.subheader("科研假设生成(Hypothesis Discovery)")
    r = st.session_state.get("run_result")
    if not r:
        st.info("尚未运行分析。")
    else:
        hyps = _read_json(os.path.join(_out_dir() or "", "hypotheses.json")) or []
        sys_h = [h for h in hyps if h.get("origin") == "system"]
        aut_h = [h for h in hyps if h.get("origin") == "author"]
        c1, c2, c3 = st.columns(3)
        c1.metric("系统生成假设", len(sys_h))
        c2.metric("其中整体通过核验", sum(1 for h in sys_h if (h.get("validation") or {}).get("passed")))
        c3.metric("作者原文假设(登记)", len(aut_h))
        st.caption("系统假设与作者假设分离统计;待审(pending_review)假设单独呈现,不冒充完整通过。")

        if hyps:
            for h in hyps:
                v = h.get("validation") or {}
                origin = "系统生成" if h.get("origin") == "system" else "作者原文"
                state = "✅ 通过" if v.get("passed") else (
                    "🟡 待审" if h.get("pending_review") or v.get("pending_review") else "❌ 未通过")
                title = (f"[{h.get('hypothesis_id', '')}] ({origin}·{state}) "
                         f"{h.get('label', '') or (h.get('statement', '') or '')[:70]}")
                with st.expander(title):
                    st.markdown(f"**假设(label)**:{h.get('label', '')}")
                    st.markdown(f"**陈述(statement)**:{h.get('statement', '')}")
                    st.markdown(f"**针对 Gap**:{h.get('gap_id', '')} · "
                                f"**配对状态**:{h.get('pairing_status', '')}")
                    if h.get("proposal_status"):
                        st.markdown(f"**方案状态**:{h.get('proposal_status', '')}"
                                    f"(设计完整性:{h.get('design_completeness', '')})")
                    if h.get("pending_review"):
                        st.markdown(f"**待人工核查项**:{'; '.join(h['pending_review'])}")
                    st.markdown(f"**拟议机制**:{h.get('proposed_mechanism', '')}")
                    for i, pr in enumerate(h.get("predictions", []) or [], 1):
                        st.markdown(f"**预测 P{i}**:{pr}")
                    ed = h.get("experimental_design") or {}
                    if ed:
                        st.markdown(f"**实验设计**:{json.dumps(ed, ensure_ascii=False)[:500]}")
                    if h.get("controls"):
                        st.markdown(f"**对照**:{'; '.join(h['controls'])}")
                    if h.get("readouts"):
                        st.markdown(f"**共同读出**:{'; '.join(h['readouts'])}")
                    if h.get("falsification_criteria"):
                        st.markdown(f"**反证标准**:{h.get('falsification_criteria', '')}")
                    nc = h.get("novelty_check") or {}
                    if nc:
                        st.markdown(f"**新颖性核查**:status={nc.get('status', '')} · "
                                    f"{nc.get('note', '')}")
                    if h.get("insufficient_evidence"):
                        st.markdown("**⚠️ 无直接支持证据:允许输出但已标注**")
                    st.json(v)
        else:
            st.warning("本轮未生成科研假设。")

# ===========================================================================
# Tab 7 — Evidence & References
# ===========================================================================
with tab_evidence:
    st.subheader("证据与参考文献")
    r = st.session_state.get("run_result")
    if not r:
        st.info("尚未运行分析。")
    else:
        d = _out_dir() or ""
        evs = _read_json(os.path.join(d, "evidences.json")) or []
        st.markdown(f"#### 证据条目({len(evs)} 条;证据矩阵见 evidence_matrix.csv)")
        if evs:
            st.dataframe(
                [{"evidence_id": e.get("evidence_id", ""), "paper_id": e.get("paper_id", ""),
                  "主张(节选)": (e.get("claim", "") or "")[:80],
                  "句类": e.get("sentence_kind", ""), "类型": e.get("evidence_type", ""),
                  "分级": e.get("evidence_level", ""), "效应": e.get("effect", ""),
                  "位置核验": "✅" if e.get("verified") else "❌"}
                 for e in evs],
                width="stretch", hide_index=True,
            )
        else:
            st.warning("无证据条目。")

        papers = _read_json(os.path.join(d, "papers.json")) or []
        refs = [p for p in papers if p.get("include")]
        st.markdown(f"#### 参考文献(纳入文献 {len(refs)} 篇)")
        if refs:
            st.dataframe(
                [{"paper_id": p.get("paper_id", ""), "标题": p.get("title", ""),
                  "作者": (p.get("authors", "") or "")[:60],
                  "期刊": p.get("journal", ""), "年份": p.get("year", ""),
                  "DOI": p.get("doi", ""), "PMID": p.get("pmid", "")}
                 for p in refs],
                width="stretch", hide_index=True,
            )

        sm = _read_json(os.path.join(d, "stats_summary.json")) or r.get("summary") or {}
        if sm:
            st.markdown("#### 统计摘要(stats_summary)")
            st.json(sm)

# ===========================================================================
# Tab 8 — Export Reports
# ===========================================================================
with tab_export:
    st.subheader("导出报告")
    r = st.session_state.get("run_result")
    d = _out_dir()
    if not r or not d:
        st.info("尚未运行分析,无报告可下载。")
    else:
        st.markdown(f"运行产物目录(临时):`{d}` · run_id:`{r.get('run_id')}` · "
                    f"状态:**{'ok' if r.get('status') == 'ok' else r.get('status')}**")
        if r.get("status") != "ok":
            st.warning(f"本轮流水线状态为 {r.get('status')},以下仅为已真实生成的文件:"
                       f"{r.get('failure', '')}")
        st.caption("⚠️ Community Cloud 无持久化磁盘:请在本会话内下载;应用休眠/重启后临时文件丢失。")

        st.markdown("#### Qwen 生成式 AI 报告分析")
        st.caption("将本次报告节选及最多30篇纳入文献的摘要发送到阿里云百炼，进行文献解读、跨文献比较、Gap复核、假设建议与批判性审查。")
        qwen_key = _qwen_setting("DASHSCOPE_API_KEY")
        qwen_region = st.selectbox("Qwen 服务地域", list(ENDPOINTS), key="qwen_region")
        qwen_model = st.text_input("Qwen 模型", value=_qwen_setting("QWEN_MODEL", "qwen3.8-flash"), key="qwen_model")
        if qwen_key:
            st.success("Qwen 密钥已配置（仅后台使用）")
        else:
            st.info('请在应用 Settings → Secrets 添加 DASHSCOPE_API_KEY = "你的百炼密钥"。')
        st.caption("调用消耗百炼额度，额度不足时停止；每次点击只请求一次，不自动重试。可在百炼控制台为模型开启仅使用免费额度。")
        if st.button("🧠 使用 Qwen 分析本次报告", disabled=not qwen_key or r.get("status") != "ok"):
            try:
                with st.spinner("Qwen 正在分析文献与报告，请稍候…"):
                    analyze_report(d, qwen_key, model=qwen_model,
                                   base_url=_qwen_setting("QWEN_BASE_URL", ENDPOINTS[qwen_region]))
                st.success("Qwen 分析已完成，结果尚待人工复核。")
            except QwenError as exc:
                st.error(str(exc))
            except Exception:
                st.error("Qwen 分析失败，未更新分析结果。请检查输入产物和服务配置。")
        qwen_md_path = os.path.join(d, "qwen_analysis.md")
        if os.path.exists(qwen_md_path):
            qwen_result = _read_json(os.path.join(d, "qwen_analysis.json")) or {}
            st.caption(f"已保存分析模型：{qwen_result.get('model')} · 时间：{qwen_result.get('created_at')} · Token用量：{qwen_result.get('usage', {})}")
            qwen_md = open(qwen_md_path, encoding="utf-8").read()
            with st.expander("查看 Qwen 分析", expanded=True):
                st.markdown(qwen_md)
            st.download_button("⬇️ Qwen 分析报告（Markdown）", qwen_md,
                               file_name="qwen_analysis.md", mime="text/markdown", key="dl_qwen")
        st.divider()

        outputs: Dict = r.get("outputs") or {}
        report_files = [
            ("Markdown 报告", outputs.get("report") or os.path.join(d, "report.md"),
             "text/markdown"),
            ("PDF 报告", outputs.get("report.pdf") or os.path.join(d, "report.pdf"),
             "application/pdf"),
            ("Word 报告", outputs.get("report.docx") or os.path.join(d, "report.docx"),
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("行动项", outputs.get("action_items") or os.path.join(d, "action_items.md"),
             "text/markdown"),
            ("人工复核样本", outputs.get("manual_review_sample") or
             os.path.join(d, "manual_review_sample.md"), "text/markdown"),
        ]
        c1, c2 = st.columns(2)
        for i, (label, path, mime) in enumerate(report_files):
            with (c1 if i % 2 == 0 else c2):
                if path and os.path.exists(path):
                    st.download_button(f"⬇️ {label}", data=open(path, "rb").read(),
                                       file_name=os.path.basename(path), mime=mime,
                                       key=f"dl_{i}", width="stretch")
                else:
                    st.warning(f"{label}:未生成(文件不存在,如实展示,不伪装成功)。")

        st.divider()
        st.markdown("#### 结构化结果(JSON / CSV,store.export() 原样落盘)")
        names = ["papers.json", "evidences.json", "evidence_matrix.json", "gaps.json",
                 "hypotheses.json", "stats_summary.json", "evidence_matrix.csv",
                 "papers.csv", "search_plan.json", "source_stats.json",
                 "report_check.json", "run_log.json", "qwen_analysis.json"]
        cc1, cc2, cc3 = st.columns(3)
        for i, fn in enumerate(names):
            fp = os.path.join(d, fn)
            with (cc1 if i % 3 == 0 else (cc2 if i % 3 == 1 else cc3)):
                if os.path.exists(fp):
                    st.download_button(f"⬇️ {fn}", data=open(fp, "rb").read(),
                                       file_name=fn,
                                       mime="text/csv" if fn.endswith(".csv") else "application/json",
                                       key=f"dlj_{fn}", width="stretch")
                else:
                    st.caption(f"{fn}:不存在")

        # 图件
        figs = outputs.get("figures") or []
        if figs:
            st.divider()
            st.markdown(f"#### 统计图件({len(figs)} 张,口径同正文)")
            cols = st.columns(min(len(figs), 4))
            for i, fp in enumerate(figs):
                if os.path.exists(fp):
                    with cols[i % len(cols)]:
                        st.image(fp, caption=os.path.basename(fp))

        # 一键打包
        st.divider()
        zbytes = _zip_run_outputs()
        if zbytes:
            st.download_button("📦 一键下载本次运行全部产物(ZIP)", data=zbytes,
                               file_name=f"gap_agent_run_{r.get('run_id', 'export')}.zip",
                               mime="application/zip", width="stretch",
                               key="dl_zip")

# ---------------------------------------------------------------------------
st.divider()
st.caption(
    f"{APP_NAME} v{APP_VERSION} · 科研核心 gap_agent/ 零改动保留 · Streamlit 单体应用 · "
    "免费云端部署(Streamlit Community Cloud)· 本应用不产生模拟数据"
)

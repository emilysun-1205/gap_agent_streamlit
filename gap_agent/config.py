"""Gap Discovery Agent - 全局配置 v0.3"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

APP_NAME = "Literature Gap & Hypothesis Discovery Agent"
APP_VERSION = "0.5.1"

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(WORKSPACE, "runs")

# ---------------- Europe PMC
EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_PAGE = 100
EUROPEPMC_RATE_SLEEP = 0.35

# ---------------- OpenAlex
OPENALEX_API = "https://api.openalex.org/works"
OA_PAGE = 100                 # per-page(官方上限 100,不得超)
OA_MAX_PAGES = 3              # 每查询分页上限(本地请求次数上限)
OA_RATE_SLEEP = 0.5           # 请求间隔(秒)
# v0.5 修复#1/#5:区分"本地请求次数上限"与"远端每日费用额度"。
# 本地请求次数上限:OA_MAX_PAGES(每查询)与 OA_LOCAL_REQUEST_CAP(每 run 全部
# OpenAlex 请求,跨主检索/Gap反查/假设核查共享)。
# 远端每日费用额度:由官方响应头 X-RateLimit-Limit/Remaining 反映,本地不预设
# 硬编码额度,而是读取响应头并据此决定是否继续(避免把远端额度误当本地预算)。
OA_LOCAL_REQUEST_CAP = 40     # 每 run 本地 OpenAlex 请求次数上限(跨三环节共享)
OA_DAILY_BUDGET = 12          # 兼容旧名:保留为本地请求预算别名(见 retriever)

# ---------------- Crossref / 通用
CROSSREF_API = "https://api.crossref.org/works/"
CROSSREF_MAILTO = os.environ.get("AGENT_CONTACT_MAIL", "gap-agent@example.org")
HTTP_TIMEOUT = 25
HTTP_RETRIES = 3
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (research prototype; mailto:{CROSSREF_MAILTO})"

# ---------------- 检索默认参数
DEFAULT_MAX_RETRIEVE = 120
DEFAULT_SCREEN_KEEP = 40
DEFAULT_DEEP_EXTRACT = 20
DEFAULT_YEAR_FROM = 2015

# ---------------- 反向检索预算(v0.3:不再"只看5条就停")
RS_EXAMINED_PER_LEVEL = 10    # 每级每源实际检查题名数
RS_MIN_RELEVANT_FOR_STOP = 2  # 触发"已解决"所需的强证据研究数(单篇也可,需强判定)
RS_TOTAL_EXAMINE_BUDGET = 40  # 每 Gap 反查总检查预算(跨级跨源)

# ---------------- Gap 评分权重(文档 §11)
GAP_WEIGHTS = {"novelty": 0.25, "significance": 0.25, "evidence": 0.20,
               "testability": 0.15, "feasibility": 0.15}
SCORE_SCALE = 5
SCORE_TO_100 = 20

# ---------------- 假设修复
HYP_MAX_REWRITES = 2          # 竞争假设 B 最多重写次数

EVIDENCE_LEVELS = {
    1: "直接证据:论文明确报告且可定位原文",
    2: "综合推论:多篇研究共同支持,需说明推理过程和局限",
    3: "待验证假设:模型提出的新解释,必须标记为假设",
}
GAP_TYPES = ["contradiction", "mechanism", "methodological",
             "context", "translational", "insufficient"]
GAP_TYPE_ZH = {"contradiction": "结论冲突", "mechanism": "机制空白",
               "methodological": "方法学空白", "context": "对象/情境空白",
               "translational": "转化空白", "insufficient": "证据不足"}

DISCLAIMER = (
    "本 Agent 是科研辅助工具,不替代领域专家判断、系统综述规范、统计咨询、伦理审查或临床决策。"
    "所有假设均为待验证假设,不构成既有科学事实;涉及人体、动物、临床样本或生物安全的实验,"
    "仍需研究团队核查伦理、法规、实验条件与专业规范。")


def new_run_id() -> str:
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

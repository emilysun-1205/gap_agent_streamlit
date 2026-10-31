"""
Module 1:文献检索器 v0.3
新增:
- OpenAlex 适配器(Authorization: Bearer 认证,密钥不进 URL/日志):
  分页/预算/限速/429按Retry-After退避/限额头解析(X-RLR-*);abstract_inverted_index 重建;
  撤稿标记/OA状态/全文链接(仅线索);状态区分:缺密钥/认证失败/额度不足/瞬时限速/服务错误/有效零结果
- 双源编排:各源编译自己的检索方言;DOI/PMID/规范题名联合去重+传递性合并+冲突保留
- 审计:url+脱敏参数+状态+响应证据(可重放 vs 可重查分开标注)
修复:
- Crossref 核验带 DOI 参数与响应快照(可重查)
- OpenAlex 元数据不自动等同 Crossref verified
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Tuple

import requests

from .config import (EUROPEPMC_SEARCH, EUROPEPMC_PAGE, EUROPEPMC_RATE_SLEEP,
                     CROSSREF_API, USER_AGENT, HTTP_TIMEOUT, HTTP_RETRIES,
                     OA_PAGE, OA_RATE_SLEEP, OA_MAX_PAGES, OA_DAILY_BUDGET,
                     OA_LOCAL_REQUEST_CAP)
from .schemas import (Paper, RunLog, SourceRef, clean_ws, norm_title,
                      paper_key, yes, redact)

HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# OpenAlex 限额响应头(官方文档: X-RateLimit-Limit / X-RateLimit-Remaining /
# X-RateLimit-Credits-Used / X-RateLimit-Reset)。v0.5 修复#5:弃用旧 x-rlr-*。
OA_RATE_HEADERS = ["x-ratelimit-limit", "x-ratelimit-remaining",
                   "x-ratelimit-credits-used", "x-ratelimit-reset"]

# v0.5 修复#1:run 级共享 OpenAlex 本地请求次数预算(非每次调用重置)。
# 区分"本地请求次数上限"(OA_LOCAL_REQUEST_CAP,跨主检索/Gap反查/假设核查共享)
# 与"远端每日费用额度"(由官方响应头 X-RateLimit-Limit/Remaining 反映,本地不
# 预设硬编码额度,而是读取响应头决定是否继续)。
# 预算在"实际 HTTP 请求边界"统一扣减:每次真正发出请求(含重试)都扣减,而非
# 仅在继续翻页路径扣减。单页结束/零结果/末页/失败/重试均计。
_oa_budget_remaining = OA_LOCAL_REQUEST_CAP


def reset_oa_budget() -> None:
    """在每次 run 的检索编排开始时调用,重置本地请求次数预算。"""
    global _oa_budget_remaining
    _oa_budget_remaining = OA_LOCAL_REQUEST_CAP


def oa_budget_remaining() -> int:
    return _oa_budget_remaining


def _consume_oa_budget(n: int = 1) -> None:
    """在每次实际 HTTP 请求边界扣减本地请求次数预算(含重试)。"""
    global _oa_budget_remaining
    _oa_budget_remaining = max(0, _oa_budget_remaining - n)


# ================================================================ HTTP 基础
class SourceError(Exception):
    """带分类的源错误:v0.3 区分失败原因,不虚构。"""
    def __init__(self, kind: str, detail: str, http_status: int = 0):
        self.kind = kind      # no_key | auth_failed | quota_exceeded | rate_limited | server_error | network_error
        self.detail = detail
        self.http_status = http_status
        super().__init__(f"{kind}: {detail}")


def _classify_openalex_error(status: int, body: str,
                             rate_headers: Dict | None = None) -> str:
    """OpenAlex 错误分类(v0.5 修复#5,按官方文档)。

    官方文档(help.openalex.org/api/authentication/):
      - 401: 认证失败(无效/缺失密钥)
      - 403: 可表示密钥未激活(key_not_activated,未点击激活邮件链接)或
             每日费用额度耗尽(quota_exceeded);需结合响应体区分。
      - 429: 可表示每日预算耗尽(quota_exceeded)或瞬时限速(rate_limited);
             需结合响应体/响应头区分。
      - 5xx: 服务错误。
    未知原因保留 unknown,不得默认成功或臆断为某类。
    """
    b = (body or "").lower()
    if status == 401 or "unauthorized" in b or "invalid api key" in b:
        return "auth_failed"
    if status == 403:
        # 官方:403 可表密钥未激活(key_not_activated)。响应体含该字样则归
        # key_not_activated(≠ quota_exceeded)。
        # v0.5.1 修复#2:未知/空正文 403 不得臆断为 quota_exceeded——只有明确
        # 证据(正文含 quota/budget/daily 或响应头 Remaining=0)才标额度耗尽;
        # 否则保留 unknown(可附 forbidden 语义),由上层原样传递。
        if "key_not_activated" in b or "not activated" in b or "activation" in b:
            return "key_not_activated"
        # v0.5.1 修复#2 补充:HTTP 头大小写不敏感,规范化键后再取 Remaining。
        rh = {str(k).lower(): v for k, v in (rate_headers or {}).items()}
        remaining = rh.get("x-ratelimit-remaining")
        if ("quota" in b or "budget" in b or "daily" in b
                or (remaining is not None and str(remaining).strip() == "0")):
            return "quota_exceeded"
        return "unknown"
    if status == 429:
        # 官方:429 可表每日预算耗尽或瞬时限速。响应体含 quota/budget/daily
        # 字样或响应头 X-RateLimit-Remaining=0 则归 quota_exceeded;
        # "limit"/"rate limit"等措辞是瞬时限速,不得误判为额度耗尽。
        rh = {str(k).lower(): v for k, v in (rate_headers or {}).items()}
        remaining = rh.get("x-ratelimit-remaining")
        if ("quota" in b or "budget" in b or "daily" in b
                or (remaining is not None and str(remaining).strip() == "0")):
            return "quota_exceeded"
        return "rate_limited"
    if status >= 500:
        return "server_error"
    if status == 0:
        return "network_error"
    return "unknown"


def get_openalex_key(run_log: RunLog | None = None) -> Optional[str]:
    """密钥来源:OPENALEX_API_KEY 环境变量 > ~/.openalex_key 文件。
    只读取;绝不写入日志或回显。"""
    k = os.environ.get("OPENALEX_API_KEY", "").strip()
    if k:
        return k
    path = os.path.expanduser("~/.openalex_key")
    if os.path.exists(path):
        try:
            k = open(path).read().strip()
            if k and run_log:
                run_log.register_secret(k)
            return k or None
        except OSError:
            return None
    return None


def _get_json(url: str, params: dict, run_log: RunLog, tool: str,
              headers: Dict | None = None, retries: int = HTTP_RETRIES,
              snapshot_fn=None, snapshot_full: bool = False,
              budget_consume: bool = False) -> Tuple[Optional[dict], int, bool, Dict, int, str]:
    """返回 (data, http_status, ok, rate_headers, requests_used, error_kind)。

    requests_used 为本次调用实际发出的 HTTP 请求次数(含重试),供上层
    meta_info["requests"] 如实记录(缺陷1:重试计数明确)。

    error_kind 为结构化错误分类(空串表示成功)。v0.5.1 修复#2:HTTP 层
    完成分类后由上层原样传递,上层不得用空正文重新猜测原因。

    v0.5 修复#1/#5 + v0.5.1 修复#1/#2:
      - budget_consume=True 时,在**每一次实际 HTTP 尝试之前**(含所有重试)
        检查剩余预算;预算不足立即停止发送,实际请求次数不超过运行上限。
        预算不足且未发出任何请求 → error_kind="budget_exhausted"。
      - 错误分类使用结构化错误码+正文+响应头;空正文不覆盖分类(未知保留
        unknown 并逐层传递)。
    """
    hdrs = dict(HEADERS)
    if headers:
        hdrs.update(headers)
    last_status = 0
    requests_used = 0
    last_kind = ""
    for attempt in range(1, retries + 1):
        # v0.5.1 修复#1:预算检查前置于每一次实际 HTTP 尝试(含重试)。
        # 预算不足即停止发送,不得"先发后扣"或"扣到 0 仍继续重试"。
        if budget_consume and _oa_budget_remaining <= 0:
            run_log.log("Retriever", tool, "budget_exhausted",
                        f"本地请求预算耗尽,停止发送(attempt {attempt}/{retries}, "
                        f"已发 {requests_used} 次)")
            if requests_used == 0:
                return None, 0, False, {}, 0, "budget_exhausted"
            # 已发出过请求(部分尝试):保留已有状态,标记预算耗尽
            return None, last_status, False, {}, requests_used, (last_kind or "budget_exhausted")
        if budget_consume:
            _consume_oa_budget(1)
        requests_used += 1
        try:
            r = requests.get(url, params=params, headers=hdrs, timeout=HTTP_TIMEOUT)
            last_status = r.status_code
            rate_headers = {h: r.headers.get(h) for h in OA_RATE_HEADERS if r.headers.get(h)}
            if r.status_code == 429:
                # 官方:429 可表每日预算耗尽或瞬时限速。用正文+响应头分类。
                body = r.text[:300]
                kind = _classify_openalex_error(r.status_code, body, rate_headers)
                if kind == "quota_exceeded":
                    # 每日预算耗尽:不再重试,直接返回失败(避免无谓重试)。
                    run_log.log("Retriever", tool, "quota_exceeded",
                                f"429 每日预算耗尽(attempt {attempt}/{retries}) "
                                f"limit_headers={rate_headers}")
                    run_log.log_api(tool, url, params, r.status_code, False, 0, None,
                                    replayable=False, note="429 每日预算耗尽;响应体已脱敏不入档")
                    return None, r.status_code, False, rate_headers, requests_used, "quota_exceeded"
                # 瞬时限速:按 Retry-After 或指数退避;最多重试 retries 次
                wait = float(r.headers.get("Retry-After") or 0) or min(2 ** attempt * 1.5, 30)
                run_log.log("Retriever", tool, "rate_limited",
                            f"429 瞬时限速 backoff {wait:.0f}s (attempt {attempt}/{retries}) "
                            f"limit_headers={rate_headers}")
                time.sleep(wait)
                last_kind = "rate_limited"
                continue
            if r.status_code in (401, 403):
                body = r.text[:300]
                kind = _classify_openalex_error(r.status_code, body, rate_headers)
                run_log.log("Retriever", tool, kind, f"status={r.status_code} body_snippet(脱敏)")
                run_log.log_api(tool, url, params, r.status_code, False, 0, None,
                                replayable=False,
                                note=f"auth/error {kind};响应体已脱敏不入档")
                return None, r.status_code, False, rate_headers, requests_used, kind
            if r.status_code != 200:
                run_log.log("Retriever", tool, "http_error", f"status={r.status_code} attempt={attempt}")
                last_kind = "server_error" if r.status_code >= 500 else "http_error"
                time.sleep(1.5 * attempt)
                continue
            data = r.json()
            snap = None
            if snapshot_fn:
                snap = snapshot_fn(data)
            run_log.log_api(tool, url, params, r.status_code, True,
                            _n_results(tool, data), snap,
                            replayable=snapshot_full,
                            note="响应含快照(可重放)" if snapshot_full else "参数可重查;快照为首条摘要")
            return data, r.status_code, True, rate_headers, requests_used, ""
        except (requests.RequestException, ValueError) as e:
            last_status = -1
            last_kind = "network_error"
            run_log.log("Retriever", tool, "request_error",
                        redact(f"{type(e).__name__}: {str(e)[:150]}", run_log._secrets))
            time.sleep(1.5 * attempt)
    run_log.log_api(tool, url, params, last_status, False, 0, None,
                    replayable=False, note="重试耗尽(网络/服务错误);参数可重查")
    return None, last_status, False, {}, requests_used, (last_kind or "unknown")


def _n_results(tool: str, data: dict) -> int:
    if tool == "europepmc":
        return int(data.get("hitCount") or 0)
    if tool == "openalex":
        return int(data.get("meta", {}).get("count") or 0)
    if tool == "crossref":
        return len(data.get("message", {}).get("items") or [])
    return 1


# ================================================================ Europe PMC
def europepmc_search(query: str, max_results: int, run_log: RunLog,
                     year_from: int | None = None) -> Tuple[List[dict], Dict]:
    """Europe PMC 检索(v0.4 修复#4:统一结构化结果契约)。

    返回 (records, meta_info)。meta_info 含:
      source / status(ok|failed|partial|no_key|not_executed)
      total_hits / returned_count / examined_count / truncated / truncation_reason
      error_kind / http_status / request_id
    请求失败时 status=failed,不再伪装成"有效零结果"。
    """
    q = query if year_from is None else f"({query}) AND (PUB_YEAR:[{year_from} TO 3000])"
    records: List[dict] = []
    cursor = "*"
    request_ok = True
    total_hits = 0
    http_status = 0
    error_kind = ""
    request_id = ""
    while len(records) < max_results:
        params = {"query": q, "format": "json", "resultType": "core",
                  "pageSize": min(EUROPEPMC_PAGE, max_results - len(records)),
                  "cursorMark": cursor}
        data, status, ok, _, _, err_kind = _get_json(
            EUROPEPMC_SEARCH, params, run_log, "europepmc",
            snapshot_fn=lambda d: [{"title": (x.get("title") or "")[:120],
                                    "doi": x.get("doi"), "pmid": x.get("pmid")}
                                   for x in (d.get("resultList", {}).get("result", []) or [])[:1]])
        http_status = status
        if not ok or not data:
            request_ok = False
            error_kind = err_kind or "request_failed"
            break
        total_hits = int(data.get("hitCount") or 0)
        results = data.get("resultList", {}).get("result", [])
        if not results:
            break
        records.extend(results)
        cursor = data.get("nextCursorMark", "")
        if not cursor or cursor == "*":
            break
        time.sleep(EUROPEPMC_RATE_SLEEP)
    truncated = len(records) >= max_results and total_hits > len(records)
    if not request_ok:
        status = "failed"
    elif truncated:
        status = "partial"
    else:
        status = "ok"
    meta_info = {
        "source": "europepmc", "status": status,
        "records": len(records), "total_hits": total_hits,
        "returned_count": len(records), "examined_count": len(records),
        "truncated": truncated,
        "truncation_reason": "result_cap" if truncated else "",
        "error_kind": error_kind, "http_status": http_status,
        "request_id": request_id,
    }
    run_log.log("Retriever", "europepmc", status,
                f"fetched={len(records)} total_hits={total_hits} "
                f"truncated={truncated} "
                f"({'有效检索' if request_ok else '请求失败,结果不完整,不代表零命中'})")
    return records, meta_info



# ================================================================ OpenAlex
def rebuild_abstract_from_inverted(inv: Dict | None) -> str:
    """从 abstract_inverted_index 重建摘要文本;缺失返回空串(如实,不伪装)。"""
    if not isinstance(inv, dict) or not inv:
        return ""
    positions: List[Tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return clean_ws(" ".join(w for _, w in positions))


def openalex_search(query: str, max_results: int, run_log: RunLog,
                    year_from: int | None = None,
                    per_page: int = OA_PAGE) -> Tuple[List[dict], Dict]:
    """OpenAlex Works 检索(Bearer 认证)。返回 (records, meta_info)。

    v0.5 修复#1/#5:
      - 页长上限 100(官方),per_page 超 100 时钳制到 100。
      - 预算在"实际 HTTP 请求边界"统一扣减:每次真正发出请求(含重试)都经
        _get_json(budget_consume=True) 扣减本地请求次数预算,而非仅在继续翻页
        路径扣减。单页结束/零结果/末页/失败/重试均计。
      - 区分本地请求次数上限(OA_LOCAL_REQUEST_CAP,跨主检索/Gap反查/假设核查
        共享)与远端每日费用额度(由官方响应头 X-RateLimit-Limit/Remaining 反映,
        本地不预设硬编码额度,读取响应头决定是否继续)。
      - 重试计数明确:meta_info 记录 requests(实际发出请求数)与 retries。
    """
    key = get_openalex_key(run_log)
    if not key:
        run_log.log("Retriever", "openalex", "no_key",
                    "缺少 OPENALEX_API_KEY(环境变量或 ~/.openalex_key):本源跳过,不虚构失败原因")
        return [], {"status": "no_key"}
    run_log.register_secret(key)
    headers = {"Authorization": f"Bearer {key}"}

    # 官方页长上限 100;per_page 超 100 时钳制(不得请求 200)。
    per_page = min(int(per_page or OA_PAGE), 100)
    params: Dict[str, str] = {"filter": query, "per-page": str(per_page),
                              "sort": "relevance_score:desc", "mailto": "gap-agent@example.org"}
    if year_from:
        params["filter"] += f",from_publication_date:{year_from}-01-01"

    records: List[dict] = []
    cursor = "*"
    page = 0
    retries_used = 0
    meta_info: Dict = {"status": "ok", "requests": 0, "retries": 0,
                       "rate": [], "truncated": False,
                       "remote_limit": None, "remote_remaining": None}
    global _oa_budget_remaining
    # v0.5.1 修复#1:预算为 0 时不得返回 status=ok + 空记录(会被下游当成
    # "有效零结果")。未发出任何请求 → not_executed + budget_exhausted。
    if _oa_budget_remaining <= 0:
        run_log.log("Retriever", "openalex", "not_executed",
                    f"本地请求预算已耗尽(remaining=0):未发出任何请求,"
                    f"query='{query[:90]}'")
        return [], {"status": "not_executed", "requests": 0, "retries": 0,
                    "error_kind": "budget_exhausted", "truncated": True,
                    "truncation_reason": "budget_exhausted",
                    "coverage": "none", "rate": [],
                    "remote_limit": None, "remote_remaining": None,
                    "note": "预算耗尽,未执行检索;不得视为零结果"}
    # 本地请求次数上限:跨主检索/Gap反查/假设核查共享(OA_LOCAL_REQUEST_CAP)。
    # 远端每日费用额度:由响应头 X-RateLimit-Limit/Remaining 反映,本地不预设
    # 硬编码额度,而是读取响应头决定是否继续。
    while len(records) < max_results and page < OA_MAX_PAGES and _oa_budget_remaining > 0:
        page += 1
        p = dict(params)
        p["cursor"] = cursor
        data, status, ok, rate_h, req_used, err_kind = _get_json(
            "https://api.openalex.org/works", p, run_log, "openalex",
            headers=headers,
            snapshot_fn=lambda d: [{"title": (d.get("results") or [{}])[0].get("title", "")[:120],
                                    "doi": (d.get("results") or [{}])[0].get("doi", ""),
                                    "oa_id": (d.get("results") or [{}])[0].get("id", "")}]
                                   if d.get("results") else None,
            budget_consume=True)
        # 实际 HTTP 请求次数(含重试)累加,而非仅翻页次数(缺陷1:重试计数明确)
        meta_info["requests"] += req_used
        retries_used += max(0, req_used - 1)
        if rate_h:
            meta_info["rate"].append(rate_h)
            # 记录远端每日费用额度(官方头)
            if "x-ratelimit-limit" in rate_h:
                meta_info["remote_limit"] = rate_h["x-ratelimit-limit"]
            if "x-ratelimit-remaining" in rate_h:
                meta_info["remote_remaining"] = rate_h["x-ratelimit-remaining"]
        if not ok or data is None:
            # v0.5.1 修复#2:错误原因由 HTTP 层结构化返回并原样传递,
            # 不再用空正文重新分类(否则 key_not_activated 会被重猜成
            # quota_exceeded)。
            meta_info["http_status"] = status
            meta_info["error_kind"] = err_kind or "unknown"
            if err_kind == "budget_exhausted" and not records:
                meta_info["status"] = "not_executed"
                meta_info["truncation_reason"] = "budget_exhausted"
                meta_info["coverage"] = "none"
            elif err_kind == "budget_exhausted":
                # 已取得部分记录后预算耗尽:保留记录,标 partial + 覆盖不足
                meta_info["status"] = "partial"
                meta_info["truncation_reason"] = "budget_exhausted"
                meta_info["coverage"] = "partial_budget"
            else:
                meta_info["status"] = "failed"
            break
        results = data.get("results", []) or []
        if not results:
            break
        records.extend(results)
        count = int((data.get("meta") or {}).get("count") or 0)
        if len(records) >= count:
            break
        nxt = (data.get("meta") or {}).get("next_cursor")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(OA_RATE_SLEEP)
    if len(records) >= max_results:
        records = records[:max_results]
        meta_info["truncated"] = True
        meta_info["truncation_reason"] = "result_cap"
    elif _oa_budget_remaining <= 0 or page >= OA_MAX_PAGES:
        meta_info["truncated"] = True
        meta_info["truncation_reason"] = "budget_or_page_cap"
    meta_info["retries"] = retries_used
    run_log.log("Retriever", "openalex",
                "ok" if meta_info["status"] == "ok" else meta_info.get("error_kind", "failed"),
                f"query='{query[:90]}' fetched={len(records)} requests={meta_info['requests']} "
                f"retries={retries_used} truncated={meta_info['truncated']}"
                f"({meta_info.get('truncation_reason','')}) "
                f"rate_last={meta_info['rate'][-1] if meta_info['rate'] else 'n/a'}")
    return records, meta_info


def openalex_to_paper(rec: dict, query_name: str = "", query_text: str = "") -> Paper:
    """OpenAlex 记录 -> 统一 Paper(映射依据留存)。"""
    doi = ((rec.get("doi") or "").replace("https://doi.org/", "").strip().lower())
    ids = rec.get("ids") or {}
    oa_id = (rec.get("id") or "").replace("https://openalex.org/", "").strip()
    pmid = str(rec.get("ids", {}).get("pmid") or "").replace("https://pubmed.ncbi.nlm.nih.gov/", "").strip()
    loc = rec.get("primary_location") or {}
    src = (loc.get("source") or {}) if loc else {}
    best_oa = rec.get("best_oa_location") or {}
    is_oa = bool((rec.get("open_access") or {}).get("is_oa"))
    oa_url = ((rec.get("open_access") or {}).get("oa_url")
              or (best_oa.get("pdf_url") if isinstance(best_oa, dict) else "")
              or "")
    # v0.4 修复#7:OpenAlex 撤稿字段为 is_retracted(布尔),非 retracted_date。
    # 兼容旧字段;优先 is_retracted。
    retracted = bool(rec.get("is_retracted", rec.get("retracted_date")))
    abstract = rebuild_abstract_from_inverted(rec.get("abstract_inverted_index"))
    key = paper_key(doi, pmid, rec.get("title") or "")
    fts = ("full_available" if (is_oa or oa_url) else
           ("abstract_only" if abstract else "no_abstract"))
    p = Paper(
        paper_id=key["primary"],
        title=clean_ws(rec.get("title") or ""),
        authors=", ".join(a.get("author", {}).get("display_name", "")
                          for a in (rec.get("authorships") or [])[:8])[:300],
        year=rec.get("publication_year"),
        journal=clean_ws(src.get("display_name") or "") if isinstance(src, dict) else "",
        doi=doi, pmid=pmid,
        pub_types=[rec.get("type") or "unknown"],
        abstract=abstract,
        cited_by=int(rec.get("cited_by_count") or 0),
        retracted=retracted,
        oa_id=oa_id,
        ft_link_found=bool(is_oa or oa_url),
        ft_raw={"is_oa": is_oa, "oa_url": (oa_url or "")[:200],
                "oa_status": (rec.get("open_access") or {}).get("oa_status"),
                "is_retracted": rec.get("is_retracted"),
                "retracted_date": rec.get("retracted_date"),
                "type": rec.get("type")},
        full_text_status=fts,
        metadata_status="not_checked",
        metadata_check={"dedup_keys": key,
                        "openalex_id": oa_id,
                        "ids_pmid": ids.get("pmid", ""),
                        "mapping_note": "OpenAlex 元数据映射;未做 Crossref 核验(不自动等同 verified)"},
    )
    p.source_refs.append(SourceRef(
        source="openalex", source_id=oa_id or key["primary"],
        query_name=query_name, query_text=query_text).to_dict())
    return p


# ================================================================ Europe PMC -> Paper
def to_paper(rec: dict, source: str = "europepmc", source_query: str = "",
             query_text: str = "") -> Paper:
    ji = rec.get("journalInfo", {}) or {}
    journal = (ji.get("journal", {}) or {}).get("title", "")
    doi = (rec.get("doi") or "").strip().lower()
    pmid = str(rec.get("pmid") or "").strip() or str(rec.get("ext_id") or "").strip()
    key = paper_key(doi, pmid, rec.get("title") or "")
    oa = yes(rec.get("isOpenAccess"))
    in_epmc = yes(rec.get("inEPMC"))
    in_pmc = yes(rec.get("inPMC"))
    ft_link_found = bool(oa or in_epmc or in_pmc)
    has_abs = bool(clean_ws(rec.get("abstractText") or ""))
    status = "full_available" if ft_link_found else ("abstract_only" if has_abs else "no_abstract")
    pub_types = rec.get("pubType") or []
    if isinstance(pub_types, str):
        pub_types = [pub_types]
    p = Paper(
        paper_id=key["primary"],
        title=clean_ws(rec.get("title") or ""),
        authors=clean_ws(rec.get("authorString") or "")[:300],
        year=ji.get("yearOfPublication"),
        journal=journal or "",
        doi=doi,
        pmid=pmid if pmid else "",
        pub_types=[str(t) for t in pub_types],
        abstract=clean_ws(rec.get("abstractText") or ""),
        cited_by=int(rec.get("citedByCount") or 0),
        ft_link_found=ft_link_found,
        ft_raw={"isOpenAccess": rec.get("isOpenAccess"),
                "inEPMC": rec.get("inEPMC"), "inPMC": rec.get("inPMC")},
        full_text_status=status,
        metadata_status="unverified",
        metadata_check={"dedup_keys": key},
    )
    p.source_refs.append(SourceRef(
        source=source, source_id=pmid or doi or key["primary"],
        query_name=source_query, query_text=query_text).to_dict())
    return p


# ================================================================ 跨源去重合并
def dedup_papers(papers: List[Paper], run_log: RunLog | None = None) -> Tuple[List[Paper], Dict]:
    """DOI->PMID->规范题名联合去重;传递性合并(union-find);冲突保留双方字段值。"""
    parent = list(range(len(papers)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_doi: Dict[str, int] = {}
    by_pmid: Dict[str, int] = {}
    by_title: Dict[str, int] = {}
    for i, p in enumerate(papers):
        keys = p.metadata_check.get("dedup_keys", {}) or {}
        doi, pmid, tk = keys.get("doi", ""), keys.get("pmid", ""), keys.get("title_key", "")
        if doi:
            if doi in by_doi:
                union(i, by_doi[doi])
            else:
                by_doi[doi] = i
        if pmid:
            if pmid in by_pmid:
                union(i, by_pmid[pmid])
            else:
                by_pmid[pmid] = i
        if tk and tk != "t:":
            if tk in by_title:
                union(i, by_title[tk])
            else:
                by_title[tk] = i

    groups: Dict[int, List[int]] = {}
    for i in range(len(papers)):
        groups.setdefault(find(i), []).append(i)

    merged: List[Paper] = []
    stats = {"raw": len(papers), "groups": len(groups),
             "merged_pairs": 0, "conflicts": []}
    for root, members in groups.items():
        members = sorted(members)           # 稳定:以首条为基础,不以后条覆盖
        base = papers[members[0]]
        for j in members[1:]:
            other = papers[j]
            stats["merged_pairs"] += 1
            # 互补字段合并(不覆盖已有)
            if other.doi and not base.doi:
                base.doi = other.doi
            if other.pmid and not base.pmid:
                base.pmid = other.pmid
            if other.oa_id and not base.oa_id:
                base.oa_id = other.oa_id
            if other.abstract and not base.abstract:
                base.abstract = other.abstract
                if base.full_text_status == "no_abstract":
                    base.full_text_status = other.full_text_status if other.full_text_status != "no_abstract" else base.full_text_status
            if other.cited_by > base.cited_by:
                base.cited_by = other.cited_by
            if other.ft_link_found and not base.ft_link_found:
                base.ft_link_found = True
                if base.full_text_status == "abstract_only":
                    base.full_text_status = "full_available"
            # 来源合并(保留全部)
            for ref in other.source_refs:
                if ref not in base.source_refs:
                    base.source_refs.append(ref)
            # 元数据冲突记录(年份/题名差异;不静默覆盖)
            if other.year and base.year and other.year != base.year:
                stats["conflicts"].append({
                    "paper_id": base.paper_id, "field": "year",
                    "values": [base.year, other.year],
                    "resolution": "保留首源值,记录差异供审计"})
        merged.append(base)
    if run_log:
        run_log.log("Retriever", "dedup", "ok",
                    f"raw={stats['raw']} merged={len(merged)} pairs={stats['merged_pairs']} "
                    f"conflicts={len(stats['conflicts'])} (DOI>PMID>题名,union-find 传递合并)")
    return merged, stats


# ================================================================ Crossref
def crossref_verify(paper: Paper, run_log: RunLog) -> Paper:
    if not paper.doi:
        paper.metadata_status = "partial"
        paper.metadata_check = {**paper.metadata_check, "crossref": "no_doi",
                                "note": "无 DOI;未做 Crossref 核对(非核对通过)"}
        return paper
    data, status, ok, _, _, _ = _get_json(
        CROSSREF_API + paper.doi, {}, run_log, "crossref",
        snapshot_fn=lambda d: {"title": " ".join((d.get("message", {}) or {}).get("title") or [])[:160]},
        snapshot_full=False)
    if not ok or not data:
        paper.metadata_check = {**paper.metadata_check, "crossref": "lookup_failed"}
        run_log.log("Retriever", "crossref", "failed", f"doi={paper.doi}")
        return paper
    msg = data.get("message", {})
    cr_title = clean_ws(" ".join(msg.get("title") or []))
    dates = {}
    for k, label in (("published-print", "print"), ("published-online", "online"), ("issued", "issued")):
        parts = (msg.get(k, {}) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            dates[label] = parts[0][0]
    match_title = bool(cr_title) and norm_title(cr_title) == norm_title(paper.title)
    epmc_year = paper.year
    year_match = any(epmc_year and v == epmc_year for v in dates.values()) if dates else None
    if match_title and year_match:
        paper.metadata_status = "verified"
        basis = f"题名匹配;年份一致({dates})"
    elif match_title and (year_match is None or year_match):
        paper.metadata_status = "verified"
        basis = f"题名匹配;年份一致或不可比({dates or '无'})"
    elif match_title:
        paper.metadata_status = "partial"
        basis = f"题名匹配但年份不一致:EPMC={epmc_year} vs Crossref={dates};保留差异记录"
        if dates.get("online"):
            paper.year = dates["online"]
    elif cr_title:
        paper.metadata_status = "partial"
        basis = f"题名不匹配"
    else:
        paper.metadata_status = "unverified"
        basis = "Crossref 无题名返回"
    paper.metadata_check = {**paper.metadata_check,
                            "crossref_title": cr_title[:200], "title_match": match_title,
                            "crossref_dates": dates, "epmc_year": epmc_year,
                            "year_match": year_match, "basis": basis}
    run_log.log("Retriever", "crossref",
                "ok" if paper.metadata_status == "verified" else paper.metadata_status,
                f"doi={paper.doi} {basis[:110]}")
    return paper


# ================================================================ 检索编排
def retrieve(plan, max_results: int, run_log: RunLog,
             sources: List[str] | None = None) -> Tuple[List[Paper], Dict]:
    """双源检索编排。返回 (papers, source_stats)。"""
    sources = sources or ["europepmc", "openalex"]
    raw: List[Paper] = []
    stats: Dict[str, Dict] = {}
    per_q = max(1, max_results // max(1, len(plan.queries)))

    # ---- Europe PMC(其方言检索式)
    if "europepmc" in sources:
        epmc_records = 0
        epmc_meta_all = {"status": "ok", "truncated": False, "total_hits": 0}
        for q in plan.queries:
            eq = q.get("epmc_query") or q["query"]
            recs, meta = europepmc_search(eq, per_q, run_log, plan.year_from)
            # 聚合各查询状态(优先级 failed > unknown > partial > ok)。
            # v0.5 缺陷2:unknown/no_key/not_executed 不得默认成功,逐层传递未知。
            st = meta.get("status") or "unknown"
            if st == "failed":
                epmc_meta_all["status"] = "failed"
            elif st in ("unknown", "no_key", "not_executed"):
                if epmc_meta_all["status"] != "failed":
                    epmc_meta_all["status"] = "unknown"
            elif st == "partial":
                if epmc_meta_all["status"] not in ("failed", "unknown"):
                    epmc_meta_all["status"] = "partial"
            epmc_meta_all["truncated"] = epmc_meta_all.get("truncated") or meta.get("truncated", False)
            epmc_meta_all["total_hits"] = epmc_meta_all.get("total_hits", 0) + meta.get("total_hits", 0)
            for rec in recs:
                try:
                    raw.append(to_paper(rec, "europepmc", q["name"], eq))
                    epmc_records += 1
                except Exception as e:
                    run_log.log("Retriever", "to_paper", "error", f"{type(e).__name__}: {str(e)[:100]}")
        stats["europepmc"] = {"records": epmc_records,
                              "status": epmc_meta_all["status"],
                              "truncated": epmc_meta_all["truncated"],
                              "total_hits": epmc_meta_all["total_hits"]}
    # ---- OpenAlex(其方言检索式)
    if "openalex" in sources:
        reset_oa_budget()   # v0.4 修复#7:run 级共享预算,编排开始时归零
        key_ok = get_openalex_key(run_log)
        if key_ok:
            run_log.register_secret(key_ok)
        oa_records = 0
        oa_meta_all = {"status": "ok"}
        for q in plan.queries:
            oq = q.get("openalex_query") or ""
            if not oq:      # 无 OpenAlex 方言:记录未执行该式
                run_log.log("Retriever", "openalex", "skipped_query",
                            f"查询 {q['name']} 无 OpenAlex 方言编译")
                continue
            recs, meta = openalex_search(oq, per_q, run_log, plan.year_from)
            # v0.5 缺陷2:异常状态保留且不被后续 ok 查询覆盖;unknown 不默认成功
            if meta.get("status") != "ok":
                if oa_meta_all.get("status") in ("failed", "unknown") and \
                        meta.get("status") not in ("failed", "unknown"):
                    pass  # 已记录更严重状态,保留
                else:
                    oa_meta_all = dict(meta)
            for rec in recs:
                try:
                    raw.append(openalex_to_paper(rec, q["name"], oq))
                    oa_records += 1
                except Exception as e:
                    run_log.log("Retriever", "openalex_to_paper", "error", str(e)[:100])
        stats["openalex"] = {"records": oa_records, "status": oa_meta_all.get("status", "ok"),
                             "truncated": oa_meta_all.get("truncated", False)}
        if not key_ok:
            stats["openalex"]["status"] = "no_key"
    merged, dedup_stats = dedup_papers(raw, run_log)
    # 跨源统计
    epmc_ids = {p.paper_id for p in merged if any(r["source"] == "europepmc" for r in p.source_refs)}
    oa_ids = {p.paper_id for p in merged if any(r["source"] == "openalex" for r in p.source_refs)}
    stats["cross_source"] = {
        "raw_total": dedup_stats["raw"], "merged": len(merged),
        "both_sources": len(epmc_ids & oa_ids),
        "epmc_only": len(epmc_ids - oa_ids), "openalex_only": len(oa_ids - epmc_ids),
        "merged_pairs": dedup_stats["merged_pairs"],
        "field_conflicts": len(dedup_stats["conflicts"]),
        "abstract_available": sum(1 for p in merged if p.abstract),
    }
    run_log.log("Retriever", "retrieve", "ok",
                f"sources={list(stats)} merged={len(merged)} "
                f"cross={stats['cross_source']}")
    return merged, stats

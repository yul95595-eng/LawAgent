"""判例数据源适配层 —— 阶段 3「数据接真」。

两条路：
- `local`（默认）：仓库自带 `data/legal/precedents.json`，66 条带真实案号的判例，
  本地 TF-IDF 检索，零外网依赖；
- `api`：配置 `PRECEDENT_API_URL`（+ 可选 `PRECEDENT_API_KEY`）后走远端判例库
  （商业判例库 / 自建检索服务都行）。

远端返回的结果必须先过 `_normalize` 这道闸：**没有 case_no 的一律丢弃**。
类案参考是要写进法律意见书给客户看的，宁可少给几条，也不能给一条
无法回溯的「某法院某年某案」。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.core.config import settings
from backend.retrieval.precedents import CASE_TYPE_LABELS, search_precedents

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


def _remote_enabled() -> bool:
    return (
        (settings.precedent_data_source or "local").strip().lower() == "api"
        and bool(settings.precedent_api_url)
    )


def _normalize(item: dict[str, Any], fallback_case_type: str | None = None) -> dict | None:
    """把各种来源的判例条目压成我方契约；缺案号的直接丢弃。"""
    case_no = str(item.get("case_no") or item.get("caseNo") or item.get("案号") or "").strip()
    if not case_no:
        return None
    case_type = str(item.get("case_type") or item.get("caseType") or fallback_case_type or "")
    try:
        year = int(item.get("year") or item.get("judge_year") or case_no[:4])
    except (TypeError, ValueError):
        year = None
    return {
        "id": str(item.get("id") or case_no),
        "case_no": case_no,
        "court": str(item.get("court") or ""),
        "year": year,
        "case_type": case_type,
        "case_type_label": str(item.get("case_type_label") or CASE_TYPE_LABELS.get(case_type, case_type)),
        "topic": str(item.get("topic") or item.get("title") or ""),
        "result": str(item.get("result") or item.get("judgement") or ""),
        "law_basis": str(item.get("law_basis") or item.get("laws") or ""),
        "score": float(item.get("score") or 0.0),
        "source": str(item.get("source") or "api"),
    }


async def search_precedents_remote(
    query: str, case_type: str | None = None, top_k: int = 3,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """远端判例检索。任何异常都抛出去，由 `search_precedents_any` 决定降级。"""
    params: dict[str, Any] = {"q": query, "top_k": top_k}
    if case_type:
        params["case_type"] = case_type
    headers = {"Authorization": f"Bearer {settings.precedent_api_key}"} if settings.precedent_api_key else {}

    owned = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.get(settings.precedent_api_url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owned:
            await client.aclose()

    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("判例库返回结构不符合约定（应为 list 或 {'results': [...]}）")

    out = []
    for item in rows:
        if isinstance(item, dict):
            norm = _normalize(item, fallback_case_type=case_type)
            if norm:
                out.append(norm)
    return out[:top_k]


async def search_precedents_any(
    query: str, case_type: str | None = None, top_k: int = 3,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """统一检索入口：远端优先（如已配置），失败/空结果回落本地判例库。

    回落是安全的：本地库同样是真实案号，只是覆盖面小；且每条结果带
    `source` 字段（api/local），报告里能看出引用来自哪。
    """
    if _remote_enabled():
        try:
            hits = await search_precedents_remote(query, case_type, top_k, client=client)
            if hits:
                return hits
            logger.info("判例库远端无命中，回落本地判例库：%s", query[:40])
        except Exception as exc:  # noqa: BLE001 - 外部依赖，降级不阻断业务
            logger.warning("判例库远端检索失败，回落本地判例库：%s", exc)

    local = search_precedents(query, case_type=case_type, top_k=top_k)
    for h in local:
        h.setdefault("source", "local")
    return local

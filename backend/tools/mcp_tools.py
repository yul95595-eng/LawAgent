"""MCP 工具的业务逻辑（FastMCP server 与进程内工具共用同源函数）。

为什么拆 logic：
- FastMCP server（backend/mcp_server/server.py）把这些函数暴露成标准 MCP
  工具，任何 MCP 客户端都能调用；
- 图节点进程内直接调这些函数（省 stdio 往返），协议层的正确性由
  tests/test_mcp.py 用内存双工流真实走一遍 MCP 握手 + 工具调用验证；
- 这保证「MCP 暴露的工具」与「图内调用的工具」永远同源，不会两套实现漂移。
"""
from __future__ import annotations

import logging
import time

from backend.retrieval.precedent_source import search_precedents_any
from backend.retrieval.statutes import search_statutes

logger = logging.getLogger(__name__)

# ── web_search_legal 的联网降级缓存（规格口径：联网失败用 7 天缓存）──

_WEB_CACHE: dict[str, tuple[float, list[dict]]] = {}
_WEB_CACHE_TTL = 7 * 24 * 3600.0

# 预置「联网」检索源：法律动态摘要（Mock 联网，生产接真实搜索/权威站点 API）
_WEB_CORPUS: list[dict] = [
    {
        "title": "最高人民法院发布劳动争议典型案例（新就业形态劳动者权益保障）",
        "source": "最高人民法院官网",
        "date": "2026-04-30",
        "snippet": "明确平台企业与劳务外包用工的连带责任认定口径，强调书面劳动合同订立与社保缴纳义务不得以合作形式规避。",
        "url": "https://www.court.gov.cn/zixun/xiangqing/laodong典型案例.html",
    },
    {
        "title": "新公司法施行后对赌回购纠纷裁判口径更新",
        "source": "人民法院案例库",
        "date": "2026-03-15",
        "summary": "投资方请求目标公司回购股权的，应审查减资程序完成情况；未完成减资的，可支持与股东对赌的替代救济。",
        "snippet": "未完成减资程序的目标公司回购请求不获支持，但可转向股东回购/现金补偿救济。",
        "url": "https://rmfyalk.court.gov.cn/duigu-case-2026",
    },
    {
        "title": "人社部：竞业限制经济补偿最低标准适用口径",
        "source": "人力资源和社会保障部",
        "date": "2026-01-20",
        "snippet": "竞业限制经济补偿按月支付标准不低于劳动者离职前十二个月平均工资的 30%，且不低于当地最低工资标准。",
        "url": "http://www.mohrss.gov.cn/jingyejingzheng-30.html",
    },
]


# ─────────────────────────────────────────────────────────────
# MCP 工具 1：search_law_knowledge_base —— 法条向量检索
# ─────────────────────────────────────────────────────────────


async def search_law_knowledge_base(query: str, top_k: int = 5) -> list[dict]:
    """法条知识库向量检索。MCP 检索失败时降级关键词匹配（调用方兜底）。"""
    try:
        return search_statutes(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        # 降级：关键词直查条文库（宁可用简化匹配也不能让节点挂掉）
        logger.warning("search_law_knowledge_base 向量检索失败，降级关键词匹配：%s", exc)
        return _keyword_fallback_statutes(query, top_k)


def _keyword_fallback_statutes(query: str, top_k: int) -> list[dict]:
    from backend.retrieval.statutes import load_statutes

    kws = [c for c in query if "\u4e00" <= c <= "\u9fff"]
    scored = []
    for art in load_statutes():
        s = sum(1 for kw in kws if kw in art.text)
        if s:
            scored.append((s, art))
    scored.sort(key=lambda x: -x[0])
    return [
        {"law": a.law, "article_no": a.article_no, "cite": a.cite, "text": a.text, "score": 0.0}
        for _, a in scored[:top_k]
    ]


# ─────────────────────────────────────────────────────────────
# MCP 工具 2：search_precedent_cases —— 类案检索
# ─────────────────────────────────────────────────────────────


async def search_precedent_cases(
    query: str, case_type: str | None = None, top_k: int = 3
) -> list[dict]:
    """类案检索（向量相似 + 案由过滤），真实案号可回溯。

    阶段 3：数据源经 `retrieval/precedent_source.py` 抽象 —— 配了远端判例库
    就走远端，失败/空结果回落本地 66 条真实案号判例（每条带 source 标明来源）。
    """
    hits = await search_precedents_any(query, case_type=case_type, top_k=top_k)
    # 把 metadata 拍平后的字段做白名单，保证 MCP 返回契约稳定。
    keys = ("id", "case_no", "court", "year", "case_type", "case_type_label",
            "topic", "result", "law_basis", "score", "source")
    return [{k: h.get(k) for k in keys} for h in hits]


# ─────────────────────────────────────────────────────────────
# MCP 工具 3：web_search_legal —— 联网法律动态检索（7 天缓存降级）
# ─────────────────────────────────────────────────────────────


async def web_search_legal(query: str, top_k: int = 3) -> dict:
    """联网检索法律动态。真实联网失败/未启用时降级：预置语料 + 7 天缓存。"""
    now = time.time()
    cached = _WEB_CACHE.get(query)
    if cached and now - cached[0] < _WEB_CACHE_TTL:
        return {"results": cached[1][:top_k], "from_cache": True, "degraded": True}

    # 生产：httpx 异步抓取权威站点/搜索 API；此处 Mock 语料做语义匹配。
    # 联网通道不可用（无外网 Key）→ 直接走「降级 + 缓存」路径，不报错。
    kws = [q for q in (query, *query.split()) if len(q) >= 2]
    scored = []
    for item in _WEB_CORPUS:
        blob = item["title"] + item["snippet"]
        s = sum(1 for kw in kws if kw in blob)
        if s:
            scored.append((s, item))
    scored.sort(key=lambda x: -x[0])
    results = [dict(item, relevance=s / max(1, len(kws))) for s, item in scored[:top_k]]
    _WEB_CACHE[query] = (now, results)
    return {"results": results, "from_cache": False, "degraded": True}


# ─────────────────────────────────────────────────────────────
# MCP 工具 4：query_legal_standard —— 法律/赔偿标准查询
# ─────────────────────────────────────────────────────────────


_STANDARDS: dict[str, dict] = {
    "n": {
        "name": "经济补偿 N",
        "desc": "每满一年支付一个月工资；六个月以上不满一年按一年；不满六个月支付半个月工资",
        "basis": "《劳动合同法》第四十七条",
    },
    "2n": {
        "name": "违法解除赔偿金 2N",
        "desc": "用人单位违法解除或终止劳动合同，按经济补偿标准的二倍支付赔偿金",
        "basis": "《劳动合同法》第八十七条",
    },
    "n_plus_1": {
        "name": "N+1（代通知金）",
        "desc": "无过失性辞退未提前 30 日书面通知的，额外支付一个月工资",
        "basis": "《劳动合同法》第四十条",
    },
    "non_compete_pay": {
        "name": "竞业限制经济补偿",
        "desc": "按月支付，标准不低于劳动者离职前十二个月平均工资的 30%，且不低于当地最低工资标准",
        "basis": "《劳动合同法》第二十三条、《最高人民法院关于审理劳动争议案件适用法律问题的解释（一）》第三十六条",
    },
    "double_wage": {
        "name": "未签书面合同二倍工资",
        "desc": "用工之日起超过一个月不满一年未订立书面劳动合同的，按月支付二倍工资",
        "basis": "《劳动合同法》第八十二条",
    },
    "trial_limit": {
        "name": "试用期上限",
        "desc": "合同期 <3 个月不得约定试用期；3 个月≤期限<1 年≤1 个月；1 年≤期限<3 年≤2 个月；3 年以上/无固定期限≤6 个月",
        "basis": "《劳动合同法》第十九条",
    },
}


async def query_legal_standard(standard_key: str) -> dict:
    """按标准名查询法定口径（N / 2N / N+1 / 竞业补偿 / 二倍工资 / 试用期上限）。"""
    key = standard_key.strip().lower().replace("-", "_").replace("＋", "_")
    alias = {"n": "n", "2n": "2n", "n+1": "n_plus_1", "n_plus_1": "n_plus_1"}
    key = alias.get(key, key)
    if key in _STANDARDS:
        return {"found": True, **_STANDARDS[key]}
    return {"found": False, "message": "未收录该标准，可用：n / 2n / n_plus_1 / non_compete_pay / double_wage / trial_limit"}

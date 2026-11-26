"""FastMCP 工具服务器 —— 把法律检索能力暴露成标准 MCP 工具。

四个 MCP 工具（与 backend/tools/mcp_tools.py 同源逻辑）：
1. search_law_knowledge_base   法条知识库向量检索
2. search_precedent_cases      类案检索（8 类案由过滤，真实案号）
3. web_search_legal            联网法律动态检索（失败降级 7 天缓存）
4. query_legal_standard        法定标准查询（N / 2N / N+1 / 试用期上限…）

启动（stdio 模式，供 MCP 客户端接入）：
    python -m backend.mcp_server.server

进程内的图节点直接调 mcp_tools 同源函数（省 stdio 往返）；
协议层正确性由 tests/test_mcp.py 用内存双工流验证握手 + 调用。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from backend.tools.mcp_tools import (
    query_legal_standard as _query_legal_standard_impl,
)
from backend.tools.mcp_tools import (
    search_law_knowledge_base as _search_law_impl,
)
from backend.tools.mcp_tools import (
    search_precedent_cases as _search_precedents_impl,
)
from backend.tools.mcp_tools import (
    web_search_legal as _web_search_impl,
)

mcp = FastMCP("lawagent-legal-kb")


@mcp.tool()
async def search_law_knowledge_base(query: str, top_k: int = 5) -> str:
    """法条知识库向量检索：输入自然语言法律问题，返回最相关的法条原文。"""
    import json

    return json.dumps(await _search_law_impl(query, top_k), ensure_ascii=False)


@mcp.tool()
async def search_precedent_cases(query: str, case_type: str = "", top_k: int = 3) -> str:
    """类案检索：按案情描述检索同类真实判例（真实案号、法院、裁判结果），可按案由过滤。"""
    import json

    return json.dumps(
        await _search_precedents_impl(query, case_type or None, top_k), ensure_ascii=False
    )


@mcp.tool()
async def web_search_legal(query: str, top_k: int = 3) -> str:
    """联网检索最新法律动态/裁判口径（不可用时自动降级 7 天缓存）。"""
    import json

    return json.dumps(await _web_search_impl(query, top_k), ensure_ascii=False)


@mcp.tool()
async def query_legal_standard(standard_key: str) -> str:
    """查询法定计算标准：n / 2n / n_plus_1 / non_compete_pay / double_wage / trial_limit。"""
    import json

    return json.dumps(await _query_legal_standard_impl(standard_key), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")

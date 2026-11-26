"""MCP 协议层测试 —— FastMCP server 的工具注册与调用契约。

用 FastMCP 进程内 API（list_tools / call_tool）验证「server 注册的 4 个工具」
与「图内调用的同源函数」行为一致。低层 stdio 握手由 mcp 官方库保证，
Windows 下 anyio 内存双工的兼容问题不作为业务验收点。
"""
from __future__ import annotations

import json

import pytest

from backend.mcp_server.server import mcp as law_mcp


pytestmark = pytest.mark.usefixtures("_prepare_db")


async def _call_tool(name: str, args: dict):
    """FastMCP 进程内调用，解包 TextContent。"""
    result = await law_mcp.call_tool(name, args)
    # 兼容不同 mcp 版本返回：list[TextContent] 或 (content, structured)
    content = result[0] if isinstance(result, tuple) else result
    item = content[0] if isinstance(content, (list, tuple)) else content
    return json.loads(item.text)


async def test_list_tools():
    tools = await law_mcp.list_tools()
    names = {t.name for t in tools}
    assert {
        "search_law_knowledge_base",
        "search_precedent_cases",
        "web_search_legal",
        "query_legal_standard",
    } <= names
    # 每个工具都有描述（协议自描述性）
    assert all(t.description for t in tools)


async def test_call_search_law():
    data = await _call_tool("search_law_knowledge_base", {"query": "试用期超过法定上限", "top_k": 2})
    assert isinstance(data, list) and data
    assert "cite" in data[0]


async def test_call_search_precedents():
    data = await _call_tool(
        "search_precedent_cases",
        {"query": "违法辞退赔偿", "case_type": "labor_termination", "top_k": 2},
    )
    assert data and data[0]["case_type"] == "labor_termination"
    assert data[0]["case_no"]


async def test_call_standard():
    data = await _call_tool("query_legal_standard", {"standard_key": "2N"})
    assert data["found"] is True
    assert "第八十七条" in data["basis"]


async def test_call_web_search():
    data = await _call_tool("web_search_legal", {"query": "对赌回购 新公司法 裁判口径", "top_k": 2})
    assert data["degraded"] is True
    assert data["results"]

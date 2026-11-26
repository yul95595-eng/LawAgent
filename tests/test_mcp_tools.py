"""MCP 工具业务逻辑测试（4 个工具同源函数）。"""
from __future__ import annotations

import pytest

from backend.tools.mcp_tools import (
    query_legal_standard,
    search_law_knowledge_base,
    search_precedent_cases,
    web_search_legal,
)


pytestmark = pytest.mark.usefixtures("_prepare_db")


class TestLawKB:
    async def test_statute_search_hit(self):
        hits = await search_law_knowledge_base("试用期超过法定上限怎么办", top_k=3)
        assert len(hits) <= 3 and hits
        assert all("cite" in h and "text" in h for h in hits)

    async def test_keyword_fallback_on_internal_error(self, monkeypatch):
        import backend.tools.mcp_tools as m

        async def _boom(*a, **k):
            raise RuntimeError("vector store down")

        monkeypatch.setattr(m, "search_statutes", _boom)
        hits = await m.search_law_knowledge_base("试用期工资标准", top_k=2)
        assert hits  # 降级关键词匹配仍然有结果


class TestPrecedents:
    async def test_case_type_filter(self):
        hits = await search_precedent_cases(
            "试用期延长二次约定", case_type="labor_probation", top_k=3
        )
        assert hits
        assert all(h["case_type"] == "labor_probation" for h in hits)
        assert all(h["case_no"] for h in hits)  # 真实案号可回溯

    async def test_flattened_fields(self):
        hits = await search_precedent_cases("违法辞退赔偿 2N", top_k=2)
        for h in hits:
            assert {"id", "case_no", "court", "year", "case_type", "topic",
                    "result", "law_basis", "score"} <= set(h)


class TestWebSearch:
    async def test_degraded_corpus(self):
        r = await web_search_legal("对赌回购 新公司法 裁判口径", top_k=2)
        assert r["degraded"] is True
        assert r["results"]
        assert all("title" in i and "snippet" in i for i in r["results"])

    async def test_cache_ttl(self):
        r1 = await web_search_legal("竞业限制经济补偿标准", top_k=2)
        r2 = await web_search_legal("竞业限制经济补偿标准", top_k=2)
        assert r2["from_cache"] is True  # 7 天缓存降级口径


class TestStandards:
    async def test_2n(self):
        r = await query_legal_standard("2n")
        assert r["found"] is True
        assert "第八十七条" in r["basis"]

    async def test_n_plus_1_alias(self):
        r = await query_legal_standard("N+1")
        assert r["found"] is True
        assert r["name"].startswith("N+1")

    async def test_unknown(self):
        r = await query_legal_standard("xxx")
        assert r["found"] is False

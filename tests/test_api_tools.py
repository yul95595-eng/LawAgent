"""外部 API 工具测试 —— Mock 数据、重试 1 次、友好降级口径。"""
from __future__ import annotations

import pytest

from backend.tools.api_tools import (
    ExternalApiError,
    get_case_detail,
    get_contract_file,
    query_enterprise_credit,
    save_legal_opinion,
    submit_review_result,
)


pytestmark = pytest.mark.usefixtures("_prepare_db")


class TestExternalApi:
    async def test_get_case_demo1(self):
        c = await get_case_detail("CASE-2026-0891")
        assert c["case_id"] == "CASE-2026-0891"

    async def test_get_contract_demo2(self):
        c = await get_contract_file("HT-2026-1122")
        assert "合同" in c.get("contract_title", "") or c.get("contract_text")

    async def test_enterprise_fuzzy(self):
        e = await query_enterprise_credit("华创精密制造股份有限公司")
        assert "华创" in e["enterprise_name"]
        fuzzy = await query_enterprise_credit("华创精密")
        assert fuzzy["enterprise_name"] == e["enterprise_name"]

    async def test_enterprise_not_found_friendly(self):
        with pytest.raises(ExternalApiError) as ei:
            await query_enterprise_credit("不存在的公司xyz")
        assert "请核对名称" in str(ei.value)

    async def test_case_not_found_no_retry(self):
        """业务性无数据不重试，直接友好报错。"""
        with pytest.raises(ExternalApiError):
            await get_case_detail("CASE-NOPE")

    async def test_submit_receipt(self):
        r = await submit_review_result("rv_test", {"risk_level": "high"})
        assert r["accepted"] is True
        assert r["receipt_no"].startswith("RCPT-")

    async def test_save_opinion_archive(self):
        r = await save_legal_opinion({"title": "意见书"})
        assert r["archived"] is True
        assert r["archive_no"].startswith("OP-")

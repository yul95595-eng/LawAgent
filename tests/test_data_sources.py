"""阶段 3 数据接真测试 —— 工商数据适配器（企查查/天眼查/mock）+ 判例数据源。

不打真实外网：适配器的 HTTP 客户端可注入，这里用 httpx.MockTransport 模拟
供应商响应，验证「字段归一化 / 鉴权头 / 错误口径 / 降级回落」这几件事。
"""
from __future__ import annotations

import httpx
import pytest

from backend.core.config import settings
from backend.tools.adapters import get_enterprise_adapter, reset_enterprise_adapter
from backend.tools.adapters.base import ExternalApiError, to_date, to_int
from backend.tools.adapters.mock import MockEnterpriseAdapter

# ── 供应商响应样本（按官方文档口径裁剪，只留用到的字段）──

QCC_OK = {
    "Status": "200",
    "Message": "查询成功",
    "Result": {
        "Name": "华创精密制造股份有限公司",
        "CreditCode": "91330100MA2H3XXXX1",
        "OperName": "陈建国",
        "RegistCapi": "8000万元人民币",
        "StartDate": "2012-06-15",
        "SocialStaffNum": "500",
        "Industry": "通用设备制造业",
        "Scope": "精密机械设备及零部件的研发、制造、加工、销售；货物进出口",
        "Address": "浙江省杭州市经济技术开发区科创大道 88 号",
        "Status": "存续",
    },
}

TYC_OK = {
    "error_code": 0,
    "reason": "ok",
    "result": {
        "name": "云启智能科技（杭州）有限公司",
        "creditCode": "91330106MA2BXXXX2X",
        "legalPersonName": "周敏",
        "regCapital": "1200万人民币",
        "estiblishTime": 1560000000000,
        "staffNumRange": "50-99人",
        "industry": "软件和信息技术服务业",
        "businessScope": "人工智能软件开发；数据处理服务",
        "regLocation": "浙江省杭州市西湖区文三路 100 号",
        "regStatus": "存续",
    },
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestQccAdapter:
    async def test_maps_fields_and_signs_token(self):
        from backend.tools.adapters.qcc import QccAdapter

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["token"] = request.headers.get("Token", "")
            seen["timespan"] = request.headers.get("Timespan", "")
            return httpx.Response(200, json=QCC_OK)

        async with _client(handler) as c:
            adapter = QccAdapter(api_key="ak", secret_key="sk", client=c)
            info = await adapter.fetch("华创精密制造股份有限公司")

        assert "GetBasicDetailsByName" in seen["url"]
        assert "keyword=" in seen["url"]
        # MD5(AppKey+Timespan+SecretKey).upper() 是 32 位十六进制大写
        assert len(seen["token"]) == 32 and seen["token"].isupper()
        assert seen["timespan"].isdigit()

        assert info["enterprise_name"] == "华创精密制造股份有限公司"
        assert info["credit_code"] == "91330100MA2H3XXXX1"
        assert info["legal_representative"] == "陈建国"
        assert info["employee_count"] == 500
        assert info["established"] == "2012-06-15"
        assert "精密机械设备" in info["business_scope"]
        assert info["data_source"] == "qcc"
        assert any("存续" in h for h in info["risk_hints"])

    async def test_business_error_raises(self):
        from backend.tools.adapters.qcc import QccAdapter

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Status": "201", "Message": "查无结果"})

        async with _client(handler) as c:
            adapter = QccAdapter(api_key="ak", secret_key="sk", client=c)
            with pytest.raises(ExternalApiError) as ei:
                await adapter.fetch("不存在公司")
        assert "查无结果" in str(ei.value)

    async def test_auth_failure_raises(self):
        from backend.tools.adapters.qcc import QccAdapter

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={})

        async with _client(handler) as c:
            adapter = QccAdapter(api_key="ak", secret_key="sk", client=c)
            with pytest.raises(ExternalApiError) as ei:
                await adapter.fetch("任何公司")
        assert "鉴权" in str(ei.value)


class TestTianyanchaAdapter:
    async def test_maps_fields(self):
        from backend.tools.adapters.tianyancha import TianyanchaAdapter

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=TYC_OK)

        async with _client(handler) as c:
            adapter = TianyanchaAdapter(api_key="tyc-token", client=c)
            info = await adapter.fetch("云启智能科技（杭州）有限公司")

        assert "baseinfo/2.0" in seen["url"]
        assert seen["auth"] == "tyc-token"
        assert info["enterprise_name"] == "云启智能科技（杭州）有限公司"
        assert info["legal_representative"] == "周敏"
        assert info["employee_count"] == 50  # "50-99人" 取下限
        assert info["data_source"] == "tianyancha"
        assert info["established"].startswith("2019-")

    async def test_error_code_raises(self):
        from backend.tools.adapters.tianyancha import TianyanchaAdapter

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error_code": 300002, "reason": "无数据"})

        async with _client(handler) as c:
            adapter = TianyanchaAdapter(api_key="t", client=c)
            with pytest.raises(ExternalApiError) as ei:
                await adapter.fetch("不存在公司")
        assert "无数据" in str(ei.value)


class TestFactory:
    def test_missing_key_falls_back_to_mock(self, monkeypatch):
        monkeypatch.setattr(settings, "enterprise_data_source", "qcc", raising=False)
        monkeypatch.setattr(settings, "qcc_api_key", "", raising=False)
        monkeypatch.setattr(settings, "qcc_secret_key", "", raising=False)
        reset_enterprise_adapter()
        try:
            assert isinstance(get_enterprise_adapter(), MockEnterpriseAdapter)
        finally:
            reset_enterprise_adapter()

    def test_configured_source_builds_real_adapter(self, monkeypatch):
        monkeypatch.setattr(settings, "enterprise_data_source", "qcc", raising=False)
        monkeypatch.setattr(settings, "qcc_api_key", "ak", raising=False)
        monkeypatch.setattr(settings, "qcc_secret_key", "sk", raising=False)
        reset_enterprise_adapter()
        try:
            assert get_enterprise_adapter().name == "qcc"
        finally:
            reset_enterprise_adapter()

    def test_default_is_mock(self):
        reset_enterprise_adapter()
        assert get_enterprise_adapter().name == "mock"


class TestEnterpriseToolIntegration:
    async def test_real_adapter_failure_surfaces_as_friendly_error(self, monkeypatch):
        """真实源调用失败时不能伪造数据：抛 ExternalApiError 让 Agent 走失败提示。"""
        from backend.tools import api_tools
        from backend.tools.adapters.qcc import QccAdapter

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        monkeypatch.setattr(api_tools, "get_enterprise_adapter", lambda: QccAdapter(
            api_key="ak", secret_key="sk", client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler)),
        ))
        with pytest.raises(ExternalApiError):
            await api_tools.query_enterprise_credit("华创精密制造股份有限公司")

    async def test_mock_path_still_masked(self):
        from backend.tools.api_tools import query_enterprise_credit

        reset_enterprise_adapter()
        info = await query_enterprise_credit("华创精密制造股份有限公司")
        assert info["data_source"] == "mock"
        assert "MA2H3XX" not in info["credit_code"]  # 脱敏链路仍然生效


class TestPrecedentSource:
    async def test_remote_results_normalized(self, monkeypatch):
        from backend.retrieval import precedent_source as ps

        monkeypatch.setattr(settings, "precedent_data_source", "api", raising=False)
        monkeypatch.setattr(settings, "precedent_api_url", "http://precedent.local/search", raising=False)
        monkeypatch.setattr(settings, "precedent_api_key", "k", raising=False)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [
                {"caseNo": "（2023）浙01民终1234号", "court": "杭州中院", "year": 2023,
                 "caseType": "labor_probation", "title": "试用期二次约定无效",
                 "judgement": "支持劳动者", "laws": "《劳动合同法》第十九条", "score": 0.88},
                {"court": "无案号应被丢弃"},  # 缺 case_no → 过滤
            ]})

        async with _client(handler) as c:
            hits = await ps.search_precedents_any("试用期", case_type="labor_probation", top_k=3, client=c)

        assert len(hits) == 1
        assert hits[0]["case_no"] == "（2023）浙01民终1234号"
        assert hits[0]["case_type_label"] == "试用期纠纷"
        assert hits[0]["source"] == "api"

    async def test_remote_failure_falls_back_to_local(self, monkeypatch):
        from backend.retrieval import precedent_source as ps

        monkeypatch.setattr(settings, "precedent_data_source", "api", raising=False)
        monkeypatch.setattr(settings, "precedent_api_url", "http://precedent.local/search", raising=False)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async with _client(handler) as c:
            hits = await ps.search_precedents_any("违法辞退赔偿", top_k=2, client=c)

        assert hits and all(h["source"] == "local" for h in hits)
        assert all(h["case_no"] for h in hits)

    async def test_local_mode_never_calls_network(self, monkeypatch):
        from backend.retrieval import precedent_source as ps

        monkeypatch.setattr(settings, "precedent_data_source", "local", raising=False)

        def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("local 模式不应发起网络请求")

        async with _client(handler) as c:
            hits = await ps.search_precedents_any("试用期", top_k=2, client=c)
        assert hits


class TestHelpers:
    def test_to_date_variants(self):
        assert to_date("2012-06-15 00:00:00") == "2012-06-15"
        assert to_date("2012/06/15") == "2012-06-15"
        assert to_date("20120615") == "2012-06-15"
        assert to_date(1560000000000).startswith("2019-")
        assert to_date("") == ""

    def test_to_int_variants(self):
        assert to_int("500") == 500
        assert to_int("500人") == 500
        assert to_int("50-99人") == 50
        assert to_int(None) == 0
        assert to_int(True) == 0

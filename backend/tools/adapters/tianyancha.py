"""天眼查开放平台适配器（ENTERPRISE_DATA_SOURCE=tianyancha）。

接口：`GET {base}/services/open/ic/baseinfo/2.0?keyword={企业名}`
鉴权：头部 `Authorization: {token}`（开放平台申请到的 token 原样带上）。
返回：`{"error_code": 0, "reason": "ok", "result": {...}}`。

同样用候选字段名兜底，供应商版本差异不至于整段失效；`estiblishTime`
是天眼查的拼写（官方就是这么拼的），别当错别字改掉。
"""
from __future__ import annotations

import logging

import httpx

from backend.tools.adapters.base import (
    EnterpriseAdapter,
    ExternalApiError,
    as_list,
    first,
    to_date,
    to_int,
)

logger = logging.getLogger(__name__)

_PATH_BASIC = "/services/open/ic/baseinfo/2.0"


class TianyanchaAdapter(EnterpriseAdapter):
    name = "tianyancha"

    def __init__(self, api_key: str, base_url: str = "https://open.api.tianyancha.com",
                 timeout: float = 5.0, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def _get(self, path: str, params: dict) -> dict:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.get(f"{self._base_url}{path}", params=params,
                                    headers={"Authorization": self._api_key})
        except httpx.HTTPError as exc:
            raise ExternalApiError(f"天眼查接口请求失败：{exc}") from exc
        finally:
            if owned:
                await client.aclose()

        if resp.status_code in (401, 403):
            raise ExternalApiError("天眼查接口鉴权失败，请检查 token 或套餐余量")
        if resp.status_code == 429:
            raise ExternalApiError("天眼查接口调用频率超限，请稍后重试")
        if resp.status_code >= 400:
            raise ExternalApiError(f"天眼查接口返回 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ExternalApiError("天眼查接口返回非 JSON 内容") from exc

        code = data.get("error_code", 0)
        if code not in (0, "0", None):
            raise ExternalApiError(f"天眼查接口业务失败：{data.get('reason') or code}")
        return data

    async def fetch(self, enterprise_name: str) -> dict:
        data = await self._get(_PATH_BASIC, {"keyword": enterprise_name})
        raw = data.get("result") or {}
        if not raw:
            raise ExternalApiError(f"天眼查未查到企业「{enterprise_name}」，请核对全称")

        status_text = str(first(raw, "regStatus", "status", default=""))
        risk_hints: list[str] = []
        if status_text:
            risk_hints.append(f"登记状态：{status_text}")
            if any(k in status_text for k in ("注销", "吊销", "清算")):
                risk_hints.append("企业登记状态异常，签约前需核实主体资格")

        # staffNumRange 形如 "100-499人"，取第一个数字作为规模下限
        staff = first(raw, "staffNumRange", "socialStaffNum", "staffNum", default="")
        return {
            "enterprise_name": first(raw, "name", "companyName", default=enterprise_name),
            "credit_code": first(raw, "creditCode", "taxNumber", "regNumber"),
            "legal_representative": first(raw, "legalPersonName", "legalPerson"),
            "registered_capital": first(raw, "regCapital", "registCapi"),
            "established": to_date(first(raw, "estiblishTime", "estiblish", "fromTime")),
            "employee_count": to_int(staff),
            "industry": first(raw, "industry", "industryCode"),
            "business_scope": first(raw, "businessScope", "scope"),
            "licenses": as_list(first(raw, "licenses", default=[])),
            "registration_address": first(raw, "regLocation", "regAddress", "address"),
            "risk_hints": risk_hints,
            "data_source": "tianyancha",
        }

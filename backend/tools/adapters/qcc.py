"""企查查开放平台适配器（ENTERPRISE_DATA_SOURCE=qcc）。

接口：`GET {base}/ECIV4/GetBasicDetailsByName?key={AppKey}&keyword={企业名}`
鉴权：头部 `Token = MD5(AppKey + Timespan + SecretKey).upper()`，`Timespan` 为秒级时间戳。
文档口径见企查查开放平台「企业工商信息详版（ECIV4）」。

真实响应字段各家版本略有差异，这里用候选字段名兜底（取第一个非空），
并在 `_RAW_KEYS` 里保留了常见别名，供应商改版时不至于整段失效。

注意：经营异常/行政处罚/司法风险属于另外的增值接口，基础版拿不到 ——
所以 `risk_hints` 只给「登记状态」这类基础事实，不编造风险结论。
"""
from __future__ import annotations

import hashlib
import logging
import time

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

_PATH_BASIC = "/ECIV4/GetBasicDetailsByName"


class QccAdapter(EnterpriseAdapter):
    name = "qcc"

    def __init__(self, api_key: str, secret_key: str, base_url: str = "https://api.qichacha.com",
                 timeout: float = 5.0, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    # ── HTTP ──
    def _auth_headers(self) -> dict:
        ts = str(int(time.time()))
        token = hashlib.md5(f"{self._api_key}{ts}{self._secret_key}".encode()).hexdigest().upper()
        return {"Token": token, "Timespan": ts}

    async def _get(self, path: str, params: dict) -> dict:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.get(f"{self._base_url}{path}", params=params,
                                    headers=self._auth_headers())
        except httpx.HTTPError as exc:
            raise ExternalApiError(f"企查查接口请求失败：{exc}") from exc
        finally:
            if owned:
                await client.aclose()

        if resp.status_code in (401, 403):
            raise ExternalApiError("企查查接口鉴权失败，请检查 AppKey/SecretKey 或套餐余量")
        if resp.status_code == 429:
            raise ExternalApiError("企查查接口调用频率超限，请稍后重试")
        if resp.status_code >= 400:
            raise ExternalApiError(f"企查查接口返回 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ExternalApiError("企查查接口返回非 JSON 内容") from exc

        status = str(data.get("Status", data.get("status", "")))
        if status and status != "200":
            raise ExternalApiError(f"企查查接口业务失败：{data.get('Message') or status}")
        return data

    # ── 对外 ──
    async def fetch(self, enterprise_name: str) -> dict:
        data = await self._get(_PATH_BASIC, {"key": self._api_key, "keyword": enterprise_name})
        raw = data.get("Result") or data.get("result") or {}
        if not raw:
            raise ExternalApiError(f"企查查未查到企业「{enterprise_name}」，请核对全称")

        status_text = str(first(raw, "Status", "RegistStatus", "regStatus", default=""))
        risk_hints: list[str] = []
        if status_text:
            risk_hints.append(f"登记状态：{status_text}")
            if any(k in status_text for k in ("注销", "吊销", "清算")):
                risk_hints.append("企业登记状态异常，签约前需核实主体资格")

        return {
            "enterprise_name": first(raw, "Name", "name", default=enterprise_name),
            "credit_code": first(raw, "CreditCode", "creditCode", "No"),
            "legal_representative": first(raw, "OperName", "operName", "LegalPerson"),
            "registered_capital": first(raw, "RegistCapi", "registCapi", "RegCapital"),
            "established": to_date(first(raw, "StartDate", "startDate", "EstiblishTime")),
            "employee_count": to_int(first(raw, "SocialStaffNum", "PersonScope", "staffNum", default=0)),
            "industry": first(raw, "Industry", "industry", "IndustryCode"),
            "business_scope": first(raw, "Scope", "scope", "BusinessScope"),
            "licenses": as_list(first(raw, "Licenses", "licenses", default=[])),
            "registration_address": first(raw, "Address", "address", "RegLocation"),
            "risk_hints": risk_hints,
            "data_source": "qcc",
        }

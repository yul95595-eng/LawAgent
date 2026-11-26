"""工商（企业征信）数据源适配器层 —— 阶段 3「数据接真」。

为什么单独抽一层：
- 演示态读 `data/mock/enterprises.json`，生产态要接企查查/天眼查开放平台，
  两者的响应结构完全不同。如果把字段映射写在 `api_tools.py` 里，切换数据源
  就会牵动 Agent 节点和测试；
- 适配器只负责「取数 + 归一化」，对上层永远吐同一套字段（见 `FIELD_SPEC`），
  换供应商只加一个适配器文件，`compliance_agent.py` 一行不用改。

归一化契约（下游唯一认这套，新增字段只能加不能改名）：
    enterprise_name / credit_code / legal_representative / registered_capital
    established / employee_count / industry / business_scope / licenses
    registration_address / risk_hints / data_source

`data_source` 会随档案一起下传并进合规报告 —— 生产环境必须能让用户看清
「这份工商信息来自哪」，不能把 mock 数据包装成真实征信结果。
"""
from __future__ import annotations

from functools import lru_cache

from backend.core.config import settings
from backend.tools.adapters.base import EnterpriseAdapter
from backend.tools.adapters.mock import MockEnterpriseAdapter

FIELD_SPEC = (
    "enterprise_name", "credit_code", "legal_representative", "registered_capital",
    "established", "employee_count", "industry", "business_scope", "licenses",
    "registration_address", "risk_hints", "data_source",
)


def _build() -> EnterpriseAdapter:
    """按配置选数据源；配了源但缺 key 时降级 mock 并在日志里喊一嗓子。

    注意降级只发生在「配置不全」这种启动期问题上，且返回的 data_source
    会标成 mock —— 不会把假数据伪装成真实征信。运行时调用失败则不降级，
    直接抛给 Agent 走「查询失败」友好提示（法务场景宁可空着也不能编）。
    """
    source = (settings.enterprise_data_source or "mock").strip().lower()

    if source == "qcc":
        if settings.qcc_api_key and settings.qcc_secret_key:
            from backend.tools.adapters.qcc import QccAdapter

            return QccAdapter(
                api_key=settings.qcc_api_key,
                secret_key=settings.qcc_secret_key,
                base_url=settings.qcc_base_url,
                timeout=settings.enterprise_api_timeout,
            )
        _warn_missing("qcc", "QCC_API_KEY / QCC_SECRET_KEY")

    elif source in ("tianyancha", "tyc"):
        if settings.tyc_api_key:
            from backend.tools.adapters.tianyancha import TianyanchaAdapter

            return TianyanchaAdapter(
                api_key=settings.tyc_api_key,
                base_url=settings.tyc_base_url,
                timeout=settings.enterprise_api_timeout,
            )
        _warn_missing("tianyancha", "TYC_API_KEY")

    elif source != "mock":
        _warn_missing(source, "ENTERPRISE_DATA_SOURCE 取值（mock/qcc/tianyancha）")

    return MockEnterpriseAdapter()


def _warn_missing(source: str, keys: str) -> None:
    import logging

    logging.getLogger(__name__).warning(
        "工商数据源 %s 未配置齐全（缺 %s），本次降级读本地 mock 数据；"
        "返回结果会标记 data_source=mock", source, keys,
    )


@lru_cache(maxsize=1)
def get_enterprise_adapter() -> EnterpriseAdapter:
    """进程内单例（适配器内含 httpx 客户端配置，重建不划算）。"""
    return _build()


def reset_enterprise_adapter() -> None:
    """测试/热切换用：清掉单例，下次按最新配置重建。"""
    get_enterprise_adapter.cache_clear()

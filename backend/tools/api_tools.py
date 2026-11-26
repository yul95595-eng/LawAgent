"""外部 API 封装工具（5 个）—— 对接甲方 4 大系统的隔离层。

甲方系统（规格文档口径）：
  案件管理系统  → get_case_detail / submit_review_result
  合同管理系统  → get_contract_file
  工商信息系统  → query_enterprise_credit
  法规库系统    → save_legal_opinion

生产形态：httpx 调对方 REST 接口 + 字段映射；本仓库为本地 Mock 实现
（规格文档明确「外部系统对接用 Mock」），但接口签名、重试、降级、
审计语义与真实封装一致 —— 换 baseURL 即切生产。

容错降级（规格文档口径）：失败重试 1 次 → 备用源（本地 Mock 数据）→
返回友好提示（绝不把异常栈抛给用户）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path

from backend.core.config import MOCK_DATA_DIR
from backend.core.masking import mask_mapping
from backend.core.security import new_id
from backend.tools.adapters import get_enterprise_adapter
from backend.tools.adapters.base import ExternalApiError

logger = logging.getLogger(__name__)

# Mock 数据源（生产切 baseURL 后此文件仅作联调夹具）
_ENTERPRISES_PATH = MOCK_DATA_DIR / "enterprises.json"
_CASES_PATH = MOCK_DATA_DIR / "cases.json"
_CONTRACTS_PATH = MOCK_DATA_DIR / "contract_files.json"

_RETRY_ONCE = 1  # 规格口径：外部 API 重试 1 次

# ExternalApiError 历史上定义在本模块，测试和其它模块都从这里 import；
# 阶段 3 挪到 adapters/base.py（适配器层也要抛它），此处保留导出以兼容旧引用。
__all__ = ["ExternalApiError", "get_case_detail", "get_contract_file",
           "submit_review_result", "query_enterprise_credit", "save_legal_opinion"]


@lru_cache(maxsize=1)
def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


async def _with_retry(coro_factory, tool_name: str):
    """重试 1 次的统一封装。仍失败 → ExternalApiError（调用方给友好提示）。"""
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ONCE + 1):
        try:
            return await coro_factory()
        except ExternalApiError:
            raise  # 业务性无数据不重试
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("[%s] 第 %d 次调用失败：%s", tool_name, attempt + 1, exc)
            await asyncio.sleep(0.05)
    logger.error("[%s] 重试耗尽，降级备用源/友好提示：%s", tool_name, last_exc)
    raise ExternalApiError(f"{tool_name} 暂时不可用，请稍后重试或联系系统管理员")


# ─────────────────────────────────────────────────────────────
# 工具 1：get_case_detail —— 案件管理系统
# ─────────────────────────────────────────────────────────────


async def get_case_detail(case_id: str) -> dict:
    """按案件号取案件档案（当事人/案由/承办/关键日期）。"""

    async def _call() -> dict:
        cases = _load(_CASES_PATH).get("cases", [])
        for c in cases:
            if c["case_id"] == case_id:
                return c
        raise ExternalApiError(f"案件 {case_id} 不存在")

    return await _with_retry(_call, "get_case_detail")


# ─────────────────────────────────────────────────────────────
# 工具 2：get_contract_file —— 合同管理系统
# ─────────────────────────────────────────────────────────────


async def get_contract_file(contract_id: str) -> dict:
    """按文件号取合同文本与元信息。"""

    async def _call() -> dict:
        files = _load(_CONTRACTS_PATH).get("contracts", [])
        for c in files:
            if c["contract_id"] == contract_id:
                # 统一别名：图与前端一律用 contract_title / contract_text
                return {
                    **c,
                    "contract_title": c.get("title", ""),
                    "contract_text": c.get("text", ""),
                }
        raise ExternalApiError(f"合同文件 {contract_id} 不存在")

    return await _with_retry(_call, "get_contract_file")


# ─────────────────────────────────────────────────────────────
# 工具 3：submit_review_result —— 回写审查结论到合同管理系统
# ─────────────────────────────────────────────────────────────


async def submit_review_result(review_id: str, result: dict) -> dict:
    """把审查意见回写甲方系统。Mock 实现：确认接收并生成回执号。"""

    async def _call() -> dict:
        # 生产：httpx.post(f"{BASE}/contracts/{review_id}/review-result", json=result)
        receipt = f"RCPT-{new_id('x')[:10].upper()}"
        logger.info("[mock] submit_review_result %s → 回执 %s", review_id, receipt)
        return {"accepted": True, "receipt_no": receipt, "submitted_at": "now"}

    return await _with_retry(_call, "submit_review_result")


# ─────────────────────────────────────────────────────────────
# 工具 4：query_enterprise_credit —— 工商信息系统
# ─────────────────────────────────────────────────────────────


async def query_enterprise_credit(enterprise_name: str) -> dict:
    """按企业名称查工商档案（经营范围/资质/许可/风险提示）。

    阶段 3 数据接真：数据源由适配器层决定（mock / 企查查 / 天眼查，
    见 `backend/tools/adapters/`），本函数只做「重试 + 统一脱敏」，
    换供应商不需要改这里，更不需要改 Agent 节点。

    阶段 2 安全合规：返回前对敏感字段统一脱敏
    （统一社会信用代码/法人证件号/联系电话打码后对外展示与落库）。

    查询失败（未授权/无数据/网络）一律抛 ExternalApiError，由 Agent 节点
    转成「查询失败」友好提示 —— 法务场景宁可空着，也不能拿假数据顶。
    """

    async def _call() -> dict:
        adapter = get_enterprise_adapter()
        try:
            profile = await adapter.fetch(enterprise_name)
        except ExternalApiError:
            raise
        except Exception as exc:  # 适配器内部未预料异常也归一到外部错误
            raise ExternalApiError(f"工商信息查询异常：{exc}") from exc
        return mask_mapping(profile)

    return await _with_retry(_call, "query_enterprise_credit")


# ─────────────────────────────────────────────────────────────
# 工具 5：save_legal_opinion —— 法规库系统（意见书归档）
# ─────────────────────────────────────────────────────────────


async def save_legal_opinion(opinion: dict) -> dict:
    """法律意见书/最终结论归档到法规库系统，返回归档编号。"""

    async def _call() -> dict:
        archive_no = f"OP-{new_id('a')[:10].upper()}"
        logger.info("[mock] save_legal_opinion → 归档号 %s", archive_no)
        return {"archived": True, "archive_no": archive_no}

    return await _with_retry(_call, "save_legal_opinion")

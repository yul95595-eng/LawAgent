"""敏感信息脱敏（阶段 2 安全合规）—— 日志与审计留痕不落敏感原文。

口径：对外展示与落盘留痕统一过 mask 工具（宁可错杀不可漏杀的宽匹配）：
  手机号   138****5678（前 3 后 4）
  身份证   110101********1234（前 6 后 4）
  统一社会信用代码  9133****XX1（前 8 后 3）
  银行卡   6222****1234（前 6 后 4）
应用点：审计日志 detail、企业工商信息对外展示、意见书导出元信息。
"""
from __future__ import annotations

import re

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_CREDIT_CODE = re.compile(r"(?<![0-9A-Z])([0-9A-Z]{8})[0-9A-Z]{7}([0-9A-Z]{3})(?![0-9A-Z])")
_BANK_CARD = re.compile(r"(?<!\d)(\d{6})\d{6,12}(\d{4})(?!\d)")


def mask_phone(s: str) -> str:
    return _PHONE.sub(r"\1****\2", s)


def mask_id_card(s: str) -> str:
    return _ID_CARD.sub(r"\1********\2", s)


def mask_credit_code(s: str) -> str:
    """统一社会信用代码（18 位）前 8 后 3，中间打码。"""
    return _CREDIT_CODE.sub(r"\1*******\2", s)


def mask_bank_card(s: str) -> str:
    return _BANK_CARD.sub(r"\1******\2", s)


def mask_text(s: str) -> str:
    """一段文本里的敏感信息全部打码（审计/日志统一入口）。"""
    if not s:
        return s
    s = mask_phone(s)
    s = mask_id_card(s)
    s = mask_credit_code(s)
    s = mask_bank_card(s)
    return s


def mask_mapping(data: dict) -> dict:
    """dict 顶层字符串值脱敏（工商信息快照等展示场景）。"""
    return {
        k: mask_text(v) if isinstance(v, str) else v
        for k, v in data.items()
    }

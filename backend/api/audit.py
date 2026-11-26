"""审计日志助手 —— 敏感操作全留痕（保存 5 年，合规口径）。

阶段 2 安全合规：detail 落库前统一脱敏（手机号/身份证/信用代码/银行卡
打码），审计留痕不存敏感原文。
"""
from __future__ import annotations

import json
import logging

from backend.core.masking import mask_text
from backend.core.security import new_id
from backend.memory.models import AuditLog, SessionLocal

logger = logging.getLogger(__name__)


async def audit(
    tenant_id: str, user_id: str, role: str,
    action: str, object_type: str, object_id: str = "", detail: dict | None = None,
) -> None:
    """记录一条审计日志。失败只打日志，不阻断业务（审计是旁路）。"""
    try:
        detail_str = mask_text(json.dumps(detail or {}, ensure_ascii=False, default=str))
        async with SessionLocal() as session:
            session.add(AuditLog(
                id=new_id("aud"),
                tenant_id=tenant_id,
                user_id=user_id,
                user_role=role,
                action=action,
                object_type=object_type,
                object_id=object_id,
                detail=detail_str,
            ))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计日志写入失败：%s", exc)

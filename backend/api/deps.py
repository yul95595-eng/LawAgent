"""路由依赖 —— JWT 解析为当前用户上下文（tenant_id / user_id / role）。"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from backend.core.security import decode_token
from backend.memory.models import SessionLocal, User

bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    """从 Authorization: Bearer <jwt> 解析当前用户。所有业务接口强制鉴权。"""
    if cred is None:
        raise HTTPException(status_code=401, detail="未登录或缺少访问令牌")
    payload = decode_token(cred.credentials)
    user_id = payload.get("sub", "")
    async with SessionLocal() as session:
        row = await session.scalar(select(User).where(User.id == user_id))
        if row is None:
            raise HTTPException(status_code=401, detail="用户不存在")
        return {
            "user_id": row.id,
            "user_no": row.user_no,
            "username": row.username,
            "role": row.role,
            "tenant_id": row.tenant_id,
        }


def require_role(*roles: str):
    """角色门禁（如合伙人审核接口只允许 partner/admin）。"""

    async def _checker(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="当前角色无权执行该操作")
        return user

    return _checker

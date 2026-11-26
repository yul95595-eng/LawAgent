"""安全层 —— JWT 双令牌签发/校验 + 工号密码 bcrypt 哈希。

登录口径（规格文档）：工号 + 密码；token 载荷含 tenant_id / user_id / role。
角色四类：lawyer 律师 / partner 合伙人 / legal 法务 / admin 管理员。
多租户隔离：所有业务查询都以 tenant_id + user_id 双层过滤（记忆层强制）。
双令牌（阶段 2 安全合规）：access 12h 业务访问 + refresh 7d 换新，
业务接口只认 access（typ 校验），refresh 令牌泄漏也无法直接调用业务。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from backend.core.config import settings

ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ROLES = ("lawyer", "partner", "legal", "admin")
ROLE_LABELS = {"lawyer": "律师", "partner": "合伙人", "legal": "法务", "admin": "管理员"}

REFRESH_EXPIRE_DAYS = 7


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: str, role: str, tenant_id: str,
                 token_type: str = "access") -> tuple[str, int]:
    """签发 JWT。返回 (token, expires_in 秒)。

    token_type: access（业务访问，默认）/ refresh（换新令牌，7 天）。
    """
    expires_in = (
        settings.jwt_expire_hours * 3600
        if token_type == "access"
        else REFRESH_EXPIRE_DAYS * 86400
    )
    payload = {
        "sub": user_id,
        "role": role,
        "tenant_id": tenant_id,
        "typ": token_type,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.signing_secret, algorithm=ALGORITHM)
    return token, expires_in


def decode_token(token: str, expected_type: str = "access") -> dict:
    """解码并校验令牌类型：业务接口只认 access（get_current_user 口径）。"""
    try:
        payload = jwt.decode(token, settings.signing_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException_401() from exc
    if payload.get("typ", "access") != expected_type:
        raise HTTPException_401()
    return payload


def HTTPException_401():
    from fastapi import HTTPException

    return HTTPException(status_code=401, detail="登录已过期，请重新登录")

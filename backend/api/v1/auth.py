"""auth 路由 —— 工号注册（审核流）/ 登录（双令牌）/ 审核 / 刷新 / 当前用户。

阶段 2 安全合规：
  注册审核流：自助注册只开 lawyer/legal，落库 pending；合伙人/管理员
    在 /auth/pending 审核放行（approve）或拒绝（reject）后才能登录；
    partner/admin 不可自助注册（防自封审核权）。
  双令牌：登录发 access（12h，业务访问）+ refresh（7d，换新）；
    业务接口只认 access（typ 校验在 decode_token），refresh 泄漏
    也无法直接调业务接口。
  限流：同一账号密码错误 5 次/分钟 → 429（登录成功即清零，正常多端登录不受影响），
    另设 30 次/分钟/IP 的登录总闸；注册 5 次/分钟/IP（进程内滑动窗口）。
    按「账号+IP」而不是纯 IP 计数，是因为律所常共用 NAT 出口 IP，
    纯 IP 卡 5 次会把第 6 个同事直接锁在门外。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from backend.api.audit import audit
from backend.api.deps import get_current_user
from backend.core.ratelimit import check_login_allowed, note_login_result, rate_limit_by_ip
from backend.core.security import (
    ROLE_LABELS,
    create_token,
    decode_token,
    hash_password,
    new_id,
    verify_password,
)
from backend.memory.models import SessionLocal, User

router = APIRouter(prefix="/auth", tags=["auth"])

SELF_REGISTER_ROLES = ("lawyer", "legal")  # partner/admin 须后台开通（防自封审核权）


class RegisterRequest(BaseModel):
    user_no: str           # 工号
    username: str          # 姓名
    password: str
    role: str = "lawyer"   # lawyer/legal（自助注册）


class LoginRequest(BaseModel):
    user_no: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


def _user_payload(row: User) -> dict:
    return {
        "user_id": row.id, "user_no": row.user_no, "username": row.username,
        "role": row.role, "role_label": ROLE_LABELS.get(row.role, row.role),
    }


@router.post("/register")
async def register(req: RegisterRequest, request: Request):
    if req.role not in SELF_REGISTER_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"自助注册仅开放 {'/'.join(SELF_REGISTER_ROLES)}；"
                   "合伙人/管理员请联系现有合伙人开通")
    if len(req.password) < 6:
        raise HTTPException(status_code=422, detail="密码至少 6 位")
    rate_limit_by_ip(request, "auth_register")
    async with SessionLocal() as session:
        exists = await session.scalar(select(User.id).where(User.user_no == req.user_no))
        if exists:
            raise HTTPException(status_code=409, detail="该工号已注册")
        user_id = new_id("u")
        session.add(User(
            id=user_id, tenant_id="tenant_firm", user_no=req.user_no,
            username=req.username, password_hash=hash_password(req.password),
            role=req.role, status="pending",
        ))
        await session.commit()
    # 审核流：注册不发 token，待合伙人/管理员放行
    return {"status": "pending",
            "message": "注册成功，待合伙人/管理员审核通过后即可登录"}


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    # IP 总闸（每次尝试计数）+ 该账号近 1 分钟密码错误次数（只读）
    check_login_allowed(request, req.user_no)
    async with SessionLocal() as session:
        row = await session.scalar(select(User).where(User.user_no == req.user_no))
        if row is None or not verify_password(req.password, row.password_hash):
            note_login_result(request, req.user_no, success=False)
            raise HTTPException(status_code=401, detail="工号或密码错误")
        # 密码正确：清空该账号失败计数（待审核也属于「凭证有效」，用户不该被锁）
        note_login_result(request, req.user_no, success=True)
        if row.status == "pending":
            raise HTTPException(status_code=403, detail="账号待审核，请等待合伙人/管理员放行")
        if row.status != "approved":
            raise HTTPException(status_code=403, detail="账号未通过审核，请联系管理员")
        access, expires_in = create_token(row.id, row.role, row.tenant_id)
        refresh, _ = create_token(row.id, row.role, row.tenant_id, token_type="refresh")
        return {"token": access, "refresh_token": refresh, "expires_in": expires_in,
                "user": _user_payload(row)}


@router.post("/refresh")
async def refresh_token(req: RefreshRequest):
    """用 refresh 令牌换新双令牌（access 过期后的无感续期）。"""
    payload = decode_token(req.refresh_token, expected_type="refresh")
    user_id = payload["sub"]
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        if row is None or row.status != "approved":
            raise HTTPException(status_code=401, detail="账号不可用，请重新登录")
        access, expires_in = create_token(row.id, row.role, row.tenant_id)
        new_refresh, _ = create_token(row.id, row.role, row.tenant_id, token_type="refresh")
        return {"token": access, "refresh_token": new_refresh, "expires_in": expires_in,
                "user": _user_payload(row)}


@router.get("/pending")
async def pending_users(user: dict = Depends(get_current_user)):
    """待审核账号列表（合伙人/管理员）。"""
    if user["role"] not in ("partner", "admin"):
        raise HTTPException(status_code=403, detail="仅合伙人/管理员可审核账号")
    async with SessionLocal() as session:
        rows = (await session.scalars(
            select(User).where(User.status == "pending").order_by(User.created_at)
        )).all()
        return {"users": [
            {"user_id": r.id, "user_no": r.user_no, "username": r.username,
             "role": r.role, "role_label": ROLE_LABELS.get(r.role, r.role),
             "created_at": r.created_at}
            for r in rows
        ]}


@router.post("/approve/{user_id}")
async def approve_user(user_id: str, user: dict = Depends(get_current_user)):
    """放行待审核账号（合伙人/管理员）。"""
    if user["role"] not in ("partner", "admin"):
        raise HTTPException(status_code=403, detail="仅合伙人/管理员可审核账号")
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if row.status != "pending":
            raise HTTPException(status_code=409, detail=f"当前状态 {row.status}，无需审核")
        row.status = "approved"
        await session.commit()
        await audit(user["tenant_id"], user["user_id"], user["role"],
                    "user_approve", "user", user_id,
                    {"user_no": row.user_no, "role": row.role})
        return {"status": "approved", "user_no": row.user_no}


@router.post("/reject/{user_id}")
async def reject_user(user_id: str, user: dict = Depends(get_current_user)):
    """拒绝注册申请（合伙人/管理员）。"""
    if user["role"] not in ("partner", "admin"):
        raise HTTPException(status_code=403, detail="仅合伙人/管理员可审核账号")
    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if row.status != "pending":
            raise HTTPException(status_code=409, detail=f"当前状态 {row.status}，无需审核")
        row.status = "rejected"
        await session.commit()
        await audit(user["tenant_id"], user["user_id"], user["role"],
                    "user_reject", "user", user_id,
                    {"user_no": row.user_no, "role": row.role})
        return {"status": "rejected", "user_no": row.user_no}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return {"user": {**user, "role_label": ROLE_LABELS.get(user["role"], user["role"])}}

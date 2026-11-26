"""进程内滑动窗口限流器（阶段 2 安全合规）。

口径：单实例内存实现（零配置可运行）；多实例部署切 Redis（INCR+EXPIRE
同款窗口算法，接口签名不变）。

策略表（`POLICIES`）：
  auth_ip       30 次/分钟/IP   —— 登录接口总闸，防撞库/扫号（律所常共用出口 IP，
                                   所以这道闸放得比较宽，真正拦爆破的是下一道）
  auth_account   5 次/分钟/账号 —— 只统计「密码错误」，第 6 次直接 429；
                                   登录成功即清零，正常用户不会因为多端登录被锁
  auth_register  5 次/分钟/IP   —— 注册防刷号
  chat          10 次/分钟/用户 —— 对话防滥用

为什么不按 IP 直接卡 5 次：律所/公司常共用一个 NAT 出口，按 IP 卡死会让
第 6 个同事直接登不上（真实的可用性事故）。密码错误按「账号+IP」计，
既能拦住对单个工号的定向爆破，又不会误伤同网段其他人。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from backend.core.config import settings

_buckets: dict[str, deque[float]] = defaultdict(deque)

# 策略表：scope → (limit, window_seconds)。阈值可由环境变量调（压测/大促要放宽），
# limit <= 0 表示该维度不限流。
POLICIES: dict[str, tuple[int, int]] = {
    "auth_ip": (settings.auth_ip_max_per_min, 60),           # 登录总闸（所有尝试都计数）
    "auth_account": (settings.login_max_failures_per_min, 60),  # 单账号密码错误次数
    "auth_register": (5, 60),                                # 注册
    "chat": (settings.chat_max_per_min, 60),                 # 对话
}

_TOO_MANY = "请求过于频繁，请稍后再试"


def rate_limit(key: str, limit: int, window_seconds: int) -> None:
    """滑动窗口检查并计数：超限抛 429（limit<=0 表示不限流）。"""
    if limit <= 0:
        return
    now = time.monotonic()
    q = _buckets[key]
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status_code=429, detail=_TOO_MANY)
    q.append(now)
    _gc()


def _ip(request: Request) -> str:
    """取客户端 IP。

    只有在 TRUST_PROXY=1（前面确实是自家反代）时才读 X-Forwarded-For，
    否则任何人都能自己伪造该头，一次换一个 IP 把限流绕干净。
    取 XFF 第一段（最靠近客户端的地址），这是 nginx 反代的常规口径。
    """
    if settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip", "")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else "unknown"


def rate_limit_by_ip(request: Request, scope: str = "auth_register") -> None:
    """按客户端 IP 限流。"""
    limit, window = POLICIES[scope]
    rate_limit(f"{scope}:{_ip(request)}", limit, window)


def rate_limit_by_user(user_id: str, scope: str = "chat") -> None:
    """按用户限流（对话等业务接口防滥用）。"""
    limit, window = POLICIES[scope]
    rate_limit(f"{scope}:{user_id}", limit, window)


# ── 登录专项：只在「密码错误」时计数，成功即清零 ──────────────────────


def _account_key(request: Request, user_no: str) -> str:
    return f"auth_account:{_ip(request)}:{user_no.strip().lower()}"


def check_login_allowed(request: Request, user_no: str) -> None:
    """登录前置检查：先过 IP 总闸（计数），再看该账号近 1 分钟失败次数。

    注意 IP 总闸在这里就计数（每次尝试都算），账号失败次数在这里只读不写，
    由 note_login_result 在密码校验之后写入 —— 这样「输对密码」不会消耗额度。
    """
    rate_limit_by_ip(request, "auth_ip")
    limit, window = POLICIES["auth_account"]
    q = _buckets.get(_account_key(request, user_no))
    if not q:
        return
    now = time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(
            status_code=429,
            detail="密码错误次数过多，请 1 分钟后再试或联系管理员重置",
        )


def note_login_result(request: Request, user_no: str, success: bool) -> None:
    """登记登录结果：失败入桶，成功清空该账号桶。"""
    key = _account_key(request, user_no)
    if success:
        _buckets.pop(key, None)
        return
    _buckets[key].append(time.monotonic())
    _gc()


def _gc() -> None:
    """惰性清理：桶数过多时回收空桶，防内存缓慢膨胀。"""
    if len(_buckets) > 5000:
        for k in [k for k, v in _buckets.items() if not v]:
            _buckets.pop(k, None)


def reset() -> None:
    """清空所有限流桶（测试隔离用）。"""
    _buckets.clear()

"""阶段 2 安全合规测试 —— 双令牌刷新 / 令牌类型门禁 / 限流 / 敏感信息脱敏。"""
from __future__ import annotations

import httpx
import pytest

from backend.core.masking import (
    mask_bank_card,
    mask_credit_code,
    mask_id_card,
    mask_phone,
    mask_text,
)
from backend.core.ratelimit import rate_limit
from backend.core.security import create_token, decode_token

pytestmark = pytest.mark.usefixtures("_prepare_db")


async def _make_client() -> httpx.AsyncClient:
    from backend.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _login(client: httpx.AsyncClient, user_no: str, password: str = "law123456") -> dict:
    r = await client.post("/api/v1/auth/login", json={"user_no": user_no, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


class TestRefreshToken:
    async def test_refresh_flow(self):
        async with await _make_client() as client:
            body = await _login(client, "L1001")
            r = await client.post("/api/v1/auth/refresh",
                                  json={"refresh_token": body["refresh_token"]})
            assert r.status_code == 200
            new = r.json()
            assert new["token"] and new["refresh_token"]
            assert new["user"]["user_no"] == "L1001"
            # 新 access 可用
            r = await client.get("/api/v1/auth/me",
                                 headers={"Authorization": f"Bearer {new['token']}"})
            assert r.status_code == 200

    async def test_refresh_token_cannot_call_business_api(self):
        """令牌类型门禁：refresh 令牌泄漏也无法直接调业务接口。"""
        async with await _make_client() as client:
            body = await _login(client, "L1001")
            r = await client.get("/api/v1/chat/threads",
                                 headers={"Authorization": f"Bearer {body['refresh_token']}"})
            assert r.status_code == 401

    async def test_access_token_cannot_refresh(self):
        """access 令牌不能当 refresh 用（防止刷新链被业务令牌污染）。"""
        async with await _make_client() as client:
            body = await _login(client, "L1001")
            r = await client.post("/api/v1/auth/refresh",
                                  json={"refresh_token": body["token"]})
            assert r.status_code == 401

    async def test_token_type_payload(self):
        """单元：双令牌 typ 载荷与有效期差异。"""
        access, acc_exp = create_token("u", "lawyer", "t")
        refresh, ref_exp = create_token("u", "lawyer", "t", token_type="refresh")
        assert decode_token(access)["typ"] == "access"
        assert decode_token(refresh, expected_type="refresh")["typ"] == "refresh"
        assert ref_exp > acc_exp  # refresh 有效期更长


class TestRateLimit:
    async def test_login_rate_limited_429(self):
        """登录限流：同账号同 IP 第 6 次密码错误起 429（防工号密码爆破）。"""
        async with await _make_client() as client:
            codes = []
            for _ in range(6):
                r = await client.post("/api/v1/auth/login",
                                      json={"user_no": "L1001", "password": "badbadbad"})
                codes.append(r.status_code)
            assert codes[:5] == [401] * 5   # 前 5 次正常拒绝
            assert codes[5] == 429          # 第 6 次限流

    async def test_successful_logins_never_throttled(self):
        """限流只统计密码错误：连续 8 次正确登录全部 200（多端登录不误伤）。"""
        async with await _make_client() as client:
            for _ in range(8):
                r = await client.post("/api/v1/auth/login",
                                      json={"user_no": "L1001", "password": "law123456"})
                assert r.status_code == 200, r.text

    async def test_failures_do_not_lock_other_accounts(self):
        """同 IP 下某账号被爆破，不能连累其他人登录（律所共用出口 IP 场景）。"""
        async with await _make_client() as client:
            for _ in range(5):
                r = await client.post("/api/v1/auth/login",
                                      json={"user_no": "L1001", "password": "badbadbad"})
                assert r.status_code == 401
            r = await client.post("/api/v1/auth/login",
                                  json={"user_no": "P2001", "password": "law123456"})
            assert r.status_code == 200, r.text

    async def test_unit_sliding_window(self):
        """单元：滑动窗口计数与超限。"""
        for _ in range(3):
            rate_limit("ut:1", limit=3, window_seconds=60)
        with pytest.raises(Exception) as ei:
            rate_limit("ut:1", limit=3, window_seconds=60)
        assert "429" in str(getattr(ei.value, "status_code", "")) or \
            getattr(ei.value, "status_code", None) == 429


class TestMasking:
    def test_mask_phone(self):
        assert mask_phone("联系我 13812345678 谢谢") == "联系我 138****5678 谢谢"

    def test_mask_id_card(self):
        assert mask_id_card("身份证 110101199003071234") == "身份证 110101********1234"

    def test_mask_credit_code(self):
        out = mask_credit_code("信用代码 91330100MA2H3XXXX1")
        assert "91330100" in out and "MA2H3XX" not in out

    def test_mask_bank_card(self):
        assert mask_bank_card("卡号 6222021234567890123") == "卡号 622202******0123"

    def test_mask_text_combined(self):
        s = "手机13812345678，证号110101199003071234"
        out = mask_text(s)
        assert "13812345678" not in out and "199003071234" not in out

    async def test_enterprise_info_masked(self):
        """工商档案对外返回前统一脱敏（统一社会信用代码打码）。"""
        from backend.tools.api_tools import query_enterprise_credit

        info = await query_enterprise_credit("华创精密制造股份有限公司")
        code = info.get("credit_code", "")
        assert code and "MA2H3XX" not in code, f"信用代码未脱敏：{code}"


class TestProxyIp:
    """反代场景的客户端 IP 解析。

    docker-compose 里前端 nginx 反代后端，所有请求的 client.host 都是 nginx 容器 IP，
    不采信 X-Forwarded-For 的话限流会退化成「全所共用一个桶」。
    """

    @staticmethod
    def _request(xff: str | None = None, real_ip: str | None = None):
        from starlette.requests import Request

        headers = []
        if xff is not None:
            headers.append((b"x-forwarded-for", xff.encode()))
        if real_ip is not None:
            headers.append((b"x-real-ip", real_ip.encode()))
        scope = {"type": "http", "method": "POST", "path": "/", "headers": headers,
                 "client": ("10.1.1.1", 1234)}
        return Request(scope)

    def test_forwarded_header_ignored_by_default(self, monkeypatch):
        """默认不信任 XFF：否则任何人换个头就能把限流绕干净。"""
        from backend.core import ratelimit

        monkeypatch.setattr(ratelimit.settings, "trust_proxy", False)
        assert ratelimit._ip(self._request(xff="1.2.3.4, 10.0.0.1")) == "10.1.1.1"

    def test_forwarded_header_used_when_trusted(self, monkeypatch):
        """TRUST_PROXY=1 时取 XFF 第一段（最靠近客户端的地址）。"""
        from backend.core import ratelimit

        monkeypatch.setattr(ratelimit.settings, "trust_proxy", True)
        assert ratelimit._ip(self._request(xff="1.2.3.4, 172.18.0.5")) == "1.2.3.4"
        assert ratelimit._ip(self._request(real_ip="5.6.7.8")) == "5.6.7.8"
        assert ratelimit._ip(self._request()) == "10.1.1.1"


class TestSigningSecret:
    """JWT 密钥不允许有可预测默认值（源码里写死默认密钥 = 谁都能自签令牌）。"""

    def test_prod_without_secret_refuses_to_start(self):
        from backend.core.config import Settings

        s = Settings(_env_file=None, app_env="prod", jwt_secret="")
        with pytest.raises(RuntimeError):
            _ = s.signing_secret

    def test_dev_without_secret_generates_ephemeral(self, monkeypatch):
        from backend.core import config as cfg

        monkeypatch.setattr(cfg, "_EPHEMERAL_SECRET", None)
        s = cfg.Settings(_env_file=None, app_env="dev", jwt_secret="")
        first = s.signing_secret
        assert len(first) >= 32
        # 同一进程内复用，否则 token 会话会莫名其妙失效
        assert s.signing_secret == first

    def test_configured_secret_is_used(self):
        from backend.core.config import Settings

        s = Settings(_env_file=None, app_env="prod", jwt_secret="x" * 48)
        assert s.signing_secret == "x" * 48

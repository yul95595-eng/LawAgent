"""阶段 4 运维测试 —— Prometheus 指标端点 / 数据库备份脚本。"""
from __future__ import annotations

import gzip
import sqlite3

import httpx
import pytest

from backend.core import metrics
from backend.core.config import settings


@pytest.fixture(autouse=True)
def _fresh_metrics():
    metrics.reset()
    yield
    metrics.reset()


async def _make_client() -> httpx.AsyncClient:
    from backend.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestMetricsRegistry:
    def test_counter_and_histogram_render(self):
        metrics.inc("lawagent_demo_total", {"kind": "a"}, 2, help_text="演示计数器")
        metrics.observe("lawagent_demo_seconds", 0.03, {"kind": "a"}, help_text="演示直方图")
        out = metrics.render()
        assert "# TYPE lawagent_demo_total counter" in out
        assert 'lawagent_demo_total{kind="a"} 2' in out
        assert "# TYPE lawagent_demo_seconds histogram" in out
        assert "lawagent_demo_seconds_count" in out
        assert 'le="+Inf"' in out

    def test_label_values_escaped(self):
        metrics.inc("lawagent_esc_total", {"path": '/a"b\\c'})
        out = metrics.render()
        assert '\\"b\\\\c' in out

    def test_gauge_set_and_add(self):
        metrics.set_gauge("lawagent_demo_gauge", 5, help_text="g")
        metrics.add_gauge("lawagent_demo_gauge", 2)
        assert 'lawagent_demo_gauge 7' in metrics.render()


class TestMetricsEndpoint:
    async def test_exposes_http_metrics_and_skips_self(self):
        async with await _make_client() as client:
            await client.get("/api/health")
            r = await client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        body = r.text
        assert 'lawagent_http_requests_total{method="GET",path="/api/health",status="200"}' in body
        assert 'path="/metrics"' not in body  # 抓取自身不计数

    async def test_route_template_not_raw_url(self):
        """路由标签必须是模板：/reviews/{review_id}/export，不能是真实 id。"""
        async with await _make_client() as client:
            body = (await _login(client, "L1001"))["token"]
            await client.get("/api/v1/reviews/rev_does_not_exist/export",
                             headers={"Authorization": f"Bearer {body}"})
            text = (await client.get("/metrics")).text
        assert 'path="/api/v1/reviews/{review_id}/export"' in text
        assert "rev_does_not_exist" not in text

    async def test_token_protection(self, monkeypatch):
        monkeypatch.setattr(settings, "metrics_token", "s3cr3t", raising=False)
        async with await _make_client() as client:
            r = await client.get("/metrics")
            assert r.status_code == 401
            r = await client.get("/metrics", headers={"Authorization": "Bearer s3cr3t"})
            assert r.status_code == 200


async def _login(client: httpx.AsyncClient, user_no: str, password: str = "law123456") -> dict:
    r = await client.post("/api/v1/auth/login", json={"user_no": user_no, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


class TestBackupScript:
    def test_sqlite_backup_creates_gzip_copy(self, tmp_path):
        from scripts.backup_db import backup_sqlite_file

        src = tmp_path / "app.db"
        conn = sqlite3.connect(src)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('案情')")
        conn.commit()
        conn.close()

        out = backup_sqlite_file(src, tmp_path / "bak")

        assert out.exists() and out.suffix == ".gz"
        with gzip.open(out, "rb") as fh:
            restored = tmp_path / "restored.db"
            restored.write_bytes(fh.read())
        conn = sqlite3.connect(restored)
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "案情"
        conn.close()

    def test_prune_keeps_recent(self, tmp_path):
        import os
        import time

        from scripts.backup_db import prune_old

        old = tmp_path / "lawagent-20260101-000000.db.gz"
        new = tmp_path / "lawagent-20260102-000000.db.gz"
        for p in (old, new):
            p.write_bytes(b"x")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old, (ten_days_ago, ten_days_ago))

        removed = prune_old(tmp_path, keep_days=7)

        assert removed == [old]
        assert new.exists()

    def test_prune_disabled_when_keep_days_zero(self, tmp_path):
        """keep_days<=0 不清理：清理在备份之后跑，不能把刚做的备份删了。"""
        from scripts.backup_db import prune_old

        f = tmp_path / "lawagent-20260101-000000.db.gz"
        f.write_bytes(b"x")
        assert prune_old(tmp_path, keep_days=0) == []
        assert f.exists()

    def test_missing_file_returns_none(self, tmp_path):
        from scripts.backup_db import backup_sqlite_file

        assert backup_sqlite_file(tmp_path / "nope.db", tmp_path / "bak") is None


class TestEnsureColumnDialect:
    """轻量加列必须按方言取列名：PG 下用 PRAGMA 会直接语法错误（容器实跑踩过）。"""

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _FakeConn:
        def __init__(self, dialect_name, rows):
            self.dialect = type("D", (), {"name": dialect_name})()
            self._rows = rows
            self.sqls: list[str] = []

        async def exec_driver_sql(self, sql):
            self.sqls.append(sql)
            return TestEnsureColumnDialect._FakeResult(self._rows)

    async def test_postgres_uses_information_schema(self):
        from backend.memory.models import _ensure_column

        conn = self._FakeConn("postgresql", [("status",)])
        await _ensure_column(conn, "users", "status", "VARCHAR DEFAULT 'approved'")
        assert "information_schema.columns" in conn.sqls[0]
        assert "PRAGMA" not in conn.sqls[0].upper()
        assert len(conn.sqls) == 1          # 列已存在，不应再 ALTER

    async def test_sqlite_uses_pragma_and_adds_missing(self):
        from backend.memory.models import _ensure_column

        # PRAGMA table_info 的返回是 (cid, name, type, ...)，列名在下标 1
        conn = self._FakeConn("sqlite", [(0, "id", "TEXT")])
        await _ensure_column(conn, "users", "status", "VARCHAR DEFAULT 'approved'")
        assert "PRAGMA table_info(users)" in conn.sqls[0]
        assert "ALTER TABLE users ADD COLUMN status" in conn.sqls[1]

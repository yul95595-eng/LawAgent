"""pytest 全局夹具。

关键约定（必须在导入 backend 之前设置环境变量）：
- USE_SQLITE=1 + SQLITE_TEST_DB=:memory:：测试用独立内存库（StaticPool 单连接），
  绝不碰开发库 data/lawagent.db；
- LLM_MOCK=1：所有 llm_* 工具走确定性 Mock，测试离线可复现；
- LAWAGENT_SKIP_SEED=1：默认不种演示账号（需要时在具体测试里显式调 _seed_demo_users）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("USE_SQLITE", "1")
os.environ.setdefault("SQLITE_TEST_DB", ":memory:")
os.environ.setdefault("LLM_MOCK", "1")
os.environ.setdefault("LAWAGENT_SKIP_SEED", "1")
# 演示账号种子密码：源码里已不写死（见 _seed_demo_users），测试统一从这里注入，
# 保证各测试文件里 law123456 的登录口径不变。
os.environ.setdefault("DEMO_PASSWORD", "law123456")

import pytest  # noqa: E402

from backend.memory.models import _seed_demo_users, init_db  # noqa: E402


@pytest.fixture(autouse=True)
async def _reset_sse_status():
    """sse_starlette 的 AppStatus.should_exit_event 是模块级单例，绑定首次
    创建它的 event loop；pytest-asyncio 每个测试函数用新 loop，不重置的话
    第二个 SSE 测试必报「bound to a different event loop」。"""
    import asyncio

    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = asyncio.Event()
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """每个测试前清空限流桶：测试里大量登录/对话调用不会互相撞 429
    （限流用例自己连打触发即可）。"""
    from backend.core import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture(scope="session", autouse=True)
async def _prepare_db():
    """整个测试会话共用一份内存库：建表 + 演示账号（HITL 测试要登录）。

    会话结束 dispose engine —— 否则 aiosqlite 的 worker 线程（非 daemon）
    会让 pytest 进程在「测试全部通过后」仍无法退出。
    """
    await init_db()
    await _seed_demo_users()
    yield
    from backend.memory.models import engine

    await engine.dispose()

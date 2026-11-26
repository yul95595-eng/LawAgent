"""LangGraph Checkpointer —— 会话记忆（thread_id 隔离）。

规格文档口径：LangGraph Checkpointer + thread_id 隔离。
三档后端（CHECKPOINTER_BACKEND 环境变量切换）：
  memory   进程内 MemorySaver —— pytest 默认（每 Test 独立事件循环，进程内最安全）
  sqlite   文件级 AsyncSqliteSaver —— 开发/单机生产：重启不丢 HITL 挂起状态
  postgres AsyncPostgresSaver —— 容器/多实例部署（langgraph-checkpoint-postgres）

关键点：
- 持久化 saver 的连接绑定事件循环，必须由 FastAPI lifespan 在主循环内
  建立（这正是不能在模块导入期建单例的原因），随后 rebuild_agents() 重编译；
- pytest 不跑 lifespan，get_checkpointer() 走 MemorySaver 兜底，行为与历史一致；
- API 进程内「发起图执行」与「合伙人确认后 resume」共用同一实例，
  中断状态才能找回。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)

_holder: dict[str, Any] = {"saver": None, "closable": None}


def get_checkpointer():
    """Agent 编译期取 saver：lifespan 已建持久化实例则复用，否则 MemorySaver 兜底。"""
    if _holder["saver"] is not None:
        return _holder["saver"]
    return MemorySaver()


def set_checkpointer(saver, closable=None) -> None:
    _holder["saver"] = saver
    _holder["closable"] = closable


async def setup_checkpointer():
    """FastAPI lifespan 内调用：按 CHECKPOINTER_BACKEND 建立持久化 Checkpointer。

    memory 模式返回 None（Agent 各自 MemorySaver，行为不变）；
    sqlite/postgres 模式返回共享 saver 并完成建表（setup 幂等）。
    """
    from backend.core.config import DATA_DIR, settings

    mode = (settings.checkpointer_backend or "memory").strip().lower()

    if mode == "sqlite":
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = settings.checkpointer_path or (
            str(Path(settings.sqlite_path).parent / "checkpoints.db")
            if settings.sqlite_path
            else str(DATA_DIR / "checkpoints.db")
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        set_checkpointer(saver, closable=conn)
        logger.info("Checkpointer：SQLite 持久化 → %s", path)
        return saver

    if mode == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        pool = AsyncConnectionPool(
            conninfo=url,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        set_checkpointer(saver, closable=pool)
        logger.info("Checkpointer：PostgreSQL 持久化")
        return saver

    logger.info("Checkpointer：memory（进程内，重启即失）")
    return None


async def close_checkpointer() -> None:
    """lifespan shutdown：释放连接/连接池。"""
    closable = _holder.get("closable")
    if closable is not None:
        try:
            await closable.close()
        except Exception:  # noqa: BLE001
            logger.warning("Checkpointer 连接关闭异常（忽略）", exc_info=True)
    set_checkpointer(None, None)

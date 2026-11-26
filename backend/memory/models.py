"""记忆层 ORM —— PostgreSQL 长期记忆（开发态 SQLite 回退，SQL 双后端通用）。

表设计（规格文档口径）：
- users                        工号账号（tenant_id + user_id 双层隔离的锚点）
- chat_threads / thread_messages  会话线程与消息（LangGraph thread_id 对齐）
- case_analyses                案件分析记录（永久保存）
- contract_reviews             合同审查记录（含合伙人审核状态，保存 5 年）
- enterprise_compliance_profiles  企业合规档案（保存 3 年）
- audit_logs                   审计日志（全部敏感操作，保存 5 年）

数据清理策略（规格文档）：对话 1 年归档、案件分析永久、审查记录 5 年、
合规档案 3 年、审计日志 5 年 —— 归档/清理由定时任务执行，表里的
created_at / reviewed_at 是清理基准字段。
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from sqlalchemy import Column, Float, Integer, String, Text, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from backend.core.config import DATA_DIR, settings

logger = logging.getLogger(__name__)

Base = declarative_base()

SQLITE_PATH = DATA_DIR / "lawagent.db"

_engine_kwargs: dict = {}
if settings.use_sqlite and settings.sqlite_test_db == ":memory:":
    from sqlalchemy.pool import StaticPool

    _engine_kwargs["poolclass"] = StaticPool
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_async_engine(settings.database_url, echo=False, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class User(Base):
    """工号账号。status：approved 正常 / pending 待审核 / rejected 已拒绝。

    注册审核流（阶段 2）：自助注册只开 lawyer/legal，落库即 pending，
    须合伙人/管理员在 /auth/pending → /auth/approve 放行后才能登录；
    partner/admin 账号只能由既有合伙人后台开通，防止自封审核权。
    """

    __tablename__ = "users"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)   # 分所/租户
    user_no = Column(String, nullable=False, unique=True)    # 工号（登录用）
    username = Column(String, nullable=False)                # 姓名
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="lawyer")  # lawyer/partner/legal/admin
    status = Column(String, nullable=False, default="pending")  # approved/pending/rejected
    created_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))


class ChatThread(Base):
    """会话线程：左侧「历史任务列表」的数据源；过去 1 年任务可回溯。"""

    __tablename__ = "chat_threads"

    id = Column(String, primary_key=True)                    # = LangGraph thread_id
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False, default="新咨询")
    intents = Column(String, nullable=False, default="")     # 命中意图，逗号分隔
    status = Column(String, nullable=False, default="active")  # active/waiting_review/done
    created_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))
    updated_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))


class ThreadMessage(Base):
    __tablename__ = "thread_messages"

    id = Column(String, primary_key=True)
    thread_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)                    # user/assistant
    content = Column(Text, nullable=False, default="")
    result_json = Column(Text, nullable=True)                # 结构化结果卡片
    created_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))


class CaseAnalysis(Base):
    """案件分析记录 —— CaseAgent 的落库产出（永久保存）。"""

    __tablename__ = "case_analyses"

    case_id = Column(String, primary_key=True)
    thread_id = Column(String, nullable=True, index=True)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    case_title = Column(String, nullable=False, default="")
    case_type = Column(String, nullable=False, default="")   # 8 类案由之一
    case_facts_json = Column(Text, nullable=False, default="{}")
    applicable_law_json = Column(Text, nullable=False, default="[]")
    precedents_json = Column(Text, nullable=False, default="[]")
    issues_json = Column(Text, nullable=False, default="[]")
    limitation_text = Column(String, nullable=False, default="")   # 时效结论
    win_rate = Column(Float, nullable=True)                  # 胜诉率估计 0~1
    win_rate_reason = Column(String, nullable=False, default="")
    framework_json = Column(Text, nullable=False, default="{}")    # 分析框架/工作大纲
    created_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))


class ContractReview(Base):
    """合同审查记录 —— ContractAgent 的落库产出（保存 5 年）。

    status 流转：draft → pending_review（高风险待合伙人审）
              → approved（合伙人确认）/ returned（合伙人退回重写意见）
    """

    __tablename__ = "contract_reviews"

    review_id = Column(String, primary_key=True)
    thread_id = Column(String, nullable=True, index=True)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)     # 提交审查的律师
    contract_title = Column(String, nullable=False, default="")
    contract_text = Column(Text, nullable=False, default="")
    clauses_json = Column(Text, nullable=False, default="[]")
    risks_json = Column(Text, nullable=False, default="[]")
    risk_level = Column(String, nullable=False, default="low")   # low/medium/high
    review_opinion = Column(Text, nullable=False, default="")    # 审查意见书
    format_check_json = Column(Text, nullable=False, default="{}")
    status = Column(String, nullable=False, default="draft")
    reviewer_id = Column(String, nullable=True)              # 合伙人
    review_comment = Column(Text, nullable=True)             # 退回理由/批注
    created_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))
    reviewed_at = Column(String, nullable=True)


class EnterpriseComplianceProfile(Base):
    """企业合规档案 —— ComplianceAgent 的落库产出（保存 3 年）。"""

    __tablename__ = "enterprise_compliance_profiles"

    profile_id = Column(String, primary_key=True)
    thread_id = Column(String, nullable=True, index=True)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    enterprise_name = Column(String, nullable=False, default="")
    credit_code = Column(String, nullable=False, default="")
    info_json = Column(Text, nullable=False, default="{}")       # 工商信息快照
    check_json = Column(Text, nullable=False, default="{}")      # 合规检查结果
    risk_score = Column(Float, nullable=False, default=0.0)      # 风险评分 0~100
    citation_check_json = Column(Text, nullable=False, default="{}")  # 法条引用校验
    created_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))
    updated_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))


class AuditLog(Base):
    """审计日志 —— 「谁、何时、对什么、做了什么」，保存 5 年（合规口径）。"""

    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    user_role = Column(String, nullable=False, default="lawyer")
    action = Column(String, nullable=False)      # chat/review_submit/review_confirm/review_reject/...
    object_type = Column(String, nullable=False)  # thread/case/contract_review/compliance_profile
    object_id = Column(String, nullable=False, default="")
    detail = Column(Text, nullable=True)
    created_at = Column(String, nullable=False, server_default=sql_text("CURRENT_TIMESTAMP"))


async def init_db() -> None:
    """建表（幂等）+ 轻量列迁移 + 演示账号种子。应用启动/pytest fixture 调用。"""
    if settings.use_sqlite:
        target = settings.sqlite_test_db or str(SQLITE_PATH)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_column(conn, "users", "status", "VARCHAR DEFAULT 'approved'")
    if settings.seed_demo_users and os.getenv("LAWAGENT_SKIP_SEED", "") != "1":
        await _seed_demo_users()


async def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """轻量加列（create_all 不会给已存在的表补列）。

    取列名要按方言分开：SQLite 用 PRAGMA，PostgreSQL 走 information_schema。
    之前只写了 PRAGMA，导致 PG 模式启动即 `syntax error at or near "PRAGMA"`
    —— 这个错只有真正用 PG 跑起来才会暴露。
    """
    if conn.dialect.name == "sqlite":
        rows = (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).fetchall()
        cols = {str(row[1]).lower() for row in rows}
    else:
        rows = (await conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND table_schema = current_schema()"
        )).fetchall()
        cols = {str(row[0]).lower() for row in rows}
    if column.lower() not in cols:
        await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def _seed_demo_users() -> None:
    """演示工号：律师 / 合伙人 / 法务，幂等；直接审核通过。

    密码不写死在源码里：优先取 DEMO_PASSWORD；没配就随机生成，
    只在新账号真正创建的那次启动日志里提示一遍。
    这样仓库里不存在「拿到代码就能登录」的明文口令，演示时按日志/环境变量给密码即可。
    """
    from backend.core.security import hash_password
    from sqlalchemy import select

    pwd = (settings.demo_password or "").strip()
    generated = not pwd
    if generated:
        pwd = secrets.token_urlsafe(9)

    demo = [
        ("L1001", "zhanglvshi", "张律师", "lawyer"),
        ("P2001", "wanghehuoren", "王合伙人", "partner"),
        ("L3001", "lifawu", "李法务", "legal"),
    ]
    created = False
    async with SessionLocal() as session:
        for user_no, username, display, role in demo:
            exists = await session.scalar(select(User.id).where(User.user_no == user_no))
            if exists:
                continue
            session.add(User(
                id=f"u_{user_no.lower()}",
                tenant_id="tenant_firm",
                user_no=user_no,
                username=display,
                password_hash=hash_password(pwd),
                role=role,
                status="approved",
            ))
            created = True
        await session.commit()

    if created and generated:
        logger.warning(
            "已创建演示账号 L1001/P2001/L3001，本次随机密码：%s "
            "（仅打印一次，请立即记录；或在 .env 设 DEMO_PASSWORD 后重建库）", pwd
        )


def get_session() -> AsyncSession:
    return SessionLocal()

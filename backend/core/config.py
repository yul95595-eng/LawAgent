"""全局配置 —— 环境变量优先，缺省值保证「零配置可运行」。

设计原则：不给一套 PG/LLM 服务就卡死的工程务实（与模板库一致）：
- 数据库：默认 SQLite（data/lawagent.db），USE_SQLITE=0 时切 PostgreSQL；
- LLM：未配 DASHSCOPE_API_KEY 或 LLM_MOCK=1 时进入 Mock 模式，
  所有 llm_* 工具降级为确定性实现（测试与离线演示不依赖真实模型）；
- Checkpointer：开发态 MemorySaver，生产态 PostgresSaver（langgraph-checkpoint-postgres）；
- 向量库：开发态本地 TF-IDF 向量检索，生产口径 Milvus（README 注明）。
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# 开发态未配 JWT_SECRET 时用的进程内临时密钥（见 signing_secret）
_EPHEMERAL_SECRET: str | None = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LEGAL_DATA_DIR = DATA_DIR / "legal"
MOCK_DATA_DIR = DATA_DIR / "mock"
DEMO_DATA_DIR = DATA_DIR / "demo"


class Settings(BaseSettings):
    app_name: str = "LawAgent 智能法律咨询 Agent 系统"
    api_port: int = 9089

    # --- LLM ---
    dashscope_api_key: str = ""
    dashscope_model: str = "qwen-plus"
    dashscope_fallback_model: str = "qwen-turbo"
    llm_mock: bool = False
    llm_timeout_seconds: int = 30  # LLM 超时 30s → 降级规则引擎（容错口径）

    # --- 数据库 ---
    use_sqlite: bool = True
    sqlite_test_db: str = ""       # pytest 注入独立库
    sqlite_path: str = ""          # 开发态 SQLite 文件路径；留空 = data/lawagent.db
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "lawagent"
    db_user: str = "postgres"
    db_password: str = "postgres"

    # --- JWT ---
    # 不给可预测的默认密钥：源码里写死的默认值 = 谁都能自己签发合法 token。
    # 生产（APP_ENV=prod）未配置直接拒绝启动；开发态随机生成一把并告警。
    app_env: str = "dev"           # dev | prod
    jwt_secret: str = ""
    jwt_expire_hours: int = 12

    # --- 演示种子账号 ---
    # 密码不进源码：从 DEMO_PASSWORD 读；不配则首次种子随机生成并打印一次日志。
    demo_password: str = ""

    # --- Checkpointer（会话记忆持久化）---
    checkpointer_backend: str = "memory"   # memory | sqlite | postgres
    checkpointer_path: str = ""            # sqlite 模式文件路径；留空 = data/checkpoints.db

    # --- 演示 ---
    seed_demo_users: bool = True

    # --- 阶段3 数据接真：工商信息（企业征信）---
    # mock（默认，演示/离线）| qcc（企查查开放平台）| tianyancha（天眼查开放平台）
    enterprise_data_source: str = "mock"
    enterprise_api_timeout: float = 5.0
    qcc_api_key: str = ""          # 企查查 AppKey
    qcc_secret_key: str = ""       # 企查查 SecretKey（算 Token 签名用）
    qcc_base_url: str = "https://api.qichacha.com"
    tyc_api_key: str = ""          # 天眼查开放平台 token
    tyc_base_url: str = "https://open.api.tianyancha.com"

    # --- 阶段3 数据接真：判例库 ---
    # local（默认，仓库内置 66 条真实案号判例）| api（商业/自建判例库）
    precedent_data_source: str = "local"
    precedent_api_url: str = ""
    precedent_api_key: str = ""

    # --- 多 Agent 改造：检索受限自主循环（P1）---
    # 关闭时完全走原三路写死逻辑（评测基线/回滚用）；开启后由协调器决定检索轮次。
    retrieval_agent_enabled: bool = False
    retrieval_agent_max_rounds: int = 4

    # --- 阶段4 运维：指标暴露 ---
    metrics_enabled: bool = True
    metrics_token: str = ""   # 留空=内网可直接抓；填了则 /metrics 需 Bearer 令牌

    # --- 阶段4 运维：限流阈值（<=0 表示该维度不限流）---
    login_max_failures_per_min: int = 5    # 单账号密码错误次数/分钟
    auth_ip_max_per_min: int = 30          # 登录接口 IP 总闸
    chat_max_per_min: int = 10             # 对话接口/用户/分钟
    # 反代场景（docker-compose 的 nginx / 云负载均衡）下 request.client.host
    # 全是反代容器 IP，会让限流退化成「全所一个桶」。此时置 1，改为信任
    # X-Forwarded-For / X-Real-IP。默认关闭：裸奔时信任该头 = 客户端可伪造绕过限流。
    trust_proxy: bool = False

    @property
    def llm_enabled(self) -> bool:
        """真实 LLM 是否可用。Mock 模式下所有 llm_* 工具走确定性实现。"""
        return not self.llm_mock and bool(self.dashscope_api_key)

    @property
    def is_prod(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production"}

    @property
    def signing_secret(self) -> str:
        """JWT 签名密钥（唯一取用入口，业务代码不要直接读 jwt_secret）。

        生产必须显式配置 JWT_SECRET —— 否则任何人都能用源码里的默认值
        自己签一个 role=partner 的令牌，等于没有鉴权。
        开发态没配时随机生成一把（进程内复用）并告警：重启后旧令牌失效，
        这是刻意取舍，宁可掉线也不留可预测的密钥。
        """
        global _EPHEMERAL_SECRET
        if self.jwt_secret:
            return self.jwt_secret
        if self.is_prod:
            raise RuntimeError(
                "生产环境（APP_ENV=prod）必须配置 JWT_SECRET；"
                "请生成随机值：python -c \"import secrets;print(secrets.token_urlsafe(48))\""
            )
        if _EPHEMERAL_SECRET is None:
            _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
            logger.warning(
                "未配置 JWT_SECRET，已生成进程内临时密钥（重启后所有令牌失效）。"
                "演示/联调可用；部署前务必在 .env 里设置 JWT_SECRET。"
            )
        return _EPHEMERAL_SECRET

    @property
    def database_url(self) -> str:
        if self.use_sqlite:
            target = (
                self.sqlite_test_db
                or self.sqlite_path
                or str(DATA_DIR / "lawagent.db")
            )
            return f"sqlite+aiosqlite:///{target}"
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


# 环境变量绑定：pydantic-settings 自动读 .env / 环境变量（大小写不敏感）
settings = Settings(
    _env_file=os.getenv("LAWAGENT_ENV", PROJECT_ROOT / ".env"),
    _env_file_encoding="utf-8",
)

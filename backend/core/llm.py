"""LLM 客户端 —— 通义千问（DashScope OpenAI 兼容模式），带 Mock 降级。

两条运行路径（工程务实 + 容错口径的落地）：
1. 真实模式（配置了 DASHSCOPE_API_KEY 且 LLM_MOCK=0）：
   langchain-openai ChatOpenAI，temperature=0 保证结论可复现，
   主模型失败自动降级备模型（qwen-plus → qwen-turbo）；
2. Mock 模式（未配 Key / LLM_MOCK=1 / 真实调用超时 30s 失败）：
   调用方回退到各自工具的确定性实现（关键词规则），
   链路不断、结果可复现 —— 这正是「LLM 超时降级规则引擎」的容错设计。

面试口径：LLM 工具一律「先试真模型、失败降 Mock」，绝不静默返回假成功；
降级事件写日志，可观测。
"""
from __future__ import annotations

import logging
import warnings

from langchain_openai import ChatOpenAI

from backend.core.config import settings

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", message="Pydantic serializer warnings", category=UserWarning)

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _build_llm(model: str, temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=settings.dashscope_api_key,
        base_url=DASHSCOPE_BASE_URL,
        temperature=temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=2,
    )


def get_primary_llm() -> ChatOpenAI:
    return _build_llm(settings.dashscope_model)


def get_fallback_llm() -> ChatOpenAI:
    return _build_llm(settings.dashscope_fallback_model)


async def ainvoke_with_fallback(prompt: str) -> str:
    """主模型调用，失败降级备模型；两路都失败抛给调用方走 Mock。

    阶段4 运维：每次调用打点（模型/结果/耗时），降级率在 Grafana 上直接看得到
    —— 「LLM 降级静默发生」是这类系统最难查的线上问题。
    """
    import time

    from backend.core import metrics

    started = time.perf_counter()
    try:
        resp = await get_primary_llm().ainvoke(prompt)
        metrics.observe("lawagent_llm_duration_seconds", time.perf_counter() - started,
                        {"model": settings.dashscope_model, "outcome": "primary_ok"},
                        help_text="LLM 调用耗时（按模型/结果）")
        metrics.inc("lawagent_llm_calls_total", {"model": settings.dashscope_model,
                                                "outcome": "primary_ok"},
                    help_text="LLM 调用次数（primary_ok/fallback_ok/failed）")
        return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("主模型 %s 调用失败，降级备模型：%s", settings.dashscope_model, exc)
        metrics.inc("lawagent_llm_calls_total", {"model": settings.dashscope_model,
                                                "outcome": "primary_failed"},
                    help_text="LLM 调用次数（primary_ok/fallback_ok/failed）")

    try:
        resp = await get_fallback_llm().ainvoke(prompt)
        metrics.inc("lawagent_llm_calls_total", {"model": settings.dashscope_fallback_model,
                                                "outcome": "fallback_ok"},
                    help_text="LLM 调用次数（primary_ok/fallback_ok/failed）")
        return resp.content
    except Exception:
        metrics.inc("lawagent_llm_calls_total", {"model": settings.dashscope_fallback_model,
                                                "outcome": "failed"},
                    help_text="LLM 调用次数（primary_ok/fallback_ok/failed）")
        raise

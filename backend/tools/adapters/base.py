"""适配器基类与公共映射工具。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ExternalApiError(Exception):
    """外部数据源不可达/未授权/无数据。上层捕获后给友好提示，不外抛栈。"""


class EnterpriseAdapter(ABC):
    """工商档案数据源统一接口。"""

    name: str = "base"

    @abstractmethod
    async def fetch(self, enterprise_name: str) -> dict:
        """按企业名称取归一化档案（字段见 adapters.FIELD_SPEC）。

        查不到时抛 ExternalApiError（业务性无数据，不重试）。
        网络/鉴权类异常也统一包成 ExternalApiError，由 api_tools 的重试层处理。
        """

    async def aclose(self) -> None:  # pragma: no cover - 单例生命周期由调用方管
        return None


def first(raw: dict, *keys: str, default: str = ""):
    """按候选字段名取第一个非空值 —— 供应商字段名前后改版也能兜住。"""
    for k in keys:
        v = raw.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def to_int(v) -> int:
    """把 '500人' / '50-99人' / '500' / 500 统一成 int（区间取下限），取不到给 0。"""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    import re

    m = re.search(r"\d+", str(v or ""))
    return int(m.group()) if m else 0


def to_date(v) -> str:
    """统一成 YYYY-MM-DD：兼容毫秒时间戳 / '2012-06-15 00:00:00' / 20120615。"""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        import datetime as _dt

        ts = float(v) / 1000 if float(v) > 1e11 else float(v)
        try:
            return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (OSError, ValueError):
            return ""
    s = str(v).strip()
    if len(s) >= 10 and s[4] in "-/年" and s[7] in "-/月":
        return f"{s[0:4]}-{s[5:7]}-{s[8:10]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


def as_list(v) -> list[str]:
    """经营范围/许可等字段：'a、b' / 'a;b' / list 统一成 list[str]。"""
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v)
    for sep in ("；", ";", "、", "，", ","):
        if sep in s:
            return [p.strip() for p in s.split(sep) if p.strip()]
    return [s.strip()]

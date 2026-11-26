"""本地 mock 数据源（默认）—— 演示/离线/无 key 场景。

数据在 `data/mock/enterprises.json`，字段已经是归一化后的形状，所以这里
主要做「精确名 → 包含匹配」的两轮查找，找不到抛 ExternalApiError。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from backend.core.config import MOCK_DATA_DIR
from backend.tools.adapters.base import EnterpriseAdapter, ExternalApiError

_ENTERPRISES_PATH = MOCK_DATA_DIR / "enterprises.json"


@lru_cache(maxsize=1)
def _load_mock(path: Path = _ENTERPRISES_PATH) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("enterprises", [])


class MockEnterpriseAdapter(EnterpriseAdapter):
    name = "mock"

    async def fetch(self, enterprise_name: str) -> dict:
        rows = _load_mock()
        for e in rows:
            if e["enterprise_name"] == enterprise_name:
                return {**e, "data_source": "mock"}
        # 模糊兜底：名称包含匹配（演示时用户常少打「股份有限公司」）
        for e in rows:
            if enterprise_name in e["enterprise_name"] or e["enterprise_name"] in enterprise_name:
                return {**e, "data_source": "mock"}
        raise ExternalApiError(f"未查到企业「{enterprise_name}」的工商信息，请核对名称")

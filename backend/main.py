"""启动入口：python -m backend.main（或 uvicorn backend.main:app --port 9089）。"""
from __future__ import annotations

import uvicorn

from backend.core.config import settings

if __name__ == "__main__":
    uvicorn.run("backend.api.app:app", host="127.0.0.1", port=settings.api_port, reload=False)

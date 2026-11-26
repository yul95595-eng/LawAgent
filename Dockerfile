# LawAgent 后端镜像（SQLite 开发态，零配置可运行）
# 构建参数：国内网络默认走镜像源；海外构建可 --build-arg 覆盖
#   docker compose build --build-arg APT_MIRROR=deb.debian.org --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

FROM python:3.11-slim

ARG APT_MIRROR
ARG PIP_INDEX_URL

WORKDIR /app

# pg_dump：阶段4 备份脚本在容器内跑 `--pg` 时需要（python-slim 不自带）
# debian bookworm 用 deb822 源（debian.sources），老格式兜底一起换，避免 apt 卡在国外源
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    USE_SQLITE=1 \
    SQLITE_PATH=/data/lawagent.db \
    SEED_DEMO_USERS=1

VOLUME ["/data"]
EXPOSE 9089

CMD ["uvicorn", "backend.api.app:app", "--host", "0.0.0.0", "--port", "9089"]

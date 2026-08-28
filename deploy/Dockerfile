# syntax=docker/dockerfile:1

# ============================================================
# 编译阶段（builder）
# 用于安装 Python 依赖并编译二进制 wheel，不进入最终镜像
# ============================================================
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 安装编译 cryptography、langchain 等包所需的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境并安装生产依赖（虚拟环境便于多阶段复制）
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ============================================================
# 运行阶段（runtime）
# 仅保留运行所需的最小依赖，镜像体积显著减小
# ============================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    GUNICORN_WORKERS=4

WORKDIR /app

# 安装运行时系统库、健康检查 curl、降权工具 gosu
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户/组（固定 UID/GID 便于宿主机权限对齐）
RUN groupadd -r appgroup -g 1000 && useradd -r -u 1000 -g appgroup appuser

# 从编译阶段复制完整的虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制入口脚本（需在 COPY . . 之前单独复制，避免被 .dockerignore 排除）
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 复制应用代码（受 .dockerignore 控制，排除测试、文档、构建无关文件）
COPY . .

# 预创建运行时目录并设置属主（避免宿主机空挂载后 root 独占）
RUN mkdir -p /app/data /app/secure && chown -R appuser:appgroup /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "main:app", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-"]

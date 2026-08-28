#!/bin/sh
set -e

# 修复宿主机挂载目录的权限（Docker 创建的空目录默认属主为 root）
chown -R appuser:appgroup /app/data 2>/dev/null || true
chown -R appuser:appgroup /app/secure 2>/dev/null || true

# 如果启动命令是 gunicorn，自动注入 worker 数环境变量
if [ "$1" = "gunicorn" ]; then
    shift
    set -- gunicorn main:app \
        -k uvicorn.workers.UvicornWorker \
        --bind 0.0.0.0:8000 \
        --workers "${GUNICORN_WORKERS:-4}" \
        --access-logfile - \
        --error-logfile - \
        "$@"
fi

# 以非 root 用户执行主进程
exec gosu appuser:appgroup "$@"

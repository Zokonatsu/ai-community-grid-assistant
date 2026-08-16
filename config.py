"""
config.py
集中配置模块

功能：统一加载 .env 文件，集中管理环境变量配置，确保系统环境变量优先级高于 .env 文件。
遵循 12-Factor App 原则：运行时的系统环境变量（Docker/K8s/Shell export）优先于本地 .env 默认值。
"""

import os

from dotenv import load_dotenv

# ------------------------------------------------------------------
# 加载 .env 文件
# ------------------------------------------------------------------
# 12-Factor App 原则：系统环境变量（Docker/K8s/Shell export）优先级高于 .env 文件。
# override=False 确保已存在的环境变量不会被 .env 中的值覆盖。
# .env 仅作为本地开发默认值，生产环境应通过 docker-compose env_file 或运行时注入。
load_dotenv(override=False)

# ------------------------------------------------------------------
# LLM API 配置
# ------------------------------------------------------------------
LLM_API_KEY: str | None = os.getenv("LLM_API_KEY")
LLM_BASE_URL: str | None = os.getenv("LLM_BASE_URL")

# 启动时主动校验必填配置，避免延迟到首次 API 请求时才失败
if not LLM_API_KEY:
    raise RuntimeError(
        "环境变量 LLM_API_KEY 未设置。"
        "请在系统环境变量或 .env 文件中配置 LLM_API_KEY。"
    )

# ------------------------------------------------------------------
# 账号数据加密配置
# ------------------------------------------------------------------
# 用于加密 secure/ 下 users.json.enc / sessions.json.enc 的 AES-256 密钥。
# 必须为 64 位 hex（= 32 字节）。密钥丢失 = 账号数据永久无法解密，务必单独备份。
DATA_ENCRYPTION_KEY: str | None = os.getenv("DATA_ENCRYPTION_KEY")

if not DATA_ENCRYPTION_KEY or len(DATA_ENCRYPTION_KEY.strip()) != 64:
    raise RuntimeError(
        "环境变量 DATA_ENCRYPTION_KEY 未设置或长度不是 64 位十六进制"
        "（对应 AES-256 的 32 字节密钥）。\n"
        "生成命令：python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "请写入系统环境变量或 .env 文件，并单独备份——密钥丢失将导致账号数据永久无法解密。"
    )

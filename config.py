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

# LLM 请求重试配置
LLM_RETRY_ATTEMPTS: int = int(os.getenv("LLM_RETRY_ATTEMPTS", "3"))
LLM_RETRY_BASE_DELAY: float = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.0"))

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

# ------------------------------------------------------------------
# 注册定位校验开关（默认开启）
# ------------------------------------------------------------------
# true/1/yes/on  -> 注册必须带浏览器定位且定位在小区范围内（原产品规则，需 HTTPS/localhost）
# false/0/no/off -> 关闭定位校验，允许无定位注册（暂无 HTTPS 环境临时开放注册用，配置 HTTPS 后建议恢复）
COMMUNITY_REQUIRE_LOCATION: bool = os.getenv("COMMUNITY_REQUIRE_LOCATION", "true").strip().lower() in ("1", "true", "yes", "on")

# ------------------------------------------------------------------
# 账号存储后端（本地加密文件 / 腾讯云 COS 云存储）
# ------------------------------------------------------------------
# file：本地 secure/*.enc（默认，测试与本地开发）
# cloudbase：用户数据读写腾讯云 COS 对象存储（见 cloud_store.py / DEPLOY.md）
AUTH_STORE: str = (os.getenv("AUTH_STORE") or "file").strip().lower()
if AUTH_STORE not in ("file", "cloudbase"):
    raise RuntimeError(
        f"环境变量 AUTH_STORE 取值只能是 file 或 cloudbase，当前值：{AUTH_STORE!r}"
    )

if AUTH_STORE == "cloudbase":
    # 云存储模式必须提供 COS 存储桶与密钥（腾讯云 API 密钥，勿提交、勿外泄）
    _cos_required = ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY")
    _cos_missing = [v for v in _cos_required if not os.getenv(v)]
    if _cos_missing:
        raise RuntimeError(
            "AUTH_STORE=cloudbase 时缺少环境变量：" + ", ".join(_cos_missing)
            + "。请按 DEPLOY.md 在 .env 中配置腾讯云 COS 存储桶信息，"
            + "或改回 AUTH_STORE=file 使用本地存储。"
        )

# ------------------------------------------------------------------
# CORS 白名单配置
# ------------------------------------------------------------------
# 允许跨域请求的来源列表，逗号分隔。空字符串表示允许所有（开发环境慎用）。
_cors_raw: str = os.getenv("CORS_ALLOW_ORIGINS", "")
if _cors_raw.strip():
    CORS_ALLOW_ORIGINS: list[str] = [origin.strip() for origin in _cors_raw.split(",") if origin.strip()]
else:
    CORS_ALLOW_ORIGINS: list[str] = [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://118.31.58.191:8000",
        "http://aiwanggeyuan.cn",
        "https://aiwanggeyuan.cn",
    ]

# ------------------------------------------------------------------
# 限流配置
# ------------------------------------------------------------------
# true/1/yes/on  -> 启用限流中间件
# false/0/no/off -> 关闭限流（测试或本地开发可临时关闭）
RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
RATE_LIMIT_LOGIN: str = os.getenv("RATE_LIMIT_LOGIN", "5/minute")
RATE_LIMIT_EVENTS: str = os.getenv("RATE_LIMIT_EVENTS", "60/minute")

# ------------------------------------------------------------------
# 日志脱敏配置
# ------------------------------------------------------------------
# true/1/yes/on  -> 启用日志脱敏（手机号、身份证号等敏感字段打码）
# false/0/no/off -> 关闭脱敏（仅调试时临时使用）
LOG_REDACT: bool = os.getenv("LOG_REDACT", "true").strip().lower() in ("1", "true", "yes", "on")

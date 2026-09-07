# -*- coding: utf-8 -*-
"""
media_store.py
媒体文件存储（居民照片/录音、管理员留证照片、回复照片）

- 默认走腾讯云 COS（环境变量 COS_REGION/COS_BUCKET/COS_SECRET_ID/COS_SECRET_KEY 齐全时）；
- 未配置 COS 时回退到本地 data/uploads/，便于本地开发与测试。
- 媒体内容不加密（图片/音频二进制），仅通过对象名隔离；读取需登录鉴权。
"""

import logging
import os
import uuid

logger = logging.getLogger("media_store")

# 上传文件类型白名单（扩展名 -> MIME）
ALLOWED_EXTENSIONS = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "webm": "audio/webm",
}

_MAX_BYTES = 15 * 1024 * 1024  # 单文件上限 15MB

LOCAL_MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
COS_PREFIX = "media/"


def _cos_configured() -> bool:
    return all(os.getenv(k) for k in ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY"))


def backend() -> str:
    return "cos" if _cos_configured() else "local"


def normalize_ext(filename: str | None) -> str:
    """从文件名推断合法扩展名；非法/未知返回空串。"""
    if not filename:
        return ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in ALLOWED_EXTENSIONS else ""


def content_type(media_id: str) -> str:
    ext = media_id.rsplit(".", 1)[-1].lower() if "." in media_id else ""
    return ALLOWED_EXTENSIONS.get(ext, "application/octet-stream")


def _validate(data: bytes, ext: str) -> None:
    if not ext or ext not in ALLOWED_EXTENSIONS:
        raise ValueError("不支持的文件类型")
    if not data:
        raise ValueError("文件内容为空")
    if len(data) > _MAX_BYTES:
        raise ValueError("文件过大，单文件不超过 15MB")


def save_upload(data: bytes, filename: str | None = None) -> str:
    """保存上传内容，返回 media_id（含扩展名）。失败抛异常。"""
    ext = normalize_ext(filename)
    _validate(data, ext)
    media_id = uuid.uuid4().hex + "." + ext
    if backend() == "cos":
        import cloud_store
        cloud_store.upload(COS_PREFIX + media_id, data)
    else:
        os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)
        path = os.path.join(LOCAL_MEDIA_DIR, media_id)
        with open(path, "wb") as f:
            f.write(data)
    logger.info("媒体已保存：%s（%d 字节，backend=%s）", media_id, len(data), backend())
    return media_id


def read_upload(media_id: str) -> bytes | None:
    """读取媒体内容；不存在返回 None，其它异常抛错。"""
    if not media_id or "/" in media_id or "\\" in media_id or ".." in media_id:
        return None
    if backend() == "cos":
        import cloud_store
        return cloud_store.download(COS_PREFIX + media_id)
    path = os.path.join(LOCAL_MEDIA_DIR, media_id)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()

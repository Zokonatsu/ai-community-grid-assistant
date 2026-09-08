# -*- coding: utf-8 -*-
"""
media_store.py
媒体文件存储（居民照片/录音、管理员留证照片、回复照片）

- 默认走腾讯云 COS（环境变量 COS_REGION/COS_BUCKET/COS_SECRET_ID/COS_SECRET_KEY 齐全时）；
- 未配置 COS 时回退到本地 data/uploads/，便于本地开发与测试。
- 录音(音频)存入 recordings/，照片存入 media/。
- 媒体内容不加密（图片/音频二进制），仅通过对象名隔离；读取需登录鉴权。
"""

import logging
import os
import uuid
from datetime import datetime

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

# 音频扩展名（用于录音分文件夹）
AUDIO_EXTENSIONS = {"mp3", "m4a", "wav", "ogg", "webm"}

_MAX_BYTES = 15 * 1024 * 1024  # 单文件上限 15MB

# COS 前缀 / 本地子目录
COS_PHOTO_PREFIX = "media/"
COS_AUDIO_PREFIX = "recordings/"
LOCAL_PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads", "media")
LOCAL_AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads", "recordings")


def _cos_configured() -> bool:
    return all(os.getenv(k) for k in ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY"))


def backend() -> str:
    return "cos" if _cos_configured() else "local"


def ext_of(media_id: str) -> str:
    return media_id.rsplit(".", 1)[-1].lower() if "." in media_id else ""


def is_audio(media_id: str) -> bool:
    """media_id（含扩展名）是否为音频/录音。"""
    return ext_of(media_id or "") in AUDIO_EXTENSIONS


def normalize_ext(filename: str | None) -> str:
    """从文件名推断合法扩展名；非法/未知返回空串。"""
    if not filename:
        return ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in ALLOWED_EXTENSIONS else ""


def content_type(media_id: str) -> str:
    ext = ext_of(media_id)
    return ALLOWED_EXTENSIONS.get(ext, "application/octet-stream")


def _validate(data: bytes, ext: str) -> None:
    if not ext or ext not in ALLOWED_EXTENSIONS:
        raise ValueError("不支持的文件类型")
    if not data:
        raise ValueError("文件内容为空")
    if len(data) > _MAX_BYTES:
        raise ValueError("文件过大，单文件不超过 15MB")


def _folder_of(kind: str) -> tuple[str, str]:
    """返回 (COS 前缀, 本地目录)。kind=audio -> recordings/，其它 -> media/。"""
    if kind == "audio":
        return COS_AUDIO_PREFIX, LOCAL_AUDIO_DIR
    return COS_PHOTO_PREFIX, LOCAL_PHOTO_DIR


def save_upload(data: bytes, filename: str | None = None, kind: str = "photo") -> str:
    """保存上传内容，返回 media_id（含扩展名）。失败抛异常。"""
    ext = normalize_ext(filename)
    _validate(data, ext)
    media_id = uuid.uuid4().hex + "." + ext
    cos_prefix, local_dir = _folder_of(kind)
    if backend() == "cos":
        import cloud_store
        cloud_store.upload(cos_prefix + media_id, data)
    else:
        os.makedirs(local_dir, exist_ok=True)
        with open(os.path.join(local_dir, media_id), "wb") as f:
            f.write(data)
    logger.info("媒体已保存：%s（kind=%s，%d 字节，backend=%s）", media_id, kind, len(data), backend())
    return media_id


def read_upload(media_id: str) -> bytes | None:
    """读取媒体内容；同时支持 recordings/ 与 media/。不存在返回 None，其它异常抛错。"""
    if not media_id or "/" in media_id or "\\" in media_id or ".." in media_id:
        return None
    if backend() == "cos":
        import cloud_store
        for prefix in (COS_AUDIO_PREFIX, COS_PHOTO_PREFIX):
            data = cloud_store.download(prefix + media_id)
            if data is not None:
                return data
        return None
    for d in (LOCAL_AUDIO_DIR, LOCAL_PHOTO_DIR):
        path = os.path.join(d, media_id)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    return None


def list_audio_keys() -> list[dict]:
    """列出所有录音对象，返回 [{key, last_modified}]。兼容 COS 与本地。"""
    if backend() == "cos":
        import cloud_store
        return cloud_store.list_objects(COS_AUDIO_PREFIX)
    if not os.path.isdir(LOCAL_AUDIO_DIR):
        return []
    out: list[dict] = []
    for fn in os.listdir(LOCAL_AUDIO_DIR):
        pth = os.path.join(LOCAL_AUDIO_DIR, fn)
        if os.path.isfile(pth):
            out.append({"key": fn, "last_modified": datetime.fromtimestamp(os.path.getmtime(pth))})
    return out


def delete_audio(media_id: str) -> bool:
    """删除一条录音；COS 走 recordings/ 前缀，本地走 recordings/ 目录。返回是否实际删除。"""
    if not media_id or "/" in media_id or "\\" in media_id or ".." in media_id:
        return False
    if backend() == "cos":
        import cloud_store
        return cloud_store.delete_object(COS_AUDIO_PREFIX + media_id)
    pth = os.path.join(LOCAL_AUDIO_DIR, media_id)
    if os.path.exists(pth):
        try:
            os.remove(pth)
            logger.info("本地录音已删除：%s", media_id)
            return True
        except OSError as exc:
            logger.warning("本地录音删除失败：%s，%s", media_id, exc)
    return False

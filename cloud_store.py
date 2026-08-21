"""
cloud_store.py
账号数据云存储封装（腾讯云 COS 对象存储后端）

背景：
    腾讯云 CloudBase 云存储底层就是腾讯云 COS 对象存储。CloudBase 官方 Python SDK
    （cloudbase_client）未在 PyPI 发布，无法通过 pip 安装，故直接使用成熟的
    cos-python-sdk-v5 操作对象存储。两者是同一套对象存储，效果一致。

职责：
    仅负责把加密后的字节 blob 上传/下载到 COS 存储桶，**不参与加解密**。
    加解密仍由 secure_store.py 负责（AES-256-GCM，密钥 DATA_ENCRYPTION_KEY）。

配置（环境变量，见 .env.example / DEPLOY.md）：
    COS_REGION       存储桶地域，如 ap-guangzhou
    COS_BUCKET       存储桶名称，如 grid-assistant-125xxxxxxx
    COS_SECRET_ID    腾讯云 API 密钥 ID（SecretId）
    COS_SECRET_KEY   腾讯云 API 密钥 Key（SecretKey）

设计约定（与 secure_store 同哲学）：
    - 对象不存在 -> download 返回 None（安全默认，视为空库）
    - 网络/鉴权/其它异常 -> 一律 raise CloudStoreError，
      绝不静默返回空，防止上层误判"无用户"而重建 admin 覆盖真实数据。

惰性加载：
    qcloud_cos 仅在真正使用云存储时导入，AUTH_STORE=file（本地/测试）完全不需要
    安装该 SDK，保证本地开发与测试零额外依赖。
"""

import logging
import os

logger = logging.getLogger("cloud_store")

# 环境变量名
ENV_REGION = "COS_REGION"
ENV_BUCKET = "COS_BUCKET"
ENV_SECRET_ID = "COS_SECRET_ID"
ENV_SECRET_KEY = "COS_SECRET_KEY"

# 云存储对象名（与本地加密文件名一致，便于控制台辨认）
USERS_OBJECT_KEY = "users.json.enc"
SESSIONS_OBJECT_KEY = "sessions.json.enc"


class CloudStoreError(RuntimeError):
    """云存储操作失败（配置缺失、SDK 未安装、网络、鉴权、对象异常）。"""


# ------------------------------------------------------------------
# 配置与客户端
# ------------------------------------------------------------------
def _config() -> dict[str, str]:
    """读取并校验云存储配置；缺失任一必需项则 raise（fail-fast）。"""
    values = {
        ENV_REGION: (os.environ.get(ENV_REGION) or "").strip(),
        ENV_BUCKET: (os.environ.get(ENV_BUCKET) or "").strip(),
        ENV_SECRET_ID: (os.environ.get(ENV_SECRET_ID) or "").strip(),
        ENV_SECRET_KEY: (os.environ.get(ENV_SECRET_KEY) or "").strip(),
    }
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise CloudStoreError(
            "云存储未配置，缺少环境变量：" + ", ".join(missing)
            + "。请按 DEPLOY.md 在 .env 中配置腾讯云 COS 存储桶信息。"
        )
    return values


_client_state: tuple | None = None  # (CosS3Client, bucket)


def _get_client() -> tuple:
    """返回 (client, bucket)。qcloud_cos 在此处惰性导入，未安装时报清晰错误。"""
    global _client_state
    if _client_state is None:
        cfg = _config()
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:
            raise CloudStoreError(
                "缺少腾讯云 COS SDK：请先安装 cos-python-sdk-v5"
                "（requirements.txt 已含；本地开发如需云存储：pip install cos-python-sdk-v5）"
            ) from exc
        config = CosConfig(
            Region=cfg[ENV_REGION],
            SecretId=cfg[ENV_SECRET_ID],
            SecretKey=cfg[ENV_SECRET_KEY],
        )
        _client_state = (CosS3Client(config), cfg[ENV_BUCKET])
    return _client_state


def _is_not_found(exc: Exception) -> bool:
    """COS 服务端 404（NoSuchKey）判定为"对象不存在"。"""
    try:
        from qcloud_cos.cos_exception import CosServiceError
    except ImportError:
        return False
    if isinstance(exc, CosServiceError):
        try:
            return exc.get_status_code() == 404
        except Exception:
            return False
    return False


# ------------------------------------------------------------------
# 对象读写
# ------------------------------------------------------------------
def download(key: str) -> bytes | None:
    """下载对象内容；对象不存在返回 None，其它异常一律 raise。"""
    client, bucket = _get_client()
    try:
        resp = client.get_object(bucket, key)
    except Exception as exc:
        if _is_not_found(exc):
            logger.info("云存储对象不存在（视为空库）：%s", key)
            return None
        raise CloudStoreError(f"云存储下载失败（key={key}）：{exc}") from exc
    try:
        return resp["Body"].get_raw_stream().read()
    except Exception as exc:
        raise CloudStoreError(f"云存储下载响应读取失败（key={key}）：{exc}") from exc


def upload(key: str, data: bytes) -> None:
    """上传对象内容（覆盖写）。失败一律 raise，避免"表面成功、重启丢数据"。"""
    client, bucket = _get_client()
    try:
        # 注意：cos-python-sdk-v5 的签名是 put_object(Bucket, Body, Key)，
        # Body 是第 2 参数、Key 是第 3 参数；用关键字传参防止位置错序
        # 把二进制 blob 误传给 Key（会被 to_unicode 解码失败）。
        client.put_object(Bucket=bucket, Key=key, Body=data)
    except Exception as exc:
        raise CloudStoreError(f"云存储上传失败（key={key}）：{exc}") from exc
    logger.info("云存储上传成功：%s（%d 字节）", key, len(data))


# ------------------------------------------------------------------
# 存储桶管理
# ------------------------------------------------------------------
def ensure_bucket() -> bool:
    """确保 COS 存储桶存在；不存在则创建（私有权限），已存在则跳过。

    返回 True 表示本次新建了存储桶，False 表示已存在。
    鉴权/网络异常一律 raise（fail-fast），绝不静默。
    注意：全程不打印任何密钥信息，仅记录桶名。
    """
    client, bucket = _get_client()
    try:
        client.head_bucket(Bucket=bucket)
        logger.info("云存储桶已存在（跳过创建）：%s", bucket)
        return False
    except Exception as exc:
        if not _is_not_found(exc):
            raise CloudStoreError(f"云存储桶检查失败（bucket={bucket}）：{exc}") from exc
    try:
        # 私有权限（默认 ACL），不开放公共读/写
        client.create_bucket(Bucket=bucket, ACL="private")
    except Exception as exc:
        raise CloudStoreError(f"云存储桶创建失败（bucket={bucket}）：{exc}") from exc
    logger.info("云存储桶已创建（私有权限）：%s", bucket)
    return True


def object_exists(key: str) -> bool:
    """判断对象是否存在（供初始化脚本使用）；异常一律 raise。"""
    client, bucket = _get_client()
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as exc:
        if _is_not_found(exc):
            return False
        raise CloudStoreError(f"云存储对象检查失败（key={key}）：{exc}") from exc


def delete_object(key: str) -> bool:
    """删除对象；对象不存在返回 False，其它异常 raise。返回是否实际删除。"""
    client, bucket = _get_client()
    try:
        client.delete_object(Bucket=bucket, Key=key)
        logger.info("云存储对象已删除：%s", key)
        return True
    except Exception as exc:
        if _is_not_found(exc):
            logger.info("云存储对象不存在（跳过删除）：%s", key)
            return False
        raise CloudStoreError(f"云存储删除失败（key={key}）：{exc}") from exc


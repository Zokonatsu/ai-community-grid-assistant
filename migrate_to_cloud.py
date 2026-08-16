"""
migrate_to_cloud.py
一次性迁移脚本：把本地 secure/users.json.enc 上传到腾讯云 COS 云存储。

用法：
    python migrate_to_cloud.py

前置条件：
    1. .env 已配置 COS_REGION / COS_BUCKET / COS_SECRET_ID / COS_SECRET_KEY；
    2. 本地存在 secure/users.json.enc（已加密账号数据）。

说明：
    - 加密 blob 自包含，字节原样上传，无需解密；
    - 本地文件**保留**（作为回滚备份），脚本不会删除；
    - 上传完成后，把服务器 .env 的 AUTH_STORE 设为 cloudbase 并重启服务即可切到云存储。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cloud_store  # noqa: E402

USERS_FILE = os.path.join("secure", "users.json.enc")
OBJECT_KEY = cloud_store.USERS_OBJECT_KEY


def main() -> int:
    if not os.path.exists(USERS_FILE):
        print(f"错误：本地 {USERS_FILE} 不存在，无法迁移。", file=sys.stderr)
        return 1

    with open(USERS_FILE, "rb") as f:
        data = f.read()
    if not data:
        print(f"错误：{USERS_FILE} 为空，疑似损坏，已中止迁移。", file=sys.stderr)
        return 1

    try:
        cloud_store.upload(OBJECT_KEY, data)
    except cloud_store.CloudStoreError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1

    print(f"✅ 已上传 {len(data)} 字节到云存储对象 {OBJECT_KEY}")
    print("本地 secure/users.json.enc 已保留（回滚备份）。")
    print("下一步：在服务器 .env 设置 AUTH_STORE=cloudbase 并重启服务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

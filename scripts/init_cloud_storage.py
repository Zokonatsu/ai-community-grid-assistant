"""
init_cloud_storage.py
一键初始化云存储（删除旧账号 + 账号/会话身份数据上云前清理）

任务书：T20260819-004（旧账号全部删除、不迁移上云；默认 admin 由空库自动重建）

职责（只处理身份/会话数据，绝不触碰业务数据）：
  1. 确保 COS 存储桶存在（不存在自动创建，私有权限，不开放公共读/写）；
  2. 删除云端旧对象 users.json.enc / sessions.json.enc（存在才删）；
  3. 删除本地身份/会话残留：
       - secure/users.json.enc、secure/sessions.json.enc
       - data/users.json、data/sessions.json（旧版明文）
       - 项目内 *.migrated.bak（升级迁移备份）
  4. 打印不含任何密钥的状态摘要。

用法：
  python init_cloud_storage.py          # 交互二次确认后执行
  python init_cloud_storage.py --yes    # 显式跳过二次确认（自动化/文档化流程）
  python init_cloud_storage.py --force  # 同 --yes

安全约定：
  - 本脚本只删除身份/会话数据；data/events.jsonl、data/tasks.json、
    data/community_config.json 等业务数据绝不触碰。
  - 不打印、不写入任何 COS 密钥与 DATA_ENCRYPTION_KEY。
  - 幂等：可重复执行，重复执行时「已不存在」项自动跳过。
"""

import os
import sys

from dotenv import load_dotenv

# 加载 .env（系统环境变量优先，不覆盖已存在变量）；不 import config，
# 避免引入 LLM/DATA_ENCRYPTION_KEY 等与本脚本无关的启动校验
load_dotenv(override=False)

import cloud_store  # noqa: E402


# ------------------------------------------------------------------
# 目标清单（冻结契约：仅限身份/会话数据）
# ------------------------------------------------------------------
LOCAL_TARGETS = [
    ("secure", "users.json.enc"),
    ("secure", "sessions.json.enc"),
    ("data", "users.json"),
    ("data", "sessions.json"),
]

# 明确禁止触碰的业务数据（防御性检查，绝不列入删除清单）
PROTECTED = {
    os.path.normpath("data/events.jsonl"),
    os.path.normpath("data/tasks.json"),
    os.path.normpath("data/community_config.json"),
}

# 递归扫描 *.migrated.bak 时跳过的目录（备份/虚拟环境/版本库/缓存）
SKIP_DIRS = {
    ".venv", ".git", "__pycache__", ".codex", ".claude",
    "_backup_20260819_T004", "_dev01_backup_20260819_T004",
}


def _find_migrated_bak(root: str) -> list[str]:
    """收集项目内 *.migrated.bak 文件（跳过备份/venv/git 目录）。"""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(("_backup_", "_dev01_backup_"))
            and not (d.endswith(".bak.test_") or ".bak.test_" in d)
        ]
        for fn in filenames:
            if fn.endswith(".migrated.bak"):
                full = os.path.join(dirpath, fn)
                if os.path.normpath(full) not in PROTECTED:
                    found.append(full)
    return found


def _local_plan() -> tuple[list[str], list[str]]:
    """返回 (待删除本地文件清单, 已不存在的忽略清单)。"""
    root = os.path.dirname(os.path.abspath(__file__))
    to_delete: list[str] = []
    missing: list[str] = []
    for sub, name in LOCAL_TARGETS:
        path = os.path.normpath(os.path.join(root, sub, name))
        if path in PROTECTED:
            continue
        if os.path.exists(path):
            to_delete.append(path)
        else:
            missing.append(path)
    for bak in _find_migrated_bak(root):
        if bak not in to_delete:
            to_delete.append(bak)
    return to_delete, missing


def _confirm(force: bool, to_delete: list[str]) -> bool:
    """二次确认：--yes/--force 跳过；否则交互输入 yes。"""
    if force:
        return True
    print("即将删除以下身份/会话数据（此操作不可撤销）：")
    for p in to_delete:
        print("  -", p)
    try:
        ans = input("确认删除以上文件？输入 yes 继续，其它任意键取消：").strip().lower()
    except EOFError:
        print("未收到确认输入，已取消。")
        return False
    return ans == "yes"


def _run(force: bool) -> int:
    print("=" * 62)
    print("初始化云存储：删除旧账号/会话数据，身份数据改由云端管理")
    print("=" * 62)

    # 1) 确保存储桶存在（私有权限）
    try:
        created = cloud_store.ensure_bucket()
    except cloud_store.CloudStoreError as exc:
        print("[FAIL] 存储桶检查/创建失败：", exc)
        return 1
    print(f"[OK] 存储桶就绪：{os.environ.get('COS_BUCKET', '')}（{'本次新建' if created else '已存在'}，私有权限）")

    # 2) 清空云端旧对象（存在才删）
    for key in (cloud_store.USERS_OBJECT_KEY, cloud_store.SESSIONS_OBJECT_KEY):
        try:
            existed = cloud_store.object_exists(key)
            if existed:
                cloud_store.delete_object(key)
                print(f"[OK] 云端对象已删除：{key}")
            else:
                print(f"[SKIP] 云端对象不存在：{key}")
        except cloud_store.CloudStoreError as exc:
            print(f"[FAIL] 云端对象处理失败（key={key}）：{exc}")
            return 1

    # 3) 删除本地身份/会话残留
    to_delete, _missing = _local_plan()
    if not to_delete:
        print("[SKIP] 本地无待删除的身份/会话残留文件")
    else:
        print(f"[INFO] 待删除本地身份/会话文件 {len(to_delete)} 个：")
        for p in to_delete:
            print("       -", p)
        if not _confirm(force, to_delete):
            print("已取消本地文件删除（云端清理已完成，脚本幂等可重跑）。")
            return 2
        for p in to_delete:
            try:
                os.remove(p)
                print(f"[OK] 已删除：{p}")
            except OSError as exc:
                print(f"[FAIL] 删除失败：{p}：{exc}")
                return 1

    # 4) 摘要（不含任何密钥）
    print("-" * 62)
    print("摘要（不含密钥）：")
    print(f"  存储桶        : {os.environ.get('COS_BUCKET', '')}（region={os.environ.get('COS_REGION', '')}）")
    print(f"  云端对象      : {cloud_store.USERS_OBJECT_KEY} / {cloud_store.SESSIONS_OBJECT_KEY} 已清空（不存在=空库）")
    print(f"  本地删除文件数: {len(to_delete)}")
    print(f"  业务数据      : data/events.jsonl、tasks.json、community_config.json 未触碰")
    print("[DONE] 初始化完成。请确保 .env 中 AUTH_STORE=cloudbase 后重启服务，空库将自动重建 admin。")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    force = any(a in ("--yes", "--force") for a in argv)
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    try:
        return _run(force)
    except cloud_store.CloudStoreError as exc:
        print("[FAIL]", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

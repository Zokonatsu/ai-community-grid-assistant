# -*- coding: utf-8 -*-
"""
scripts/migrate_events_encryption.py
存量 data/events.jsonl + data/tasks.json 敏感字段一次性加密迁移脚本（T20260821-003）。

用法：
  python scripts/migrate_events_encryption.py             # 默认执行迁移
  python scripts/migrate_events_encryption.py --dry-run   # 仅预览，不写文件

行为：
- events.jsonl 加密 description/address/reply/user_id/lat/lng；
  tasks.json 加密 TASK_ENCRYPT_FIELDS（13 个字段，含数值 event_lat/event_lng）。
- 已 `enc:v1:` 开头的字段跳过（幂等，二次运行 0 变更）。
- 执行前将 data/ 整体备份到 data.bak.<时间戳>/；脚本结束（成功或失败）自动清理，
  不留残留（data.bak.* 不匹配 scan_secrets 的备份跳过规则，必须清理）。
- 加密前校验 DATA_ENCRYPTION_KEY：缺失/非法退出码非 0，不触碰任何数据。
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import secure_store  # noqa: E402
from secure_store import (  # noqa: E402
    FIELD_PREFIX,
    SecureStoreError,
    TASK_ENCRYPT_FIELDS,
    TASK_NUMERIC_FIELDS,
    EVENT_ENCRYPT_FIELDS,
    encrypt_field,
)

DATA_DIR = os.path.join(PROJECT_DIR, "data")
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_KEY = 2


def _atomic_write_text(path: str, text: str) -> None:
    """临时文件 + os.replace 原子写，避免迁移中途写坏原文件。"""
    tmp = f"{path}.migrate.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _encrypt_obj_fields(obj: dict, fields, numeric_fields, stats: dict) -> bool:
    """对单条记录逐字段加密；已 enc:v1: 跳过；返回是否有字段新加密。"""
    changed = False
    for field in fields:
        value = obj.get(field)
        if value is None or value == "":
            continue
        if isinstance(value, str) and value.startswith(FIELD_PREFIX):
            continue  # 幂等：已加密字段跳过
        obj[field] = encrypt_field(str(value))
        stats[field] = stats.get(field, 0) + 1
        changed = True
    return changed


def migrate_events(dry_run: bool):
    """迁移 events.jsonl；返回 (新加密字段数, 按字段统计 dict)。"""
    if not os.path.exists(EVENTS_FILE):
        return 0, {}
    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    stats: dict[str, int] = {}
    new_lines: list[str] = []
    for line in lines:
        if not line.strip():
            new_lines.append("")
            continue
        obj = json.loads(line)
        _encrypt_obj_fields(obj, EVENT_ENCRYPT_FIELDS, (), stats)
        new_lines.append(json.dumps(obj, ensure_ascii=False))
    if not dry_run:
        os.makedirs(DATA_DIR, exist_ok=True)
        _atomic_write_text(EVENTS_FILE, ("\n".join(new_lines) + "\n") if new_lines else "")
    return sum(stats.values()), stats


def migrate_tasks(dry_run: bool):
    """迁移 tasks.json；返回 (新加密字段数, 按字段统计 dict)。"""
    if not os.path.exists(TASKS_FILE):
        return 0, {}
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    stats: dict[str, int] = {}
    if isinstance(data, dict):
        for task in data.values():
            if isinstance(task, dict):
                _encrypt_obj_fields(task, TASK_ENCRYPT_FIELDS, TASK_NUMERIC_FIELDS, stats)
    if not dry_run:
        _atomic_write_text(TASKS_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    return sum(stats.values()), stats


def _print_stats(title: str, stats: dict) -> None:
    print(f"  {title}: {sum(stats.values())} 个字段新加密")
    for field in sorted(stats):
        print(f"    - {field}: {stats[field]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="存量 data/events.jsonl + data/tasks.json 敏感字段加密迁移（字段级，AES-256-GCM）。",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅预览将加密的字段数，不写文件、不生成备份",
    )
    args = parser.parse_args(argv)

    # 加密前校验密钥：缺失/非法立即退出非 0，不触碰任何数据
    try:
        secure_store.get_key()
    except SecureStoreError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_KEY

    backup = None
    if not args.dry_run:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = os.path.join(PROJECT_DIR, f"data.bak.{ts}")
        if os.path.exists(DATA_DIR):
            shutil.copytree(DATA_DIR, backup)
            print(f"[备份] data/ -> {backup}（脚本结束自动清理，不留残留）")

    mode = "DRY-RUN" if args.dry_run else "执行"
    try:
        e_total, e_stats = migrate_events(args.dry_run)
        t_total, t_stats = migrate_tasks(args.dry_run)
        print(f"[{mode}] events.jsonl：")
        _print_stats("events.jsonl", e_stats)
        print(f"[{mode}] tasks.json：")
        _print_stats("tasks.json", t_stats)
        total = e_total + t_total
        if total == 0:
            print(f"[{mode}] 0 个字段需要加密（已是 enc:v1: 密文或无敏感字段）——幂等，无需变更。")
        else:
            print(f"[{mode}] 共 {total} 个字段新加密。")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 —— 迁移失败退出非 0
        print(f"[错误] 迁移失败，未写入损坏数据：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if backup and os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
            print(f"[清理] 已删除临时备份 {backup}")


if __name__ == "__main__":
    sys.exit(main())

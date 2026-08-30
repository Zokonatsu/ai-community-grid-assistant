"""
backup.py
数据备份脚本（data/ + secure/）

用法：
    python scripts/backup.py                     # 默认备份到 ./backups，保留最近 7 份
    python scripts/backup.py --out D:/backups    # 指定输出目录
    python scripts/backup.py --keep 14           # 保留最近 14 份
    python scripts/backup.py --include-env       # 额外打包 .env（含密钥，请加密存放）

说明：
    - 仅备份业务运行数据（data/、secure/），不含代码、.venv。
    - secure/ 内为 AES-256-GCM 加密文件，直接备份即可。
    - 建议结合系统计划任务（Windows: 任务计划程序 / Linux: cron）定时执行。
"""
import argparse
import datetime
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SECURE_DIR = os.path.join(ROOT, "secure")
ENV_FILE = os.path.join(ROOT, ".env")
SKIP_NAMES = {"__pycache__", ".git", ".venv", "node_modules"}
SKIP_SUFFIXES = (".lock", ".tmp", ".pyc", ".pyo")


def _iter_files(base):
    if not os.path.isdir(base):
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        for f in filenames:
            if f.endswith(SKIP_SUFFIXES):
                continue
            yield os.path.join(dirpath, f)


def _make_archive(out_dir, include_env):
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    arc_path = os.path.join(out_dir, "backup-%s.zip" % ts)
    count = 0
    with zipfile.ZipFile(arc_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in _iter_files(DATA_DIR):
            zf.write(f, os.path.relpath(f, ROOT))
            count += 1
        for f in _iter_files(SECURE_DIR):
            zf.write(f, os.path.relpath(f, ROOT))
            count += 1
        if include_env and os.path.exists(ENV_FILE):
            zf.write(ENV_FILE, ".env")
            count += 1
        manifest = {
            "created_at": ts,
            "files": count,
            "note": "ai-community-grid-assistant 数据备份。secure/ 为加密文件；若含 .env 请加密存放。",
        }
        zf.writestr("MANIFEST.txt", str(manifest))
    return arc_path, count


def _prune(out_dir, keep):
    if keep <= 0:
        return
    backups = sorted(
        [
            os.path.join(out_dir, f)
            for f in os.listdir(out_dir)
            if f.startswith("backup-") and f.endswith(".zip")
        ],
        key=os.path.getmtime,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="备份 data/ 与 secure/ 业务数据")
    ap.add_argument("--out", default=os.path.join(ROOT, "backups"), help="备份输出目录")
    ap.add_argument("--keep", type=int, default=7, help="保留最近 N 份，默认 7")
    ap.add_argument("--include-env", action="store_true", help="额外打包 .env（含密钥，请加密存放）")
    args = ap.parse_args()
    arc, count = _make_archive(args.out, args.include_env)
    _prune(args.out, args.keep)
    print("[backup] 完成：%s（%d 个文件）" % (arc, count))


if __name__ == "__main__":
    main()

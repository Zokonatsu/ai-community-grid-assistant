"""
restore.py
从 backup.py 生成的备份包恢复 data/ 与 secure/。

用法：
    python scripts/restore.py --file backups/backup-20260830-101500.zip --dry-run   # 演练（预览）
    python scripts/restore.py --file backups/backup-20260830-101500.zip --yes      # 执行恢复

注意：
    - 会覆盖当前 data/ 与 secure/，恢复前建议先手动备份现状。
    - 仅恢复 backup 包内的 data/、secure/（及可选 .env），不影响代码。
"""
import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED_TOP = {"data", "secure"}


def _list(arc_path):
    with zipfile.ZipFile(arc_path) as zf:
        return [n for n in zf.namelist() if not n.endswith("/")]


def main():
    ap = argparse.ArgumentParser(description="从备份包恢复 data/ 与 secure/")
    ap.add_argument("--file", required=True, help="备份 zip 路径")
    ap.add_argument("--dry-run", action="store_true", help="仅列出内容，不实际恢复")
    ap.add_argument("--yes", action="store_true", help="确认恢复（会覆盖现有 data/secure）")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print("[restore] 备份文件不存在：%s" % args.file)
        sys.exit(1)

    names = _list(args.file)
    print("[restore] 备份包 %s 共 %d 个文件" % (args.file, len(names)))
    if args.dry_run:
        for n in names:
            print("   ", n)
        print("[restore] DRY-RUN 结束，未做任何改动。")
        return

    if not args.yes:
        print("[restore] 将覆盖当前 data/、secure/。确认请加 --yes。")
        sys.exit(0)

    with zipfile.ZipFile(args.file) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            top = name.split("/", 1)[0]
            if top not in ALLOWED_TOP:
                continue
            target = os.path.join(ROOT, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                dst.write(src.read())
    print("[restore] 恢复完成。")


if __name__ == "__main__":
    main()

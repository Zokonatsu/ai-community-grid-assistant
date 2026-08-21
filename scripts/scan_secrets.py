# -*- coding: utf-8 -*-
"""
scripts/scan_secrets.py
密钥泄漏扫描（CI frontend-security job 调用；本地可直接复跑）。

检查 1：`.env` 必须被 git 忽略（git check-ignore .env 命中）。
检查 2：扫描仓库内所有「非 .env」文件（排除 .git/.venv/__pycache__/.codex/.claude/
        node_modules/备份目录），断言不存在真实密钥形态（占位 pattern，不内嵌真实值）：
  - COS_SECRET_(ID|KEY)\\s*[:=]
  - DATA_ENCRYPTION_KEY\\s*[:=]\\s*[0-9a-fA-F]{32,}
  - AKID[0-9A-Za-z]{20,}（腾讯云 SecretId 常见前缀）
任一命中 -> 退出码非 0；只报「文件 + pattern 名」，绝不打印命中的值/行内容。

误报排除（文档占位符，非「真实值形态」，与任务书口径一致）：
- COS_SECRET_(ID|KEY) 的整行注释与占位赋值（值为空 / <...> / ${...} / ${{ secrets.X }} 引用）不命中
  （.env.example 示例注释、DEPLOY.md 的 <SecretId>、docker-compose.yml 的 ${COS_SECRET_ID}）；
- DATA_ENCRYPTION_KEY(32+ hex) 与 AKID 前缀在注释中也照常命中（防注释形式泄漏真实密钥）。

安全：本脚本不含、不输出任何真实密钥；COS 密钥 / DATA_ENCRYPTION_KEY 只允许存在于 .env。
"""
import os
import re
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 需要整体跳过的目录名（含备份目录，见任务书 3.4）
SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".codex", ".claude",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
# 备份目录特征（_*_backup_* / *_planner_backup_* / _dev01_backup_*）
BACKUP_MARKERS = ("_backup_", "_planner_backup_", "_dev01_backup_")

# 占位 pattern（只做形状匹配，不含真实值）
PATTERNS = [
    ("COS_SECRET_ID/KEY", re.compile(r"COS_SECRET_(ID|KEY)\s*[:=]")),
    ("DATA_ENCRYPTION_KEY_HEX32", re.compile(r"DATA_ENCRYPTION_KEY\s*[:=]\s*[0-9a-fA-F]{32,}")),
    ("AKID_PREFIX", re.compile(r"AKID[0-9A-Za-z]{20,}")),
]

_PLACEHOLDER_RE = re.compile(r"^<\s*[^>]*\s*>$")   # <SecretId> 等文档占位符
_REFERENCE_RE = re.compile(r"^(?:\$\{\{[^}]*\}\}|\$\{[^}]*\})$")  # ${VAR} / ${{ secrets.X }} 引用


def _is_backup_dir(name: str) -> bool:
    return any(marker in name for marker in BACKUP_MARKERS)


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or _is_backup_dir(name)


def _iter_files(root: str):
    """深度遍历仓库文件，排除指定目录与 .env 本身。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for name in filenames:
            if name == ".env":  # .env 本身不扫描（由 git check-ignore 检查覆盖）
                continue
            yield os.path.join(dirpath, name)


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_doc_placeholder(value: str) -> bool:
    """赋值值为空 / <...> / ${...} -> 文档占位符，非真实密钥形态。"""
    v = value.strip()
    if not v:
        return True
    return bool(_PLACEHOLDER_RE.match(v) or _REFERENCE_RE.match(v))


def _scan_file(path: str) -> list:
    """返回命中列表 [(pattern_name, path)]；只记文件+pattern，不记值。"""
    hits = []
    try:
        text = _read_text(path)
    except OSError:
        return hits
    for line in text.splitlines():
        stripped = line.lstrip()
        is_comment = stripped.startswith("#")
        for name, pattern in PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            # COS_SECRET_(ID|KEY)：整行注释与文档占位赋值不命中；其余两 pattern 注释也命中
            if name == "COS_SECRET_ID/KEY":
                if is_comment:
                    continue
                if _is_doc_placeholder(line[match.end():]):
                    continue
            hits.append((name, path))
            break  # 一行只记一个 pattern
    return hits


def check_gitignore() -> bool:
    """git check-ignore .env 必须命中。"""
    try:
        proc = subprocess.run(
            ["git", "check-ignore", ".env"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except OSError:
        print("[FAIL] git 不可用，无法执行 git check-ignore .env")
        return False
    if proc.returncode != 0:
        print("[FAIL] git check-ignore .env 未命中（.env 存在入库风险）")
        return False
    print(f"[PASS] git check-ignore .env 命中：{proc.stdout.strip() or '.env'}")
    return True


def main(argv=None) -> int:
    git_ok = check_gitignore()
    total = 0
    hits = []
    for path in _iter_files(PROJECT_DIR):
        total += 1
        hits.extend(_scan_file(path))

    if hits:
        print(f"SECRET_SCAN_TOTAL={total} / HITS={len(hits)}")
        for name, path in sorted(set(hits)):
            print(f"  HIT pattern={name} file={os.path.relpath(path, PROJECT_DIR)}")
        print("[FAIL] 检测到疑似密钥形态，请人工核查（本工具不打印值）")
        return 1
    print(f"SECRET_SCAN_TOTAL={total} / NO SECRET HITS")
    if not git_ok:
        return 1
    print("[PASS] 密钥泄漏扫描通过（0 命中 + .env gitignore 校验通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
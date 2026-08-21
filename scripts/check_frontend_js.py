# -*- coding: utf-8 -*-
"""
scripts/check_frontend_js.py
前端内联脚本语法检查（CI frontend-security job 调用；本地可直接复跑）。

做法：
- 解析 static/index.html、static/login.html、static/admin.html；
- 抽取所有「无 src 属性」的内联 <script>...</script> 内容；
- 逐个写入临时 .js 文件并执行 `node --check`（仅语法检查，不执行、不联网）；
- 任一失败 -> 退出码非 0，并列出页面与临时文件名。

依赖：node（CI 由 actions/setup-node@v4 提供，node 20）。
安全：本脚本不读取 .env、不打印任何密钥、全程本地执行。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ["static/index.html", "static/login.html", "static/admin.html"]

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_SRC_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)


def _read_text(path: str) -> str:
    """读取 HTML，优先 UTF-8，失败回退 gb18030（防历史文件编码差异）。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_inline_scripts(html: str) -> list:
    """返回所有无 src 的内联 <script> 正文（去除首尾空白）。"""
    scripts = []
    for match in _SCRIPT_RE.finditer(html):
        attrs, body = match.group(1), match.group(2)
        if _SRC_RE.search(attrs):
            continue  # 外部脚本，跳过
        body = body.strip()
        # 兼容 CDATA / HTML 注释包裹的旧式内联脚本
        if body.startswith("<!--"):
            body = body[4:]
        if body.endswith("//-->"):
            body = body[:-5]
        elif body.endswith("-->"):
            body = body[:-3]
        scripts.append(body.strip())
    return scripts


def check_file(html_path: str, tmp_dir: str) -> tuple:
    """检查单个页面，返回 (ok, 明细列表)。"""
    html = _read_text(html_path)
    scripts = extract_inline_scripts(html)
    if not scripts:
        return True, [f"{html_path}: 未发现内联 <script>（跳过）"]
    details = []
    all_ok = True
    for idx, body in enumerate(scripts, start=1):
        name = f"{os.path.basename(html_path)}_{idx}.js"
        js_path = os.path.join(tmp_dir, name)
        with open(js_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        proc = subprocess.run(
            ["node", "--check", js_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if proc.returncode == 0:
            details.append(f"  PASS {html_path} 内联脚本 #{idx} -> {name}")
        else:
            all_ok = False
            details.append(f"  FAIL {html_path} 内联脚本 #{idx} -> {name}")
            details.append("  " + (proc.stderr or proc.stdout or "").strip().replace("\n", "\n  "))
    return all_ok, details


def main(argv=None) -> int:
    node = shutil.which("node")
    if not node:
        print("[FAIL] 未找到 node 可执行文件（CI 由 actions/setup-node@v4 提供）")
        return 1
    ok_all = True
    with tempfile.TemporaryDirectory(prefix="ci_jscheck_") as tmp_dir:
        for rel in PAGES:
            html_path = os.path.join(PROJECT_DIR, rel)
            if not os.path.exists(html_path):
                print(f"[FAIL] 页面不存在：{rel}")
                ok_all = False
                continue
            ok, details = check_file(html_path, tmp_dir)
            print("\n".join(details))
            ok_all = ok_all and ok
    if ok_all:
        print(f"[PASS] {len(PAGES)} 个页面内联脚本语法检查通过（node={node}）")
        return 0
    print("[FAIL] 存在内联脚本语法错误，详见上方输出")
    return 1


if __name__ == "__main__":
    sys.exit(main())

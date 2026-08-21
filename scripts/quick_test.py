# -*- coding: utf-8 -*-
"""
scripts/quick_test.py — 一键测试入口（测试智能体 tester 的主命令）

用法：
    python scripts/quick_test.py          # 全量（约 4 分钟，上线/发布前必跑）
    python scripts/quick_test.py core     # 快速档（约 1 分钟，日常/开发中）

流程（按顺序执行，任一失败即整体不通过）：
    1. pytest 收集检查（快速失败，防低级语法/导入错误）
    2. 回归测试 tests/run_regression.py（full=全部脚本串行子进程隔离 / core=P0 子集）
    3. 密钥泄露扫描 scripts/scan_secrets.py（要求 0 命中）
    4. 前端 JS 语法检查 scripts/check_frontend_js.py

注意：
    不要用单进程 `python -m pytest -q` 作为门禁：限流专项测试会污染同进程状态，
    产生与真实回归无关的 429 误报（原因见 tests/run_regression.py 注释）。
"""
import os
import subprocess
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

STEPS = [
    ("收集检查", [PY, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"]),
    ("回归(full)", [PY, "tests/run_regression.py", "full"]),
    ("密钥扫描", [PY, "scripts/scan_secrets.py"]),
    ("前端JS检查", [PY, "scripts/check_frontend_js.py"]),
]


def _env():
    e = dict(os.environ)
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e.setdefault("AUTH_STORE", "file")
    e.setdefault("DATA_ENCRYPTION_KEY", "1" * 64)
    e.setdefault("LLM_API_KEY", "test-key")
    e.setdefault("LLM_BASE_URL", "http://test")
    e.setdefault("RATE_LIMIT_ENABLED", "false")
    return e


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "full").lower()
    steps = list(STEPS)
    if mode == "core":
        steps[1] = ("回归(core)", [PY, "tests/run_regression.py", "core"])
    elif mode != "full":
        print(__doc__)
        return 2

    print("=" * 70)
    print(f"  一键测试：{mode}（{len(steps)} 步）")
    print("=" * 70)
    results = []
    for name, cmd in steps:
        t0 = time.time()
        print(f"\n>> [{name}] 运行中 ...", flush=True)
        try:
            proc = subprocess.run(
                cmd, cwd=PROJECT, env=_env(), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=1800,
            )
            ok = proc.returncode == 0
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-12:]
            if tail:
                print("\n".join(tail))
        except subprocess.TimeoutExpired:
            ok = False
            print("[超时] 该步骤超过 1800s")
        results.append((name, ok, time.time() - t0))
        print(f"\n[{'PASS' if ok else 'FAIL'}] {name}（{time.time() - t0:.1f}s）", flush=True)

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 70)
    if failed:
        print(f"  结果：{len(results) - len(failed)}/{len(results)} 通过，失败项：{', '.join(r[0] for r in failed)}")
        return 1
    print(f"  结果：{len(results)}/{len(results)} 全部通过（OK）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
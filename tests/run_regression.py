# -*- coding: utf-8 -*-
"""
tests/run_regression.py
统一回归 runner（core / full / cov 三档）。

用法：
    python tests/run_regression.py core   # 11 个 P0 核心脚本（串行）
    python tests/run_regression.py full   # 全部 20 个脚本（test_server 默认 skip）
    python tests/run_regression.py cov    # pytest-cov 覆盖率报告（auth/cloud_store/main）

特性：
- 每个脚本在独立子进程中调用 pytest（严格串行，避免 data/secure 目录互踩与模块状态串扰）；
- 输出逐脚本 通过/失败 + 耗时；末尾汇总（N 通过 / M 失败 / 总耗时 / 残留备份检查）；
- 任一脚本失败或存在残留备份 -> 退出码非 0。
"""
import os
import subprocess
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(PROJECT_DIR, "tests")

# core：P0 高价值核心回归集（与任务书一致）
CORE = [
    "test_auth.py",
    "test_security_fixes.py",
    "test_data_isolation.py",
    "test_cloud_store.py",
    "test_register_location.py",
    "test_event_cancel.py",
    "test_comprehensive.py",
    "test_semantic_timeout.py",
    "test_input_validation.py",
    "test_security_authorization.py",  # T20260820-001-TB：越权矩阵 + 八项攻防
    "test_chain_breaks.py",            # T20260820-001-TB：四类断链
]

# full：全部 20 个脚本（test_server 默认 skip；mutation_effectiveness 仅入 full；
# test_infra_strength.py 崩溃保护强度测试仅入 full，不入 CORE 保 CI core 快速稳定）
_ALL = sorted(
    f for f in os.listdir(TESTS_DIR) if f.startswith("test_") and f.endswith(".py")
)
FULL = CORE + [f for f in _ALL if f not in CORE]

# 每个脚本子进程超时上限（秒）；全量集 ≤15 分钟整体预算
SCRIPT_TIMEOUT = 900

# cov：核心模块覆盖率目标（pytest-cov）
COV_MODULES = ["auth", "cloud_store", "main"]


def _env():
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("AUTH_STORE", "file")
    env.setdefault("DATA_ENCRYPTION_KEY", "1" * 64)
    env.setdefault("LLM_API_KEY", "test-key")
    env.setdefault("LLM_BASE_URL", "http://test")
    return env


def _residue_names():
    """残留备份：匹配 *.bak.* 以及 conftest 备份前缀 _pytest_bak_*。"""
    out = []
    for name in os.listdir(PROJECT_DIR):
        if ".bak" in name or name.startswith("_pytest_bak_"):
            out.append(name)
    return sorted(out)


def run_script(name: str):
    """在独立子进程运行单个脚本的 pytest 收集，返回 (ok, elapsed, output)。"""
    script = os.path.join(TESTS_DIR, name)
    cmd = [sys.executable, "-m", "pytest", script, "-q", "-p", "no:cacheprovider"]
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            env=_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCRIPT_TIMEOUT,
        )
        ok = proc.returncode == 0
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        ok = False
        output = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        output += f"\n[FATAL] 脚本超时（>{SCRIPT_TIMEOUT}s）"
    elapsed = time.time() - start
    return ok, elapsed, output


def run_cov() -> int:
    """可选 cov 子命令：跑 pytest-cov 覆盖率（核心模块 ≥80% 目标）。

    注：单进程全量 pytest 下 test_scene_tag 存在既有兼容问题（main.receive_node
    import 期绑定被先导入模块污染，与 T-B 无关），故 cov 档按文件排除该脚本。
    """
    cmd = [
        sys.executable, "-m", "pytest",
        *["--cov=" + m for m in COV_MODULES],
        "--cov-report=term-missing",
        TESTS_DIR, "-q", "-p", "no:cacheprovider",
        "--ignore=" + os.path.join(TESTS_DIR, "test_scene_tag.py"),
    ]
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=PROJECT_DIR, env=_env(), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=SCRIPT_TIMEOUT * 2,
        )
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        output += f"\n[FATAL] 覆盖率执行超时"
        proc = None
    print(output[-6000:])
    if proc is not None and proc.returncode != 0:
        print("覆盖率执行失败，详见上方输出")
        return 1
    # 尝试解析汇总行（TOTAL ... %）判断是否达标（仅供参考，正式以 pytest-cov 报告为准）
    total_line = [ln for ln in output.splitlines() if ln.strip().startswith("TOTAL")]
    if total_line:
        print("覆盖率汇总（TOTAL 行）：" + total_line[-1].strip())
    print(f"cov 子命令耗时 {time.time() - start:.1f}s")
    return 0


def tail(output: str, n: int = 25) -> str:
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "cov":
        return run_cov()
    if argv[0] not in ("core", "full"):
        print(__doc__)
        return 2
    profile = argv[0]
    scripts = CORE if profile == "core" else FULL

    print("=" * 74)
    print(f"  回归测试：{profile}（{len(scripts)} 个脚本，串行）")
    print("=" * 74)

    results = []
    total_start = time.time()
    for name in scripts:
        ok, elapsed, output = run_script(name)
        results.append((name, ok, elapsed))
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name:<36} {elapsed:6.1f}s")
        if not ok:
            print("-" * 74)
            print(tail(output, n=30))
            print("-" * 74)

    total_elapsed = time.time() - total_start
    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed

    # 残留备份检查
    residue = _residue_names()
    residue_ok = len(residue) == 0

    print("=" * 74)
    print(f"  汇总：{passed} 通过 / {failed} 失败 / 总耗时 {total_elapsed:.1f}s")
    print(f"  残留备份检查={ '无' if residue_ok else ('发现: ' + ', '.join(residue)) }")
    print("=" * 74)

    return 1 if (failed > 0 or not residue_ok) else 0


if __name__ == "__main__":
    sys.exit(main())

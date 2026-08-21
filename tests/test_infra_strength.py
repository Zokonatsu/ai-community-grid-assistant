# -*- coding: utf-8 -*-
"""
test_infra_strength.py —— 测试基建强度测试（T20260820-001-TD，入 FULL 不入 CORE）。

目标：固化 conftest module 级 autouse `data_isolation` 的崩溃保护——
测试模块内抛异常（探针故意 assert False）后：
  1) 探针子进程退出码非 0（探针确实失败）；
  2) 项目根目录无「新增/消失」备份残留（_pytest_bak_* / data.bak.* / secure.bak.*）
     ——本模块自身运行中的活备份（_pytest_bak_data_<pid>_*）是正常备份，不计入
     残留；探针子进程的备份必须在其 finally 中恢复，不得新增残留，也不得误删
     外层活备份（嵌套运行安全，见 conftest _cleanup_residue 注释）；
  3) data/、secure/ 快照（文件清单 + SHA256）与运行前一致（恢复原样）。

实现：子进程运行 `python -m pytest tests/_infra_probe_fail.py -q`；
conftest 的 data_isolation 自动生效。
"""
import hashlib
import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(PROJECT_DIR, "tests")
PROBE_FILE = os.path.join(TESTS_DIR, "_infra_probe_fail.py")
DATA_DIR = os.path.join(PROJECT_DIR, "data")
SECURE_DIR = os.path.join(PROJECT_DIR, "secure")


def _snapshot_dir(path):
    """返回 {相对路径: sha256} 文件清单；目录缺失返回 None（不应发生）。"""
    if not os.path.isdir(path):
        return None
    snap = {}
    for _root, _dirs, files in os.walk(path):
        for name in sorted(files):
            full = os.path.join(_root, name)
            rel = os.path.relpath(full, path).replace("\\", "/")
            h = hashlib.sha256()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            snap[rel] = h.hexdigest()
    return snap


def _residue_names():
    """项目根目录下所有备份残留名（与 conftest _is_bak_residue 口径一致）。"""
    return sorted(
        n for n in os.listdir(PROJECT_DIR)
        if n.startswith("_pytest_bak_")
        or n.startswith("data.bak.")
        or n.startswith("secure.bak.")
    )


def test_crash_protection_probe_failure_leaves_no_residue():
    # 在 data/secure 写入标记文件，让「恢复原样」比较更有意义
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SECURE_DIR, exist_ok=True)
    marker_data = os.path.join(DATA_DIR, "_strength_marker.txt")
    marker_secure = os.path.join(SECURE_DIR, "_strength_marker.txt")
    with open(marker_data, "w", encoding="utf-8") as fh:
        fh.write("data-marker-1\n")
    with open(marker_secure, "w", encoding="utf-8") as fh:
        fh.write("secure-marker-1\n")

    # 运行前快照（含本模块自身活备份的集合，供嵌套运行安全对照）
    before_residue = set(_residue_names())
    before_data = _snapshot_dir(DATA_DIR)
    before_secure = _snapshot_dir(SECURE_DIR)
    assert before_data is not None and before_secure is not None, "data/ secure/ 应存在"

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", PROBE_FILE, "-q", "-p", "no:cacheprovider"],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")

    # 1) 探针必须失败（故意 assert False），证明崩溃场景真实发生
    assert proc.returncode != 0, (
        f"探针子进程应失败但退出码=0；输出尾：{combined[-1500:]!r}"
    )
    assert "assert False" in combined, "探针输出应包含断言失败信息"

    # 2) 无新增/消失备份残留（本模块活备份必须原样保留，探针不得误删/新增）
    after_residue = set(_residue_names())
    assert after_residue == before_residue, (
        "项目根目录备份状态变化: "
        f"新增={sorted(after_residue - before_residue)} "
        f"消失={sorted(before_residue - after_residue)}"
    )

    # 3) data/secure 快照（文件清单 + SHA256）与运行前一致（恢复原样）
    assert _snapshot_dir(DATA_DIR) == before_data, "data/ 与运行前不一致（探针崩溃后未恢复原样）"
    assert _snapshot_dir(SECURE_DIR) == before_secure, "secure/ 与运行前不一致（探针崩溃后未恢复原样）"

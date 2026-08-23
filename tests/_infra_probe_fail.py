# -*- coding: utf-8 -*-
"""
tests/_infra_probe_fail.py —— 崩溃保护探针模块（T20260820-001-TD 专用）。

命名以 `_` 开头，不匹配 pytest.ini 的 `python_files = test_*.py` 收集规则，
因此不会被常规收集（`pytest tests` 会忽略本文件）；仅由 tests/test_infra_strength.py
显式以子进程方式运行：`python -m pytest tests/_infra_probe_fail.py -q`。

用途：模拟「测试模块内抛异常」的崩溃场景——本模块一个用例故意 `assert False`，
用于验证 conftest 的 module 级 autouse `data_isolation` 在测试失败后仍能
try/finally 恢复 data/、secure/ 且不残留 _pytest_bak_* / *.bak.*。
"""


def test_probe_intentional_failure():
    """故意失败的探针用例：强度测试断言子进程退出码非 0。"""
    assert False, "T-D 探针故意失败：用于验证 data_isolation 崩溃保护"

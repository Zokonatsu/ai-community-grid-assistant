# -*- coding: utf-8 -*-
"""
tests/conftest.py
pytest 测试基础设施：环境固定 + 数据隔离（module 级 autouse fixture）。

一、环境固定（本文件 import 期执行，早于任何 test 模块导入 config/auth/main）：
    - AUTH_STORE=file
    - DATA_ENCRYPTION_KEY（64 位 hex 固定测试值）
    - LLM_API_KEY / LLM_BASE_URL
    刻意不读取 .env，保证 CI 无 .env 时同样可运行。

二、数据隔离（module 级 autouse fixture `data_isolation`，try/finally 崩溃保护）：
    1. 清理上次残留 data.bak.* / secure.bak.* / _pytest_bak_*（防上次崩溃残留；
       仅清理属主进程已退出的陈旧 _pytest_bak_*，保留嵌套/并行运行中的活备份）；
    2. 备份当前 data/、secure/ 到独立目录（命名含 _pytest_bak_，不匹配 *.bak.* 清理规则）；
    3. 新建空 data/、secure/；
    4. yield 运行测试；
    5. finally：删除临时目录 -> 恢复备份目录 -> 断言无 *.bak.* 残留。

三、公共 helper（供 T-B 扩展）：
    - resident_pair：A/B 两居民 token 对（最小实现，越权测试用）。
"""
import os
import shutil
import uuid

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
SECURE_DIR = os.path.join(PROJECT_DIR, "secure")

# ------------------------------------------------------------------
# 环境固定：必须在任何 config/auth/main 导入之前设置，且不读 .env
# ------------------------------------------------------------------
os.environ["AUTH_STORE"] = "file"
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"


# ------------------------------------------------------------------
# 数据隔离工具
# ------------------------------------------------------------------
def _is_bak_residue(name: str) -> bool:
    """判断是否为需要清理/检查的备份残留名。

    覆盖两类：
    - 脚本直跑模式命名：data.bak.* / secure.bak.*（匹配 *.bak.*）
    - conftest 备份命名：_pytest_bak_data_* / _pytest_bak_secure_*
    """
    return (
        name.startswith("data.bak.")
        or name.startswith("secure.bak.")
        or name.startswith("_pytest_bak_data_")
        or name.startswith("_pytest_bak_secure_")
    )


def _remove_path(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _backup_owner_pid(name: str):
    """从 conftest 备份名 `_pytest_bak_data_<pid>_<uuid>` 解析属主进程 PID。

    解析失败（如直跑模式遗留的 data.bak.* / secure.bak.*，或旧版无 PID 命名）
    返回 None，调用方按「陈旧残留」处理。
    """
    parts = name.split("_")
    # parts = ["", "pytest", "bak", "data", "<pid>", "<uuid>"]
    if len(parts) < 6:
        return None
    try:
        return int(parts[4])
    except ValueError:
        return None


def _is_pid_alive(pid: int) -> bool:
    """跨平台进程存活检测（Windows 用 OpenProcess，POSIX 用 os.kill(pid, 0)）。

    注：Windows 上 os.kill(pid, 0) 会把信号当作真实终止信号处理，不能用于
    存在性探测，故用 ctypes.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        k32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_live_backup(name: str) -> bool:
    """备份名属主进程是否存活（活备份=嵌套/并行运行中，非残留）。"""
    pid = _backup_owner_pid(name)
    return pid is not None and _is_pid_alive(pid)


def _cleanup_residue() -> None:
    """清理残留备份（嵌套/崩溃场景安全版，T20260820-001-TD）。

    - data.bak.* / secure.bak.*（直跑模式遗留，无属主信息）→ 直接删除；
    - _pytest_bak_*（conftest 备份，命名含属主 PID）→ 仅删除「属主进程已退出」
      的陈旧备份；跳过仍存活进程的活备份，避免嵌套/并行 pytest 子进程误删
      外层正在使用的真实 data/secure（修复 T-B 观察：多进程/直跑组合运行后
      data/secure 曾被遗留为测试态）。
    """
    for name in os.listdir(PROJECT_DIR):
        if _is_bak_residue(name):
            if name.startswith("_pytest_bak_"):
                pid = _backup_owner_pid(name)
                if pid is not None and _is_pid_alive(pid):
                    continue  # 活备份（嵌套/并行运行中），不删除
            _remove_path(os.path.join(PROJECT_DIR, name))


def _residue_names() -> list:
    return [n for n in os.listdir(PROJECT_DIR) if _is_bak_residue(n)]


@pytest.fixture(scope="module", autouse=True)
def data_isolation():
    """module 级自动数据隔离：备份/恢复 data 与 secure，try/finally 崩溃保护。"""
    _cleanup_residue()

    bak_id = f"{os.getpid()}_{uuid.uuid4().hex[:12]}"
    bak_data = os.path.join(PROJECT_DIR, f"_pytest_bak_data_{bak_id}")
    bak_secure = os.path.join(PROJECT_DIR, f"_pytest_bak_secure_{bak_id}")

    # 备份当前目录（改名到独立备份目录，命名不匹配 *.bak.*）
    os.rename(DATA_DIR, bak_data) if os.path.exists(DATA_DIR) else os.makedirs(bak_data)
    os.rename(SECURE_DIR, bak_secure) if os.path.exists(SECURE_DIR) else os.makedirs(bak_secure)

    # 新建空目录
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SECURE_DIR, exist_ok=True)

    try:
        yield
    finally:
        # 删除临时目录
        _remove_path(DATA_DIR)
        _remove_path(SECURE_DIR)
        # 恢复备份目录
        if os.path.exists(bak_data):
            os.rename(bak_data, DATA_DIR)
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(bak_secure):
            os.rename(bak_secure, SECURE_DIR)
        else:
            os.makedirs(SECURE_DIR, exist_ok=True)
        # 断言无「陈旧」残留：仅允许仍存活进程的活备份（嵌套/并行 pytest
        # 场景），本运行自身产生的备份此时已恢复；属主进程已退出的备份不应残留。
        stale = [n for n in _residue_names() if not _is_live_backup(n)]
        assert stale == [], f"数据隔离恢复后仍存在备份残留: {stale}"


# ------------------------------------------------------------------
# 公共 helper（最小实现，T-B 扩展）
# ------------------------------------------------------------------
@pytest.fixture(scope="module")
def resident_pair():
    """A/B 两居民 token 对（最小公共 helper，供越权测试）。

    本期最小实现：调用本项目的 auth 模块注册两位测试居民并登录。
    T-B 阶段可扩展为返回完整用户对象/事件种子数据。
    """
    import importlib
    import auth

    importlib.reload(auth)
    ok_a, _, user_a = auth.register_user(
        username="resident_a_pair", password="test123456", real_name="居民A",
        phone="13900000091", role="resident", building="1栋", unit="1单元", room="101",
        register_lat=30.274150, register_lng=120.155150,
    )
    ok_b, _, user_b = auth.register_user(
        username="resident_b_pair", password="test123456", real_name="居民B",
        phone="13900000092", role="resident", building="1栋", unit="1单元", room="102",
        register_lat=30.274150, register_lng=120.155150,
    )
    if not (ok_a and ok_b):
        pytest.fail(f"resident_pair 预置用户失败: A={ok_a} B={ok_b}")

    _, _, login_a = auth.login_user("resident_a_pair", "test123456")
    _, _, login_b = auth.login_user("resident_b_pair", "test123456")
    return {
        "resident_a": {"token": login_a["token"], "user": user_a},
        "resident_b": {"token": login_b["token"], "user": user_b},
    }

# ------------------------------------------------------------------
# 公共 helper（T-B 扩展：admin token / 事件种子，最小实现）
# ------------------------------------------------------------------
@pytest.fixture(scope="module")
def admin_token():
    """默认管理员 token（空库初始化自动创建 admin/admin123456）。

    仅在需要后台管理员身份的用例中使用；不得用于绕过居民越权断言。
    """
    import importlib
    import auth

    importlib.reload(auth)
    ok, msg, data = auth.login_user("admin", "admin123456")
    if not ok or not data or not data.get("token"):
        pytest.fail(f"admin_token 预置失败: {msg}")
    return data["token"]


@pytest.fixture(scope="module")
def event_seed():
    """事件种子 helper：返回 create_event(client, token, description=...) -> event_id。

    调用方需自行构建 TestClient（并确保 receive_node 已 mock 为有效语义结果），
    本 helper 仅负责「提交事件并断言成功、返回 event_id」，不触碰 env/数据隔离。
    """
    def create_event(client, token, description="小区楼下下水道堵了", **extra):
        payload = {"description": description}
        payload.update(extra)
        resp = client.post(
            "/api/events",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        assert data.get("success"), f"事件种子失败: HTTP {resp.status_code} {data}"
        assert data.get("data", {}).get("event_id"), f"事件种子缺少 event_id: {data}"
        return data["data"]["event_id"]

    return create_event

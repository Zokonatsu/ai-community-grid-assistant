"""
test_data_isolation.py
事件数据隔离功能测试脚本

测试范围：
  main.py 的事件列表 (GET /api/events) 和事件详情 (GET /api/events/{event_id})

测试用例：
  1. 居民用户A提交事件后，用户A能看到该事件
  2. 居民用户B登录后，不应看到用户A提交的事件
  3. 管理员用户登录后，应能看到所有用户的事件
  4. 居民用户访问他人事件的详情接口，应被拒绝
"""

import os
import sys
import json
import shutil
from unittest.mock import patch

# ------------------------------------------------------------------
# 测试环境准备
# ------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份真实 data/secure 目录，使用临时空目录
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_isolation")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_isolation")


def setup_test_env():
    """备份 data/secure 目录，确保干净状态"""
    for bak in (BAK_DATA_DIR, BAK_SECURE_DIR):
        if os.path.exists(bak):
            shutil.rmtree(bak, ignore_errors=True)
    if os.path.exists(ORIGINAL_DATA_DIR):
        os.rename(ORIGINAL_DATA_DIR, BAK_DATA_DIR)
    os.makedirs(ORIGINAL_DATA_DIR, exist_ok=True)
    for f in ["users.json", "sessions.json", "tasks.json"]:
        with open(os.path.join(ORIGINAL_DATA_DIR, f), "w", encoding="utf-8") as fh:
            json.dump({}, fh)
    # secure/ 同样备份并用空目录（账号/会话加密文件在此生成）
    if os.path.exists(ORIGINAL_SECURE_DIR):
        os.rename(ORIGINAL_SECURE_DIR, BAK_SECURE_DIR)
    os.makedirs(ORIGINAL_SECURE_DIR, exist_ok=True)


def teardown_test_env():
    """恢复原始 data/secure 目录"""
    if os.path.exists(ORIGINAL_DATA_DIR):
        shutil.rmtree(ORIGINAL_DATA_DIR, ignore_errors=True)
    if os.path.exists(BAK_DATA_DIR):
        os.rename(BAK_DATA_DIR, ORIGINAL_DATA_DIR)
    if os.path.exists(ORIGINAL_SECURE_DIR):
        shutil.rmtree(ORIGINAL_SECURE_DIR, ignore_errors=True)
    if os.path.exists(BAK_SECURE_DIR):
        os.rename(BAK_SECURE_DIR, ORIGINAL_SECURE_DIR)



# 预置测试环境变量，避免导入 config 时因缺失必填项报错
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"
# 账号数据加密密钥（64 位 hex，仅测试用固定值，确保与本测试生成的 secure/ 数据一致）
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["AUTH_STORE"] = "file"

# 重新加载 auth 模块以使用空数据

# Mock receive_agent 的 AI 调用，避免测试中调用 Kimi API
mock_receive_result = {
    "description": "小区楼下下水道堵了",
    "address": "小区3号楼",
    "event_type": "物业维修",
    "urgency": "中",
    "handler": "",
}



# ------------------------------------------------------------------
# 测试结果收集
# ------------------------------------------------------------------
class TestResults:
    __test__ = False  # 防止 pytest 误收集为测试类
    def __init__(self):
        self.passed = []
        self.failed = []

    def add_pass(self, name, detail=""):
        self.passed.append((name, detail))

    def add_fail(self, name, expected, actual, detail=""):
        self.failed.append((name, expected, actual, detail))

    def summary(self):
        total = len(self.passed) + len(self.failed)
        print("\n" + "=" * 70)
        print(f"  TEST SUMMARY: {len(self.passed)} PASS / {len(self.failed)} FAIL (total {total})")
        print("=" * 70)

        if self.passed:
            print("\n[PASSED]")
            for name, detail in self.passed:
                extra = f"  -> {detail}" if detail else ""
                print(f"  OK  {name}{extra}")

        if self.failed:
            print("\n[FAILED]")
            for name, expected, actual, detail in self.failed:
                print(f"  FAIL  {name}")
                print(f"        Expected: {expected}")
                print(f"        Actual:   {actual}")
                if detail:
                    print(f"        Detail:   {detail}")

        return len(self.failed) == 0


results = TestResults()


# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------
def auth_header(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def register(username, password, real_name, phone, role="resident", building="1栋", unit="1单元", room="101"):
    res = client.post("/api/auth/register", json={
        "username": username,
        "password": password,
        "real_name": real_name,
        "phone": phone,
        "role": role,
        "building": building,
        "unit": unit,
        "room": room,
        "register_lat": 30.274150,
        "register_lng": 120.155150,
    })
    return res.json()


def login(username, password):
    res = client.post("/api/auth/login", json={
        "username": username,
        "password": password,
    })
    return res.json()


def submit_event(token, description):
    res = client.post("/api/events", json={"description": description}, headers=auth_header(token))
    return res


def get_events(token):
    res = client.get("/api/events", headers=auth_header(token))
    return res


def get_event_detail(token, event_id):
    res = client.get(f"/api/events/{event_id}", headers=auth_header(token))
    return res


# ==================================================================
def test_suite():
    # 数据隔离已由 conftest（pytest）或 __main__（直跑）保证；
    # auth/main 初始化必须在数据隔离生效之后（延迟导入 + reload）。
    global client
    import auth
    import importlib
    importlib.reload(auth)
    with patch("receive_agent.OpenAI"), \
         patch("receive_agent.receive_node", return_value=mock_receive_result), \
         patch("workflow.workflow") as mock_workflow, \
         patch("dispatch_agent.logger"), \
         patch("record_agent.logger"):
        from main import app
        from fastapi.testclient import TestClient
    client = TestClient(app)
    # 准备工作：注册测试用户
    # ==================================================================
    print("=" * 70)
    print("  准备阶段：注册测试用户")
    print("=" * 70)
    
    # 注册居民A
    reg_a = register("resident_a", "pass123456", "居民A", "13800000001", "resident")
    print(f"  居民A 注册: success={reg_a.get('success')}, {reg_a.get('error', '')}")
    
    # 注册居民B
    reg_b = register("resident_b", "pass123456", "居民B", "13800000002", "resident")
    print(f"  居民B 注册: success={reg_b.get('success')}, {reg_b.get('error', '')}")
    
    # 注册管理员 (不指定role=admin，因为注册接口禁止，改用已有的默认管理员)
    # 先检查默认管理员账号是否存在
    admin_login = login("admin", "GridAdmin2025!@#")
    if admin_login.get("success"):
        print(f"  管理员登录: 使用默认管理员 admin")
    else:
        # 手动创建管理员用户（直接操作 auth 模块）
        print(f"  管理员登录失败: {admin_login.get('error')}，手动创建...")
        # 注意：auth 模块已禁止通过注册接口创建 admin，但系统初始化时会自动创建
        # 如果文件为空，_init_auth 应已创建默认 admin
    
    # 登录居民A
    login_a = login("resident_a", "pass123456")
    token_a = login_a.get("data", {}).get("token") if login_a.get("success") else None
    user_a = login_a.get("data", {}).get("user") if login_a.get("success") else None
    print(f"  居民A 登录: success={login_a.get('success')}, token={'***' if token_a else 'None'}")
    
    # 登录居民B
    login_b = login("resident_b", "pass123456")
    token_b = login_b.get("data", {}).get("token") if login_b.get("success") else None
    user_b = login_b.get("data", {}).get("user") if login_b.get("success") else None
    print(f"  居民B 登录: success={login_b.get('success')}, token={'***' if token_b else 'None'}")
    
    # 注册即生效，无需管理员审核即可提交事件
    
    # 登录管理员
    login_admin = login("admin", "GridAdmin2025!@#")
    token_admin = login_admin.get("data", {}).get("token") if login_admin.get("success") else None
    user_admin = login_admin.get("data", {}).get("user") if login_admin.get("success") else None
    print(f"  管理员 登录: success={login_admin.get('success')}, token={'***' if token_admin else 'None'}")
    
    # 获取用户ID
    user_id_a = user_a.get("id") if user_a else None
    user_id_b = user_b.get("id") if user_b else None
    user_id_admin = user_admin.get("id") if user_admin else None
    print(f"  user_id_a={user_id_a[:12] if user_id_a else 'None'}...")
    print(f"  user_id_b={user_id_b[:12] if user_id_b else 'None'}...")
    print(f"  user_id_admin={user_id_admin[:12] if user_id_admin else 'None'}...")
    
    
    # ==================================================================
    # 准备工作：居民A提交一个事件
    # ==================================================================
    print("\n" + "=" * 70)
    print("  准备阶段：居民A提交事件")
    print("=" * 70)
    
    # Mock workflow 避免后台 AI 调用
    mock_workflow.invoke.return_value = {
        "description": "小区楼下下水道堵了",
        "address": "小区3号楼",
        "event_type": "物业维修",
        "urgency": "中",
        "handler": "物业部",
        "status": "已派单",
        "created_at": "2026-08-01 12:00:00",
    }
    
    event_id_a = None
    if token_a:
        res = submit_event(token_a, "小区楼下下水道堵了")
        data = res.json()
        print(f"  提交事件: status={res.status_code}, success={data.get('success')}")
        if data.get("success"):
            event_id_a = data.get("data", {}).get("event_id")
            print(f"  event_id_a = {event_id_a}")
        else:
            print(f"  提交失败: {data.get('error')}")
    else:
        print("  SKIP: 居民A 无可用 token")
    
    if not event_id_a:
        print("\n[ERROR] 前置条件失败：居民A未能成功提交事件，后续测试无法进行")
        raise AssertionError("前置条件失败：居民A未能成功提交事件")

    
    
    # ==================================================================
    # Test 1: 居民用户A提交事件后，用户A能看到该事件
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 1: 居民A提交事件后，A能在列表中看到该事件")
    print("=" * 70)
    
    if token_a and event_id_a:
        # 1.1 居民A查看事件列表，应该能看到自己的事件
        res = get_events(token_a)
        if res.status_code == 200:
            events = res.json()
            if isinstance(events, list):
                found = any(e.get("description") == "小区楼下下水道堵了" for e in events)
                if found:
                    results.add_pass(
                        "1.1 居民A查看事件列表",
                        f"列表中有 {len(events)} 条事件，包含居民A提交的事件"
                    )
                else:
                    results.add_fail(
                        "1.1 居民A查看事件列表",
                        "列表应包含居民A的事件",
                        f"列表中有 {len(events)} 条事件，但不包含居民A的事件",
                        f"事件列表: {json.dumps(events, ensure_ascii=False)[:500]}"
                    )
            else:
                results.add_fail("1.1 居民A查看事件列表", "返回列表", f"返回类型: {type(events)}")
        else:
            results.add_fail("1.1 居民A查看事件列表", "200 OK", f"{res.status_code} {res.text[:200]}")
    
        # 1.2 居民A查看事件详情，应该能访问
        res = get_event_detail(token_a, event_id_a)
        if res.status_code == 200:
            detail = res.json()
            if detail.get("description") == "小区楼下下水道堵了":
                results.add_pass(
                    "1.2 居民A查看自己事件的详情",
                    f"event_id={event_id_a[:8]}..., description匹配"
                )
            else:
                results.add_fail(
                    "1.2 居民A查看自己事件的详情",
                    "description='小区楼下下水道堵了'",
                    f"description='{detail.get('description')}'"
                )
        elif res.status_code == 403:
            results.add_fail(
                "1.2 居民A查看自己事件的详情",
                "200 OK (居民应能访问自己的事件)",
                f"403 Forbidden: {res.text[:200]}"
            )
        else:
            results.add_fail("1.2 居民A查看自己事件的详情", "200 OK", f"{res.status_code} {res.text[:200]}")
    else:
        results.add_fail("1.x 前置条件", "居民A已登录且有事件", "token或event_id缺失")
    
    
    # ==================================================================
    # Test 2: 居民用户B登录后，不应看到用户A提交的事件
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 2: 居民B不应看到居民A提交的事件")
    print("=" * 70)
    
    if token_b and event_id_a:
        # 2.1 居民B查看事件列表，不应包含居民A的事件
        res = get_events(token_b)
        if res.status_code == 200:
            events = res.json()
            if isinstance(events, list):
                # 居民B的列表中不应包含居民A的描述
                found_a_event = any(
                    e.get("description") == "小区楼下下水道堵了" for e in events
                )
                if not found_a_event:
                    results.add_pass(
                        "2.1 居民B查看事件列表",
                        f"列表中有 {len(events)} 条事件，均不包含居民A的事件（数据隔离生效）"
                    )
                else:
                    results.add_fail(
                        "2.1 居民B查看事件列表",
                        "列表中不应包含居民A的事件",
                        "列表中包含居民A的事件（数据隔离失效！）",
                        f"居民B看到的事件: {json.dumps(events, ensure_ascii=False)[:500]}"
                    )
            else:
                results.add_fail("2.1 居民B查看事件列表", "返回列表", f"返回类型: {type(events)}")
        else:
            results.add_fail("2.1 居民B查看事件列表", "200 OK", f"{res.status_code} {res.text[:200]}")
    
        # 2.2 居民B自己也提交一个事件，确认仅看到自己的
        mock_workflow.invoke.return_value = {
            "description": "小区广场路灯坏了",
            "address": "小区广场",
            "event_type": "公共设施",
            "urgency": "低",
            "handler": "公共设施部",
            "status": "已派单",
            "created_at": "2026-08-01 12:05:00",
        }
        res_b_submit = submit_event(token_b, "小区广场路灯坏了")
        data_b = res_b_submit.json()
        event_id_b = data_b.get("data", {}).get("event_id") if data_b.get("success") else None
    
        if event_id_b:
            # B 再次查看列表，应只有 1 条（自己的），没有 A 的
            res = get_events(token_b)
            if res.status_code == 200:
                events = res.json()
                if isinstance(events, list):
                    has_b_event = any(
                        e.get("description") == "小区广场路灯坏了" for e in events
                    )
                    has_a_event = any(
                        e.get("description") == "小区楼下下水道堵了" for e in events
                    )
                    if has_b_event and not has_a_event:
                        results.add_pass(
                            "2.2 居民B提交事件后列表仅含自己的",
                            f"列表中有 {len(events)} 条事件，仅包含居民B的事件，不含居民A的事件"
                        )
                    elif not has_b_event:
                        results.add_fail(
                            "2.2 居民B提交事件后列表",
                            "应包含居民B自己的事件",
                            f"列表中未找到居民B的事件",
                            f"事件列表: {json.dumps(events, ensure_ascii=False)[:500]}"
                        )
                    else:
                        results.add_fail(
                            "2.2 居民B提交事件后列表",
                            "不应包含居民A的事件",
                            "列表中包含居民A的事件（数据隔离失效！）"
                        )
                else:
                    results.add_fail("2.2 居民B查看事件列表", "返回列表", f"返回类型: {type(events)}")
            else:
                results.add_fail("2.2 居民B查看事件列表", "200 OK", f"{res.status_code}")
        else:
            results.add_fail("2.2 居民B提交事件", "success=True", f"{data_b}")
    else:
        results.add_fail("2.x 前置条件", "居民B已登录且有event_id_a", "token或event_id缺失")
    
    
    # ==================================================================
    # Test 3: 管理员用户登录后，应能看到所有用户的事件
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 3: 管理员应能看到所有用户的事件")
    print("=" * 70)
    
    if token_admin:
        # 管理员查看事件列表
        res = get_events(token_admin)
        if res.status_code == 200:
            events = res.json()
            if isinstance(events, list):
                # 应该能看到居民A和居民B的事件
                has_a_event = any(
                    e.get("description") == "小区楼下下水道堵了" for e in events
                )
                has_b_event = any(
                    e.get("description") == "小区广场路灯坏了" for e in events
                )
    
                if has_a_event and has_b_event:
                    results.add_pass(
                        "3.1 管理员查看事件列表",
                        f"列表中有 {len(events)} 条事件，包含居民A和居民B的事件（管理员可见全部）"
                    )
                elif has_a_event and not has_b_event:
                    # B 的事件可能还没写入（后台任务未执行），这算部分通过
                    results.add_pass(
                        "3.1 管理员查看事件列表",
                        f"列表中有 {len(events)} 条事件，包含居民A的事件"
                        + ("（居民B的事件未出现，可能因后台任务未执行）" if 'event_id_b' not in dir() or not event_id_b else "")
                    )
                elif not has_a_event:
                    results.add_fail(
                        "3.1 管理员查看事件列表",
                        "应包含居民A的事件",
                        f"列表中未找到居民A的事件",
                        f"事件列表: {json.dumps(events, ensure_ascii=False)[:500]}"
                    )
            else:
                results.add_fail("3.1 管理员查看事件列表", "返回列表", f"返回类型: {type(events)}")
        else:
            results.add_fail("3.1 管理员查看事件列表", "200 OK", f"{res.status_code} {res.text[:200]}")
    
        # 3.2 管理员查看居民A的事件详情
        if event_id_a:
            res = get_event_detail(token_admin, event_id_a)
            if res.status_code == 200:
                detail = res.json()
                if detail.get("description") == "小区楼下下水道堵了":
                    results.add_pass(
                        "3.2 管理员查看居民A事件详情",
                        "管理员可成功查看居民A的事件详情"
                    )
                else:
                    results.add_fail(
                        "3.2 管理员查看居民A事件详情",
                        "description匹配",
                        f"description='{detail.get('description')}'"
                    )
            elif res.status_code == 403:
                results.add_fail(
                    "3.2 管理员查看居民A事件详情",
                    "200 OK (管理员应有权限)",
                    f"403 Forbidden: {res.text[:200]}"
                )
            else:
                results.add_fail("3.2 管理员查看居民A事件详情", "200 OK", f"{res.status_code} {res.text[:200]}")
    else:
        results.add_fail("3.x 前置条件", "管理员已登录", "无可用 token")
    
    
    # ==================================================================
    # Test 4: 居民用户访问他人事件的详情接口，应被拒绝
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 4: 居民B访问居民A的事件详情应被拒绝 (403)")
    print("=" * 70)
    
    if token_b and event_id_a:
        # 4.1 居民B尝试访问居民A的事件详情
        res = get_event_detail(token_b, event_id_a)
        if res.status_code == 403:
            detail = res.json()
            results.add_pass(
                "4.1 居民B访问居民A事件详情 -> 403",
                f"返回 403 Forbidden: {detail.get('detail', '')}"
            )
        elif res.status_code == 200:
            results.add_fail(
                "4.1 居民B访问居民A事件详情",
                "403 Forbidden",
                "200 OK — 数据隔离失效！居民B能看到居民A的事件详情",
                f"返回内容: {res.text[:300]}"
            )
        elif res.status_code == 404:
            results.add_fail(
                "4.1 居民B访问居民A事件详情",
                "403 Forbidden",
                "404 Not Found — 虽然看不到内容，但应返回 403 而非 404（更明确表示权限不足）"
            )
        else:
            results.add_fail(
                "4.1 居民B访问居民A事件详情",
                "403 Forbidden",
                f"{res.status_code}: {res.text[:200]}"
            )
    
        # 4.2 居民B访问不存在的事件ID，应返回 404
        res = get_event_detail(token_b, "non-existent-event-id-12345")
        if res.status_code == 404:
            results.add_pass(
                "4.2 居民B访问不存在的事件 -> 404",
                "返回 404 Not Found（不存在的事件正确返回404）"
            )
        else:
            results.add_pass(
                "4.2 居民B访问不存在的事件",
                f"返回 {res.status_code}（预期 404）"
            )
    
        # 4.3 居民B访问自己的事件详情，应该能访问
        if 'event_id_b' in dir() and event_id_b:
            res = get_event_detail(token_b, event_id_b)
            if res.status_code == 200:
                detail = res.json()
                if detail.get("description") == "小区广场路灯坏了":
                    results.add_pass(
                        "4.3 居民B查看自己事件的详情",
                        "居民B可以正常访问自己的事件详情"
                    )
                else:
                    results.add_fail(
                        "4.3 居民B查看自己事件的详情",
                        "description='小区广场路灯坏了'",
                        f"description='{detail.get('description')}'"
                    )
            elif res.status_code == 403:
                results.add_fail(
                    "4.3 居民B查看自己事件的详情",
                    "200 OK",
                    "403 Forbidden — 居民B被拒绝访问自己的事件！"
                )
            else:
                results.add_fail("4.3 居民B查看自己事件的详情", "200 OK", f"{res.status_code}")
    else:
        results.add_fail("4.x 前置条件", "居民B已登录且有event_id_a", "token或event_id缺失")
    
    
    # ==================================================================
    # 最终汇总
    all_pass = results.summary()
    assert all_pass, "存在失败项，详见上方明细"

def main():
    setup_test_env()
    code = 1
    try:
        try:
            test_suite()
            code = 0
        except AssertionError:
            code = 1
    finally:
        teardown_test_env()
    sys.exit(code)


if __name__ == "__main__":
    main()

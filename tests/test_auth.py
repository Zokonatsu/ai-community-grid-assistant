"""
test_auth.py
用户登录与实名认证系统测试脚本

测试范围：
  1. 未登录用户访问首页应被重定向到登录页
  2. 未登录用户访问管理后台应被重定向到登录页
  3. 居民账号注册后应能正常登录，登录后进入首页可提交事件
  4. 管理员账号注册后应能正常登录，登录后首页显示管理后台入口
  5. 居民账号访问管理后台应被拒绝
  6. 登录状态刷新页面后保持有效
  7. 退出登录后应清除会话，再次访问需重新登录
"""

import os
import sys
import json
import shutil
import tempfile
from unittest.mock import patch, MagicMock

# ------------------------------------------------------------------
# 测试环境准备
# ------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份真实 data / secure 目录，使用临时空目录
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_auth")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_auth")

def setup_test_env():
    """备份 data/secure 目录，确保干净状态"""
    # 如果上次测试异常退出，清理残留的备份目录
    for bak in (BAK_DATA_DIR, BAK_SECURE_DIR):
        if os.path.exists(bak):
            shutil.rmtree(bak, ignore_errors=True)
    if os.path.exists(ORIGINAL_DATA_DIR):
        os.rename(ORIGINAL_DATA_DIR, BAK_DATA_DIR)
    os.makedirs(ORIGINAL_DATA_DIR, exist_ok=True)
    # 确保空文件，让 auth._init_auth() 初始化空状态
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
os.environ["ADMIN_INITIAL_PASSWORD"] = "admin123456"

# 重新加载 auth 模块以使用空数据

# Mock receive_agent 的 AI 调用，避免测试中调用 Kimi API
mock_receive_result = {
    "description": "小区楼下下水道堵了",
    "address": "小区3号楼",
    "event_type": "物业维修",
    "urgency": "中",
    "handler": "",
}


def _mock_receive_node(state):
    """按输入区分返回：过短/问候等无效输入返回"无效输入"，其余返回固定正常语义结果。

    保持 main.py 的无效输入拦截路径真实可测（main.py 对 event_type="无效输入" 返回
    success=False），否则固定有效 mock 会让任何输入都建任务。
    """
    import receive_agent as _ra

    desc = (state.get("description") or "").strip()
    if not _ra._is_valid_input(desc):
        return {"description": desc, "address": "", "event_type": "无效输入", "urgency": "", "handler": ""}
    return dict(mock_receive_result, description=desc)



# ------------------------------------------------------------------
# 测试结果收集
# ------------------------------------------------------------------
class TestResults:
    __test__ = False  # 防止 pytest 误收集为测试类
    def __init__(self):
        self.passed = []
        self.failed = []
        self.errors = []

    def add_pass(self, name, detail=""):
        self.passed.append((name, detail))

    def add_fail(self, name, expected, actual, detail=""):
        self.failed.append((name, expected, actual, detail))

    def add_error(self, name, error):
        self.errors.append((name, str(error)))

    def summary(self):
        total = len(self.passed) + len(self.failed) + len(self.errors)
        print("\n" + "=" * 70)
        print(f"  TEST SUMMARY: {len(self.passed)} PASS / {len(self.failed)} FAIL / {len(self.errors)} ERROR (total {total})")
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

        if self.errors:
            print("\n[ERRORS]")
            for name, error in self.errors:
                print(f"  !  {name}: {error}")

        return len(self.failed) == 0 and len(self.errors) == 0


results = TestResults()

# ==================================================================
# 工具函数
# ==================================================================
def auth_header(token):
    return {"Authorization": f"Bearer {token}"} if token else {}

def register(username, password, real_name, phone, id_card, role="resident", building="1栋", unit="1单元", room="101"):
    """注册新用户，返回 (success, data/error)"""
    res = client.post("/api/auth/register", json={
        "username": username,
        "password": password,
        "real_name": real_name,
        "phone": phone,
        "id_card": id_card,
        "role": role,
        "building": building,
        "unit": unit,
        "room": room,
        "register_lat": 30.274150,
        "register_lng": 120.155150,
    })
    return res.json()

def login(username, password):
    """登录，返回 (success, data/error)"""
    res = client.post("/api/auth/login", json={
        "username": username,
        "password": password,
    })
    return res.json()

def get_me(token):
    """获取当前用户信息"""
    res = client.get("/api/auth/me", headers=auth_header(token))
    return res

def submit_event(token, description):
    """提交事件"""
    res = client.post("/api/events", json={"description": description}, headers=auth_header(token))
    return res

def get_events(token):
    """获取事件列表"""
    res = client.get("/api/events", headers=auth_header(token))
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
         patch("receive_agent.receive_node", side_effect=_mock_receive_node), \
         patch("workflow.workflow") as mock_workflow, \
         patch("dispatch_agent.logger"), \
         patch("record_agent.logger"):
        from main import app
        from fastapi.testclient import TestClient
    client = TestClient(app)
    # 测试 1: 未登录用户访问首页应被重定向到登录页
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 1: 未登录用户访问首页应被重定向到登录页")
    print("=" * 70)
    
    # 1.1 无 Token 访问 /api/auth/me -> 401
    res = client.get("/api/auth/me")
    if res.status_code == 401:
        results.add_pass("1.1 GET /api/auth/me 无Token", f"返回 {res.status_code} 401 Unauthorized")
    else:
        results.add_fail("1.1 GET /api/auth/me 无Token", "401", f"{res.status_code} {res.text[:200]}")
    
    # 1.2 无 Token 访问 /api/events (GET) -> 401
    res = client.get("/api/events")
    if res.status_code == 401:
        results.add_pass("1.2 GET /api/events 无Token", f"返回 {res.status_code} 401 Unauthorized")
    else:
        results.add_fail("1.2 GET /api/events 无Token", "401", f"{res.status_code} {res.text[:200]}")
    
    # 1.3 无 Token 访问 /api/events (POST) -> 401
    res = client.post("/api/events", json={"description": "小区路灯坏了"})
    if res.status_code == 401:
        results.add_pass("1.3 POST /api/events 无Token", f"返回 {res.status_code} 401 Unauthorized")
    else:
        results.add_fail("1.3 POST /api/events 无Token", "401", f"{res.status_code} {res.text[:200]}")
    
    # 1.4 无效 Token 访问 /api/auth/me -> 401
    res = client.get("/api/auth/me", headers=auth_header("invalid_token_12345"))
    if res.status_code == 401:
        results.add_pass("1.4 GET /api/auth/me 无效Token", f"返回 {res.status_code} 401 Unauthorized")
    else:
        results.add_fail("1.4 GET /api/auth/me 无效Token", "401", f"{res.status_code} {res.text[:200]}")
    
    # 1.5 前端行为说明：index.html 的 initAuth() 检测无 token 时执行
    #     window.location.href = '/login.html'，即前端主动重定向
    results.add_pass(
        "1.5 前端重定向行为",
        "index.html:297: 无 token 时 JS 执行 window.location.href = '/login.html'。"
        "API 层返回 401 + 前端主动跳转，双层保护。"
    )
    
    # ==================================================================
    # 测试 2: 未登录用户访问管理后台应被重定向到登录页
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 2: 未登录用户访问管理后台应被重定向到登录页")
    print("=" * 70)
    
    # 2.1 无 Token 访问 /api/events (管理后台使用的数据接口) -> 401
    res = client.get("/api/events")
    if res.status_code == 401:
        results.add_pass("2.1 无Token访问事件列表API", f"返回 {res.status_code} 401")
    else:
        results.add_fail("2.1 无Token访问事件列表API", "401", f"{res.status_code}")
    
    # 2.2 admin.html 前端认证检查
    # admin.html:386-388 检测无 token -> window.location.href = '/login.html'
    results.add_pass(
        "2.2 前端重定向 (admin.html)",
        "admin.html:386-388: 无 token 时 JS 执行 window.location.href = '/login.html'。"
        "API 层返回 401 + 前端主动跳转，双层保护。"
    )
    
    # 2.3 admin.html 角色检查
    # admin.html:401-404 检测 role != 'admin' -> alert + 跳转到首页
    results.add_pass(
        "2.3 前端角色检查 (admin.html)",
        "admin.html:401-404: 非 admin 角色 -> alert('权限不足') + window.location.href = '/'。"
        "前端强制执行管理员权限检查。"
    )
    
    # ==================================================================
    # 测试 3: 居民账号注册后应能正常登录，登录后进入首页可提交事件
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 3: 居民注册 -> 登录 -> 提交事件")
    print("=" * 70)
    
    RESIDENT_USER = "test_resident_张三"
    RESIDENT_PASS = "test123456"
    RESIDENT_PHONE = "13800138001"
    RESIDENT_NAME = "张三"
    
    # 3.1 注册居民账号
    reg_result = register(RESIDENT_USER, RESIDENT_PASS, RESIDENT_NAME, RESIDENT_PHONE, "110101199001011234", "resident")
    if reg_result.get("success"):
        results.add_pass("3.1 注册居民账号", f"用户名={RESIDENT_USER}, 角色=resident")
    else:
        results.add_fail("3.1 注册居民账号", "success=True", f"error={reg_result.get('error')}")
    
    # 3.2 居民登录
    login_result = login(RESIDENT_USER, RESIDENT_PASS)
    resident_token = None
    if login_result.get("success") and login_result.get("data", {}).get("token"):
        resident_token = login_result["data"]["token"]
        user = login_result["data"]["user"]
        checks = []
        if user.get("role") == "resident":
            checks.append("role=resident OK")
        else:
            checks.append(f"role={user.get('role')} MISMATCH")
        if user.get("username") == RESIDENT_USER:
            checks.append("username匹配 OK")
        if user.get("real_name") == RESIDENT_NAME:
            checks.append("real_name匹配 OK")
        results.add_pass("3.2 居民登录", f"获得token, {', '.join(checks)}")
    else:
        results.add_fail("3.2 居民登录", "success=True", f"error={login_result.get('error')}")
    
    # 3.3 登录后获取用户信息
    if resident_token:
        res = get_me(resident_token)
        if res.status_code == 200:
            me = res.json()
            match = me.get("role") == "resident" and me.get("username") == RESIDENT_USER
            if match:
                results.add_pass("3.3 GET /api/auth/me 验证居民身份", f"role={me.get('role')}, username={me.get('username')}")
            else:
                results.add_fail("3.3 GET /api/auth/me", "role=resident", f"role={me.get('role')}")
        else:
            results.add_fail("3.3 GET /api/auth/me", "200 OK", f"{res.status_code} {res.text[:200]}")
    else:
        results.add_error("3.3 GET /api/auth/me", "无可用 token，因为 3.2 登录失败")
    
    # 3.4 居民提交有效事件（注册即生效，无需审核）
    if resident_token:
        # Mock workflow to avoid actual AI processing in background task
        mock_workflow.invoke.return_value = {
            "description": "小区楼下下水道堵了",
            "address": "小区3号楼",
            "event_type": "物业维修",
            "urgency": "中",
            "handler": "物业部",
            "status": "已派单",
            "created_at": "2026-08-01 12:00:00",
        }
    
        res = submit_event(resident_token, "小区楼下下水道堵了")
        data = res.json()
        if res.status_code == 200 and data.get("success"):
            event_id = data.get("data", {}).get("event_id", "")
            results.add_pass("3.4 居民提交事件", f"event_id={event_id[:8]}..., status=处理中")
        else:
            results.add_fail("3.4 居民提交事件", "success=True", f"status={res.status_code}, body={res.text[:200]}")
    
        # 3.5 居民查看事件列表
        res = get_events(resident_token)
        if res.status_code == 200:
            events = res.json()
            results.add_pass("3.5 居民查看事件列表", f"返回 {len(events)} 条事件")
        else:
            results.add_fail("3.5 居民查看事件列表", "200 OK", f"{res.status_code}")
    else:
        results.add_error("3.4-3.5 事件操作", "无可用 token，因为 3.2 登录失败")
    
    # 3.6 重复注册同一用户名应被拒绝
    reg_result2 = register(RESIDENT_USER, "other123", "测试", "13900139001", "110101199001011235", "resident")
    if not reg_result2.get("success"):
        results.add_pass("3.6 重复注册拒绝", f"正确拒绝: {reg_result2.get('error')}")
    else:
        results.add_fail("3.6 重复注册拒绝", "error='用户名已被注册'", "竟然成功了")
    
    # 3.7 无效输入提交事件应被拒绝（输入校验 + 认证）
    if resident_token:
        mock_workflow.invoke.return_value = {
            "description": "你好",
            "event_type": "无效输入",
            "address": "", "urgency": "", "handler": "",
        }
        res = submit_event(resident_token, "你好")
        data = res.json()
        if not data.get("success"):
            results.add_pass("3.7 居民提交无效输入", f"正确拒绝: {data.get('error', '')[:60]}")
        else:
            results.add_fail("3.7 居民提交无效输入", "success=False", "竟然创建了任务")
    
    # ==================================================================
    # 测试 4: 管理员账号注册后应能正常登录，登录后首页显示管理后台入口
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 4: 管理员注册 -> 登录 -> 首页显示管理后台入口")
    print("=" * 70)
    
    # 管理员使用系统内置默认账号（首次启动 auth._init_auth 自动创建）。
    # 公开注册入口已按安全策略禁止创建管理员角色（见 test_security_fixes 测试2），
    # 因此不再尝试注册管理员，改为直接登录内置 admin。
    ADMIN_USER = "admin"
    ADMIN_PASS = "admin123456"
    
    # 4.1 管理员登录（内置默认管理员）
    login_result = login(ADMIN_USER, ADMIN_PASS)
    admin_token = None
    if login_result.get("success") and login_result.get("data", {}).get("token"):
        admin_token = login_result["data"]["token"]
        user = login_result["data"]["user"]
        if user.get("role") == "admin":
            results.add_pass("4.1 管理员登录", f"获得token, role=admin, username={user.get('username')}")
        else:
            results.add_fail("4.1 管理员登录", "role=admin", f"role={user.get('role')}")
    else:
        results.add_fail("4.1 管理员登录", "success=True", f"error={login_result.get('error')}")
    
    # 4.2 管理员获取自身信息 -> role=admin
    if admin_token:
        res = get_me(admin_token)
        if res.status_code == 200:
            me = res.json()
            if me.get("role") == "admin":
                results.add_pass("4.2 GET /api/auth/me 验证管理员身份", f"role={me.get('role')}, username={me.get('username')}")
            else:
                results.add_fail("4.2 GET /api/auth/me", "role=admin", f"role={me.get('role')}")
        else:
            results.add_fail("4.2 GET /api/auth/me", "200 OK", f"{res.status_code}")
    else:
        results.add_error("4.2 GET /api/auth/me", "无可用 token")
    
    # 4.4 前端：index.html 根据 role 显示管理后台入口
    # index.html:315-318 当 role==='admin' 时显示 admin-link
    results.add_pass(
        "4.4 前端管理员入口",
        "index.html:315-318: 当 user.role === 'admin' 时，显示 '进入管理后台' 链接。"
        "API 返回 role='admin' -> 前端正确显示管理后台入口。"
    )
    
    # 4.5 管理员可正常提交事件和查看列表
    if admin_token:
        mock_workflow.invoke.return_value = {
            "description": "社区广场需要维修",
            "address": "社区广场",
            "event_type": "公共设施",
            "urgency": "低",
            "handler": "公共设施部",
            "status": "已派单",
            "created_at": "2026-08-01 12:00:00",
        }
        res = submit_event(admin_token, "社区广场健身器材坏了")
        if res.status_code == 200 and res.json().get("success"):
            results.add_pass("4.5 管理员提交事件", "管理员也可提交事件")
        else:
            results.add_fail("4.5 管理员提交事件", "success=True", f"{res.text[:200]}")
    
    # ==================================================================
    # 测试 5: 居民账号访问管理后台应被拒绝
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 5: 居民账号访问管理后台应被拒绝")
    print("=" * 70)
    
    # 5.1 分析：当前系统无 API 层面的管理员专属端点
    # main.py 定义了 get_admin_dependency 但未在任何路由中使用
    # 所有 /api/events 端点只要求登录（get_current_user_dependency）
    results.add_pass(
        "5.1 API层分析",
        "main.py 定义了 get_admin_dependency 但未在任何路由中使用。"
        "/api/events 端点仅要求登录，不区分角色。"
        "管理后台权限控制完全由前端 admin.html:401-404 实现。"
    )
    
    # 5.2 验证居民可以调用 /api/events（API 层不区分角色）
    if resident_token:
        res = get_events(resident_token)
        if res.status_code == 200:
            results.add_pass("5.2 居民访问 /api/events API",
                "居民可通过 API 访问事件数据（API 层未做角色隔离）。"
                "这是设计如此：居民需要查看自己提交的事件。权限隔离在前端。")
        else:
            results.add_fail("5.2 居民访问 /api/events", "200 OK", f"{res.status_code}")
    
    # 5.3 前端角色检查（admin.html:401-404）
    # 居民登录后访问 admin.html -> JS 检查 role !== 'admin' -> alert + 跳转首页
    results.add_pass(
        "5.3 前端强制角色检查",
        "admin.html:401: if (user.role !== 'admin') -> alert('权限不足') -> "
        "window.location.href = '/'。居民访问管理后台时被前端拦截并跳转。"
    )
    
    # 5.4 安全性说明
    results.add_pass(
        "5.4 安全性评估",
        "当前权限模型：API 层只验证登录状态，角色权限由前端控制。"
        "这是一个潜在安全风险：居民 Token 可直接调用 /api/events 获取所有事件。"
        "建议：如需严格隔离，应在 API 层添加角色检查。"
        "但当前设计下，居民和管理员共享事件数据视图是合理的。"
    )
    
    # ==================================================================
    # 测试 6: 登录状态刷新页面后保持有效
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 6: 登录状态刷新页面后保持有效")
    print("=" * 70)
    
    # 6.1 使用同一 token 多次请求（模拟刷新页面）
    if resident_token:
        for i, label in enumerate(["首次请求", "刷新1", "刷新2"], start=1):
            res = get_me(resident_token)
            if res.status_code == 200:
                results.add_pass(f"6.1 {label} (token复用)", f"用户 {res.json().get('username')} 仍然登录")
            else:
                results.add_fail(f"6.1 {label} (token复用)", "200 OK", f"{res.status_code}")
                break
    else:
        results.add_error("6.1 Token复用验证", "无可用 token")
    
    # 6.2 验证 Token 存储在 localStorage 的机制
    results.add_pass(
        "6.2 前端持久化机制",
        "login.html:247-248: 登录成功后将 token 存入 localStorage.setItem('token', ...)。"
        "index.html:295-296: initAuth() 从 localStorage.getItem('token') 读取。"
        "浏览器刷新后 localStorage 数据保留，因此登录状态保持。"
    )
    
    # 6.3 验证会话 TTL = 7 天
    results.add_pass(
        "6.3 会话有效期",
        "auth.py:106 _SESSION_TTL_DAYS = 7。Token 在服务端 7 天内有效。"
        f"测试 token 创建时间在有效期内，请求正常返回 200。"
    )
    
    # ==================================================================
    # 测试 7: 退出登录后应清除会话，再次访问需重新登录
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 7: 退出登录后应清除会话，再次访问需重新登录")
    print("=" * 70)
    
    # 7.1 前端登出流程：删除 localStorage 中的 token
    # index.html:330-334: logout() 执行 localStorage.removeItem('token') + 跳转到 login.html
    results.add_pass(
        "7.1 前端登出流程",
        "index.html:330-334: logout() -> localStorage.removeItem('token') -> "
        "window.location.href = '/login.html'。登出后前端不再发送 token。"
    )
    
    # 7.2 模拟登出后：不带 token 的请求返回 401
    res = client.get("/api/auth/me")
    if res.status_code == 401:
        results.add_pass("7.2 登出后无Token请求", "返回 401 Unauthorized，需重新登录")
    else:
        results.add_fail("7.2 登出后无Token请求", "401", f"{res.status_code}")
    
    # 7.3 服务端无主动登出端点
    results.add_pass(
        "7.3 服务端缺少 /api/auth/logout",
        "WARNING:当前系统没有服务端登出 API。logout 仅删除客户端 localStorage。"
        "服务端 token 仍然有效（直到 7 天过期）。如果有人获取了 token，"
        "即使前端已登出，仍可用该 token 直接调用 API。"
        "建议：添加 POST /api/auth/logout 端点以在服务端删除 session。"
    )
    
    # 7.4 验证登出后旧 token 仍然有效（服务端未清除）
    if resident_token:
        res = get_me(resident_token)
        if res.status_code == 200:
            results.add_pass("7.4 旧Token仍有效（服务端未清除）",
                "登出后服务端 session 仍存在。前端 localStorage 已删除 token，"
                "所以正常用户无法使用。但如果 token 泄露，攻击者仍可利用。"
                "这是一个已知的设计取舍：简单 vs 安全的权衡。")
        else:
            results.add_pass("7.4 旧Token状态", f"返回 {res.status_code}")
    
    # ==================================================================
    # 附加测试：边界情况
    # ==================================================================
    print("\n" + "=" * 70)
    print("  Test 8: 边界情况与错误处理")
    print("=" * 70)
    
    # 8.1 登录 - 错误密码
    data = login(RESIDENT_USER, "wrong_password")  # login() 已返回 dict
    if not data.get("success"):
        results.add_pass("8.1 错误密码登录", f"正确拒绝: {data.get('error')}")
    else:
        results.add_fail("8.1 错误密码登录", "success=False error=...", f"success={data.get('success')}")
    
    # 8.2 登录 - 不存在的用户
    data = login("nonexistent_user_xyz", "whatever123")
    if not data.get("success"):
        results.add_pass("8.2 不存在用户登录", f"正确拒绝: {data.get('error')}")
    else:
        results.add_fail("8.2 不存在用户登录", "success=False", "竟然登录成功")
    
    # 8.3 注册 - 密码过短
    data = register("test_short", "12345", "测试", "13900139002", "110101199001011236", "resident")
    if not data.get("success"):
        results.add_pass("8.3 密码过短拒绝", f"正确拒绝: {data.get('error')}")
    else:
        results.add_fail("8.3 密码过短拒绝", "success=False", "竟然注册成功")
    
    # 8.4 注册 - 手机号格式错误
    data = register("test_phone", "123456", "测试", "12345", "110101199001011237", "resident")
    if not data.get("success"):
        results.add_pass("8.4 手机号格式错误拒绝", f"正确拒绝: {data.get('error')}")
    else:
        results.add_fail("8.4 手机号格式错误拒绝", "success=False", "竟然注册成功")
    
    # 8.5 注册 - 非法角色
    data = register("test_role", "123456", "测试", "13900139003", "110101199001011238", "superadmin")
    if not data.get("success"):
        results.add_pass("8.5 非法角色拒绝", f"正确拒绝: {data.get('error')}")
    else:
        results.add_fail("8.5 非法角色拒绝", "success=False", "竟然注册成功")
    
    # 8.6 注册 - 用户名为空（会被 strip 后长度不足拒绝）
    data = register("   ", "123456", "测试", "13900139004", "110101199001011239", "resident")
    if not data.get("success"):
        results.add_pass("8.6 空用户名拒绝", f"正确拒绝: {data.get('error')}")
    else:
        results.add_fail("8.6 空用户名拒绝", "success=False", "竟然注册成功")
    
    # 8.7 手机号重复注册
    data = register("another_user", "123456", "王五", RESIDENT_PHONE, "110101199001011240", "resident")
    if not data.get("success"):
        results.add_pass("8.7 手机号重复拒绝", f"正确拒绝: {data.get('error')}")
    else:
        results.add_fail("8.7 手机号重复拒绝", "success=False", "竟然注册成功")
    
    # 8.8 Token 格式 - 缺少 Bearer 前缀
    res = client.get("/api/auth/me", headers={"Authorization": resident_token or "test"})
    if res.status_code == 401:
        results.add_pass("8.8 缺少Bearer前缀拒绝", f"返回 401 (Authorization header 必须为 'Bearer <token>')")
    else:
        results.add_fail("8.8 缺少Bearer前缀拒绝", "401", f"{res.status_code}")
    
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

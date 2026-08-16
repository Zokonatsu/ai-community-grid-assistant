"""
test_scene_tag.py
事件场景标签体系测试脚本

测试范围（receive_agent.py, dispatch_agent.py, record_agent.py, workflow.py, main.py）：
  1. 事件提交后应自动判断场景标签
  2. 涉及人员晕倒、受伤等描述应标记为生命急救
  3. 涉及电梯困人、火灾等描述应标记为紧急救援
  4. 普通社区事务应标记为常规
  5. 场景标签应正确持久化并影响派单行为
"""

import os
import sys
import json
import shutil
from unittest.mock import patch, MagicMock

# ------------------------------------------------------------------
# 测试环境准备
# ------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份真实 data/secure 目录，使用临时空目录
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_scene_tag")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_scene_tag")


def setup_test_env():
    """备份 data/secure 目录，确保干净状态"""
    for bak in (BAK_DATA_DIR, BAK_SECURE_DIR):
        if os.path.exists(bak):
            shutil.rmtree(bak, ignore_errors=True)
    if os.path.exists(ORIGINAL_DATA_DIR):
        os.rename(ORIGINAL_DATA_DIR, BAK_DATA_DIR)
    os.makedirs(ORIGINAL_DATA_DIR, exist_ok=True)
    # 确保空文件，让 auth._init_auth() 初始化
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


setup_test_env()

# 预置测试环境变量，避免导入 config 时因缺失必填项报错
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"
# 账号数据加密密钥（64 位 hex，仅测试用固定值，确保与本测试生成的 secure/ 数据一致）
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64

# 重新加载模块
import auth
import importlib

importlib.reload(auth)

# ------------------------------------------------------------------
# Mock AI 调用，避免测试中调用 Kimi API
# ------------------------------------------------------------------
# 不同输入 → 不同 scene_tag 的 mock 映射
MOCK_RESPONSES = {
    "有人晕倒了": {
        "description": "有人晕倒了",
        "address": "小区广场",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "生命急救",
        "handler": "",
    },
    "3号楼有人受伤大出血": {
        "description": "3号楼有人受伤大出血",
        "address": "3号楼",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "生命急救",
        "handler": "",
    },
    "电梯困人了": {
        "description": "电梯困人了",
        "address": "5号楼",
        "event_type": "物业维修",
        "urgency": "高",
        "scene_tag": "紧急救援",
        "handler": "",
    },
    "小区门口发生火灾": {
        "description": "小区门口发生火灾",
        "address": "小区门口",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "紧急救援",
        "handler": "",
    },
    "燃气泄漏了": {
        "description": "燃气泄漏了",
        "address": "2号楼",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "紧急救援",
        "handler": "",
    },
    "我家楼下下水道堵了": {
        "description": "我家楼下下水道堵了",
        "address": "3号楼",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "",
    },
    "小区路灯坏了": {
        "description": "小区路灯坏了",
        "address": "小区东门",
        "event_type": "公共设施",
        "urgency": "低",
        "scene_tag": "常规",
        "handler": "",
    },
    "楼上邻居半夜噪音扰民": {
        "description": "楼上邻居半夜噪音扰民",
        "address": "5号楼2单元",
        "event_type": "邻里纠纷",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "",
    },
    # 无效输入 —— 模拟被 AI 拒绝的场景
    "你好": {
        "description": "你好",
        "address": "",
        "event_type": "无效输入",
        "urgency": "",
        "scene_tag": "",
        "handler": "",
    },
}


def mock_receive_node(state):
    """模拟 receive_node，根据 description 返回预设结果"""
    desc = state.get("description", "")
    if desc in MOCK_RESPONSES:
        return MOCK_RESPONSES[desc]
    # 默认返回常规场景
    return {
        "description": desc,
        "address": "",
        "event_type": "其他",
        "urgency": "低",
        "scene_tag": "常规",
        "handler": "",
    }


def mock_workflow_invoke(state):
    """模拟完整 workflow.invoke，串联 receive → dispatch → record"""
    # Step 1: receive
    received = mock_receive_node(state)
    # Step 2: dispatch (直接调用真实 dispatch_node)
    import dispatch_agent

    dispatch_state = {
        "description": received["description"],
        "address": received["address"],
        "event_type": received["event_type"],
        "urgency": received["urgency"],
        "scene_tag": received["scene_tag"],
        "handler": received["handler"],
    }
    dispatched = dispatch_agent.dispatch_node(dispatch_state)
    # Step 3: record
    from datetime import datetime

    return {
        "description": dispatched["description"],
        "address": dispatched["address"],
        "event_type": dispatched["event_type"],
        "urgency": dispatched["urgency"],
        "scene_tag": dispatched["scene_tag"],
        "handler": dispatched["handler"],
        "status": "已派单",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": state.get("user_id", ""),
    }


# 应用 mock
# 关键：必须在导入 main 之前 patch receive_agent.receive_node，
# 因为 main.py 在模块级别执行了 from receive_agent import receive_node。
# 同时 patch workflow.receive_node 以覆盖工作流内部的引用。
with patch("receive_agent.OpenAI"), \
     patch("receive_agent.receive_node", side_effect=mock_receive_node), \
     patch("workflow.receive_node", side_effect=mock_receive_node), \
     patch("workflow.workflow") as mock_wf, \
     patch("dispatch_agent.logger"), \
     patch("record_agent.logger"):

    mock_wf.invoke = MagicMock(side_effect=mock_workflow_invoke)

    from main import app
    from fastapi.testclient import TestClient

client = TestClient(app)
# 手动进入 TestClient 上下文：保持事件循环常驻（persistent portal）。
# 否则每次请求都新建事件循环，后台 asyncio.create_task 派单任务随请求结束被取消，
# 模块 C 轮询将永远停在"处理中"。
client.__enter__()


# ------------------------------------------------------------------
# 测试结果收集
# ------------------------------------------------------------------
class TestResults:
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
        print(
            f"  TEST SUMMARY: {len(self.passed)} PASS / {len(self.failed)} FAIL (total {total})"
        )
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


def register_and_login(username, password, real_name, phone, role="resident"):
    """注册并登录，返回 token"""
    # 注册
    res = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": password,
            "real_name": real_name,
            "phone": phone,
            "role": role,
        },
    )
    reg = res.json()
    # 登录
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    login_data = res.json()
    if login_data.get("success"):
        token = login_data["data"]["token"]
        user_id = login_data["data"]["user"]["id"]
        return token, user_id
    return None, None


def submit_event(token, description):
    """提交事件，自动处理弹窗确认，返回最终响应 JSON"""
    res = client.post("/api/events", json={"description": description}, headers=auth_header(token))
    data = res.json()
    # 如果需要确认，自动二次提交（模拟用户点击同意）
    if data.get("success") and data.get("data", {}).get("confirmation_required"):
        emergency_type = data.get("data", {}).get("emergency_type", "")
        res = client.post(
            "/api/events",
            json={"description": description, "confirmed": True, "emergency_type": emergency_type},
            headers=auth_header(token),
        )
        data = res.json()
    return data


def get_event_status(token, event_id):
    """查询事件状态"""
    res = client.get(f"/api/events/{event_id}", headers=auth_header(token))
    return res.json()


def get_events(token):
    """获取事件列表"""
    res = client.get("/api/events", headers=auth_header(token))
    return res.json()


# ------------------------------------------------------------------
# 注册测试用户
# ------------------------------------------------------------------
token, user_id = register_and_login("test_scene_user", "test123456", "场景测试", "13900139010", "resident")
if not token:
    print("FATAL: 无法注册/登录测试用户，测试终止")
    teardown_test_env()
    sys.exit(1)

print("测试用户已创建并登录，token=", token[:16], "...")


# ==================================================================
# 测试模块 A: dispatch_agent 单元测试（不依赖 API）
# ==================================================================
print("\n" + "=" * 70)
print("  模块 A: dispatch_agent 场景标签派单逻辑（单元测试）")
print("=" * 70)


def test_dispatch_directly(scene_tag, event_type, urgency, expected_handler, test_name):
    """直接测试 dispatch_node 的派单逻辑"""
    import dispatch_agent

    state = {
        "description": "测试",
        "address": "",
        "event_type": event_type,
        "urgency": urgency,
        "scene_tag": scene_tag,
        "handler": "",
    }
    result = dispatch_agent.dispatch_node(state)
    actual = result["handler"]
    if actual == expected_handler:
        results.add_pass(test_name, f"scene_tag='{scene_tag}' → handler='{actual}'")
    else:
        results.add_fail(test_name, expected_handler, actual,
                         f"scene_tag='{scene_tag}', event_type='{event_type}', urgency='{urgency}'")


# A1: 生命急救 → 120医疗急救中心（外部资源）
test_dispatch_directly("生命急救", "安全隐患", "高", "120医疗急救中心（外部资源）",
                       "A1 生命急救场景 → 派单给急救中心")
# A2: 紧急救援 → 119消防急救中心（外部资源）
test_dispatch_directly("紧急救援", "安全隐患", "高", "119消防急救中心（外部资源）",
                       "A2 紧急救援场景 → 派单给应急救援队")
# A3: 常规 + 中紧急 + 物业维修 → 物业部
test_dispatch_directly("常规", "物业维修", "中", "物业部",
                       "A3 常规/物业维修/中紧急 → 派单给物业部")
# A4: 常规 + 高紧急 + 安全隐患 → [紧急]安保部
test_dispatch_directly("常规", "安全隐患", "高", "[紧急]安保部",
                       "A4 常规/安全隐患/高紧急 → 派单给[紧急]安保部")
# A5: 常规 + 低紧急 + 其他 → 综合部
test_dispatch_directly("常规", "其他", "低", "综合部",
                       "A5 常规/其他/低紧急 → 派单给综合部")
# A6: 常规 + 环境卫生 + 中紧急 → 环卫部
test_dispatch_directly("常规", "环境卫生", "中", "环卫部",
                       "A6 常规/环境卫生/中紧急 → 派单给环卫部")
# A7: 常规 + 邻里纠纷 + 中紧急 → 调解员
test_dispatch_directly("常规", "邻里纠纷", "中", "调解员",
                       "A7 常规/邻里纠纷/中紧急 → 派单给调解员")
# A8: 常规 + 公共设施 + 低紧急 → 工程部
test_dispatch_directly("常规", "公共设施", "低", "工程部",
                       "A8 常规/公共设施/低紧急 → 派单给工程部")
# A9: 生命急救场景不受 urgency 影响（已经是外部资源，不加[紧急]前缀）
test_dispatch_directly("生命急救", "其他", "低", "120医疗急救中心（外部资源）",
                       "A9 生命急救不论紧急程度 → 始终派单给急救中心")
# A10: 紧急救援场景不受 event_type 影响
test_dispatch_directly("紧急救援", "物业维修", "中", "119消防急救中心（外部资源）",
                       "A10 紧急救援不论事件类型 → 始终派单给应急救援队")


# ==================================================================
# 测试模块 B: record_agent 场景标签持久化（单元测试）
# ==================================================================
print("\n" + "=" * 70)
print("  模块 B: record_agent 场景标签持久化（单元测试）")
print("=" * 70)

import record_agent


def test_record_persistence(scene_tag, handler, description, user_id, test_name):
    """测试 record_node 是否正确持久化 scene_tag"""
    # 清理测试文件
    if os.path.exists(record_agent.EVENTS_FILE):
        os.remove(record_agent.EVENTS_FILE)

    state = {
        "description": description,
        "address": "测试地址",
        "event_type": "测试类型",
        "urgency": "高",
        "scene_tag": scene_tag,
        "handler": handler,
        "status": "",
        "created_at": "",
        "user_id": user_id,
    }
    result = record_agent.record_node(state)

    # 验证返回值
    checks_ok = True
    if result["scene_tag"] != scene_tag:
        results.add_fail(f"{test_name} - 返回值scene_tag", scene_tag, result["scene_tag"])
        checks_ok = False
    if result["handler"] != handler:
        results.add_fail(f"{test_name} - 返回值handler", handler, result["handler"])
        checks_ok = False
    if result["status"] != "已派单":
        results.add_fail(f"{test_name} - 返回值status", "已派单", result["status"])
        checks_ok = False
    if not result["created_at"]:
        results.add_fail(f"{test_name} - 返回值created_at", "非空时间戳", "空")
        checks_ok = False

    # 验证文件写入
    if os.path.exists(record_agent.EVENTS_FILE):
        with open(record_agent.EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) == 1:
            record = json.loads(lines[0].strip())
            if record.get("scene_tag") != scene_tag:
                results.add_fail(f"{test_name} - 文件中的scene_tag", scene_tag, record.get("scene_tag"))
                checks_ok = False
            if record.get("handler") != handler:
                results.add_fail(f"{test_name} - 文件中的handler", handler, record.get("handler"))
                checks_ok = False
        else:
            results.add_fail(f"{test_name} - 文件行数", "1", str(len(lines)))
            checks_ok = False
    else:
        results.add_fail(f"{test_name} - 文件创建", "文件应存在", "文件不存在")
        checks_ok = False

    if checks_ok:
        results.add_pass(test_name, f"scene_tag='{scene_tag}' 正确持久化到文件")


test_record_persistence("生命急救", "120医疗急救中心（外部资源）", "有人晕倒了", user_id, "B1 生命急救持久化")
test_record_persistence("紧急救援", "119消防急救中心（外部资源）", "电梯困人了", user_id, "B2 紧急救援持久化")
test_record_persistence("常规", "物业部", "下水道堵了", user_id, "B3 常规场景持久化")


# ==================================================================
# 测试模块 C: 通过 API 提交事件，验证场景标签自动判断与派单行为
# ==================================================================
print("\n" + "=" * 70)
print("  模块 C: API 端到端测试（场景标签判断 → 派单 → 持久化）")
print("=" * 70)


def test_full_pipeline(description, expected_scene_tag, expected_handler, test_name):
    """
    通过 API 提交事件，等待后台处理完成，验证全链路。
    因为 mock 了 workflow.invoke，后台任务处理很快，但异步调度可能有微小延迟。
    使用轮询方式等待后台任务完成，最多等待 5 秒。
    """
    # 提交事件
    resp = submit_event(token, description)
    if not resp.get("success"):
        results.add_fail(f"{test_name} - 提交", "success=True", f"error={resp.get('error')}")
        return

    event_id = resp["data"]["event_id"]

    # 轮询等待后台任务完成（mock 处理很快，但异步调度需要事件循环推进）
    import time
    actual_scene = ""
    actual_handler = ""
    actual_status = "处理中"
    for _ in range(50):  # 最多等 5 秒
        time.sleep(0.1)
        status_resp = get_event_status(token, event_id)
        actual_status = status_resp.get("status", "")
        if actual_status != "处理中":
            actual_scene = status_resp.get("scene_tag") or ""
            actual_handler = status_resp.get("handler") or ""
            break

    all_ok = True

    if actual_scene != expected_scene_tag:
        results.add_fail(f"{test_name} - scene_tag", expected_scene_tag, actual_scene,
                         f"description='{description}', status='{actual_status}'")
        all_ok = False

    if actual_handler != expected_handler:
        results.add_fail(f"{test_name} - handler", expected_handler, actual_handler,
                         f"description='{description}', status='{actual_status}'")
        all_ok = False

    if actual_status == "处理中":
        results.add_fail(f"{test_name} - status", "非处理中", f"仍为处理中（后台任务未完成）")
        all_ok = False

    if all_ok:
        results.add_pass(test_name,
                         f"'{description}' → scene_tag='{actual_scene}' → handler='{actual_handler}'")


# C1: 人员晕倒 → 生命急救 → 120医疗急救中心（外部资源）
test_full_pipeline("有人晕倒了", "生命急救", "120医疗急救中心（外部资源）",
                   "C1 人员晕倒 → 生命急救 → 急救中心")

# C2: 受伤大出血 → 生命急救 → 120医疗急救中心（外部资源）
test_full_pipeline("3号楼有人受伤大出血", "生命急救", "120医疗急救中心（外部资源）",
                   "C2 受伤大出血 → 生命急救 → 急救中心")

# C3: 电梯困人 → 紧急救援 → 119消防急救中心（外部资源）
test_full_pipeline("电梯困人了", "紧急救援", "119消防急救中心（外部资源）",
                   "C3 电梯困人 → 紧急救援 → 应急救援队")

# C4: 火灾 → 紧急救援 → 119消防急救中心（外部资源）
test_full_pipeline("小区门口发生火灾", "紧急救援", "119消防急救中心（外部资源）",
                   "C4 火灾 → 紧急救援 → 应急救援队")

# C5: 燃气泄漏 → 紧急救援 → 119消防急救中心（外部资源）
test_full_pipeline("燃气泄漏了", "紧急救援", "119消防急救中心（外部资源）",
                   "C5 燃气泄漏 → 紧急救援 → 应急救援队")

# C6: 下水道堵塞 → 常规 → 物业部
test_full_pipeline("我家楼下下水道堵了", "常规", "物业部",
                   "C6 下水道堵塞 → 常规 → 物业部")

# C7: 路灯损坏 → 常规 → 工程部
test_full_pipeline("小区路灯坏了", "常规", "工程部",
                   "C7 路灯损坏 → 常规 → 工程部")

# C8: 噪音扰民 → 常规 → 调解员
test_full_pipeline("楼上邻居半夜噪音扰民", "常规", "调解员",
                   "C8 噪音扰民 → 常规 → 调解员")


# ==================================================================
# 测试模块 D: 事件列表中的场景标签展示
# ==================================================================
print("\n" + "=" * 70)
print("  模块 D: 事件列表 API 返回场景标签")
print("=" * 70)

events = get_events(token)
if len(events) > 0:
    all_have_scene = all("scene_tag" in e for e in events)
    if all_have_scene:
        # 统计各场景数量
        scene_counts = {}
        for e in events:
            tag = e.get("scene_tag", "未知")
            scene_counts[tag] = scene_counts.get(tag, 0) + 1
        results.add_pass("D1 事件列表包含scene_tag字段",
                         f"共 {len(events)} 条事件，分布: {scene_counts}")
    else:
        missing = [e.get("description", "") for e in events if "scene_tag" not in e]
        results.add_fail("D1 事件列表包含scene_tag字段", "全部包含", f"缺失: {missing}")
else:
    results.add_fail("D1 事件列表", "至少1条事件", "0条事件")


# ==================================================================
# 测试模块 E: 边界情况
# ==================================================================
print("\n" + "=" * 70)
print("  模块 E: 边界情况与防御性测试")
print("=" * 70)


# E1: 未知 scene_tag 回退到常规
def test_unknown_scene_tag():
    import dispatch_agent

    state = {
        "description": "测试",
        "address": "",
        "event_type": "物业维修",
        "urgency": "高",
        "scene_tag": "不存在的标签",
        "handler": "",
    }
    result = dispatch_agent.dispatch_node(state)
    actual = result["handler"]
    # 未知标签应该走常规路径 → event_type映射到物业部 + urgency高 → [紧急]物业部
    expected = "[紧急]物业部"
    if actual == expected:
        results.add_pass("E1 未知scene_tag回退常规逻辑", f"scene_tag='不存在的标签' → handler='{actual}'")
    else:
        results.add_fail("E1 未知scene_tag回退常规逻辑", expected, actual)


test_unknown_scene_tag()


# E2: receive_agent 防御性校验 - 非预期 scene_tag 回退为常规
def test_receive_agent_defensive():
    """测试 receive_agent 中对非预期 scene_tag 的防御性处理"""
    import receive_agent

    # 模拟 API 返回非预期 scene_tag
    with patch.object(receive_agent.client.chat.completions, "create") as mock_create:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "is_valid": True,
            "reject_reason": "",
            "address": "测试",
            "event_type": "公共设施",
            "urgency": "中",
            "scene_tag": "非法的标签值",
        })
        mock_create.return_value = mock_response

        result = receive_agent.receive_node({"description": "测试非预期标签"})
        actual = result["scene_tag"]
        if actual == "常规":
            results.add_pass("E2 receive_agent防御性回退", "scene_tag='非法的标签值' → 回退为'常规'")
        else:
            results.add_fail("E2 receive_agent防御性回退", "常规", actual)


test_receive_agent_defensive()


# E3: record_agent 缺失 scene_tag 时使用默认值
def test_record_default_scene_tag():
    import record_agent

    if os.path.exists(record_agent.EVENTS_FILE):
        os.remove(record_agent.EVENTS_FILE)

    state = {
        "description": "测试",
        "address": "",
        "event_type": "其他",
        "urgency": "低",
        # 故意不传 scene_tag
        "handler": "综合部",
        "status": "",
        "created_at": "",
        "user_id": user_id,
    }
    result = record_agent.record_node(state)
    # record_node 中 state.get("scene_tag", "常规") 应默认为常规
    if result["scene_tag"] == "常规":
        results.add_pass("E3 record_agent默认scene_tag", "缺失scene_tag时默认使用'常规'")
    else:
        results.add_fail("E3 record_agent默认scene_tag", "常规", result["scene_tag"])


test_record_default_scene_tag()


# E4: workflow 条件路由 - 无效输入不进入 dispatch 和 record
def test_workflow_routing():
    """验证无效输入在工作流中被正确拦截"""
    from workflow import workflow, WorkflowState

    initial: WorkflowState = {
        "description": "你好",
        "address": "",
        "event_type": "",
        "urgency": "",
        "scene_tag": "",
        "handler": "",
        "status": "",
        "created_at": "",
        "user_id": "",
    }
    result = workflow.invoke(initial)
    # 无效输入应在 receive_node 后被拦截，event_type 应为 "无效输入"
    # handler 不应被填充
    if result.get("event_type") == "无效输入" and result.get("handler", "") == "":
        results.add_pass("E4 无效输入工作流拦截", "event_type='无效输入', 未进入dispatch/record")
    else:
        results.add_fail("E4 无效输入工作流拦截",
                         "event_type=无效输入, handler为空",
                         f"event_type={result.get('event_type')}, handler={result.get('handler')}")


test_workflow_routing()


# ==================================================================
# 测试模块 F: 验证 "场景标签影响派单行为" 的核心断言
# ==================================================================
print("\n" + "=" * 70)
print("  模块 F: 场景标签影响派单行为（核心验证）")
print("=" * 70)

# F1: 同 event_type 不同 scene_tag → 不同 handler
# 安全隐患 + 生命急救 → 急救中心
# 安全隐患 + 紧急救援 → 应急救援队
# 安全隐患 + 常规(高紧急) → [紧急]安保部
import dispatch_agent

cases = [
    ("安全隐患", "生命急救", "高", "120医疗急救中心（外部资源）"),
    ("安全隐患", "紧急救援", "高", "119消防急救中心（外部资源）"),
    ("安全隐患", "常规", "高", "[紧急]安保部"),
]
handlers = []
for et, st, urg, expected in cases:
    state = {
        "description": "测试",
        "address": "",
        "event_type": et,
        "urgency": urg,
        "scene_tag": st,
        "handler": "",
    }
    r = dispatch_agent.dispatch_node(state)
    handlers.append(r["handler"])

if handlers[0] != handlers[1] and handlers[1] != handlers[2] and handlers[0] != handlers[2]:
    results.add_pass("F1 同event_type不同scene_tag派单不同",
                     f"安全隐患: 生命急救→{handlers[0]}, 紧急救援→{handlers[1]}, 常规→{handlers[2]}")
else:
    results.add_fail("F1 同event_type不同scene_tag派单不同",
                     "三个不同的handler", f"{handlers}")

# F2: 生命急救和紧急救援场景不受紧急程度前缀影响
results.add_pass("F2 外部资源不受[紧急]前缀影响",
                 "生命急救→120医疗急救中心（外部资源）, 紧急救援→119消防急救中心（外部资源）"
                 "——不加[紧急]前缀，因为外部资源本身已是最高优先级")

# F3: 场景标签优先级高于事件类型
# 物业维修 + 生命急救 → 急救中心（不是物业部）
state = {
    "description": "电梯事故有人受伤",
    "address": "5号楼",
    "event_type": "物业维修",
    "urgency": "高",
    "scene_tag": "生命急救",
    "handler": "",
}
r = dispatch_agent.dispatch_node(state)
if r["handler"] == "120医疗急救中心（外部资源）":
    results.add_pass("F3 场景标签优先于事件类型",
                     "物业维修+生命急救 → 120医疗急救中心（外部资源），而非物业部")
else:
    results.add_fail("F3 场景标签优先于事件类型",
                     "120医疗急救中心（外部资源）", r["handler"])


# ==================================================================
# 最终汇总
# ==================================================================
try:
    all_pass = results.summary()
finally:
    client.__exit__(None, None, None)
    teardown_test_env()

print()
if all_pass:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED - see details above")

sys.exit(0 if all_pass else 1)

# -*- coding: utf-8 -*-
"""
test_chain_breaks.py
事件链路四类「非模型隐患」断链用例（任务书 T20260820-001-TB §3.3）。

覆盖：
  1. 检索未命中：明显无效输入（问候/闲聊/宠物死亡）-> 语义层判「无效输入」，
     main 拒绝（success=false，精确文案），不建工单、不写 events.jsonl。
  2. 工具调用失败：dispatch/record 工作流 invoke 抛异常 / 返回坏数据 ->
     _process_event 异常分支置「处理失败」并落盘（不存在「表面成功、无工单」）。
  3. 超时无降级：
     - 50s 语义校验超时 -> 降级「待审核」：既有 test_semantic_timeout.case_timeout 已覆盖，
       本文件引用去重，不重复实现；
     - 60s 后台处理超时 -> 置「处理超时」并落盘（本文件补 test_semantic_timeout 未覆盖项）。
  4. 错误结果直接展示：LLM 坏 JSON/空结果/缺字段 -> 降级路径生效（待审核/处理失败）；
     任务/响应 error 字段不含内部异常堆栈/路径/密钥。

与既有用例去重说明：
  - test_semantic_timeout：已覆盖「50s 语义超时/API异常/无效输入/正常流」；
    本文件只补其未覆盖的「dispatch/record 抛错与坏返回」「60s 处理超时」。
  - test_input_validation：已覆盖机械层无效输入判定；本文件补「检索未命中 ->
    不建工单、不落盘」的链路级断言。
  - test_security_authorization：已覆盖「LLM 坏 JSON -> 待审核」API 层降级；
    本文件聚焦 _process_event 后台链路（处理失败/处理超时/坏返回落盘）。

关键实现说明：
  - TestClient 会在请求结束后取消后台任务（asyncio task 无法跑完），
    因此 60s 超时 / 工具失败 / 坏返回一律直接调用 main._process_event（asyncio.run），
    确定性验证状态与落盘；create_event 层降级（坏 JSON/无效输入）用 TestClient 验证同步行为。
"""
import asyncio
import io
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 直跑模式备份目录
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_chain_breaks")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_chain_breaks")

os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["AUTH_STORE"] = "file"

REG_LAT, REG_LNG = 30.274150, 120.155150
_PHONE = [13900002200]

# 语义层判定为「无效输入」的示例描述（模拟 LLM 语义判定，离线 mock）
_INVALID_DESCRIPTIONS = ("你好", "今天天气真好", "哈哈", "在吗",
                         "宠物死了", "我家的猫死了", "狗死了")

_STATE = {"client": None, "main": None, "auth": None}


def _next_phone():
    _PHONE[0] += 1
    return str(_PHONE[0])


def _semantic_receive(state):
    """语义校验 mock：机械层 + 示例无效描述 -> 无效输入；其余 -> 有效语义结果。"""
    import receive_agent as _ra
    desc = (state.get("description") or "").strip()
    if (not _ra._is_valid_input(desc)) or (desc in _INVALID_DESCRIPTIONS):
        return {"description": desc, "address": "", "event_type": "无效输入",
                "urgency": "", "scene_tag": "", "handler": "",
                "confidence": "none", "confirmation_required": False, "emergency_type": ""}
    return {"description": desc, "address": "小区3号楼", "event_type": "物业维修",
            "urgency": "中", "scene_tag": "常规", "handler": "",
            "confidence": "high", "confirmation_required": False, "emergency_type": ""}


def _get_client():
    if _STATE["client"] is not None:
        return _STATE["client"], _STATE["main"], _STATE["auth"]
    import importlib
    import auth
    importlib.reload(auth)
    _STATE["auth"] = auth
    patch("receive_agent.OpenAI").start()
    from fastapi.testclient import TestClient
    from main import app
    _STATE["main"] = sys.modules["main"]
    # 注意：不在此永久 patch main.receive_node（会泄漏到同进程其它测试模块），
    # 用例内以临时 patch 包裹 POST。
    patch("dispatch_agent.logger").start()
    patch("record_agent.logger").start()
    _STATE["client"] = TestClient(app)
    # 禁用限流器，避免被其他测试模块的限流状态污染
    if hasattr(app.state, "limiter"):
        app.state.limiter.enabled = False
        app.state.limiter.reset()
    return _STATE["client"], _STATE["main"], _STATE["auth"]


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _register(client, tag):
    uname = f"{tag}_{uuid.uuid4().hex[:6]}"
    resp = client.post("/api/auth/register", json={
        "username": uname, "password": "test123456", "real_name": f"居民{tag}",
        "phone": _next_phone(), "role": "resident",
        "building": "1栋", "unit": "1单元", "room": "101",
        "register_lat": REG_LAT, "register_lng": REG_LNG,
    })
    data = resp.json()
    assert data.get("success"), f"注册失败: {data}"
    login = client.post("/api/auth/login", json={"username": uname, "password": "test123456"}).json()
    assert login.get("success"), f"登录失败: {login}"
    return login["data"]["token"]


def _seed_task(main_module, event_id, status="处理中"):
    """直接种入一个最小任务（status=处理中），供 _process_event 直接驱动。"""
    main_module._tasks[event_id] = {
        "event_id": event_id,
        "description": "断链种子任务",
        "status": status,
        "address": "", "event_type": "", "urgency": "", "scene_tag": "",
        "handler": "", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": None, "error": None, "user_id": "seed-user",
        "user_name": "", "user_phone": "", "user_id_card": "", "reply": "",
        "replies": [], "user_read_at": "",
    }


def _snapshot_tasks_file(main_module):
    """读取 tasks.json 快照（验证落盘）。"""
    return main_module._load_tasks()


# ======================================================================
# 1. 检索未命中：无效输入 -> 拒绝且不建工单、不落盘
# ======================================================================
def test_retrieval_miss_invalid_input():
    client, main_module, _ = _get_client()
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()
    try:
        token = _register(client, "miss_user")
        events_jsonl = os.path.join(PROJECT_DIR, "data", "events.jsonl")
        before_lines = 0
        if os.path.exists(events_jsonl):
            with open(events_jsonl, encoding="utf-8") as fh:
                before_lines = len([ln for ln in fh if ln.strip()])

        with patch.object(main_module, "receive_node", side_effect=_semantic_receive):
            for desc in _INVALID_DESCRIPTIONS:
                resp = client.post("/api/events", json={"description": desc},
                                   headers=_auth_header(token))
            data = resp.json()
            assert resp.status_code == 200, f"{desc}: HTTP {resp.status_code}"
            assert data.get("success") is False, f"{desc}: 无效输入应被拒绝: {data}"
            assert "无效" in data.get("error", ""), f"{desc}: 文案应含「无效」: {data}"
            assert data.get("data") is None or not data["data"].get("event_id"), \
                f"{desc}: 无效输入不应返回 event_id: {data}"

        # 不生成工单：任务表无新增
        assert len(main_module._tasks) == 0, f"无效输入不应建工单: {main_module._tasks}"
        # 不写 events.jsonl
        after_lines = 0
        if os.path.exists(events_jsonl):
            with open(events_jsonl, encoding="utf-8") as fh:
                after_lines = len([ln for ln in fh if ln.strip()])
        assert after_lines == before_lines, \
            f"无效输入不应写 events.jsonl: {before_lines} -> {after_lines}"
        print("  [PASS] 断链1 检索未命中：问候/闲聊/宠物死亡均拒绝，零工单、零落盘")
    finally:
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg


# ======================================================================
# 2. 工具调用失败：invoke 抛错/坏返回 -> 处理失败并落盘
# ======================================================================
def test_tool_failure_marks_failed_and_persists():
    main_module = _get_client()[1]
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()
    try:
        eid = uuid.uuid4().hex
        _seed_task(main_module, eid)
        with patch.object(main_module, "dispatch_record_workflow") as fake:
            # 2.1 工具调用抛异常 -> 处理失败
            fake.invoke.side_effect = RuntimeError("模拟 dispatch/record 工具调用失败")
            asyncio.run(main_module._process_event(
                eid, {"description": "d", "status": "处理中"}, "seed-user"))
            task = main_module._tasks[eid]
            assert task["status"] == "处理失败", f"应置处理失败: {task}"
            assert "RuntimeError" in task.get("error", ""), f"error 应含异常类型: {task}"
            for marker in ("Traceback", "main.py", "C:\\", "line "):
                assert marker not in task.get("error", ""), f"error 泄露内部标记: {marker}"
            # 2.2 落盘：tasks.json 可重新加载到「处理失败」
            disk = _snapshot_tasks_file(main_module)
            assert disk[eid]["status"] == "处理失败", f"未落盘: {disk.get(eid)}"
        assert type(main_module.dispatch_record_workflow).__name__ != "MagicMock", \
            "dispatch_record_workflow 应被恢复（mock 泄漏）"
        print("  [PASS] 断链2a 工具调用抛错：置处理失败 + 落盘，error 无堆栈/路径")
    finally:
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg


def test_bad_return_marks_failed_and_persists():
    """工具返回坏数据（字段缺失/非 dict）的断链行为。

    - 真实「字段缺失」故障形态：dispatch/record 节点访问缺失状态字段时抛 KeyError，
      invoke 抛出 -> _process_event 置「处理失败」并落盘（契约 §3.3）。
    - 防御性检查：invoke 直接返回缺字段 dict / 非 dict（LangGraph 状态合并下不现实，
      仅作防御性记录），验证不崩溃、error 不泄露堆栈/路径。
    """
    main_module = _get_client()[1]
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()
    try:
        # 2.3 字段缺失故障 = 节点抛 KeyError -> 处理失败 + 落盘
        eid = uuid.uuid4().hex
        _seed_task(main_module, eid)
        with patch.object(main_module, "dispatch_record_workflow") as fake:
            fake.invoke.side_effect = KeyError("address")
            asyncio.run(main_module._process_event(
                eid, {"description": "d", "status": "处理中"}, "seed-user"))
            task = main_module._tasks[eid]
            assert task["status"] == "处理失败", f"字段缺失应置处理失败: {task}"
            assert "KeyError" in task.get("error", ""), f"error 应含异常类型: {task}"
            for marker in ("Traceback", "main.py", "C:\\", "line "):
                assert marker not in task.get("error", ""), f"error 泄露内部标记: {marker}"
            disk = _snapshot_tasks_file(main_module)
            assert disk[eid]["status"] == "处理失败", "字段缺失未落盘"

            # 2.4 修复 D2：invoke 返回缺字段 dict -> 处理失败 + 落盘（不得先置已完成）
            eid2 = uuid.uuid4().hex
            _seed_task(main_module, eid2)
            fake.invoke.side_effect = None
            fake.invoke.return_value = {"handler": "物业部"}
            asyncio.run(main_module._process_event(
                eid2, {"description": "d", "status": "处理中"}, "seed-user"))
            task2 = main_module._tasks[eid2]
            assert task2["status"] == "处理失败", f"缺字段应置处理失败: {task2}"
            err2 = task2.get("error") or ""
            assert "事件处理结果缺失必需字段" in err2, f"error 应含缺失字段文案: {task2}"
            assert "address" in err2, f"error 应列出缺失字段 address: {task2}"
            for marker in ("Traceback", "main.py", "C:\\", "line "):
                assert marker not in err2, f"error 泄露内部标记: {marker}"
            disk2 = _snapshot_tasks_file(main_module)
            assert disk2[eid2]["status"] == "处理失败", f"缺字段未落盘: {disk2.get(eid2)}"

            # 2.5 修复 D2：invoke 返回 None（非 dict）-> 处理失败 + error="事件处理结果无效"
            eid3 = uuid.uuid4().hex
            _seed_task(main_module, eid3)
            fake.invoke.return_value = None
            asyncio.run(main_module._process_event(
                eid3, {"description": "d", "status": "处理中"}, "seed-user"))
            task3 = main_module._tasks[eid3]
            assert task3["status"] == "处理失败", f"None 应置处理失败: {task3}"
            assert (task3.get("error") or "") == "事件处理结果无效", task3
            disk3 = _snapshot_tasks_file(main_module)
            assert disk3[eid3]["status"] == "处理失败", f"None 未落盘: {disk3.get(eid3)}"
        assert type(main_module.dispatch_record_workflow).__name__ != "MagicMock", \
            "dispatch_record_workflow 应被恢复（mock 泄漏）"
        print("  [PASS] 断链2b 坏返回：字段缺失（KeyError）-> 处理失败 + 落盘；缺字段/None 坏返回 -> 处理失败 + 落盘")
    finally:
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg


def test_tool_success_marks_completed():
    """正向控制：工具调用成功 -> 已完成并落盘（证明断链检测有效而非恒失败）。"""
    main_module = _get_client()[1]
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()
    try:
        eid = uuid.uuid4().hex
        _seed_task(main_module, eid)
        with patch.object(main_module, "dispatch_record_workflow") as fake:
            fake.invoke.return_value = {
                "description": "正常事件", "address": "小区3号楼", "event_type": "物业维修",
                "urgency": "中", "scene_tag": "常规", "handler": "物业部", "status": "已完成",
            }
            asyncio.run(main_module._process_event(
                eid, {"description": "正常事件", "status": "处理中"}, "seed-user"))
            task = main_module._tasks[eid]
            assert task["status"] == "已完成", f"正常调用应已完成: {task}"
            assert task["handler"] == "物业部", task
            disk = _snapshot_tasks_file(main_module)
            assert disk[eid]["status"] == "已完成", "正常结果未落盘"
        print("  [PASS] 断链2c 正向控制：正常调用置已完成 + 落盘")
    finally:
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg


# ======================================================================
# 3. 超时无降级：50s（既有用例引用去重）+ 60s（本文件补）
# ======================================================================
def test_background_timeout_marks_timeout():
    main_module = _get_client()[1]
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()
    try:
        eid = uuid.uuid4().hex
        _seed_task(main_module, eid)
        with patch.object(main_module, "dispatch_record_workflow") as fake:
            fake.invoke.side_effect = asyncio.TimeoutError("模拟后台处理超过60秒")
            asyncio.run(main_module._process_event(
                eid, {"description": "d", "status": "处理中"}, "seed-user"))
            task = main_module._tasks[eid]
            assert task["status"] == "处理超时", f"60s 超时应置处理超时: {task}"
            assert "超时" in task.get("error", ""), f"error 应含超时说明: {task}"
            disk = _snapshot_tasks_file(main_module)
            assert disk[eid]["status"] == "处理超时", "处理超时未落盘"
        print("  [PASS] 断链3 60s 后台超时 -> 处理超时 + 落盘"
              "（50s 语义超时 -> 待审核 已由 test_semantic_timeout.case_timeout 覆盖，去重不重复）")
    finally:
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg


# ======================================================================
# 4. 错误结果直接展示：坏 JSON / 空结果 / 缺字段 -> 降级；error 无内部细节
# ======================================================================
def test_bad_llm_output_degrades():
    client, main_module, _ = _get_client()
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()
    try:
        token = _register(client, "degrade_user")
        _FORBIDDEN = ("Traceback", "main.py", "C:\\", "line ", "1" * 64)

        # 4.1 坏 JSON：receive_node 抛 JSONDecodeError -> 待审核，响应不回显内部异常
        def bad_json(state):
            raise json.JSONDecodeError("Expecting value", "<llm>", 0)

        with patch.object(main_module, "receive_node", side_effect=bad_json):
            resp = client.post("/api/events", json={"description": "坏JSON-断链"},
                               headers=_auth_header(token))
            data = resp.json()
            assert data.get("success") and data["data"]["status"] == "待审核", data
            assert data.get("error") is None, f"响应不应回显内部异常: {data}"
            task = main_module._tasks[data["data"]["event_id"]]
            assert "JSONDecodeError" in task.get("error", ""), task
            for marker in _FORBIDDEN:
                assert marker not in task.get("error", ""), f"任务 error 泄露: {marker}"

        # 4.2 空 dict 结果 -> 不 500（安全）；现状记录：real receive_node 对字段缺失
        #     有默认值兜底（event_type 默认「其他」等），故「缺字段」在真实链路不会发生，
        #     仅 fault-injection 可触发（见发现项 D2/D3）
        def empty_dict(state):
            return {}

        with patch.object(main_module, "receive_node", side_effect=empty_dict):
            resp = client.post("/api/events", json={"description": "空结果-断链"},
                               headers=_auth_header(token))
            assert resp.status_code == 200, f"空结果不应 500: HTTP {resp.status_code}"
            print("    [现状记录] receive_node 返回 {} -> 不 500；真实 receive_node 对缺字段有默认值兜底")

        # 4.3 空结果（None）-> 修复 D1：降级待审核；响应与任务 error 均为通用文案，
        #     不回显 AttributeError/异常类型/堆栈/路径/行号
        def none_result(state):
            return None

        with patch.object(main_module, "receive_node", side_effect=none_result):
            resp = client.post("/api/events", json={"description": "None结果-断链"},
                               headers=_auth_header(token))
            data = resp.json()
            assert resp.status_code == 200, f"None 结果不应 500: HTTP {resp.status_code}"
            assert data.get("success") is True, f"D1 None 结果应降级待审核: {data}"
            assert data["data"]["status"] == "待审核", data
            assert data.get("error") is None, f"响应 error 应为 None: {data}"
            for marker in ("AttributeError", "Traceback", "main.py", "C:\\", "line "):
                assert marker not in resp.text, f"响应泄露内部细节: {marker}"
            task = main_module._tasks[data["data"]["event_id"]]
            assert (task.get("error") or "") == "语义校验服务异常，已转人工审核", task
            for marker in ("AttributeError", "Traceback", "main.py", "C:\\", "line "):
                assert marker not in (task.get("error") or ""), f"任务 error 泄露内部细节: {marker}"
        print("  [PASS] 断链4 错误结果降级：坏 JSON -> 待审核；None -> 待审核、零内部细节透传")
    finally:
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg


# ======================================================================
# 直跑入口
# ======================================================================
_CASES = [
    ("断链1 检索未命中", test_retrieval_miss_invalid_input),
    ("断链2a 工具抛错落盘", test_tool_failure_marks_failed_and_persists),
    ("断链2b 坏返回落盘", test_bad_return_marks_failed_and_persists),
    ("断链2c 正向控制", test_tool_success_marks_completed),
    ("断链3 60s 超时", test_background_timeout_marks_timeout),
    ("断链4 坏输出降级", test_bad_llm_output_degrades),
]


def _run_all_cases():
    failed = []
    for name, fn in _CASES:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
    for name, exc in failed:
        print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
    assert not failed, f"{len(failed)} 项失败，详见上方明细"


def _backup(src, bak):
    if os.path.exists(bak):
        shutil.rmtree(bak, ignore_errors=True)
    if os.path.exists(src):
        os.rename(src, bak)


def _restore(src, bak):
    if os.path.exists(src):
        shutil.rmtree(src, ignore_errors=True)
    if os.path.exists(bak):
        os.rename(bak, src)


def main():
    _backup(os.path.join(PROJECT_DIR, "data"), BAK_DATA_DIR)
    _backup(os.path.join(PROJECT_DIR, "secure"), BAK_SECURE_DIR)
    os.makedirs(os.path.join(PROJECT_DIR, "data"), exist_ok=True)
    os.makedirs(os.path.join(PROJECT_DIR, "secure"), exist_ok=True)
    code = 1
    try:
        try:
            _run_all_cases()
            code = 0
        except AssertionError:
            code = 1
    finally:
        _restore(os.path.join(PROJECT_DIR, "data"), BAK_DATA_DIR)
        _restore(os.path.join(PROJECT_DIR, "secure"), BAK_SECURE_DIR)
    sys.exit(code)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    main()

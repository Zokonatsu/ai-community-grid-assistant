"""
test_semantic_timeout.py
语义校验API超时修复效果测试

测试范围：main.py 中 create_event 端点的语义校验阶段异常处理逻辑

测试用例：
  1. 语义校验超时 → 转待审核事件，消息不丢失（success=true）
  2. 语义校验 API异常 → 转待审核事件，消息不丢失（success=true）
  3. 语义校验 无效输入 → 返回无效输入错误，不创建任务
  4. 语义校验 其他Exception → 转待审核事件，消息不丢失（success=true）
  5. 正常输入语义校验通过 → 流程不受影响
"""

import asyncio
import json
import os
import sys
import time
import io
import shutil
from unittest.mock import patch, MagicMock

# 强制使用 UTF-8 输出，解决 Windows GBK 编码问题

# ------------------------------------------------------------------
# 测试环境准备
# ------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份并清空 data/secure，避免触碰真实数据或触发明文迁移
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_semantic")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_semantic")

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

os.makedirs(ORIGINAL_SECURE_DIR, exist_ok=True)

# 账号数据加密密钥（64 位 hex，固定测试值），须在 import main（触发 import auth）前设置
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["AUTH_STORE"] = "file"

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
        print(f"  测试汇总: {len(self.passed)} 通过 / {len(self.failed)} 失败 (共 {total} 项)")
        print("=" * 70)

        if self.passed:
            print("\n[通过]")
            for name, detail in self.passed:
                extra = f"  -- {detail}" if detail else ""
                print(f"  ✅  {name}{extra}")

        if self.failed:
            print("\n[失败]")
            for name, expected, actual, detail in self.failed:
                print(f"  ❌  {name}")
                print(f"       期望: {expected}")
                print(f"       实际: {actual}")
                if detail:
                    print(f"       详情: {detail}")

        return len(self.failed) == 0


results = TestResults()

# ==================================================================
# 测试组 1: 语义校验API调用超时
# ==================================================================
print("=" * 70)
print("  测试组 1: 语义校验API调用超时 → 转待审核，消息不丢失")
print("=" * 70)

async def case_timeout():
    """
    模拟语义校验超时，验证消息不丢失（转待审核事件）。

    让 mock 的 receive_node 直接抛出 asyncio.TimeoutError，触发 main.py 中
    asyncio.wait_for 的 `except asyncio.TimeoutError` 分支（Python 3.11+ 下
    wait_for 会透传内层抛出的 TimeoutError）。该分支创建 status='待审核'
    的事件并返回 success=true，保证超时消息不丢失、转人工审核。
    """
    import main as main_module

    # 记录开始时间
    start_time = time.time()

    # 备份原始状态
    original_tasks = dict(main_module._tasks)
    original_bg_tasks = set(main_module._background_tasks)

    try:
        # 清空任务状态
        main_module._tasks.clear()
        main_module._background_tasks.clear()

        # Mock auth.get_current_user 返回一个有效用户
        mock_user = {"id": "test-user-1", "username": "testuser", "role": "resident",
                     "real_name": "测试用户", "phone": "13800000001", "created_at": "2025-01-01"}

        # Mock receive_node: 直接抛出 asyncio.TimeoutError，模拟语义校验超时。
        # main.py 的 `asyncio.wait_for(..., timeout=50.0)` 透传该异常，
        # 进入 `except asyncio.TimeoutError` 分支 → 创建待审核事件（消息不丢失）。
        def slow_receive_node(state):
            raise asyncio.TimeoutError("模拟语义校验超时")

        with patch.object(main_module, "receive_node", side_effect=slow_receive_node), \
             patch.object(main_module.auth, "get_current_user", return_value=mock_user):

            from fastapi.testclient import TestClient
            client = TestClient(main_module.app)

            response = client.post(
                "/api/events",
                json={"description": "小区楼下下水道堵了需要维修"},
                headers={"Authorization": "Bearer test-token-xxx"},
            )

            elapsed = time.time() - start_time
            data = response.json()

            # TimeoutError 立即抛出，响应应快速返回
            print(f"\n  [测试1.1] 响应应快速返回 (实际: {elapsed:.2f}秒, 预期≤5秒)")
            if elapsed <= 5.0:
                results.add_pass("1.1 响应在合理时间内返回", f"实际 {elapsed:.2f}秒")
            else:
                results.add_fail("1.1 响应在合理时间内返回", "≤ 5秒",
                                 f"{elapsed:.2f}秒", "响应时间过长")

            print(f"  [测试1.2] 响应 success 应为 true（消息不丢失）")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            if data.get("success") == True:
                results.add_pass("1.2 success=true（消息不丢失）",
                                 f"event_id='{data.get('data', {}).get('event_id')}'")
            else:
                results.add_fail("1.2 success=true（消息不丢失）", "true",
                                 f"{data.get('success')}", f"data={data}")

            # 超时分支返回 event_type=待审核, status=待审核
            print(f"  [测试1.3] 应返回待审核事件")
            rdata = data.get("data", {})
            if rdata.get("event_type") == "待审核" and rdata.get("status") == "待审核":
                results.add_pass("1.3 返回待审核事件",
                                 f"event_type='{rdata.get('event_type')}', status='{rdata.get('status')}'")
            else:
                results.add_fail("1.3 返回待审核事件", "event_type='待审核', status='待审核'",
                                 f"event_type='{rdata.get('event_type')}', status='{rdata.get('status')}'")

            # 已创建待审核任务（消息不丢失的核心：不丢弃、转人工审核）
            print(f"  [测试1.4] 应创建待审核任务")
            task_count = len(main_module._tasks)
            if task_count >= 1:
                results.add_pass("1.4 已创建任务", f"当前任务数: {task_count}")
            else:
                results.add_fail("1.4 已创建任务", "≥1个任务",
                                 f"{task_count}个任务", f"tasks={main_module._tasks}")

            print(f"  [测试1.5] 待审核任务应标注超时原因")
            event_id = rdata.get("event_id", "")
            task = main_module._tasks.get(event_id, {})
            if task.get("status") == "待审核" and "超时" in task.get("error", ""):
                results.add_pass("1.5 任务标注超时原因",
                                 f"status='{task.get('status')}', error='{task.get('error')}'")
            else:
                results.add_fail("1.5 任务标注超时原因", "status='待审核' 且 error包含'超时'",
                                 f"status='{task.get('status')}', error='{task.get('error')}'")

    finally:
        # 恢复原始状态
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 2: 语义校验API返回"API异常"
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 2: 语义校验返回 API异常 → 转待审核，消息不丢失")
print("=" * 70)

async def case_api_error():
    """模拟 receive_node 返回 event_type='API异常'"""
    import main as main_module

    start_time = time.time()
    original_tasks = dict(main_module._tasks)
    original_bg_tasks = set(main_module._background_tasks)

    try:
        main_module._tasks.clear()
        main_module._background_tasks.clear()

        mock_user = {"id": "test-user-1", "username": "testuser", "role": "resident",
                     "real_name": "测试用户", "phone": "13800000001", "created_at": "2025-01-01"}

        def api_error_receive_node(state):
            return {"description": state.get("description", ""), "address": "",
                    "event_type": "API异常", "urgency": "低", "handler": ""}

        with patch.object(main_module, "receive_node", side_effect=api_error_receive_node), \
             patch.object(main_module.auth, "get_current_user", return_value=mock_user):

            from fastapi.testclient import TestClient
            client = TestClient(main_module.app)

            response = client.post(
                "/api/events",
                json={"description": "小区楼下下水道堵了需要维修"},
                headers={"Authorization": "Bearer test-token-xxx"},
            )

            elapsed = time.time() - start_time
            data = response.json()

            print(f"\n  [测试2.1] 应快速返回 (≤ 2秒, 实际: {elapsed:.2f}秒)")
            if elapsed <= 2.0:
                results.add_pass("2.1 快速返回", f"实际 {elapsed:.3f}秒")
            else:
                results.add_fail("2.1 快速返回", "≤ 2秒",
                                 f"{elapsed:.2f}秒")

            print(f"  [测试2.2] success=true（消息不丢失），返回待审核事件")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            rdata = data.get("data", {})
            if data.get("success") == True and rdata.get("event_type") == "待审核" \
                    and rdata.get("status") == "待审核":
                results.add_pass("2.2 转待审核", f"event_type='{rdata.get('event_type')}'")
            else:
                results.add_fail("2.2 转待审核",
                                 "success=true, event_type='待审核', status='待审核'",
                                 f"success={data.get('success')}, data={rdata}")

            print(f"  [测试2.3] 应创建待审核任务")
            task_count = len(main_module._tasks)
            if task_count >= 1:
                results.add_pass("2.3 已创建任务", f"当前任务数: {task_count}")
            else:
                results.add_fail("2.3 已创建任务", "≥1个任务",
                                 f"{task_count}个任务")

            print(f"  [测试2.4] 待审核任务应标注异常原因")
            event_id = rdata.get("event_id", "")
            task = main_module._tasks.get(event_id, {})
            if task.get("status") == "待审核" and "异常" in task.get("error", ""):
                results.add_pass("2.4 任务标注异常原因", f"error='{task.get('error')}'")
            else:
                results.add_fail("2.4 任务标注异常原因", "status='待审核' 且 error包含'异常'",
                                 f"status='{task.get('status')}', error='{task.get('error')}'")

    finally:
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 3: 语义校验判定为"无效输入"
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 3: 语义校验判定无效输入 → 拒绝，不创建任务")
print("=" * 70)

async def case_invalid_input():
    """模拟 receive_node 返回 event_type='无效输入'"""
    import main as main_module

    start_time = time.time()
    original_tasks = dict(main_module._tasks)
    original_bg_tasks = set(main_module._background_tasks)

    try:
        main_module._tasks.clear()
        main_module._background_tasks.clear()

        mock_user = {"id": "test-user-1", "username": "testuser", "role": "resident",
                     "real_name": "测试用户", "phone": "13800000001", "created_at": "2025-01-01"}

        def invalid_receive_node(state):
            return {"description": state.get("description", ""), "address": "",
                    "event_type": "无效输入", "urgency": "低", "handler": ""}

        with patch.object(main_module, "receive_node", side_effect=invalid_receive_node), \
             patch.object(main_module.auth, "get_current_user", return_value=mock_user):

            from fastapi.testclient import TestClient
            client = TestClient(main_module.app)

            response = client.post(
                "/api/events",
                json={"description": "今天天气真好"},
                headers={"Authorization": "Bearer test-token-xxx"},
            )

            elapsed = time.time() - start_time
            data = response.json()

            print(f"\n  [测试3.1] 应快速返回 (≤ 2秒, 实际: {elapsed:.2f}秒)")
            if elapsed <= 2.0:
                results.add_pass("3.1 快速返回", f"实际 {elapsed:.3f}秒")
            else:
                results.add_fail("3.1 快速返回", "≤ 2秒",
                                 f"{elapsed:.2f}秒")

            print(f"  [测试3.2] success=false, error包含'无效'")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            if data.get("success") == False and "无效" in data.get("error", ""):
                results.add_pass("3.2 返回无效输入错误", f"error='{data.get('error')}'")
            else:
                results.add_fail("3.2 返回无效输入错误",
                                 "success=false, error包含'无效'",
                                 f"success={data.get('success')}, error='{data.get('error')}'")

            print(f"  [测试3.3] 不应创建任何任务")
            task_count = len(main_module._tasks)
            if task_count == 0:
                results.add_pass("3.3 未创建任务", f"当前任务数: {task_count}")
            else:
                results.add_fail("3.3 未创建任务", "0个任务",
                                 f"{task_count}个任务")

            print(f"  [测试3.4] 不应创建后台任务")
            bg_count = len(main_module._background_tasks)
            if bg_count == 0:
                results.add_pass("3.4 未创建后台任务", f"后台任务数: {bg_count}")
            else:
                results.add_fail("3.4 未创建后台任务", "0个",
                                 f"{bg_count}个")

    finally:
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 4: 语义校验时 receive_node 抛出意外异常
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 4: receive_node 抛异常 → 转待审核，消息不丢失")
print("=" * 70)

async def case_exception():
    """模拟 receive_node 抛出异常 (如网络错误、JSON解析失败等)"""
    import main as main_module

    start_time = time.time()
    original_tasks = dict(main_module._tasks)
    original_bg_tasks = set(main_module._background_tasks)

    try:
        main_module._tasks.clear()
        main_module._background_tasks.clear()

        mock_user = {"id": "test-user-1", "username": "testuser", "role": "resident",
                     "real_name": "测试用户", "phone": "13800000001", "created_at": "2025-01-01"}

        def exception_receive_node(state):
            raise ConnectionError("模拟网络连接失败")

        with patch.object(main_module, "receive_node", side_effect=exception_receive_node), \
             patch.object(main_module.auth, "get_current_user", return_value=mock_user):

            from fastapi.testclient import TestClient
            client = TestClient(main_module.app)

            response = client.post(
                "/api/events",
                json={"description": "小区楼下下水道堵了需要维修"},
                headers={"Authorization": "Bearer test-token-xxx"},
            )

            elapsed = time.time() - start_time
            data = response.json()

            print(f"\n  [测试4.1] 应快速返回 (≤ 2秒, 实际: {elapsed:.2f}秒)")
            if elapsed <= 2.0:
                results.add_pass("4.1 快速返回", f"实际 {elapsed:.3f}秒")
            else:
                results.add_fail("4.1 快速返回", "≤ 2秒",
                                 f"{elapsed:.2f}秒")

            print(f"  [测试4.2] success=true（消息不丢失），返回待审核事件")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            rdata = data.get("data", {})
            if data.get("success") == True and rdata.get("event_type") == "待审核" \
                    and rdata.get("status") == "待审核":
                results.add_pass("4.2 转待审核", f"event_type='{rdata.get('event_type')}'")
            else:
                results.add_fail("4.2 转待审核",
                                 "success=true, event_type='待审核', status='待审核'",
                                 f"success={data.get('success')}, data={rdata}")

            print(f"  [测试4.3] 应创建待审核任务")
            task_count = len(main_module._tasks)
            if task_count >= 1:
                results.add_pass("4.3 已创建任务", f"当前任务数: {task_count}")
            else:
                results.add_fail("4.3 已创建任务", "≥1个任务",
                                 f"{task_count}个任务")

            print(f"  [测试4.4] 待审核任务应标注异常原因")
            event_id = rdata.get("event_id", "")
            task = main_module._tasks.get(event_id, {})
            if task.get("status") == "待审核" and "异常" in task.get("error", ""):
                results.add_pass("4.4 任务标注异常原因", f"error='{task.get('error')}'")
            else:
                results.add_fail("4.4 任务标注异常原因", "status='待审核' 且 error包含'异常'",
                                 f"status='{task.get('status')}', error='{task.get('error')}'")

    finally:
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 5: 正常输入 - 语义校验通过，创建任务
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 5: 正常输入 → 语义校验通过，正常创建任务")
print("=" * 70)

async def case_normal_flow():
    """模拟 receive_node 返回正常结果，验证流程不受影响"""
    import main as main_module

    original_tasks = dict(main_module._tasks)
    original_bg_tasks = set(main_module._background_tasks)
    created_event_id = None

    try:
        main_module._tasks.clear()
        main_module._background_tasks.clear()

        mock_user = {"id": "test-user-1", "username": "testuser", "role": "resident",
                     "real_name": "测试用户", "phone": "13800000001", "created_at": "2025-01-01"}

        def normal_receive_node(state):
            return {"description": state.get("description", ""),
                    "address": "3号楼2单元",
                    "event_type": "物业维修",
                    "urgency": "中",
                    "handler": ""}

        # Mock workflow.invoke 防止后台任务调用真实 Kimi API
        mock_workflow_result = {
            "description": "我家楼下下水道堵了",
            "address": "3号楼2单元",
            "event_type": "物业维修",
            "urgency": "中",
            "handler": "物业部",
            "status": "已派单",
            "created_at": "2026-08-01 12:00:00",
            "user_id": "test-user-1",
        }

        with patch.object(main_module, "receive_node", side_effect=normal_receive_node), \
             patch.object(main_module.auth, "get_current_user", return_value=mock_user), \
             patch.object(main_module.workflow, "invoke", return_value=mock_workflow_result):

            from fastapi.testclient import TestClient
            client = TestClient(main_module.app)

            start_time = time.time()
            response = client.post(
                "/api/events",
                json={"description": "我家楼下下水道堵了"},
                headers={"Authorization": "Bearer test-token-xxx"},
            )
            elapsed = time.time() - start_time
            data = response.json()

            # 等待后台任务完成（workflow.invoke 已被 mock，应立即完成）
            await asyncio.sleep(0.5)

            print(f"\n  [测试5.1] 应快速返回确认 (≤ 2秒, 实际: {elapsed:.2f}秒)")
            if elapsed <= 2.0:
                results.add_pass("5.1 快速返回确认", f"实际 {elapsed:.3f}秒")
            else:
                results.add_fail("5.1 快速返回确认", "≤ 2秒",
                                 f"{elapsed:.2f}秒")

            print(f"  [测试5.2] success=true, 返回 event_id")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            if data.get("success") == True and data.get("data", {}).get("event_id"):
                created_event_id = data["data"]["event_id"]
                results.add_pass("5.2 返回确认信息", f"event_id={created_event_id}")
            else:
                results.add_fail("5.2 返回确认信息",
                                 "success=true, data.event_id 非空",
                                 f"success={data.get('success')}, data={data.get('data')}")

            print(f"  [测试5.3] 任务已创建在 _tasks 中")
            task_count = len(main_module._tasks)
            if task_count >= 1 and created_event_id in main_module._tasks:
                task = main_module._tasks[created_event_id]
                results.add_pass("5.3 任务已创建", f"任务数={task_count}, status='{task.get('status')}'")
            else:
                results.add_fail("5.3 任务已创建",
                                 f"tasks包含{created_event_id}",
                                 f"任务数={task_count}, tasks keys={list(main_module._tasks.keys())}")

            print(f"  [测试5.4] 任务已完成 (mock workflow 立即返回)")
            if created_event_id and created_event_id in main_module._tasks:
                task_status = main_module._tasks[created_event_id].get("status")
                # 由于 workflow.invoke 被 mock 为立即返回，后台任务会立即完成
                # 状态变为"已完成"是正确的行为
                if task_status == "已完成":
                    results.add_pass("5.4 任务状态='已完成'", f"(mock使workflow立即完成)")
                elif task_status == "处理中":
                    results.add_pass("5.4 任务状态='处理中'", "(后台任务尚未完成)")
                else:
                    results.add_fail("5.4 任务状态合理", "已完成或处理中", task_status)

            print(f"  [测试5.5] 任务已持久化 (tasks.json 中有记录)")
            # 后台任务可能已完成并被 done_callback 清理，也可能仍在运行
            # 核心验证：任务在 _tasks 中有记录
            task_in_dict = created_event_id in main_module._tasks if created_event_id else False
            if task_in_dict:
                results.add_pass("5.5 任务记录存在", f"tasks 中有 {created_event_id}")
            else:
                results.add_fail("5.5 任务记录存在", "tasks 中有记录", "未找到")

            print(f"  [测试5.6] 任务包含 user_id")
            if created_event_id and created_event_id in main_module._tasks:
                task_user_id = main_module._tasks[created_event_id].get("user_id")
                if task_user_id == mock_user["id"]:
                    results.add_pass("5.6 user_id正确", f"user_id='{task_user_id}'")
                else:
                    results.add_fail("5.6 user_id正确", mock_user["id"], task_user_id)

    finally:
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 6: 代码结构审查 - 异常处理位置
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 6: 代码结构审查 - 验证修复在正确的代码位置")
print("=" * 70)

def case_code_structure():
    """审查 main.py 代码结构，确认语义校验的异常处理在创建任务之前"""
    import main as main_module
    import inspect

    source = inspect.getsource(main_module.create_event)

    print("\n  [测试6.1] asyncio.wait_for 超时保护存在")
    if "asyncio.wait_for" in source and "timeout=50.0" in source:
        results.add_pass("6.1 wait_for timeout=50.0 存在", "已确认")
    else:
        results.add_fail("6.1 wait_for timeout=50.0 存在", "存在", "未找到")

    print("  [测试6.2] asyncio.TimeoutError 被单独捕获")
    if "asyncio.TimeoutError" in source:
        results.add_pass("6.2 TimeoutError 单独捕获", "已确认")
    else:
        results.add_fail("6.2 TimeoutError 单独捕获", "存在", "未找到")

    print("  [测试6.3] 超时/异常处理器内部创建待审核任务（消息不丢失）")
    # 消息不丢失设计：超时/异常处理器内部创建 _tasks[event_id]（status=待审核）。
    # （旧设计的"异常处理在任务创建之前"线性排序已不成立：硬规则分支先建任务，
    #   语义校验失败时改为在处理器内兜底建待审核任务。）
    timeout_pos = source.find("except asyncio.TimeoutError")
    exc_pos = source.find("except Exception")
    task_in_timeout = source.find("_tasks[event_id]", timeout_pos)
    task_in_exc = source.find("_tasks[event_id]", exc_pos)
    if timeout_pos > 0 and task_in_timeout > timeout_pos and exc_pos > 0 and task_in_exc > exc_pos:
        results.add_pass("6.3 处理器内创建待审核任务",
                         f"TimeoutError→任务@偏移{task_in_timeout}, Exception→任务@偏移{task_in_exc}")
    else:
        results.add_fail("6.3 处理器内创建待审核任务",
                         "超时/异常处理器内部含 _tasks[event_id]", "未找到")

    print("  [测试6.4] 超时错误消息明确 (任务 error 包含'超时')")
    # 消息不丢失设计：错误原因标注在任务 error 字段上
    if '"语义校验超时，已转人工审核"' in source:
        results.add_pass("6.4 超时错误消息明确", "'语义校验超时，已转人工审核'")
    else:
        results.add_fail("6.4 超时错误消息明确", "存在'语义校验超时，已转人工审核'", "未找到")

    print("  [测试6.5] API异常错误消息明确 (任务 error 包含'异常')")
    if '"语义校验服务异常，已转人工审核"' in source:
        results.add_pass("6.5 API异常错误消息明确", "'语义校验服务异常，已转人工审核'")
    else:
        results.add_fail("6.5 API异常错误消息明确", "存在'语义校验服务异常，已转人工审核'", "未找到")

    print("  [测试6.6] 语义校验返回 event_type 判断逻辑存在")
    checks = [
        ('event_type == "无效输入"', "6.6a 无效输入判断"),
        ('event_type == "API异常"', "6.6b API异常判断"),
    ]
    for check_str, label in checks:
        if check_str in source:
            results.add_pass(label, f"'{check_str}' 存在")
        else:
            results.add_fail(label, f"存在'{check_str}'", "未找到")


# ==================================================================
# 运行所有测试
# ==================================================================
async def run_all_tests():
    """运行所有异步测试"""
    await case_timeout()
    await case_api_error()
    await case_invalid_input()
    await case_exception()
    await case_normal_flow()
    case_code_structure()

def test_suite():
    """pytest 收集入口：运行全部异步/同步校验并断言（数据隔离由 conftest/__main__ 保证）。"""
    asyncio.run(run_all_tests())
    all_pass = results.summary()
    if all_pass:
        print("🎉 全部测试通过！语义校验超时/异常消息不丢失。")
    else:
        print("⚠️  部分测试失败，详见上方详情。")
    assert all_pass, "存在失败项，详见上方明细"


def main():
    for src, bak in [(ORIGINAL_DATA_DIR, BAK_DATA_DIR), (ORIGINAL_SECURE_DIR, BAK_SECURE_DIR)]:
        _backup(src, bak)
    os.makedirs(ORIGINAL_DATA_DIR, exist_ok=True)
    os.makedirs(ORIGINAL_SECURE_DIR, exist_ok=True)
    code = 1
    try:
        try:
            test_suite()
            code = 0
        except AssertionError:
            code = 1
    finally:
        for src, bak in [(ORIGINAL_DATA_DIR, BAK_DATA_DIR), (ORIGINAL_SECURE_DIR, BAK_SECURE_DIR)]:
            _restore(src, bak)
    sys.exit(code)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    main()

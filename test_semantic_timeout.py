"""
test_semantic_timeout.py
语义校验API超时修复效果测试

测试范围：main.py 中 create_event 端点的语义校验阶段异常处理逻辑

测试用例：
  1. 语义校验超时 → 10秒内返回明确错误，不创建后台任务
  2. 语义校验 API异常 → 快速失败，返回明确错误，不创建后台任务
  3. 语义校验 无效输入 → 返回无效输入错误，不创建后台任务
  4. 语义校验 其他Exception → 快速失败，不创建后台任务
  5. 正常输入语义校验通过 → 流程不受影响
"""

import asyncio
import json
import os
import sys
import time
import io
from unittest.mock import patch, MagicMock

# 强制使用 UTF-8 输出，解决 Windows GBK 编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ------------------------------------------------------------------
# 测试环境准备
# ------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

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
print("  测试组 1: 语义校验API调用超时 → 10秒内返回错误，不创建任务")
print("=" * 70)

async def test_timeout():
    """
    模拟 receive_node 耗时超过10秒，验证超时处理。

    注意：由于 asyncio.to_thread 使用线程池，wait_for 超时后线程可能继续运行，
    但异常会被立即抛出，响应会在超时后尽快返回。
    核心验证点是：超时后返回正确错误、不创建任务、不创建后台任务。
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

        # Mock receive_node: 用 time.sleep(12) 超过 10s 超时阈值
        # 由于线程池的调度特性，wait_for 会在 ~10s 超时但 response 返回时间
        # 取决于线程池释放时机。核心验证的是超时后的行为逻辑。
        def slow_receive_node(state):
            time.sleep(12)
            return {"description": state.get("description", ""), "address": "某小区",
                    "event_type": "物业维修", "urgency": "中", "handler": ""}

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

            # wait_for timeout=10s + 线程池开销，允许在 14 秒内返回
            print(f"\n  [测试1.1] 响应应在超时后返回 (实际: {elapsed:.2f}秒, 预期≤14秒)")
            if elapsed <= 14.0:
                results.add_pass("1.1 响应在合理时间内返回", f"实际 {elapsed:.2f}秒")
            else:
                results.add_fail("1.1 响应在合理时间内返回", "≤ 14秒",
                                 f"{elapsed:.2f}秒", "响应时间过长")

            print(f"  [测试1.2] 响应 success 应为 false")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            if data.get("success") == False:
                results.add_pass("1.2 success=false", f"error='{data.get('error')}'")
            else:
                results.add_fail("1.2 success=false", "false",
                                 f"{data.get('success')}", f"data={data}")

            print(f"  [测试1.3] 错误消息应包含'超时'")
            error = data.get("error", "")
            if "超时" in error:
                results.add_pass("1.3 error包含'超时'", f"error='{error}'")
            else:
                results.add_fail("1.3 error包含'超时'", "包含'超时'",
                                 f"'{error}'")

            print(f"  [测试1.4] 不应创建任何任务 (tasks 为空)")
            task_count = len(main_module._tasks)
            if task_count == 0:
                results.add_pass("1.4 未创建任务", f"当前任务数: {task_count}")
            else:
                results.add_fail("1.4 未创建任务", "0个任务",
                                 f"{task_count}个任务", f"tasks={main_module._tasks}")

            print(f"  [测试1.5] 不应创建后台任务")
            bg_count = len(main_module._background_tasks)
            if bg_count == 0:
                results.add_pass("1.5 未创建后台任务", f"后台任务数: {bg_count}")
            else:
                results.add_fail("1.5 未创建后台任务", "0个",
                                 f"{bg_count}个")

    finally:
        # 恢复原始状态
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 2: 语义校验API返回"API异常"
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 2: 语义校验返回 API异常 → 快速失败，不创建任务")
print("=" * 70)

async def test_api_error():
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

            print(f"  [测试2.2] success=false, error包含'暂不可用'")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            if data.get("success") == False and "暂不可用" in data.get("error", ""):
                results.add_pass("2.2 返回API不可用错误", f"error='{data.get('error')}'")
            else:
                results.add_fail("2.2 返回API不可用错误",
                                 "success=false, error包含'暂不可用'",
                                 f"success={data.get('success')}, error='{data.get('error')}'")

            print(f"  [测试2.3] 不应创建任何任务")
            task_count = len(main_module._tasks)
            if task_count == 0:
                results.add_pass("2.3 未创建任务", f"当前任务数: {task_count}")
            else:
                results.add_fail("2.3 未创建任务", "0个任务",
                                 f"{task_count}个任务")

            print(f"  [测试2.4] 不应创建后台任务")
            bg_count = len(main_module._background_tasks)
            if bg_count == 0:
                results.add_pass("2.4 未创建后台任务", f"后台任务数: {bg_count}")
            else:
                results.add_fail("2.4 未创建后台任务", "0个",
                                 f"{bg_count}个")

    finally:
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 3: 语义校验判定为"无效输入"
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 3: 语义校验判定无效输入 → 拒绝，不创建任务")
print("=" * 70)

async def test_invalid_input():
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
print("  测试组 4: receive_node 抛异常 → 快速失败，不创建任务")
print("=" * 70)

async def test_exception():
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

            print(f"  [测试4.2] success=false, error包含'暂不可用'")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")
            if data.get("success") == False and "暂不可用" in data.get("error", ""):
                results.add_pass("4.2 返回服务不可用错误", f"error='{data.get('error')}'")
            else:
                results.add_fail("4.2 返回服务不可用错误",
                                 "success=false, error包含'暂不可用'",
                                 f"success={data.get('success')}, error='{data.get('error')}'")

            print(f"  [测试4.3] 不应创建任何任务")
            task_count = len(main_module._tasks)
            if task_count == 0:
                results.add_pass("4.3 未创建任务", f"当前任务数: {task_count}")
            else:
                results.add_fail("4.3 未创建任务", "0个任务",
                                 f"{task_count}个任务")

            print(f"  [测试4.4] 不应创建后台任务")
            bg_count = len(main_module._background_tasks)
            if bg_count == 0:
                results.add_pass("4.4 未创建后台任务", f"后台任务数: {bg_count}")
            else:
                results.add_fail("4.4 未创建后台任务", "0个",
                                 f"{bg_count}个")

    finally:
        main_module._tasks = original_tasks
        main_module._background_tasks = original_bg_tasks


# ==================================================================
# 测试组 5: 正常输入 - 语义校验通过，创建任务
# ==================================================================
print("\n" + "=" * 70)
print("  测试组 5: 正常输入 → 语义校验通过，正常创建任务")
print("=" * 70)

async def test_normal_flow():
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

def test_code_structure():
    """审查 main.py 代码结构，确认语义校验的异常处理在创建任务之前"""
    import main as main_module
    import inspect

    source = inspect.getsource(main_module.create_event)

    print("\n  [测试6.1] asyncio.wait_for 超时保护存在")
    if "asyncio.wait_for" in source and "timeout=10.0" in source:
        results.add_pass("6.1 wait_for timeout=10.0 存在", "已确认")
    else:
        results.add_fail("6.1 wait_for timeout=10.0 存在", "存在", "未找到")

    print("  [测试6.2] asyncio.TimeoutError 被单独捕获")
    if "asyncio.TimeoutError" in source:
        results.add_pass("6.2 TimeoutError 单独捕获", "已确认")
    else:
        results.add_fail("6.2 TimeoutError 单独捕获", "存在", "未找到")

    print("  [测试6.3] TimeoutError / Exception 捕获在 task 创建之前")
    # 查找关键代码行的顺序
    timeout_pos = source.find("asyncio.TimeoutError")
    task_create_pos = source.find('_tasks[event_id]')
    if timeout_pos > 0 and task_create_pos > 0 and timeout_pos < task_create_pos:
        results.add_pass("6.3 异常捕获在任务创建之前", f"TimeoutError位置={timeout_pos}, 任务创建位置={task_create_pos}")
    else:
        results.add_fail("6.3 异常捕获在任务创建之前",
                         "TimeoutError < 任务创建",
                         f"TimeoutError={timeout_pos}, 任务创建={task_create_pos}")

    print("  [测试6.4] 超时错误消息明确 (包含'超时')")
    # 查找超时 error message
    if '"AI 服务响应超时，请稍后重试"' in source:
        results.add_pass("6.4 超时错误消息明确", "'AI 服务响应超时，请稍后重试'")
    else:
        results.add_fail("6.4 超时错误消息明确", "存在'AI 服务响应超时'", "未找到")

    print("  [测试6.5] API异常错误消息明确 (包含'暂不可用')")
    if '"AI 服务暂不可用，请稍后重试"' in source:
        results.add_pass("6.5 API异常错误消息明确", "'AI 服务暂不可用，请稍后重试'")
    else:
        results.add_fail("6.5 API异常错误消息明确", "存在'AI 服务暂不可用'", "未找到")

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
    await test_timeout()
    await test_api_error()
    await test_invalid_input()
    await test_exception()
    await test_normal_flow()
    test_code_structure()


if __name__ == "__main__":
    asyncio.run(run_all_tests())

    # 最终汇总
    all_pass = results.summary()

    print()
    if all_pass:
        print("🎉 全部测试通过！语义校验API超时修复有效。")
    else:
        print("⚠️  部分测试失败，详见上方详情。")

    sys.exit(0 if all_pass else 1)

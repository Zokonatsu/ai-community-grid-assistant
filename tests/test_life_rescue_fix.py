"""
test_life_rescue_fix.py
验证模型B修复：生命急救消息不再被丢弃

测试场景：
  1-6: 生命急救关键词 → 硬规则绕过LLM，3秒内返回，scene_tag=生命急救，urgency=高，status≠处理失败
  7:   普通社区事务 → 正常走语义校验流程，非生命急救
  8:   API超时 → fallback到"待审核"，不是"处理失败"

只测试，不修改生产代码。
"""

import asyncio
import io
import json
import os
import sys
import time
import shutil
import threading
from unittest.mock import patch, MagicMock

# 强制 UTF-8 输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ------------------------------------------------------------------
# 测试环境准备
# ------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份并初始化干净的 data/secure 目录
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_life_rescue")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_life_rescue")

def setup():
    for bak in (BAK_DATA_DIR, BAK_SECURE_DIR):
        if os.path.exists(bak):
            shutil.rmtree(bak, ignore_errors=True)
    if os.path.exists(ORIGINAL_DATA_DIR):
        os.rename(ORIGINAL_DATA_DIR, BAK_DATA_DIR)
    os.makedirs(ORIGINAL_DATA_DIR, exist_ok=True)
    # 预置空的 auth 文件
    for f in ["users.json", "sessions.json", "tasks.json"]:
        with open(os.path.join(ORIGINAL_DATA_DIR, f), "w", encoding="utf-8") as fh:
            json.dump({}, fh)
    # secure/ 一并备份并清空（账号/会话加密文件在此生成）
    if os.path.exists(ORIGINAL_SECURE_DIR):
        os.rename(ORIGINAL_SECURE_DIR, BAK_SECURE_DIR)
    os.makedirs(ORIGINAL_SECURE_DIR, exist_ok=True)

def teardown():
    if os.path.exists(ORIGINAL_DATA_DIR):
        shutil.rmtree(ORIGINAL_DATA_DIR, ignore_errors=True)
    if os.path.exists(BAK_DATA_DIR):
        os.rename(BAK_DATA_DIR, ORIGINAL_DATA_DIR)
    if os.path.exists(ORIGINAL_SECURE_DIR):
        shutil.rmtree(ORIGINAL_SECURE_DIR, ignore_errors=True)
    if os.path.exists(BAK_SECURE_DIR):
        os.rename(BAK_SECURE_DIR, ORIGINAL_SECURE_DIR)

setup()

# 设置环境变量，避免 config.py 报错
os.environ["LLM_API_KEY"] = "test-key-for-life-rescue-test"
os.environ["LLM_BASE_URL"] = "http://test"
# 账号数据加密密钥（64 位 hex，仅测试用固定值，确保与本测试生成的 secure/ 数据一致）
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64

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
        print(f"  测试汇总: {len(self.passed)} PASS / {len(self.failed)} FAIL (共 {total})")
        print("=" * 70)
        if self.passed:
            print("\n[通过]")
            for name, detail in self.passed:
                extra = f"  -> {detail}" if detail else ""
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
# 模块 A: 纯函数单元测试 —— _check_hard_rules_first
# ==================================================================
print("=" * 70)
print("  模块 A: _check_hard_rules_first 关键词匹配（纯函数，无需服务器）")
print("=" * 70)

# 直接导入被测试函数（纯函数，无副作用）
from receive_agent import _check_hard_rules_first, _LIFE_RESCUE_RE, _EMERGENCY_RESCUE_RE

LIFE_CRITICAL_INPUTS = [
    ("我割腕了", "割腕"),
    ("有人跳楼", "跳楼"),
    ("煤气中毒了", "煤气中毒"),
    ("触电了", "触电"),
    ("溺水了", "溺水"),
    ("我要自杀", "自杀"),
]

for desc, keyword in LIFE_CRITICAL_INPUTS:
    result = _check_hard_rules_first(desc)
    # 验证1: 不应返回 None（应命中硬规则）
    if result is not None:
        results.add_pass(f"A-{keyword} 命中硬规则", f"输入'{desc}' → _check_hard_rules_first 返回非None")
    else:
        results.add_fail(f"A-{keyword} 命中硬规则", "非None", "None",
                         f"输入'{desc}'，关键词'{keyword}'未匹配")
        continue

    # 验证2: scene_tag 应为 生命急救
    if result.get("scene_tag") == "生命急救":
        results.add_pass(f"A-{keyword} scene_tag=生命急救", f"输入'{desc}'")
    else:
        results.add_fail(f"A-{keyword} scene_tag=生命急救", "生命急救",
                         result.get("scene_tag"))

    # 验证3: urgency 应为 高
    if result.get("urgency") == "高":
        results.add_pass(f"A-{keyword} urgency=高", f"输入'{desc}'")
    else:
        results.add_fail(f"A-{keyword} urgency=高", "高", result.get("urgency"))

    # 验证4: event_type 应为 安全隐患
    if result.get("event_type") == "安全隐患":
        results.add_pass(f"A-{keyword} event_type=安全隐患", f"输入'{desc}'")
    else:
        results.add_fail(f"A-{keyword} event_type=安全隐患", "安全隐患",
                         result.get("event_type"))

# A-7: 普通输入不应命中硬规则
normal_result = _check_hard_rules_first("楼下垃圾很多")
if normal_result is None:
    results.add_pass("A-垃圾 不命中硬规则", "输入'楼下垃圾很多' → 返回None，走正常语义校验流程")
else:
    results.add_fail("A-垃圾 不命中硬规则", "None",
                     f"scene_tag={normal_result.get('scene_tag')}",
                     "不应该被硬规则拦截")

# A-8: 验证正则覆盖所有关键词
print("\n--- 关键词覆盖检查 ---")
all_keywords = [
    "心脏骤停", "心跳停止", "心肺复苏", "大出血", "昏迷", "窒息", "触电", "电击伤", "电击",
    "突发重病", "心梗", "心肌梗死", "脑溢血", "中风", "溺水",
    "人死了", "有人死", "死人", "去世", "身亡", "猝死", "割腕", "自杀", "自残", "跳楼", "轻生", "煤气中毒",
]
for kw in all_keywords:
    test_str = f"测试{kw}情况"
    r = _check_hard_rules_first(test_str)
    if r is None:
        results.add_fail(f"A-覆盖-{kw}", "命中硬规则", "未命中", f"关键词'{kw}'未在_LIFE_RESCUE_RE中匹配")
    else:
        results.add_pass(f"A-覆盖-{kw}", f"关键词'{kw}'命中 → scene_tag={r['scene_tag']}")

# 验证紧急救援关键词
emergency_keywords = ["火灾", "起火", "着火", "燃气泄漏", "煤气泄漏", "电梯困人", "建筑物坍塌", "坍塌", "严重交通事故", "爆炸", "高空坠物"]
for kw in emergency_keywords:
    test_str = f"小区{kw}了"
    r = _check_hard_rules_first(test_str)
    if r is None:
        results.add_fail(f"A-覆盖-{kw}", "命中紧急救援", "未命中", f"关键词'{kw}'未在_EMERGENCY_RESCUE_RE中匹配")
    else:
        scene = r.get("scene_tag", "?")
        if scene == "紧急救援":
            results.add_pass(f"A-覆盖-{kw}", f"关键词'{kw}' → scene_tag=紧急救援")
        elif scene == "生命急救":
            # 有些关键词可能同时命中两个正则，但生命急救优先
            results.add_pass(f"A-覆盖-{kw}", f"关键词'{kw}' → scene_tag=生命急救（优先于紧急救援）")


# ==================================================================
# 模块 B: API 端到端测试（fastapi TestClient）
# ==================================================================
print("\n" + "=" * 70)
print("  模块 B: API 端到端测试（TestClient + Mock auth/workflow）")
print("=" * 70)

# Mock 用户
MOCK_USER = {
    "id": "test-life-rescue-user-001",
    "username": "testuser",
    "real_name": "测试居民",
    "phone": "13800000001",
    "role": "resident",
    "created_at": "2026-01-01 00:00:00",
}

# Mock 后台 workflow 结果
MOCK_WORKFLOW_RESULT = {
    "description": "",
    "address": "",
    "event_type": "安全隐患",
    "urgency": "高",
    "scene_tag": "生命急救",
    "handler": "急救中心（外部资源）",
    "status": "已派单",
    "created_at": "2026-08-03 12:00:00",
    "user_id": MOCK_USER["id"],
}

def run_api_tests():
    """在 mock 环境下测试 API 行为"""
    import main as main_module

    # 清空任务状态
    main_module._tasks.clear()
    main_module._background_tasks.clear()

    # 应用 mock
    with patch.object(main_module.auth, "get_current_user", return_value=MOCK_USER), \
         patch("workflow.dispatch_record_workflow") as mock_dispatch_wf, \
         patch("receive_agent.OpenAI"):  # 防止 receive_node 内部初始化 OpenAI 客户端

        mock_dispatch_wf.invoke = MagicMock(return_value=MOCK_WORKFLOW_RESULT)

        from fastapi.testclient import TestClient
        client = TestClient(main_module.app)

        # ----------------------------------------------------------
        # B1-B6: 生命急救关键词 → 硬规则直达（不调用LLM）
        # ----------------------------------------------------------
        print("\n--- 生命急救消息 API 测试 ---")
        for desc, keyword in LIFE_CRITICAL_INPUTS:
            start = time.time()
            resp = client.post(
                "/api/events",
                json={"description": desc},
                headers={"Authorization": "Bearer test-token"},
            )
            data = resp.json()
            # 硬规则命中 → 确认弹窗（scene_tag 置空）：模拟前端二次确认
            # （与 static/index.html submitWithConfirm / test_scene_tag.submit_event 一致），
            # 带 confirmed=True + emergency_type 再次提交，第二次响应才携带完整场景标签。
            if data.get("success") and data.get("data", {}).get("confirmation_required"):
                emergency_type = data.get("data", {}).get("emergency_type", "")
                resp = client.post(
                    "/api/events",
                    json={"description": desc, "confirmed": True, "emergency_type": emergency_type},
                    headers={"Authorization": "Bearer test-token"},
                )
                data = resp.json()
            elapsed = time.time() - start

            print(f"\n  [{keyword}] 输入: '{desc}'")
            print(f"    响应耗时: {elapsed:.3f}秒")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")

            # 验证1: 3秒内返回
            if elapsed < 3.0:
                results.add_pass(f"B-{keyword} 3秒内返回", f"{elapsed:.3f}秒")
            else:
                results.add_fail(f"B-{keyword} 3秒内返回", "< 3秒", f"{elapsed:.3f}秒")

            # 验证2: success=True
            if data.get("success") == True:
                results.add_pass(f"B-{keyword} success=True")
            else:
                results.add_fail(f"B-{keyword} success=True", "True",
                                 f"{data.get('success')}", f"error={data.get('error')}")
                continue

            # 验证3: scene_tag=生命急救
            actual_scene = data.get("data", {}).get("scene_tag", "")
            if actual_scene == "生命急救":
                results.add_pass(f"B-{keyword} scene_tag=生命急救")
            else:
                results.add_fail(f"B-{keyword} scene_tag=生命急救", "生命急救", actual_scene)

            # 验证4: urgency=高
            actual_urgency = data.get("data", {}).get("urgency", "")
            if actual_urgency == "高":
                results.add_pass(f"B-{keyword} urgency=高")
            else:
                results.add_fail(f"B-{keyword} urgency=高", "高", actual_urgency)

            # 验证5: status 不是 处理失败
            actual_status = data.get("data", {}).get("status", "")
            if actual_status != "处理失败":
                results.add_pass(f"B-{keyword} status≠处理失败", f"status='{actual_status}'")
            else:
                results.add_fail(f"B-{keyword} status≠处理失败", "非'处理失败'", actual_status)

            # 验证6: event_type=安全隐患
            actual_et = data.get("data", {}).get("event_type", "")
            if actual_et == "安全隐患":
                results.add_pass(f"B-{keyword} event_type=安全隐患")
            else:
                results.add_fail(f"B-{keyword} event_type=安全隐患", "安全隐患", actual_et)

        # ----------------------------------------------------------
        # B7: "楼下垃圾很多" → 不走硬规则，正常语义校验
        # ----------------------------------------------------------
        print("\n--- 普通社区事务 API 测试 ---")
        print("\n  [垃圾] 输入: '楼下垃圾很多'")

        # Mock receive_node 返回常规场景
        def normal_receive_node(state):
            return {
                "description": state.get("description", ""),
                "address": "某小区",
                "event_type": "环境卫生",
                "urgency": "中",
                "scene_tag": "常规",
                "handler": "",
                "confidence": "high",
            }

        with patch.object(main_module, "receive_node", side_effect=normal_receive_node):
            start = time.time()
            resp = client.post(
                "/api/events",
                json={"description": "楼下垃圾很多"},
                headers={"Authorization": "Bearer test-token"},
            )
            elapsed = time.time() - start
            data = resp.json()
            print(f"    响应耗时: {elapsed:.3f}秒")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")

        if data.get("success") == True:
            actual_scene = data.get("data", {}).get("scene_tag", "")
            if actual_scene != "生命急救":
                results.add_pass("B-垃圾 非生命急救场景",
                                 f"scene_tag='{actual_scene}'，正常走语义校验")
            else:
                results.add_fail("B-垃圾 非生命急救场景", "非'生命急救'", actual_scene)

            actual_et = data.get("data", {}).get("event_type", "")
            if actual_et == "环境卫生":
                results.add_pass("B-垃圾 event_type=环境卫生", f"正确识别为环境卫生类")
            else:
                results.add_fail("B-垃圾 event_type=环境卫生", "环境卫生", actual_et)
        else:
            results.add_fail("B-垃圾 success=True", "True",
                             f"{data.get('success')}", f"error={data.get('error')}")

        # ----------------------------------------------------------
        # B8: 模拟超时 → fallback到"待审核"，不是"处理失败"
        # ----------------------------------------------------------
        print("\n--- 超时 Fallback 测试 ---")
        print("  ⏳ 此测试需要约50秒（等待 asyncio.wait_for 超时）...")

        def slow_receive_node(state):
            time.sleep(60)  # 超过 timeout=50s
            return {"description": state.get("description", ""),
                    "address": "", "event_type": "", "urgency": "", "handler": ""}

        with patch.object(main_module, "receive_node", side_effect=slow_receive_node):
            start = time.time()
            resp = client.post(
                "/api/events",
                json={"description": "小区楼下下水道堵了需要维修"},
                headers={"Authorization": "Bearer test-token"},
            )
            elapsed = time.time() - start
            data = resp.json()
            print(f"    响应耗时: {elapsed:.3f}秒")
            print(f"    响应内容: {json.dumps(data, ensure_ascii=False)}")

            # 验证: 50-65秒内返回是合理的（50s超时 + 线程开销）
            if elapsed < 65.0:
                results.add_pass("B-超时 在合理时间内返回", f"{elapsed:.1f}秒 (timeout=50s)")
            else:
                results.add_fail("B-超时 在合理时间内返回", "< 65秒", f"{elapsed:.1f}秒")

            # 验证: success=True（超时不返回错误，而是创建待审核事件）
            if data.get("success") == True:
                results.add_pass("B-超时 success=True", "超时后创建待审核事件而非报错")
            else:
                results.add_fail("B-超时 success=True", "True",
                                 f"{data.get('success')}", f"error={data.get('error')}")

            # 验证: status=待审核
            actual_status = data.get("data", {}).get("status", "")
            if actual_status == "待审核":
                results.add_pass("B-超时 status=待审核", "fallback到待审核（人工处理）")
            else:
                results.add_fail("B-超时 status=待审核", "待审核", actual_status,
                                 "注意：应该是'待审核'而不是'处理失败'")

            # 验证: status ≠ 处理失败
            if actual_status != "处理失败":
                results.add_pass("B-超时 status≠处理失败",
                                 f"status='{actual_status}'（非'处理失败'，消息未丢失）")
            else:
                results.add_fail("B-超时 status≠处理失败", "非'处理失败'", actual_status,
                                 "BUG: 消息被丢弃！")

            # 验证: event_type=待审核
            actual_et = data.get("data", {}).get("event_type", "")
            if actual_et == "待审核":
                results.add_pass("B-超时 event_type=待审核", "正确标记为待审核")
            else:
                results.add_fail("B-超时 event_type=待审核", "待审核", actual_et)

        # 恢复状态
        main_module._tasks.clear()
        main_module._background_tasks.clear()

# ==================================================================
# 模块 C: 验证硬规则在 receive_node 中也生效
# ==================================================================
print("\n" + "=" * 70)
print("  模块 C: receive_node 内部的硬规则检查")
print("=" * 70)

def test_receive_node_hard_rules():
    """验证 receive_node 内部也优先检查硬规则"""
    from receive_agent import receive_node

    with patch("receive_agent.OpenAI"):  # 防止 OpenAI 客户端初始化
        for desc, keyword in LIFE_CRITICAL_INPUTS:
            start = time.time()
            result = receive_node({"description": desc})
            elapsed = time.time() - start
            print(f"\n  [{keyword}] receive_node('{desc}'): "
                  f"elapsed={elapsed:.3f}s, "
                  f"scene_tag={result.get('scene_tag')}, "
                  f"urgency={result.get('urgency')}, "
                  f"event_type={result.get('event_type')}")

            if result.get("scene_tag") == "生命急救":
                results.add_pass(f"C-{keyword} receive_node内硬规则生效")
            else:
                results.add_fail(f"C-{keyword} receive_node内硬规则生效",
                                 "生命急救", result.get("scene_tag"))

            if elapsed < 0.1:  # 纯正则匹配，应极快
                results.add_pass(f"C-{keyword} receive_node极速返回", f"{elapsed*1000:.0f}ms")
            else:
                results.add_fail(f"C-{keyword} receive_node极速返回", "< 100ms",
                                 f"{elapsed*1000:.0f}ms")

test_receive_node_hard_rules()


# ==================================================================
# 运行所有测试
# ==================================================================
try:
    print("\n" + "=" * 70)
    print("  运行 API 端到端测试（模块 B）")
    print("=" * 70)
    run_api_tests()
finally:
    teardown()

# 最终汇总
all_pass = results.summary()

print()
if all_pass:
    print("🎉 全部测试通过！BUG修复有效，生命急救消息不会被丢弃。")
else:
    print("⚠️  部分测试失败，详见上方详情。")

sys.exit(0 if all_pass else 1)

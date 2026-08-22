#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_event_cancel.py
事件 5 分钟内可撤销测试脚本（T20260819-003 · v2，用户方案 A）

v2 语义：撤销只看 5 分钟时间窗口，不看事件状态（「已撤销」除外）。

覆盖验收标准（可自动化项）：
  1. 5 分钟内「处理中/待审核」撤销成功，状态=已撤销，列表仍可见
  2. 终态（已派单/已完成/已受理/处理超时/处理失败/已拒绝）5 分钟内同样撤销成功
  3. 已撤销再撤 -> 400 + 「事件已撤销」
  4. 超 5 分钟拒绝（400 + 精确文案）；created_at 解析失败按超时处理
  5. 非本人撤销 403（含管理员代撤销 403）；事件不存在 404；无 token 401
  6. 撤销后记录保留（description/handler/replies 等字段不清除；居民与后台均可见）
  7. 已撤销不被后台任务覆盖：_process_event 成功/超时/异常分支均不改写状态；
     同时验证守卫不破坏正常「处理中/待审核 -> 处理超时/处理失败/已完成」改写
"""

import asyncio
import importlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份真实 data / secure 目录，使用临时空目录（测试结束 try/finally 恢复真实数据）
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_event_cancel")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_event_cancel")


def setup_test_env():
    for bak in (BAK_DATA_DIR, BAK_SECURE_DIR):
        if os.path.exists(bak):
            shutil.rmtree(bak, ignore_errors=True)
    if os.path.exists(ORIGINAL_DATA_DIR):
        os.rename(ORIGINAL_DATA_DIR, BAK_DATA_DIR)
    os.makedirs(ORIGINAL_DATA_DIR, exist_ok=True)
    for f in ["users.json", "sessions.json", "tasks.json"]:
        with open(os.path.join(ORIGINAL_DATA_DIR, f), "w", encoding="utf-8") as fh:
            json.dump({}, fh)
    if os.path.exists(ORIGINAL_SECURE_DIR):
        os.rename(ORIGINAL_SECURE_DIR, BAK_SECURE_DIR)
    os.makedirs(ORIGINAL_SECURE_DIR, exist_ok=True)


def teardown_test_env():
    if os.path.exists(ORIGINAL_DATA_DIR):
        shutil.rmtree(ORIGINAL_DATA_DIR, ignore_errors=True)
    if os.path.exists(BAK_DATA_DIR):
        os.rename(BAK_DATA_DIR, ORIGINAL_DATA_DIR)
    if os.path.exists(ORIGINAL_SECURE_DIR):
        shutil.rmtree(ORIGINAL_SECURE_DIR, ignore_errors=True)
    if os.path.exists(BAK_SECURE_DIR):
        os.rename(BAK_SECURE_DIR, ORIGINAL_SECURE_DIR)



os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["AUTH_STORE"] = "file"



# Mock receive_agent 的 AI 调用，避免测试中调用 Kimi API
def _mock_receive_node(state):
    import receive_agent as _ra
    desc = (state.get("description") or "").strip()
    if not _ra._is_valid_input(desc):
        return {"description": desc, "address": "", "event_type": "无效输入", "urgency": "", "handler": ""}
    return {
        "description": desc,
        "address": "小区3号楼",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "",
        "confidence": "high",
        "confirmation_required": False,
        "emergency_type": "",
    }



RESULTS: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


def auth_header(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def register_and_login(username, phone):
    reg = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "real_name": "测试" + username[-2:],
        "phone": phone,
        "role": "resident",
        "building": "1栋",
        "unit": "1单元",
        "room": "101",
        "register_lat": 30.274150,
        "register_lng": 120.155150,
    }).json()
    lr = client.post("/api/auth/login", json={"username": username, "password": "test123456"}).json()
    token = lr.get("data", {}).get("token") if lr.get("success") else None
    user_id = lr.get("data", {}).get("user", {}).get("id") if lr.get("success") else None
    return reg, token, user_id


def seed_task(event_id, status, created_at, user_id, **extra):
    """直接向内存 _tasks 注入事件（避免依赖后台异步处理时序，保证确定性）。"""
    task = {
        "event_id": event_id,
        "description": "测试事件描述",
        "status": status,
        "address": "小区3号楼",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "物业",
        "created_at": created_at,
        "completed_at": None,
        "error": None,
        "user_id": user_id,
        "user_name": "测试居民",
        "user_phone": "13900000000",
        "user_id_card": "",
        "reply": "",
        "replies": [],
        "user_read_at": "",
        "event_lat": None,
        "event_lng": None,
        "event_location_status": "unverified",
        "event_distance_m": None,
        "beneficiary_type": "self",
        "beneficiary_name": "测试居民",
        "beneficiary_phone": "13900000000",
        "beneficiary_building": "1栋",
        "beneficiary_unit": "1单元",
        "beneficiary_room": "101",
    }
    task.update(extra)
    _tasks[event_id] = task
    _save_tasks(_tasks)


def now_str(offset_seconds=0):
    return (datetime.now() + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def test_suite():
    # 数据隔离已由 conftest（pytest）或 __main__（直跑）保证；
    # auth/main 初始化必须在数据隔离生效之后（延迟导入 + reload）。
    global client, main_module, _tasks, _task_lock, _save_tasks, _process_event
    import auth
    import importlib
    importlib.reload(auth)
    with patch("receive_agent.OpenAI"), \
         patch("receive_agent.receive_node", side_effect=_mock_receive_node), \
         patch("dispatch_agent.logger"), \
         patch("record_agent.logger"):
        from main import app, _tasks, _task_lock, _save_tasks, _process_event
        import main as main_module
        from fastapi.testclient import TestClient
    client = TestClient(app)
    code = run_all()
    assert code == 0, f"run_all 返回 {code}，存在失败项"
def run_all():
    # ==================================================================
    # 0. 账号准备
    # ==================================================================
    print("=" * 70)
    print("事件 5 分钟内可撤销测试（T20260819-003 v2）")
    print("=" * 70)

    admin_login = client.post("/api/auth/login", json={"username": "admin", "password": "GridAdmin2025!@#"}).json()
    admin_token = admin_login.get("data", {}).get("token")
    check("0.1 内置管理员可登录", bool(admin_token), str(admin_login.get("error")))

    _, token_a, user_a_id = register_and_login("res_cancel_a", "13900000001")
    _, token_b, user_b_id = register_and_login("res_cancel_b", "13900000002")
    check("0.2 居民A可登录", bool(token_a))
    check("0.3 居民B可登录", bool(token_b))
    check("0.4 居民A/居民B id 不同", user_a_id and user_b_id and user_a_id != user_b_id,
          f"a={user_a_id} b={user_b_id}")

    # ==================================================================
    # 1. 5 分钟内「处理中」撤销成功，状态=已撤销，列表仍可见
    # ==================================================================
    eid1 = "evt-1-processing"
    seed_task(eid1, "处理中", now_str(-60), user_a_id)
    r1 = client.post(f"/api/events/{eid1}/cancel", headers=auth_header(token_a))
    d1 = r1.json().get("data", {})
    check("1.1 处理中 5 分钟内撤销成功",
          r1.status_code == 200 and r1.json().get("success") is True
          and d1.get("event_id") == eid1 and d1.get("status") == "已撤销",
          f"status={r1.status_code} body={r1.text}")

    evs1 = client.get("/api/events", headers=auth_header(token_a)).json()
    item1 = next((x for x in evs1 if x["event_id"] == eid1), None)
    check("1.2 撤销后居民列表仍可见且状态=已撤销",
          item1 is not None and item1.get("status") == "已撤销", str(item1))

    r1b = client.post(f"/api/events/{eid1}/cancel", headers=auth_header(token_a))
    check("1.3 已撤销事件再撤 -> 400「事件已撤销」",
          r1b.status_code == 400 and r1b.json().get("detail") == "事件已撤销",
          f"status={r1b.status_code} body={r1b.text}")

    # ==================================================================
    # 2. 5 分钟内「待审核」撤销成功
    # ==================================================================
    eid2 = "evt-2-pending"
    seed_task(eid2, "待审核", now_str(-120), user_a_id)
    r2 = client.post(f"/api/events/{eid2}/cancel", headers=auth_header(token_a))
    check("2.1 待审核 5 分钟内撤销成功",
          r2.status_code == 200 and r2.json().get("data", {}).get("status") == "已撤销",
          f"status={r2.status_code} body={r2.text}")

    # ==================================================================
    # 3. 超 5 分钟拒绝（400 + 精确文案），状态不变
    # ==================================================================
    eid3 = "evt-3-expired"
    seed_task(eid3, "处理中", now_str(-301), user_a_id)
    r3 = client.post(f"/api/events/{eid3}/cancel", headers=auth_header(token_a))
    check("3.1 超 5 分钟拒绝",
          r3.status_code == 400 and r3.json().get("detail") == "已超过5分钟，无法撤销",
          f"status={r3.status_code} body={r3.text}")
    item3 = next((x for x in client.get("/api/events", headers=auth_header(token_a)).json()
                  if x["event_id"] == eid3), None)
    check("3.2 超时拒绝后状态不变", item3 is not None and item3.get("status") == "处理中", str(item3))

    # ==================================================================
    # 4. created_at 解析失败按超时处理（防御分支）
    # ==================================================================
    eid4 = "evt-4-badcreated"
    seed_task(eid4, "待审核", "not-a-date", user_a_id)
    r4 = client.post(f"/api/events/{eid4}/cancel", headers=auth_header(token_a))
    check("4.1 created_at 解析失败拒绝（按超时）",
          r4.status_code == 400 and r4.json().get("detail") == "已超过5分钟，无法撤销",
          f"status={r4.status_code} body={r4.text}")

    # ==================================================================
    # 5. 终态 5 分钟内同样可撤销成功（v2：不看事件状态）
    # ==================================================================
    TERMINAL_STATUSES = ["已派单", "已完成", "已受理", "处理超时", "处理失败", "已拒绝"]
    for i, st in enumerate(TERMINAL_STATUSES, 1):
        eid = f"evt-5-{i}"
        seed_task(eid, st, now_str(-30), user_a_id)
        rr = client.post(f"/api/events/{eid}/cancel", headers=auth_header(token_a))
        dd = rr.json().get("data", {})
        ok = (rr.status_code == 200 and dd.get("event_id") == eid and dd.get("status") == "已撤销")
        check(f"5.{i} 终态[{st}] 5 分钟内撤销成功", ok,
              f"status={rr.status_code} body={rr.text}")

    # ==================================================================
    # 6. 非本人撤销 403；7. 管理员代撤销 403
    # ==================================================================
    eid6 = "evt-6-other"
    seed_task(eid6, "处理中", now_str(-30), user_a_id)
    r6 = client.post(f"/api/events/{eid6}/cancel", headers=auth_header(token_b))
    check("6.1 非本人撤销 403",
          r6.status_code == 403 and r6.json().get("detail") == "无权操作该事件",
          f"status={r6.status_code} body={r6.text}")
    r7 = client.post(f"/api/events/{eid6}/cancel", headers=auth_header(admin_token))
    check("7.1 管理员代撤销 403",
          r7.status_code == 403 and r7.json().get("detail") == "无权操作该事件",
          f"status={r7.status_code} body={r7.text}")

    # ==================================================================
    # 8. 事件不存在 404；9. 无 token 401
    # ==================================================================
    r8 = client.post("/api/events/no-such-event-id/cancel", headers=auth_header(token_a))
    check("8.1 事件不存在 404",
          r8.status_code == 404 and r8.json().get("detail") == "事件不存在",
          f"status={r8.status_code} body={r8.text}")
    r9 = client.post(f"/api/events/{eid1}/cancel")
    check("9.1 无 token 401", r9.status_code == 401, f"status={r9.status_code}")

    # ==================================================================
    # 10. 撤销后记录保留（居民与后台均可见，字段不清除）
    # ==================================================================
    eid10 = "evt-10-keep"
    seed_task(eid10, "已受理", now_str(-30), user_a_id,
              description="保留字段测试事件",
              handler="物业",
              reply="历史回复",
              replies=[{"content": "历史回复", "created_at": now_str(-600),
                        "reviewer_id": "r1", "reviewer_name": "后台"}])
    r10 = client.post(f"/api/events/{eid10}/cancel", headers=auth_header(token_a))
    item10 = next((x for x in client.get("/api/events", headers=auth_header(token_a)).json()
                   if x["event_id"] == eid10), None)
    check("10.1 撤销后记录保留（状态/描述/处理部门/回复）",
          r10.status_code == 200
          and item10 is not None
          and item10.get("status") == "已撤销"
          and item10.get("description") == "保留字段测试事件"
          and item10.get("handler") == "物业"
          and bool(item10.get("replies"))
          and item10["replies"][0].get("content") == "历史回复",
          str(item10))
    item_admin = next((x for x in client.get("/api/events", headers=auth_header(admin_token)).json()
                       if x["event_id"] == eid10), None)
    check("10.2 后台列表可见已撤销事件", item_admin is not None and item_admin.get("status") == "已撤销",
          str(item_admin))
    d10 = client.get(f"/api/events/{eid10}", headers=auth_header(token_a)).json()
    check("10.3 详情接口可见已撤销与保留字段",
          d10.get("status") == "已撤销" and d10.get("handler") == "物业" and d10.get("reply") == "历史回复",
          str(d10))

    # ==================================================================
    # 11-16. 已撤销不被后台任务覆盖（_process_event 守卫）+ 正常改写不受影响
    # ==================================================================
    _STATE = {
        "description": "测试事件描述",
        "address": "",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
        "status": "处理中",
        "confidence": "high",
        "confirmation_required": False,
        "emergency_type": "",
    }
    _RESULT = {
        "address": "小区3号楼",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "物业",
    }

    def simulate_success(event_id, user_id):
        async def _go():
            with patch.object(main_module, "dispatch_record_workflow") as mwf:
                mwf.invoke.return_value = dict(_RESULT)
                await main_module._process_event(event_id, dict(_STATE), user_id)
        asyncio.run(_go())

    async def _wait_for_timeout(coro, timeout):
        """模拟 asyncio.wait_for 超时路径（T20260820-001-TD 警告清理）。

        真实 asyncio.wait_for 超时会取消并消费内部协程（asyncio.to_thread(...)）；
        若直接 side_effect=asyncio.TimeoutError()，该 to_thread 协程永不 await，
        触发「coroutine 'to_thread' was never awaited」RuntimeWarning。此处正确
        创建任务、取消并 await 消费后再抛 TimeoutError，语义与真实超时一致。
        注意：patch.object 对 async 函数自动生成 AsyncMock，side_effect 必须是
        async 函数，其返回值才会被 AsyncMock await（普通函数返回协程不会被消费）。
        """
        task = asyncio.ensure_future(coro)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        raise asyncio.TimeoutError()

    def simulate_timeout(event_id, user_id):
        async def _go():
            with patch.object(main_module.asyncio, "wait_for", side_effect=_wait_for_timeout):
                await main_module._process_event(event_id, dict(_STATE), user_id)
        asyncio.run(_go())

    def simulate_exception(event_id, user_id):
        async def _go():
            with patch.object(main_module, "dispatch_record_workflow") as mwf:
                mwf.invoke.side_effect = RuntimeError("boom")
                await main_module._process_event(event_id, dict(_STATE), user_id)
        asyncio.run(_go())

    # 11. 已撤销 + 成功分支 -> 不被覆盖
    eid11 = "evt-11-success-guard"
    seed_task(eid11, "已撤销", now_str(-30), user_a_id)
    simulate_success(eid11, user_a_id)
    check("11.1 成功分支不覆盖已撤销", _tasks[eid11]["status"] == "已撤销",
          f"status={_tasks[eid11]['status']}")

    # 12. 已撤销 + 超时分支 -> 不被覆盖
    eid12 = "evt-12-timeout-guard"
    seed_task(eid12, "已撤销", now_str(-30), user_a_id)
    simulate_timeout(eid12, user_a_id)
    check("12.1 超时分支不覆盖已撤销", _tasks[eid12]["status"] == "已撤销",
          f"status={_tasks[eid12]['status']}")

    # 13. 已撤销 + 异常分支 -> 不被覆盖
    eid13 = "evt-13-exc-guard"
    seed_task(eid13, "已撤销", now_str(-30), user_a_id)
    simulate_exception(eid13, user_a_id)
    check("13.1 异常分支不覆盖已撤销", _tasks[eid13]["status"] == "已撤销",
          f"status={_tasks[eid13]['status']}")

    # 14. 处理中 + 超时分支 -> 仍改写为处理超时（守卫不破坏正常逻辑）
    eid14 = "evt-14-timeout-normal"
    seed_task(eid14, "处理中", now_str(-30), user_a_id)
    simulate_timeout(eid14, user_a_id)
    check("14.1 处理中仍可被改写为处理超时", _tasks[eid14]["status"] == "处理超时",
          f"status={_tasks[eid14]['status']}")

    # 15. 待审核 + 异常分支 -> 仍改写为处理失败（守卫允许）
    eid15 = "evt-15-exc-pending"
    seed_task(eid15, "待审核", now_str(-30), user_a_id)
    simulate_exception(eid15, user_a_id)
    check("15.1 待审核仍可被改写为处理失败", _tasks[eid15]["status"] == "处理失败",
          f"status={_tasks[eid15]['status']}")

    # 16. 处理中 + 成功分支 -> 仍改写为已完成（原有守卫保持）
    eid16 = "evt-16-success-normal"
    seed_task(eid16, "处理中", now_str(-30), user_a_id)
    simulate_success(eid16, user_a_id)
    check("16.1 处理中成功分支仍标记已完成", _tasks[eid16]["status"] == "已完成",
          f"status={_tasks[eid16]['status']}")

    # ==================================================================
    # 汇总
    # ==================================================================
    print("")
    print("=" * 70)
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n结果：{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    return 0 if failed == 0 else 1

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

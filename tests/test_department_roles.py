# -*- coding: utf-8 -*-
"""部门角色与新版工单流程的聚焦回归。"""

import asyncio
import importlib
import os
import sys
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("AUTH_STORE", "file")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "1" * 64)
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://test")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "admin123456")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("COMMUNITY_REQUIRE_LOCATION", "false")


def _imports():
    import auth
    import dispatch_agent as da
    import media_store
    import main
    importlib.reload(auth)
    importlib.reload(main)
    return auth, da, media_store, main


def test_handler_department_mapping():
    _, da, _, _ = _imports()
    assert da.handler_to_department("物业部") == ("property", "物业部")
    assert da.handler_to_department("[紧急]安保部") == ("security", "安保部")
    assert da.handler_to_department("120医疗急救中心（外部资源）") == ("", "")
    assert da.handler_to_department("人工部") == ("", "")


def test_dept_account_crud():
    auth, da, _, _ = _imports()
    ok, msg, u = auth.create_dept_user("wuye01", "test123456", "物业小王", "13900000001", "property")
    assert ok, msg
    assert u["department_name"] == "物业部"
    assert any(x["username"] == "wuye01" for x in auth.list_dept_users())
    ok2, _, u2 = auth.update_dept_user(u["id"], department="security", status="disabled")
    assert ok2 and u2["department_name"] == "安保部" and u2["status"] == "disabled"
    # 非法部门
    assert auth.create_dept_user("wuye02", "test123456", "X", "13900000002", "nope")[0] is False


def test_process_event_dispatches_to_department():
    auth, da, _, main = _imports()
    from main import _tasks, _save_tasks, _build_task, _build_task
    eid = "evt-dept-dispatch"
    user = {"id": "u1", "username": "r", "real_name": "居民", "phone": "13900000003",
            "role": "resident", "id_card": "", "building": "1栋", "unit": "1单元", "room": "101"}
    _tasks[eid] = _build_task(event_id=eid, description="楼下漏水", created_at="2026-09-07 12:00:00",
                              status="待处理", address="", event_type="物业维修", urgency="中",
                              scene_tag="常规", user=user)
    _save_tasks(_tasks)

    state = {"description": "楼下漏水", "address": "", "event_type": "物业维修", "urgency": "中",
             "scene_tag": "常规", "handler": "", "status": "待处理", "created_at": "", "user_id": "u1",
             "confidence": "high", "confirmation_required": False, "emergency_type": "", "confirmed": False}

    async def _go():
        with patch.object(main, "dispatch_record_workflow") as mwf:
            mwf.invoke.return_value = {"handler": "物业部", "address": "", "event_type": "物业维修",
                                       "urgency": "中", "scene_tag": "常规"}
            await main._process_event(eid, state, "u1")
    asyncio.run(_go())

    assert _tasks[eid]["status"] == "待处理"
    assert _tasks[eid]["assigned_dept"] == "property"
    assert _tasks[eid]["department_name"] == "物业部"
    assert any(n["type"] == "待处理" for n in _tasks[eid]["timeline"])


def test_complete_requires_photo():
    auth, da, media_store, main = _imports()
    from main import _tasks, _save_tasks, _build_task
    eid = "evt-complete-photo"
    user = {"id": "u1", "username": "r", "real_name": "居民", "phone": "13900000003",
            "role": "resident", "id_card": "", "building": "1栋", "unit": "1单元", "room": "101"}
    _tasks[eid] = _build_task(event_id=eid, description="x", created_at="2026-09-07 12:00:00",
                              status="已受理", address="", event_type="物业维修", urgency="中",
                              scene_tag="常规", user=user)
    _tasks[eid]["assigned_dept"] = "property"
    _tasks[eid]["reviewer_dept"] = "property"
    _save_tasks(_tasks)

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    ok, _, du = auth.create_dept_user("wuye_z", "test123456", "物业", "13900000004", "property")
    ok, _, login = auth.login_user("wuye_z", "test123456")
    tok = login["token"]
    h = {"Authorization": f"Bearer {tok}"}
    r = client.post(f"/api/events/{eid}/complete", json={"reply": "done", "photos": []}, headers=h)
    assert r.status_code == 400
    assert "照片" in r.json().get("error", "")

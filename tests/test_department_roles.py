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


def test_media_store_kinds():
    """录音存 recordings/，照片存 media/，均能读取（本地后端）。"""
    import media_store as ms
    assert ms.is_audio("x.m4a") is True
    assert ms.is_audio("x.png") is False
    aid = ms.save_upload(b"audiodata", "rec.m4a", kind="audio")
    pid = ms.save_upload(b"photodata", "p.png", kind="photo")
    assert ms.read_upload(aid) == b"audiodata", "录音应能读回"
    assert ms.read_upload(pid) == b"photodata", "照片应能读回"


def test_audio_event_dispatches():
    """录音事件：ASR 转写 + DeepSeek 分类成功 -> 派单到部门。"""
    import importlib
    import media_store as ms
    import main as mn
    importlib.reload(mn)

    def _fake_asr(data, ext):
        return "我家水龙头漏水了"

    def _fake_receive(state):
        return {"description": state.get("description", ""), "address": "3号楼",
                "event_type": "物业维修", "urgency": "中", "scene_tag": "常规",
                "handler": "", "confidence": "high", "confirmation_required": False, "emergency_type": ""}

    user = {"id": "u_audio", "username": "r", "real_name": "居民", "phone": "13900000099",
            "role": "resident", "id_card": "", "building": "1栋", "unit": "1单元", "room": "101"}
    # 先存一条录音
    aid = ms.save_upload(b"audiodata", "rec.m4a", kind="audio")
    eid = "evt-audio-dispatch"
    from main import _tasks, _save_tasks, _build_task
    _tasks[eid] = _build_task(event_id=eid, description="", created_at="2026-09-08 12:00:00",
                              status="待审核", address="", event_type="待审核", urgency="中",
                              scene_tag="常规", user=user)
    _tasks[eid]["media"] = [{"media_id": aid, "kind": "audio", "uploaded_by": "u_audio", "created_at": "", "note": ""}]
    _save_tasks(_tasks)

    import asyncio
    async def _go():
        with patch.object(mn.asr, "transcribe", side_effect=_fake_asr), \
             patch.object(mn, "receive_node", side_effect=_fake_receive):
            await mn._process_audio_event(eid, {"description": ""}, "u_audio", None, None, [aid])
    asyncio.run(_go())

    assert _tasks[eid]["audio_transcript"] == "我家水龙头漏水了"
    assert _tasks[eid]["status"] == "待处理"
    assert _tasks[eid]["assigned_dept"] == "property"
    assert _tasks[eid]["department_name"] == "物业部"


def test_audio_event_fallback_pending():
    """转写失败 -> 保持待审核。"""
    import asyncio
    import importlib
    import media_store as ms
    import main as mn
    importlib.reload(mn)
    aid = ms.save_upload(b"audiodata", "rec.m4a", kind="audio")
    user = {"id": "u_audio2", "username": "r2", "real_name": "居民2", "phone": "13900000098",
            "role": "resident", "id_card": "", "building": "1栋", "unit": "1单元", "room": "101"}
    eid = "evt-audio-fallback"
    from main import _tasks, _save_tasks, _build_task
    _tasks[eid] = _build_task(event_id=eid, description="", created_at="2026-09-08 12:00:00",
                              status="待审核", address="", event_type="待审核", urgency="中",
                              scene_tag="常规", user=user)
    _tasks[eid]["media"] = [{"media_id": aid, "kind": "audio", "uploaded_by": "u_audio2", "created_at": "", "note": ""}]
    _save_tasks(_tasks)
    async def _go():
        with patch.object(mn.asr, "transcribe", side_effect=RuntimeError("asr fail")):
            await mn._process_audio_event(eid, {"description": ""}, "u_audio2", None, None, [aid])
    asyncio.run(_go())
    assert _tasks[eid]["status"] == "待审核"
    assert _tasks[eid]["audio_transcript"] == ""


def test_reassign_event_syncs_departments():
    """超管改派：事件从物业部挪到环卫部，物业部列表不再含、环卫部含。"""
    import importlib
    import asyncio
    import main as mn
    importlib.reload(mn)
    user = {"id": "u_r", "username": "r", "real_name": "居民", "phone": "13900000088",
            "role": "resident", "id_card": "", "building": "1栋", "unit": "1单元", "room": "101"}
    eid = "evt-reassign"
    from main import _tasks, _save_tasks, _build_task
    _tasks[eid] = _build_task(event_id=eid, description="楼道漏水", created_at="2026-09-08 12:00:00",
                              status="待处理", address="3号楼", event_type="物业维修", urgency="中",
                              scene_tag="常规", user=user)
    _tasks[eid]["assigned_dept"] = "property"
    _tasks[eid]["department_name"] = "物业部"
    _tasks[eid]["handler"] = "物业部"
    _save_tasks(_tasks)

    admin = {"id": "adm1", "role": "admin", "real_name": "超管", "username": "admin"}
    async def _go():
        res = await mn.update_event_dept(eid, mn.DeptUpdateRequest(department="sanitation"), admin)
        prop_list = await mn.list_events({"role": "dept", "department": "property", "id": "p1"})
        san_list = await mn.list_events({"role": "dept", "department": "sanitation", "id": "s1"})
        return res, prop_list, san_list
    res, prop_list, san_list = asyncio.run(_go())
    assert res.get("data", {}).get("department_name") == "环卫部"
    assert _tasks[eid]["assigned_dept"] == "sanitation"
    assert _tasks[eid]["status"] == "待处理"
    assert _tasks[eid]["reviewer_id"] == ""
    assert not any(x["event_id"] == eid for x in prop_list), "旧部门(物业)不应再看到"
    assert any(x["event_id"] == eid for x in san_list), "新部门(环卫)应看到该事件"


def test_reassign_pending_review_guard():
    """待审核事件必须先改类型，不能被直接改派部门。"""
    import importlib
    import asyncio
    import main as mn
    importlib.reload(mn)
    user = {"id": "u_g", "username": "r2", "real_name": "居民2", "phone": "13900000089",
            "role": "resident", "id_card": "", "building": "1栋", "unit": "1单元", "room": "101"}
    eid = "evt-reassign-guard"
    from main import _tasks, _save_tasks, _build_task
    _tasks[eid] = _build_task(event_id=eid, description="", created_at="2026-09-08 12:00:00",
                              status="待审核", address="", event_type="待审核", urgency="中",
                              scene_tag="常规", user=user)
    _save_tasks(_tasks)
    admin = {"id": "adm2", "role": "admin", "real_name": "超管", "username": "admin"}
    async def _go():
        try:
            await mn.update_event_dept(eid, mn.DeptUpdateRequest(department="sanitation"), admin)
            return None
        except Exception as e:
            return e
    e = asyncio.run(_go())
    assert e is not None, "待审核事件直接改派应被拒绝"
    assert "待审核" in str(e), f"错误信息应为待审核相关: {e}"


def test_single_session_per_account():
    """同一账号二次登录：旧 token 失效，仅最新 token 有效。"""
    import importlib
    import auth
    importlib.reload(auth)
    ok_r, _, _ = auth.register_user(
        "sess1", "test123456", "居民S", "13900000091", "110101199001011234",
        "resident", "1栋", "1单元", "101", 30.274150, 120.155150,
    )
    assert ok_r, "预置用户失败"
    ok_a, _, ua = auth.login_user("sess1", "test123456")
    ok_b, _, ub = auth.login_user("sess1", "test123456")
    assert ok_a and ok_b
    assert ua["token"] != ub["token"]
    # 旧 token 应失效（后登录踢掉先登录）
    assert auth.get_current_user(ua["token"]) is None, "旧 token 应已失效"
    assert auth.get_current_user(ub["token"]) is not None, "新 token 应有效"

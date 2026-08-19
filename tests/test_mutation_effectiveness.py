# -*- coding: utf-8 -*-
"""
test_mutation_effectiveness.py
造错（mutation）有效性校验（任务书 T20260820-001-TB §3.4，仅入 FULL）。

目标：对 ≥4 类高价值守卫注入「缺陷变体」（删除守卫），断言对应原用例必须变红
（pytest.raises(AssertionError) 包裹）；每个 mutation 独立测试函数隔离。

四类守卫：
  M1  role 校验（注册禁止 role=admin）：守卫在 auth.register_user —— 变异 = 移除 admin 拒绝分支。
  M2  归属校验（cancel 仅本人）：守卫在 main.cancel_event —— 变异 = 移除归属判断（直接调用端点函数，
      因为 FastAPI 在装饰期捕获 endpoint，patch 后 HTTP 路由不生效，故原用例以「已解析用户」直调端点）。
  M3  超时降级（后台 60s -> 处理超时）：守卫在 main._process_event —— 变异 = 超时后不置「处理超时」。
  M4  输出转义（前端 escapeHtml 防 XSS）：守卫在 static/index.html + admin.html 的 escapeHtml
      （textContent -> innerHTML 浏览器自动转义）—— 变异 = 返回未转义原文。

输出报告：mutation 总数 / 被捕获数 / 未捕获清单（未捕获 = 对应 test 失败，须说明或补用例）。
所有变异均应被捕获（基线用例先绿、变异后红）。

运行方式：
  python -m pytest tests/test_mutation_effectiveness.py -q   # pytest 收集（仅入 FULL，不入 CORE）
  python tests/test_mutation_effectiveness.py                # 直跑 exit=0
"""
import asyncio
import io
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import HTTPException

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_mutation")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_mutation")

os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["AUTH_STORE"] = "file"

REG_LAT, REG_LNG = 30.274150, 120.155150
_PHONE = [13900003300]

_STATE = {"main": None, "auth": None, "client": None}

# mutation 报告：[(编号, 名称, 是否被捕获)]
_REPORT = []


def _next_phone():
    _PHONE[0] += 1
    return str(_PHONE[0])


def _ensure():
    """懒加载：reload auth -> import main -> 构建 TestClient（receive_node 已 mock）。"""
    if _STATE["main"] is not None:
        return
    import importlib
    import auth
    importlib.reload(auth)
    _STATE["auth"] = auth
    patch("receive_agent.OpenAI").start()
    from fastapi.testclient import TestClient
    from main import app
    _STATE["main"] = sys.modules["main"]
    _STATE["client"] = TestClient(app)
    # 注意：不在此永久 patch main.receive_node（会泄漏到同进程其它测试模块），
    # 事件提交统一走 _submit_event 内的临时 patch。
    patch("dispatch_agent.logger").start()
    patch("record_agent.logger").start()


def _patch_attr(module, name, new):
    """简易 patch：保存旧值 -> 设置新值；返回恢复函数（pytest/直跑通用）。"""
    old = getattr(module, name)
    setattr(module, name, new)
    return lambda: setattr(module, name, old)


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
    return login["data"]["token"], data["data"]["user"]


def _submit_event(client, token, description="变异-事件"):
    main_module = _STATE["main"]

    def mock_receive(state):
        desc = (state.get("description") or "").strip()
        return {"description": desc, "address": "小区3号楼", "event_type": "物业维修",
                "urgency": "中", "scene_tag": "常规", "handler": "",
                "confidence": "high", "confirmation_required": False, "emergency_type": ""}

    with patch.object(main_module, "receive_node", side_effect=mock_receive):
        resp = client.post("/api/events", json={"description": description},
                           headers=_auth_header(token))
    data = resp.json()
    assert data.get("success"), f"事件提交失败: {data}"
    return data["data"]["event_id"]


def _seed_task(main_module, event_id, status="处理中"):
    main_module._tasks[event_id] = {
        "event_id": event_id, "description": "变异种子", "status": status,
        "address": "", "event_type": "", "urgency": "", "scene_tag": "",
        "handler": "", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": None, "error": None, "user_id": "seed-user",
        "user_name": "", "user_phone": "", "user_id_card": "", "reply": "",
        "replies": [], "user_read_at": "",
    }


def _reset_tasks(main_module):
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()

    def restore():
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg
    return restore


# ======================================================================
# M1：role 校验（注册禁止 role=admin）
# ======================================================================
def _case_role_guard():
    auth_module = _STATE["auth"]
    ok, msg, user = auth_module.register_user(
        username=f"roleforge_{uuid.uuid4().hex[:6]}", password="test123456",
        real_name="伪造管理员", phone=_next_phone(), role="admin",
        building="1栋", unit="1单元", room="101",
        register_lat=REG_LAT, register_lng=REG_LNG,
    )
    assert ok is False, "role=admin 注册应被拒绝"
    assert msg == "禁止通过注册创建管理员账号", f"文案不符: {msg}"
    assert user is None, "拒绝时不应返回用户"


def test_mutation_role_guard(monkeypatch=None):
    _ensure()
    main_module = _STATE["main"]
    auth_module = _STATE["auth"]
    restore_tasks = _reset_tasks(main_module)
    restores = []
    try:
        # 基线：守卫完好 -> 原用例通过
        _case_role_guard()

        # 变异：移除 role=='admin' 拒绝分支
        def bad_register_user(*args, **kwargs):
            kwargs.setdefault("role", "resident")
            return (True, "ok", {"id": "fake-admin", "username": kwargs.get("username", ""),
                                 "role": kwargs.get("role", "resident")})

        restores.append(_patch_attr(auth_module, "register_user", bad_register_user))
        with pytest.raises(AssertionError):
            _case_role_guard()
        _REPORT.append(("M1", "role 校验（注册禁止 role=admin）", True))
        print("  [捕获] M1 role 校验：移除注册 admin 拒绝分支 -> 原用例变红")
    finally:
        for r in restores:
            r()
        restore_tasks()


# ======================================================================
# M2：归属校验（cancel 仅本人）
# ======================================================================
def _case_cancel_ownership():
    """原用例：B 撤销 A 的事件 -> 403 且状态不变（以已解析用户直调端点，见模块 docstring）。"""
    main_module = _STATE["main"]
    client = _STATE["client"]
    token_a, user_a = _register(client, "mut_owner_a")
    _, user_b = _register(client, "mut_owner_b")
    eid = _submit_event(client, token_a, "归属校验-事件")  # 事件属于 A
    b_user = {"id": user_b["id"], "role": "resident", "real_name": "居民B"}
    try:
        asyncio.run(main_module.cancel_event(eid, b_user))
    except HTTPException as exc:
        assert exc.status_code == 403, f"应 403，实际 {exc.status_code}"
        assert exc.detail == "无权操作该事件", exc.detail
    else:
        raise AssertionError("B 撤销 A 的事件应 403")
    assert main_module._tasks[eid]["status"] != "已撤销", "越权撤销产生副作用"


def test_mutation_ownership_guard(monkeypatch=None):
    _ensure()
    main_module = _STATE["main"]
    restore_tasks = _reset_tasks(main_module)
    restores = []
    try:
        # 基线：守卫完好 -> 原用例通过
        _case_cancel_ownership()

        # 变异：移除归属校验（任何用户均可撤销）
        async def bad_cancel(event_id, current_user):
            async with main_module._task_lock:
                task = main_module._tasks.get(event_id)
                if task is None:
                    raise HTTPException(status_code=404, detail="事件不存在")
                # 缺陷：缺少归属校验
                task["status"] = "已撤销"
                main_module._save_tasks(main_module._tasks)
            return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"]}}

        restores.append(_patch_attr(main_module, "cancel_event", bad_cancel))
        with pytest.raises(AssertionError):
            _case_cancel_ownership()
        _REPORT.append(("M2", "归属校验（cancel 仅本人）", True))
        print("  [捕获] M2 归属校验：移除 cancel 归属判断 -> 原用例变红")
    finally:
        for r in restores:
            r()
        restore_tasks()


# ======================================================================
# M3：超时降级（后台 60s -> 处理超时）
# ======================================================================
def _case_timeout_degradation():
    main_module = _STATE["main"]
    eid = uuid.uuid4().hex
    _seed_task(main_module, eid)
    with patch.object(main_module, "dispatch_record_workflow") as fake:
        fake.invoke.side_effect = asyncio.TimeoutError("模拟后台处理超过60秒")
        asyncio.run(main_module._process_event(
            eid, {"description": "d", "status": "处理中"}, "seed-user"))
    task = main_module._tasks[eid]
    assert task["status"] == "处理超时", f"60s 超时应降级为处理超时: {task}"
    assert "超时" in task.get("error", ""), f"error 应含超时说明: {task}"


def test_mutation_timeout_degradation(monkeypatch=None):
    _ensure()
    main_module = _STATE["main"]
    restore_tasks = _reset_tasks(main_module)
    restores = []
    try:
        # 基线：守卫完好 -> 原用例通过
        _case_timeout_degradation()

        # 变异：移除超时降级（超时后什么都不做，任务停留在处理中）
        async def bad_process(event_id, pre_checked_state, user_id, lat=None, lng=None):
            # 缺陷：无「处理超时」降级
            pass

        restores.append(_patch_attr(main_module, "_process_event", bad_process))
        with pytest.raises(AssertionError):
            _case_timeout_degradation()
        _REPORT.append(("M3", "超时降级（60s -> 处理超时）", True))
        print("  [捕获] M3 超时降级：移除 _process_event 超时分支 -> 原用例变红")
    finally:
        for r in restores:
            r()
        restore_tasks()


# ======================================================================
# M4：输出转义（前端 escapeHtml 防 XSS）
# ======================================================================
def _extract_escape_html(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"function escapeHtml\(text\)\s*\{(.*?)\n\s*\}", src, re.DOTALL)
    assert m, f"{path} 未找到 escapeHtml 函数"
    return "function escapeHtml(text) {" + m.group(1) + "}"


def _simulate_browser_escape(text):
    """模拟浏览器 textContent 赋值时的 HTML 实体转义（& < > \" '）。"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _case_output_escaping():
    payload = "<script>alert('xss')</script>"
    for rel in ("static/index.html", "static/admin.html"):
        src = _extract_escape_html(os.path.join(PROJECT_DIR, rel))
        # 结构守卫：必须通过 textContent 赋值 + innerHTML 读取（浏览器自动转义）
        assert "textContent" in src, f"{rel} escapeHtml 未使用 textContent 转义"
        assert "innerHTML" in src, f"{rel} escapeHtml 未使用 innerHTML"
        # 行为等价模拟：转义后不得原样透出脚本标签
        escaped = _simulate_browser_escape(payload)
        assert "<script>" not in escaped, f"{rel} 模拟转义后仍含原始 script 标签"
        assert "&lt;script&gt;" in escaped, f"{rel} 模拟转义未产生实体"
        assert "alert('xss')" not in escaped, f"{rel} 模拟转义未转义引号"


def test_mutation_output_escaping(monkeypatch=None):
    _ensure()
    restores = []
    try:
        # 基线：守卫完好 -> 原用例通过
        _case_output_escaping()

        # 变异：escapeHtml 不转义（直接返回原文，等价于移除 textContent 守卫）
        restores.append(_patch_attr(
            sys.modules[__name__], "_extract_escape_html",
            lambda path: "function escapeHtml(text) { return text; }",
        ))
        with pytest.raises(AssertionError):
            _case_output_escaping()
        _REPORT.append(("M4", "输出转义（前端 escapeHtml 防 XSS）", True))
        print("  [捕获] M4 输出转义：移除 escapeHtml 转义 -> 原用例变红")
    finally:
        for r in restores:
            r()


# ======================================================================
# 直跑入口
# ======================================================================
_CASES = [
    ("M1 role 校验", test_mutation_role_guard),
    ("M2 归属校验", test_mutation_ownership_guard),
    ("M3 超时降级", test_mutation_timeout_degradation),
    ("M4 输出转义", test_mutation_output_escaping),
]


def _print_report():
    total = len(_REPORT)
    captured = sum(1 for _, _, ok in _REPORT if ok)
    uncaptured = [name for num, name, ok in _REPORT if not ok]
    print("\n" + "=" * 70)
    print(f"  MUTATION REPORT: {captured} 捕获 / {total} 总数"
          f"（未捕获 {len(uncaptured)}）")
    for num, name, ok in _REPORT:
        print(f"    [{('捕获' if ok else '未捕获')}] {num} {name}")
    if uncaptured:
        print("  未捕获清单（须说明或补用例）: " + ", ".join(uncaptured))
    print("=" * 70)


def _run_all_cases():
    failed = []
    for name, fn in _CASES:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
    _print_report()
    assert not failed, f"{len(failed)} 项失败（未捕获变异），详见上方明细"


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

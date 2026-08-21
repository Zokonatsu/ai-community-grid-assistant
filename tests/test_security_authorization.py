# -*- coding: utf-8 -*-
"""
test_security_authorization.py
核心高风险用例：越权矩阵 + 八项接口攻防（OWASP API Security Top 10 映射）。

范围（任务书 T20260820-001-TB §3.1/§3.2）：
  1. 水平越权（BOLA，API1）：居民 A 用自己 token 访问/撤销/标记已读 B 的事件 -> 403/404 且无副作用。
  2. 垂直越权（BFLA，API5）：居民调 /api/admin/*、accept、reply -> 403；无 token -> 401。
  3. 认证与会话失效（API2）：无 token/伪造/过期/登出后复用/缺 Bearer -> 401。
  4. 对象属性越权与列表隔离（BOPLA/API1/API3）：居民只见自己；响应不含他人身份证/手机/定位
     （event_lat/lng/event_distance_m 仅 admin）；请求携带越权字段被忽略。
  5. 资源限制（API4）：超长 description/用户名 -> 422；大请求不 500（现状无限流，以注释记录基线）。
  6. 安全配置（API8）：错误 detail 不含堆栈/路径/密钥；默认凭据 admin/admin123456 风险基线；
     CORS 中间件基线（allow_origins=*，待收紧注释）。
  7. 注入与不安全消费（API10）：HTML/脚本内容后端原样存储不执行；LLM 坏 JSON 降级
     （完整断链矩阵见 test_chain_breaks.py）。
  8. 资产盘点（API9）：app.routes vs 文档化接口清单，输出未文档化/废弃清单（不阻塞）。

与既有用例去重说明：
  - test_data_isolation：已覆盖「列表隔离 + 详情 403」主路径；本文件补 mark_read/cancel 的
    「无副作用」断言、admin-only 字段（lat/lng/distance_m）裁剪、BOPLA 越权字段忽略。
  - test_event_cancel：已覆盖非本人 cancel 403；本文件以 BOLA 矩阵形式断言「无副作用」（状态/已读不变）。
  - test_security_fixes：已覆盖 auth.register_user(role=admin) 业务层拒绝；本文件补 API 层
    精确文案「禁止通过注册创建管理员账号」与非法 role 422。
  - test_auth：已覆盖无 token/无效 token 401 主路径；本文件补过期 token/登出复用/缺 Bearer 细分。
  - test_input_validation：已覆盖输入长度/字符集校验；本文件仅补 API 层 422 状态码断言与
    「大请求不 500」基线记录。

运行方式：
  python -m pytest tests/test_security_authorization.py -q   # pytest 收集
  python tests/test_security_authorization.py                # 直跑 exit=0
"""
import importlib
import io
import json
import os
import re
import shutil
import sys
import time
import uuid
import io
import os
import re
import shutil
import sys
import time
import uuid
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 直跑模式备份目录（不匹配 *.bak.* 清理规则的 conftest 备份由 conftest 处理）
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_security_authz")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_security_authz")

# 环境固定：必须在任何 config/auth/main 导入之前设置（conftest 在 pytest 下同样生效）
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["AUTH_STORE"] = "file"

# 固定注册坐标（在默认社区中心半径内，与 conftest 一致）
REG_LAT, REG_LNG = 30.274150, 120.155150

_STATE = {"client": None, "main": None, "auth": None}
_PHONE = [13900001100]


def _next_phone() -> str:
    _PHONE[0] += 1
    return str(_PHONE[0])


def _mock_receive_node(state):
    """语义校验 mock：机械层放行的描述返回固定有效语义；否则返回「无效输入」。"""
    import receive_agent as _ra
    desc = (state.get("description") or "").strip()
    if not _ra._is_valid_input(desc):
        return {"description": desc, "address": "", "event_type": "无效输入",
                "urgency": "", "scene_tag": "", "handler": "",
                "confidence": "none", "confirmation_required": False, "emergency_type": ""}
    return {"description": desc, "address": "小区3号楼", "event_type": "物业维修",
            "urgency": "中", "scene_tag": "常规", "handler": "",
            "confidence": "high", "confirmation_required": False, "emergency_type": ""}


def _get_client():
    """懒加载单例：reload auth -> import main -> 构建 TestClient（receive_node 已 mock）。"""
    if _STATE["client"] is not None:
        return _STATE["client"], _STATE["main"], _STATE["auth"]
    import auth
    importlib.reload(auth)
    _STATE["auth"] = auth
    # 先 patch OpenAI 再 import main（避免 receive_agent 模块级构造真实客户端）
    _patch_openai = patch("receive_agent.OpenAI")
    _patch_openai.start()
    from fastapi.testclient import TestClient
    from main import app
    _STATE["main"] = sys.modules["main"]
    # 注意：不在此永久 patch main.receive_node（会泄漏到同进程其它测试模块），
    # 事件提交统一走 _submit_event/_submit_event_pending 内的临时 patch。
    patch("dispatch_agent.logger").start()
    patch("record_agent.logger").start()
    _STATE["client"] = TestClient(app)
    return _STATE["client"], _STATE["main"], _STATE["auth"]


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _register(client, tag, phone=None):
    """注册一名居民并登录，返回 (username, token, user)。"""
    uname = f"{tag}_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={
        "username": uname, "password": "test123456", "real_name": f"居民{tag}",
        "phone": phone or _next_phone(), "role": "resident",
        "building": "1栋", "unit": "1单元", "room": "101",
        "register_lat": REG_LAT, "register_lng": REG_LNG,
    })
    data = resp.json()
    assert resp.status_code == 200, f"注册失败 HTTP {resp.status_code}: {data}"
    assert data.get("success"), f"注册未成功: {data}"
    token = _login(client, uname, "test123456")
    return uname, token, data["data"]["user"]


def _login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    data = resp.json()
    assert data.get("success"), f"登录失败: {data}"
    return data["data"]["token"]


def _submit_event(client, token, description="小区楼下下水道堵了"):
    main_module = _STATE["main"]
    with patch.object(main_module, "receive_node", side_effect=_mock_receive_node):
        resp = client.post("/api/events", json={"description": description},
                           headers=_auth_header(token))
    data = resp.json()
    assert data.get("success"), f"事件提交失败 HTTP {resp.status_code}: {data}"
    return data["data"]["event_id"]


def _submit_event_pending(client, token, description="待审核-测试事件"):
    """通过「语义校验=待审核」路径提交事件（状态同步置为待审核，可受理/可回复）。

    说明：TestClient 会在请求结束后取消后台任务，无法等待后台处理完成；
    需要「已受理/待审核/已完成」状态才能回复的用例，统一走本同步路径。
    """
    main_module = _STATE["main"]

    def pending_receive(state):
        return {"description": state.get("description", ""), "address": "",
                "event_type": "待审核", "urgency": "中", "scene_tag": "常规", "handler": "",
                "confidence": "low", "confirmation_required": False, "emergency_type": ""}

    with patch.object(main_module, "receive_node", side_effect=pending_receive):
        resp = client.post("/api/events", json={"description": description},
                           headers=_auth_header(token))
    data = resp.json()
    assert data.get("success") and data["data"]["status"] == "待审核",         f"待审核事件提交失败 HTTP {resp.status_code}: {data}"
    return data["data"]["event_id"]


def _reset_tasks(main_module):
    """备份并清空任务状态；返回恢复函数。"""
    saved_tasks, saved_bg = main_module._tasks, main_module._background_tasks
    main_module._tasks, main_module._background_tasks = {}, set()

    def restore():
        main_module._tasks, main_module._background_tasks = saved_tasks, saved_bg
    return restore


# ======================================================================
# 1. 水平越权（BOLA，API1）：A 访问/撤销/标记已读 B 的事件 -> 403 且无副作用
# ======================================================================
def test_bola_horizontal_privilege_escalation():
    client, main_module, _ = _get_client()
    restore = _reset_tasks(main_module)
    try:
        _, token_a, user_a = _register(client, "bola_a")
        _, token_b, _ = _register(client, "bola_b")
        event_id = _submit_event(client, token_a, "小区A事件-水平越权")

        # 前置：A 能看到自己的事件
        r = client.get(f"/api/events/{event_id}", headers=_auth_header(token_a))
        assert r.status_code == 200, f"A 查看自己事件应 200，实际 {r.status_code}"

        task_before = dict(main_module._tasks[event_id])
        status_before = task_before.get("status")
        read_before = task_before.get("user_read_at", "")

        # B 访问 A 的事件详情 -> 403 精确文案
        r = client.get(f"/api/events/{event_id}", headers=_auth_header(token_b))
        assert r.status_code == 403, f"B 访问 A 事件详情应 403，实际 {r.status_code}"
        assert r.json().get("detail") == "无权访问该事件", r.json()

        # B 撤销 A 的事件 -> 403 精确文案
        r = client.post(f"/api/events/{event_id}/cancel", headers=_auth_header(token_b))
        assert r.status_code == 403, f"B 撤销 A 事件应 403，实际 {r.status_code}"
        assert r.json().get("detail") == "无权操作该事件", r.json()

        # B 标记 A 的事件已读 -> 403 精确文案
        r = client.post(f"/api/events/{event_id}/mark_read", headers=_auth_header(token_b))
        assert r.status_code == 403, f"B 标记 A 事件已读应 403，实际 {r.status_code}"
        assert r.json().get("detail") == "无权访问此事件", r.json()

        # 无副作用：B 的越权操作后，A 事件状态/已读时间不变，事件仍存在
        task_after = main_module._tasks.get(event_id)
        assert task_after is not None, "事件不应被删除"
        assert task_after.get("status") == status_before, \
            f"越权撤销产生副作用：状态 {status_before} -> {task_after.get('status')}"
        assert task_after.get("user_read_at", "") == read_before, \
            "越权 mark_read 产生副作用：user_read_at 被修改"
        assert task_after.get("status") != "已撤销", "事件不应被他人撤销"

        # B 访问不存在的随机 id -> 404（语义清晰，不泄露存在性之外的细节）
        r = client.get("/api/events/no-such-event-xyz", headers=_auth_header(token_b))
        assert r.status_code == 404 and r.json().get("detail") == "事件不存在", r.text[:200]
        print("  [PASS] BOLA 水平越权：GET/cancel/mark_read 均 403，无副作用")
    finally:
        restore()


# ======================================================================
# 2. 垂直越权（BFLA，API5）：居民 -> /api/admin/*、accept、reply -> 403
# ======================================================================
def test_bfia_vertical_privilege_escalation():
    client, main_module, _ = _get_client()
    restore = _reset_tasks(main_module)
    try:
        _, token_res, _ = _register(client, "bfia_res")
        event_id = _submit_event(client, token_res, "小区B事件-垂直越权")

        # 居民访问管理员专属接口 -> 403 精确文案
        for method, path, body in [
            ("get", "/api/admin/users", None),
            ("get", "/api/admin/community", None),
            ("put", "/api/admin/community", {"name": "改", "center_lat": REG_LAT,
                                              "center_lng": REG_LNG, "radius_m": 500}),
        ]:
            kwargs = {"headers": _auth_header(token_res)}
            if body is not None:
                kwargs["json"] = body
            r = getattr(client, method)(path, **kwargs)
            assert r.status_code == 403, f"居民 {method.upper()} {path} 应 403，实际 {r.status_code}"
            assert r.json().get("detail") == "权限不足，仅管理员可访问", r.json()

        # 居民调用受理/回复 -> 403（依赖先于事件状态校验）
        r = client.post(f"/api/events/{event_id}/accept", headers=_auth_header(token_res))
        assert r.status_code == 403 and r.json().get("detail") == "权限不足，仅管理员可访问", r.text[:200]
        r = client.post(f"/api/events/{event_id}/reply", json={"reply": "越权回复"},
                        headers=_auth_header(token_res))
        assert r.status_code == 403 and r.json().get("detail") == "权限不足，仅管理员可访问", r.text[:200]

        # 无 token 访问管理员接口 -> 401
        r = client.get("/api/admin/users")
        assert r.status_code == 401, f"无 token 应 401，实际 {r.status_code}"
        r = client.post(f"/api/events/{event_id}/accept")
        assert r.status_code == 401, f"无 token accept 应 401，实际 {r.status_code}"

        # 管理员的越权尝试不产生副作用：事件仍为处理中/已完成，未被受理
        task = main_module._tasks[event_id]
        assert task.get("status") != "已受理", "居民不应能受理事件"
        assert task.get("replies", []) == [], "居民不应能添加回复"
        print("  [PASS] BFLA 垂直越权：admin 接口/accept/reply 均 403，无副作用")
    finally:
        restore()


# ======================================================================
# 3. 认证与会话失效（API2）：无/伪造/过期/登出复用/缺 Bearer -> 401
# ======================================================================
def test_auth_session_failures():
    client, main_module, auth_module = _get_client()
    restore = _reset_tasks(main_module)
    try:
        _, token, user = _register(client, "sess_user")
        # 3.1 无 token -> 401
        r = client.get("/api/auth/me")
        assert r.status_code == 401, f"无 token 应 401，实际 {r.status_code}"
        assert r.json().get("detail") == "未登录或登录已过期，请重新登录", r.json()
        # 3.2 伪造 token -> 401
        r = client.get("/api/auth/me", headers=_auth_header("forged-token-12345"))
        assert r.status_code == 401, f"伪造 token 应 401，实际 {r.status_code}"
        # 3.3 缺 Bearer 前缀（裸 token）-> 401
        r = client.get("/api/auth/me", headers={"Authorization": token})
        assert r.status_code == 401, f"缺 Bearer 应 401，实际 {r.status_code}"
        r = client.get("/api/auth/me", headers={"Authorization": f"Token {token}"})
        assert r.status_code == 401, f"非 Bearer scheme 应 401，实际 {r.status_code}"
        # 3.4 过期 token -> 401（把会话 created_at 改为 8 天前，触发 TTL 清理）
        with auth_module._auth_lock:
            auth_module._sessions[token]["created_at"] = "2018-01-01 00:00:00"
            auth_module._save_sessions(auth_module._sessions)
        r = client.get("/api/auth/me", headers=_auth_header(token))
        assert r.status_code == 401, f"过期 token 应 401，实际 {r.status_code}"
        # 3.5 登出后复用 -> 401
        r = client.post("/api/auth/logout", headers=_auth_header(token))
        assert r.status_code == 200, f"登出应 200，实际 {r.status_code}"
        r = client.get("/api/auth/me", headers=_auth_header(token))
        assert r.status_code == 401, f"登出后复用应 401，实际 {r.status_code}"
        print("  [PASS] API2 认证失效：无/伪造/过期/登出复用/缺 Bearer 均 401")
    finally:
        restore()


# ======================================================================
# 4. 列表隔离 / BOPLA：居民只见自己；他人敏感字段不泄露；越权字段被忽略
# ======================================================================
def test_bola_list_isolation_and_bopla():
    client, main_module, _ = _get_client()
    restore = _reset_tasks(main_module)
    try:
        _, token_a, user_a = _register(client, "iso_a")
        _, token_b, user_b = _register(client, "iso_b")
        admin_token = _login(client, "admin", "admin123456")

        # A 提交事件（描述含唯一标记，便于断言）
        desc_a = f"隔离测试-唯一标记-{uuid.uuid4().hex[:6]}"
        eid_a = _submit_event(client, token_a, desc_a)
        eid_b = _submit_event(client, token_b, "隔离测试-B的事件")

        # 4.1 A 的列表只含 A 自己的事件
        r = client.get("/api/events", headers=_auth_header(token_a))
        assert r.status_code == 200, r.text[:200]
        items = r.json()
        assert isinstance(items, list) and len(items) == 1, f"A 应只见自己的 1 条事件，实际 {len(items)}"
        assert items[0]["event_id"] == eid_a, items
        # 4.2 A 的响应不含 B 的身份证/手机（序列化整体检查）
        blob = json.dumps(items, ensure_ascii=False)
        assert user_b.get("id_card") not in blob or not user_b.get("id_card"), "泄露他人身份证"
        assert user_b.get("phone") not in blob, "泄露他人手机号"
        # 4.3 居民端不返回定位坐标/距中心米数（仅 admin）
        assert "event_lat" not in items[0] and "event_lng" not in items[0], \
            "居民端不应返回 event_lat/lng"
        assert "event_distance_m" not in items[0], "居民端不应返回 event_distance_m"
        # 4.4 B 的列表同理：只含 B 自己，不含 A 的敏感字段
        r = client.get("/api/events", headers=_auth_header(token_b))
        items_b = r.json()
        assert len(items_b) == 1 and items_b[0]["event_id"] == eid_b, items_b
        blob_b = json.dumps(items_b, ensure_ascii=False)
        if user_a.get("id_card"):
            assert user_a["id_card"] not in blob_b, "B 的响应泄露 A 的身份证"
        assert user_a.get("phone") not in blob_b, "B 的响应泄露 A 的手机号"
        r = client.get("/api/events", headers=_auth_header(admin_token))
        items_admin = r.json()
        admin_items = [e for e in items_admin if e["event_id"] == eid_a]
        assert len(admin_items) == 1, "admin 应能看到 A 的事件"
        assert "event_lat" in admin_items[0] and "event_distance_m" in admin_items[0], \
            "admin 端应返回定位字段"
        # 4.6 BOPLA：提交请求携带越权字段（role=admin / 他人 user_id / 伪造 reviewer）应被忽略
        with patch.object(main_module, "receive_node", side_effect=_mock_receive_node):
            resp = client.post("/api/events", json={
                "description": "BOPLA-越权字段注入",
                "role": "admin", "user_id": user_b["id"], "reviewer": "伪造管理员",
                "status": "已完成",
            }, headers=_auth_header(token_a))
        data = resp.json()
        assert data.get("success"), f"越权字段注入不应导致失败: {data}"
        eid_inject = data["data"]["event_id"]
        task = main_module._tasks[eid_inject]
        assert task["user_id"] == user_a["id"], "user_id 越权字段未被忽略"
        assert task["status"] == "处理中", "status 越权字段未被忽略"
        assert task.get("reviewer_id") != "伪造管理员", "reviewer 越权字段未被忽略"
        print("  [PASS] BOPLA/列表隔离：只见自己、他人敏感字段零泄露、越权字段被忽略")
    finally:
        restore()


# ======================================================================
# 5. 资源限制（API4）：超长输入 422；大请求不 500（现状无限流基线记录）
# ======================================================================
def test_resource_limits():
    client, main_module, _ = _get_client()
    restore = _reset_tasks(main_module)
    try:
        _, token, _ = _register(client, "limit_user")
        # 5.1 事件描述 >500 字 -> 422（Pydantic max_length=500）
        r = client.post("/api/events", json={"description": "很" * 501},
                        headers=_auth_header(token))
        assert r.status_code == 422, f"501 字描述应 422，实际 {r.status_code}"
        # 5.2 用户名 >20 / <3 -> 422
        r = client.post("/api/auth/register", json={
            "username": "u" * 21, "password": "test123456", "real_name": "太长",
            "phone": _next_phone(), "register_lat": REG_LAT, "register_lng": REG_LNG,
        })
        assert r.status_code == 422, f"21 字用户名应 422，实际 {r.status_code}"
        r = client.post("/api/auth/register", json={
            "username": "ab", "password": "test123456", "real_name": "太短",
            "phone": _next_phone(), "register_lat": REG_LAT, "register_lng": REG_LNG,
        })
        assert r.status_code == 422, f"2 字用户名应 422，实际 {r.status_code}"
        # 5.3 超长回复（ReplyRequest 无 max_length，现状放行）-> 不 500（基线记录：回复无长度上限）
        eid = _submit_event_pending(client, token, "资源限制-回复基线")
        r = client.post(f"/api/events/{eid}/reply", json={"reply": "回" * 10000},
                        headers=_auth_header(_login(client, "admin", "admin123456")))
        # 现状：回复无长度上限，应放行（200）而非 500；如未来收紧为 422 也算合规（不 500）
        assert r.status_code in (200, 422), f"超长回复应不 500，实际 {r.status_code}"
        assert r.status_code != 500, "超长回复导致 500"
        # 5.4 现状记录：注册/提交/轮询无服务端限流（不做破坏性压测，仅注释记录基线）
        print("  [PASS] API4 资源限制：超长输入 422；超长回复不 500；"
              "（基线记录：服务端暂无限流、回复无长度上限，需评估加慢速限流/长度上限）")
    finally:
        restore()


# ======================================================================
# 6. 安全配置（API8）：错误不泄露内部细节；默认凭据基线；CORS 基线
# ======================================================================
def test_security_config():
    client, main_module, _ = _get_client()
    restore = _reset_tasks(main_module)
    try:
        _, token, _ = _register(client, "cfg_user")
        _FORBIDDEN = ("Traceback", "main.py", "secure_store", "C:\\", "/app/",
                      "DATA_ENCRYPTION_KEY", "COS_SECRET", "1" * 64)
        # 6.1 404 语义清晰且不含内部细节
        r = client.get("/api/events/no-such-event-abc", headers=_auth_header(token))
        assert r.status_code == 404 and r.json().get("detail") == "事件不存在", r.text[:200]
        for marker in _FORBIDDEN:
            assert marker not in r.text, f"404 响应泄露内部标记: {marker}"
        # 6.2 422 校验错误不含堆栈/路径/密钥
        r = client.post("/api/events", json={"description": "很" * 501},
                        headers=_auth_header(token))
        assert r.status_code == 422
        for marker in _FORBIDDEN:
            assert marker not in r.text, f"422 响应泄露内部标记: {marker}"
        # 6.3 401 文案精确、不含内部细节
        r = client.get("/api/auth/me", headers=_auth_header("bad-token"))
        assert r.status_code == 401
        assert r.json().get("detail") == "未登录或登录已过期，请重新登录", r.json()
        for marker in _FORBIDDEN:
            assert marker not in r.text, f"401 响应泄露内部标记: {marker}"
        # 6.4 默认凭据风险基线：admin/admin123456 存在且可登录（显式断言，标注待改密）
        r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123456"})
        assert r.status_code == 200 and r.json().get("success"), \
            "默认管理员 admin/admin123456 应存在（风险基线）"
        # 6.5 CORS 基线：中间件存在；allow_origins=* 为现状（上线前需收紧）
        cors = [m for m in main_module.app.user_middleware
                if getattr(m, "cls", None).__name__ == "CORSMiddleware"]
        assert cors, "应配置 CORSMiddleware"
        opts = getattr(cors[0], "kwargs", {})
        print(f"  [PASS] API8 安全配置：错误响应零内部细节；默认凭据基线=admin/admin123456"
              f"（待改密）；CORS 现状 allow_origins={opts.get('allow_origins')}（待收紧）")
    finally:
        restore()


# ======================================================================
# 7. 注入与不安全消费（API10）：HTML/脚本原样存储不执行；坏 JSON 降级
# ======================================================================
def test_injection_and_unsafe_consumption():
    client, main_module, _ = _get_client()
    restore = _reset_tasks(main_module)
    try:
        _, token, _ = _register(client, "xss_user")
        payload = "<script>alert('xss')</script>"
        eid = _submit_event_pending(client, token, payload)
        # 7.1 服务端原样存储（不执行、不净化）：后端职责是存储，转义在渲染层
        assert main_module._tasks[eid]["description"] == payload, "描述应原样存储"
        # 7.2 回复原样存储（不执行）
        admin_token = _login(client, "admin", "admin123456")
        r = client.post(f"/api/events/{eid}/reply", json={"reply": payload},
                        headers=_auth_header(admin_token))
        assert r.status_code == 200, r.text[:200]
        assert main_module._tasks[eid]["reply"] == payload, "回复应原样存储"
        # 7.3 渲染层转义在 test_mutation_effectiveness 中做守卫变体校验（防 XSS）
        # 7.4 LLM 坏 JSON -> 降级（待审核），不把内部异常透传（完整断链见 chain_breaks）
        def bad_json_receive(state):
            raise json.JSONDecodeError("Expecting value", "<llm output>", 0)

        with patch.object(main_module, "receive_node", side_effect=bad_json_receive):
            r = client.post("/api/events", json={"description": "坏JSON降级"}, headers=_auth_header(token))
            data = r.json()
            assert data.get("success"), f"坏 JSON 应降级而非失败: {data}"
            assert data["data"]["status"] == "待审核", data
            # 响应 error 为空（不把内部异常透传给居民）
            assert data.get("error") is None, f"响应不应回显内部异常: {data}"
            # 降级原因只落任务 error 字段（类型名，无堆栈/路径/密钥）
            task = main_module._tasks[data["data"]["event_id"]]
            assert "JSONDecodeError" in task.get("error", ""), task
            for marker in ("Traceback", "main.py", "C:\\", "line "):
                assert marker not in task.get("error", ""), f"任务 error 泄露内部标记: {marker}"
            assert "Traceback" not in r.text and "C:\\" not in r.text, "泄露内部细节"
        print("  [PASS] API10 注入与不安全消费：HTML 原样存储不执行；坏 JSON 降级不泄漏")
    finally:
        restore()


# ======================================================================
# 8. 资产盘点（API9）：app.routes vs 文档化接口清单（不阻塞，输出清单）
# ======================================================================
# 文档化接口清单（来源：INTERFACE.md + 测试方案二·③ + DEPLOY/README）
DOCUMENTED_API = {
    "/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me",
    "/api/admin/users", "/api/admin/community", "/api/community",
    "/api/events", "/api/events/{event_id}",
    "/api/events/{event_id}/cancel", "/api/events/{event_id}/accept",
    "/api/events/{event_id}/reply", "/api/events/{event_id}/mark_read",
}


def test_asset_inventory():
    client, main_module, _ = _get_client()
    registered = sorted({
        getattr(r, "path", "")
        for r in main_module.app.routes
        if getattr(r, "path", "").startswith("/api")
    })
    undocumented = [p for p in registered if p not in DOCUMENTED_API]
    missing = sorted(DOCUMENTED_API - set(registered))
    # 已文档化接口必须全部注册（契约完整性）
    assert not missing, f"文档化但未注册的接口: {missing}"
    # 未文档化/废弃接口：输出清单（不阻塞）
    print(f"  [PASS] API9 资产盘点：已注册 /api 路由 {len(registered)} 个")
    print(f"         未文档化（需评估废弃或补文档）: {undocumented if undocumented else '无'}")
    print(f"         文档化但未注册: {missing if missing else '无'}")
    print("         完整路由清单: " + ", ".join(registered))


# ======================================================================
# 直跑入口（pytest 直接逐个收集 test_*）
# ======================================================================
_CASES = [
    ("1 BOLA 水平越权", test_bola_horizontal_privilege_escalation),
    ("2 BFLA 垂直越权", test_bfia_vertical_privilege_escalation),
    ("3 API2 认证失效", test_auth_session_failures),
    ("4 BOPLA 列表隔离", test_bola_list_isolation_and_bopla),
    ("5 API4 资源限制", test_resource_limits),
    ("6 API8 安全配置", test_security_config),
    ("7 API10 注入消费", test_injection_and_unsafe_consumption),
    ("8 API9 资产盘点", test_asset_inventory),
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


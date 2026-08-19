"""
test_proxy_beneficiary.py
事件提交方式（本人/代人办）测试脚本

测试范围：
  1. 本人提交：无需额外字段，事件 beneficiary_type=self，被帮助人即提交人
  2. 代人办全字段提交：事件存储被帮助人信息，管理员列表可见
  3. 代人办缺姓名/楼栋等字段 -> 拒绝（success=False 且错误信息提示）
  4. beneficiary_type 非法 -> 拒绝
  5. 居民自己的事件列表可见 beneficiary_* 字段
"""

import os
import sys
import json
import shutil
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_proxy_ben")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_proxy_ben")

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

def register(username, password="test123456", real_name="测试用户", phone="13900000000",
             building="1栋", unit="1单元", room="101"):
    body = {
        "username": username,
        "password": password,
        "real_name": real_name,
        "phone": phone,
        "role": "resident",
        "building": building,
        "unit": unit,
        "room": room,
        "register_lat": 30.274150,
        "register_lng": 120.155150,
    }
    return client.post("/api/auth/register", json=body).json()

def login(username, password="test123456"):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    return res.json()

def test_suite():
    # 数据隔离已由 conftest（pytest）或 __main__（直跑）保证；
    # auth/main 初始化必须在数据隔离生效之后（延迟导入 + reload）。
    global client
    import auth
    import importlib
    importlib.reload(auth)
    with patch("receive_agent.OpenAI"), \
         patch("receive_agent.receive_node", side_effect=_mock_receive_node), \
         patch("dispatch_agent.logger"), \
         patch("record_agent.logger"):
        from main import app
        from fastapi.testclient import TestClient
    client = TestClient(app)
    # 注册本人提交的居民
    register("res_self", real_name="自我测试", phone="13900000021", building="2栋", unit="3单元", room="401")
    tok_self = login("res_self").get("data", {}).get("token")
    check("0.1 本人居民登录成功", bool(tok_self), "")
    
    admin_token = login("admin", "admin123456").get("data", {}).get("token")
    check("0.2 管理员登录成功", bool(admin_token), "")
    
    # ---------------------------------------------------------------
    # 1. 本人提交：无需额外字段
    # ---------------------------------------------------------------
    res_self_ev = client.post("/api/events",
                              json={"description": "我家水管坏了"},
                              headers=auth_header(tok_self))
    self_body = res_self_ev.json()
    check("1.1 本人提交（无 beneficiary 字段）成功", res_self_ev.status_code == 200 and self_body.get("success"), res_self_ev.text[:200])
    self_id = self_body.get("data", {}).get("event_id")
    
    evs = client.get("/api/events", headers=auth_header(tok_self)).json()
    self_ev = next((e for e in evs if e["event_id"] == self_id), {})
    check("1.2 本人事件 beneficiary_type=self", self_ev.get("beneficiary_type") == "self", f"type={self_ev.get('beneficiary_type')}")
    check("1.3 本人事件被帮助人=提交人 real_name", self_ev.get("beneficiary_name") == "自我测试", f"name={self_ev.get('beneficiary_name')}")
    check("1.4 本人事件被帮助人=提交人住户信息", self_ev.get("beneficiary_building") == "2栋"
          and self_ev.get("beneficiary_unit") == "3单元" and self_ev.get("beneficiary_room") == "401",
          f"b={self_ev.get('beneficiary_building')} u={self_ev.get('beneficiary_unit')} r={self_ev.get('beneficiary_room')}")
    
    # ---------------------------------------------------------------
    # 2. 代人办全字段提交
    # ---------------------------------------------------------------
    res_proxy_ev = client.post("/api/events",
                               json={
                                   "description": "替邻居报修楼道灯",
                                   "beneficiary_type": "proxy",
                                   "beneficiary_name": "李四",
                                   "beneficiary_phone": "13911112222",
                                   "beneficiary_building": "5栋",
                                   "beneficiary_unit": "1单元",
                                   "beneficiary_room": "203",
                               },
                               headers=auth_header(tok_self))
    proxy_body = res_proxy_ev.json()
    check("2.1 代人办全字段提交成功", res_proxy_ev.status_code == 200 and proxy_body.get("success"), res_proxy_ev.text[:200])
    proxy_id = proxy_body.get("data", {}).get("event_id")
    
    evs_admin = client.get("/api/events", headers=auth_header(admin_token)).json()
    proxy_ev = next((e for e in evs_admin if e["event_id"] == proxy_id), {})
    check("2.2 代办事件 beneficiary_type=proxy", proxy_ev.get("beneficiary_type") == "proxy", f"type={proxy_ev.get('beneficiary_type')}")
    check("2.3 代办事件存储被帮助人姓名", proxy_ev.get("beneficiary_name") == "李四", f"name={proxy_ev.get('beneficiary_name')}")
    check("2.4 代办事件存储被帮助人手机/住户", proxy_ev.get("beneficiary_phone") == "13911112222"
          and proxy_ev.get("beneficiary_building") == "5栋" and proxy_ev.get("beneficiary_room") == "203",
          f"p={proxy_ev.get('beneficiary_phone')} b={proxy_ev.get('beneficiary_building')} r={proxy_ev.get('beneficiary_room')}")
    
    # ---------------------------------------------------------------
    # 3. 代人办缺字段 -> 拒绝
    # ---------------------------------------------------------------
    miss_name = client.post("/api/events",
                            json={"description": "缺姓名", "beneficiary_type": "proxy",
                                  "beneficiary_phone": "13911112222", "beneficiary_building": "5栋",
                                  "beneficiary_unit": "1单元", "beneficiary_room": "203"},
                            headers=auth_header(tok_self)).json()
    check("3.1 缺被帮助人姓名被拒绝", (not miss_name.get("success")) and "姓名" in miss_name.get("error", ""),
          f"error={miss_name.get('error')}")
    
    miss_room = client.post("/api/events",
                            json={"description": "缺房间", "beneficiary_type": "proxy",
                                  "beneficiary_name": "李四", "beneficiary_phone": "13911112222",
                                  "beneficiary_building": "5栋", "beneficiary_unit": "1单元"},
                            headers=auth_header(tok_self)).json()
    check("3.2 缺房间号被拒绝", (not miss_room.get("success")) and "房间号" in miss_room.get("error", ""),
          f"error={miss_room.get('error')}")
    
    # ---------------------------------------------------------------
    # 4. beneficiary_type 非法 -> 拒绝
    # ---------------------------------------------------------------
    bad_type = client.post("/api/events",
                           json={"description": "非法类型", "beneficiary_type": "other"},
                           headers=auth_header(tok_self)).json()
    check("4.1 非法提交方式被拒绝", (not bad_type.get("success")) and "提交方式不合法" in bad_type.get("error", ""),
          f"error={bad_type.get('error')}")
    
    # ---------------------------------------------------------------
    # 5. 居民自己的事件列表可见 beneficiary_* 字段
    # ---------------------------------------------------------------
    evs_self_list = client.get("/api/events", headers=auth_header(tok_self)).json()
    self_ev2 = next((e for e in evs_self_list if e["event_id"] == proxy_id), {})
    check("5.1 居民自己的代办事件列表含 beneficiary_*", self_ev2.get("beneficiary_type") == "proxy"
          and self_ev2.get("beneficiary_name") == "李四", f"type={self_ev2.get('beneficiary_type')} name={self_ev2.get('beneficiary_name')}")
    # 输出明细并断言（pytest 与直跑共用同一套校验逻辑）
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n结果：{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    assert failed == 0, f"{failed} 项校验失败，详见上方明细"

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

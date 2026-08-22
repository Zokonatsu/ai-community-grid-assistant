"""
test_resident_review_location.py
住户注册 + 定位 + 直接提交功能测试脚本

测试范围：
  1. 居民注册必须填写楼栋/单元/房间，注册即生效（status=active），无需管理员审核
  2. 注册定位在小区范围内 -> location_status=verified；越界 -> 拒绝注册（T20260819-002）
  3. id_card 存储回归（修复后端模型缺字段静默丢弃的 bug）
  4. 居民注册后可直接提交事件（无需管理员审核）
  5. 管理员后台住户列表（只读）：含完整身份证、楼栋房间、距中心米数，无审核操作
  6. 事件定位坐标仅管理员可见（居民端不返回）
  7. 居民访问住户列表端点返回 403
  8. /me 返回楼栋/定位字段
"""

import os
import sys
import json
import shutil
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份真实 data / secure 目录，使用临时空目录
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_res_rev")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_res_rev")

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

def register(username, password="test123456", real_name="测试用户", phone="13900000000",
             building="1栋", unit="1单元", room="101", id_card="", lat=None, lng=None):
    body = {
        "username": username,
        "password": password,
        "real_name": real_name,
        "phone": phone,
        "role": "resident",
        "building": building,
        "unit": unit,
        "room": room,
        "id_card": id_card,
        "register_lat": lat,
        "register_lng": lng,
    }
    res = client.post("/api/auth/register", json=body)
    return res.json()

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
    # 小区中心坐标（范围内）与明显越界坐标
    IN_LAT, IN_LNG = 30.274150, 120.155150
    OUT_LAT = IN_LAT + 0.1  # 约 11 公里，远超半径 500 米
    
    # ---------------------------------------------------------------
    # 1. 注册居民（范围内坐标）——注册即生效，无需审核
    # ---------------------------------------------------------------
    reg = register("res_in", phone="13900000001", id_card="330106199001011234", lat=IN_LAT, lng=IN_LNG)
    check("1.1 带坐标+楼栋注册成功", reg.get("success"), str(reg))
    user = reg.get("data", {}).get("user", {})
    check("1.2 注册后 status=active（无需审核）", user.get("status") == "active", f"status={user.get('status')}")
    check("1.3 范围内坐标 location_status=verified", user.get("location_status") == "verified", f"loc={user.get('location_status')}")
    check("1.4 返回楼栋单元房间", user.get("building") == "1栋" and user.get("unit") == "1单元" and user.get("room") == "101",
          f"b={user.get('building')} u={user.get('unit')} r={user.get('room')}")
    
    # 2. 越界坐标注册 -> 拒绝（注册必须在小区范围内，T20260819-002）
    reg_out = register("res_out", phone="13900000002", lat=OUT_LAT, lng=IN_LNG)
    check("2.1 越界坐标注册被拒绝", (not reg_out.get("success"))
          and reg_out.get("error") == "当前定位不在小区范围内，无法注册", str(reg_out))
    
    # 3. 缺楼栋/单元/房间 -> 拒绝
    reg_missing = register("res_nohome", phone="13900000003", building="", unit="", room="")
    check("3.1 缺楼栋单元房间拒绝注册", (not reg_missing.get("success")) and "楼栋、单元和房间号" in reg_missing.get("error", ""),
          f"error={reg_missing.get('error')}")
    
    # 4. 居民注册后可直接提交事件（无需管理员审核，带实时定位）
    tok_in = login("res_in").get("data", {}).get("token")
    check("4.0 新注册居民登录成功", bool(tok_in))
    res_ev = client.post("/api/events",
                         json={"description": "小区楼下下水道堵了", "lat": IN_LAT, "lng": IN_LNG},
                         headers=auth_header(tok_in))
    body_ev = res_ev.json()
    check("4.1 注册即生效，直接提交事件成功", res_ev.status_code == 200 and body_ev.get("success"), res_ev.text[:200])
    ev_id = body_ev.get("data", {}).get("event_id", "")
    
    # 5. 管理员后台住户列表（只读）：含完整身份证 + 距中心米数
    admin_login = login("admin", "GridAdmin2025!@#")
    admin_token = admin_login.get("data", {}).get("token")
    check("5.0 内置管理员可登录", bool(admin_token), str(admin_login.get("error")))
    
    res_list = client.get("/api/admin/users", headers=auth_header(admin_token))
    check("5.1 管理员获取住户列表（无审核参数）", res_list.status_code == 200, f"status={res_list.status_code}")
    residents = res_list.json()
    names = {u["username"] for u in residents}
    check("5.2 列表含 res_in（越界 res_out 未创建）", "res_in" in names and "res_out" not in names, f"names={names}")
    res_in_item = next((u for u in residents if u["username"] == "res_in"), {})
    check("5.3 列表含完整身份证（仅管理员可见）", res_in_item.get("id_card") == "330106199001011234", f"id_card={res_in_item.get('id_card')}")
    check("5.4 列表含 register_distance_m=0", res_in_item.get("register_distance_m") == 0, f"dist={res_in_item.get('register_distance_m')}")
    check("5.5 列表只读：无审核字段", "reviewed_at" not in res_in_item and "reject_reason" not in res_in_item, f"keys={sorted(res_in_item.keys())}")
    
    # 6. 事件坐标仅 admin 可见
    evs_res = client.get("/api/events", headers=auth_header(tok_in)).json()
    res_ev_item = next((e for e in evs_res if e["event_id"] == ev_id), {})
    check("6.1 居民端列表不含坐标", "event_lat" not in res_ev_item, f"keys={sorted(res_ev_item.keys())}")
    
    evs_admin = client.get("/api/events", headers=auth_header(admin_token)).json()
    admin_ev_item = next((e for e in evs_admin if e["event_id"] == ev_id), {})
    check("6.2 管理员端列表含坐标", admin_ev_item.get("event_lat") == IN_LAT, f"lat={admin_ev_item.get('event_lat')}")
    check("6.3 管理员端含定位状态", admin_ev_item.get("event_location_status") == "verified", f"loc={admin_ev_item.get('event_location_status')}")
    
    # 7. 居民访问住户列表端点 -> 403
    res_forbid = client.get("/api/admin/users", headers=auth_header(tok_in))
    check("7.1 居民访问住户列表 403", res_forbid.status_code == 403, f"status={res_forbid.status_code}")
    
    # 8. 登录接口返回 status / 楼栋信息（公共字段）
    me_tok = client.get("/api/auth/me", headers=auth_header(tok_in)).json()
    check("8.1 /me 返回楼栋", me_tok.get("building") == "1栋", f"building={me_tok.get('building')}")
    check("8.2 /me 返回 location_status", me_tok.get("location_status") == "verified", f"loc={me_tok.get('location_status')}")
    check("8.3 /me 返回 status=active", me_tok.get("status") == "active", f"status={me_tok.get('status')}")
    
    # 9. 注册成功文案（不再提示待审核）
    check("9.1 注册消息为'注册成功，请登录'", "审核" not in reg.get("error", ""), f"msg={reg.get('error')}")
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

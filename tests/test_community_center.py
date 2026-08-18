"""
test_community_center.py
社区中心设置（后台可改）+ 事件距中心距离测试脚本

测试范围：
  1. 默认社区配置返回环境变量回退值
  2. 居民访问/修改社区设置端点 -> 403
  3. 管理员保存新社区中心成功；非法经纬度/半径被拒绝
  4. 保存新中心后，事件 event_distance_m 按新中心计算
  5. 事件 event_distance_m 仅管理员可见
  6. 保存新中心后，住户注册定位判定与 register_distance_m 跟随新中心
"""

import os
import sys
import json
import shutil
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 备份真实 data / secure 目录，使用临时空目录（data/community_config.json 一并备份/恢复）
ORIGINAL_DATA_DIR = os.path.join(PROJECT_DIR, "data")
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_comm_center")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_comm_center")

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

setup_test_env()

os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_BASE_URL"] = "http://test"
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64

import auth
import importlib
importlib.reload(auth)
import geo
importlib.reload(geo)

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

with patch("receive_agent.OpenAI"), \
     patch("receive_agent.receive_node", side_effect=_mock_receive_node), \
     patch("dispatch_agent.logger"), \
     patch("record_agent.logger"):

    from main import app
    from fastapi.testclient import TestClient

client = TestClient(app)

RESULTS: list[tuple[str, bool, str]] = []

def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))

def auth_header(token):
    return {"Authorization": f"Bearer {token}"} if token else {}

def login(username, password="test123456"):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    return res.json()

def register(username, password="test123456", real_name="测试用户", phone="13900000000",
             building="1栋", unit="1单元", room="101", lat=None, lng=None):
    body = {
        "username": username,
        "password": password,
        "real_name": real_name,
        "phone": phone,
        "role": "resident",
        "building": building,
        "unit": unit,
        "room": room,
        "register_lat": lat,
        "register_lng": lng,
    }
    return client.post("/api/auth/register", json=body).json()

# 默认中心（env 回退）与将要保存的新中心
DEFAULT_LAT, DEFAULT_LNG = 30.274150, 120.155150
NEW_LAT, NEW_LNG = 30.250000, 120.100000
NEW_RADIUS = 800

# ---------------------------------------------------------------
# 0. 默认社区配置
# ---------------------------------------------------------------
admin_login = login("admin", "admin123456")
admin_token = admin_login.get("data", {}).get("token")
check("0.1 内置管理员可登录", bool(admin_token), str(admin_login.get("error")))

res_default = client.get("/api/admin/community", headers=auth_header(admin_token))
cfg = res_default.json()
check("0.2 默认配置返回 env 回退中心", res_default.status_code == 200
      and abs(cfg.get("center_lat", 0) - DEFAULT_LAT) < 1e-6
      and abs(cfg.get("center_lng", 0) - DEFAULT_LNG) < 1e-6,
      f"cfg={cfg}")

# ---------------------------------------------------------------
# 1. 居民访问/修改 -> 403
# ---------------------------------------------------------------
reg = register("res_comm", lat=DEFAULT_LAT, lng=DEFAULT_LNG)
tok = login("res_comm").get("data", {}).get("token")
check("1.1 注册居民成功", reg.get("success") and bool(tok), str(reg))
r_get = client.get("/api/admin/community", headers=auth_header(tok))
check("1.2 居民 GET 社区设置 403", r_get.status_code == 403, f"status={r_get.status_code}")
r_put = client.put("/api/admin/community", json={"center_lat": NEW_LAT, "center_lng": NEW_LNG, "radius_m": NEW_RADIUS}, headers=auth_header(tok))
check("1.3 居民 PUT 社区设置 403", r_put.status_code == 403, f"status={r_put.status_code}")

# ---------------------------------------------------------------
# 2. 管理员保存新中心；非法参数拒绝
# ---------------------------------------------------------------
res_save = client.put("/api/admin/community",
                      json={"name": "测试社区", "center_lat": NEW_LAT, "center_lng": NEW_LNG, "radius_m": NEW_RADIUS},
                      headers=auth_header(admin_token))
saved = res_save.json()
check("2.1 保存新中心成功", res_save.status_code == 200
      and abs(saved.get("center_lat", 0) - NEW_LAT) < 1e-6
      and saved.get("radius_m") == NEW_RADIUS, f"cfg={saved}")
check("2.2 返回 updated_at", bool(saved.get("updated_at")), str(saved))

res_get2 = client.get("/api/admin/community", headers=auth_header(admin_token))
check("2.3 重新读取为新中心", res_get2.json().get("center_lat") == NEW_LAT, f"cfg={res_get2.json()}")

bad_lat = client.put("/api/admin/community", json={"center_lat": 91.0, "center_lng": NEW_LNG, "radius_m": NEW_RADIUS}, headers=auth_header(admin_token))
check("2.4 非法纬度被拒绝", bad_lat.status_code >= 400, f"status={bad_lat.status_code}")
bad_lng = client.put("/api/admin/community", json={"center_lat": NEW_LAT, "center_lng": 181.0, "radius_m": NEW_RADIUS}, headers=auth_header(admin_token))
check("2.5 非法经度被拒绝", bad_lng.status_code >= 400, f"status={bad_lng.status_code}")
bad_radius = client.put("/api/admin/community", json={"center_lat": NEW_LAT, "center_lng": NEW_LNG, "radius_m": 0}, headers=auth_header(admin_token))
check("2.6 非法半径被拒绝", bad_radius.status_code >= 400, f"status={bad_radius.status_code}")

# ---------------------------------------------------------------
# 3. 事件按新中心计算距离
# ---------------------------------------------------------------
# 中心点本身 -> 距离 0；距中心约 1.11 公里处 -> 约 1111 米（> 半径 800）
ev_center = client.post("/api/events", json={"description": "小区路灯坏了", "lat": NEW_LAT, "lng": NEW_LNG}, headers=auth_header(tok))
ev_center_id = ev_center.json().get("data", {}).get("event_id")
check("3.1 中心点事件提交成功", ev_center.status_code == 200 and ev_center.json().get("success"), ev_center.text[:200])

OFF_LAT = NEW_LAT + 0.01  # 约 1111 米
ev_off = client.post("/api/events", json={"description": "楼道漏水", "lat": OFF_LAT, "lng": NEW_LNG}, headers=auth_header(tok))
ev_off_id = ev_off.json().get("data", {}).get("event_id")
check("3.2 偏移事件提交成功", ev_off.json().get("success"), ev_off.text[:200])

evs_admin = client.get("/api/events", headers=auth_header(admin_token)).json()
ev_c = next((e for e in evs_admin if e["event_id"] == ev_center_id), {})
ev_o = next((e for e in evs_admin if e["event_id"] == ev_off_id), {})
check("3.3 中心点事件距离为 0", ev_c.get("event_distance_m") == 0, f"dist={ev_c.get('event_distance_m')}")
check("3.4 中心点事件定位 verified", ev_c.get("event_location_status") == "verified", f"loc={ev_c.get('event_location_status')}")
check("3.5 偏移事件距离>800（按新中心/新半径判定）", ev_o.get("event_distance_m") is not None
      and ev_o.get("event_distance_m") > NEW_RADIUS, f"dist={ev_o.get('event_distance_m')}")
check("3.6 偏移事件定位 unverified", ev_o.get("event_location_status") == "unverified", f"loc={ev_o.get('event_location_status')}")

# 3.7 事件距离仅管理员可见（居民端列表不返回）
evs_res = client.get("/api/events", headers=auth_header(tok)).json()
res_ev = next((e for e in evs_res if e["event_id"] == ev_center_id), {})
check("3.7 居民端列表不含 event_distance_m", "event_distance_m" not in res_ev, f"keys={sorted(res_ev.keys())}")

# ---------------------------------------------------------------
# 4. 注册定位判定跟随新中心（回归 auth 链路）
# ---------------------------------------------------------------
reg_in = register("res_in_new", phone="13900000011", lat=NEW_LAT, lng=NEW_LNG)
check("4.1 新中心内注册 verified", reg_in.get("data", {}).get("user", {}).get("location_status") == "verified",
      f"loc={reg_in.get('data', {}).get('user', {}).get('location_status')}")
reg_out = register("res_out_new", phone="13900000012", lat=OFF_LAT, lng=NEW_LNG)
check("4.2 新中心外注册 unverified", reg_out.get("data", {}).get("user", {}).get("location_status") == "unverified",
      f"loc={reg_out.get('data', {}).get('user', {}).get('location_status')}")

users_admin = client.get("/api/admin/users", headers=auth_header(admin_token)).json()
in_item = next((u for u in users_admin if u["username"] == "res_in_new"), {})
out_item = next((u for u in users_admin if u["username"] == "res_out_new"), {})
check("4.3 新中心内住户 register_distance_m=0", in_item.get("register_distance_m") == 0, f"dist={in_item.get('register_distance_m')}")
check("4.4 新中心外住户 register_distance_m>800", out_item.get("register_distance_m") is not None
      and out_item.get("register_distance_m") > NEW_RADIUS, f"dist={out_item.get('register_distance_m')}")


def main():
    print("=" * 70)
    print("社区中心设置 + 事件距中心距离测试")
    print("=" * 70)
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n结果：{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    teardown_test_env()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

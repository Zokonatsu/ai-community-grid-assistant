"""
test_register_location.py
注册必须在小区范围内（T20260819-002）功能测试脚本

测试范围（验收标准 1/2/3/4）：
  1. 无坐标注册（register_lat/register_lng 任一为 null）-> success=false + 精确文案，不创建用户
  2. 坐标越界（距当前生效中心 > radius_m）-> success=false + 精确文案，不创建用户
  3. 坐标在范围内（<= radius_m）-> 注册成功，data.user.location_status == "verified"
  4. 半径持久化生效：后台保存新中心/半径后立即按新值判定（无需缓存/重启）
  5. 管理员角色注册仍被禁止（现有逻辑不变）
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
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_register_location")
ORIGINAL_SECURE_DIR = os.path.join(PROJECT_DIR, "secure")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_register_location")

NO_LOC_ERROR = "注册需先获取定位，请允许浏览器定位权限后重试"
OUT_OF_RANGE_ERROR = "当前定位不在小区范围内，无法注册"

# 默认中心（env 回退）与将要保存的新中心
DEFAULT_LAT, DEFAULT_LNG = 30.274150, 120.155150
NEW_LAT, NEW_LNG = 30.250000, 120.100000
NEW_RADIUS = 800
# 距新中心约 333 米（< 800，范围内）；约 1111 米（> 800，范围外）
IN_LAT = NEW_LAT + 0.003
OFF_LAT = NEW_LAT + 0.01


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


def test_suite():
    # 数据隔离已由 conftest（pytest）或 __main__（直跑）保证，此处不再自行备份
    RESULTS: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        RESULTS.append((name, bool(cond), detail))

    os.environ["LLM_API_KEY"] = "test-key"
    os.environ["LLM_BASE_URL"] = "http://test"
    os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
    os.environ["AUTH_STORE"] = "file"

    import auth
    import importlib
    importlib.reload(auth)

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

    def auth_header(token):
        return {"Authorization": f"Bearer {token}"} if token else {}

    def login(username, password="test123456"):
        res = client.post("/api/auth/login", json={"username": username, "password": password})
        return res.json()

    def register(username, password="test123456", real_name="测试用户", phone="13900000000",
                 building="1栋", unit="1单元", room="101", id_card="", role="resident",
                 lat=None, lng=None):
        body = {
            "username": username,
            "password": password,
            "real_name": real_name,
            "phone": phone,
            "role": role,
            "building": building,
            "unit": unit,
            "room": room,
            "id_card": id_card,
            "register_lat": lat,
            "register_lng": lng,
        }
        res = client.post("/api/auth/register", json=body)
        return res.json()

    def usernames():
        return {u.get("username") for u in auth._users.values()}

    # ---------------------------------------------------------------
    # 1. 无坐标拒绝
    # ---------------------------------------------------------------
    r1 = register("no_loc_user", phone="13900000001")
    check("1.1 无坐标注册 success=false", r1.get("success") is False, str(r1))
    check("1.2 无坐标错误文案精确匹配", r1.get("error") == NO_LOC_ERROR, f"error={r1.get('error')!r}")
    check("1.3 无坐标不创建用户", "no_loc_user" not in usernames(), str(sorted(usernames())))

    # 仅缺 register_lng（register_lat 有值）也应拒绝
    r1b = register("no_lng_user", phone="13900000002", lat=DEFAULT_LAT, lng=None)
    check("1.4 仅缺 register_lng 拒绝", r1b.get("success") is False and r1b.get("error") == NO_LOC_ERROR, str(r1b))
    check("1.5 仅缺 register_lng 不创建用户", "no_lng_user" not in usernames(), "")

    # ---------------------------------------------------------------
    # 2. 越界拒绝（默认半径 500m；距中心约 1111 米）
    # ---------------------------------------------------------------
    OUT_LAT = DEFAULT_LAT + 0.01
    r2 = register("out_of_range_user", phone="13900000003", lat=OUT_LAT, lng=DEFAULT_LNG)
    check("2.1 越界注册 success=false", r2.get("success") is False, str(r2))
    check("2.2 越界错误文案精确匹配", r2.get("error") == OUT_OF_RANGE_ERROR, f"error={r2.get('error')!r}")
    check("2.3 越界不创建用户", "out_of_range_user" not in usernames(), "")

    # ---------------------------------------------------------------
    # 3. 范围内成功（默认中心，距离 0 <= 500）
    # ---------------------------------------------------------------
    r3 = register("in_range_user", phone="13900000004", lat=DEFAULT_LAT, lng=DEFAULT_LNG)
    check("3.1 范围内注册成功", r3.get("success") is True, str(r3))
    user3 = r3.get("data", {}).get("user", {})
    check("3.2 范围内 location_status=verified", user3.get("location_status") == "verified",
          f"loc={user3.get('location_status')}")
    check("3.3 成功响应结构不变（error=注册成功，请登录）", r3.get("error") == "注册成功，请登录",
          f"error={r3.get('error')!r}")
    check("3.4 范围内用户已创建", "in_range_user" in usernames(), "")

    # ---------------------------------------------------------------
    # 4. 半径持久化生效（后台保存新中心/半径后立即按新值判定）
    # ---------------------------------------------------------------
    admin_login = login("admin", "admin123456")
    admin_token = admin_login.get("data", {}).get("token")
    check("4.0 内置管理员可登录", bool(admin_token), str(admin_login.get("error")))

    res_save = client.put("/api/admin/community",
                          json={"name": "测试社区", "center_lat": NEW_LAT, "center_lng": NEW_LNG, "radius_m": NEW_RADIUS},
                          headers=auth_header(admin_token))
    saved = res_save.json()
    check("4.1 保存新中心/半径成功", res_save.status_code == 200 and saved.get("radius_m") == NEW_RADIUS, str(saved))

    # 新中心范围内一点（约 333 米 < 800）
    r4 = register("new_in_user", phone="13900000005", lat=IN_LAT, lng=NEW_LNG)
    check("4.2 按新中心/半径，范围内注册成功且 verified",
          r4.get("success") is True and r4.get("data", {}).get("user", {}).get("location_status") == "verified",
          str(r4))

    # 旧默认中心点距新中心约 6 公里：改半径前（默认中心）可注册，改后应被拒绝 -> 证明立即生效
    r4b = register("old_center_user", phone="13900000006", lat=DEFAULT_LAT, lng=DEFAULT_LNG)
    check("4.3 新配置立即生效：旧中心点被拒绝",
          r4b.get("success") is False and r4b.get("error") == OUT_OF_RANGE_ERROR, str(r4b))
    check("4.4 被拒后不创建用户", "old_center_user" not in usernames(), "")

    # 新中心外一点（约 1111 米 > 800）仍拒绝
    r4c = register("new_out_user", phone="13900000007", lat=OFF_LAT, lng=NEW_LNG)
    check("4.5 新半径外仍拒绝", r4c.get("success") is False and r4c.get("error") == OUT_OF_RANGE_ERROR, str(r4c))

    # ---------------------------------------------------------------
    # 5. 管理员角色注册仍被禁止（现有逻辑不变）
    # ---------------------------------------------------------------
    r5 = register("forged_admin", phone="13900000008", role="admin", lat=DEFAULT_LAT, lng=DEFAULT_LNG)
    check("5.1 管理员角色注册仍被禁止", r5.get("success") is False and "禁止" in r5.get("error", ""), str(r5))

    # ---------------------------------------------------------------
    # 汇总
    # ---------------------------------------------------------------
    print("=" * 70)
    print("注册必须在小区范围内测试")
    print("=" * 70)
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n结果：{len(RESULTS) - failed}/{len(RESULTS)} 通过")
    # T20260820-001-TD：test_suite 不返回值（消除 PytestReturnNotNoneWarning），
    # 保留 assert 语义；直跑入口 main() 仍按结果 sys.exit(0/1)。
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

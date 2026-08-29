"""
测试注册系统安全修复效果

测试范围：
1. 注册页面不应出现"街道管理员"角色选项
2. 直接调用注册接口并伪造管理员角色，应被拒绝
3. 系统启动后应存在默认管理员账号
4. 居民账号注册应正常通过
"""
import json
import os
import sys
import io
import shutil


# 备份并清空 data/secure，确保测试默认管理员创建逻辑
DATA_DIR = "./data"
SECURE_DIR = "./secure"
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAK_DATA_DIR = os.path.join(PROJECT_DIR, "data.bak.test_security")
BAK_SECURE_DIR = os.path.join(PROJECT_DIR, "secure.bak.test_security")

# 固定以仓库根为工作目录并加入 sys.path，保证从 tests/ 目录执行时
# 也能正确解析 ./data、./secure、static/ 并 import auth
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

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


# 设置账号数据加密密钥（64 位 hex，固定测试值）
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64
os.environ["AUTH_STORE"] = "file"
os.environ["ADMIN_INITIAL_PASSWORD"] = "admin123456"

# 重新导入 auth 模块（会触发 _init_auth 创建默认管理员）

def test_suite():
    # auth 初始化必须在数据隔离生效之后（延迟导入 + reload）
    import importlib
    if "auth" in sys.modules:
        importlib.reload(sys.modules["auth"])
    import auth
    print("=" * 60)
    print("测试注册系统安全修复效果")
    print("=" * 60)
    
    results = []
    
    # ------------------------------------------------------------------
    # 测试 1：注册页面不应出现"街道管理员"角色选项
    # ------------------------------------------------------------------
    print("\n[测试1] 注册页面不应出现'街道管理员'角色选项")
    with open("static/login.html", "r", encoding="utf-8") as f:
        html = f.read()
    
    # 检查下拉框中是否只有"居民"选项
    import re
    select_match = re.search(r'<select[^>]*id="reg-role"[^>]*>(.*?)</select>', html, re.DOTALL)
    if select_match:
        select_content = select_match.group(1)
        options = re.findall(r'<option[^>]*value="([^"]*)"', select_content)
        print(f"  注册角色下拉框选项: {options}")
        if "admin" in options or "管理员" in select_content:
            print("  ❌ 失败：注册页面角色选项中包含管理员相关选项")
            results.append(("测试1：注册页面不应出现管理员角色选项", "失败", "页面中存在管理员角色选项"))
        else:
            print("  ✅ 通过：注册页面仅显示'居民'角色")
            results.append(("测试1：注册页面不应出现管理员角色选项", "通过", ""))
    else:
        print("  ❌ 失败：未找到注册角色下拉框")
        results.append(("测试1：注册页面不应出现管理员角色选项", "失败", "未找到 reg-role 下拉框"))
    
    # 检查 select 是否 disabled
    if 'id="reg-role" disabled' in html:
        print("  ✅ 通过：角色下拉框已禁用，用户无法修改")
    else:
        print("  ⚠️ 注意：角色下拉框未禁用")
    
    # 检查隐藏字段值
    hidden_match = re.search(r'id="reg-role-value"[^>]*value="([^"]*)"', html)
    if hidden_match:
        hidden_value = hidden_match.group(1)
        print(f"  隐藏字段 reg-role-value 值: '{hidden_value}'")
        if hidden_value == "resident":
            print("  ✅ 通过：隐藏字段值为 resident")
        else:
            print(f"  ❌ 失败：隐藏字段值为 '{hidden_value}'，应为 'resident'")
            results.append(("测试1-补充：隐藏字段值", "失败", f"值为 {hidden_value}"))
    else:
        print("  ❌ 失败：未找到 reg-role-value 隐藏字段")
        results.append(("测试1-补充：隐藏字段", "失败", "未找到 reg-role-value"))
    
    # ------------------------------------------------------------------
    # 测试 2：直接调用注册接口并伪造管理员角色，应被拒绝
    # ------------------------------------------------------------------
    print("\n[测试2] 直接调用注册接口并伪造管理员角色，应被拒绝")
    
    # 测试 2a：通过 auth.register_user 直接调用，传入 role="admin"
    print("  2a. 直接调用 auth.register_user(role='admin')...")
    success, message, user = auth.register_user(
        username="test_admin_forgery",
        password="test123456",
        real_name="伪造管理员",
        phone="13900000001",
        id_card="110101199001011234",
        role="admin"
    )
    print(f"    返回: success={success}, message='{message}'")
    if not success:
        print("  ✅ 通过：业务层正确拒绝了管理员角色注册")
        results.append(("测试2a：auth.register_user 拒绝 admin 角色", "通过", ""))
    else:
        print(f"  ❌ 失败：业务层未正确拒绝，success={success}, message={message}")
        results.append(("测试2a：auth.register_user 拒绝 admin 角色", "失败", f"success={success}, message={message}"))
    
    # 测试 2b：直接调用 auth.register_user，传入 role="" (空字符串)
    print("  2b. 直接调用 auth.register_user(role='')...")
    success, message, user = auth.register_user(
        username="test_empty_role",
        password="test123456",
        real_name="空角色测试",
        phone="13900000002",
        id_card="110101199001011235",
        role=""
    )
    print(f"    返回: success={success}, message='{message}'")
    if not success:
        print("  ✅ 通过：空角色被拒绝")
        results.append(("测试2b：auth.register_user 拒绝空角色", "通过", ""))
    else:
        print("  ❌ 失败：空角色未被拒绝")
        results.append(("测试2b：auth.register_user 拒绝空角色", "失败", "空角色未被拒绝"))
    
    # 测试 2c：直接调用 auth.register_user，传入 role="superadmin" (不在允许列表中)
    print("  2c. 直接调用 auth.register_user(role='superadmin')...")
    success, message, user = auth.register_user(
        username="test_superadmin_forgery",
        password="test123456",
        real_name="超级管理员伪造",
        phone="13900000003",
        id_card="110101199001011236",
        role="superadmin"
    )
    print(f"    返回: success={success}, message='{message}'")
    if not success:
        print("  ✅ 通过：无效角色被拒绝")
        results.append(("测试2c：auth.register_user 拒绝无效角色", "通过", ""))
    else:
        print("  ❌ 失败：无效角色未被拒绝")
        results.append(("测试2c：auth.register_user 拒绝无效角色", "失败", "无效角色未被拒绝"))
    
    # ------------------------------------------------------------------
    # 测试 3：系统启动后应存在默认管理员账号
    # ------------------------------------------------------------------
    print("\n[测试3] 系统启动后应存在默认管理员账号")
    
    admin_found = False
    for uid, u in auth._users.items():
        if u.get("role") == "admin":
            admin_found = True
            print(f"  ID: {u['id']}")
            print(f"  用户名: {u['username']}")
            print(f"  角色: {u['role']}")
            print(f"  创建时间: {u['created_at']}")
            # 验证密码哈希存在
            if u.get("password_hash") and "$" in u["password_hash"]:
                print("  ✅ 密码哈希格式正常")
            else:
                print("  ❌ 密码哈希格式异常")
                results.append(("测试3-补充：密码哈希格式", "失败", "哈希格式异常"))
            break
    
    if admin_found:
        print("  ✅ 通过：默认管理员账号已自动创建")
        results.append(("测试3：默认管理员账号存在", "通过", ""))
    else:
        print("  ❌ 失败：未找到默认管理员账号")
        results.append(("测试3：默认管理员账号存在", "失败", "未找到管理员账号"))
    
    # 测试 3b：验证默认管理员可以登录
    print("  3b. 验证默认管理员登录...")
    success, message, result = auth.login_user("admin", "admin123456")
    print(f"    返回: success={success}, message='{message}'")
    if success and result and result.get("token"):
        print("  ✅ 通过：默认管理员可以正常登录")
        results.append(("测试3b：默认管理员登录", "通过", ""))
        # 保存 token 用于后续测试
        admin_token = result["token"]
    else:
        print(f"  ❌ 失败：默认管理员无法登录，message={message}")
        results.append(("测试3b：默认管理员登录", "失败", message))
    
    # ------------------------------------------------------------------
    # 测试 4：居民账号注册应正常通过
    # ------------------------------------------------------------------
    print("\n[测试4] 居民账号注册应正常通过")
    
    # 确保之前测试的残留数据不会干扰
    import time
    test_username = f"tst_{int(time.time()) % 100000}"
    success, message, user = auth.register_user(
        username=test_username,
        password="test123456",
        real_name="测试居民",
        phone="13900000004",
        id_card="110101199001011237",
        role="resident",
        building="1栋", unit="1单元", room="101",
        register_lat=30.274150, register_lng=120.155150
    )
    print(f"  注册返回: success={success}, message='{message}'")
    if success and user:
        print(f"  用户信息: username={user['username']}, role={user['role']}, real_name={user['real_name']}")
        if user["role"] == "resident":
            print("  ✅ 通过：居民账号注册成功，角色正确")
            results.append(("测试4a：居民账号注册", "通过", ""))
        else:
            print(f"  ❌ 失败：注册成功但角色错误，role={user['role']}")
            results.append(("测试4a：居民账号注册", "失败", f"角色为 {user['role']}"))
    else:
        print(f"  ❌ 失败：居民账号注册失败，message={message}")
        results.append(("测试4a：居民账号注册", "失败", message))
    
    # 测试 4b：验证居民账号可以登录
    if success:
        print("  4b. 验证居民账号登录...")
        success2, message2, result2 = auth.login_user(test_username, "test123456")
        if success2 and result2 and result2.get("token"):
            print("  ✅ 通过：居民账号可以正常登录")
            results.append(("测试4b：居民账号登录", "通过", ""))
        else:
            print(f"  ❌ 失败：居民账号无法登录，message={message2}")
            results.append(("测试4b：居民账号登录", "失败", message2))
    
        # 测试 4c：验证居民账号尝试获取管理员接口结果
        print("  4c. 验证居民账号角色权限...")
        resident_user = auth.get_current_user(result2["token"])
        if resident_user and resident_user.get("role") == "resident":
            print("  ✅ 通过：居民账号角色确认为 resident")
            results.append(("测试4c：居民账号角色确认", "通过", ""))
        else:
            print(f"  ❌ 失败：角色不匹配")
            results.append(("测试4c：居民账号角色确认", "失败", "角色不匹配"))
    
        # 测试 4d：重复用户名注册应被拒绝
        print("  4d. 重复用户名注册应被拒绝...")
        success3, message3, user3 = auth.register_user(
            username=test_username,
            password="test123456",
            real_name="重复测试",
            phone="13900000005",
            id_card="110101199001011238",
            role="resident",
            building="1栋", unit="1单元", room="101",
            register_lat=30.274150, register_lng=120.155150
        )
        if not success3:
            print(f"  ✅ 通过：重复用户名被拒绝，message='{message3}'")
            results.append(("测试4d：重复注册拒绝", "通过", ""))
        else:
            print("  ❌ 失败：重复用户名未被拒绝")
            results.append(("测试4d：重复注册拒绝", "失败", "重复用户名未被拒绝"))
    
    # ------------------------------------------------------------------
    # 测试汇总
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)
    
    passed = 0
    failed = 0
    for name, status, detail in results:
        icon = "✅" if status == "通过" else "❌"
        detail_str = f" — {detail}" if detail else ""
        print(f"  {icon} {name}{detail_str}")
        if status == "通过":
            passed += 1
        else:
            failed += 1
    
    print(f"\n总计: {passed + failed} 项测试, {passed} 通过, {failed} 失败")
    print("=" * 60)
    assert failed == 0, f"{failed} 项失败，详见上方明细"

def main():
    # 直跑：备份 data/secure -> 空目录 -> 运行 -> finally 恢复（脚本自足性）
    for src, bak in [(DATA_DIR, BAK_DATA_DIR), (SECURE_DIR, BAK_SECURE_DIR)]:
        _backup(src, bak)
    os.makedirs(DATA_DIR, exist_ok=True)
    code = 1
    try:
        try:
            test_suite()
            code = 0
        except AssertionError:
            code = 1
    finally:
        for src, bak in [(DATA_DIR, BAK_DATA_DIR), (SECURE_DIR, BAK_SECURE_DIR)]:
            _restore(src, bak)
    sys.exit(code)


if __name__ == "__main__":
    # 直跑模式恢复 UTF-8 输出（pytest 模式由 conftest/runner 保证）
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    main()


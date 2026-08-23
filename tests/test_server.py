#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_server.py
自动化测试脚本：测试社区网格员助手的后端 API。

测试场景：
  1. 注册测试账号（如已注册则跳过）
  2. 登录获取 Token
  3. 依次发送 3 条事件描述并打印完整响应
     - "我家楼下下水道堵了"
     - "割腕"
     - "我去买菜了"
"""

import requests
import json
import sys
import time
import io
import os
os.environ["AUTH_STORE"] = "file"
import pytest

# 远程部署冒烟（需真实服务器 118.31.58.191），默认 skip，非本地回归
pytestmark = pytest.mark.skip(reason="远程部署冒烟，非本地回归")

# 修复 Windows GBK 控制台编码问题

# ------------------------------------------------------------------
# 配置
# ------------------------------------------------------------------
BASE_URL = "http://118.31.58.191:8000"

# 测试账号信息（每次随机后缀避免冲突）
TEST_USERNAME = "testuser_" + str(int(time.time()))[-6:]
TEST_PASSWORD = "test123456"
TEST_REAL_NAME = "测试居民"
TEST_PHONE = "13900001111"

# 三条测试描述
TEST_DESCRIPTIONS = [
    "我家楼下下水道堵了",
    "割腕",
    "我去买菜了",
]


def print_separator(title: str) -> None:
    """打印分隔标题。"""
    width = 70
    print("")
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_json(label: str, data: dict) -> None:
    """格式化打印 JSON 响应。"""
    print(f"\n  [{label}]")
    print(json.dumps(data, ensure_ascii=False, indent=4))


def test_suite():
    # ==================================================================
    # 场景 1：注册测试账号
    # ==================================================================
    print_separator("场景 1：注册测试账号")
    
    register_payload = {
        "username": TEST_USERNAME,
        "password": TEST_PASSWORD,
        "real_name": TEST_REAL_NAME,
        "phone": TEST_PHONE,
        "role": "resident",
        "building": "1栋",
        "unit": "1单元",
        "room": "101",
    }
    
    print(f"  -> POST {BASE_URL}/api/auth/register")
    print(f"    请求体: {json.dumps(register_payload, ensure_ascii=False)}")
    
    try:
        resp = requests.post(
            f"{BASE_URL}/api/auth/register",
            json=register_payload,
            timeout=10,
        )
        print(f"  HTTP 状态码: {resp.status_code}")
        print_json("响应", resp.json())
    
        if resp.status_code == 200 and resp.json().get("success"):
            print("  [OK] 注册成功！")
            token = None  # 注册后仍需登录获取 token
        else:
            error_msg = resp.json().get("error", "")
            if "已被注册" in error_msg:
                print(f"  [WARN] 账号已存在，将尝试直接登录")
            else:
                print(f"  [WARN] 注册失败: {error_msg}，将尝试直接登录")
    
    except requests.exceptions.ConnectionError:
        print(f"  [FAIL] 无法连接到服务器 {BASE_URL}")
        print("    请确认：")
        print("    1. 服务器是否已启动")
        print("    2. 网络是否可达")
        print("    3. 防火墙是否放行 8000 端口")
        raise AssertionError("远程冒烟失败，详见上方输出")
    except Exception as e:
        print(f"  [FAIL] 注册请求异常: {e}")
        raise AssertionError("远程冒烟失败，详见上方输出")
    
    # ==================================================================
    # 场景 2：登录获取 Token
    # ==================================================================
    print_separator("场景 2：登录获取 Token")
    
    login_payload = {
        "username": TEST_USERNAME,
        "password": TEST_PASSWORD,
    }
    
    print(f"  -> POST {BASE_URL}/api/auth/login")
    print(f"    请求体: {json.dumps(login_payload, ensure_ascii=False)}")
    
    try:
        resp = requests.post(
            f"{BASE_URL}/api/auth/login",
            json=login_payload,
            timeout=10,
        )
        print(f"  HTTP 状态码: {resp.status_code}")
        data = resp.json()
        print_json("响应", data)
    
        if resp.status_code == 200 and data.get("success"):
            token = data["data"]["token"]
            user_info = data["data"]["user"]
            print(f"  [OK] 登录成功！")
            print(f"    Token 前 20 位: {token[:20]}...")
            print(f"    用户名: {user_info.get('username')}")
            print(f"    角色:   {user_info.get('role')}")
        else:
            print(f"  [FAIL] 登录失败: {data.get('error', '未知错误')}")
            # 尝试用默认管理员账号登录
            print("\n  -> 尝试使用默认管理员账号登录...")
            admin_login = {
                "username": "admin",
                "password": "admin123456",
            }
            resp2 = requests.post(
                f"{BASE_URL}/api/auth/login",
                json=admin_login,
                timeout=10,
            )
            data2 = resp2.json()
            print_json("管理员登录响应", data2)
            if resp2.status_code == 200 and data2.get("success"):
                token = data2["data"]["token"]
                print(f"  [OK] 使用管理员账号登录成功！")
            else:
                print(f"  [FAIL] 管理员登录也失败，无法继续测试")
                raise AssertionError("远程冒烟失败，详见上方输出")
    
    except Exception as e:
        print(f"  [FAIL] 登录请求异常: {e}")
        raise AssertionError("远程冒烟失败，详见上方输出")
    
    # ==================================================================
    # 场景 3：依次发送 3 条事件描述
    # ==================================================================
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    for idx, description in enumerate(TEST_DESCRIPTIONS, 1):
        print_separator(f"场景 3.{idx}：发送 → \"{description}\"")
    
        event_payload = {"description": description}
    
        print(f"  -> POST {BASE_URL}/api/events")
        print(f"    Authorization: Bearer {token[:20]}...")
        print(f"    请求体: {json.dumps(event_payload, ensure_ascii=False)}")
    
        try:
            resp = requests.post(
                f"{BASE_URL}/api/events",
                json=event_payload,
                headers=headers,
                timeout=70,  # 后台处理最长 60 秒，留余量
            )
            print(f"  HTTP 状态码: {resp.status_code}")
            data = resp.json()
            print_json("完整响应", data)
    
            # 简要解读
            if data.get("success"):
                event_data = data.get("data", {})
                print(f"\n  [OK] 提交成功！")
                print(f"    event_id:   {event_data.get('event_id', 'N/A')}")
                print(f"    event_type: {event_data.get('event_type', 'N/A')}")
                print(f"    urgency:    {event_data.get('urgency', 'N/A')}")
                print(f"    scene_tag:  {event_data.get('scene_tag', 'N/A')}")
                print(f"    status:     {event_data.get('status', 'N/A')}")
                print(f"    created_at: {event_data.get('created_at', 'N/A')}")
            else:
                error = data.get("error", "未知错误")
                print(f"\n  [BLOCKED] 被拦截: {error}")
    
        except requests.exceptions.Timeout:
            print(f"  [WARN] 请求超时（>70秒），服务器可能正在处理中")
        except Exception as e:
            print(f"  [FAIL] 请求异常: {e}")
    
    # ==================================================================
    # 测试完成
    # ==================================================================
    print_separator("测试完成")
    
    print(f"""
      总结:
      ─────────────────────────────────────
      服务器地址: {BASE_URL}
      测试账号:   {TEST_USERNAME}
      测试密码:   {TEST_PASSWORD}
    
      3 条事件已全部发送，详见上方输出。
      可通过以下命令查询事件列表:
        curl -H "Authorization: Bearer {token[:20]}..." {BASE_URL}/api/events
    """)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        test_suite()
    except AssertionError:
        sys.exit(1)
    sys.exit(0)

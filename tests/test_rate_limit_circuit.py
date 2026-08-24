# -*- coding: utf-8 -*-
"""
tests/test_rate_limit_circuit.py
限流 + LLM 熔断/退避重试专项测试（T20260821-004）

本文件在导入 config/main 之前显式置 RATE_LIMIT_ENABLED=true（与 conftest 默认
false 相反），验证：
  1. 登录/注册限流 5 次/分钟/IP：第 6 次 HTTP 429 + 精确 JSON
     {"detail": "请求过于频繁，请稍后再试"}
  2. POST /api/events 限流 10 次/分钟/用户（keyfunc=Bearer user_id）：
     第 11 次 429，且其他用户不受影响
  3. LLM 熔断：连续失败 >= 阈值 -> open；open 期间不再调用 LLM（mock 计数不变）；
     cooldown 到期自动半开，试探成功恢复 closed、试探失败重新 open
  4. LLM 退避重试：瞬时失败（连接/超时/限流/5xx）自动重试 2 次；
     业务解析类异常（ValueError 等）不重试

说明：限流用例通过 rl_ctx fixture 复用同一 main app（receive_node 仅在
main 命名空间内替换为 mock，receive_agent.receive_node 保持真实，供熔断用例
直接调用）；每个限流用例开头调用 limiter.reset() 清空窗口，避免用例间串扰。
"""

import os
import sys
import time
from unittest.mock import patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 生产默认熔断/重试参数由 config 提供；测试内用 monkeypatch 短阈值/短退避覆盖，
# 此处不覆盖，保持 config.py 默认值可被 grep 验收。


def _base_state(desc: str = "小区楼下下水道堵了") -> dict:
    return {
        "description": desc,
        "address": "",
        "event_type": "",
        "urgency": "",
        "scene_tag": "",
        "handler": "",
        "confidence": "",
        "confirmation_required": False,
        "emergency_type": "",
        "confirmed": False,
    }


def _mock_receive_node(state):
    """main 命名空间用 mock：有效描述返回固定语义结果，无效输入走拦截路径。"""
    import receive_agent as _ra

    desc = (state.get("description") or "").strip()
    if not _ra._is_valid_input(desc):
        return {"description": desc, "address": "", "event_type": "无效输入", "urgency": "低", "handler": ""}
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


def _valid_llm_result(desc: str) -> dict:
    """熔断恢复/重试用：模拟一次成功的 LLM 解析结果。"""
    return {
        "is_valid": True,
        "reject_reason": "",
        "address": "小区3号楼",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
    }


def _wait_state(breaker, target: str, timeout: float = 2.0) -> str:
    """轮询等待熔断器进入目标状态（Windows 定时器粒度粗，避免固定 sleep 竞态）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = breaker.state()
        if st == target:
            return st
        time.sleep(0.01)
    return breaker.state()


@pytest.fixture(scope="function")
def rl_ctx(monkeypatch):
    """限流测试上下文：显式开启限流的 main app + TestClient。

    receive_node 仅替换 main 命名空间（receive_agent.receive_node 保持真实，
    供熔断/重试用例直接调用）。
    """
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_LOGIN", "5/minute")
    monkeypatch.setenv("RATE_LIMIT_EVENTS", "10/minute")
    import importlib

    import config
    importlib.reload(config)  # 确保 RATE_LIMIT_ENABLED=true 生效
    # 清理 main 模块缓存，确保重新导入时装饰器绑定新的 config 限流规则
    if "main" in sys.modules:
        del sys.modules["main"]
        # 清理 prometheus 默认注册表，避免 Instrumentator 重复注册导致计数失效
        from prometheus_client import REGISTRY
        for collector in list(REGISTRY._collector_to_names.keys()):
            REGISTRY.unregister(collector)
    import auth
    importlib.reload(auth)

    p1 = patch("dispatch_agent.logger")
    p2 = patch("record_agent.logger")
    p1.start()
    p2.start()
    try:
        with patch("receive_agent.OpenAI"):
            from main import app, limiter
            import main as main_module

        # 仅替换 main 命名空间的 receive_node，事件提交不触发真实 LLM
        main_module.receive_node = _mock_receive_node

        from fastapi.testclient import TestClient

        client = TestClient(app)
        yield {"client": client, "app": app, "limiter": limiter, "main": main_module}
    finally:
        main_module.app.state.limiter.enabled = False
        main_module.app.state.limiter.reset()
        p2.stop()
        p1.stop()


# ----------------------------------------------------------------------
# 1. 登录/注册限流（5 次/分钟/IP）
# ----------------------------------------------------------------------
def test_login_rate_limit_429(rl_ctx):
    client = rl_ctx["client"]
    rl_ctx["limiter"].reset()
    for i in range(5):
        r = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
        assert r.status_code == 200, f"第 {i + 1} 次登录不应被限流: {r.status_code} {r.text}"
    r6 = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
    assert r6.status_code == 429, f"第 6 次登录应 429: {r6.status_code} {r6.text}"
    assert r6.json() == {"success": False, "error": "请求过于频繁，请稍后再试"}, r6.text


def test_register_rate_limit_429(rl_ctx):
    client = rl_ctx["client"]
    rl_ctx["limiter"].reset()
    payload = {
        "password": "test123456",
        "real_name": "限流居民",
        "phone": "13900000001",
        "role": "resident",
        "building": "1栋",
        "unit": "1单元",
        "room": "101",
        "register_lat": 30.274150,
        "register_lng": 120.155150,
    }
    for i in range(5):
        p = dict(payload, username=f"rl_reg_user_{i}", phone=f"1390000000{i + 1}")
        r = client.post("/api/auth/register", json=p)
        assert r.status_code == 200 and r.json().get("success") is True, (
            f"第 {i + 1} 次注册不应被限流: {r.status_code} {r.text}"
        )
    p6 = dict(payload, username="rl_reg_user_6", phone="13900000007")
    r6 = client.post("/api/auth/register", json=p6)
    assert r6.status_code == 429, f"第 6 次注册应 429: {r6.status_code} {r6.text}"
    assert r6.json() == {"success": False, "error": "请求过于频繁，请稍后再试"}, r6.text


# ----------------------------------------------------------------------
# 2. POST /api/events 限流（10 次/分钟/用户）
# ----------------------------------------------------------------------
def test_events_rate_limit_per_user(rl_ctx):
    client = rl_ctx["client"]
    rl_ctx["limiter"].reset()

    def _register_login(username, phone):
        r = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "real_name": "事件限流用户",
            "phone": phone,
            "role": "resident",
            "building": "1栋",
            "unit": "1单元",
            "room": "101",
            "register_lat": 30.274150,
            "register_lng": 120.155150,
        })
        assert r.status_code == 200 and r.json().get("success") is True, r.text
        lr = client.post("/api/auth/login", json={"username": username, "password": "test123456"})
        token = lr.json().get("data", {}).get("token")
        assert token, f"登录失败: {lr.text}"
        return {"Authorization": f"Bearer {token}"}

    headers_a = _register_login("rl_ev_user_a", "13900000011")
    for i in range(10):
        r = client.post("/api/events", json={"description": "小区楼下下水道堵了"}, headers=headers_a)
        assert r.status_code == 200 and r.json().get("success") is True, (
            f"第 {i + 1} 次事件提交不应被限流: {r.status_code} {r.text}"
        )
    r11 = client.post("/api/events", json={"description": "小区楼下下水道堵了"}, headers=headers_a)
    assert r11.status_code == 429, f"第 11 次事件提交应 429: {r11.status_code} {r11.text}"
    assert r11.json() == {"success": False, "error": "请求过于频繁，请稍后再试"}, r11.text

    # keyfunc=user_id：用户 B 不受用户 A 限流影响
    headers_b = _register_login("rl_ev_user_b", "13900000012")
    rb = client.post("/api/events", json={"description": "小区东门路灯坏了"}, headers=headers_b)
    assert rb.status_code == 200 and rb.json().get("success") is True, rb.text


# ----------------------------------------------------------------------
# 3. LLM 熔断器（open / open 期间不调 LLM / cooldown 半开恢复）
# ----------------------------------------------------------------------
def test_circuit_breaker_open_skips_llm_calls(monkeypatch):
    import receive_agent as ra

    breaker = ra._LLMCircuitBreaker(threshold=3, cooldown=0.05)
    monkeypatch.setattr(ra, "llm_circuit", breaker)
    monkeypatch.setattr(ra, "SEMANTIC_CHECK_ROUNDS", 1)

    calls = {"n": 0}

    def _fail(desc):
        calls["n"] += 1
        raise TimeoutError("mock LLM 瞬时故障")

    monkeypatch.setattr(ra, "_call_llm_once", _fail)

    state = _base_state()
    for i in range(3):
        result = ra.receive_node(state)
        assert result["event_type"] == "API异常", f"第 {i + 1} 次应走 API异常 降级: {result}"
        assert result["confidence"] == "none", result
    assert breaker.state() == "open", f"连续失败应熔断 open，当前 state={breaker.state()}"

    n_before = calls["n"]
    result = ra.receive_node(state)
    assert calls["n"] == n_before, "open 期间不应再调用 LLM（mock 调用计数必须不变）"
    assert result["event_type"] == "API异常" and result["confidence"] == "none", result


def test_circuit_breaker_cooldown_half_open_recovery(monkeypatch):
    import receive_agent as ra

    breaker = ra._LLMCircuitBreaker(threshold=2, cooldown=0.05)
    monkeypatch.setattr(ra, "llm_circuit", breaker)
    monkeypatch.setattr(ra, "SEMANTIC_CHECK_ROUNDS", 1)

    calls = {"n": 0}

    def _fail(desc):
        calls["n"] += 1
        raise TimeoutError("mock LLM 瞬时故障")

    monkeypatch.setattr(ra, "_call_llm_once", _fail)

    state = _base_state()
    for _ in range(2):
        ra.receive_node(state)
    assert breaker.state() == "open"

    # 冷却到期 -> 半开，允许一次试探（轮询等待，兼容粗粒度定时器）
    assert _wait_state(breaker, "half_open") == "half_open", f"冷却到期应半开，当前 state={breaker.state()}"

    # 试探成功 -> 恢复 closed，计数清零
    def _ok(desc):
        calls["n"] += 1
        return _valid_llm_result(desc)
    monkeypatch.setattr(ra, "_call_llm_once", _ok)
    n_before = calls["n"]
    result = ra.receive_node(state)
    assert calls["n"] == n_before + 1, "半开试探应恰好触发一次真实调用"
    assert breaker.state() == "closed", f"试探成功应恢复 closed，当前 state={breaker.state()}"
    assert result["event_type"] == "物业维修", result

    # 试探失败 -> 重新 open（再次冷却后仍可恢复）
    monkeypatch.setattr(ra, "_call_llm_once", _fail)
    for _ in range(2):
        ra.receive_node(state)
    assert breaker.state() == "open"
    assert _wait_state(breaker, "half_open") == "half_open"
    ra.receive_node(state)  # 半开试探（此次失败）
    assert breaker.state() == "open", "半开试探失败应重新 open"


# ----------------------------------------------------------------------
# 4. LLM 退避重试（瞬时异常重试 2 次；业务异常不重试）
# ----------------------------------------------------------------------
def test_retry_transient_then_success(monkeypatch):
    import receive_agent as ra

    monkeypatch.setattr(ra.config, "LLM_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(ra.config, "LLM_RETRY_BASE_DELAY", 0.001)

    calls = {"n": 0}

    def _flaky(desc):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TimeoutError("mock 连接超时（瞬时）")
        return _valid_llm_result(desc)

    monkeypatch.setattr(ra, "_call_llm_once_impl", _flaky)
    result = ra._call_llm_once("小区楼下下水道堵了")
    assert calls["n"] == 3, f"瞬时失败应重试 2 次（共 3 次调用），实际 {calls['n']}"
    assert result["event_type"] == "物业维修", result


def test_retry_no_retry_on_business_error(monkeypatch):
    import receive_agent as ra

    monkeypatch.setattr(ra.config, "LLM_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(ra.config, "LLM_RETRY_BASE_DELAY", 0.001)

    calls = {"n": 0}

    def _bad_json(desc):
        calls["n"] += 1
        raise ValueError("mock JSON 解析失败（业务异常，不重试）")

    monkeypatch.setattr(ra, "_call_llm_once_impl", _bad_json)
    with pytest.raises(ValueError):
        ra._call_llm_once("小区楼下下水道堵了")
    assert calls["n"] == 1, f"业务解析类异常不应重试，实际调用 {calls['n']} 次"

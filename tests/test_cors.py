# -*- coding: utf-8 -*-
"""
tests/test_cors.py
CORS 跨域来源白名单回归测试（任务书 T20260821-002）

覆盖（冻结契约可测试部分）：
1. 默认配置（未设 CORS_ALLOW_ORIGINS）：放行本机 http://127.0.0.1:8000、
   http://localhost:8000 与生产前端 http://118.31.58.191:8000（ACAO 回显该 origin）；
   不放行 http://evil.example.com（响应无 access-control-allow-origin 头）。
2. 设 CORS_ALLOW_ORIGINS=https://a.example.com, https://b.example.com 后：
   a/b 放行，旧默认 118.31.58.191 不再放行。
3. CORS_ALLOW_ORIGINS=* 时：allow_credentials 置 False（响应无
   access-control-allow-credentials 头）+ 记录 warning 日志。
4. 默认配置下 main.py 源码不存在 allow_origins=["*"]（静态断言）。

说明：config 与 main 在模块导入期读取环境变量，故每个用例通过
importlib.reload 重建 config/main/app 以模拟不同 CORS_ALLOW_ORIGINS。
本文件由 run_regression 以独立子进程运行，reload 不影响其它测试脚本。
"""
import importlib
import logging
import os
import sys

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

# 环境固定（与 conftest 一致；CORS_ALLOW_ORIGINS 由各用例动态设置/清除）
os.environ.setdefault("AUTH_STORE", "file")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "1" * 64)
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://test")

DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://118.31.58.191:8000",
)
PROD_FRONTEND_ORIGIN = "http://118.31.58.191:8000"
EVIL_ORIGIN = "http://evil.example.com"


def _set_cors_env(value):
    """value=None 表示「未设置」（移除环境变量，走 config 默认值）。"""
    if value is None:
        os.environ.pop("CORS_ALLOW_ORIGINS", None)
    else:
        os.environ["CORS_ALLOW_ORIGINS"] = value


def _build_client(cors_env=None):
    """按给定 CORS_ALLOW_ORIGINS 重建 config + main + TestClient。

    先 patch receive_agent.OpenAI（与 test_security_authorization 一致），
    避免模块级构造真实 OpenAI 客户端；随后 reload config 与 main，
    使中间件按最新环境变量注册。
    """
    _set_cors_env(cors_env)
    from unittest.mock import patch

    _patch_openai = patch("receive_agent.OpenAI")
    _patch_openai.start()
    try:
        import config
        importlib.reload(config)
        import main
        importlib.reload(main)
        from fastapi.testclient import TestClient

        return TestClient(main.app)
    finally:
        _patch_openai.stop()


def _get_health(client, origin):
    return client.get("/health", headers={"Origin": origin})


# ------------------------------------------------------------------
# 1. 默认配置
# ------------------------------------------------------------------
def test_default_allows_local_and_production_origins():
    client = _build_client(None)
    for origin in DEFAULT_ALLOWED_ORIGINS:
        resp = _get_health(client, origin)
        assert resp.status_code == 200, resp.text
        acao = resp.headers.get("access-control-allow-origin")
        assert acao == origin, f"期望回显 {origin}，实际 {acao!r}"


def test_default_rejects_evil_origin():
    client = _build_client(None)
    resp = _get_health(client, EVIL_ORIGIN)
    assert resp.status_code == 200, resp.text
    assert "access-control-allow-origin" not in resp.headers


# ------------------------------------------------------------------
# 2. 环境变量覆盖
# ------------------------------------------------------------------
def test_env_override_replaces_default_whitelist():
    custom = ["https://a.example.com", "https://b.example.com"]
    client = _build_client("https://a.example.com, https://b.example.com")
    for origin in custom:
        resp = _get_health(client, origin)
        assert resp.status_code == 200, resp.text
        acao = resp.headers.get("access-control-allow-origin")
        assert acao == origin, f"期望回显 {origin}，实际 {acao!r}"
    # 旧默认不再放行
    resp = _get_health(client, PROD_FRONTEND_ORIGIN)
    assert resp.status_code == 200, resp.text
    assert "access-control-allow-origin" not in resp.headers


# ------------------------------------------------------------------
# 3. 通配符 *
# ------------------------------------------------------------------
def test_wildcard_disables_credentials_and_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="main"):
        client = _build_client("*")
        resp = _get_health(client, EVIL_ORIGIN)
    assert resp.status_code == 200, resp.text
    # 通配放行 → 存在 ACAO 头；但绝不携带凭据头（通配 + 凭据组合被浏览器拒绝）
    assert "access-control-allow-origin" in resp.headers
    assert "access-control-allow-credentials" not in resp.headers
    # 记录 warning 日志
    assert any(
        r.levelno >= logging.WARNING and "allow_credentials" in r.getMessage()
        for r in caplog.records
    ), "未捕获 allow_credentials 降级 warning 日志"


# ------------------------------------------------------------------
# 4. 默认源码不得存在 allow_origins=["*"]
# ------------------------------------------------------------------
def test_default_source_has_no_wildcard_allow_origins():
    with open(os.path.join(PROJECT_DIR, "main.py"), "r", encoding="utf-8") as f:
        source = f.read()
    assert 'allow_origins=["*"]' not in source

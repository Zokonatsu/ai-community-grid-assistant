# -*- coding: utf-8 -*-
"""
tests/test_metrics.py
Prometheus 指标端点冒烟测试（T20260821-005）

覆盖（冻结契约可测试部分）：
1. GET /metrics 无需鉴权：不携带 Authorization 也返回 200；
2. Content-Type：text/plain; version=0.0.4; charset=utf-8；
3. 响应体包含 http_requests_total 与 http_request_duration_seconds 标准指标；
4. 可观测性冒烟：连续访问 GET /health 2 次后 http_requests_total 对应计数增加
   （delta >= 2）；
5. RATE_LIMIT_ENABLED=true 时 /metrics 与 /health 仍 200（指标端点不参与业务限流）；
6. 静态断言：main.py 使用 Instrumentator().add(metrics.default())...expose(
   endpoint="/metrics", include_in_schema=False)（默认指标注册 + 不进 OpenAPI）。

说明：本文件显式置 RATE_LIMIT_ENABLED=true（与 conftest 默认 false 相反），
在导入 config/main 前设置，验证「指标端点不受限流开关影响」。main 在 module
fixture 内懒加载（先 patch receive_agent.OpenAI，与既有测试一致），避免模块
导入期触碰真实 data/secure。
"""
import os
import re
import sys
from unittest.mock import patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 显式开启限流：必须在导入 config/main 之前设置（conftest 默认置 false）。
os.environ["RATE_LIMIT_ENABLED"] = "true"


@pytest.fixture(scope="module")
def metrics_ctx():
    """重建 config+main（RATE_LIMIT_ENABLED=true）+ TestClient。

    与 test_security_authorization 一致：先 patch receive_agent.OpenAI 再导入
    main，避免模块级构造真实 OpenAI 客户端；在 fixture 内懒加载，保证运行于
    conftest 数据隔离之后。
    """
    import config  # noqa: F401  确保环境变量先于 main 读取

    _patch = patch("receive_agent.OpenAI")
    _patch.start()
    try:
        import main  # 首次导入即可（本进程独立运行，无需 reload；
        # reload 会令 metrics.default() 在默认注册表上重复注册被吞，计数失效）
        from fastapi.testclient import TestClient

        return TestClient(main.app)
    finally:
        _patch.stop()


def _requests_total(body: str) -> float:
    """解析 http_requests_total 全序列求和（prometheus-client 输出浮点 2.0）。"""
    return sum(
        float(v)
        for v in re.findall(r"^http_requests_total\{[^}]*\} ([\d.]+)$", body, re.M)
    )


def test_metrics_no_auth_200_and_content_type(metrics_ctx):
    """无鉴权 + 200 + text/plain; version=0.0.4。"""
    resp = metrics_ctx.get("/metrics")  # 不携带 Authorization
    assert resp.status_code == 200
    ct = resp.headers.get("content-type", "")
    assert ct.startswith("text/plain")
    assert "0.0.4" in ct
    assert "charset=utf-8" in ct


def test_metrics_contains_standard_metrics(metrics_ctx):
    """包含 http_requests_total 与 http_request_duration_seconds 标准指标。"""
    body = metrics_ctx.get("/metrics").text
    assert "# HELP http_requests_total" in body
    assert "http_requests_total{" in body
    assert "# HELP http_request_duration_seconds" in body
    assert "http_request_duration_seconds_bucket" in body


def test_metrics_counter_changes_on_health_hits(metrics_ctx):
    """可观测性冒烟：连续 2 次 /health 后计数增加。"""
    before = _requests_total(metrics_ctx.get("/metrics").text)
    assert metrics_ctx.get("/health").status_code == 200
    assert metrics_ctx.get("/health").status_code == 200
    after = _requests_total(metrics_ctx.get("/metrics").text)
    assert after - before >= 2


def test_metrics_not_limited_when_rate_limit_enabled(metrics_ctx):
    """RATE_LIMIT_ENABLED=true 下 /metrics 与 /health 仍 200（不限流）。"""
    assert metrics_ctx.get("/metrics").status_code == 200
    assert metrics_ctx.get("/health").status_code == 200


def test_main_uses_instrumentator_default_metrics():
    """静态断言：默认指标注册、/metrics 端点与不进 OpenAPI schema。"""
    with open(os.path.join(PROJECT_DIR, "main.py"), encoding="utf-8") as f:
        src = f.read()
    assert "Instrumentator()" in src
    assert "metrics.default()" in src
    assert 'endpoint="/metrics"' in src
    assert "include_in_schema=False" in src

# -*- coding: utf-8 -*-
"""
scripts/smoke_test.py — 部署冒烟（只读）

对运行中的服务做上线后冒烟检查：
  1. GET /health            -> 200 + {"status":"ok"}
  2. GET /                  -> 200（首页/静态资源）
  3. GET /metrics           -> 200（Prometheus 指标）
  4. POST /api/auth/login   -> 错误凭据返回 401/400（非 500）
  5. GET /api/events        -> 无 token 返回 401（鉴权生效）

用法：
    python scripts/smoke_test.py                      # BASE_URL 默认 http://127.0.0.1:8000
    python scripts/smoke_test.py http://118.31.58.191:8000
    $env:BASE_URL='http://118.31.58.191:8000'; python scripts/smoke_test.py
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")


def get(path, timeout=10):
    req = urllib.request.Request(BASE_URL + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def post(path, payload, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE_URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main():
    checks = []

    # 1. 健康检查
    try:
        code, body = get("/health")
        ok = code == 200 and '"ok"' in body
        checks.append(("GET /health -> 200 + ok", ok, f"HTTP {code} {body[:80]}"))
    except Exception as exc:
        checks.append(("GET /health -> 200 + ok", False, repr(exc)))

    # 2. 首页
    try:
        code, _ = get("/")
        checks.append(("GET / -> 200 首页", code == 200, f"HTTP {code}"))
    except Exception as exc:
        checks.append(("GET / -> 200 首页", False, repr(exc)))

    # 3. /metrics
    try:
        code, body = get("/metrics")
        checks.append(("GET /metrics -> 200 指标", code == 200 and "python_" in body, f"HTTP {code}"))
    except Exception as exc:
        checks.append(("GET /metrics -> 200 指标", False, repr(exc)))

    # 4. 登录（错误凭据 -> 401/400，非 500）
    try:
        code, body = post("/api/auth/login", {"username": "__smoke_nobody__", "password": "x"})
        # 业务约定：失败返回 200 + success:false；也可能 401/400，均视为鉴权生效
        ok_login = code in (400, 401) or (code == 200 and '"success":false' in body)
        checks.append(("POST /login 错误凭据 -> 拒绝", ok_login, f"HTTP {code} {body[:60]}"))
    except Exception as exc:
        checks.append(("POST /login 错误凭据 -> 401/400", False, repr(exc)))

    # 5. 鉴权（无 token 访问事件 -> 401）
    try:
        code, _ = get("/api/events")
        checks.append(("GET /api/events 无token -> 401", code == 401, f"HTTP {code}"))
    except Exception as exc:
        checks.append(("GET /api/events 无token -> 401", False, repr(exc)))

    print("=" * 60)
    print(f"  部署冒烟：{BASE_URL}")
    print("=" * 60)
    failed = 0
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] {name}（{detail}）")
    print("=" * 60)
    if failed:
        print(f"  冒烟结果：{len(checks) - failed}/{len(checks)} 通过，失败 {failed} 项")
        return 1
    print(f"  冒烟结果：{len(checks)}/{len(checks)} 全部通过（OK）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
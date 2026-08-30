"""
health_probe.py
服务健康探针：轮询 /health，失败时通过 webhook 告警（可选）。

用法：
    python scripts/health_probe.py                 # 检查 http://127.0.0.1:8000/health
    python scripts/health_probe.py --url http://127.0.0.1:8000 --once
    # 告警：设置环境变量 ALERT_WEBHOOK_URL（可选），失败时 POST JSON 到该地址

退出码：0=健康，1=不健康。可配合计划任务/系统服务定期执行。
"""
import argparse
import json
import os
import sys
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"


def _post_alert(msg, webhook, status):
    if not webhook:
        return
    payload = json.dumps(
        {"title": "AI社区网格员助手 - 健康探针告警", "text": msg, "status": status}
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:  # noqa: BLE001
        print("[probe] 告警发送失败：%s" % exc, file=sys.stderr)


def check(base, timeout, webhook):
    url = base.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            ok = resp.status == 200 and '"ok"' in body
    except Exception as exc:  # noqa: BLE001
        msg = "[probe] 不健康：%s 无法访问（%s）" % (url, exc)
        print(msg, file=sys.stderr)
        _post_alert(msg, webhook, "down")
        return 1

    if not ok:
        msg = "[probe] 不健康：%s 返回异常：%s" % (url, body[:200])
        print(msg, file=sys.stderr)
        _post_alert(msg, webhook, "degraded")
        return 1

    print("[probe] 健康：%s -> %s" % (url, body.strip()))
    return 0


def main():
    ap = argparse.ArgumentParser(description="健康探针")
    ap.add_argument("--url", default=DEFAULT_BASE, help="服务根地址")
    ap.add_argument("--timeout", type=int, default=5, help="超时秒数")
    ap.add_argument("--once", action="store_true", help="仅检查一次")
    args = ap.parse_args()
    webhook = os.environ.get("ALERT_WEBHOOK_URL", "")
    code = check(args.url, args.timeout, webhook)
    sys.exit(code)


if __name__ == "__main__":
    main()

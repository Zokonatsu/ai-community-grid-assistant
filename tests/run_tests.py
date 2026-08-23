"""
tests/run_tests.py —— 实时冒烟脚本（定位说明，T20260820-001-TD）。

定位：实时冒烟脚本，不是 pytest 回归套件。
- 前置条件：服务需已启动（默认 127.0.0.1:8000）且下方 TOKEN 有效；
- 行为：直接对运行中的服务发 HTTP 请求，验证输入稳定性
  （多轮投票 / 置信度 / 待审核）；
- 回归请用 tests/run_regression.py core / full（pytest 套件，含数据隔离与崩溃保护）。

原始说明：Test script: verify input validation stability (multi-round voting, confidence, pending review)
"""
import subprocess
import json
import time
import sys
import os
import tempfile

TOKEN = "X1_oadI2AXDMDeTjUP_boP8gG35uY1Jq2ygjeA4OuZk"
URL = "http://127.0.0.1:8000/api/events"
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_results.json")

INPUTS = [
    "水管破了",
    "人死了",
    "我家狗死了 太重了搬不动",
    "在吗？",
    "我家着火了",
]

def test_one(description):
    """Run one test via curl subprocess"""
    body_file = os.path.join(tempfile.gettempdir(), "_test_body.json")
    with open(body_file, "w", encoding="utf-8") as f:
        json.dump({"description": description}, f, ensure_ascii=False)

    start = time.time()
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", URL,
         "-H", "Content-Type: application/json; charset=utf-8",
         "-H", f"Authorization: Bearer {TOKEN}",
         "-d", f"@{body_file}"],
        capture_output=True, timeout=60
    )
    elapsed_ms = round((time.time() - start) * 1000)
    raw = result.stdout.decode("utf-8") if result.stdout else ""
    try:
        data = json.loads(raw)
        return elapsed_ms, data
    except json.JSONDecodeError:
        return elapsed_ms, {"_error": f"JSON parse failed: {raw[:200]}"}

def classify(data):
    """Classify response status"""
    if "_error" in data:
        err = data["_error"]
        if "timeout" in err.lower():
            return "API_TIMEOUT"
        return f"ERROR: {err[:80]}"
    if not data.get("success"):
        err = data.get("error", "")
        if "无效" in err:
            return "REJECTED(quick_check)"
        if "不可用" in err or "稍后重试" in err:
            return "API_UNAVAILABLE"
        return f"REJECTED: {err[:80]}"
    d = data.get("data", {})
    return d.get("status", "UNKNOWN")

all_results = []  # list of detailed results
summary = {}  # input -> [classifications]

print("=" * 80, flush=True)
print("Input Validation Stability Test", flush=True)
print(f"Start: {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
print("=" * 80, flush=True)

for round_num in [1, 2, 3]:
    print(f"\n--- Round {round_num} ---", flush=True)
    for inp in INPUTS:
        elapsed, data = test_one(inp)
        cls = classify(data)
        summary.setdefault(inp, []).append(cls)

        record = {
            "round": round_num,
            "input": inp,
            "elapsed_ms": elapsed,
            "classification": cls,
            "raw_response": data,
        }
        all_results.append(record)

        # Brief output
        detail = ""
        if data.get("success") and data.get("data"):
            d = data["data"]
            detail = f"status={d.get('status','?')} event_type={d.get('event_type','?')} urgency={d.get('urgency','?')} scene_tag={d.get('scene_tag','?')} address={d.get('address','?')}"
        else:
            detail = f"error={data.get('error', data.get('_error', ''))}"
        print(f"  [{cls}] \"{inp}\" ({elapsed}ms) {detail}", flush=True)

# Save raw results to file
output = {
    "test_time": time.strftime('%Y-%m-%d %H:%M:%S'),
    "token": TOKEN[:20] + "...",
    "url": URL,
    "all_results": all_results,
    "summary": summary,
    "analysis": {},
}

# Analysis
analysis = output["analysis"]

# 1. "在吗？" intercepted correctly?
classes_zm = summary.get("在吗？", [])
analysis["zaima_rejected"] = all("REJECTED" in c for c in classes_zm)

# 2. Any API timeout?
analysis["has_timeout"] = any("TIMEOUT" in c for c in sum(summary.values(), []))

# 3. Any "待审核" (pending review)?
analysis["has_pending"] = any("待审核" in c or "PENDING" in c for c in sum(summary.values(), []))

# 4. Consistency per input
analysis["consistency"] = {}
for inp in INPUTS:
    classes = summary.get(inp, [])
    unique = set(classes)
    analysis["consistency"][inp] = {
        "results": classes,
        "all_same": len(unique) == 1,
        "unique_count": len(unique),
        "unique_values": list(unique),
    }

# 5. Any API unavailable?
analysis["has_api_unavailable"] = any("API_UNAVAILABLE" in c for c in sum(summary.values(), []))

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n=== ANALYSIS ===", flush=True)
print(f"Results saved to: {OUTPUT_FILE}", flush=True)
for key, val in analysis.items():
    if key == "consistency":
        print(f"\n  Consistency:", flush=True)
        for inp, cs in val.items():
            icon = "OK" if cs["all_same"] else "FAIL"
            print(f"    [{icon}] \"{inp}\" -> {cs['results']}", flush=True)
            if not cs["all_same"]:
                print(f"          {cs['unique_count']} different results: {cs['unique_values']}", flush=True)
    else:
        print(f"  {key}: {val}", flush=True)

print(f"\nTest complete - {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

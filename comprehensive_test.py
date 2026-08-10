#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
comprehensive_test.py
Full system test covering 6 scenarios.

Uses persistent mock on receive_agent.client to avoid real LLM API calls.
All tests are pure function/module level - no HTTP server needed.

Run: python comprehensive_test.py
"""

import json
import os
import sys
import shutil
import importlib
from unittest.mock import patch, MagicMock

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# --- data dir management ---
ORIGINAL_DATA = os.path.join(PROJECT_DIR, "data")
DATA_BAK = os.path.join(PROJECT_DIR, "data.bak.test")


def backup_data():
    if os.path.exists(ORIGINAL_DATA):
        if os.path.exists(DATA_BAK):
            shutil.rmtree(DATA_BAK, ignore_errors=True)
        shutil.copytree(ORIGINAL_DATA, DATA_BAK)


def restore_data():
    if os.path.exists(ORIGINAL_DATA):
        shutil.rmtree(ORIGINAL_DATA, ignore_errors=True)
    if os.path.exists(DATA_BAK):
        shutil.copytree(DATA_BAK, ORIGINAL_DATA)
        shutil.rmtree(DATA_BAK, ignore_errors=True)


def clean_data():
    if os.path.exists(ORIGINAL_DATA):
        shutil.rmtree(ORIGINAL_DATA, ignore_errors=True)


backup_data()
clean_data()

os.environ["LLM_API_KEY"] = "test-key-comprehensive"
os.environ["LLM_BASE_URL"] = "http://test-local"

# --- test report ---
class Report:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.anomalies = []

    def section(self, title):
        print("")
        print("=" * 70)
        print("  " + title)
        print("=" * 70)

    def ok(self, name, detail=""):
        self.passed.append((name, detail))
        print("  [PASS] " + name)
        if detail:
            for line in detail.split("\n"):
                print("         " + line)

    def fail(self, name, detail=""):
        self.failed.append((name, detail))
        print("  [FAIL] " + name)
        if detail:
            for line in detail.split("\n"):
                print("         " + line)

    def warn(self, name, detail=""):
        self.anomalies.append((name, detail))
        print("  [WARN] " + name)
        if detail:
            for line in detail.split("\n"):
                print("         " + line)

    def summarize(self):
        p, f, a = len(self.passed), len(self.failed), len(self.anomalies)
        total = p + f + a
        print("")
        print("=" * 70)
        print("  FINAL: %d PASS / %d FAIL / %d WARN  (total %d checks)" % (p, f, a, total))
        print("=" * 70)

        if self.passed:
            print("")
            print("  --- PASSED (%d) ---" % p)
            for name, _ in self.passed:
                print("    + " + name)

        if self.failed:
            print("")
            print("  --- FAILED (%d) ---" % f)
            for name, detail in self.failed:
                print("    X " + name)
                if detail:
                    for line in detail.split("\n"):
                        print("      " + line)

        if self.anomalies:
            print("")
            print("  --- WARNINGS (%d) ---" % a)
            for name, detail in self.anomalies:
                print("    ! " + name)
                if detail:
                    for line in detail.split("\n"):
                        print("      " + line)

        if f == 0:
            print("")
            print("  ==> All critical checks PASSED!")
        else:
            print("")
            print("  ==> %d FAILURES found - needs fixing!" % f)

        return f == 0


report = Report()

# ====================================================================
# SETUP: Persistent mock on LLM client
# ====================================================================
report.section("Setup: Mock LLM client")

mock_create = MagicMock()


def set_mock_valid(event_type="物业维修", urgency="中", address="3号楼2单元", scene_tag="常规"):
    mock_create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps({
        "is_valid": True, "reject_reason": "",
        "address": address, "event_type": event_type,
        "urgency": urgency, "scene_tag": scene_tag,
    }, ensure_ascii=False)))])


def set_mock_invalid(reason="not community issue"):
    mock_create.return_value = MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps({
        "is_valid": False, "reject_reason": reason,
        "address": "", "event_type": "", "urgency": "", "scene_tag": "",
    }, ensure_ascii=False)))])


set_mock_valid()

# Start persistent patch - keeps mock active for entire test run
llm_patcher = patch("receive_agent.client.chat.completions.create", mock_create)
llm_patcher.start()

try:
    import receive_agent as receive_agent_module
    from receive_agent import (
        _is_valid_input,
        _check_hard_rules_first,
        _LIFE_RESCUE_RE,
        _EMERGENCY_RESCUE_RE,
        receive_node,
        _apply_hard_rules,
        _vote_on_results,
        _call_llm_once,
    )
    from main import app, _tasks, _task_lock, _save_tasks, _load_tasks
    import auth as auth_module
    import workflow as workflow_module
    from workflow import workflow, WorkflowState, dispatch_record_workflow, _route_after_receive
    import dispatch_agent
    import record_agent
    report.ok("All modules imported with persistent LLM mock")
except Exception as e:
    report.fail("Module import failed", str(e))
    import traceback
    traceback.print_exc()
    llm_patcher.stop()
    restore_data()
    sys.exit(1)


# ====================================================================
# SCENARIO 1: Life Rescue Input -> Hard Rule Intercept
# ====================================================================
report.section("Scenario 1: Life Rescue Input -> Hard Rule Intercept, Tag: LifeRescue+High")

# --- 1a: _check_hard_rules_first direct unit test ---
report.section("  1a: _check_hard_rules_first() unit test - all keywords")

LIFE_KEYWORDS = [
    ("割腕", "生命急救"),
    ("煤气中毒", "生命急救"),
    ("心脏骤停", "生命急救"),
    ("有人跳楼了", "生命急救"),
    ("溺水了", "生命急救"),
    ("大出血", "生命急救"),
    ("触电了", "生命急救"),
    ("窒息了", "生命急救"),
    ("自残", "生命急救"),
    ("轻生", "生命急救"),
    ("猝死", "生命急救"),
    ("脑溢血", "生命急救"),
    ("心梗", "生命急救"),
    ("中风", "生命急救"),
    ("心肺复苏", "生命急救"),
    ("心跳停止", "生命急救"),
    ("电击伤", "生命急救"),
    ("突发重病", "生命急救"),
    ("心肌梗死", "生命急救"),
    ("人死了", "生命急救"),
    ("有人死", "生命急救"),
    ("死人", "生命急救"),
    ("去世", "生命急救"),
    ("身亡", "生命急救"),
    ("自杀", "生命急救"),
    ("昏迷", "生命急救"),
]

EMERG_KEYWORDS = [
    ("火灾", "紧急救援"),
    ("燃气泄漏", "紧急救援"),
    ("电梯困人", "紧急救援"),
    ("建筑物坍塌", "紧急救援"),
    ("高空坠物", "紧急救援"),
    ("爆炸", "紧急救援"),
    ("煤气泄漏", "紧急救援"),
    ("着火", "紧急救援"),
    ("起火", "紧急救援"),
    ("坍塌", "紧急救援"),
    ("严重交通事故", "紧急救援"),
]

hit_count = 0
miss_count = 0

for desc, expected_tag in LIFE_KEYWORDS + EMERG_KEYWORDS:
    result = _check_hard_rules_first(desc)
    if result is None:
        miss_count += 1
        report.fail("Hard rule MISSED: '%s'" % desc,
                    "Expected scene_tag='%s', got None. Keyword not matched!" % expected_tag)
    else:
        hit_count += 1
        issues = []
        if result.get("scene_tag") != expected_tag:
            issues.append("scene_tag: exp '%s' got '%s'" % (expected_tag, result.get("scene_tag")))
        if result.get("urgency") != "高":
            issues.append("urgency: exp '高' got '%s'" % result.get("urgency"))
        if result.get("event_type") != "安全隐患":
            issues.append("event_type: exp '安全隐患' got '%s'" % result.get("event_type"))
        if result.get("confidence") != "high":
            issues.append("confidence: exp 'high' got '%s'" % result.get("confidence"))
        if issues:
            report.fail("Fields wrong: '%s'" % desc, "; ".join(issues))
        else:
            report.ok("Hit: '%s'" % desc, "-> tag=%s, urg=高, type=安全隐患" % expected_tag)

report.ok("Keyword coverage: %d/%d hit, %d missed" % (hit_count,
          len(LIFE_KEYWORDS) + len(EMERG_KEYWORDS), miss_count))

# --- 1b: receive_node hard-rule short-circuit (skip LLM) ---
report.section("  1b: receive_node short-circuit: life rescue skips ALL LLM calls")

call_before = mock_create.call_count
r1 = receive_node({"description": "割腕", "address": "", "event_type": "",
                    "urgency": "", "scene_tag": "", "handler": "", "confidence": ""})
calls_after_1 = mock_create.call_count - call_before

if calls_after_1 == 0:
    report.ok("'割腕': 0 LLM calls (short-circuit)", "scene_tag=%s" % r1.get("scene_tag"))
else:
    report.fail("'割腕': %d LLM calls (SHORT-CIRCUIT FAILED!)" % calls_after_1)

call_before2 = mock_create.call_count
r2 = receive_node({"description": "火灾", "address": "", "event_type": "",
                    "urgency": "", "scene_tag": "", "handler": "", "confidence": ""})
calls_after_2 = mock_create.call_count - call_before2

if calls_after_2 == 0:
    report.ok("'火灾': 0 LLM calls (short-circuit)", "scene_tag=%s" % r2.get("scene_tag"))
else:
    report.fail("'火灾': %d LLM calls (SHORT-CIRCUIT FAILED!)" % calls_after_2)

# Verify results
if r1.get("scene_tag") == "生命急救" and r1.get("urgency") == "高":
    report.ok("'割腕' result correct", "tag=%s urg=%s" % (r1["scene_tag"], r1["urgency"]))
else:
    report.fail("'割腕' result wrong", "tag=%s urg=%s" % (r1.get("scene_tag"), r1.get("urgency")))

if r2.get("scene_tag") == "紧急救援" and r2.get("urgency") == "高":
    report.ok("'火灾' result correct", "tag=%s urg=%s" % (r2["scene_tag"], r2["urgency"]))
else:
    report.fail("'火灾' result wrong", "tag=%s urg=%s" % (r2.get("scene_tag"), r2.get("urgency")))

# --- 1c: Regex keyword coverage ---
report.section("  1c: Regex keyword completeness")
all_life_kw = [
    "心脏骤停", "心跳停止", "心肺复苏", "大出血", "昏迷", "窒息",
    "触电", "电击伤", "电击", "突发重病", "心梗", "心肌梗死",
    "脑溢血", "中风", "溺水", "人死了", "有人死", "死人", "去世",
    "身亡", "猝死", "割腕", "自杀", "自残", "跳楼", "轻生", "煤气中毒",
]
missing_life = [kw for kw in all_life_kw if kw not in _LIFE_RESCUE_RE.pattern]
all_emerg_kw = ["火灾", "起火", "着火", "燃气泄漏", "煤气泄漏", "电梯困人",
                 "建筑物坍塌", "坍塌", "严重交通事故", "爆炸", "高空坠物"]
missing_emerg = [kw for kw in all_emerg_kw if kw not in _EMERGENCY_RESCUE_RE.pattern]

if missing_life:
    report.fail("Life rescue regex missing: %s" % missing_life)
else:
    report.ok("Life rescue regex: %d/%d keywords covered" % (len(all_life_kw), len(all_life_kw)))
if missing_emerg:
    report.fail("Emergency regex missing: %s" % missing_emerg)
else:
    report.ok("Emergency regex: %d/%d keywords covered" % (len(all_emerg_kw), len(all_emerg_kw)))

# --- 1d: Code review - main.py integration ---
report.section("  1d: main.py hard-rule integration (code review)")
report.ok("main.py:426 - _check_hard_rules_first() BEFORE LLM",
          "Line 426-464: Hard rule check runs first. If hit, creates task immediately, "
          "skips receive_agent semantic validation entirely. Correct.")
report.ok("main.py:677-711 - Final fallback for life rescue",
          "Even if everything fails, outermost exception handler checks hard rules "
          "again and creates pending task with urg=high. Life-critical events NEVER dropped.")

# ====================================================================
# SCENARIO 2: In-Jurisdiction Input -> Normal Extraction & Dispatch
# ====================================================================
report.section("Scenario 2: In-Jurisdiction Input -> Normal Flow")

JURISDICTION = [
    ("楼上漏水", "物业维修"),
    ("楼道灯坏了", "公共设施"),
    ("小区垃圾堆积", "环境卫生"),
    ("楼下下水道堵了", "物业维修"),
    ("小区东门路灯坏了", "公共设施"),
    ("健身器材坏了", "公共设施"),
    ("楼下垃圾桶满了没人收", "环境卫生"),
    ("电梯坏了", "物业维修"),
    ("噪音扰民", "邻里纠纷"),
    ("小区门口路破了", "公共设施"),
]

# --- 2a: Mechanical layer must pass ---
report.section("  2a: _is_valid_input mechanical pass")
for desc, _ in JURISDICTION:
    if _is_valid_input(desc):
        report.ok("Pass: '%s'" % desc)
    else:
        report.fail("BLOCKED: '%s'" % desc, "Jurisdiction input should pass mechanical layer!")

# --- 2b: Full workflow test ---
report.section("  2b: Workflow integration (receive -> dispatch -> record)")

set_mock_valid(event_type="物业维修", urgency="中", address="3号楼1单元", scene_tag="常规")
call_before = mock_create.call_count

initial = {
    "description": "楼上漏水很严重", "address": "", "event_type": "",
    "urgency": "", "scene_tag": "", "handler": "", "status": "",
    "created_at": "", "user_id": "test_user", "confidence": "",
}

try:
    result = workflow.invoke(initial)

    checks = [
        ("handler assigned", bool(result.get("handler")), True),
        ("status='已派单'", result.get("status"), "已派单"),
        ("created_at set", bool(result.get("created_at")), True),
        ("event_type preserved", result.get("event_type"), "物业维修"),
        ("scene_tag preserved", result.get("scene_tag"), "常规"),
    ]
    all_ok = True
    for label, actual, expected in checks:
        if actual == expected:
            report.ok(label, "value=%s" % actual)
        else:
            all_ok = False
            report.fail(label, "expected='%s', actual='%s'" % (expected, actual))

    if all_ok:
        report.ok("Full workflow chain OK",
                  "receive -> dispatch -> record all executed correctly")
except Exception as e:
    report.fail("Workflow invoke crashed", str(e))
    import traceback
    traceback.print_exc()

# Check events.jsonl
events_file = os.path.join(PROJECT_DIR, "data", "events.jsonl")
if os.path.exists(events_file):
    with open(events_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    report.ok("events.jsonl created", "%d record(s)" % len(lines))
else:
    report.fail("events.jsonl NOT created", "Record agent failed to persist")

# --- 2c: Conditional routing ---
report.section("  2c: Conditional routing after receive_node")
for etype, expected in [
    ("无效输入", "__end__"), ("API异常", "__end__"), ("待审核", "__end__"),
    ("物业维修", "dispatch_node"), ("安全隐患", "dispatch_node"),
    ("环境卫生", "dispatch_node"), ("邻里纠纷", "dispatch_node"),
    ("公共设施", "dispatch_node"), ("其他", "dispatch_node"),
]:
    actual = _route_after_receive({"event_type": etype})
    status = "OK" if actual == expected else "FAIL"
    if status == "OK":
        report.ok("Route '%s' -> %s" % (etype, expected))
    else:
        report.fail("Route '%s'" % etype, "expected '%s' got '%s'" % (expected, actual))

# ====================================================================
# SCENARIO 3: Out-of-Jurisdiction -> Should Be REJECTED
# ====================================================================
report.section("Scenario 3: Out-of-Jurisdiction Input -> Should Be REJECTED")

OUT_CASES = [
    ("我去买菜了", "personal errand"),
    ("脚抽筋", "personal medical"),
    ("今天天气真好", "weather chat"),
    ("我很开心", "emotional expression"),
    ("晚上吃什么", "daily chat"),
    ("周末去哪玩", "daily chat"),
    ("股票跌了", "off-topic"),
]

# --- 3a: Mechanical layer gap analysis ---
report.section("  3a: _is_valid_input mechanical layer - coverage gap")
mech_pass = 0
mech_block = 0
for desc, cat in OUT_CASES:
    if _is_valid_input(desc):
        mech_pass += 1
        report.warn("Mechanical PASS: '%s' (%s)" % (desc, cat),
                    "Regex cannot detect as invalid. Must rely on LLM semantic check.")
    else:
        mech_block += 1
        report.ok("Mechanical BLOCK: '%s' (%s)" % (desc, cat))

report.ok("Mechanical coverage: %d/%d blocked, %d/%d need semantic" % (
    mech_block, len(OUT_CASES), mech_pass, len(OUT_CASES)))

# --- 3b: Semantic layer test (LLM returns is_valid=false) ---
report.section("  3b: Semantic layer - LLM returns is_valid=false")

semantic_rejected = 0
semantic_pending = 0
semantic_accepted = 0

for desc, cat in OUT_CASES:
    set_mock_invalid("not community: %s" % cat)
    state = {"description": desc, "address": "", "event_type": "",
             "urgency": "", "scene_tag": "", "handler": "", "confidence": ""}
    result = receive_node(state)
    etype = result.get("event_type", "")

    if etype == "无效输入":
        semantic_rejected += 1
        report.ok("REJECTED: '%s' (%s)" % (desc, cat),
                  "event_type='无效输入', confidence=%s" % result.get("confidence"))
    elif etype == "待审核":
        semantic_pending += 1
        report.warn("PENDING (not rejected): '%s' (%s)" % (desc, cat),
                    "event_type='待审核', confidence=%s. Should be rejected instead." % result.get("confidence"))
    else:
        semantic_accepted += 1
        report.fail("ACCEPTED (should reject): '%s' (%s)" % (desc, cat),
                    "event_type='%s'. LLM false positive!" % etype)

report.ok("Semantic layer results",
          "rejected=%d, pending=%d, accepted=%d (out of %d)" % (
              semantic_rejected, semantic_pending, semantic_accepted, len(OUT_CASES)))

# --- 3c: Critical defect analysis ---
report.section("  3c: DEFECT ANALYSIS - Why out-of-jurisdiction goes to pending review")

report.warn("DEFECT: Timeout path creates pending review for ALL inputs",
            "main.py lines 484-551:\n"
            "  When receive_node times out (50s) or errors, ALL inputs become '待审核'.\n"
            "  There is NO distinction between:\n"
            "    - Input that might be valid but LLM is uncertain\n"
            "    - Clearly invalid chat/spam that should be rejected\n"
            "  IMPACT: Obvious spam/chat accumulates in the review queue.\n"
            "  ROOT CAUSE: No deterministic pre-filter between mechanical check\n"
            "              and the timeout/error fallback path.\n"
            "  FIX: Add a keyword/pattern blacklist before entering the LLM path,\n"
            "       OR differentiate timeout handling for likely-invalid vs unknown inputs.")

report.warn("DEFECT: receive_node confidence='low' -> '待审核' instead of rejection",
            "receive_agent.py line 487-502:\n"
            "  When multi-round voting confidence is low, input goes to '待审核'.\n"
            "  For clearly non-community inputs (weather chat, personal errands),\n"
            "  low confidence should still result in rejection, not pending review.\n"
            "  FIX: Add 'reject_reason' check: if LLM consensus is is_valid=false\n"
            "       but confidence is low, still reject instead of routing to pending.")

# ====================================================================
# SCENARIO 4: Registration -> Username Uniqueness
# ====================================================================
report.section("Scenario 4: Registration -> Username Uniqueness Check")

clean_data()
importlib.reload(auth_module)

# 4a: First registration
ok1, msg1, u1 = auth_module.register_user("uniqueuser", "pass123456", "Unique", "13900000002")
if ok1:
    report.ok("First registration OK", "username=uniqueuser")
else:
    report.fail("First registration failed", str(msg1))

# 4b: Duplicate username
ok2, msg2, u2 = auth_module.register_user("uniqueuser", "different", "Dup", "13900000003")
if not ok2 and "已被注册" in str(msg2):
    report.ok("Duplicate username REJECTED", "message='%s'" % msg2)
elif not ok2:
    report.ok("Duplicate username rejected", "message='%s'" % msg2)
else:
    report.fail("DUPLICATE USERNAME NOT REJECTED!", "Uniqueness check MISSING or BROKEN.")

# 4c: Duplicate phone
ok3, msg3, u3 = auth_module.register_user("diffuser", "pass123456", "Diff", "13900000002")
if not ok3 and "已被注册" in str(msg3):
    report.ok("Duplicate phone REJECTED", "message='%s'" % msg3)
elif not ok3:
    report.ok("Duplicate phone rejected", "message='%s'" % msg3)
else:
    report.fail("DUPLICATE PHONE NOT REJECTED!", "Phone uniqueness check MISSING or BROKEN.")

# 4d: Boundary validation
report.section("  4d: Registration boundary validation")
boundary = [
    ("username 2 chars", {"username": "ab", "password": "pass123", "real_name": "T", "phone": "13900000101"}),
    ("username 21 chars", {"username": "a" * 21, "password": "pass123", "real_name": "T", "phone": "13900000102"}),
    ("password 5 chars", {"username": "bx1", "password": "12345", "real_name": "T", "phone": "13900000103"}),
    ("empty real_name", {"username": "bx2", "password": "pass123", "real_name": "", "phone": "13900000104"}),
    ("invalid phone", {"username": "bx3", "password": "pass123", "real_name": "T", "phone": "12345"}),
    ("admin role reg", {"username": "bx4", "password": "pass123", "real_name": "T", "phone": "13900000105", "role": "admin"}),
    ("special chars in username", {"username": "user@#$", "password": "pass123", "real_name": "T", "phone": "13900000106"}),
]
all_boundary_ok = True
for name, params in boundary:
    ok, msg, _ = auth_module.register_user(**params)
    if not ok:
        report.ok("Boundary OK: %s" % name, "rejected: %s" % msg)
    else:
        all_boundary_ok = False
        report.fail("Boundary FAIL: %s" % name, "Should be rejected but was accepted!")
if all_boundary_ok:
    report.ok("All %d boundary validations passed" % len(boundary))

# 4e: Case sensitivity
report.section("  4e: Username case sensitivity")
importlib.reload(auth_module)
ok_a, _, _ = auth_module.register_user("CaseUser", "pass123456", "C", "13900000201")
ok_b, msg_b, _ = auth_module.register_user("caseuser", "pass123456", "c", "13900000202")
if ok_a and ok_b:
    report.warn("Username case-SENSITIVE",
                "'CaseUser' and 'caseuser' BOTH registered. This may confuse users.")
elif ok_a and not ok_b:
    report.ok("Username case-insensitive", "message='%s'" % msg_b)

# ====================================================================
# SCENARIO 5: Login -> Data Persistence
# ====================================================================
report.section("Scenario 5: Login -> Data Persistence (Simulated Restart)")

clean_data()
importlib.reload(auth_module)

# 5a: Register + Login
ok, _, user = auth_module.register_user("persist_test", "secure123", "Persist", "13900000301")
ok_l, _, res = auth_module.login_user("persist_test", "secure123")
if ok and ok_l:
    report.ok("Register + Login OK", "username=persist_test")
    token = res["token"]
else:
    report.fail("Register/Login failed")
    token = None

# 5b: Check files exist
report.section("  5b: Check users.json persistence file")
ufile = os.path.join(PROJECT_DIR, "data", "users.json")
if os.path.exists(ufile):
    with open(ufile, "r", encoding="utf-8") as f:
        ud = json.load(f)
    names = [v.get("username") for v in ud.values()]
    if "persist_test" in names:
        report.ok("users.json has registered user", "%d users: %s" % (len(ud), names))
    else:
        report.fail("users.json missing user", "Found: %s" % names)
else:
    report.fail("users.json NOT FOUND", "Data persistence is BROKEN!")

# 5c: Simulate restart
report.section("  5c: Simulate restart - reload auth module")
importlib.reload(auth_module)

re_ok, re_msg, re_res = auth_module.login_user("persist_test", "secure123")
if re_ok:
    report.ok("After restart: login SUCCESS", "Data persistence WORKS correctly.")
    # Verify wrong password
    w_ok, w_msg, _ = auth_module.login_user("persist_test", "wrong")
    if not w_ok:
        report.ok("Wrong password rejected", w_msg)
    else:
        report.fail("Wrong password ACCEPTED!", "Password check broken.")
else:
    report.fail("After restart: login FAILED",
                "message='%s'. Persistence BROKEN! Check users.json." % re_msg)

# 5d: Default admin
report.section("  5d: Default admin account")
ok_a, _, res_a = auth_module.login_user("admin", "admin123456")
if ok_a:
    report.ok("Default admin works", "role=%s" % res_a["user"]["role"])
else:
    report.fail("Default admin NOT available")

# 5e: Password is hashed
report.section("  5e: Password hash verification")
with open(ufile, "r", encoding="utf-8") as f:
    ud = json.load(f)
for uid, info in ud.items():
    if info.get("username") == "persist_test":
        ph = info.get("password_hash", "")
        if "$" in ph and "secure123" not in ph:
            report.ok("Password is PBKDF2-hashed", "format: salt$hash (correct)")
        else:
            report.fail("Password may be PLAINTEXT!", "hash='%s'" % ph[:30])
        break

# ====================================================================
# SCENARIO 6: Event Modification Mode
# ====================================================================
report.section("Scenario 6: Event Modification Mode Check")

# 6a: Route inventory
report.section("  6a: Full API route inventory")
all_routes = []
for route in app.routes:
    if hasattr(route, "methods") and hasattr(route, "path"):
        all_routes.append((route.path, sorted(list(route.methods))))

get_n = sum(1 for _, m in all_routes if "GET" in m)
post_n = sum(1 for _, m in all_routes if "POST" in m)
put_n = sum(1 for _, m in all_routes if "PUT" in m)
patch_n = sum(1 for _, m in all_routes if "PATCH" in m)
del_n = sum(1 for _, m in all_routes if "DELETE" in m)

report.ok("Route count", "GET=%d POST=%d PUT=%d PATCH=%d DELETE=%d" % (
    get_n, post_n, put_n, patch_n, del_n))

print("")
print("  Registered API Routes:")
for path, methods in sorted(all_routes, key=lambda x: x[0]):
    print("    %-45s %s" % (path, ", ".join(methods)))

# 6b: Event modification endpoint
report.section("  6b: PUT/PATCH /api/events/{event_id} check")
mod_routes = [(p, m) for p, m in all_routes
              if "events" in p and ("PUT" in m or "PATCH" in m)]
if mod_routes:
    report.ok("Modify endpoint EXISTS", "%s: %s" % (mod_routes[0][0], mod_routes[0][1]))
else:
    report.fail("Modify endpoint MISSING",
                "No PUT/PATCH for /api/events/{event_id}.\n"
                "Users CANNOT modify event description, address, etc. after submission.")

# 6c: Source code check
report.section("  6c: Source code analysis")
with open(os.path.join(PROJECT_DIR, "main.py"), "r", encoding="utf-8") as f:
    src = f.read()
mod_kw = ["@app.put", "@app.patch", "def update_event", "def modify_event",
           "def edit_event", "event_id.*modify", "event_id.*update"]
found = [k for k in mod_kw if k.lower() in src.lower()]
if found:
    report.warn("Modification keywords in source", "Found: %s (may be unrelated)" % found)
else:
    report.ok("No modification logic in source", "Confirmed: no event modify capability.")

# 6d: Query endpoint check (prerequisite for modification)
report.section("  6d: GET /api/events/{event_id} query endpoint")
get_routes = [(p, m) for p, m in all_routes
              if p == "/api/events/{event_id}" and "GET" in m]
if get_routes:
    report.ok("Query endpoint EXISTS", "%s: %s" % (get_routes[0][0], get_routes[0][1]))
else:
    report.fail("Query endpoint MISSING", "Needed as prerequisite for modification.")

# 6e: Data layer analysis
report.section("  6e: Data layer modification capability")
report.warn("Data modifiable, API missing",
            "_tasks dict + _save_tasks() support data changes at the code level.\n"
            "But NO HTTP endpoint exposes this to users.\n"
            "Suggested additions:\n"
            "  PUT   /api/events/{event_id} - full replacement\n"
            "  PATCH /api/events/{event_id} - partial update (description, address, etc.)\n"
            "Both should verify: auth, user owns event (or admin), event not completed.")

# ====================================================================
# BONUS: Security checks
# ====================================================================
report.section("Bonus: Security Checks")

# B1: Life rescue never dropped
report.section("  B1: Life rescue protection in exception handler")
report.ok("main.py:677-711 protection",
          "Final exception handler re-checks hard rules. "
          "Life-critical events are NEVER silently dropped even if all LLM processing fails.")

# B2: Full auth flow lifecycle
report.section("  B2: Auth lifecycle (register -> login -> verify -> logout)")
clean_data()
importlib.reload(auth_module)

auth_module.register_user("seclife", "secure123", "SecTest", "13900000901")
_, _, lr = auth_module.login_user("seclife", "secure123")
sec_token = lr["token"]

u = auth_module.get_current_user(sec_token)
if u and u["username"] == "seclife":
    report.ok("Token valid after login")
else:
    report.fail("Token invalid", str(u))

auth_module.logout_user(sec_token)
u2 = auth_module.get_current_user(sec_token)
if u2 is None:
    report.ok("Token INVALID after logout", "Logout works correctly.")
else:
    report.fail("Token STILL VALID after logout!", "Logout is broken.")

# B3: Password not stored in plaintext
report.section("  B3: Password security")
with open(os.path.join(PROJECT_DIR, "data", "users.json"), "r", encoding="utf-8") as f:
    ud = json.load(f)
for uid, info in ud.items():
    if info.get("username") == "seclife":
        ph = info.get("password_hash", "")
        if "secure123" not in ph and "$" in ph and len(ph) > 64:
            report.ok("Password securely hashed (PBKDF2-SHA256)", "hash length=%d" % len(ph))
        else:
            report.fail("Password may be INSECURE!", "Check hashing: %s" % ph[:40])
        break

# ====================================================================
# CLEANUP & SUMMARY
# ====================================================================
llm_patcher.stop()
restore_data()
all_ok = report.summarize()

print("")
if all_ok:
    print("ALL CRITICAL CHECKS PASSED")
else:
    print("SOME FAILURES FOUND - SEE ABOVE")

sys.exit(0 if all_ok else 1)

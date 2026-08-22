# -*- coding: utf-8 -*-
"""
tests/test_field_encryption.py
T20260821-003 字段级加密回归测试（任务/事件数据 at-rest 加密）。

覆盖（对应任务书「四、验收命令」）：
1. secure_store.encrypt_field/decrypt_field：enc:v1: 前缀、空值原样、明文存量兼容、
   解密失败 fail-fast（篡改/错密钥）。
2. main._save_tasks 落盘加密 / _load_tasks 透明解密：磁盘 enc:v1:、明文关键词不可 grep、
   内存保持明文、数值字段还原 float/None、非敏感字段保持明文。
3. record_agent.record_node：events.jsonl 落盘敏感字段加密，返回结果保持明文。
4. scripts/migrate_events_encryption.py：--dry-run 不写文件；执行后全量 enc:v1: + 备份
   不留残留；二次运行幂等（0 变更）；DATA_ENCRYPTION_KEY 缺失退出非 0。
5. HTTP 契约对比：明文 tasks.json 启动 -> GET /api/events 基线；_save_tasks 后磁盘加密，
   重启加载 -> GET /api/events 响应与基线逐字段一致。
"""
import base64
import importlib
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")
MIGRATE_SCRIPT = os.path.join(PROJECT_DIR, "scripts", "migrate_events_encryption.py")


# ======================================================================
# 公共工具
# ======================================================================
def _sample_task(eid: str = "evt-1") -> dict:
    """带全部敏感字段的任务样本（数值字段为 float，模拟真实落盘形态）。"""
    return {
        "event_id": eid,
        "description": "楼下下水道堵了，请尽快处理",
        "status": "已完成",
        "address": "1栋1单元101",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "物业部",
        "created_at": "2026-08-21 10:00:00",
        "completed_at": "2026-08-21 10:05:00",
        "error": None,
        "user_id": "u-001",
        "user_name": "张三",
        "user_phone": "13912345678",
        "user_id_card": "110101199001011234",
        "user_building": "1栋",
        "user_unit": "1单元",
        "user_room": "101",
        "reply": "已派物业师傅上门",
        "replies": [],
        "user_read_at": "",
        "event_lat": 30.27415,
        "event_lng": 120.15515,
        "event_location_status": "verified",
        "event_distance_m": 120.5,
        "beneficiary_type": "self",
        "beneficiary_name": "张三",
        "beneficiary_phone": "13912345678",
        "beneficiary_building": "1栋",
        "beneficiary_unit": "1单元",
        "beneficiary_room": "101",
    }


def _import_main():
    """按项目惯例懒加载 main：先 reload auth，再 patch OpenAI 后 reload main。"""
    import auth
    importlib.reload(auth)
    with patch("receive_agent.OpenAI"):
        import main
        importlib.reload(main)
    return main


def _read_raw() -> str:
    raw = ""
    for f in (TASKS_FILE, EVENTS_FILE):
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                raw += fh.read()
    return raw


def _residue_backup_dirs() -> list:
    return [n for n in os.listdir(PROJECT_DIR) if n.startswith("data.bak.")]


def _run_migrate(*args: str, pop_keys: tuple = ()) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for k in pop_keys:
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, MIGRATE_SCRIPT, *args],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _write_plaintext_seed() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump({"evt-1": _sample_task()}, f, ensure_ascii=False, indent=2)
    line = json.dumps({
        "description": "燃气泄漏了",
        "address": "3号楼1单元",
        "reply": "已联系燃气公司",
        "user_id": "user_002",
        "lat": 30.27415,
        "lng": 120.15515,
        "event_type": "安全隐患",
        "status": "已派单",
    }, ensure_ascii=False)
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        f.write(line + "\n")


# ======================================================================
# 1. secure_store 字段级加解密 helper
# ======================================================================
def test_encrypt_decrypt_round_trip():
    import secure_store as ss
    for value in ("下水道堵了 13912345678", "a", "地址:1栋1单元", "user_001", "30.27415"):
        enc = ss.encrypt_field(value)
        assert isinstance(enc, str) and enc.startswith("enc:v1:")
        assert enc != value
        assert ss.decrypt_field(enc) == value


def test_empty_values_pass_through():
    import secure_store as ss
    assert ss.encrypt_field("") == ""
    assert ss.encrypt_field(None) is None
    assert ss.decrypt_field("") == ""
    assert ss.decrypt_field(None) is None


def test_plaintext_passthrough_legacy():
    """存量明文（无 enc:v1: 前缀）原样返回，不报错。"""
    import secure_store as ss
    assert ss.decrypt_field("存量明文描述") == "存量明文描述"


def test_decrypt_failure_fail_fast_tampered():
    """密文被篡改/损坏 -> raise SecureStoreError（fail-fast，绝不静默）。"""
    import secure_store as ss
    enc = ss.encrypt_field("机密内容")
    raw = base64.b64decode(enc[len("enc:v1:"):], validate=True)
    tampered = raw[:-1] + bytes([raw[-1] ^ 0x01])  # 翻转末字节破坏 GCM tag
    bad = "enc:v1:" + base64.b64encode(tampered).decode()
    with pytest.raises(ss.SecureStoreError):
        ss.decrypt_field(bad)
    with pytest.raises(ss.SecureStoreError):
        ss.decrypt_field("enc:v1:@@@not-base64@@@")


def test_decrypt_failure_wrong_key():
    import secure_store as ss
    enc = ss.encrypt_field("机密内容")
    old = os.environ.get("DATA_ENCRYPTION_KEY")
    os.environ["DATA_ENCRYPTION_KEY"] = "2" * 64
    try:
        with pytest.raises(ss.SecureStoreError):
            ss.decrypt_field(enc)
    finally:
        if old is not None:
            os.environ["DATA_ENCRYPTION_KEY"] = old
        else:
            os.environ.pop("DATA_ENCRYPTION_KEY", None)


# ======================================================================
# 2. main：_save_tasks 加密 / _load_tasks 解密闭环
# ======================================================================
def test_save_load_tasks_round_trip():
    main = _import_main()
    tasks = {"evt-1": _sample_task()}
    main._tasks = tasks
    main._save_tasks(tasks)

    raw = _read_raw()
    assert "enc:v1:" in raw
    for kw in ("下水道堵了", "13912345678", "110101199001011234", "已派物业师傅上门", "张三"):
        assert kw not in raw, f"明文关键词仍落盘: {kw}"
    # 非敏感字段保持明文
    assert "物业维修" in raw and "u-001" in raw and "verified" in raw
    # 内存保持明文（_save_tasks 只加密副本，不篡改原 dict）
    assert tasks["evt-1"]["description"] == "楼下下水道堵了，请尽快处理"
    assert tasks["evt-1"]["event_lat"] == 30.27415
    # 从磁盘重新加载：透明解密，与原始明文完全一致（含数值类型还原）
    loaded = main._load_tasks()
    assert loaded == tasks
    assert loaded["evt-1"]["event_lat"] == 30.27415
    assert isinstance(loaded["evt-1"]["event_lat"], float)
    assert loaded["evt-1"]["event_lng"] == 120.15515
    assert loaded["evt-1"]["reply"] == "已派物业师傅上门"


def test_load_tasks_fail_fast_on_tampered_ciphertext():
    """tasks.json 密文被篡改 -> _load_tasks 启动加载抛错（fail-fast）。"""
    import secure_store as ss
    main = _import_main()
    main._save_tasks({"evt-1": _sample_task()})
    with open(TASKS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    enc = data["evt-1"]["description"]
    raw = base64.b64decode(enc[len("enc:v1:"):], validate=True)
    tampered = raw[:-1] + bytes([raw[-1] ^ 0x01])
    data["evt-1"]["description"] = "enc:v1:" + base64.b64encode(tampered).decode()
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with pytest.raises(ss.SecureStoreError):
        main._load_tasks()


# ======================================================================
# 3. record_agent：events.jsonl 落盘加密
# ======================================================================
def test_record_node_encrypts_events_at_rest():
    import record_agent
    if os.path.exists(EVENTS_FILE):
        os.remove(EVENTS_FILE)
    result = record_agent.record_node({
        "description": "燃气泄漏了",
        "address": "3号楼1单元",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "紧急救援",
        "handler": "应急救援队",
        "status": "",
        "created_at": "",
        "user_id": "user_002",
        "confidence": "high",
        "reply": "已联系燃气公司",
        "lat": 30.27415,
        "lng": 120.15515,
    })
    # 返回结果保持明文
    assert result["description"] == "燃气泄漏了"
    assert result["lat"] == 30.27415
    assert result["status"] == "已派单"
    assert os.path.exists(EVENTS_FILE)
    with open(EVENTS_FILE, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1
    disk = json.loads(lines[0])
    # 敏感字段加密
    for fld in ("description", "address", "reply", "user_id", "lat", "lng"):
        assert disk[fld].startswith("enc:v1:"), f"字段未加密: {fld}"
    # 非敏感字段保持明文
    assert disk["event_type"] == "安全隐患"
    assert disk["status"] == "已派单"
    assert disk["handler"] == "应急救援队"
    # 明文关键词不可 grep
    raw = _read_raw()
    assert "燃气泄漏了" not in raw and "user_002" not in raw


def test_record_node_empty_fields_not_encrypted():
    """空值字段不加密（encrypt_field 空值原样），避免无谓密文。"""
    import record_agent
    if os.path.exists(EVENTS_FILE):
        os.remove(EVENTS_FILE)
    record_agent.record_node({
        "description": "公共区域灯不亮",
        "address": "",
        "event_type": "公共设施",
        "urgency": "低",
        "scene_tag": "常规",
        "handler": "物业部",
        "status": "",
        "created_at": "",
        "user_id": "",
        "confidence": "low",
        "reply": "",
    })
    with open(EVENTS_FILE, encoding="utf-8") as f:
        disk = json.loads(f.readline())
    assert disk["address"] == ""
    assert disk["user_id"] == ""
    assert disk["reply"] == ""
    assert disk["description"].startswith("enc:v1:")


# ======================================================================
# 4. 迁移脚本
# ======================================================================
def test_migrate_dry_run_does_not_write():
    _write_plaintext_seed()
    r = _run_migrate("--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY-RUN" in r.stdout
    raw = _read_raw()
    assert "enc:v1:" not in raw  # 不写文件
    assert "下水道堵了" in raw and "燃气泄漏了" in raw  # 仍为明文
    assert _residue_backup_dirs() == []  # dry-run 不生成备份


def test_migrate_execute_encrypts_idempotent_no_residue():
    _write_plaintext_seed()
    r = _run_migrate()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "执行" in r.stdout
    raw = _read_raw()
    assert "enc:v1:" in raw
    for kw in ("下水道堵了", "13912345678", "110101199001011234", "已派物业师傅上门",
               "燃气泄漏了", "user_002"):
        assert kw not in raw, f"迁移后明文仍残留: {kw}"
    assert "物业维修" in raw  # 非敏感字段保持明文
    assert _residue_backup_dirs() == []  # 备份不留残留
    # 幂等：二次运行 0 变更
    r2 = _run_migrate()
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "0 个字段需要加密" in r2.stdout
    assert _residue_backup_dirs() == []


def test_migrate_missing_key_exits_nonzero():
    _write_plaintext_seed()
    r = _run_migrate(pop_keys=("DATA_ENCRYPTION_KEY",))
    assert r.returncode != 0
    assert "DATA_ENCRYPTION_KEY" in (r.stdout + r.stderr)
    raw = _read_raw()
    assert "enc:v1:" not in raw  # 数据未被改动
    assert _residue_backup_dirs() == []


# ======================================================================
# 5. HTTP 契约对比：明文启动兼容读取，加密后 API 响应不变
# ======================================================================
def test_http_contract_unchanged_after_encryption():
    import auth as auth_mod
    main = _import_main()
    ok, msg, data = auth_mod.login_user("admin", "GridAdmin2025!@#")
    assert ok and data and data.get("token"), msg
    token = data["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 步骤 1：明文 tasks.json -> 启动加载 -> 基线响应
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump({"evt-1": _sample_task()}, f, ensure_ascii=False, indent=2)
    main = _import_main()  # 重新加载：读取明文
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    base_resp = client.get("/api/events", headers=headers)
    assert base_resp.status_code == 200, base_resp.text
    base_json = base_resp.json()
    assert len(base_json) == 1
    item = base_json[0]
    assert item["description"] == "楼下下水道堵了，请尽快处理"
    assert item["user_phone"] == "13912345678"
    assert item["user_id_card"] == "110101199001011234"
    assert item["event_lat"] == 30.27415
    assert item["event_lng"] == 120.15515
    assert item["beneficiary_name"] == "张三"
    assert item["reply"] == "已派物业师傅上门"

    # 步骤 2：任一次 _save_tasks 后磁盘自动升级为密文
    main._save_tasks(main._tasks)
    raw = _read_raw()
    assert "enc:v1:" in raw
    for kw in ("13912345678", "110101199001011234", "下水道堵了"):
        assert kw not in raw

    # 步骤 3：重启（重新加载加密文件）-> API 响应与基线逐字段一致
    main2 = _import_main()
    client2 = TestClient(main2.app)
    after_resp = client2.get("/api/events", headers=headers)
    assert after_resp.status_code == 200, after_resp.text
    assert after_resp.json() == base_json

# -*- coding: utf-8 -*-
"""
scripts/memory.py — 任务收尾记忆管理（两轨道）

轨道：
  temp      临时中间状态（进度/待办/临时结论），保留期(TTL)到期自动清理，不进长期记忆。
  longterm  可复用经验（踩坑/约定/部署要点/验收结论），必须带 来源+有效期，经
            提炼->去重->冲突检查 后写入；带衰减评分。

用法：
  python scripts/memory.py add temp --content "..." [--ttl-seconds 604800]
  python scripts/memory.py add longterm --content "..." --source "..." [--valid-days 90] [--tags a,b] [--confidence high|medium|low]
  python scripts/memory.py clean [--dry-run]
  python scripts/memory.py list [temp|longterm|expired|conflicts]
  python scripts/memory.py search <关键词>

敏感数据底线（硬性）：API 密钥/密码/token/手机号/身份证/云凭据等一律不进记忆，
命中即拒绝写入（临时与长期都不行）。
"""
import argparse
import datetime
import hashlib
import uuid
import json
import os
import re
import sys

MEMORY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "coordination", "memory")
TEMP_DIR = os.path.join(MEMORY_DIR, "temporary")
LONG_DIR = os.path.join(MEMORY_DIR, "longterm")
DEFAULT_TTL_SECONDS = 7 * 24 * 3600          # 临时保留期：7 天
DEFAULT_VALID_DAYS = 90                       # 长期时效：90 天
DEFAULT_DECAY_RATE = 0.05                     # 每天衰减率（约 20 天半衰）
LOW_SCORE_THRESHOLD = 0.2                     # 低于该分建议复核
DEFAULT_CONFIDENCE = "medium"

# 敏感数据模式（命中即拒绝；只做模式匹配，不解析/回显值）
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)sk-[a-zA-Z0-9]{16,}"),            # OpenAI/DeepSeek 类密钥
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),               # AWS AK
    re.compile(r"(?i)(secret|password|passwd|token|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),           # 手机号
    re.compile(r"(?<!\d)\d{6}\d{8}\d{3}[\dXx](?!\d)"), # 身份证
    re.compile(r"(?i)COS_SECRET|COS_SECRET_ID|COS_SECRET_KEY"),
]


def _now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _id():
    return uuid.uuid4().hex[:12]


def _ensure_dirs():
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(LONG_DIR, exist_ok=True)


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save(path, record):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)


def _all_records(track):
    d = TEMP_DIR if track == "temp" else LONG_DIR
    out = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            if name.endswith(".json"):
                rec = _load(os.path.join(d, name))
                if rec:
                    out.append(rec)
    return out


def _content_hash(content):
    norm = re.sub(r"\s+", " ", content.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _similar(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def _age_days(iso_str):
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
        return max(0.0, (_now() - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


def _score(record):
    rate = float(record.get("decay_rate", DEFAULT_DECAY_RATE))
    return max(0.0, 1.0 * (rate ** max(0.0, _age_days(record.get("created_at", _iso(_now())))))) if rate < 1 else 0.0


def sensitive(text):
    hits = []
    for pat in SENSITIVE_PATTERNS:
        if pat.search(text or ""):
            hits.append(pat.pattern[:30])
    return hits


def add_temp(content, ttl_seconds=DEFAULT_TTL_SECONDS):
    hits = sensitive(content)
    if hits:
        return False, "拒绝写入：内容命中敏感数据模式（敏感数据不进记忆，可改为记录引用位置）"
    rec = {
        "id": _id(), "track": "temp", "content": content,
        "created_at": _iso(_now()),
        "ttl_seconds": int(ttl_seconds),
        "expires_at": _iso(_now() + datetime.timedelta(seconds=int(ttl_seconds))),
    }
    _save(os.path.join(TEMP_DIR, rec["id"] + ".json"), rec)
    return True, "临时记录已写入（保留期到期自动清理）：" + rec["id"]


def add_longterm(content, source, valid_days=DEFAULT_VALID_DAYS, tags=None, confidence=DEFAULT_CONFIDENCE):
    if not source or not source.strip():
        return False, "拒绝写入：长期记忆必须带来源（--source）"
    hits = sensitive(content)
    if hits:
        return False, "拒绝写入：内容命中敏感数据模式（敏感数据不进记忆，可改为记录引用位置）"
    existing = _all_records("longterm")
    ch = _content_hash(content)
    for rec in existing:
        if rec.get("content_hash") == ch:
            return False, "去重：与既有记录内容相同，跳过（" + rec["id"] + "）"
    for rec in existing:
        if rec.get("status") == "active" and _similar(rec.get("content", ""), content) > 0.85:
            return False, "去重：与既有记录高度相似，跳过（" + rec["id"] + "）；如需更新请删除旧记录后重写"
    now = _now()
    valid_days = int(valid_days)
    rec = {
        "id": _id(), "track": "longterm", "content": content,
        "content_hash": ch,
        "source": source,
        "created_at": _iso(now),
        "valid_until": _iso(now + datetime.timedelta(days=valid_days)),
        "decay_rate": DEFAULT_DECAY_RATE,
        "score": 1.0,
        "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
        "confidence": confidence,
        "status": "active",
    }
    # 冲突检查：同主题（tags 有交集）且为 active 的既有记录
    new_tags = set(rec["tags"])
    for old in existing:
        if old.get("status") == "active" and new_tags & set(old.get("tags", [])) and old.get("content_hash") != ch:
            rec["status"] = "conflict"
            rec["conflict_with"] = old["id"]
            break
    _save(os.path.join(LONG_DIR, rec["id"] + ".json"), rec)
    if rec["status"] == "conflict":
        return True, "已写入但标记为冲突（与 " + rec["conflict_with"] + " 同主题结论不同，需人工裁决）：" + rec["id"]
    return True, "长期记忆已写入：" + rec["id"]


def clean(dry_run=False):
    removed = []
    expired = []
    low = []
    now = _now()
    # 临时轨道：到期即清
    for rec in _all_records("temp"):
        try:
            exp = datetime.datetime.fromisoformat(rec["expires_at"])
            if now >= exp:
                removed.append(rec["id"])
                if not dry_run:
                    os.remove(os.path.join(TEMP_DIR, rec["id"] + ".json"))
        except Exception:
            pass
    # 长期轨道：到期/衰减处理（标记 deprecated，不硬删）
    for rec in _all_records("longterm"):
        try:
            valid = datetime.datetime.fromisoformat(rec["valid_until"])
        except Exception:
            valid = now
        if now >= valid:
            rec["status"] = "deprecated"
            rec["deprecated_at"] = _iso(now)
            expired.append(rec["id"])
            if not dry_run:
                _save(os.path.join(LONG_DIR, rec["id"] + ".json"), rec)
            continue
        sc = _score(rec)
        rec["score"] = round(sc, 3)
        if sc < LOW_SCORE_THRESHOLD:
            low.append((rec["id"], sc))
        if not dry_run:
            _save(os.path.join(LONG_DIR, rec["id"] + ".json"), rec)
    print("临时已清理：" + (", ".join(removed) if removed else "无"))
    print("长期已标记过期(deprecated)：" + (", ".join(expired) if expired else "无"))
    print("低时效建议复核：" + (", ".join(i + "(score=" + str(round(s, 2)) + ")" for i, s in low) if low else "无"))
    if dry_run:
        print("（dry-run，未实际变更）")
    return len(removed) + len(expired) + len(low)


def list_track(track=None):
    if track in (None, "temp"):
        print("== 临时轨道（到期自动清理）==")
        for r in _all_records("temp"):
            print(r["id"], r["expires_at"], r["content"][:60])
    if track in (None, "longterm"):
        print("== 长期轨道（来源+时效+衰减）==")
        for r in _all_records("longterm"):
            print(r["id"], "[" + r.get("status", "?") + "]", "score=" + str(r.get("score", "?")),
                  "until=" + r.get("valid_until", "?"), r["content"][:60])
    if track == "expired":
        for r in _all_records("longterm"):
            if r.get("status") == "deprecated":
                print(r["id"], r["content"][:60])
    if track == "conflicts":
        for r in _all_records("longterm"):
            if r.get("status") == "conflict":
                print(r["id"], "conflict_with=" + str(r.get("conflict_with")), r["content"][:60])


def search(kw):
    kw = kw.lower()
    hit = 0
    for track in ("temp", "longterm"):
        for r in _all_records(track):
            if kw in r.get("content", "").lower() or kw in " ".join(r.get("tags", [])).lower():
                print("[" + track + "] " + r["id"], r["content"][:80])
                hit += 1
    if not hit:
        print("无匹配记录")


def main():
    _ensure_dirs()
    ap = argparse.ArgumentParser(description="任务收尾记忆管理")
    sub = ap.add_subparsers(dest="cmd")
    a1 = sub.add_parser("add")
    a1.add_argument("track", choices=["temp", "longterm"])
    a1.add_argument("--content", required=True)
    a1.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    a1.add_argument("--source", default="")
    a1.add_argument("--valid-days", type=int, default=DEFAULT_VALID_DAYS)
    a1.add_argument("--tags", default="")
    a1.add_argument("--confidence", default=DEFAULT_CONFIDENCE, choices=["high", "medium", "low"])
    a2 = sub.add_parser("clean")
    a2.add_argument("--dry-run", action="store_true")
    sub.add_parser("list")
    sub.add_parser("expired")
    sub.add_parser("conflicts")
    a3 = sub.add_parser("search")
    a3.add_argument("keyword")
    args = ap.parse_args()

    if args.cmd == "add":
        if args.track == "temp":
            ok, msg = add_temp(args.content, args.ttl_seconds)
        else:
            ok, msg = add_longterm(args.content, args.source, args.valid_days, args.tags, args.confidence)
        print(("OK  " if ok else "FAIL") + " " + msg)
        return 0 if ok else 1
    if args.cmd == "clean":
        clean(args.dry_run)
        return 0
    if args.cmd == "list":
        list_track(None)
        return 0
    if args.cmd == "expired":
        list_track("expired")
        return 0
    if args.cmd == "conflicts":
        list_track("conflicts")
        return 0
    if args.cmd == "search":
        search(args.keyword)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
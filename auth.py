"""
auth.py
用户认证与授权模块

功能：基于文件存储的用户注册、登录、Token 校验与角色权限管理。
      密码使用 PBKDF2+HMAC-SHA256 哈希，Token 使用 secrets 生成随机字符串。
      与现有文件存储架构保持一致，不引入外部数据库。
"""

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

import cloud_store
import config
import geo
from filelock import FileLock
from secure_store import decrypt, encrypt, load_encrypted, save_encrypted

logger = logging.getLogger("auth")

# 账号存储后端：file（本地加密文件，默认/测试）| cloudbase（腾讯云 COS 云存储）
AUTH_STORE = os.getenv("AUTH_STORE", "file").strip().lower()

# ------------------------------------------------------------------
# 数据文件路径
# 账号/会话数据使用 AES-256-GCM 加密存于 secure/（密钥见环境变量
# DATA_ENCRYPTION_KEY）；明文 events/tasks 等仍存于 data/。
# AUTH_STORE=cloudbase 时，用户与会话数据均改为读写云存储（cloud_store），
# 本地 secure/*.enc 不再作为权威（file 模式仍为本地加密文件）。
# ------------------------------------------------------------------
DATA_DIR = "./data"
SECURE_DIR = "./secure"
USERS_FILE = os.path.join(SECURE_DIR, "users.json.enc")
SESSIONS_FILE = os.path.join(SECURE_DIR, "sessions.json.enc")

# 旧版明文文件（首次升级时用于迁移到加密存储）
LEGACY_USERS_FILE = os.path.join(DATA_DIR, "users.json")
LEGACY_SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")

# ------------------------------------------------------------------
# 内存缓存与并发锁
# ------------------------------------------------------------------
_users: dict[str, dict[str, Any]] = {}
_sessions: dict[str, dict[str, Any]] = {}
_AUTH_LOCK_PATH = os.path.join(SECURE_DIR, ".auth.lock")
_auth_lock = FileLock(_AUTH_LOCK_PATH)

_users_mtime: float = 0.0
_sessions_mtime: float = 0.0
_username_index: dict[str, str] = {}  # username -> user_id
_phone_index: dict[str, str] = {}     # phone -> user_id


# ------------------------------------------------------------------
# 工具函数：文件读写（账号/会话走加密存储）
# ------------------------------------------------------------------
def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(kind: str, path: str) -> dict[str, Any]:
    """读取加密存储文件。

    文件不存在 -> 返回 {}（安全默认）；
    文件存在但解密/解析失败 -> 抛错 fail-fast，绝不静默返回 {}，
    否则上层会误判"无用户"并重建 admin 覆盖真实数据。
    """
    return load_encrypted(kind, path)


def _save_json(kind: str, path: str, data: dict[str, Any]) -> None:
    """加密并原子写入存储文件。写失败抛错，让注册/登录返回失败，
    而不是"表面成功、重启丢数据"。
    """
    _ensure_data_dir()
    save_encrypted(kind, path, data)


# ------------------------------------------------------------------
# 辅助函数：索引重建与缓存刷新
# ------------------------------------------------------------------
def _rebuild_indices() -> None:
    """根据当前 _users 重建 username/phone 辅助索引（O(n)，仅在加载后调用）。"""
    global _username_index, _phone_index
    _username_index = {}
    _phone_index = {}
    for uid, u in _users.items():
        uname = u.get("username")
        if uname:
            _username_index[uname] = uid
        phone = u.get("phone")
        if phone:
            _phone_index[phone] = uid


def _refresh_users_if_stale() -> None:
    """若 users 加密文件比内存缓存新，则重载并重建索引。"""
    global _users, _users_mtime
    if AUTH_STORE == "cloudbase":
        return
    try:
        mtime = os.path.getmtime(USERS_FILE)
    except FileNotFoundError:
        return
    if mtime > _users_mtime:
        _users = _load_users()
        _rebuild_indices()
        _users_mtime = mtime


def _refresh_sessions_if_stale() -> None:
    """若 sessions 加密文件比内存缓存新，则重载并清理过期会话。"""
    global _sessions, _sessions_mtime
    if AUTH_STORE == "cloudbase":
        return
    try:
        mtime = os.path.getmtime(SESSIONS_FILE)
    except FileNotFoundError:
        return
    if mtime > _sessions_mtime:
        _sessions = _load_sessions()
        _sessions_mtime = mtime


def _is_session_expired(session: dict[str, Any]) -> bool:
    """判断会话是否过期。优先使用 last_active_at（滑动过期），兼容旧数据 fallback 到 created_at（固定过期）。"""
    cutoff = (datetime.now() - timedelta(days=_SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    last_active = session.get("last_active_at") or session.get("created_at", "")
    return last_active < cutoff


def _update_mtime_after_save(path: str) -> float:
    """保存文件后读取并返回最新 mtime，用于同步内存缓存标记。"""
    try:
        return os.path.getmtime(path)
    except FileNotFoundError:
        return 0.0


# ------------------------------------------------------------------
# 用户数据存储分发（本地加密文件 / 云存储）
# ------------------------------------------------------------------
def _load_users() -> dict[str, Any]:
    """读取用户数据，按 AUTH_STORE 分发。

    云存储模式：对象不存在 -> 返回 {}（安全默认，视为空库）；
    其它异常 -> 抛错 fail-fast，防止误判"无用户"重建 admin 覆盖云端数据。
    """
    if AUTH_STORE == "cloudbase":
        blob = cloud_store.download(cloud_store.USERS_OBJECT_KEY)
        if blob is None:
            return {}
        return decrypt("users", blob)
    return _load_json("users", USERS_FILE)


def _save_users(data: dict[str, Any]) -> None:
    """保存用户数据：先 AES-256-GCM 加密，再按 AUTH_STORE 写入本地或云存储。"""
    if AUTH_STORE == "cloudbase":
        cloud_store.upload(cloud_store.USERS_OBJECT_KEY, encrypt("users", data))
        return
    _save_json("users", USERS_FILE, data)

# ------------------------------------------------------------------
# 会话数据存储分发（本地加密文件 / 云存储）
# ------------------------------------------------------------------
def _load_sessions() -> dict[str, Any]:
    """读取会话数据，按 AUTH_STORE 分发。

    云存储模式：对象不存在 -> 返回 {}（安全默认，视为空库）；
    其它异常 -> 抛错 fail-fast，防止误判导致数据覆盖。
    加载后自动清理过期会话。
    """
    if AUTH_STORE == "cloudbase":
        blob = cloud_store.download(cloud_store.SESSIONS_OBJECT_KEY)
        if blob is None:
            return {}
        data = decrypt("sessions", blob)
    else:
        data = _load_json("sessions", SESSIONS_FILE)
    return _cleanup_expired_sessions(data)


def _save_sessions(data: dict[str, Any]) -> None:
    """保存会话数据：先 AES-256-GCM 加密，再按 AUTH_STORE 写入本地或云存储。"""
    if AUTH_STORE == "cloudbase":
        cloud_store.upload(cloud_store.SESSIONS_OBJECT_KEY, encrypt("sessions", data))
        return
    _save_json("sessions", SESSIONS_FILE, data)


# ------------------------------------------------------------------
# 旧版明文数据迁移
# ------------------------------------------------------------------
def _read_plaintext_json(path: str) -> dict[str, Any] | None:
    """读取旧版明文 JSON；文件不存在或解析失败返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.error("读取旧版明文文件失败，path=%s，异常=%s", path, exc)
        return None


def _rename_to_bak(path: str) -> None:
    """旧文件改名 .migrated.bak，保留现场以便回退。"""
    if not os.path.exists(path):
        return
    bak = path + ".migrated.bak"
    if os.path.exists(bak):
        os.remove(bak)
    os.replace(path, bak)


def _migrate_legacy_to_secure() -> None:
    """把旧版明文 data/users.json、sessions.json 迁移为 secure/ 加密文件。

    优先级：secure 加密文件为权威。仅当 secure 文件不存在、旧明文存在时才迁移；
    已迁移后（secure 已存在）直接忽略明文，绝不回退。
    云存储模式：用户数据来自云端，跳过本地明文迁移。
    """
    if AUTH_STORE == "cloudbase":
        return
    if os.path.exists(USERS_FILE) or os.path.exists(SESSIONS_FILE):
        return  # 已有加密数据，以 secure 为权威，不做迁移

    users_legacy_exists = os.path.exists(LEGACY_USERS_FILE)
    sessions_legacy_exists = os.path.exists(LEGACY_SESSIONS_FILE)
    if not users_legacy_exists and not sessions_legacy_exists:
        return

    if users_legacy_exists:
        data = _read_plaintext_json(LEGACY_USERS_FILE) or {}
        if data:
            _save_json("users", USERS_FILE, data)
            logger.info("已迁移 %d 个用户账号到加密存储", len(data))
        _rename_to_bak(LEGACY_USERS_FILE)

    if sessions_legacy_exists:
        data = _read_plaintext_json(LEGACY_SESSIONS_FILE) or {}
        if data:
            _save_json("sessions", SESSIONS_FILE, data)
            logger.info("已迁移 %d 条会话到加密存储", len(data))
        _rename_to_bak(LEGACY_SESSIONS_FILE)

    logger.info("明文账号数据已迁移，原文件已改名 *.migrated.bak")


# ------------------------------------------------------------------
# 初始化加载
# ------------------------------------------------------------------
def _init_auth() -> None:
    global _users, _sessions, _users_mtime, _sessions_mtime
    # 云存储模式：先确保存储桶存在，再加载 users/sessions（桶缺失自动创建）
    if AUTH_STORE == "cloudbase":
        cloud_store.ensure_bucket()
    # 首次升级：把旧版明文账号数据迁移到加密存储（幂等，secure 为权威；
    # cloudbase 模式跳过，身份数据以云端为准）
    _migrate_legacy_to_secure()

    _users = _load_users()
    _rebuild_indices()
    _sessions = _load_sessions()
    _cleanup_expired_sessions()

    # 记录初始 mtime，便于后续多进程缓存同步
    if AUTH_STORE != "cloudbase":
        try:
            _users_mtime = os.path.getmtime(USERS_FILE)
        except FileNotFoundError:
            _users_mtime = 0.0
        try:
            _sessions_mtime = os.path.getmtime(SESSIONS_FILE)
        except FileNotFoundError:
            _sessions_mtime = 0.0

    # 数据兼容：为存量用户补齐住户/定位字段。
    # 住户注册即生效（status=active），无需人工审核；存量 pending/rejected 一并转 active。
    changed = False
    for u in _users.values():
        for key in ("building", "unit", "room", "register_time"):
            if key not in u:
                u[key] = ""
                changed = True
        if u.get("status", "active") in ("pending", "rejected"):
            u["status"] = "active"
            changed = True
        if "location_status" not in u:
            u["location_status"] = "unverified"
            changed = True
        for key in ("register_lat", "register_lng"):
            if key not in u:
                u[key] = None
                changed = True
    if changed:
        _save_users(_users)

    # 若系统中没有任何用户，自动创建默认管理员账号
    if not _users:
        admin_id = secrets.token_hex(16)
        _admin_initial_password = os.environ.get("ADMIN_INITIAL_PASSWORD")
        if not _admin_initial_password:
            _admin_initial_password = secrets.token_urlsafe(16)
            logger.warning(
                "未设置 ADMIN_INITIAL_PASSWORD 环境变量，已生成随机强密码。"
                "请使用日志中的密码首次登录，并立即修改密码。"
            )
        admin_user = {
            "id": admin_id,
            "username": "admin",
            "password_hash": _hash_password(_admin_initial_password),
            "real_name": "系统管理员",
            "phone": "13800000000",
            "role": "admin",
            "status": "active",
            "location_status": "unverified",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _users[admin_id] = admin_user
        _save_users(_users)
        _save_sessions(_sessions)  # 空库重建：会话表一并上云（空表）
        logger.info(
            "系统初始化：已创建默认管理员账号，请及时修改密码",
        )


# ------------------------------------------------------------------
# 密码哈希
# ------------------------------------------------------------------
_SALT_LEN = 32
_ITERATIONS = 600_000


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(_SALT_LEN)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS
    ).hex()
    return f"{salt}${pwd_hash}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, pwd_hash = stored.split("$", 1)
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS
        ).hex()
        if secrets.compare_digest(computed, pwd_hash):
            return True
        # 兼容旧哈希（100000 次迭代）
        computed_legacy = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000
        ).hex()
        return secrets.compare_digest(computed_legacy, pwd_hash)
    except ValueError:
        logger.error("密码哈希格式异常（缺少$分隔符或结构损坏），stored长度=%d", len(stored))
        return False


def _mask_id_card(id_card: str) -> str:
    """身份证号脱敏：显示前6位和后4位，中间用*代替。"""
    if not id_card or len(id_card) < 10:
        return id_card
    return id_card[:6] + "********" + id_card[-4:]


def _user_status(user: dict[str, Any]) -> str:
    """住户审核状态：管理员账号恒为 active。"""
    if user.get("role") == "admin":
        return "active"
    return user.get("status", "active")  # 兼容旧数据（存量住户视为已通过）


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    """返回对外展示的用户信息（身份证脱敏，含住户/定位字段）。"""
    return {
        "id": user["id"],
        "username": user["username"],
        "real_name": user["real_name"],
        "phone": user["phone"],
        "id_card": _mask_id_card(user.get("id_card", "")),
        "role": user["role"],
        "created_at": user["created_at"],
        "status": _user_status(user),
        "building": user.get("building", ""),
        "unit": user.get("unit", ""),
        "room": user.get("room", ""),
        "location_status": user.get("location_status", "unverified"),
    }


# ------------------------------------------------------------------
# Token 与会话管理
# ------------------------------------------------------------------
_SESSION_TTL_DAYS = 7


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _cleanup_expired_sessions(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """清理过期会话（超过 7 天未活跃）。

    若传入 data，则清理传入的 dict 并返回；
    否则清理全局 _sessions 并保存。
    """
    if data is not None:
        expired = [t for t, s in data.items() if _is_session_expired(s)]
        for t in expired:
            data.pop(t, None)
        return data

    expired = [t for t, s in _sessions.items() if _is_session_expired(s)]
    for t in expired:
        _sessions.pop(t, None)
    if expired:
        _save_sessions(_sessions)
        logger.info("清理 %d 条过期会话", len(expired))
    return _sessions


# ------------------------------------------------------------------
# 用户注册
# ------------------------------------------------------------------
def register_user(
    username: str, password: str, real_name: str, phone: str, id_card: str = "",
    role: str = "resident", building: str = "", unit: str = "", room: str = "",
    register_lat: float | None = None, register_lng: float | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    注册新用户。
    居民注册必须填写楼栋/单元/房间号，且定位必须在小区范围内（无定位/越界拒绝），
    注册即生效（status=active），可直接提交事件。
    返回：(success, message, user_dict)
    """
    username = username.strip()
    real_name = real_name.strip()
    phone = phone.strip()
    id_card = id_card.strip().upper()
    building = building.strip()
    unit = unit.strip()
    room = room.strip()

    # 基本校验
    if not username or len(username) < 3 or len(username) > 20:
        return False, "注册失败，请检查输入信息", None
    if not re.match(r"^[a-zA-Z0-9_一-龥]+$", username):
        return False, "注册失败，请检查输入信息", None
    if not password or len(password) < 6:
        return False, "注册失败，请检查输入信息", None
    if not real_name or len(real_name) < 1 or len(real_name) > 20:
        return False, "注册失败，请检查输入信息", None
    if not re.match(r"^1[3-9]\d{9}$", phone):
        return False, "注册失败，请检查输入信息", None
    if not id_card or not re.match(r"^[1-9]\d{5}(18|19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dX]$", id_card):
        return False, "注册失败，请检查输入信息", None
    if role not in ("resident", "admin"):
        return False, "注册失败，请检查输入信息", None
    if role == "admin":
        return False, "注册失败，请检查输入信息", None
    if role == "resident" and not (building and unit and room):
        return False, "注册失败，请检查输入信息", None

    with _auth_lock:
        global _users
        _refresh_users_if_stale()
        # 检查用户名/手机号是否已存在（O(1) 索引查找）
        if username in _username_index:
            return False, "注册失败，请检查输入信息", None
        if phone in _phone_index:
            return False, "注册失败，请检查输入信息", None

        user_id = secrets.token_hex(16)
        register_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 注册定位校验（默认开启，可用 COMMUNITY_REQUIRE_LOCATION=false 关闭）
        # 开启：必须带定位且在小区范围内，否则拒绝（原产品规则，浏览器定位需 HTTPS/localhost）
        # 关闭：允许无定位注册（暂无 HTTPS 的环境临时开放注册用）
        if config.COMMUNITY_REQUIRE_LOCATION:
            if register_lat is None or register_lng is None:
                return False, "注册失败，请检查输入信息", None
            within, _dist = geo.is_within_community(register_lat, register_lng)
            if not within:
                return False, "注册失败，请检查输入信息", None
            location_status = "verified"
        else:
            location_status = "unverified"
        user = {
            "id": user_id,
            "username": username,
            "password_hash": _hash_password(password),
            "real_name": real_name,
            "phone": phone,
            "id_card": id_card,
            "role": role,
            "building": building,
            "unit": unit,
            "room": room,
            "status": "active",  # 注册即生效，无需管理员审核
            "location_status": location_status,
            "register_lat": register_lat,
            "register_lng": register_lng,
            "register_time": register_time,
            "created_at": register_time,
        }
        _users[user_id] = user
        _username_index[username] = user_id
        _phone_index[phone] = user_id
        _save_users(_users)
        _users_mtime = _update_mtime_after_save(USERS_FILE)

    return True, "注册成功，请登录", _public_user(user)


# ------------------------------------------------------------------
# 用户登录
# ------------------------------------------------------------------
def login_user(username: str, password: str) -> tuple[bool, str, dict[str, Any] | None]:
    """
    用户登录。
    返回：(success, message, {"token": ..., "user": {...}})
    """
    username = username.strip()

    with _auth_lock:
        global _users
        _refresh_users_if_stale()
        user_id = _username_index.get(username)
        user = _users.get(user_id) if user_id else None

        if user is None:
            return False, "用户名或密码错误", None

        if not _verify_password(password, user["password_hash"]):
            return False, "用户名或密码错误", None

        token = _generate_token()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _sessions[token] = {
            "token": token,
            "user_id": user["id"],
            "created_at": now,
            "last_active_at": now,
        }
        _save_sessions(_sessions)
        _sessions_mtime = _update_mtime_after_save(SESSIONS_FILE)

    return True, "登录成功", {"token": token, "user": _public_user(user)}


# ------------------------------------------------------------------
# 用户登出（使 Token 失效）
# ------------------------------------------------------------------
def logout_user(token: str | None) -> bool:
    """
    登出：从服务端会话存储中删除指定 Token，使其立即失效。
    返回：是否成功删除。
    """
    if not token:
        return False
    with _auth_lock:
        global _sessions
        _refresh_sessions_if_stale()
        if token in _sessions:
            del _sessions[token]
            _save_sessions(_sessions)
            _sessions_mtime = _update_mtime_after_save(SESSIONS_FILE)
            return True
    return False


# ------------------------------------------------------------------
# Token 校验与当前用户获取
# ------------------------------------------------------------------
def get_current_user(token: str | None) -> dict[str, Any] | None:
    """
    根据 Token 获取当前用户信息。
    若 Token 无效或过期，返回 None。
    """
    if not token:
        return None

    with _auth_lock:
        global _sessions, _sessions_mtime
        _refresh_sessions_if_stale()
        session = _sessions.get(token)
        if session is None:
            return None

        if _is_session_expired(session):
            _sessions.pop(token, None)
            _save_sessions(_sessions)
            _sessions_mtime = _update_mtime_after_save(SESSIONS_FILE)
            return None

        # 滑动过期：更新最后活跃时间并持久化
        session["last_active_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_sessions(_sessions)
        _sessions_mtime = _update_mtime_after_save(SESSIONS_FILE)

        global _users
        _refresh_users_if_stale()
        user = _users.get(session.get("user_id"))
        if user is None:
            return None

    return _public_user(user)


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    """
    根据用户ID获取完整用户信息（含原始身份证号，供后台审核使用）。
    """
    with _auth_lock:
        global _users
        _refresh_users_if_stale()
        user = _users.get(user_id)
        if user is None:
            return None
    return {
        "id": user["id"],
        "username": user["username"],
        "real_name": user["real_name"],
        "phone": user["phone"],
        "id_card": user.get("id_card", ""),
        "role": user["role"],
        "created_at": user["created_at"],
    }


# ------------------------------------------------------------------
# 住户列表（只读，供管理员后台查看）
# ------------------------------------------------------------------
def list_users() -> list[dict[str, Any]]:
    """
    返回全部居民（仅供管理员后台使用，只读）。

    含完整身份证号、楼栋/单元/房间、注册定位坐标与距中心点米数，
    便于管理员了解本小区住户构成。注册即生效，无需审核操作。
    """
    with _auth_lock:
        global _users
        _refresh_users_if_stale()
        users = []
        for u in _users.values():
            if u.get("role") != "resident":
                continue
            lat = u.get("register_lat")
            lng = u.get("register_lng")
            distance = geo.is_within_community(lat, lng)[1] if (lat is not None and lng is not None) else None
            users.append({
                "id": u["id"],
                "username": u["username"],
                "real_name": u["real_name"],
                "phone": u["phone"],
                "id_card": u.get("id_card", ""),  # 完整身份证，仅管理员可见
                "role": u["role"],
                "building": u.get("building", ""),
                "unit": u.get("unit", ""),
                "room": u.get("room", ""),
                "status": _user_status(u),
                "location_status": u.get("location_status", "unverified"),
                "register_lat": lat,
                "register_lng": lng,
                "register_distance_m": distance,
                "register_time": u.get("register_time", ""),
                "created_at": u["created_at"],
            })
        # 注册时间倒序（新住户在前）
        users.sort(key=lambda x: x.get("register_time") or x.get("created_at") or "", reverse=True)
        return users


# ------------------------------------------------------------------
# 初始化（模块导入时执行）
# ------------------------------------------------------------------
_init_auth()

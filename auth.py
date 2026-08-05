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
import threading
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger("auth")

# ------------------------------------------------------------------
# 数据文件路径
# ------------------------------------------------------------------
DATA_DIR = "./data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")

# ------------------------------------------------------------------
# 内存缓存与并发锁
# ------------------------------------------------------------------
_users: dict[str, dict[str, Any]] = {}
_sessions: dict[str, dict[str, Any]] = {}
_auth_lock = threading.Lock()


# ------------------------------------------------------------------
# 工具函数：文件读写
# ------------------------------------------------------------------
def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.error("加载文件失败，path=%s，异常=%s", path, exc)
        return {}


def _save_json(path: str, data: dict[str, Any]) -> None:
    _ensure_data_dir()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as exc:
        logger.error("保存文件失败，path=%s，异常=%s", path, exc)


# ------------------------------------------------------------------
# 初始化加载
# ------------------------------------------------------------------
def _init_auth() -> None:
    global _users, _sessions
    _users = _load_json(USERS_FILE)
    _sessions = _load_json(SESSIONS_FILE)
    _cleanup_expired_sessions()

    # 若系统中没有任何用户，自动创建默认管理员账号
    if not _users:
        admin_id = secrets.token_hex(16)
        admin_user = {
            "id": admin_id,
            "username": "admin",
            "password_hash": _hash_password("admin123456"),
            "real_name": "系统管理员",
            "phone": "13800000000",
            "role": "admin",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _users[admin_id] = admin_user
        _save_json(USERS_FILE, _users)
        logger.info("系统初始化：已创建默认管理员账号 admin / admin123456，请及时修改密码")


# ------------------------------------------------------------------
# 密码哈希
# ------------------------------------------------------------------
_SALT_LEN = 32
_ITERATIONS = 100_000


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
        return secrets.compare_digest(computed, pwd_hash)
    except ValueError:
        return False


# ------------------------------------------------------------------
# Token 与会话管理
# ------------------------------------------------------------------
_SESSION_TTL_DAYS = 7


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _cleanup_expired_sessions() -> None:
    """清理过期会话（超过 7 天未活跃）。"""
    cutoff = (datetime.now() - timedelta(days=_SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    expired = [t for t, s in _sessions.items() if s.get("created_at", "") < cutoff]
    for t in expired:
        _sessions.pop(t, None)
    if expired:
        _save_json(SESSIONS_FILE, _sessions)
        logger.info("清理 %d 条过期会话", len(expired))


# ------------------------------------------------------------------
# 用户注册
# ------------------------------------------------------------------
def register_user(
    username: str, password: str, real_name: str, phone: str, role: str = "resident"
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    注册新用户。
    返回：(success, message, user_dict)
    """
    username = username.strip()
    real_name = real_name.strip()
    phone = phone.strip()

    # 基本校验
    if not username or len(username) < 3 or len(username) > 20:
        return False, "用户名长度需在 3-20 个字符之间", None
    if not re.match(r"^[a-zA-Z0-9_一-龥]+$", username):
        return False, "用户名只能包含中文、字母、数字和下划线", None
    if not password or len(password) < 6:
        return False, "密码长度至少为 6 位", None
    if not real_name or len(real_name) < 1 or len(real_name) > 20:
        return False, "真实姓名不能为空且不能超过 20 个字符", None
    if not re.match(r"^1[3-9]\d{9}$", phone):
        return False, "手机号格式不正确", None
    if role not in ("resident", "admin"):
        return False, "角色类型无效", None
    if role == "admin":
        return False, "禁止通过注册创建管理员账号", None

    with _auth_lock:
        # 检查用户名是否已存在
        for user in _users.values():
            if user.get("username") == username:
                return False, "用户名已被注册", None
            if user.get("phone") == phone:
                return False, "手机号已被注册", None

        user_id = secrets.token_hex(16)
        user = {
            "id": user_id,
            "username": username,
            "password_hash": _hash_password(password),
            "real_name": real_name,
            "phone": phone,
            "role": role,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _users[user_id] = user
        _save_json(USERS_FILE, _users)

    # 返回脱敏后的用户信息
    public_user = {
        "id": user["id"],
        "username": user["username"],
        "real_name": user["real_name"],
        "phone": user["phone"],
        "role": user["role"],
        "created_at": user["created_at"],
    }
    return True, "注册成功", public_user


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
        user = None
        for u in _users.values():
            if u.get("username") == username:
                user = u
                break

        if user is None:
            return False, "用户名或密码错误", None

        if not _verify_password(password, user["password_hash"]):
            return False, "用户名或密码错误", None

        token = _generate_token()
        _sessions[token] = {
            "token": token,
            "user_id": user["id"],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_json(SESSIONS_FILE, _sessions)

    public_user = {
        "id": user["id"],
        "username": user["username"],
        "real_name": user["real_name"],
        "phone": user["phone"],
        "role": user["role"],
        "created_at": user["created_at"],
    }
    return True, "登录成功", {"token": token, "user": public_user}


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
        if token in _sessions:
            del _sessions[token]
            _save_json(SESSIONS_FILE, _sessions)
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
        session = _sessions.get(token)
        if session is None:
            return None

        # 检查是否过期
        created = session.get("created_at", "")
        cutoff = (datetime.now() - timedelta(days=_SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        if created < cutoff:
            _sessions.pop(token, None)
            _save_json(SESSIONS_FILE, _sessions)
            return None

        user = _users.get(session.get("user_id"))
        if user is None:
            return None

    return {
        "id": user["id"],
        "username": user["username"],
        "real_name": user["real_name"],
        "phone": user["phone"],
        "role": user["role"],
        "created_at": user["created_at"],
    }


# ------------------------------------------------------------------
# 初始化（模块导入时执行）
# ------------------------------------------------------------------
_init_auth()

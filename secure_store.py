"""
secure_store.py
账号/会话数据的加密存储模块

功能：
- 使用 AES-256-GCM 对 users.json / sessions.json 进行 at-rest 加密。
- 密钥来自环境变量 DATA_ENCRYPTION_KEY（64 位 hex = 32 字节）。
- 加密文件二进制格式自包含：MAGIC(8B) || nonce(12B) || ciphertext(||tag)。
  注意：cryptography 的 AESGCM.encrypt() 仅返回 ct||tag（不前置 nonce），
  因此 nonce 需显式写入文件，解密时再取出。
- 失败策略：文件不存在 -> 返回 {}（安全）；文件存在但解密/解析失败 -> raise，
  绝不静默返回空字典（否则上层会误判"无用户"并重建 admin 覆盖真实数据）。
"""

import json
import logging
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("secure_store")


class SecureStoreError(RuntimeError):
    """加密存储操作失败（密钥错误、文件损坏、格式非法等）。"""


# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------
MAGIC: bytes = b"AGCRYPT1"  # 8 字节魔数
NONCE_LEN = 12  # GCM 标准 nonce 长度
KEY_ENV = "DATA_ENCRYPTION_KEY"
_KEY_BYTES = 32  # AES-256
_KEY_HEX_LEN = _KEY_BYTES * 2  # 64


# ------------------------------------------------------------------
# 密钥管理
# ------------------------------------------------------------------
def generate_key_hex() -> str:
    """生成 64 位 hex（32 字节）加密密钥，供写入 .env 使用。"""
    return secrets.token_hex(_KEY_BYTES)


def get_key() -> bytes:
    """读取并校验环境变量密钥，返回 32 字节。缺失/格式错误则 raise。"""
    raw = (os.environ.get(KEY_ENV) or "").strip()
    if len(raw) != _KEY_HEX_LEN:
        raise SecureStoreError(
            f"环境变量 {KEY_ENV} 未设置或长度不是 {_KEY_HEX_LEN} 位十六进制"
            f"（对应 {_KEY_BYTES} 字节）。\n"
            f"生成命令：python -c \"import secrets; print(secrets.token_hex({_KEY_BYTES}))\""
        )
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise SecureStoreError(
            f"环境变量 {KEY_ENV} 不是合法的十六进制字符串。"
        ) from exc


# ------------------------------------------------------------------
# 加解密
# ------------------------------------------------------------------
def _aad(kind: str) -> bytes:
    """AAD 域隔离：users 与 sessions 使用不同上下文，防止文件被交换。"""
    if kind not in ("users", "sessions"):
        raise ValueError(f"未知的存储域 kind={kind!r}")
    return f"ai-community-auth:{kind}:v1".encode("utf-8")


def encrypt(kind: str, data: dict, key: bytes | None = None) -> bytes:
    """加密 dict 为自包含二进制：MAGIC || nonce || ciphertext(||tag)。"""
    if key is None:
        key = get_key()
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    nonce = secrets.token_bytes(NONCE_LEN)  # 每次新 nonce，满足 GCM key+nonce 不复用
    # AESGCM.encrypt() 返回 ct||tag（不含 nonce），此处显式写入 nonce
    ct_tag = AESGCM(key).encrypt(nonce, plaintext, _aad(kind))
    return MAGIC + nonce + ct_tag


def decrypt(kind: str, blob: bytes, key: bytes | None = None) -> dict:
    """解密自包含二进制；任何一步失败 raise SecureStoreError（绝不返回空）。"""
    if key is None:
        key = get_key()
    if not blob.startswith(MAGIC):
        raise SecureStoreError(
            f"文件魔数不匹配，可能不是本系统加密的文件，或混入了明文 JSON。"
        )
    body = blob[len(MAGIC):]
    if len(body) <= NONCE_LEN:
        raise SecureStoreError("加密文件内容过短，已损坏。")
    nonce, ct_tag = body[:NONCE_LEN], body[NONCE_LEN:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ct_tag, _aad(kind))
    except Exception as exc:  # InvalidTag 等
        raise SecureStoreError(
            f"解密 {kind} 数据失败（密钥不匹配或文件被篡改/损坏）。\n"
            f"可能原因：1) {KEY_ENV} 与写入时不一致；2) 文件被篡改或损坏。\n"
            f"请勿删除该文件，恢复备份并核对密钥后重启。"
        ) from exc
    try:
        data = json.loads(plaintext)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SecureStoreError("解密成功但内容不是合法 JSON，文件可能损坏。") from exc
    if not isinstance(data, dict):
        raise SecureStoreError("解密内容结构异常（应为对象）。")
    return data


# ------------------------------------------------------------------
# 文件读写（原子写）
# ------------------------------------------------------------------
def atomic_write(path: str, blob: bytes) -> None:
    """原子写入：临时文件 + fsync + os.replace，防止写一半损坏加密文件。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_encrypted(kind: str, path: str, key: bytes | None = None) -> dict:
    """读取加密文件；文件不存在返回 {}，存在但解密失败则 raise。"""
    if not os.path.exists(path):
        return {}
    with open(path, "rb") as f:
        blob = f.read()
    if not blob:
        raise SecureStoreError(f"加密文件 {path} 为空，已损坏。")
    return decrypt(kind, blob, key)


def save_encrypted(kind: str, path: str, data: dict, key: bytes | None = None) -> None:
    """加密并原子写入文件。"""
    atomic_write(path, encrypt(kind, data, key))


# ------------------------------------------------------------------
# 命令行工具：生成密钥 / 密钥轮换
# ------------------------------------------------------------------
def _rekey_all(new_key_hex: str, secure_dir: str = "./secure") -> None:
    """用当前环境变量密钥解密 secure/ 下所有 .enc，再用新密钥重加密。"""
    old_key = get_key()
    new_key = bytes.fromhex(new_key_hex)
    for kind, filename in (("users", "users.json.enc"), ("sessions", "sessions.json.enc")):
        path = os.path.join(secure_dir, filename)
        if not os.path.exists(path):
            logger.warning("跳过不存在的文件：%s", path)
            continue
        data = load_encrypted(kind, path, old_key)
        save_encrypted(kind, path, data, new_key)
        logger.info("已用新密钥重加密：%s", path)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else __import__("sys").argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("用法：")
        print("  python secure_store.py genkey              # 生成新的 64 位 hex 密钥")
        print("  python secure_store.py rekey --new <hex>   # 用当前 DATA_ENCRYPTION_KEY 解密，再用新密钥重加密 secure/")
        return 0
    if argv[0] == "genkey":
        print(generate_key_hex())
        return 0
    if argv[0] == "rekey":
        new_key = None
        for i, a in enumerate(argv[1:]):
            if a == "--new" and i + 1 < len(argv[1:]):
                new_key = argv[1:][i + 1]
        if not new_key or len(new_key) != _KEY_HEX_LEN:
            print("rekey 需要 --new <64 位 hex 新密钥>")
            return 1
        _rekey_all(new_key)
        print("密钥轮换完成。请同步更新 .env 中的 DATA_ENCRYPTION_KEY。")
        return 0
    print(f"未知命令：{argv[0]}")
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())

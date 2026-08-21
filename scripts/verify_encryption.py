"""
verify_encryption.py
账号加密存储方案验证脚本（临时）

在临时 scratch 目录运行，不触碰项目真实 data/secure。
验证场景：
  1. 缺 key -> import auth 报错（含生成命令）
  2. 首启   -> 生成二进制 secure/users.json.enc，无明文 admin 字样
  3. 错 key -> 报错"密钥不匹配"，加密文件未被改动
  4. 篡改   -> 翻转一个字节 -> 报错（fail-fast）
  5. 迁移   -> 明文 data/users.json 自动加密迁移，原用户可登录，不重复建 admin
  6. 闭环   -> 注册 -> 重启（重新 import）-> 登录成功
"""
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile

PROJ = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(tempfile.gettempdir(), "verify_enc_scratch")
KEY = "1" * 64
WRONG_KEY = "2" * 64
PASS = []
FAIL = []


def check(name, ok, detail=""):
    if ok:
        PASS.append(name)
        print(f"  [PASS] {name}  {detail}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


def reset_scratch(with_users_plaintext=False):
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH)
    shutil.copy(os.path.join(PROJ, "auth.py"), SCRATCH)
    shutil.copy(os.path.join(PROJ, "secure_store.py"), SCRATCH)
    shutil.copy(os.path.join(PROJ, "cloud_store.py"), SCRATCH)
    shutil.copy(os.path.join(PROJ, "geo.py"), SCRATCH)
    shutil.copy(os.path.join(PROJ, "community_store.py"), SCRATCH)
    if with_users_plaintext:
        os.makedirs(os.path.join(SCRATCH, "data"))
        salt = secrets.token_hex(32)
        pwd_hash = hashlib.pbkdf2_hmac(
            "sha256", "migrate123".encode("utf-8"), salt.encode("utf-8"), 100_000
        ).hex()
        users = {
            "legacy_user_id": {
                "id": "legacy_user_id",
                "username": "legacy_old",
                "password_hash": f"{salt}${pwd_hash}",
                "real_name": "老用户",
                "phone": "13800009999",
                "id_card": "110101199001011234",
                "role": "resident",
                "created_at": "2026-08-01 10:00:00",
            }
        }
        with open(os.path.join(SCRATCH, "data", "users.json"), "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)


def run(code, key=None, cwd=SCRATCH):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if key:
        env["DATA_ENCRYPTION_KEY"] = key
    else:
        env.pop("DATA_ENCRYPTION_KEY", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
    )


# ==================================================================
# 场景 1：缺 key
# ==================================================================
print("\n=== 场景 1: 缺失 DATA_ENCRYPTION_KEY ===")
reset_scratch()
r = run("import auth")
err = r.stderr + r.stdout
check("缺key 启动报错", r.returncode != 0, f"exit={r.returncode}")
check("缺key 报错含密钥名", "DATA_ENCRYPTION_KEY" in err, "")
check("缺key 报错含生成命令", "token_hex" in err or "secrets" in err, "")

# ==================================================================
# 场景 2：首启
# ==================================================================
print("\n=== 场景 2: 首次启动生成加密文件 ===")
reset_scratch()
r = run("import auth; print('USERS', len(auth._users))", key=KEY)
check("首启成功", r.returncode == 0 and "USERS 1" in r.stdout, r.stdout.strip()[:80])
enc = os.path.join(SCRATCH, "secure", "users.json.enc")
if os.path.exists(enc):
    with open(enc, "rb") as f:
        blob = f.read()
    check("加密文件含 MAGIC 头", blob[:8] == b"AGCRYPT1", f"头8字节={blob[:8]!r}")
    check("加密文件无明文 admin 字样", b"admin" not in blob and b"admin123456" not in blob, f"大小={len(blob)}B")
    check("加密文件是二进制(含0字节)", b"\x00" in blob, "")
# sessions 文件在首次登录（写入会话）后才生成
r = run("import auth; ok, msg, d = auth.login_user('admin', 'admin123456'); print('L', ok)",
        key=KEY)
sess = os.path.join(SCRATCH, "secure", "sessions.json.enc")
check("登录成功(触发会话写入)", r.returncode == 0 and "L True" in r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout else "")
check("生成 sessions.json.enc", os.path.exists(sess), "")
if os.path.exists(sess):
    with open(sess, "rb") as f:
        sblob = f.read()
    check("sessions 加密文件含 MAGIC 头", sblob[:8] == b"AGCRYPT1", f"头8字节={sblob[:8]!r}")

# ==================================================================
# 场景 3：错 key
# ==================================================================
print("\n=== 场景 3: 错误密钥 ===")
reset_scratch()
r = run("import auth", key=KEY)
assert r.returncode == 0
enc = os.path.join(SCRATCH, "secure", "users.json.enc")
with open(enc, "rb") as f:
    before = f.read()
r = run("import auth", key=WRONG_KEY)
err = r.stderr + r.stdout
check("错key 启动报错", r.returncode != 0, f"exit={r.returncode}")
check("错key 报错含密钥不匹配提示", ("密钥不匹配" in err) or ("解密" in err and "DATA_ENCRYPTION_KEY" in err), "")
with open(enc, "rb") as f:
    after = f.read()
check("错key 未改动加密文件", before == after, f"{len(before)}B -> {len(after)}B")

# ==================================================================
# 场景 4：篡改
# ==================================================================
print("\n=== 场景 4: 篡改加密文件 ===")
reset_scratch()
r = run("import auth", key=KEY)
assert r.returncode == 0
enc = os.path.join(SCRATCH, "secure", "users.json.enc")
with open(enc, "rb") as f:
    blob = bytearray(f.read())
blob[len(blob) // 2] ^= 0x01  # 翻转中间一个字节
with open(enc, "wb") as f:
    f.write(bytes(blob))
r = run("import auth", key=KEY)
check("篡改后 启动报错", r.returncode != 0, f"exit={r.returncode}")
check("篡改后 报错为 fail-fast", "解密" in (r.stderr + r.stdout), "")

# ==================================================================
# 场景 5：迁移
# ==================================================================
print("\n=== 场景 5: 明文迁移到加密 ===")
reset_scratch(with_users_plaintext=True)
r = run(
    "import auth; print('COUNT', len(auth._users)); "
    "ok, msg, data = auth.login_user('legacy_old', 'migrate123'); "
    "print('LOGIN', ok, msg)", key=KEY)
out = r.stdout
err = r.stderr
check("迁移启动成功", r.returncode == 0 and "COUNT 1" in out, (out or err).strip()[:200])
check("迁移不重复创建 admin", "COUNT 1" in out and "COUNT 2" not in out, "")
check("原用户可登录", "LOGIN True" in out, (out + err).strip().splitlines()[-1] if (out or err) else "")
legacy_bak = os.path.join(SCRATCH, "data", "users.json.migrated.bak")
check("明文文件已改名 .migrated.bak", os.path.exists(legacy_bak), "")
check("明文文件已被移除", not os.path.exists(os.path.join(SCRATCH, "data", "users.json")), "")
check("迁移后生成加密文件", os.path.exists(os.path.join(SCRATCH, "secure", "users.json.enc")), "")

# ==================================================================
# 场景 6：闭环（注册 -> 重启 -> 登录）
# ==================================================================
print("\n=== 场景 6: 注册 -> 重启 -> 登录 ===")
reset_scratch()
r = run(
    "import auth; "
    "ok, msg, u = auth.register_user('loopuser', 'loop123456', '闭环测试', '13900007777', id_card='', role='resident', building='1栋', unit='1单元', room='101', register_lat=30.274150, register_lng=120.155150); "
    "print('REG', ok, msg)", key=KEY)
check("注册成功", r.returncode == 0 and "REG True" in r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout else "")
r = run(
    "import auth; "
    "ok, msg, data = auth.login_user('loopuser', 'loop123456'); "
    "print('LOGIN', ok, msg, bool(data and data.get('token')))", key=KEY)
check("重启后登录成功", r.returncode == 0 and "LOGIN True" in r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout else "")

# ==================================================================
# 汇总
# ==================================================================
print("\n" + "=" * 60)
print(f"  验证结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 60)
for n in PASS:
    print(f"  PASS  {n}")
for n in FAIL:
    print(f"  FAIL  {n}")

# 清理
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(0 if not FAIL else 1)

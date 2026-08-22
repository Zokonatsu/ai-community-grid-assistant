"""
test_cloud_store.py
测试腾讯云 COS 云存储封装（cloud_store.py）与 secure_store 加解密的闭环。

不访问真实网络：monkeypatch cloud_store._get_client 为内存假客户端，
验证 download/upload/ensure_bucket/object_exists/delete_object 的行为与异常语义，
以及 cloudbase 模式下 auth 的空库重建 admin / 会话云端读写闭环。
"""

import io
import importlib
import os
import sys
from unittest.mock import patch

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# 固定本地模式（.env 已为 cloudbase；本文件内云存储用例会临时切 cloudbase）
os.environ["AUTH_STORE"] = "file"

import cloud_store  # noqa: E402
import secure_store  # noqa: E402

# 加密密钥（固定测试值，仅本脚本运行期间使用）
os.environ["DATA_ENCRYPTION_KEY"] = "1" * 64


# ------------------------------------------------------------------
# 内存假客户端
# ------------------------------------------------------------------
class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def get_raw_stream(self) -> io.BytesIO:
        return io.BytesIO(self._data)


class _FakeNotFound(RuntimeError):
    """模拟 COS 服务端 404（对象/桶不存在）。"""


class _FakeClient:
    def __init__(self, bucket: str = "test-bucket"):
        self.bucket = bucket
        self.buckets = {bucket}
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[tuple[str, str, bytes]] = []
        self.create_calls: list[tuple[str, str]] = []  # (Bucket, ACL)

    # --- 对象 ---
    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs) -> None:
        # 与真实 SDK 签名 put_object(Bucket, Body, Key) 保持一致（参数名即关键字名）
        self.put_calls.append((Bucket, Key, bytes(Body)))
        self.objects[Key] = bytes(Body)

    def get_object(self, bucket: str, key: str) -> dict:
        if key in self.objects:
            return {"Body": _FakeBody(self.objects[key])}
        raise _FakeNotFound(f"no such key: {key}")

    def head_object(self, Bucket: str, Key: str) -> dict:
        if Key in self.objects:
            return {}
        raise _FakeNotFound(f"no such key: {Key}")

    def delete_object(self, Bucket: str, Key: str, **kwargs) -> dict:
        if Key in self.objects:
            del self.objects[Key]
            return {}
        raise _FakeNotFound(f"no such key: {Key}")

    # --- 桶 ---
    def head_bucket(self, Bucket: str) -> dict:
        if Bucket in self.buckets:
            return {}
        raise _FakeNotFound(f"no such bucket: {Bucket}")

    def create_bucket(self, Bucket: str, ACL: str = "private", **kwargs) -> dict:
        self.buckets.add(Bucket)
        self.create_calls.append((Bucket, ACL))
        return {}


def _install_fake() -> _FakeClient:
    fake = _FakeClient()
    cloud_store._get_client = lambda: (fake, fake.bucket)
    cloud_store._is_not_found = lambda exc: isinstance(exc, _FakeNotFound)
    return fake


# ------------------------------------------------------------------
# 测试用例
# ------------------------------------------------------------------
def test_upload_download_roundtrip():
    fake = _install_fake()
    data = b"hello-cloud-store-blob"
    cloud_store.upload("users.json.enc", data)
    assert fake.put_calls == [("test-bucket", "users.json.enc", data)], "put_object 参数不符"
    got = cloud_store.download("users.json.enc")
    assert got == data, "下载内容与上传不一致"


def test_download_missing_returns_none():
    _install_fake()
    assert cloud_store.download("no-such-key") is None, "对象不存在应返回 None"


def test_download_error_raises():
    fake = _install_fake()

    def boom(bucket: str, key: str):
        raise RuntimeError("network down")

    fake.get_object = boom
    try:
        cloud_store.download("users.json.enc")
        raise AssertionError("应抛出 CloudStoreError")
    except cloud_store.CloudStoreError:
        pass  # 期望行为


def test_upload_error_raises():
    fake = _install_fake()

    def boom(Bucket: str, Key: str, Body: bytes, **kwargs):
        raise RuntimeError("permission denied")

    fake.put_object = boom
    try:
        cloud_store.upload("k", b"x")
        raise AssertionError("应抛出 CloudStoreError")
    except cloud_store.CloudStoreError:
        pass  # 期望行为


def test_config_missing_raises():
    saved = {
        k: os.environ.get(k)
        for k in ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY")
    }
    for k in saved:
        os.environ.pop(k, None)
    try:
        try:
            cloud_store._config()
            raise AssertionError("缺少配置应抛出 CloudStoreError")
        except cloud_store.CloudStoreError:
            pass  # 期望行为
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_encrypt_upload_download_decrypt():
    """端到端：secure_store 加密 -> 云上传 -> 云下载 -> secure_store 解密，数据一致。"""
    _install_fake()
    users = {"u1": {"username": "alice", "role": "resident", "password_hash": "salt$hash"}}
    blob = secure_store.encrypt("users", users)
    cloud_store.upload("users.json.enc", blob)
    got = cloud_store.download("users.json.enc")
    assert got == blob, "加密 blob 上传/下载应逐字节一致"
    assert secure_store.decrypt("users", got) == users, "解密结果应与原数据一致"


# ------------------------------------------------------------------
# ensure_bucket / object_exists / delete_object
# ------------------------------------------------------------------
def test_ensure_bucket_missing_creates_private():
    """桶不存在 -> 自动创建，ACL 为 private（不开放公共读/写）。"""
    fake = _install_fake()
    fake.buckets = set()  # 桶不存在
    created = cloud_store.ensure_bucket()
    assert created is True, "桶缺失时应返回 True（新建）"
    assert fake.buckets == {"test-bucket"}, "应创建存储桶"
    assert fake.create_calls == [("test-bucket", "private")], "创建时应使用私有 ACL"
    assert "COS_SECRET_ID" not in str(fake.create_calls), "不得记录密钥"


def test_ensure_bucket_exists_skips():
    """桶已存在 -> 跳过创建，返回 False。"""
    fake = _install_fake()
    created = cloud_store.ensure_bucket()
    assert created is False, "桶已存在时应返回 False（跳过）"
    assert fake.create_calls == [], "已存在的桶不应重复创建"


def test_ensure_bucket_error_raises():
    """桶检查遇到非 404 异常 -> fail-fast 抛 CloudStoreError。"""
    fake = _install_fake()

    def boom(Bucket: str):
        raise RuntimeError("auth failed")

    fake.head_bucket = boom
    try:
        cloud_store.ensure_bucket()
        raise AssertionError("应抛出 CloudStoreError")
    except cloud_store.CloudStoreError:
        pass  # 期望行为


def test_object_exists_and_delete():
    """object_exists/delete_object：存在/不存在语义正确，异常 raise。"""
    fake = _install_fake()
    assert cloud_store.object_exists("users.json.enc") is False, "空对象应不存在"
    cloud_store.upload("users.json.enc", b"x")
    assert cloud_store.object_exists("users.json.enc") is True, "上传后应存在"
    assert cloud_store.delete_object("users.json.enc") is True, "删除存在对象应返回 True"
    assert cloud_store.object_exists("users.json.enc") is False, "删除后应不存在"
    assert cloud_store.delete_object("users.json.enc") is False, "删除不存在对象应返回 False"

    def boom(Bucket: str, Key: str, **kwargs):
        raise RuntimeError("network down")

    fake.delete_object = boom
    try:
        cloud_store.delete_object("users.json.enc")
        raise AssertionError("应抛出 CloudStoreError")
    except cloud_store.CloudStoreError:
        pass  # 期望行为


# ------------------------------------------------------------------
# cloudbase 模式：auth 空库重建 admin 上云 + 会话云端读写
# ------------------------------------------------------------------
def _snapshot_secure() -> dict[str, bytes]:
    """快照 secure/ 身份文件内容，用于断言 cloudbase 模式不写本地。"""
    snap = {}
    for name in ("users.json.enc", "sessions.json.enc"):
        path = os.path.join("secure", name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                snap[name] = f.read()
        else:
            snap[name] = None
    return snap


def _reload_auth_cloudbase(fake: _FakeClient):
    """以 cloudbase 模式重载 auth（fake 客户端），触发 _init_auth。"""
    os.environ["AUTH_STORE"] = "cloudbase"
    cloud_store._get_client = lambda: (fake, fake.bucket)
    cloud_store._is_not_found = lambda exc: isinstance(exc, _FakeNotFound)
    import auth
    auth_module = importlib.reload(auth)
    return auth_module


def test_cloud_empty_rebuild_admin_upload():
    """空库重建：云端无对象 -> auth 启动自动创建 admin 并加密上传 users.json.enc。"""
    before = _snapshot_secure()
    fake = _install_fake()
    auth_module = _reload_auth_cloudbase(fake)

    assert cloud_store.USERS_OBJECT_KEY in fake.objects, "admin 用户表应上传到云端"
    assert cloud_store.SESSIONS_OBJECT_KEY in fake.objects, "空会话表也应上传到云端"
    users = secure_store.decrypt("users", fake.objects[cloud_store.USERS_OBJECT_KEY])
    sessions = secure_store.decrypt("sessions", fake.objects[cloud_store.SESSIONS_OBJECT_KEY])
    assert "admin" in [u.get("username") for u in users.values()], "应包含默认 admin"
    assert len(sessions) == 0, "空库启动时会话表应为空"
    # 明文不得落云：云端 blob 是加密字节，不包含明文 JSON 结构
    raw = fake.objects[cloud_store.USERS_OBJECT_KEY]
    assert b'"password_hash"' not in raw, "云端对象必须是加密 blob，不得含明文"

    # cloudbase 模式不写本地 secure/
    after = _snapshot_secure()
    assert after == before, "cloudbase 模式不得改写本地 secure/ 身份文件"
    # 恢复 file 模式，避免影响后续用例
    os.environ["AUTH_STORE"] = "file"
    assert auth_module is not None


def test_cloud_session_write_read_delete():
    """会话上云：登录写云端 sessions、get_current_user 校验、登出删除。"""
    fake = _install_fake()
    auth_module = _reload_auth_cloudbase(fake)

    ok, msg, data = auth_module.login_user("admin", "GridAdmin2025!@#")
    assert ok, f"admin 登录应成功：{msg}"
    token = data["token"]
    sessions = secure_store.decrypt("sessions", fake.objects[cloud_store.SESSIONS_OBJECT_KEY])
    assert token in sessions, "登录后会话应写入云端 sessions.json.enc"
    assert sessions[token]["user_id"] == data["user"]["id"]

    me = auth_module.get_current_user(token)
    assert me is not None and me["username"] == "admin", "云端会话应能通过 get_current_user 校验"

    assert auth_module.logout_user(token) is True, "登出应成功"
    sessions2 = secure_store.decrypt("sessions", fake.objects[cloud_store.SESSIONS_OBJECT_KEY])
    assert token not in sessions2, "登出后云端会话应删除"
    assert auth_module.get_current_user(token) is None, "已登出 token 应失效"

    os.environ["AUTH_STORE"] = "file"


def test_cloud_register_resident_upload():
    """新注册居民上云：注册写云端 users.json.enc，解密可验证新用户。"""
    fake = _install_fake()
    auth_module = _reload_auth_cloudbase(fake)

    import geo
    original = geo.is_within_community
    geo.is_within_community = lambda lat, lng: (True, 0.0)  # 与坐标解耦（离线）
    try:
        ok, msg, user = auth_module.register_user(
            username="cloud_res_001", password="test123456", real_name="云端测试居民",
            phone="13900001234", id_card="", building="1栋", unit="1单元", room="101",
            register_lat=28.368178, register_lng=121.356875,
        )
    finally:
        geo.is_within_community = original
    assert ok, f"注册应成功：{msg}"
    users = secure_store.decrypt("users", fake.objects[cloud_store.USERS_OBJECT_KEY])
    usernames = [u.get("username") for u in users.values()]
    assert "cloud_res_001" in usernames, "云端 users.json.enc 应包含新注册居民"
    assert "admin" in usernames, "默认 admin 应保留在云端"
    raw = fake.objects[cloud_store.USERS_OBJECT_KEY]
    assert b"cloud_res_001" not in raw, "云端对象必须是加密 blob，不得含明文用户名"

    os.environ["AUTH_STORE"] = "file"


# ------------------------------------------------------------------
# 覆盖率补充（T20260820-001-TB）：真实 _get_client / _is_not_found / 读响应异常等分支
# ------------------------------------------------------------------
_ORIG_GET_CLIENT = cloud_store._get_client    # 真实函数（模块导入期捕获）
_ORIG_IS_NOT_FOUND = cloud_store._is_not_found  # 真实判定函数


def test_get_client_real_path():
    """覆盖 cloud_store._get_client 真实路径（qcloud_cos 惰性导入 + 配置校验 + 幂等缓存）。"""
    saved = {k: os.environ.get(k) for k in
             ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY")}
    saved_state = cloud_store._client_state
    saved_get_client = cloud_store._get_client
    cloud_store._client_state = None
    cloud_store._get_client = _ORIG_GET_CLIENT
    for k, v in {"COS_REGION": "ap-guangzhou", "COS_BUCKET": "test-bucket",
                 "COS_SECRET_ID": "test-id", "COS_SECRET_KEY": "test-key"}.items():
        os.environ[k] = v
    try:
        client, bucket = cloud_store._get_client()
        assert bucket == "test-bucket", bucket
        assert client is not None
        client2, bucket2 = cloud_store._get_client()
        assert client2 is client and bucket2 == bucket, "应命中缓存"
    finally:
        cloud_store._client_state = saved_state
        cloud_store._get_client = saved_get_client
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_get_client_sdk_missing():
    """SDK 未安装 -> 清晰报错（ImportError 分支）。"""
    saved = {k: os.environ.get(k) for k in
             ("COS_REGION", "COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY")}
    saved_state = cloud_store._client_state
    saved_get_client = cloud_store._get_client
    cloud_store._client_state = None
    cloud_store._get_client = _ORIG_GET_CLIENT
    for k, v in {"COS_REGION": "ap-guangzhou", "COS_BUCKET": "test-bucket",
                 "COS_SECRET_ID": "test-id", "COS_SECRET_KEY": "test-key"}.items():
        os.environ[k] = v
    real_import = __import__

    def fake_import(name, *a, **k):
        if name.startswith("qcloud_cos"):
            raise ImportError("simulate sdk missing")
        return real_import(name, *a, **k)

    try:
        with patch("builtins.__import__", side_effect=fake_import):
            try:
                cloud_store._get_client()
                raise AssertionError("SDK 缺失应抛出 CloudStoreError")
            except cloud_store.CloudStoreError as exc:
                assert "缺少腾讯云 COS SDK" in str(exc), exc
    finally:
        cloud_store._client_state = saved_state
        cloud_store._get_client = saved_get_client
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_is_not_found_real():
    """覆盖真实 _is_not_found：404 判定 / 非 404 / 非 CosServiceError / SDK 缺失。"""
    from qcloud_cos.cos_exception import CosServiceError
    saved = cloud_store._is_not_found
    cloud_store._is_not_found = _ORIG_IS_NOT_FOUND
    try:
        assert cloud_store._is_not_found(CosServiceError("GET", "no such key", 404)) is True
        assert cloud_store._is_not_found(CosServiceError("GET", "internal", 500)) is False
        assert cloud_store._is_not_found(RuntimeError("network")) is False

        class BadStatus(CosServiceError):
            def get_status_code(self):
                raise ValueError("bad status")

        assert cloud_store._is_not_found(BadStatus("GET", "bad", 404)) is False
        real_import = __import__

        def fake_import(name, *a, **k):
            if name.startswith("qcloud_cos"):
                raise ImportError("simulate sdk missing")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            assert cloud_store._is_not_found(RuntimeError("x")) is False
    finally:
        cloud_store._is_not_found = saved


def test_download_read_error():
    """下载响应体读取失败 -> CloudStoreError（不静默返回空）。"""
    fake = _install_fake()

    class BadBody:
        def get_raw_stream(self):
            raise RuntimeError("stream read failed")

    fake.get_object = lambda bucket, key: {"Body": BadBody()}
    try:
        cloud_store.download("k")
        raise AssertionError("响应读取失败应抛出 CloudStoreError")
    except cloud_store.CloudStoreError:
        pass


def test_ensure_bucket_create_error():
    """创建桶失败 -> CloudStoreError（fail-fast）。"""
    fake = _install_fake()
    fake.buckets = set()  # 桶不存在 -> head_bucket 404 -> 尝试创建

    def boom(Bucket, ACL="private", **kwargs):
        raise RuntimeError("create bucket denied")

    fake.create_bucket = boom
    try:
        cloud_store.ensure_bucket()
        raise AssertionError("创建桶失败应抛出 CloudStoreError")
    except cloud_store.CloudStoreError:
        pass


def test_object_exists_error():
    """对象检查非 404 异常 -> CloudStoreError。"""
    fake = _install_fake()

    def boom(Bucket, Key):
        raise RuntimeError("network down")

    fake.head_object = boom
    try:
        cloud_store.object_exists("k")
        raise AssertionError("应抛出 CloudStoreError")
    except cloud_store.CloudStoreError:
        pass


# ------------------------------------------------------------------
# 汇总
# ------------------------------------------------------------------
def main() -> int:
    tests = [
        ("上传/下载字节往返", test_upload_download_roundtrip),
        ("下载不存在对象返回 None", test_download_missing_returns_none),
        ("下载异常 raise", test_download_error_raises),
        ("上传异常 raise", test_upload_error_raises),
        ("配置缺失 raise", test_config_missing_raises),
        ("加密→上传→下载→解密闭环", test_encrypt_upload_download_decrypt),
        ("ensure_bucket 缺失创建（私有 ACL）", test_ensure_bucket_missing_creates_private),
        ("ensure_bucket 已存在跳过", test_ensure_bucket_exists_skips),
        ("ensure_bucket 异常 raise", test_ensure_bucket_error_raises),
        ("object_exists/delete_object 语义", test_object_exists_and_delete),
        ("空库重建 admin 上云 + 不写本地 secure", test_cloud_empty_rebuild_admin_upload),
        ("会话云端写/读/删闭环", test_cloud_session_write_read_delete),
        ("新注册居民身份上云（加密）", test_cloud_register_resident_upload),
        ("真实 _get_client 路径/缓存", test_get_client_real_path),
        ("SDK 缺失报错", test_get_client_sdk_missing),
        ("真实 _is_not_found 判定", test_is_not_found_real),
        ("下载响应读取异常", test_download_read_error),
        ("创建桶失败异常", test_ensure_bucket_create_error),
        ("对象检查异常", test_object_exists_error),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [PASS] {name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}：{exc!r}", flush=True)
    total = len(tests)
    print(f"TEST SUMMARY: {total - failed} PASS / {failed} FAIL", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

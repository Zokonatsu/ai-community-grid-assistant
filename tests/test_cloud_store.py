"""
test_cloud_store.py
测试腾讯云 COS 云存储封装（cloud_store.py）与 secure_store 加解密的闭环。

不访问真实网络：monkeypatch cloud_store._get_client 为内存假客户端，
验证 download/upload 的行为与异常语义。
"""

import io
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

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
    """模拟 COS 服务端 404（对象不存在）。"""


class _FakeClient:
    def __init__(self):
        self.bucket = "test-bucket"
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[tuple[str, str, bytes]] = []

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs) -> None:
        # 与真实 SDK 签名 put_object(Bucket, Body, Key) 保持一致（参数名即关键字名）
        self.put_calls.append((Bucket, Key, bytes(Body)))
        self.objects[Key] = bytes(Body)

    def get_object(self, bucket: str, key: str) -> dict:
        if key in self.objects:
            return {"Body": _FakeBody(self.objects[key])}
        raise _FakeNotFound(f"no such key: {key}")


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

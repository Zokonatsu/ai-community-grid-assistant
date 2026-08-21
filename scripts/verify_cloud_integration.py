# -*- coding: utf-8 -*-
"""
scripts/verify_cloud_integration.py
云端集成冒烟验证（cloud-integration.yml 手动 workflow 调用；本地可离线单测）。

流程（只操作临时对象，绝不触碰 users.json.enc / sessions.json.enc）：
    1) ensure_bucket()               确保 COS 存储桶存在（私有权限）
    2) upload(encrypt(payload))      上传「加密后的临时冒烟数据」
    3) download + decrypt 比对        下载并解密，与上传前内容比对
    4) delete_object()               删除临时对象（finally 兜底清理）

临时对象命名：_ci_verify_<uuid>.enc；payload 仅含 purpose/ts/nonce，无任何用户数据。

安全约定：
- 绝不读/写 users.json.enc、sessions.json.enc；
- 任何输出不含密钥（COS_SECRET_* / DATA_ENCRYPTION_KEY 值一律不打印）；
- 失败退出码非 0。

离线单测（不触网）：--offline 会 monkeypatch cloud_store._get_client 为内存 fake client，
并用固定测试密钥跑同一套逻辑；真实云端由 GitHub Actions workflow_dispatch 执行。

说明：secure_store.encrypt/decrypt 的 kind 参数仅支持 users/sessions（AAD 域隔离），
本脚本以 kind="users" 复用与真实账号数据完全相同的 AES-256-GCM 加解密路径；
由于对象 key 为独立临时对象 _ci_verify_<uuid>.enc，不会影响任何真实数据。
"""
import os
import sys
import uuid
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import cloud_store  # noqa: E402
import secure_store  # noqa: E402


class _FakeStream:
    """模拟 COS 响应 Body.get_raw_stream().read()。"""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk, self._pos = self._data[self._pos:], len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def get_raw_stream(self) -> _FakeStream:
        return _FakeStream(self._data)


class _FakeClient:
    """内存版 COS 客户端：仅实现 cloud_store 用到的接口，不触网。"""

    def __init__(self):
        self.store = {}

    def head_bucket(self, Bucket=None, **kwargs):
        return {}  # 视为存储桶已存在

    def create_bucket(self, Bucket=None, **kwargs):
        return {}

    def put_object(self, Bucket=None, Key=None, Body=None, **kwargs):
        self.store[Key] = bytes(Body)

    def get_object(self, Bucket=None, Key=None, **kwargs):
        if Key not in self.store:
            raise KeyError(f"NoSuchKey: {Key}")
        return {"Body": _FakeBody(self.store[Key])}

    def delete_object(self, Bucket=None, Key=None, **kwargs):
        self.store.pop(Key, None)

    def head_object(self, Bucket=None, Key=None, **kwargs):
        if Key not in self.store:
            raise KeyError(f"NoSuchKey: {Key}")
        return {}


def _install_fake_client() -> None:
    """离线单测：把 cloud_store._get_client 替换为内存 fake。"""
    fake = _FakeClient()
    cloud_store._get_client = lambda: (fake, "ci-verify-fake-bucket")


def run_verify() -> None:
    """执行云端冒烟验证（真实云端或 offline fake 共用同一逻辑）。"""
    key = f"_ci_verify_{uuid.uuid4().hex}.enc"
    payload = {
        "purpose": "ci-verify",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nonce": uuid.uuid4().hex,
    }
    error = None
    uploaded = False
    try:
        cloud_store.ensure_bucket()
        blob = secure_store.encrypt("users", payload)  # kind=users：复用正式加密路径
        cloud_store.upload(key, blob)
        uploaded = True
        downloaded = cloud_store.download(key)
        if downloaded is None:
            raise RuntimeError("下载临时对象返回 None（对象不可读）")
        decrypted = secure_store.decrypt("users", downloaded)
        if decrypted != payload:
            raise RuntimeError("解密后内容与上传前不一致")
        print(f"[PASS] 云集成验证通过：临时对象 {key} 上传/下载/解密比对 全链路 OK（{len(blob)} 字节）")
    except Exception as exc:
        # 安全：只报异常类型 + 通用提示，绝不打印异常详情中可能含有的密钥/请求信息
        print(f"[FAIL] 云集成验证失败（{type(exc).__name__}）："
              f"请检查 COS 配置/网络/密钥权限；本工具不打印任何密钥")
        error = exc
    finally:
        if uploaded:
            try:
                cloud_store.delete_object(key)
                print(f"[INFO] 已清理临时对象：{key}")
            except Exception:
                print("[WARN] 临时对象清理失败（不影响结论，请人工核对云端）")
    if error is not None:
        raise SystemExit(1)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    offline = "--offline" in argv
    if offline:
        os.environ.setdefault("DATA_ENCRYPTION_KEY", "1" * 64)  # 固定测试密钥，非真实值
        _install_fake_client()
        print("[offline] 已启用内存 fake client（不触网），开始逻辑验证…")
    run_verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())
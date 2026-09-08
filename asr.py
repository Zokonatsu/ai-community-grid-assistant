# -*- coding: utf-8 -*-
"""
asr.py
腾讯云语音识别（一句话识别 SentenceRecognition）

- 仅用标准库实现（hmac/hashlib/json/base64/urllib），不引入额外 pip 依赖；
- 密钥读环境变量 ASR_SECRET_ID / ASR_SECRET_KEY / ASR_REGION（放 .env，勿入库）。
- 每次调用同步返回识别文本；失败抛异常，由调用方降级为「待审核」。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("asr")

SERVICE = "asr"
HOST = "asr.tencentcloudapi.com"
ACTION = "SentenceRecognition"
VERSION = "2019-06-14"
ENDPOINT = "https://asr.tencentcloudapi.com/"

# 扩展名 -> VoiceFormat（腾讯云一句话识别支持的音频格式）
VOICE_FORMATS = {
    "wav": "wav",
    "mp3": "mp3",
    "m4a": "m4a",
    "ogg": "ogg",
    "amr": "amr",
    "silk": "silk",
    "aac": "aac",
    "webm": "webm",  # 部分腾讯云接口可能不支持；失败时降级待审核
}


def _secret() -> tuple[str, str, str]:
    sid = os.getenv("ASR_SECRET_ID", "").strip()
    skey = os.getenv("ASR_SECRET_KEY", "").strip()
    region = os.getenv("ASR_REGION", "ap-guangzhou").strip()
    if not sid or not skey:
        raise RuntimeError("未配置腾讯云语音识别密钥(ASR_SECRET_ID/ASR_SECRET_KEY)")
    return sid, skey, region


def _tc3_sign(params: dict) -> dict:
    """构造 TC3-HMAC-SHA256 请求头与 body。"""
    sid, skey, region = _secret()
    payload = json.dumps(params).encode("utf-8")
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    # 1. 拼接规范请求串
    ct = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{ct}\nhost:{HOST}\nx-tc-action:{ACTION.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    hashed_payload = hashlib.sha256(payload).hexdigest()
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"

    # 2. 拼接待签名字符串
    credential_scope = f"{date}/{SERVICE}/tc3_request"
    hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n{hashed_canonical}"

    # 3. 计算签名
    secret_date = hmac.new(("TC3" + skey).encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
    secret_service = hmac.new(secret_date, SERVICE.encode("utf-8"), hashlib.sha256).digest()
    secret_signing = hmac.new(secret_service, "tc3_request".encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={sid}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": ct,
        "Host": HOST,
        "X-TC-Action": ACTION,
        "X-TC-Version": VERSION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
    }
    return headers, payload


def transcribe(data: bytes, ext: str = "") -> str:
    """把一段音频转成中文文本。失败抛异常（调用方降级为待审核）。"""
    ext = (ext or "").lower()
    voice_format = VOICE_FORMATS.get(ext)
    if ext not in VOICE_FORMATS:
        logger.warning("不支持的音频格式，跳过转写：%s", ext)
        return ""
    if not data:
        return ""
    if len(data) > 10 * 1024 * 1024:
        logger.warning("音频过大，跳过转写：%d 字节", len(data))
        return ""
    params = {
        "ProjectId": 0,
        "SubServiceType": 2,
        "EngSerViceType": "16k_zh",
        "SourceType": 1,
        "VoiceFormat": voice_format,
        "Data": base64.b64encode(data).decode("ascii"),
        "DataLen": len(data),
    }
    headers, payload = _tc3_sign(params)
    req = urllib.request.Request(ENDPOINT, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    resp_body = body.get("Response", {})
    err = resp_body.get("Error")
    if err:
        raise RuntimeError(f"腾讯云ASR失败: {err.get('Code')} - {err.get('Message')}")
    result = resp_body.get("Result", "")
    logger.info("ASR 转写完成，%d 字：%s", len(result), result[:60])
    return (result or "").strip()

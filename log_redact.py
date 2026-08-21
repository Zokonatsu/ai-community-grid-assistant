# -*- coding: utf-8 -*-
"""
log_redact.py — 日志 PII 脱敏（问题 10）

背景：事件描述/地址可能内嵌手机号、身份证号等个人信息，直接写入日志会形成
明文 PII 泄露（合规风险）。本模块提供统一的掩码函数，所有记录用户输入的日志
调用点都应经过 redact_pii() 后再落盘。

规则（可扩展）：
- 身份证号（18 位：17 数字 + 数字/X）：保留前 6 + 后 4，中间 8 位掩码
- 大陆手机号（11 位，1[3-9] 开头）：保留前 3 + 后 4，中间 4 位掩码
- 处理顺序：先掩身份证再掩手机号，避免身份证内的 11 位数字段被手机号规则误伤
- 非结构化内容（如具体地址）无法用正则可靠掩码，仅对其中能识别的 PII 模式脱敏

开关：config.LOG_REDACT（环境变量 LOG_REDACT，默认 true）。
  关闭时原样返回，便于本地调试看原始内容。
"""
from __future__ import annotations

import re

from config import LOG_REDACT

# 身份证：6 位地区 + 8 位生日(掩) + 3 位顺序(留) + 1 位校验（数字/X/x）
_IDCARD_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
# 大陆手机号：1[3-9] 开头共 11 位（保留前 3 + 后 4）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def _mask_idcard(match: re.Match) -> str:
    return f"{match.group(1)}********{match.group(2)}"


def _mask_phone(match: re.Match) -> str:
    return f"{match.group(1)}****{match.group(2)}"


def redact_pii(text: str | None) -> str | None:
    """对文本中的模式化 PII（身份证/手机号）做掩码；None 原样返回。

    开关关闭（LOG_REDACT=false）时不做任何处理，便于本地调试。
    """
    if not LOG_REDACT or text is None:
        return text
    if not isinstance(text, str):
        text = str(text)
    text = _IDCARD_RE.sub(_mask_idcard, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return text
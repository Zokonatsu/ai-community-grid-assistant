# -*- coding: utf-8 -*-
"""
log_redact 脱敏单元测试（问题 10：日志 PII 不落明文）。

覆盖：手机号/身份证掩码、身份证优先于手机号（避免身份证内 11 位数字段被误伤）、
无 PII 文本原样、None / 非字符串入参、LOG_REDACT=false 关闭开关。
"""
import pytest

import log_redact
from log_redact import redact_pii


def test_phone_masked():
    assert redact_pii("联系电话 13812345678，请尽快处理") == "联系电话 138****5678，请尽快处理"


def test_phone_short_number_untouched():
    # 非 11 位 / 非 1[3-9] 开头的数字不应被误掩
    assert redact_pii("门牌号 502，电话 12345") == "门牌号 502，电话 12345"


def test_idcard_masked():
    assert redact_pii("身份证 11010119900307781X 已登记") == "身份证 110101********781X 已登记"


def test_idcard_lowercase_x():
    assert "110101********781x" in redact_pii("证件号 11010119900307781x")


def test_idcard_processed_before_phone():
    # 身份证内含有 11 位数字段（19900307781），必须先掩身份证，避免被手机号规则二次误伤
    out = redact_pii("身份证 11010119900307781X")
    assert "1990****7781" not in out
    assert out == "身份证 110101********781X"


def test_plain_text_unchanged():
    text = "幸福小区3栋502室 下水道堵塞，请尽快处理"
    assert redact_pii(text) == text


def test_none_and_nonstr():
    assert redact_pii(None) is None
    assert redact_pii(12345) == "12345"
    assert redact_pii(13812345678) == "138****5678"


def test_disabled_returns_original(monkeypatch):
    monkeypatch.setattr(log_redact, "LOG_REDACT", False)
    assert redact_pii("电话 13812345678") == "电话 13812345678"
    monkeypatch.setattr(log_redact, "LOG_REDACT", True)


def test_multiple_pii_in_one_text():
    out = redact_pii("手机13812345678 身份证11010119900307781X 手机13900001111")
    assert out.count("****") == 4  # 两个手机号各 1 组 **** + 身份证 1 组 ********
    assert "13812345678" not in out
    assert "11010119900307781X" not in out
    assert "13900001111" not in out
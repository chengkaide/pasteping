"""cleaner 模块单元测试（纯逻辑，无 GUI、无剪贴板依赖）。"""

from __future__ import annotations

import os
import sys

import pytest

# 保证无论从哪个工作目录运行 pytest，都能 import 到项目根目录下的模块。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import cleaner  # noqa: E402
import detector  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_undo():
    """每个用例前后清空撤销快照。"""
    cleaner.clear_undo()
    yield
    cleaner.clear_undo()


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def test_clean_none_returns_empty_result():
    result = cleaner.clean(None)
    assert result.original_text == ""
    assert result.cleaned_text == ""
    assert result.removed == []
    assert result.skipped is False


def test_clean_benign_text_no_change():
    text = "会议纪要：本次例会明确了三点安排，各部门按计划推进即可。"
    result = cleaner.clean(text)
    assert result.cleaned_text == text
    assert result.removed == []
    assert result.skipped is False
    assert cleaner.has_undo() is False


# --------------------------------------------------------------------------- #
# AI 残留 / 内部批注：整句删除
# --------------------------------------------------------------------------- #
def test_clean_deletes_ai_tail_sentence_and_internal_note():
    text = "希望对你有帮助。这是正文。勿外传。联系电话 13800138000。"
    result = cleaner.clean(text)
    assert result.skipped is False
    assert "希望对你有帮助" not in result.cleaned_text
    assert "勿外传" not in result.cleaned_text
    assert "这是正文" in result.cleaned_text
    assert "［已移除：手机号］" in result.cleaned_text
    assert "13800138000" not in result.cleaned_text


def test_clean_deletes_bracket_note():
    text = "【内部批注】这段不要对外发送。" + "正文内容" * 20 + "。"
    result = cleaner.clean(text)
    assert result.skipped is False
    assert "【内部批注】" not in result.cleaned_text
    assert "这段不要对外发送" not in result.cleaned_text


# --------------------------------------------------------------------------- #
# 中段命中（全文生效，不只看首尾 200 字）
# --------------------------------------------------------------------------- #
def test_clean_handles_middle_internal_note():
    text = "甲" * 300 + "。" + "勿外传" + "。" + "乙" * 300
    result = cleaner.clean(text)
    assert result.skipped is False
    assert "勿外传" not in result.cleaned_text
    assert len(result.cleaned_text) == len(text) - 4  # 删除「勿外传。」


def test_clean_handles_middle_sensitive():
    text = "甲" * 300 + "。" + "身份证 11010519491231002X" + "。" + "乙" * 300
    result = cleaner.clean(text)
    assert result.skipped is False
    assert "11010519491231002X" not in result.cleaned_text
    assert "［已移除：身份证］" in result.cleaned_text


def test_find_spans_returns_middle_offsets():
    text = "甲" * 300 + "。" + "勿外传" + "。" + "乙" * 300
    spans = detector.find_spans(text)
    internal = [s for s in spans if s.category == detector.CATEGORY_INTERNAL_NOTE]
    assert internal, "中段内部批注应能被 find_spans 命中"
    span = internal[0]
    assert text[span.start:span.end] == "勿外传。"


# --------------------------------------------------------------------------- #
# 敏感信息：替换为占位符（保留结构）
# --------------------------------------------------------------------------- #
def test_clean_replaces_phone_with_placeholder():
    result = cleaner.clean("电话 13800138000。")
    assert result.cleaned_text == "电话 ［已移除：手机号］。"
    assert result.summary_label == "敏感信息"


def test_clean_replaces_apikey_with_placeholder():
    result = cleaner.clean("token = sk-abcdefgh12345678 请勿外泄")
    assert "sk-abcdefgh12345678" not in result.cleaned_text
    assert "［已移除：API Key］" in result.cleaned_text


def test_clean_replaces_bankcard_with_placeholder():
    result = cleaner.clean("卡号 6222021234567890 请转账")
    assert "6222021234567890" not in result.cleaned_text
    assert "［已移除：银行卡］" in result.cleaned_text


def test_clean_replaces_idcard_not_bankcard():
    result = cleaner.clean("证件 11010519491231002X")
    assert "［已移除：身份证］" in result.cleaned_text


# --------------------------------------------------------------------------- #
# 安全阀
# --------------------------------------------------------------------------- #
def test_clean_skips_when_removal_exceeds_ratio():
    text = "勿外传"  # 整段即命中，移除比例 100% > 60%
    result = cleaner.clean(text)
    assert result.skipped is True
    assert result.cleaned_text == text
    assert result.cleaned_text == result.original_text
    assert cleaner.has_undo() is False


def test_clean_does_not_skip_below_ratio():
    text = "正文正文正文正文正文正文正文正文正文。勿外传。"
    result = cleaner.clean(text)
    assert result.skipped is False
    assert "勿外传" not in result.cleaned_text


# --------------------------------------------------------------------------- #
# 敏感信息不被整句删除吞掉（clamp）
# --------------------------------------------------------------------------- #
def test_clean_clamps_internal_sentence_around_sensitive():
    text = "勿外传 电话13800138000。"
    result = cleaner.clean(text)
    assert "勿外传" not in result.cleaned_text
    assert "［已移除：手机号］" in result.cleaned_text


# --------------------------------------------------------------------------- #
# 撤销
# --------------------------------------------------------------------------- #
def test_undo_restores_original():
    text = "希望对你有帮助。" + "这是正文内容。" * 10
    result = cleaner.clean(text)
    assert result.cleaned_text != text
    assert cleaner.has_undo() is True

    undone = cleaner.undo()
    assert undone is not None
    assert undone.cleaned_text == text
    # 撤销后快照被消费，只能撤销最近一次。
    assert cleaner.has_undo() is False
    assert cleaner.undo() is None


def test_undo_after_skipped_clean_is_none():
    cleaner.clean("勿外传")
    assert cleaner.has_undo() is False

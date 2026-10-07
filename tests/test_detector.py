"""detector 模块单元测试（纯逻辑，无 GUI / 无剪贴板依赖）。"""

from __future__ import annotations

import os
import sys

# 保证无论从哪个工作目录运行 pytest，都能 import 到项目根目录下的模块。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import detector  # noqa: E402
from detector import Hit  # noqa: E402


# --------------------------------------------------------------------------- #
# 空值处理
# --------------------------------------------------------------------------- #
def test_none_returns_none():
    assert detector.detect(None) is None


def test_empty_returns_none():
    assert detector.detect("") is None


def test_whitespace_returns_none():
    assert detector.detect("   ") is None


# --------------------------------------------------------------------------- #
# 第一类：内部批注
# --------------------------------------------------------------------------- #
def test_internal_keyword_head():
    hit = detector.detect("转发文案：请在群内统一发布新品")
    assert hit is not None
    assert hit.category == "internal_note"
    assert hit.category_label == "内部批注"
    assert hit.position == "head"


def test_internal_keyword_tail():
    text = "正文内容" * 60 + "勿外传"
    hit = detector.detect(text)
    assert hit is not None
    assert hit.category == "internal_note"
    assert hit.position == "tail"


def test_internal_regex_keyword():
    hit = detector.detect("本文件仅供内部参考，谢谢配合。")
    assert hit is not None
    assert hit.category == "internal_note"


def test_internal_confirm_keyword():
    # 降误报修订（2026-10-06）：`请确认` 属低区分度关键词，需同一窗口出现**强**语义词
    # 才判定，否则常规业务流程里的「请确认…」会被误报（基准集 2 条）。
    hit = detector.detect("内部稿，请尽快确认后回复")
    assert hit is not None
    assert hit.category == "internal_note"


def test_low_signal_keyword_alone_not_flagged():
    # 无强语义词的裸「请尽快确认」不再提醒（降误报修订，见 docs/false-positive-benchmark.md）。
    assert detector.detect("请尽快确认后回复") is None


def test_internal_bracket_pattern():
    hit = detector.detect("【内部批注】这段不要对外发送")
    assert hit is not None
    assert hit.category == "internal_note"


def test_internal_quote_pattern():
    hit = detector.detect("> 这是引用过来的批注\n正文")
    assert hit is not None
    assert hit.category == "internal_note"


def test_internal_dash_pattern():
    hit = detector.detect("价格待定 —— 待财务确认后再对外")
    assert hit is not None
    assert hit.category == "internal_note"


def test_internal_keyword_in_middle_not_detected():
    text = "A" * 250 + "转发文案" + "B" * 250
    assert detector.detect(text) is None


def test_internal_normal_note_not_flagged():
    text = "这是普通的会议纪要，没有任何批注标记，请正常查看。"
    assert detector.detect(text) is None


def test_internal_fragment_truncated_and_single_line():
    # 「——」补充说明模式最长可命中 62 字，超过 60 字上限时应被截断并追加省略号。
    hit = detector.detect("备注 ——" + "补" * 60)
    assert hit is not None
    assert hit.category == "internal_note"
    assert "\n" not in hit.fragment
    assert len(hit.fragment) <= 61  # 60 字 + 省略号


# --------------------------------------------------------------------------- #
# 第二类：AI 残留
# --------------------------------------------------------------------------- #
def test_ai_head_feature():
    hit = detector.detect("当然可以，下面是为你整理的方案：")
    assert hit is not None
    assert hit.category == "ai_residue"
    assert hit.category_label == "AI 残留"
    assert hit.position == "head"


def test_ai_head_following():
    hit = detector.detect("以下是整理好的内容")
    assert hit is not None
    assert hit.category == "ai_residue"


def test_ai_tail_feature():
    text = "项目的结论是这样。" + "补充说明" * 60 + "希望对你有帮助"
    hit = detector.detect(text)
    assert hit is not None
    assert hit.category == "ai_residue"
    assert hit.position == "tail"


def test_ai_in_middle_not_detected():
    text = "内容" * 150 + "以下是" + "结尾" * 150
    assert detector.detect(text) is None


def test_ai_tail_feature_too_early_not_detected():
    # 结尾特征出现在 tail 区间但不在最后 120 字内，不应命中。
    text = "希望对你有帮助" + "后续正文" * 100
    assert detector.detect(text) is None


# --------------------------------------------------------------------------- #
# 第三类：敏感信息
# --------------------------------------------------------------------------- #
def test_idcard_detected():
    hit = detector.detect("身份证号 11010519491231002X 请核对")
    assert hit is not None
    assert hit.category == "sensitive"
    assert hit.category_label == "敏感信息"
    assert hit.rule == "身份证"
    assert hit.position == "body"


def test_phone_detected():
    hit = detector.detect("联系电话 13800138000 谢谢")
    assert hit is not None
    assert hit.category == "sensitive"
    assert hit.rule == "手机号"


def test_apikey_detected():
    hit = detector.detect("key = sk-abcdefgh12345678")
    assert hit is not None
    assert hit.category == "sensitive"
    assert hit.rule == "API Key"


def test_bankcard_detected():
    hit = detector.detect("卡号 6222021234567890 请转账")
    assert hit is not None
    assert hit.category == "sensitive"
    assert hit.rule == "银行卡"


def test_15_digit_not_detected():
    assert detector.detect("编号 123456789012345") is None


def test_20_digit_not_detected():
    assert detector.detect("编号 12345678901234567890") is None


def test_phone_adjacent_digits_not_detected():
    assert detector.detect("订单号 9913800138000") is None


def test_email_not_detected():
    assert detector.detect("邮箱 a@b.com 谢谢") is None


def test_sensitive_fragment_is_masked():
    hit = detector.detect("电话 13800138000")
    assert hit is not None
    assert "*" in hit.fragment
    assert "13800138000" not in hit.fragment


def test_idcard_fragment_is_masked():
    hit = detector.detect("身份证 11010519491231002X")
    assert hit is not None
    assert "*" in hit.fragment
    assert "11010519491231002X" not in hit.fragment


def test_apikey_fragment_is_masked():
    hit = detector.detect("token sk-abcdefgh12345678")
    assert hit is not None
    assert "*" in hit.fragment
    assert "sk-abcdefgh12345678" not in hit.fragment


def test_bankcard_does_not_duplicate_idcard():
    hit = detector.detect("证件 11010519491231002X")
    assert hit is not None
    assert hit.rule == "身份证"


def test_sensitive_returns_first_match_by_position():
    hit = detector.detect("先出现手机号 13800138000，再出现身份证 11010519491231002X")
    assert hit is not None
    assert hit.rule == "手机号"


# --------------------------------------------------------------------------- #
# 优先级
# --------------------------------------------------------------------------- #
def test_priority_internal_over_ai():
    # 文本需含强语义词，`请确认` 才会被判为内部批注（降误报修订）。
    hit = detector.detect("以下是内容。内部稿，请尽快确认。")
    assert hit is not None
    assert hit.category == "internal_note"


def test_priority_sensitive_over_internal():
    hit = detector.detect("请确认：电话 13800138000")
    assert hit is not None
    assert hit.category == "sensitive"


# --------------------------------------------------------------------------- #
# 长文本 / 性能
# --------------------------------------------------------------------------- #
def test_long_text_middle_keyword_not_detected():
    text = "段落内容" * 1000 + "转发文案" + "后续内容" * 1000
    assert len(text) > 5000
    assert detector.detect(text) is None


def test_long_text_head_keyword_detected():
    text = "勿外传" + "正文" * 2000
    hit = detector.detect(text)
    assert hit is not None
    assert hit.position == "head"


def test_long_text_sensitive_in_middle_detected():
    text = "前缀说明" * 500 + " 13800138000 " + "后缀说明" * 500
    hit = detector.detect(text)
    assert hit is not None
    assert hit.category == "sensitive"
    assert hit.position == "body"


# --------------------------------------------------------------------------- #
# Hit 结构
# --------------------------------------------------------------------------- #
def test_hit_fields_complete():
    hit = detector.detect("转发文案：示例")
    assert isinstance(hit, Hit)
    assert isinstance(hit.category, str)
    assert isinstance(hit.category_label, str)
    assert isinstance(hit.rule, str)
    assert isinstance(hit.fragment, str)
    assert isinstance(hit.position, str)

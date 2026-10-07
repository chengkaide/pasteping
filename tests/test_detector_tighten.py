"""降误报杠杆（``detector.TIGHTEN``）的独立覆盖。

背景：QA 基准实测误报率 58.3%（21/36 干净样本），据此按 ``tools/fp_lab.py`` 的全组合
实测数据择优落地「5 杠杆」档位 —— 开启 disclaimer / structure / bracket / bankcard /
low_signal_keywords，**不**开启 ai_boundary。本文件锁定该档位并逐一验证每个杠杆的
正例（仍命中）与反例（应静默）。

改动本文件任何断言前，请先复跑 ``python tools/fp_lab.py`` 并同步
``docs/false-positive-benchmark.md``。
"""

from __future__ import annotations

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import detector  # noqa: E402


@pytest.fixture
def gates():
    """临时改写杠杆开关，用例结束后恢复原值。

    Yields:
      可调用对象 ``apply(**lever=bool)``。
    """
    original = dict(detector.TIGHTEN)

    def apply(**kwargs: bool) -> None:
        for name, value in kwargs.items():
            detector.TIGHTEN[name] = value

    yield apply
    detector.TIGHTEN.clear()
    detector.TIGHTEN.update(original)


# --------------------------------------------------------------------------- #
# 档位锁定
# --------------------------------------------------------------------------- #
def test_shipped_tighten_profile():
    """落档位为「5 杠杆」，且 ai_boundary 明确关闭（不削弱 200 字窗口规格）。"""
    assert detector.TIGHTEN == {
        "ai_boundary": False,
        "disclaimer": True,
        "structure": True,
        "bracket": True,
        "bankcard": True,
        "low_signal_keywords": True,
    }


# --------------------------------------------------------------------------- #
# disclaimer：「仅供参考」需批注语义词
# --------------------------------------------------------------------------- #
def test_disclaimer_with_context_hits():
    hit = detector.detect("本文件仅供内部参考，谢谢配合。")
    assert hit is not None and hit.category == "internal_note"


def test_disclaimer_without_context_silent():
    assert detector.detect("本报告数据仅供参考，不构成任何投资建议。") is None
    assert detector.detect("以上内容仅供参考，最终以现场实际情况为准。") is None


def test_disclaimer_gate_can_be_disabled(gates):
    """关闭该杠杆后恢复旧行为（证明杠杆是唯一变量）。"""
    gates(disclaimer=False)
    hit = detector.detect("本报告数据仅供参考，不构成任何投资建议。")
    assert hit is not None


# --------------------------------------------------------------------------- #
# structure：行首 > 引用块 / —— 补充说明 需批注语义词
# --------------------------------------------------------------------------- #
def test_structure_quote_with_context_hits():
    # 注意：规则按 `_INTERNAL_RULES` 顺序命中，此样本中的「勿外传」字面量关键词先于
    # 「模式:引用块」命中，故只断言类别，不断言具体规则名。
    hit = detector.detect("> 这是引用过来的内部批注，请勿外传。")
    assert hit is not None and hit.category == "internal_note"


def test_structure_quote_without_context_silent():
    assert detector.detect("> 原始邮件内容如下，请相关部门知悉。") is None
    assert detector.detect("> 以下为上一封邮件的引用内容，请参考。") is None


def test_structure_dash_without_context_silent():
    assert detector.detect("行程安排：北京——上海——杭州，三天两晚。") is None


# --------------------------------------------------------------------------- #
# bracket：【】需方括号内文含批注语义词
# --------------------------------------------------------------------------- #
def test_bracket_with_context_hits():
    hit = detector.detect("【内部批注】这段不要对外发送。")
    assert hit is not None and hit.rule == "模式:【内联批注】"
    hit2 = detector.detect("【此段勿对外】")
    assert hit2 is not None and hit2.rule == "模式:【内联批注】"


def test_bracket_without_context_silent():
    assert detector.detect("【公告】系统将于本周六凌晨进行维护，请提前保存工作。") is None
    assert detector.detect("【通知】请各部门于周五前提交月度总结。") is None


# --------------------------------------------------------------------------- #
# bankcard：16–19 位数字需 Luhn 通过或邻近有上下文词
# --------------------------------------------------------------------------- #
def test_bankcard_luhn_valid_without_context_hits():
    # 4111111111111111 是 Luhn 合法的测试卡号。
    hit = detector.detect("编号 4111111111111111 请核对")
    assert hit is not None and hit.rule == "银行卡"


def test_bankcard_context_without_luhn_hits():
    # 6222021234567890 不满足 Luhn，但邻近有「卡号 / 转账」上下文。
    hit = detector.detect("卡号 6222021234567890 请转账。")
    assert hit is not None and hit.rule == "银行卡"


def test_bankcard_neither_luhn_nor_context_silent():
    for text in (
        "订单号 202409181234567890 请核对。",
        "流水号 8888160012345678 已生成。",
        "会话 ID 5500123456789012 已建立。",
        "时间戳拼接串 1695000000123456 请忽略。",
        "批次号 3210987654321098765 已入库。",
    ):
        assert detector.detect(text) is None, text


def test_bankcard_single_char_context_word_covers_shorthand():
    """单字「卡」计入上下文词 —— 「卡 6222…」这种简写必须仍能命中。"""
    hit = detector.detect("卡 6222021234567890。")
    assert hit is not None and hit.rule == "银行卡"


# --------------------------------------------------------------------------- #
# low_signal_keywords：注：/ 备注：/ 请确认 / 请审阅 需**强**语义词
# --------------------------------------------------------------------------- #
def test_low_signal_keyword_with_strong_context_hits():
    assert detector.detect("内部资料 注：本表格数据已核对。") is not None
    assert detector.detect("内部稿 备注：请提前十分钟到达。") is not None
    assert detector.detect("内部稿，请尽快确认后回复") is not None
    assert detector.detect("内部稿，请审阅后回复意见。") is not None


def test_low_signal_keyword_without_strong_context_silent():
    assert detector.detect("注：本表格数据截至 2024 年第三季度。") is None
    assert detector.detect("备注：请提前十分钟到达会议室。") is None
    assert detector.detect("请确认您已阅读并同意用户协议。") is None
    assert detector.detect("请张三确认后提交至审批流程。") is None
    assert detector.detect("请审阅后回复意见，谢谢配合。") is None


def test_low_signal_keyword_gate_does_not_use_generic_annotation_words(gates):
    """「备注：」不能因自身含「备注」二字而永远通过门槛（避免自指失效）。"""
    assert detector.detect("备注：请提前十分钟到达会议室。") is None


# --------------------------------------------------------------------------- #
# ai_boundary：默认关闭 → AI 特征仍按「首尾各 200 字」识别
# --------------------------------------------------------------------------- #
def test_ai_boundary_off_keeps_200_char_window():
    """规格中写死的「首尾各 200 字」是落档位的一部分，不得被静默收窄。"""
    assert detector.TIGHTEN["ai_boundary"] is False
    text = "甲" * 150 + "以下是" + "乙" * 400
    hit = detector.detect(text)
    assert hit is not None and hit.category == "ai_residue"

    tail_text = "甲" * 450 + "希望对你有帮助" + "乙" * 150
    tail_hit = detector.detect(tail_text)
    assert tail_hit is not None and tail_hit.category == "ai_residue"


def test_ai_boundary_on_would_narrow_window(gates):
    """显式开启 ai_boundary 后，远离首尾的 AI 特征不再命中（记录该杠杆的副作用）。"""
    gates(ai_boundary=True)
    assert detector.detect("甲" * 150 + "以下是" + "乙" * 400) is None
    assert detector.detect("甲" * 450 + "希望对你有帮助" + "乙" * 150) is None
    # 贴身的仍命中。
    hit = detector.detect("以下是整理好的内容，供你参考。")
    assert hit is not None and hit.category == "ai_residue"


# --------------------------------------------------------------------------- #
# detect() 与 find_spans()（清洗路径）口径一致
# --------------------------------------------------------------------------- #
def test_find_spans_respects_structure_gate():
    assert detector.find_spans("> 原始邮件内容如下，请相关部门知悉。") == []
    spans = detector.find_spans("> 这是引用过来的内部批注，请勿外传。")
    assert spans and spans[0].category == detector.CATEGORY_INTERNAL_NOTE


def test_find_spans_respects_bankcard_gate():
    assert detector.find_spans("订单号 202409181234567890 请核对。") == []
    spans = detector.find_spans("卡号 6222021234567890 请转账。")
    assert spans and spans[0].rule == "银行卡"


def test_find_spans_respects_low_signal_keyword_gate():
    assert detector.find_spans("备注：请提前十分钟到达会议室。") == []
    spans = detector.find_spans("内部稿 备注：请提前十分钟到达。")
    assert spans and spans[0].category == detector.CATEGORY_INTERNAL_NOTE

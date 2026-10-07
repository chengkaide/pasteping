"""QA 对抗性验证 —— detector 检测规则边界（独立于工程师的 test_detector.py）。

本文件由 QA 工程师（严过关）编写，目的是「试图证明它不能工作」，
刻意覆盖工程师原测试未触及的边界：首尾窗口的精确字符边界、多类别优先级对抗、
脱敏是否泄漏、API Key 六种前缀、手机号反向断言、银行卡/身份证去重、
邮箱不得命中、内部批注三种模式、AI 残留「仅首尾」反例、空值与超长输入。

注意：本模块只 import 纯逻辑模块 detector，绝不触碰 pywin32 / GUI，
      也不会启动任何托盘或 Win32 消息循环。
"""

from __future__ import annotations

import os
import re
import sys
import time

# 保证无论从哪个工作目录运行 pytest，都能 import 到项目根目录下的模块。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import detector  # noqa: E402

# 用于断言「脱敏后不得残留连续 6 位以上数字」。
_SIX_DIGIT_RUN = re.compile(r"\d{6,}")


# =========================================================================== #
# A.1 首尾 200 字符窗口本身的边界
# --------------------------------------------------------------------------- #
# 说明（窗口语义，来源 detector.py:30 `_EDGE_WINDOW = 200`）：
#   head = text[:200]   -> 覆盖索引 0..199
#   tail = text[-200:]  -> 覆盖索引 [len-200, len)
# 为使 head/tail 不重叠，所有用例的总长度均 > 400。
# =========================================================================== #
def test_head_keyword_last_char_is_200th_hit():
    """关键词完全落在前 200 字符内（末字符=第 200 字符，索引 199）→ 命中 head。"""
    text = "甲" * 196 + "转发文案" + "乙" * 400
    assert len(text) > 400
    hit = detector.detect(text)
    assert hit is not None, "关键词在第 200 字符处应命中，实际未命中"
    assert hit.category == "internal_note"
    assert hit.position == "head"


def test_head_keyword_first_char_is_201st_miss():
    """关键词首字符在第 201 字符（索引 200）→ 完全出头部窗口 → 不命中。"""
    text = "甲" * 200 + "转发文案" + "乙" * 400
    assert len(text) > 400
    assert detector.detect(text) is None, "关键词已在第 201 字符之后，不应命中"


def test_head_keyword_straddles_200th_boundary_miss():
    """关键词从索引 197 开始（跨越第 200 字符边界）→ 头部窗口不含完整关键词 → 不命中。"""
    text = "甲" * 197 + "转发文案" + "乙" * 400
    assert detector.detect(text) is None


def test_tail_keyword_fully_inside_last_200_hit():
    """关键词完全落在尾部 200 字符内 → 命中 tail。"""
    text = "甲" * 400 + "转发文案" + "乙" * 196  # 总长 600，tail 覆盖 400..599
    assert len(text) == 600
    hit = detector.detect(text)
    assert hit is not None, "关键词在尾部 200 字符内应命中"
    assert hit.category == "internal_note"
    assert hit.position == "tail"


def test_tail_keyword_straddles_boundary_miss():
    """关键词从索引 399 开始（跨越尾部窗口起点 400）→ 不命中。"""
    text = "甲" * 399 + "转发文案" + "乙" * 197  # 总长 600
    assert len(text) == 600
    assert detector.detect(text) is None


# =========================================================================== #
# A.1' AI 残留窗口：PRD 规定首尾各 200 字符，实现却只用 120 字符（偏差探针）
# =========================================================================== #
def test_spec_ai_head_feature_within_200_should_hit():
    """PRD：AI 开头特征在首 200 字符内即应命中。

    本用例把「以下是」放在索引 150（< 200），按 PRD 应命中 ai_residue。
    若失败，说明实现把 AI 头部扫描窗口收窄到了 120（detector.py:32 `_HEAD_SCAN = 120`）。
    """
    text = "甲" * 150 + "以下是" + "乙" * 400
    hit = detector.detect(text)
    assert hit is not None and hit.category == "ai_residue", (
        "PRD 规定首尾各 200 字符，索引 150 处的 AI 特征未被检出（窗口被收窄为 120）"
    )


def test_spec_ai_tail_feature_within_200_should_hit():
    """PRD：AI 结尾特征在末 200 字符内即应命中（关键词距末尾仅 150 字）。"""
    keyword = "希望对你有帮助"
    suffix = "乙" * 150  # 关键词结束处距文末 150 字（< 200）
    text = "甲" * 450 + keyword + suffix
    # 断言关键词确实落在「末 200 字符」窗口内。
    assert len(suffix) == 150
    assert text.rindex(keyword) >= len(text) - 200
    hit = detector.detect(text)
    assert hit is not None and hit.category == "ai_residue", (
        "PRD 规定首尾各 200 字符，距末尾 150 字处的 AI 特征未被检出（窗口被收窄为 120）"
    )


# =========================================================================== #
# A.2 优先级对抗
# =========================================================================== #
def test_priority_all_three_returns_sensitive():
    """AI 残留 + 内部批注 + 手机号 同时命中 → 必须返回 sensitive。"""
    text = "以下是内容。请尽快确认。联系电话 13800138000"
    hit = detector.detect(text)
    assert hit is not None
    assert hit.category == "sensitive"
    assert hit.rule == "手机号"


def test_priority_ai_plus_internal_returns_internal():
    """AI 残留 + 内部批注 同时命中 → 必须返回 internal_note。

    降误报修订（2026-10-06）：`请确认` 需同窗口出现**强**语义词才判为内部批注，
    故样本中加入「内部稿」，使「内部批注优先于 AI 残留」这条优先级语义仍被有效覆盖。
    """
    text = "以下是内容。内部稿，请尽快确认。"
    hit = detector.detect(text)
    assert hit is not None
    assert hit.category == "internal_note"


def test_priority_ai_only_returns_ai():
    """只有 AI 残留命中 → 返回 ai_residue。"""
    hit = detector.detect("以下是本次整理的要点。")
    assert hit is not None and hit.category == "ai_residue"


# =========================================================================== #
# A.3 脱敏不泄漏（关键）
# =========================================================================== #
def _assert_masked(hit, raw_secret: str):
    """断言 fragment 已脱敏：含 '*'、无完整原文、无连续 6 位以上数字。"""
    assert hit is not None
    assert "*" in hit.fragment, f"fragment 未脱敏：{hit.fragment!r}"
    assert _SIX_DIGIT_RUN.search(hit.fragment) is None, (
        f"fragment 泄漏了连续 6 位以上数字：{hit.fragment!r}"
    )
    assert raw_secret not in hit.fragment, f"fragment 泄漏完整原文：{hit.fragment!r}"


def test_mask_phone_no_leak():
    hit = detector.detect("联系电话 13800138000 谢谢")
    _assert_masked(hit, "13800138000")


def test_mask_idcard_no_leak():
    hit = detector.detect("身份证 11010519491231002X 请核对")
    _assert_masked(hit, "11010519491231002X")


def test_mask_bankcard_no_leak():
    hit = detector.detect("卡号 6222021234567890 请转账")
    _assert_masked(hit, "6222021234567890")


def test_mask_apikey_no_leak():
    key = "sk-abcdefgh12345678"
    hit = detector.detect(f"token = {key}")
    _assert_masked(hit, key)


# =========================================================================== #
# A.4 API Key 六种前缀
# =========================================================================== #
def test_apikey_prefix_sk():
    hit = detector.detect("key = sk-abcdefgh12345678")
    assert hit is not None and hit.category == "sensitive" and hit.rule == "API Key"


def test_apikey_prefix_ghp():
    hit = detector.detect("token ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert hit is not None and hit.rule == "API Key"


def test_apikey_prefix_gho():
    hit = detector.detect("token gho_abcdefghijklmnopqrstuvwxyz0123")
    assert hit is not None and hit.rule == "API Key"


def test_apikey_prefix_akia():
    hit = detector.detect("aws AKIAABCDEFGHIJKLMNOP end")
    assert hit is not None and hit.rule == "API Key"


def test_apikey_prefix_xoxb():
    hit = detector.detect("slack xoxb-1234567890abcdefghij end")
    assert hit is not None and hit.rule == "API Key"


def test_apikey_prefix_xoxp():
    hit = detector.detect("slack xoxp-1234567890abcdefghij end")
    assert hit is not None and hit.rule == "API Key"


# =========================================================================== #
# A.5 手机号反向断言（前后不能是数字）
# =========================================================================== #
def test_phone_embedded_in_longer_digit_run_not_detected():
    for bad in ("9913800138000", "138001380001", "01380013800", "1380013800000"):
        assert detector.detect(f"编号 {bad}") is None, f"{bad} 不应命中手机号"


def test_phone_standalone_detected():
    hit = detector.detect("电话 13800138000 谢谢")
    assert hit is not None and hit.rule == "手机号"


def test_phone_surrounded_by_non_digits_detected():
    hit = detector.detect("电话:13800138000。")
    assert hit is not None and hit.rule == "手机号"


# =========================================================================== #
# A.6 银行卡 vs 身份证去重 / 位数边界
# =========================================================================== #
def test_valid_idcard_reported_as_idcard_only():
    """18 位合法身份证只报「身份证」，绝不被降级为「银行卡」。"""
    hit = detector.detect("证件 11010519491231002X")
    assert hit is not None
    assert hit.category == "sensitive"
    assert hit.rule == "身份证"


def test_char_18_digit_bankcard_now_requires_context_or_luhn():
    """降误报修订（2026-10-06）：16–19 位数字需通过 Luhn 或邻近出现银行卡上下文词。

    ⚠️ 本用例的断言方向与原 `test_18_digit_non_idcard_reported_as_bankcard` **相反**：
    原实现把任何 16–19 位连续数字都判为银行卡，导致「编号 / 订单号 / 流水号 / 会话 ID」
    等业务 token 大量误报（基准集 5 条）。修订后无上下文且不满足 Luhn 的业务 token 静默。
    改动此处断言前请先复跑 `tools/fp_lab.py`。
    """
    assert detector.detect("编号 123456789012345678") is None
    hit = detector.detect("卡号 123456789012345678")
    assert hit is not None and hit.rule == "银行卡"


def test_15_digit_not_bankcard():
    assert detector.detect("编号 123456789012345") is None


def test_20_digit_not_bankcard():
    assert detector.detect("编号 12345678901234567890") is None


# =========================================================================== #
# A.7 邮箱不得命中（MVP 明确排除）
# =========================================================================== #
def test_email_not_detected():
    for email in (
        "zhangsan@example.com",
        "a.b+c@d.co",
        "user_name-1@sub.domain.org",
        "user1234567890@x.com",
    ):
        hit = detector.detect(f"邮箱 {email} 谢谢")
        assert hit is None or hit.category != "sensitive", f"{email} 不应命中敏感信息"


# =========================================================================== #
# A.8 内部批注的三种模式
# =========================================================================== #
def test_internal_bracket_pattern():
    hit = detector.detect("【此段勿对外】")
    assert hit is not None and hit.category == "internal_note"
    assert hit.rule == "模式:【内联批注】"


def test_internal_quote_pattern_line_start():
    hit = detector.detect("> 这是一段被引用的批注")
    assert hit is not None and hit.category == "internal_note"
    assert hit.rule == "模式:引用块"


def test_internal_dash_pattern():
    # 降误报修订：`——` 需同窗口出现批注语义词（否则中文破折号正常用法会被误报）。
    hit = detector.detect("—— 本段为内部补充说明")
    assert hit is not None and hit.category == "internal_note"
    assert hit.rule == "模式:补充说明"


def test_char_dash_without_context_not_flagged():
    """降误报修订：无批注语义词的破折号句不再提醒（改动需复跑 tools/fp_lab.py）。"""
    assert detector.detect("本次合作意向已初步达成 —— 后续细节另行沟通。") is None


# =========================================================================== #
# A.9 AI 残留「仅首尾」反例
# =========================================================================== #
def test_ai_feature_in_very_middle_not_detected():
    """AI 语气句出现在正文正中间（前后各 240 字符填充）→ 不应命中。"""
    text = "甲乙丙丁" * 60 + "当然可以" + "戊己庚辛" * 60
    assert len(text) > 400
    assert detector.detect(text) is None


# =========================================================================== #
# A.10 空值与垃圾输入
# =========================================================================== #
def test_none_input():
    assert detector.detect(None) is None


def test_empty_string():
    assert detector.detect("") is None


def test_whitespace_only():
    assert detector.detect("   \n\t ") is None


def test_very_long_plain_text_performance():
    text = "测试内容" * 2500  # 10000 字符
    assert len(text) == 10000
    start = time.perf_counter()
    hit = detector.detect(text)
    elapsed = time.perf_counter() - start
    assert hit is None
    assert elapsed < 1.0, f"10000 字符检测耗时 {elapsed:.3f}s，过慢"


# =========================================================================== #
# A.11 超长纯数字串
# =========================================================================== #
def test_long_digit_run_no_crash_no_false_hit():
    hit = detector.detect("1" * 5000)
    assert hit is None, "5000 位纯数字不应命中身份证/银行卡"


# =========================================================================== #
# A.12 真阴性检查（普通办公文本不得误报）
# =========================================================================== #
def test_benign_meeting_minutes_not_flagged():
    """不含任何风险特征的普通会议纪要不应误报。"""
    text = "会议纪要：本次例会明确了三点安排，各部门按计划推进即可。"
    assert detector.detect(text) is None


def test_benign_enabled_chat_not_flagged():
    """日常聊天口吻但无 AI 残留/AI 特征开头的文本不应误报。"""
    text = "今天下午三点的评审记得带笔记本，会议室在 5 楼。"
    assert detector.detect(text) is None


# =========================================================================== #
# A.13 回归：P2 修复（AI 扫描窗口 120 → 200）后应命中
# =========================================================================== #
def test_p2_fix_ai_head_at_150_detected():
    """修复后：索引 150 处的 AI 开头特征应命中 ai_residue。"""
    hit = detector.detect("甲" * 150 + "以下是" + "乙" * 400)
    assert hit is not None and hit.category == "ai_residue"
    assert hit.position == "head"


def test_p2_fix_ai_tail_near_end_detected():
    """修复后：距末尾 150 字处的 AI 结尾特征应命中 ai_residue。"""
    hit = detector.detect("甲" * 450 + "希望对你有帮助" + "乙" * 150)
    assert hit is not None and hit.category == "ai_residue"
    assert hit.position == "tail"


def test_p2_side_effect_priority_still_internal_over_ai():
    """P2 副作用检查：AI 词@100 与内部批注词@176 同时命中 → 仍须返回 internal_note。"""
    text = "甲" * 100 + "以下是" + "甲" * 68 + "内部稿，请尽快确认"
    assert text.index("以下是") == 100
    assert text.index("请尽快确认") < 200
    hit = detector.detect(text)
    assert hit is not None and hit.category == "internal_note"


def test_p2_boundary_201st_still_miss():
    """P2 副作用检查：窗口放大到 200 后，第 201 字起的 AI 特征仍不应命中。"""
    text = "甲" * 200 + "以下是" + "乙" * 400
    assert detector.detect(text) is None


# =========================================================================== #
# A.14 回归：P3 修复（敏感正则改 ASCII-only）后仍照常命中 + 全角不再命中
# =========================================================================== #
def test_p3_ascii_idcard_still_detected():
    hit = detector.detect("身份证 11010519491231002X 请核对")
    assert hit is not None and hit.category == "sensitive" and hit.rule == "身份证"


def test_p3_ascii_phone_still_detected():
    hit = detector.detect("电话 13800138000 谢谢")
    assert hit is not None and hit.rule == "手机号"


def test_p3_ascii_bankcard_still_detected():
    hit = detector.detect("卡号 6222021234567890 请转账")
    assert hit is not None and hit.rule == "银行卡"


def test_p3_idcard_bankcard_dedup_intact():
    """P3 副作用检查：身份证/银行卡重叠去重逻辑未被破坏。"""
    hit = detector.detect("证件 11010519491231002X")
    assert hit is not None and hit.rule == "身份证"


def test_p3_fullwidth_idcard_not_detected():
    assert detector.detect("１１０１０５１９４９１２３１００２Ｘ") is None


def test_p3_fullwidth_phone_not_detected():
    assert detector.detect("１３８００１３８０００") is None


def test_p3_fullwidth_bankcard_not_detected():
    assert detector.detect("１２３４５６７８９０１２３４５６") is None

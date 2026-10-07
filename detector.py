"""PastePing 剪贴板内容检测规则。

提供三类检测：

* ``internal_note`` —— 内部批注标记（仅检测文本首尾各 200 字符）；
* ``ai_residue``    —— AI 生成残留语气（仅检测文本首尾各 200 字符）；
* ``sensitive``     —— 法定敏感信息（全文检测）。

本模块是纯逻辑模块：不依赖任何 Windows / GUI 组件，方便在无界面环境下测试。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

CATEGORY_INTERNAL_NOTE = "internal_note"
CATEGORY_AI_RESIDUE = "ai_residue"
CATEGORY_SENSITIVE = "sensitive"

# 类别 -> 中文标签。
_LABELS = {
    CATEGORY_INTERNAL_NOTE: "内部批注",
    CATEGORY_AI_RESIDUE: "AI 残留",
    CATEGORY_SENSITIVE: "敏感信息",
}

# 首尾检测窗口长度。
_EDGE_WINDOW = 200
# 开头特征实际扫描范围（与产品定义一致：文案首尾各 200 字符内）。
_HEAD_SCAN = 200
# 结尾特征实际扫描范围（必须足够靠后）。
_TAIL_SCAN = 200

# 内部批注：字面量关键词。
_INTERNAL_KEYWORDS = (
    "转发文案",
    "内部使用",
    "内部资料",
    "勿外传",
    "备注：",
    "备注:",
    "注：",
    "注:",
)

# 内部批注：带占位（XX）的正则关键词。
_INTERNAL_REGEX_KEYWORDS = (
    ("关键词:仅供参考", r"仅供.{0,12}?参考"),
    ("关键词:请确认", r"请.{0,8}?确认"),
    ("关键词:请审阅", r"请.{0,8}?审阅"),
)

# 内部批注：结构模式。
_INTERNAL_PATTERNS = (
    ("模式:【内联批注】", r"【[^】\n]{1,40}】"),
    ("模式:引用块", r"(?m)^\s{0,3}>"),
    ("模式:补充说明", r"——.{1,60}"),
)

# 编译后的内部批注规则列表：(规则名, 编译后的正则)。
_INTERNAL_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    [(f"关键词:{keyword}", re.compile(re.escape(keyword))) for keyword in _INTERNAL_KEYWORDS]
    + [(name, re.compile(pattern)) for name, pattern in _INTERNAL_REGEX_KEYWORDS]
    + [(name, re.compile(pattern)) for name, pattern in _INTERNAL_PATTERNS]
)

# AI 残留：开头特征。
_AI_HEAD_FEATURES = ("当然可以", "以下是", "好的我", "没问题")
# AI 残留：结尾特征。
_AI_TAIL_FEATURES = (
    "希望对你有帮助",
    "需要我帮你",
    "要不要我帮你",
    "如果你需要我",
    "你可以随时告诉我",
)

# 敏感信息正则（全文检测；明确不做邮箱检测，办公场景误报率过高）。
# 法定敏感信息仅按 ASCII 数字识别；全角数字不视为敏感信息（避免与中文全角标点混用时的误分类）。
_IDCARD_RE = re.compile(
    r"(?<![0-9])[1-9][0-9]{5}(?:19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx](?![0-9])"
)
_PHONE_RE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
_APIKEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bp]-[A-Za-z0-9\-]{10,})"
)
_BANKCARD_RE = re.compile(r"(?<![0-9])[0-9]{16,19}(?![0-9])")

# API Key 常见前缀，脱敏时予以保留。
_APIKEY_PREFIXES = ("sk-", "ghp_", "gho_", "AKIA", "xoxb-", "xoxp-")

# fragment 截断上限。
_FRAGMENT_LIMIT = 60


# =========================================================================== #
# 降误报杠杆（v0.1 / v0.2 修订）
#
# 背景：QA 基准实测误报率 58.3%（21/36 干净样本被误报），直击本产品第一成功标准
# 「不误报打扰」。以下 6 个杠杆**相互独立、可单独开关**，用于把误报压进目标区间，
# 同时用基准集（tools/fp_lab.py）回归监控召回代价。
#
# 有效杠杆是「语义词共现门槛」，而非「位置收紧」（误报样本的触发词往往就在第 0 字）。
#
#   ai_boundary —— AI 残留特征必须「贴身」出现在文本首尾（前 / 后 8 字内），
#                  且边界之外不得出现实义字符。
#                  ⚠️ 会把 AI 特征的有效识别窗口从「首尾各 200 字」收窄到 8 字，
#                  属对产品规格的实质收窄，**当前不启用**（详见下方落地档位）。
#   disclaimer  —— 「仅供…参考」需在**同一扫描窗口**内出现批注语义词。
#   structure   —— 行首 `>` 引用块 / `——` 补充说明需在同一窗口出现批注语义词。
#   bracket     —— `【…】` 需方括号**内文**含批注语义词。
#   bankcard    —— 16–19 位数字需通过 Luhn 校验，或邻近出现银行卡上下文词。
#   low_signal_keywords —— `注：` / `备注：` / `请确认` / `请审阅` 需在同一窗口出现**强**语义词。
# =========================================================================== #
# 落地档位（按 tools/fp_lab.py 的全组合实测数据择优确定）：
#   开启 disclaimer / structure / bracket / bankcard / low_signal_keywords 五个杠杆，
#   误报率 58.3% → 8.3%（3/36），召回率保持 100%，精确率 43.2% → 84.2%。
#   刻意**不**开启 ai_boundary —— 它单独只降 1 个误报，却要把 AI 特征的有效识别窗口
#   从「首尾各 200 字」收窄到 8 字，属对产品规格的实质削弱，性价比不成立。
# 全组合实测见 tools/fp_lab.py，报告见 docs/false-positive-benchmark.md。
# =========================================================================== #

TIGHTEN: dict[str, bool] = {
    "ai_boundary": False,
    "disclaimer": True,
    "structure": True,
    "bracket": True,
    "bankcard": True,
    "low_signal_keywords": True,
}

# ai_boundary：开头特征须落在文本前 N 字符内；结尾特征须落在末 N 字符内。
_AI_BOUNDARY_TIGHT = 8

# **强**语义词：无歧义指向「内部 / 未定稿」语境。
_STRONG_CONTEXT_WORDS: tuple[str, ...] = (
    "内部",
    "勿外传",
    "勿对外",
    "不要外传",
    "不要对外",
    "不得外传",
    "禁止外传",
    "请勿转发",
    "请勿外发",
    "暂不对外",
    "不对外",
    "保密",
    "机密",
    "草案",
    "草稿",
    "初稿",
    "待定",
    "待确认",
    "待补充",
    "待审",
    "待批注",
    "仅限内部",
    "仅限部门",
    "仅部门",
)

# 批注语义词 = 强语义词 + 通用批注词（结构标记 / 【】 本身已暗示批注语境，故放宽）。
_ANNOTATION_CONTEXT_WORDS: tuple[str, ...] = _STRONG_CONTEXT_WORDS + ("批注", "备注")

# 向后兼容别名：disclaimer / structure / bracket 三个杠杆使用批注语义词。
_INTERNAL_CONTEXT_WORDS: tuple[str, ...] = _ANNOTATION_CONTEXT_WORDS

# bankcard：银行卡上下文词。
# 注：单字「卡」也计入 —— 现实中「卡 6222…」这种简写很常见（QA 用例
# `test_placeholder_and_no_raw_digits` 即命中该写法），漏报真实卡号比多提醒一次更糟。
_BANKCARD_CONTEXT_WORDS: tuple[str, ...] = (
    "卡",
    "银行卡",
    "银行",
    "信用卡",
    "储蓄卡",
    "借记卡",
    "账户",
    "账号",
    "转账",
    "汇款",
    "收款",
    "付款",
    "支付",
    "开户",
    "银联",
)

# bankcard：上下文词搜索半径（命中数字前后各 N 字符）。
_BANKCARD_CONTEXT_RADIUS = 12

# bracket：提取【…】内文的辅助正则（内文长度上限与 _INTERNAL_PATTERNS 保持一致）。
_BRACKET_INNER_RE = re.compile(r"【([^】\n]{1,40})】")

# 规则名 -> 杠杆名（未列出的规则不设门槛，行为恒定）。
_RULE_GATE: dict[str, str] = {
    "关键词:仅供参考": "disclaimer",
    "模式:引用块": "structure",
    "模式:补充说明": "structure",
    "模式:【内联批注】": "bracket",
    "关键词:注：": "low_signal_keywords",
    "关键词:注:": "low_signal_keywords",
    "关键词:备注：": "low_signal_keywords",
    "关键词:备注:": "low_signal_keywords",
    "关键词:请确认": "low_signal_keywords",
    "关键词:请审阅": "low_signal_keywords",
}

# 需要批注语义词门槛的杠杆（允许「批注 / 备注」这类通用批注词）。
_ANNOTATION_GATES = frozenset({"disclaimer", "structure"})


def _is_padding(char: str) -> bool:
    """判断字符是否为「非实义」字符（空白、标点、符号）。

    Args:
      char: 单个字符。

    Returns:
      空白 / 标点 / 符号返回 True，汉字、字母、数字等实义字符返回 False。
    """
    if char.isspace():
        return True
    return unicodedata.category(char).startswith(("P", "S"))


def _is_padding_only(fragment: str) -> bool:
    """判断一段文本是否只由非实义字符组成。

    Args:
      fragment: 待判断文本。

    Returns:
      全部为非实义字符（含空串）时返回 True。
    """
    return all(_is_padding(char) for char in fragment)


def _has_context_word(window: str, words: tuple[str, ...] | None = None) -> bool:
    """判断扫描窗口内是否出现批注语义词。

    Args:
      window: 待扫描的文本窗口。
      words: 语义词表；缺省时使用批注语义词表。

    Returns:
      出现任一语义词返回 True。
    """
    table = _ANNOTATION_CONTEXT_WORDS if words is None else words
    return any(word in window for word in table)


def _luhn_ok(digits: str) -> bool:
    """对纯数字串做 Luhn（模 10）校验。

    Args:
      digits: 仅含 ASCII 数字的字符串。

    Returns:
      通过校验返回 True。
    """
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - ord("0")
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _bankcard_gate_ok(text: str, start: int, end: int) -> bool:
    """银行卡杠杆门槛：Luhn 校验通过，或邻近存在银行卡上下文词。

    Args:
      text: 原文。
      start: 命中数字起始索引。
      end: 命中数字结束索引（不含）。

    Returns:
      满足门槛返回 True。
    """
    if _luhn_ok(text[start:end]):
        return True
    left = max(0, start - _BANKCARD_CONTEXT_RADIUS)
    right = min(len(text), end + _BANKCARD_CONTEXT_RADIUS)
    neighborhood = text[left:right]
    return any(word in neighborhood for word in _BANKCARD_CONTEXT_WORDS)


def _rule_gate_ok(
    rule_name: str, matched: str, window: str
) -> bool:
    """内部批注类规则的门槛判定。

    Args:
      rule_name: 命中的规则名。
      matched: 命中的原始文本（用于 bracket 杠杆取方括号内文）。
      window: 当前扫描窗口（用于 disclaimer / structure 杠杆的共现判定）。

    Returns:
      门槛通过（或该规则无门槛）返回 True。
    """
    gate = _RULE_GATE.get(rule_name)
    if gate is None or not TIGHTEN.get(gate, False):
        return True
    if gate == "bracket":
        inner = _BRACKET_INNER_RE.search(matched)
        return inner is not None and _has_context_word(inner.group(1))
    if gate == "low_signal_keywords":
        # 低区分度关键词（注：/备注：/请确认/请审阅）：需出现**强**语义词。
        # 不能使用批注语义词表 —— 否则「备注：」会因自身含「备注」而永远通过。
        return _has_context_word(window, _STRONG_CONTEXT_WORDS)
    # disclaimer / structure：批注语义词须出现在同一扫描窗口内。
    return _has_context_word(window)


def _ai_head_gate_ok(text: str, start: int) -> bool:
    """AI 残留开头特征门槛：必须贴身于文本开头。

    Args:
      text: 原文。
      start: 特征起始索引。

    Returns:
      门槛通过（或该杠杆关闭）返回 True。
    """
    if not TIGHTEN.get("ai_boundary", False):
        return True
    return start <= _AI_BOUNDARY_TIGHT and _is_padding_only(text[:start])


def _ai_tail_gate_ok(text: str, end: int) -> bool:
    """AI 残留结尾特征门槛：必须贴身于文本末尾。

    Args:
      text: 原文。
      end: 特征结束索引（不含）。

    Returns:
      门槛通过（或该杠杆关闭）返回 True。
    """
    if not TIGHTEN.get("ai_boundary", False):
        return True
    return len(text) - end <= _AI_BOUNDARY_TIGHT and _is_padding_only(text[end:])


@dataclass(frozen=True)
class Hit:
    """一次命中的检测结果。

    Attributes:
      category: 类别枚举值，取 ``internal_note`` / ``ai_residue`` / ``sensitive``。
      category_label: 类别中文标签。
      rule: 命中的具体规则名。
      fragment: 命中片段（敏感信息已脱敏、去换行、按需截断）。
      position: 命中位置，取 ``head`` / ``tail`` / ``body``。
    """

    category: str
    category_label: str
    rule: str
    fragment: str
    position: str


def _single_line(raw: str) -> str:
    """把任意文本压成单行（去掉换行、折叠多余空白）。

    Args:
      raw: 原始文本。

    Returns:
      单行文本。
    """
    return " ".join(raw.split())


def _truncate(raw: str, limit: int = _FRAGMENT_LIMIT) -> str:
    """把文本截断到指定长度，超长时追加省略号。

    Args:
      raw: 原始文本。
      limit: 最大字符数。

    Returns:
      截断后的文本。
    """
    if len(raw) > limit:
        return raw[:limit] + "…"
    return raw


def _mask_numbers(raw: str) -> str:
    """对纯数字类敏感信息做脱敏。

    规则：保留首 3 位 + 若干个 ``*``（长度-5，最少 4 个）+ 尾 2 位；
    总长不超过 6 时全部替换为星号。

    Args:
      raw: 原始数字串。

    Returns:
      脱敏后的字符串。
    """
    length = len(raw)
    if length <= 6:
        return "*" * length
    stars = max(4, length - 5)
    return raw[:3] + "*" * stars + raw[-2:]


def _mask_apikey(raw: str) -> str:
    """对 API Key 做脱敏：保留已知前缀与尾部 2 位。

    Args:
      raw: 原始 API Key。

    Returns:
      脱敏后的字符串。
    """
    prefix = ""
    for candidate in _APIKEY_PREFIXES:
        if raw.startswith(candidate):
            prefix = candidate
            break
    tail = raw[-2:]
    stars = max(4, len(raw) - len(prefix) - len(tail))
    return prefix + "*" * stars + tail


def _detect_internal_note(text: str) -> Optional[Hit]:
    """检测内部批注标记（仅首尾各 200 字符）。

    Args:
      text: 剪贴板文本。

    Returns:
      命中时返回 Hit，否则返回 None。
    """
    head = text[:_EDGE_WINDOW]
    tail = text[-_EDGE_WINDOW:]
    for rule_name, pattern in _INTERNAL_RULES:
        for window, position in ((head, "head"), (tail, "tail")):
            for match in pattern.finditer(window):
                if not _rule_gate_ok(rule_name, match.group(0), window):
                    continue
                fragment = _truncate(_single_line(match.group(0)))
                return Hit(
                    CATEGORY_INTERNAL_NOTE,
                    _LABELS[CATEGORY_INTERNAL_NOTE],
                    rule_name,
                    fragment,
                    position,
                )
    return None


def _detect_ai_residue(text: str) -> Optional[Hit]:
    """检测 AI 生成残留语气（仅首尾各 200 字符）。

    Args:
      text: 剪贴板文本。

    Returns:
      命中时返回 Hit，否则返回 None。
    """
    head = text[:_EDGE_WINDOW]
    tail = text[-_EDGE_WINDOW:]
    head_scan = head[:_HEAD_SCAN]
    tail_scan = tail[-_TAIL_SCAN:]
    tail_offset = len(text) - len(tail_scan)

    for keyword in _AI_HEAD_FEATURES:
        start = head_scan.find(keyword)
        while start != -1:
            if _ai_head_gate_ok(text, start):
                return Hit(
                    CATEGORY_AI_RESIDUE,
                    _LABELS[CATEGORY_AI_RESIDUE],
                    f"开头特征:{keyword}",
                    _truncate(_single_line(keyword)),
                    "head",
                )
            start = head_scan.find(keyword, start + 1)
    for keyword in _AI_TAIL_FEATURES:
        start = tail_scan.find(keyword)
        while start != -1:
            end = tail_offset + start + len(keyword)
            if _ai_tail_gate_ok(text, end):
                return Hit(
                    CATEGORY_AI_RESIDUE,
                    _LABELS[CATEGORY_AI_RESIDUE],
                    f"结尾特征:{keyword}",
                    _truncate(_single_line(keyword)),
                    "tail",
                )
            start = tail_scan.find(keyword, start + 1)
    return None


def _detect_sensitive(text: str) -> Optional[Hit]:
    """检测法定敏感信息（全文检测，同类多条时只返回最靠前的一条）。

    Args:
      text: 剪贴板文本。

    Returns:
      命中时返回 Hit（fragment 已脱敏），否则返回 None。
    """
    id_spans: list[tuple[int, int]] = []
    candidates: list[tuple[int, str, str, str]] = []

    for match in _IDCARD_RE.finditer(text):
        id_spans.append((match.start(), match.end()))
        candidates.append((match.start(), "身份证", match.group(0), "idcard"))

    for match in _PHONE_RE.finditer(text):
        candidates.append((match.start(), "手机号", match.group(0), "phone"))

    for match in _APIKEY_RE.finditer(text):
        candidates.append((match.start(), "API Key", match.group(0), "apikey"))

    for match in _BANKCARD_RE.finditer(text):
        # 优先身份证：与身份证区间重叠的银行卡候选直接丢弃。
        overlapped = any(
            not (match.end() <= start or match.start() >= end) for start, end in id_spans
        )
        if overlapped:
            continue
        if TIGHTEN.get("bankcard", False) and not _bankcard_gate_ok(
            text, match.start(), match.end()
        ):
            continue
        candidates.append((match.start(), "银行卡", match.group(0), "bankcard"))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _, rule, raw, kind = candidates[0]
    fragment = _single_line(_mask_apikey(raw) if kind == "apikey" else _mask_numbers(raw))
    return Hit(
        CATEGORY_SENSITIVE,
        _LABELS[CATEGORY_SENSITIVE],
        rule,
        fragment,
        "body",
    )


def detect(text: Optional[str]) -> Optional[Hit]:
    """对一段剪贴板文本执行检测。

    分级优先级：``sensitive`` > ``internal_note`` > ``ai_residue``，同时命中时只返回最严重的一条。

    Args:
      text: 剪贴板文本，可能为 None。

    Returns:
      命中时返回 Hit，否则返回 None。
    """
    if text is None:
        return None
    if not text.strip():
        return None

    for detector_fn in (_detect_sensitive, _detect_internal_note, _detect_ai_residue):
        hit = detector_fn(text)
        if hit is not None:
            return hit
    return None


# =========================================================================== #
# v0.2 新增：全文、多次命中、带偏移的定位能力（供 cleaner 使用）
#
# 重要约定（设计 §6.3）：本段代码**只做新增**，``detect()`` / ``Hit`` 及全部既有
# 常量与私有函数**逐字未改**，以保证 v0.1 的 108 项测试零回归。二者共用同一批
# 已编译正则，但互不干扰。
# =========================================================================== #

# 句子 / 行的边界分隔符；AI 残留与内部批注按「所在整句」删除。
_SENTENCE_SEPARATORS = "。！？!?\n"


@dataclass(frozen=True)
class Span:
    """一次命中的「定位」结果（含精确偏移与原始片段）。

    Attributes:
      category: 类别枚举值（同 ``Hit.category``）。
      category_label: 类别中文标签。
      rule: 命中的具体规则名。
      start: 命中片段在原文中的起始索引（含）。
      end: 命中片段在原文中的结束索引（不含）。
      raw: 命中片段的**原始文本**（供替换 / 删除；仅内存，不落盘、不外发）。
      position: 命中位置，取 ``head`` / ``tail`` / ``body``。
    """

    category: str
    category_label: str
    rule: str
    start: int
    end: int
    raw: str
    position: str


def _span_position(text: str, start: int, end: int) -> str:
    """按 v0.1 的首尾窗口语义给出 span 的位置标签。

    Args:
      text: 原文。
      start: 起始索引。
      end: 结束索引。

    Returns:
      ``head`` / ``tail`` / ``body`` 之一。
    """
    if end <= _EDGE_WINDOW:
        return "head"
    if start >= len(text) - _EDGE_WINDOW:
        return "tail"
    return "body"


def _iter_sensitive_matches(text: str) -> list[tuple[int, int, str]]:
    """全文扫描敏感信息，返回 ``(start, end, rule)`` 列表。

    沿用既有「身份证与银行卡区间重叠时丢弃银行卡」的去重逻辑（独立实现，
    ``detect()`` 内的原逻辑保持原样）。

    Args:
      text: 原文。

    Returns:
      ``(start, end, rule)`` 三元组列表。
    """
    id_spans: list[tuple[int, int]] = []
    results: list[tuple[int, int, str]] = []
    for match in _IDCARD_RE.finditer(text):
        id_spans.append((match.start(), match.end()))
        results.append((match.start(), match.end(), "身份证"))
    for match in _PHONE_RE.finditer(text):
        results.append((match.start(), match.end(), "手机号"))
    for match in _APIKEY_RE.finditer(text):
        results.append((match.start(), match.end(), "API Key"))
    for match in _BANKCARD_RE.finditer(text):
        overlapped = any(
            not (match.end() <= start or match.start() >= end) for start, end in id_spans
        )
        if overlapped:
            continue
        if TIGHTEN.get("bankcard", False) and not _bankcard_gate_ok(
            text, match.start(), match.end()
        ):
            continue
        results.append((match.start(), match.end(), "银行卡"))
    return results


def _expand_to_sentence(text: str, start: int, end: int) -> tuple[int, int]:
    """把命中位置向外扩展到所在「句 / 行」的边界。

    Args:
      text: 原文。
      start: 命中起始索引。
      end: 命中结束索引。

    Returns:
      扩展后的 ``(start, end)``；结束位置会包含句末分隔符（若有）。
    """
    left = start
    while left > 0 and text[left - 1] not in _SENTENCE_SEPARATORS:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _SENTENCE_SEPARATORS:
        right += 1
    if right < len(text) and text[right] in _SENTENCE_SEPARATORS:
        right += 1
    return left, right


def _clamp_against_ranges(
    start: int, end: int, protected: list[tuple[int, int]]
) -> tuple[int, int]:
    """把 ``(start, end)`` 收窄，避免覆盖到受保护的敏感信息区间。

    Args:
      start: 起始索引。
      end: 结束索引。
      protected: 需要保护（不被普通删除逻辑吞掉）的区间列表。

    Returns:
      收窄后的 ``(start, end)``。
    """
    for protect_start, protect_end in protected:
        if protect_end <= start or protect_start >= end:
            continue
        if protect_start > start:
            end = min(end, protect_start)
        if protect_end < end:
            start = max(start, protect_end)
    return start, end


def find_spans(text: Optional[str]) -> list[Span]:
    """全文、多次命中、带偏移地定位三类命中（供清洗使用）。

    与 ``detect()`` 完全独立：``detect()`` 仅看首尾 200 字且只返回最严重的一条；
    本函数对 **内部批注 / AI 残留 / 敏感信息三类均做全文扫描**（清洗需处理文本中段），
    敏感信息精确定位（后续替换为占位符），内部批注与 AI 残留扩展为整句（后续整句删除）。

    Args:
      text: 剪贴板文本，可能为 None。

    Returns:
      按 ``start`` 升序、且去除相互重叠（保留更长者）后的 ``Span`` 列表。
    """
    if text is None or not text.strip():
        return []

    sensitive = _iter_sensitive_matches(text)
    protected = [(start, end) for start, end, _ in sensitive]

    spans: list[Span] = []

    # 敏感信息：精确定位（保留结构，后续替换为占位符）。
    for start, end, rule in sensitive:
        spans.append(
            Span(
                CATEGORY_SENSITIVE,
                _LABELS[CATEGORY_SENSITIVE],
                rule,
                start,
                end,
                text[start:end],
                _span_position(text, start, end),
            )
        )

    # 内部批注：全文 finditer + 扩句（并避免吞掉敏感信息）。
    for rule_name, pattern in _INTERNAL_RULES:
        for match in pattern.finditer(text):
            if not _rule_gate_ok(rule_name, match.group(0), text):
                continue
            start, end = _expand_to_sentence(text, match.start(), match.end())
            start, end = _clamp_against_ranges(start, end, protected)
            if end <= start:
                continue
            spans.append(
                Span(
                    CATEGORY_INTERNAL_NOTE,
                    _LABELS[CATEGORY_INTERNAL_NOTE],
                    rule_name,
                    start,
                    end,
                    text[start:end],
                    _span_position(text, start, end),
                )
            )

    # AI 残留：全文查找 head / tail 特征词 + 扩句。
    for keyword in _AI_HEAD_FEATURES:
        for match in re.finditer(re.escape(keyword), text):
            if not _ai_head_gate_ok(text, match.start()):
                continue
            start, end = _expand_to_sentence(text, match.start(), match.end())
            start, end = _clamp_against_ranges(start, end, protected)
            if end <= start:
                continue
            spans.append(
                Span(
                    CATEGORY_AI_RESIDUE,
                    _LABELS[CATEGORY_AI_RESIDUE],
                    f"开头特征:{keyword}",
                    start,
                    end,
                    text[start:end],
                    _span_position(text, start, end),
                )
            )
    for keyword in _AI_TAIL_FEATURES:
        for match in re.finditer(re.escape(keyword), text):
            if not _ai_tail_gate_ok(text, match.end()):
                continue
            start, end = _expand_to_sentence(text, match.start(), match.end())
            start, end = _clamp_against_ranges(start, end, protected)
            if end <= start:
                continue
            spans.append(
                Span(
                    CATEGORY_AI_RESIDUE,
                    _LABELS[CATEGORY_AI_RESIDUE],
                    f"结尾特征:{keyword}",
                    start,
                    end,
                    text[start:end],
                    _span_position(text, start, end),
                )
            )

    # 去重：按 start 升序，重叠时保留更长者。
    spans.sort(key=lambda span: (span.start, -(span.end - span.start)))
    merged: list[Span] = []
    for span in spans:
        if merged and span.start < merged[-1].end:
            previous = merged[-1]
            if (span.end - span.start) > (previous.end - previous.start):
                merged[-1] = span
            continue
        merged.append(span)
    return merged

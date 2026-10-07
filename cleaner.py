"""PastePing 自动清洗模块（纯逻辑，无 GUI、无剪贴板依赖）。

清洗规则（PRD §3.2）：

* **AI 残留**：删除命中的开头/结尾特征**整句**；
* **内部批注**：删除命中的关键词 / ``【批注】`` / 行首 ``>`` 引用块 / ``——`` 补充句；
* **敏感信息**：替换为占位符 ``［已移除：<类别>］``（保留结构，避免语义断裂）。

安全阀：若清洗将移除超过 ``CLEAN_MAX_REMOVAL_RATIO``（默认 0.6）比例的字符，
则**放弃清洗、仅提醒**，防止误删大段内容（CLN-6）。

撤销：在内存中保留一份「清洗前原文 + 时间戳」，**只在最近一次清洗有效**，原文**不落盘**。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import detector
from detector import Span

# 安全阀：单次清洗移除超过此比例即放弃清洗。
CLEAN_MAX_REMOVAL_RATIO: float = 0.6

# 敏感信息占位符（全角方括号，最大程度避免与正文 ASCII 括号冲突）。
CLEAN_PLACEHOLDER: str = "［已移除：{类别}］"

# 撤销快照保护锁（模块级）。
_UNDO_LOCK: threading.RLock = threading.RLock()

# 撤销快照：(清洗前原文, 时间戳)；None 表示无可用撤销。
_undo_snapshot: Optional[tuple[str, float]] = None


@dataclass(frozen=True)
class CleanResult:
    """一次清洗的结果。

    Attributes:
      original_text: 清洗前原文。
      cleaned_text: 清洗后文本（跳过时为原文）。
      removed: 被处理的命中片段列表。
      removed_ratio: 移除字符占比（0.0 ~ 1.0）。
      skipped: 是否因触发安全阀而放弃清洗。
      summary_label: 清洗摘要标签（如「内部批注、敏感信息」）；无命中时为 ``""``。
    """

    original_text: str
    cleaned_text: str
    removed: list[Span]
    removed_ratio: float
    skipped: bool
    summary_label: str


def _unique_labels(spans: list[Span]) -> list[str]:
    """按出现顺序收集去重后的类别标签。

    Args:
      spans: 命中片段列表。

    Returns:
      去重后的类别标签列表。
    """
    labels: list[str] = []
    for span in spans:
        if span.category_label not in labels:
            labels.append(span.category_label)
    return labels


def _set_undo(original_text: str) -> None:
    """写入撤销快照（仅内存）。

    Args:
      original_text: 清洗前原文。
    """
    global _undo_snapshot
    with _UNDO_LOCK:
        _undo_snapshot = (original_text, time.monotonic())


def clean(text: Optional[str]) -> CleanResult:
    """对一段文本执行清洗。

    对敏感信息替换为占位符、对 AI 残留 / 内部批注整句删除；命中移除比例超过
    安全阀时返回 ``skipped=True`` 且 ``cleaned_text == original_text``。

    Args:
      text: 待清洗文本，可能为 None。

    Returns:
      清洗结果 ``CleanResult``。
    """
    original = text if isinstance(text, str) else ""
    if not original:
        return CleanResult(original, original, [], 0.0, False, "")

    try:
        spans = detector.find_spans(original)
    except Exception:
        # 清洗异常：捕获并退化为「仅提醒」。
        return CleanResult(original, original, [], 0.0, True, "")

    if not spans:
        return CleanResult(original, original, [], 0.0, False, "")

    pieces: list[str] = []
    cursor = 0
    for span in spans:
        if span.start < cursor:
            # 理论上去重后不会发生；防御性跳过重叠片段。
            continue
        pieces.append(original[cursor:span.start])
        if span.category == detector.CATEGORY_SENSITIVE:
            # 敏感信息用「具体类别」（手机号 / 身份证 / API Key / 银行卡）作为占位符标签。
            pieces.append(CLEAN_PLACEHOLDER.format(类别=span.rule))
        cursor = span.end
    pieces.append(original[cursor:])
    cleaned = "".join(pieces)

    removed_ratio = (len(original) - len(cleaned)) / len(original)
    if removed_ratio > CLEAN_MAX_REMOVAL_RATIO:
        # 安全阀触发：放弃清洗，仅提醒。
        return CleanResult(original, original, spans, removed_ratio, True, "")

    if cleaned == original:
        return CleanResult(original, original, spans, 0.0, False, "")

    _set_undo(original)
    return CleanResult(
        original,
        cleaned,
        spans,
        removed_ratio,
        False,
        "、".join(_unique_labels(spans)),
    )


def has_undo() -> bool:
    """是否存在可用的撤销快照。

    Returns:
      存在返回 True，否则 False。
    """
    with _UNDO_LOCK:
        return _undo_snapshot is not None


def undo() -> Optional[CleanResult]:
    """撤销最近一次清洗。

    Returns:
      撤销成功时返回 ``CleanResult``，其中 ``cleaned_text`` 为**恢复后的原文**；
      无可用撤销时返回 None。撤销后快照被消费（只能在最近一次清洗有效）。
    """
    global _undo_snapshot
    with _UNDO_LOCK:
        if _undo_snapshot is None:
            return None
        original, _timestamp = _undo_snapshot
        _undo_snapshot = None
    return CleanResult(original, original, [], 0.0, False, "已恢复原始内容")


def clear_undo() -> None:
    """清除撤销快照（仅供测试使用）。"""
    global _undo_snapshot
    with _UNDO_LOCK:
        _undo_snapshot = None

"""PastePing 全局运行时状态。

本模块集中存放进程内可变全局状态，并使用一把可重入锁（RLock）保护全部读写，
避免「剪贴板监听线程」与「托盘线程」并发访问导致竞态。

v0.1 语义（提醒开关 / 暂停 / 降频）**逐字保留、行为不变**；v0.2 在此基础上
**只做加法**：

* 新增「自动清洗开关」（默认关闭，见 ``CLEAN_ENABLED_DEFAULT``）；
* 新增赞助相关常量（金额 / 平台名）。

⚠️ **2026-10-07 重大语义变更（开源）**：本项目转为**全部功能免费**；
「付费解锁」这一整层被**移除** —— 不再有激活码、不再有验签、不再有任何形式的
「付费才能用」。原先的 Ed25519 授权模块（``licensing`` / ``license_codec``）与
激活码输入框（``ui_dialog``）已随之下线删除，:func:`get_unlocked` 等 API 一并消失。

因此本模块**不含任何持久化状态**：全部状态都是内存态，进程退出即消失，
不读也不写任何配置文件。唯一会写盘的东西是**诊断日志**
（``%APPDATA%\\PastePing\\pasteping.log``，由 ``diagnostics`` 负责），
且它只记文本长度与命中的规则名，**从不记剪贴板原文**。
"""

from __future__ import annotations

import hashlib
import threading
import time

# 默认降频窗口（秒）：同一段文本在此时长内只提醒一次。
DEFAULT_COOLDOWN: float = 60.0

# --------------------------------------------------------------------------- #
# v0.2 新增：面向用户的可改常量（赞助）——改文案只改这一处
# --------------------------------------------------------------------------- #

# 支持开发者（纯自愿打赏）的金额。
# 2026-10-07 起，这是本项目**唯一**的对外付费入口 —— 没有购买、没有解锁、
# 没有任何「付费才能用」的东西；赞助与功能**完全无关**。
SUPPORT_PRICE_CNY: int = 3

# 赞助平台名称（只承接自愿打赏，不承接任何商品交易）。
SUPPORT_PLATFORM_NAME: str = "爱发电"

# 自动清洗开关的默认值：默认关闭，用户在托盘菜单里显式开启才生效。
CLEAN_ENABLED_DEFAULT: bool = False

# --------------------------------------------------------------------------- #
# 可变状态（全部由 _lock 保护）
# --------------------------------------------------------------------------- #

# 保护全部可变状态的重入锁。
_lock: threading.RLock = threading.RLock()

# 敏感提醒总开关。
_enabled: bool = True

# 暂停截止时间（time.monotonic() 基准）；0.0 表示当前未暂停。
_pause_until: float = 0.0

# 降频记录：key = 剪贴板文本的 sha256 摘要，value = 上次提醒的 monotonic 时间戳。
# 只保存摘要而非原文，避免在内存中长期驻留敏感内容。
_last_hits: dict[str, float] = {}

# --- v0.2 新增状态 --- #

# 自动清洗开关。
_clean_enabled: bool = CLEAN_ENABLED_DEFAULT

# 注：v0.2 曾有一个 `_clean_guide_shown`「引导会话内只弹一次」标记。
# 2026-10-06 实测确认它会让三个增强功能菜单项在首次点击后变成**死按键**
# （点了完全没反应），已连同 `is_clean_guide_shown` / `mark_clean_guide_shown`
# 一并删除，且不再恢复 —— 用户主动点击的菜单项必须每次都有响应。


def _digest(text: str) -> str:
    """计算文本的 sha256 摘要（十六进制字符串）。

    Args:
      text: 任意文本。

    Returns:
      文本对应的 sha256 摘要字符串。
    """
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def is_detection_active() -> bool:
    """当前是否处于「可提醒」状态。

    Returns:
      当敏感提醒已开启且未处于暂停期内时返回 True，否则返回 False。
    """
    with _lock:
        return _enabled and time.monotonic() >= _pause_until


def get_enabled() -> bool:
    """读取敏感提醒总开关。

    Returns:
      总开关当前取值。
    """
    with _lock:
        return _enabled


def set_enabled(flag: bool) -> None:
    """设置敏感提醒总开关。

    Args:
      flag: 目标开关值。
    """
    global _enabled
    with _lock:
        _enabled = bool(flag)


def toggle_enabled() -> bool:
    """翻转敏感提醒总开关。

    Returns:
      翻转后的新取值。
    """
    global _enabled
    with _lock:
        _enabled = not _enabled
        return _enabled


def pause(seconds: int) -> None:
    """暂停提醒一段时间。

    Args:
      seconds: 暂停时长（秒），小于 0 时按 0 处理。
    """
    global _pause_until
    with _lock:
        _pause_until = time.monotonic() + max(0, int(seconds))


def resume() -> None:
    """立即结束暂停状态。"""
    global _pause_until
    with _lock:
        _pause_until = 0.0


def seconds_until_resume() -> int:
    """返回距离暂停结束还剩多少秒。

    Returns:
      剩余秒数（四舍五入，最少 0）。未暂停时返回 0。
    """
    with _lock:
        remaining = _pause_until - time.monotonic()
        return max(0, int(round(remaining)))


def should_emit(text: str, cooldown: float = DEFAULT_COOLDOWN) -> bool:
    """判断是否应当为这段文本发出提醒（降频）。

    同一段文本在 cooldown 秒内只允许提醒一次；判断与写入在同一把锁内完成，避免竞态。
    同时会顺手剔除已过期的历史条目，防止字典无限增长。

    Args:
      text: 剪贴板文本。
      cooldown: 降频窗口（秒）。

    Returns:
      允许提醒返回 True，处于冷却期内返回 False。
    """
    digest = _digest(text)
    now = time.monotonic()
    with _lock:
        expired = [key for key, ts in _last_hits.items() if now - ts > cooldown]
        for key in expired:
            del _last_hits[key]
        last = _last_hits.get(digest)
        if last is not None and now - last < cooldown:
            return False
        _last_hits[digest] = now
        return True


def clear_history() -> None:
    """清空降频历史（仅供测试使用）。"""
    with _lock:
        _last_hits.clear()


# --------------------------------------------------------------------------- #
# v0.2 新增：自动清洗开关
# --------------------------------------------------------------------------- #
def get_clean_enabled() -> bool:
    """读取自动清洗开关。

    Returns:
      自动清洗开启返回 True，否则 False。
    """
    with _lock:
        return _clean_enabled


def set_clean_enabled(flag: bool) -> None:
    """设置自动清洗开关。

    Args:
      flag: 目标开关值。
    """
    global _clean_enabled
    with _lock:
        _clean_enabled = bool(flag)


def toggle_clean_enabled() -> bool:
    """翻转自动清洗开关。

    Returns:
      翻转后的新取值。
    """
    global _clean_enabled
    with _lock:
        _clean_enabled = not _clean_enabled
        return _clean_enabled


def reset() -> None:
    """恢复初始状态（仅供测试使用）。"""
    global _enabled, _pause_until, _clean_enabled
    with _lock:
        _enabled = True
        _pause_until = 0.0
        _last_hits.clear()
        _clean_enabled = CLEAN_ENABLED_DEFAULT

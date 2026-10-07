"""PastePing 轻提示封装。

把一次命中转换为两个「不打断用户」的轻信号：

* 系统气泡通知（经托盘图标 ``Icon.notify`` 发出）；
* 托盘图标短暂变红，2 秒后自动恢复。

本模块通过回调注入托盘能力，**不反向 import tray**，避免循环依赖。
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import diagnostics
from detector import Hit

# 图标变红持续时间（秒）。
_FLASH_SECONDS = 2.0


class Notifier:
    """把命中渲染为通知 + 图标闪烁的轻提示器。

    Attributes:
      _on_notify: 发送系统通知的回调，签名为 ``(message, title)``。
    """

    def __init__(
        self,
        on_flash_start: Optional[Callable[[], None]] = None,
        on_flash_end: Optional[Callable[[], None]] = None,
        on_notify: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """初始化轻提示器。

        Args:
          on_flash_start: 让托盘图标变红的回调。
          on_flash_end: 让托盘图标恢复灰色的回调。
          on_notify: 发送系统通知的回调，签名为 ``(message, title)``。
        """
        self._on_flash_start = on_flash_start
        self._on_flash_end = on_flash_end
        self._on_notify = on_notify
        self._timer: Optional[threading.Timer] = None
        self._flash_gen = 0
        self._lock = threading.Lock()

    def notify(self, hit: Hit) -> None:
        """对一次命中发出轻提示。

        Args:
          hit: 检测命中结果。
        """
        title = f"PastePing · 检测到{hit.category_label}"
        diagnostics.log("hit", f"category={hit.category} rule={hit.rule}")
        self._show_notification(title, hit)
        self._flash()

    def notify_cleaned(self, labels: list, can_undo: bool = True) -> None:
        """发出「已自动清洗」提示。

        Args:
          labels: 被处理内容的类别标签列表（如 ``["内部批注", "敏感信息"]``）。
          can_undo: 是否可撤销（提示中附带「可在托盘菜单撤销」）。
        """
        title = "PastePing · 已自动清洗"
        text = "已自动清洗：" + "、".join(labels) if labels else "已自动清洗"
        if can_undo:
            text += "（可在托盘菜单撤销）"
        if self._on_notify is not None:
            try:
                self._on_notify(text, title)
            except Exception:
                pass
        self._flash()

    def notify_message(self, title: str, message: str) -> None:
        """发出任意标题 / 正文的通用通知（不闪烁）。

        Args:
          title: 通知标题。
          message: 通知正文。
        """
        if self._on_notify is None:
            return
        try:
            self._on_notify(message, title)
        except Exception:
            pass

    def _show_notification(self, title: str, hit: Hit) -> None:
        """发送系统通知，优先使用换行排版，失败时降级为单行。

        Args:
          title: 通知标题。
          hit: 检测命中结果。
        """
        if self._on_notify is None:
            return
        try:
            self._on_notify(f"{hit.rule}\n{hit.fragment}", title)
            diagnostics.log("notify", "multiline-ok")
        except Exception as error:
            try:
                self._on_notify(f"{hit.rule} | {hit.fragment}", title)
                diagnostics.log("notify", f"singleline-ok after={type(error).__name__}")
            except Exception as inner:
                diagnostics.log("notify", f"failed {type(inner).__name__}: {inner}")

    def _flash(self) -> None:
        """触发图标变红，并在 2 秒后恢复；连续命中时取消旧计时器避免叠加。"""
        diagnostics.log("flash", "icon->red (2s)")
        with self._lock:
            self._flash_gen += 1
            generation = self._flash_gen
            if self._timer is not None:
                self._timer.cancel()
            if self._on_flash_start is not None:
                try:
                    self._on_flash_start()
                except Exception:
                    pass
            self._timer = threading.Timer(_FLASH_SECONDS, self._flash_end, args=(generation,))
            self._timer.daemon = True
            self._timer.start()

    def _flash_end(self, generation: int) -> None:
        """计时器回调：仅在仍是当前这一代闪烁时才恢复图标。

        Args:
          generation: 触发本次恢复的闪烁代号。
        """
        with self._lock:
            if generation != self._flash_gen:
                return
            self._timer = None
        if self._on_flash_end is not None:
            try:
                self._on_flash_end()
            except Exception:
                pass

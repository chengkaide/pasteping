"""基于 Win32 事件驱动的剪贴板监听（v0.2 改造版）。

实现要点：

* 使用 ``ctypes`` 创建一个 message-only 隐藏窗口（``HWND_MESSAGE`` 作为父窗口）；
* 通过 ``AddClipboardFormatListener`` 订阅 ``WM_CLIPBOARDUPDATE``，**完全事件驱动**；
* 消息循环运行在独立后台守护线程中，``stop()`` 通过 ``WM_CLOSE`` 让循环退出；
* **读写一律委托** ``clipboard_io``（本模块不再直接使用 ``win32clipboard``）。

v0.2 关键改动（设计 §6.1，**最高风险点**）：

* 新增 :meth:`set_text` —— 唯一写回路径。写回前**先登记内容哈希抑制标志**（1.5s 窗口），
  再写回剪贴板，使写回引发的下一批 ``WM_CLIPBOARDUPDATE`` 被精确消费一次；
* :meth:`_handle_update` 由「纯时间防抖」改为「**内容哈希 + 时间**防抖」：
  同一内容的重复事件被折叠，而**内容不同的事件永不因防抖被丢弃**
  （保证用户的下一次真实复制不会被吞）。
"""

from __future__ import annotations

import ctypes
import hashlib
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

import clipboard_io
import diagnostics

# 窗口消息常量。
WM_CLIPBOARDUPDATE = 0x031D
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002

# 防抖：仅折叠「同一次复制」产生的重复同内容事件。
_DEBOUNCE_SECONDS = 0.2
# 抑制有效窗口（秒）：足以覆盖写回事件的异步投递（通常 <50ms），又尽量短。
_SUPPRESS_WINDOW = 1.5
# 读取剪贴板的重试次数与间隔（由 clipboard_io 使用）。
_READ_RETRY = 3
_READ_RETRY_INTERVAL = 0.08

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 窗口过程回调类型（LRESULT = 指针宽度的有符号整数）。
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WNDCLASSW(ctypes.Structure):
    """Win32 ``WNDCLASSW`` 结构体。"""

    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _MSG(ctypes.Structure):
    """Win32 ``MSG`` 结构体。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


# 显式声明函数签名，避免 64 位下指针/整型被错误转换。
user32.RegisterClassW.restype = ctypes.c_ushort
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

# HWND_MESSAGE：message-only 窗口的父窗口句柄。
_HWND_MESSAGE = wintypes.HWND(-3)
_CLASS_NAME = "PastePingMessageWindow"


def _digest(text: str) -> str:
    """计算文本的 sha256 摘要（十六进制）。

    Args:
      text: 任意文本。

    Returns:
      sha256 摘要字符串（与 config 一致，不驻留原文）。
    """
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class ClipboardListener:
    """事件驱动的剪贴板监听器（v0.2 支持写回与自触发抑制）。

    Attributes:
      _on_text: 命中一次可用文本时触发的回调（在监听线程内执行）。
    """

    def __init__(self, on_text: Callable[[str], None]) -> None:
        """初始化监听器。

        Args:
          on_text: 收到剪贴板文本时调用的回调，参数为文本内容。
        """
        self._on_text = on_text
        self._thread: Optional[threading.Thread] = None
        self._hwnd: Optional[int] = None
        self._wndproc_cb: Optional[_WNDPROC] = None
        self._last_event: float = 0.0
        self._last_digest: Optional[str] = None
        self._suppress_digest: Optional[str] = None
        self._suppress_deadline: float = 0.0
        self._running = False
        self._subscribed = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """在独立后台守护线程中启动消息循环。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="PastePingClipboard", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """请求停止监听（通过向隐藏窗口投递 WM_CLOSE 让消息循环退出）。"""
        with self._lock:
            hwnd = self._hwnd
        self._running = False
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    def is_subscribed(self) -> bool:
        """是否已成功创建隐藏窗口并订阅剪贴板事件。

        未订阅 = 收到不到任何复制通知（表现为「复制了却一直没提醒」）。
        供入口在启动后自检，并在失败时明确告知用户，而不是静默失灵。

        Returns:
          已订阅返回 True。
        """
        with self._lock:
            return self._subscribed

    def _run(self) -> None:
        """创建隐藏窗口、订阅剪贴板事件并运行消息循环（监听线程入口）。"""
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc_cb = _WNDPROC(self._wndproc_impl)

        wndclass = _WNDCLASSW()
        wndclass.style = 0
        wndclass.lpfnWndProc = self._wndproc_cb
        wndclass.cbClsExtra = 0
        wndclass.cbWndExtra = 0
        wndclass.hInstance = hinst
        wndclass.hIcon = None
        wndclass.hCursor = None
        wndclass.hbrBackground = None
        wndclass.lpszMenuName = None
        wndclass.lpszClassName = _CLASS_NAME
        user32.RegisterClassW(ctypes.byref(wndclass))

        hwnd = user32.CreateWindowExW(
            0,
            _CLASS_NAME,
            "PastePing",
            0,
            0,
            0,
            0,
            0,
            _HWND_MESSAGE,
            None,
            hinst,
            None,
        )
        if not hwnd:
            diagnostics.log(
                "listener", f"window-create-failed err={ctypes.get_last_error()}"
            )
            self._running = False
            return

        with self._lock:
            self._hwnd = hwnd

        if not user32.AddClipboardFormatListener(hwnd):
            diagnostics.log(
                "listener", f"subscribe-failed err={ctypes.get_last_error()}"
            )
        else:
            with self._lock:
                self._subscribed = True
            diagnostics.log("listener", "subscribed")

        try:
            msg = _MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.RemoveClipboardFormatListener(hwnd)
            user32.DestroyWindow(hwnd)
            with self._lock:
                self._hwnd = None
                self._subscribed = False
            diagnostics.log("listener", "stopped")

    def _wndproc_impl(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """窗口过程回调。

        Args:
          hwnd: 窗口句柄。
          msg: 消息编号。
          wparam: 消息参数。
          lparam: 消息参数。

        Returns:
          消息处理结果。
        """
        if msg == WM_CLIPBOARDUPDATE:
            self._handle_update()
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def set_text(self, text: str) -> bool:
        """把文本写回剪贴板（唯一写回路径，含「撤销」）。

        写回前**先登记内容哈希抑制标志**，使写回引发的 ``WM_CLIPBOARDUPDATE``
        被精确消费一次，避免死循环 / 重复清洗。

        Args:
          text: 待写回文本。

        Returns:
          写入成功返回 True，失败静默返回 False。
        """
        self._suppress_next(_digest(text))
        return clipboard_io.write_text(text)

    def _suppress_next(self, digest: str) -> None:
        """登记「抑制下一次内容为 digest 的更新事件」。

        Args:
          digest: 待抑制内容对应的 sha256 摘要。
        """
        with self._lock:
            self._suppress_digest = digest
            self._suppress_deadline = time.monotonic() + _SUPPRESS_WINDOW

    def read_text(self) -> Optional[str]:
        """读取剪贴板文本（委托 ``clipboard_io``）。

        Returns:
          剪贴板文本；失败返回 None。
        """
        return clipboard_io.read_text()

    def read_html(self) -> Optional[str]:
        """读取剪贴板 ``CF_HTML`` 片段（委托 ``clipboard_io``）。

        Returns:
          HTML 片段字符串；失败返回 None。
        """
        return clipboard_io.read_html()

    def _handle_update(self) -> None:
        """处理一次剪贴板更新（先读 → ① 抑制消费 → ② 内容哈希防抖）。

        每次事件都在本地诊断日志留下**指纹**（长度 + 哈希前 8 位，不含原文），
        用于区分「事件根本没收到」与「收到了但没命中规则」这两种完全不同的故障。
        """
        text = clipboard_io.read_text()
        if not text:
            diagnostics.log("clipboard", "event-read-empty")
            return
        digest = _digest(text)
        now = time.monotonic()

        # ① 抑制校验（无条件优先，且「消费一次」即失效）。
        #    顺手登记 _last_digest，使写回若触发多次同内容事件时，后续事件再被②折叠，
        #    从而保证「写回事件注入两次仍只被处理一次」。
        with self._lock:
            if self._suppress_digest == digest and now <= self._suppress_deadline:
                self._suppress_digest = None
                self._last_digest = digest
                self._last_event = now
                diagnostics.log(
                    "clipboard", f"suppressed-writeback {diagnostics.describe(text)}"
                )
                return

        # ② 内容哈希防抖：仅折叠「同一次复制」产生的重复同内容事件。
        with self._lock:
            if (
                digest == self._last_digest
                and (now - self._last_event) < _DEBOUNCE_SECONDS
            ):
                diagnostics.log("clipboard", "debounced-duplicate")
                return
            self._last_digest = digest
            self._last_event = now

        diagnostics.log("clipboard", f"dispatch {diagnostics.describe(text)}")
        try:
            self._on_text(text)
        except Exception as error:
            diagnostics.log("clipboard", f"handler-error {type(error).__name__}: {error}")

    def _read_clipboard_text(self) -> Optional[str]:
        """兼容旧调用：读取剪贴板文本（委托 ``clipboard_io.read_text``）。

        Returns:
          剪贴板文本；失败返回 None。
        """
        return clipboard_io.read_text()

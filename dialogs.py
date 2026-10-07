"""模态对话框的**统一闸门**：同一时刻只允许一个对话框。

为什么必须有这个模块（2026-10-06 实测复现的真实故障）
------------------------------------------------------
``tray`` 里的消息框是在**托盘线程**上同步调用的，而模态框自己的消息循环会
继续给托盘窗口派发消息 —— 也就是说**弹窗开着的时候托盘菜单仍然能点**。
于是「关于」可以被点第二次，第二个消息框会叠在第一个上面：

    [动作] 收到第 1 次「关于」点击 → 弹框
    [动作] 收到第 2 次「关于」点击 → 弹框      ← 第一个框还开着
    [量测] 两次点击之后，同标题弹窗数量 = 2
    [结果] 点一次「确定」之后，还剩 1 个弹窗   ← 用户：点了确定窗口却不消失

用户看到的就是「点了『确定』窗口却不消失」——其实每点一次都关掉了一个，
只是底下还压着一个。这类「点了没反应」的观感是本项目最忌讳的故障。

本模块提供三件事
----------------
1. **单例闸门**：:func:`begin` / :func:`end`。已有对话框打开时，后续请求
   **不再新开窗口**，改为把已有窗口提到前台（:func:`reforeground`）并记日志 ——
   既有可见反馈，又不会留下关不完的窗口；
2. **统一的消息框入口**：:func:`message` / :func:`ask_yes_no`，一律带
   ``MB_SETFOREGROUND | MB_TOPMOST``，保证弹出来就在最前面、能立刻被点到，
   不会出现「框在别的窗口后面、点了没反应」；
3. **对话框是否打开的查询**：:func:`is_busy` / :func:`current_title`，
   供菜单回调判断当前是否处于模态状态。

依赖方向：本模块只 import ``diagnostics``（不 import 本项目任何其他模块），
可以被任意模块安全引用。
"""

from __future__ import annotations

import ctypes
import threading

import diagnostics

# MessageBoxW 的 flags（Win32 SDK 取值）。
MB_ICON_INFO = 0x00000040
MB_ICON_QUESTION = 0x00000020
MB_YESNO = 0x00000004
# 让消息框**成为前台窗口**：否则它可能弹在别的窗口后面，第一次点击只是把窗口
# 激活、并没有按到按钮 —— 观感同样是「点了确定没反应」。
MB_SETFOREGROUND = 0x00010000
# 置顶：托盘应用的消息框必须让用户**一眼看到**，不能藏在浏览器后面。
MB_TOPMOST = 0x00040000

# 消息框按钮返回值。
IDYES = 6

# 系统消息框的窗口类名（「把已有对话框提到前台」时用它识别）。
SYSTEM_DIALOG_CLASS = "#32770"

# 提到前台用的 SetWindowPos 参数。
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040

# 统一给消息框加上的修饰位。
_MODAL_FLAGS = MB_SETFOREGROUND | MB_TOPMOST

_lock = threading.Lock()
_busy = False
_title = ""
_depth = 0


def message_flags(base: int) -> int:
    """给消息框的 buttons/icon 位加上统一的模态修饰位。

    Args:
      base: 按钮与图标位（如 ``MB_ICON_INFO`` / ``MB_YESNO | MB_ICON_QUESTION``）。

    Returns:
      可直接传给 ``MessageBoxW`` 的 flags。
    """
    return base | _MODAL_FLAGS


def is_busy() -> bool:
    """当前是否有对话框正在显示。

    Returns:
      有对话框打开返回 True。
    """
    with _lock:
        return _busy


def current_title() -> str:
    """正在显示的对话框标题（无对话框时为空串）。

    Returns:
      标题文本。
    """
    with _lock:
        return _title


def begin(title: str) -> bool:
    """尝试占用「对话框位」。

    Args:
      title: 即将显示的对话框标题。

    Returns:
      True 表示可以显示；False 表示已有对话框在显示，**调用方不得再开新窗口**。
    """
    global _busy, _title, _depth
    with _lock:
        if _busy:
            return False
        _busy = True
        _title = title
        _depth = 0
        return True


def end() -> None:
    """释放「对话框位」（可重入计数归零后释放）。"""
    global _busy, _title
    with _lock:
        if _depth > 0 or not _busy:
            return
        _busy = False
        _title = ""


def enter_nested() -> None:
    """在已持有对话框位的情况下进入一次嵌套调用（只计数，不改变状态）。"""
    global _depth
    with _lock:
        _depth += 1


def leave_nested() -> None:
    """退出一次嵌套调用。"""
    global _depth
    with _lock:
        if _depth > 0:
            _depth -= 1


def _find_open_dialog() -> int:
    """在当前线程的窗口里找我们打开着的对话框。

    Returns:
      窗口句柄；找不到返回 0。
    """
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.EnumThreadWindows.restype = wintypes.BOOL
        user32.EnumThreadWindows.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.LPARAM]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        thread_id = int(kernel32.GetCurrentThreadId())
        found: list = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _visit(hwnd, _lparam):
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buffer, 256)
            if buffer.value == SYSTEM_DIALOG_CLASS:
                found.append(hwnd)
            return True

        user32.EnumThreadWindows(thread_id, _visit, 0)
        return int(found[0]) if found else 0
    except Exception:
        return 0


def reforeground() -> bool:
    """把当前打开着的对话框提到前台（作为「点不动」时的可见反馈）。

    Returns:
      成功把某个窗口提到前台返回 True。
    """
    hwnd = _find_open_dialog()
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _message_box_raw(text: str, title: str, flags: int) -> int:
    """直接调用 ``MessageBoxW``（单独抽出来便于测试替换）。

    Args:
      text: 正文。
      title: 标题。
      flags: 完整 flags。

    Returns:
      被点击按钮的 ID；调用失败返回 0。
    """
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:
        return 0


def message(text: str, title: str) -> None:
    """弹出一个只读信息框（同一时刻只允许一个）。

    若已有对话框在显示，则**不新开窗口**，改为把已有窗口提到前台并记日志。

    Args:
      text: 正文。
      title: 标题。
    """
    if not begin(title):
        diagnostics.log("dialog", f"suppressed info {title!r} (busy: {current_title()!r})")
        reforeground()
        return
    diagnostics.log("dialog", f"open info {title!r}")
    try:
        _message_box_raw(text, title, message_flags(MB_ICON_INFO))
    finally:
        end()
        diagnostics.log("dialog", f"closed info {title!r}")


def ask_yes_no(text: str, title: str) -> bool:
    """弹出「是/否」对话框（同一时刻只允许一个）。

    Args:
      text: 正文。
      title: 标题。

    Returns:
      用户点「是」返回 True；点「否」、被抑制或调用失败返回 False。
    """
    if not begin(title):
        diagnostics.log("dialog", f"suppressed ask {title!r} (busy: {current_title()!r})")
        reforeground()
        return False
    diagnostics.log("dialog", f"open ask {title!r}")
    try:
        return _message_box_raw(text, title, message_flags(MB_YESNO | MB_ICON_QUESTION)) == IDYES
    finally:
        end()
        diagnostics.log("dialog", f"closed ask {title!r}")


def guard(label: str) -> bool:
    """菜单回调用的轻量检查：对话框开着时不要再开新窗口。

    Args:
      label: 回调短名（写日志用）。

    Returns:
      True 表示当前**有**对话框在显示、调用方应当直接返回。
    """
    if not is_busy():
        return False
    diagnostics.log("dialog", f"menu {label} suppressed: busy {current_title()!r}")
    reforeground()
    return True


def find_open_dialog() -> int:
    """对外暴露「找当前对话框句柄」，供测试与诊断使用。

    Returns:
      窗口句柄；没有返回 0。
    """
    return _find_open_dialog()

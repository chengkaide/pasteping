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

2026-10-07 又补了第三种、也是最隐蔽的一种「点了没反应」
----------------------------------------------------------
用户反馈托盘菜单「关于」点了没反应、只能从**任务栏**把它关掉。根因有两层：

1. **前台锁**。pystray 的菜单回调是在 ``TrackPopupMenuEx`` **返回之后**才执行的
   （``_win32.py`` 用的是 ``TPM_RETURNCMD``），此刻前台窗口已经交还给用户原来那个
   程序。Win32 规定只有「当前前台进程」或「刚收到用户输入的进程」才能调用
   ``SetForegroundWindow``，于是消息框自带的 ``MB_SETFOREGROUND`` 会被**静默拒绝**：
   窗口只在任务栏闪一下。所以要靠 :func:`_force_to_front` 用 ``AttachThreadInput``
   把输入队列临时合并，绕开这条限制。
2. **一个传参 Bug**。``SetWindowPos`` 的 ``hWndInsertAfter`` 没声明 ``argtypes``，
   而 ``HWND_TOPMOST`` 在 SDK 里是**指针宽**的 ``(HWND)-1``；ctypes 在 64 位下会把
   Python 的 ``-1`` 按 32 位整数传递，于是它不等于 ``HWND_TOPMOST``，被当成一个
   非法窗口句柄 —— 实测 ``SetWindowPos`` 返回 0、``GetLastError`` 为
   **1400 (ERROR_INVALID_WINDOW_HANDLE)**，即**整条「提到前台」的保证是空的**。
   现在统一走 :func:`_set_topmost`，它声明了 argtypes 并传指针宽的常量。

本模块提供四件事
----------------
1. **单例闸门**：:func:`begin` / :func:`end`。已有对话框打开时，后续请求
   **不再新开窗口**，改为把已有窗口提到前台（:func:`reforeground`）并记日志 ——
   既有可见反馈，又不会留下关不完的窗口；
2. **统一的消息框入口**：:func:`message` / :func:`ask_yes_no`，一律带
   ``MB_SETFOREGROUND | MB_TOPMOST``；
3. **弹出来之后主动抢前台**：开一个短命的看守线程，等消息框一出现就
   **居中到光标所在显示器**并强行提到最前（:func:`_place_and_raise`）——
   这比只依赖 ``MB_SETFOREGROUND`` 可靠得多，也是「点了没反应」的正解；
4. **对话框是否打开的查询**：:func:`is_busy` / :func:`current_title`，
   供菜单回调判断当前是否处于模态状态。

依赖方向：本模块只 import ``diagnostics``（不 import 本项目任何其他模块），
可以被任意模块安全引用。
"""

from __future__ import annotations

import ctypes
import threading
import time

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

# 提到前台/居中用的 SetWindowPos 参数。
# ⚠️ ``HWND_TOPMOST`` 在 Win32 SDK 里是 ``(HWND)-1`` —— **指针宽**的常量。
# 若 ctypes 没声明 ``argtypes``，Python 的 ``-1`` 会按 32 位整数传递，
# 于是它不等于 ``HWND_TOPMOST``，被当成非法窗口句柄：SetWindowPos 返回 0、
# GetLastError = 1400。x64 上必须传成指针宽的值（见 ``_set_topmost``）。
_HWND_TOPMOST = ctypes.c_void_p(-1 & ((1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1))
_HWND_NOTOPMOST = ctypes.c_void_p(-2 & ((1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1))
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040

# 居中到「光标所在显示器」时用到的 MonitorFromPoint 标志。
_MONITOR_DEFAULTTONEAREST = 0x00000002

# 看守线程的扫描节奏与上限。
_RAISE_POLL_SECONDS = 0.05
_RAISE_TIMEOUT_SECONDS = 3.0

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


def _find_dialog_on_thread(thread_id: int) -> int:
    """在**指定线程**创建的窗口里找我们打开着的对话框。

    为什么要传线程 ID：消息框是在调用线程上创建的，``EnumThreadWindows``
    只枚举**指定线程**的窗口。看守线程与创建线程不是同一个，
    所以不能沿用 ``GetCurrentThreadId``。

    Args:
      thread_id: 创建消息框的线程 ID。

    Returns:
      窗口句柄；找不到返回 0。
    """
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.EnumThreadWindows.restype = wintypes.BOOL
        user32.EnumThreadWindows.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.LPARAM]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        found: list = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _visit(hwnd, _lparam):
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buffer, 256)
            if buffer.value == SYSTEM_DIALOG_CLASS:
                found.append(hwnd)
            return True

        user32.EnumThreadWindows(int(thread_id), _visit, 0)
        return int(found[0]) if found else 0
    except Exception:
        return 0


def _current_thread_id() -> int:
    """当前线程 ID（取不到时返回 0）。"""
    try:
        return int(ctypes.windll.kernel32.GetCurrentThreadId())
    except Exception:
        return 0


def _find_open_dialog() -> int:
    """在当前线程的窗口里找我们打开着的对话框。

    Returns:
      窗口句柄；找不到返回 0。
    """
    thread_id = _current_thread_id()
    return _find_dialog_on_thread(thread_id) if thread_id else 0


def _set_topmost(hwnd: int, move_to=None) -> bool:
    """把窗口置顶（可选同时移动），并**声明 argtypes** 以避开指针宽常量陷阱。

    ``HWND_TOPMOST`` 是 ``(HWND)-1``，指针宽；不声明 argtypes 时 ctypes 会把
    Python 的 ``-1`` 当 32 位整数传，导致调用被拒（``GetLastError`` = 1400）。
    这里统一声明原型 + 传指针宽常量，因此返回值**可以当真的用**。

    Args:
      hwnd: 目标窗口句柄。
      move_to: ``(x, y)`` 时同时移动窗口；None 表示只改 z 序。

    Returns:
      Win32 调用返回非零时 True。
    """
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        if move_to is None:
            x, y = 0, 0
            flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW
        else:
            x, y = int(move_to[0]), int(move_to[1])
            flags = _SWP_NOSIZE | _SWP_SHOWWINDOW
        return bool(user32.SetWindowPos(hwnd, _HWND_TOPMOST, x, y, 0, 0, flags))
    except Exception:
        return False


def _force_to_front(hwnd: int) -> bool:
    """绕过 Windows 前台锁，把窗口强行提到最前并激活。

    为什么需要 ``AttachThreadInput``
    -------------------------------
    Win32 规定：只有「当前前台进程」或「刚刚收到用户输入的进程」才有资格调用
    ``SetForegroundWindow``。而 pystray 的菜单回调是在 ``TrackPopupMenuEx``
    **返回之后**才执行的，此时前台窗口已交还给用户原来那个程序 ——
    于是消息框自带的 ``MB_SETFOREGROUND`` 会被静默拒绝，窗口只在任务栏闪一下，
    用户看到的就是「点了『关于』没反应，只能去任务栏关掉」。
    ``AttachThreadInput`` 把两个线程的输入队列临时合并，即可绕开这条限制；
    这是该问题公认的标准解法。

    Args:
      hwnd: 要提到前台的窗口句柄。

    Returns:
      调用链没有抛异常返回 True（提不动也静默 —— 绝不能让窗口操作把进程搞崩）。
    """
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetFocus.argtypes = [wintypes.HWND]

        _set_topmost(hwnd)

        front = user32.GetForegroundWindow()
        front_thread = int(user32.GetWindowThreadProcessId(front, None)) if front else 0
        our_thread = _current_thread_id()
        attached = False
        if front_thread and our_thread and front_thread != our_thread:
            attached = bool(user32.AttachThreadInput(front_thread, our_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(front_thread, our_thread, False)
        return True
    except Exception:
        return False


def _centre_on_cursor_monitor(hwnd: int) -> bool:
    """把窗口移到**光标所在显示器**工作区的正中央。

    多显示器 / 缩放环境下，系统给消息框挑的位置可能落在另一块屏幕、或压在任务栏
    下面，用户会以为「根本没弹出来」。显式居中比听天由命可靠。

    Args:
      hwnd: 目标窗口句柄。

    Returns:
      移动成功返回 True。
    """
    try:
        from ctypes import wintypes

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return False
        monitor = user32.MonitorFromPoint(point, _MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return False
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        work = info.rcWork
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        left = work.left + max(0, (work.right - work.left - width) // 2)
        top = work.top + max(0, (work.bottom - work.top - height) // 2)
        return _set_topmost(hwnd, (left, top))
    except Exception:
        return False


def _place_and_raise(thread_id: int, title: str, finished: threading.Event) -> None:
    """看守线程：等消息框出现后，把它居中并强行提到最前。

    为什么要单开线程：``MessageBoxW`` 是**阻塞**的，调用它的那一行之后
    再也没有代码会执行，没法在窗口显示之后再去调整位置或抢前台。

    Args:
      thread_id: 创建消息框的线程 ID。
      title: 对话框标题（仅用于日志）。
      finished: 主调用返回时置位；置位后本线程立刻收工，不留残余线程。
    """
    deadline = time.time() + _RAISE_TIMEOUT_SECONDS
    while not finished.is_set() and time.time() < deadline:
        hwnd = _find_dialog_on_thread(thread_id)
        if hwnd:
            placed = _centre_on_cursor_monitor(hwnd)
            front = _force_to_front(hwnd)
            diagnostics.log("dialog", f"raised {title!r} centred={placed} front={front}")
            return
        finished.wait(_RAISE_POLL_SECONDS)
    if not finished.is_set():
        diagnostics.log("dialog", f"raise-timeout {title!r}")


def reforeground() -> bool:
    """把当前打开着的对话框提到前台（作为「点不动」时的可见反馈）。

    Returns:
      成功把某个窗口提到前台返回 True。
    """
    hwnd = _find_open_dialog()
    if not hwnd:
        diagnostics.log("dialog", "reforeground: no open dialog found")
        return False
    placed = _centre_on_cursor_monitor(hwnd)
    front = _force_to_front(hwnd)
    diagnostics.log("dialog", f"reforeground centred={placed} front={front}")
    return front


def _message_box_raw(text: str, title: str, flags: int) -> int:
    """直接调用 ``MessageBoxW``（单独抽出来便于测试替换）。

    这里**显式声明原型**：与 ``SetWindowPos`` 同一个道理 —— 声明了参数类型，
    ctypes 才会按指针宽度传参，不会把 NULL 或句柄按 32 位截断。

    Args:
      text: 正文。
      title: 标题。
      flags: 完整 flags。

    Returns:
      被点击按钮的 ID；调用失败返回 0。
    """
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT,
        ]
        return int(user32.MessageBoxW(None, text, title, flags))
    except Exception:
        return 0


def _start_raise_watcher(title: str) -> threading.Event:
    """启动看守线程，等消息框出现后把它居中并提到最前。

    Args:
      title: 对话框标题（日志用）。

    Returns:
      主调用结束时必须 ``set()`` 的事件（让看守线程立刻收工）。
    """
    finished = threading.Event()
    threading.Thread(
        target=_place_and_raise,
        args=(_current_thread_id(), title, finished),
        daemon=True,
    ).start()
    return finished


def message(text: str, title: str) -> None:
    """弹出一个只读信息框（同一时刻只允许一个）。

    若已有对话框在显示，则**不新开窗口**，改为把已有窗口提到前台并记日志。
    窗口一出现就会被居中到光标所在显示器并强行提到最前（见 :func:`_place_and_raise`），
    以免用户在任务栏里才找得到它。

    Args:
      text: 正文。
      title: 标题。
    """
    if not begin(title):
        diagnostics.log("dialog", f"suppressed info {title!r} (busy: {current_title()!r})")
        reforeground()
        return
    diagnostics.log("dialog", f"open info {title!r}")
    finished = _start_raise_watcher(title)
    try:
        _message_box_raw(text, title, message_flags(MB_ICON_INFO))
    finally:
        finished.set()
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
    finished = _start_raise_watcher(title)
    try:
        return _message_box_raw(text, title, message_flags(MB_YESNO | MB_ICON_QUESTION)) == IDYES
    finally:
        finished.set()
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

"""统一对话框闸门与消息框真机行为的回归测试。

这些用例守着 2026-10-06 复现出来的那个真实故障：

    连着点两次「关于」→ 叠出两个一模一样的消息框 →
    点一次「确定」只关掉上面那个，底下还压着一个 →
    用户看到的就是「点了『确定』窗口却不消失」。

修复分两层，本文件各有用例守着：

1. **闸门**（``dialogs``）：同一时刻只允许一个对话框，后来的请求不再新开窗口，
   改为把已有窗口提到前台（有可见反馈），并且**一个窗口都不会遗留**；
2. **菜单回调入口检查**（``tray.TrayApp._dialog_busy``）：
   弹窗开着的时候再点会弹窗的菜单项，会被挡掉。

真机用例（``TestRealDialogs``）需要桌面会话；无桌面时自动跳过。
"""

import os
import sys
import threading
import time

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import dialogs  # noqa: E402

# Windows 消息与常量。
_WM_CLOSE = 0x0010
_BM_CLICK = 0x00F5
_DIALOG_CLASS = "#32770"

_TITLE = "PastePing 测试对话框"
_TEXT = "这是测试用的对话框正文。"


@pytest.fixture(autouse=True)
def _reset_gate():
    """每个用例前后都把闸门复位，避免用例之间互相影响。"""
    dialogs._busy = False
    dialogs._title = ""
    dialogs._depth = 0
    yield
    dialogs._busy = False
    dialogs._title = ""
    dialogs._depth = 0


def _user32():
    """返回设置了必要原型的 user32（与本项目其它真机测试同样的做法）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumThreadWindows.restype = wintypes.BOOL
    user32.EnumThreadWindows.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user32


def _hval(handle) -> int:
    """把句柄统一成整数。"""
    value = getattr(handle, "value", handle)
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(bytes(value), "little")
    try:
        return int(handle)
    except Exception:
        return 0


def _title_of(user32, hwnd) -> str:
    """取窗口标题。"""
    import ctypes

    buffer = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buffer, 256)
    return buffer.value


def _class_of(user32, hwnd) -> str:
    """取窗口类名。"""
    import ctypes

    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _boxes_with_title(title: str) -> list:
    """枚举屏幕上所有标题匹配的消息框。"""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _lparam):
        if _class_of(user32, hwnd) == _DIALOG_CLASS and _title_of(user32, hwnd) == title:
            found.append(_hval(hwnd))
        return True

    user32.EnumWindows(_visit, 0)
    return found


def _ok_button(hwnd) -> int:
    """在消息框里找「确定」按钮（中文系统上它的控件 ID 是 2，不能按 IDOK 找）。"""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(child, _lparam):
        if _class_of(user32, child).lower() == "button":
            text = _title_of(user32, child)
            if text.startswith("确定") or text.upper().startswith("OK"):
                found.append(_hval(child))
        return True

    user32.EnumChildWindows(hwnd, _visit, 0)
    return found[0] if found else 0


def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.05):
    """轮询等待条件成立。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class _BoxSession:
    """在后台线程里弹一个真实消息框，供真机用例驱动。"""

    def __init__(self, text: str = _TEXT, title: str = _TITLE) -> None:
        self.text = text
        self.title = title
        self.thread_id: int = 0
        self.done = threading.Event()
        self.error: list = []

    def __enter__(self) -> "_BoxSession":
        def _worker() -> None:
            import ctypes

            self.thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
            try:
                dialogs.message(self.text, self.title)
            except Exception as error:  # noqa: BLE001 - 收集后交给断言
                self.error.append(repr(error))
            finally:
                self.done.set()

        self.thread = threading.Thread(target=_worker, daemon=True)
        self.thread.start()
        assert _wait_for(lambda: self.thread_id) is not None, "后台线程没有起来"
        assert _wait_for(lambda: _boxes_with_title(self.title)) is not None, (
            "消息框没有弹出来（若为无桌面环境，请跳过本用例）"
        )
        return self

    def __exit__(self, *_exc) -> None:
        for hwnd in _boxes_with_title(self.title):
            import ctypes

            _user32().PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        self.thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# 闸门本身的逻辑（不需要桌面）
# --------------------------------------------------------------------------- #
class TestGate:
    """闸门的纯逻辑行为。"""

    def test_begin_and_end(self):
        assert dialogs.is_busy() is False
        assert dialogs.begin("A") is True
        assert dialogs.is_busy() is True
        assert dialogs.current_title() == "A"
        dialogs.end()
        assert dialogs.is_busy() is False
        assert dialogs.current_title() == ""

    def test_second_begin_is_refused(self):
        """同一时刻只能有一个对话框：第二次 begin 必须被拒绝。"""
        assert dialogs.begin("A") is True
        assert dialogs.begin("B") is False, "闸门没有挡住第二个对话框"
        assert dialogs.current_title() == "A", "当前对话框标题被后来者改写了"
        dialogs.end()
        assert dialogs.begin("B") is True
        dialogs.end()

    def test_message_suppressed_while_busy(self, monkeypatch):
        """已有对话框在显示时，message() 不得新开窗口，而要提示已有窗口。"""
        opened: list = []
        refocused: list = []
        monkeypatch.setattr(
            dialogs, "_message_box_raw", lambda text, title, flags: opened.append(title) or 1
        )
        monkeypatch.setattr(dialogs, "reforeground", lambda: refocused.append(True) or True)

        dialogs.begin("占位对话框")
        dialogs.message("正文", "被抑制的对话框")
        dialogs.end()

        assert opened == [], "已经有对话框了还新开了窗口（就是叠窗的元凶）"
        assert refocused, "被抑制时没有把已有对话框提到前台，用户会看到「点了没反应」"

    def test_ask_yes_no_suppressed_returns_false(self, monkeypatch):
        """问询框被抑制时必须返回 False，且不新开窗口。"""
        opened: list = []
        monkeypatch.setattr(
            dialogs, "_message_box_raw", lambda text, title, flags: opened.append(flags) or 6
        )
        monkeypatch.setattr(dialogs, "reforeground", lambda: True)

        dialogs.begin("占位对话框")
        result = dialogs.ask_yes_no("正文", "被抑制的问询框")
        dialogs.end()

        assert result is False, "被抑制的问询框不能返回「是」"
        assert opened == []

    def test_guard_reports_busy(self, monkeypatch):
        """菜单回调入口检查：有对话框时必须自报「忙」。"""
        monkeypatch.setattr(dialogs, "reforeground", lambda: True)
        assert dialogs.guard("about") is False
        dialogs.begin("占位对话框")
        assert dialogs.guard("about") is True
        dialogs.end()
        assert dialogs.guard("about") is False

    def test_flags_force_foreground_and_topmost(self):
        """消息框必须带「置前 + 置顶」位，否则可能弹在别的窗口后面点不到。"""
        flags = dialogs.message_flags(dialogs.MB_ICON_INFO)
        assert flags & dialogs.MB_SETFOREGROUND
        assert flags & dialogs.MB_TOPMOST
        # 不能把调用方给的按钮位吃掉。
        assert flags & dialogs.MB_ICON_INFO

    def test_gate_is_reentrant_aware(self):
        """闸门状态在 begin/end 之间必须自洽（原「自绘输入框共用闸门」用例的替代）。

        2026-10-07 起项目里**只剩消息框一种对话框**（激活码输入框已随授权体系移除），
        因此原用例改测闸门本身：占用 → 忙 → 释放 → 不忙，且过程中不影响其它常量可用。
        """
        assert dialogs.begin("占位对话框") is True
        assert dialogs.is_busy() is True
        assert dialogs.message_flags(0) & dialogs.MB_SETFOREGROUND  # 顺带确认常量可用
        dialogs.end()
        assert dialogs.is_busy() is False


# --------------------------------------------------------------------------- #
# 真机：真弹窗、真点击
# --------------------------------------------------------------------------- #
class TestRealDialogs:
    """真机行为：点「确定」必须真的关掉窗口，且不允许叠窗。"""

    def test_ok_button_closes_the_box(self):
        """点「确定」→ 窗口消失且调用返回（这是用户报的那个「不消失」的正解）。"""
        with _BoxSession() as session:
            boxes = _boxes_with_title(_TITLE)
            assert boxes, "消息框没有出现"
            button = _ok_button(boxes[0])
            assert button, "没能在消息框里找到「确定」按钮"

            _user32().SendMessageW(button, _BM_CLICK, 0, 0)

            assert _wait_for(lambda: not _boxes_with_title(_TITLE), timeout=3.0), (
                "点了「确定」窗口却没有消失"
            )
            assert session.done.wait(timeout=3.0), "窗口关了但调用没有返回"
            assert session.error == []

    def test_second_dialog_is_not_stacked(self):
        """已有弹窗时再请求弹窗：不能再开一个（这正是「点了确定不消失」的成因）。"""
        with _BoxSession() as session:
            assert len(_boxes_with_title(_TITLE)) == 1

            # 模拟用户在第一个框还开着时又点了一次「关于」。
            dialogs.message("第二份正文", _TITLE)
            time.sleep(0.4)

            assert len(_boxes_with_title(_TITLE)) == 1, "对话框被叠起来了"
            assert dialogs.is_busy() is True, "闸门状态被破坏"

            # 关掉唯一那个窗口必须一次到位。
            box = _boxes_with_title(_TITLE)[0]
            _user32().SendMessageW(_ok_button(box), _BM_CLICK, 0, 0)
            assert _wait_for(lambda: not _boxes_with_title(_TITLE), timeout=3.0)
            assert session.done.wait(timeout=3.0)

    def test_about_handler_refuses_when_busy(self, monkeypatch):
        """「关于」在已有对话框时必须被挡掉，不能叠第二个。"""
        import tray

        shown: list = []
        monkeypatch.setattr(
            tray.TrayApp, "_message_box", staticmethod(lambda text, title: shown.append(title))
        )
        try:
            app = tray.TrayApp()
        except Exception as exc:  # pragma: no cover - 无桌面时
            pytest.skip(f"无法构造 TrayApp：{exc}")

        dialogs.begin("占位对话框")
        app._on_about(None, None)
        dialogs.end()

        assert shown == [], "已有对话框时「关于」仍然弹了一个新窗口"

    def test_second_dialog_is_refused_and_first_is_raised(self, monkeypatch):
        """闸门语义：已有对话框时**不新开窗口**，而是把已有窗口提到前台。

        2026-10-07 起项目里只剩消息框一种对话框（激活码输入框已随授权体系移除），
        故本用例直接从「第二次 message 调用」这一侧验证同一条契约。
        """
        opened: list = []
        raised: list = []
        monkeypatch.setattr(
            dialogs, "_message_box_raw", lambda text, title, flags: opened.append(title) or 1
        )
        monkeypatch.setattr(dialogs, "reforeground", lambda: raised.append(True) or True)

        dialogs.begin("占位对话框")
        dialogs.message("正文", "被抑制的对话框")
        dialogs.end()

        assert opened == [], "已有对话框时不该再新开窗口（会叠出第二个）"
        assert raised, "被挡下时必须把已有窗口提到前台，否则用户看到的就是「点了没反应」"

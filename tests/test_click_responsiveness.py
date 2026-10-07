"""点必有应：托盘菜单每一项在任何状态下都必须给出**可见响应**。

背景（2026-10-06 用户实测反馈「很多按钮点击无反应」）。定位到两类根因，本文件各有一组用例：

1. **结构性死按键** —— 历史上 ``_on_locked_guide`` 曾带「会话内只弹一次」门控，
   而它同时挂在「自动清洗粘贴」「Markdown → Word」「网页表格 → Excel」三个菜单项上，
   于是用户点过一次之后这三个按钮全部变成静默 ``return``。该函数与那个门控
   **已于 2026-10-07 一并删除**（全部功能免费后不存在「未解锁引导」），
   回归守卫在 ``test_qa_v02_adversarial.py::test_dead_key_class_of_bug_is_gone``。
2. **静默异常** —— pystray 的 Win32 后端在托盘消息循环里**直接调用**回调，回调抛出的
   异常被消息循环吞掉；``--windowed`` 的 exe 既没有控制台也没有 stderr，
   观感同样是「点了没反应」。

约束：全部走假对象与 monkeypatch —— **不启动 pystray 的 run()、不起 Win32 消息循环、
不弹任何真实对话框写入剪贴板**。
"""

from __future__ import annotations

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config  # noqa: E402
import diagnostics  # noqa: E402
import tray  # noqa: E402


def _read_source(name: str) -> str:
    """读取项目根下某个文件源码。

    Args:
      name: 相对项目根的文件名。

    Returns:
      文件内容。
    """
    with open(os.path.join(_PROJECT_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后恢复 config 初始状态。"""
    config.reset()
    yield
    config.reset()


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """把 APPDATA 指到临时目录并打开诊断日志。

    Args:
      tmp_path: pytest 临时目录。
      monkeypatch: pytest 补丁夹具。

    Returns:
      日志文件路径。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    diagnostics.set_enabled(True)
    yield diagnostics.log_path()
    diagnostics.set_enabled(False)


@pytest.fixture
def app(monkeypatch):
    """构造一个 TrayApp，并把弹窗 / 通知 / 问句全部换成可观测的假实现。

    Args:
      monkeypatch: pytest 补丁夹具。

    Returns:
      ``(app, seen)``：``app`` 为托盘对象，``seen`` 记录各类反馈。
    """
    seen: dict = {"boxes": [], "asks": [], "notices": []}

    def _box(text, title):
        seen["boxes"].append(text)

    def _ask(text, title):
        seen["asks"].append(text)
        return False

    monkeypatch.setattr(tray.TrayApp, "_message_box", staticmethod(_box))
    monkeypatch.setattr(tray.TrayApp, "_ask_yes_no", staticmethod(_ask))

    try:
        instance = tray.TrayApp()
    except Exception as error:  # pragma: no cover - 环境缺 pystray 时跳过
        pytest.skip(f"无法构造 TrayApp：{error}")

    class _Recorder:
        """记录 notify_message 调用的假 notifier。"""

        def notify_message(self, title, message):
            seen["notices"].append((title, message))

    instance.attach_notifier(_Recorder())
    instance.rebuild_menu()
    return instance, seen


def _menu_texts(instance) -> list:
    """把菜单里的全部文字摊平成列表。

    Args:
      instance: TrayApp。

    Returns:
      菜单文字列表（含子菜单，带缩进）。
    """
    out: list = []

    def walk(items, depth=0):
        for item in items:
            text = item.text if isinstance(item.text, str) else item.text(item)
            out.append("  " * depth + str(text))
            if item.submenu is not None:
                walk(item.submenu.items, depth + 1)

    walk(instance.icon.menu.items)
    return out


class TestNoSilentClicks:
    """A：点击不可能静默消失。"""

    def test_every_menu_handler_is_guarded(self):
        """静态核对：tray 里每个 ``_on_*`` 回调都必须带 ``@_guarded`` 装饰器。

        新加菜单项时最容易忘的就是这个装饰器，而忘了就等于新加了一个
        「出错时静默」的按钮 —— 用静态检查把它钉死。
        """
        lines = _read_source("tray.py").splitlines()
        handlers = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def _on_"):
                previous = lines[index - 1].strip() if index else ""
                handlers.append((stripped, previous))
        assert len(handlers) >= 12, f"回调数量异常，疑似扫描逻辑失效：{len(handlers)}"
        for name, previous in handlers:
            assert previous.startswith("@_guarded("), f"{name} 缺少 @_guarded 装饰器"

    def test_guard_reports_exception_instead_of_silence(self, app, monkeypatch, log_file):
        """回调内部抛异常时：不向外抛、弹窗告知、日志留痕（含出错行号）。"""
        instance, seen = app

        def _boom(_text):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(tray.converter, "markdown_to_docx", _boom)
        # 关键断言：异常**不得**穿透出去（穿透出去在 exe 里就是静默）。
        instance._on_convert_markdown(None, None)

        assert seen["boxes"], "回调抛异常后必须弹窗告知用户，不能静默"
        assert "kaboom" in seen["boxes"][0]
        assert diagnostics.log_path() in seen["boxes"][0]

        with open(log_file, encoding="utf-8") as handle:
            content = handle.read()
        assert "kaboom" in content
        assert "convert-markdown" in content
        # 出错行号：形如 conv...py:123，是定位的关键。
        assert "@ " in content and ".py:" in content

    def test_guard_logs_entry_before_acting(self, app, log_file):
        """每次点击都必须先记 ``enter``，日志里能看到「点击确实被收到了」。"""
        instance, _seen = app
        instance._on_pause_5(None, None)
        with open(log_file, encoding="utf-8") as handle:
            content = handle.read()
        assert "pause-5 enter" in content
        assert "pause-5 done" in content


class TestVisibleFeedback:
    """B：状态类操作必须有看得见的反馈（原来只改 tooltip，悬停才看得到）。"""

    def test_toggle_detect_notifies_both_directions(self, app):
        instance, seen = app
        instance._on_toggle(None, None)
        instance._on_toggle(None, None)
        messages = [message for _title, message in seen["notices"]]
        assert len(messages) == 2, "两次点击必须有两条可见反馈"
        assert "已关闭" in messages[0]
        assert "已开启" in messages[1]

    def test_pause_notifies_and_menu_offers_resume(self, app):
        instance, seen = app
        instance._on_pause_5(None, None)
        assert seen["notices"], "暂停后必须有可见反馈"
        assert config.seconds_until_resume() > 0
        # 暂停中菜单必须出现「恢复」入口 —— 否则点了 1 小时就只能重启程序。
        texts = _menu_texts(instance)
        assert any("恢复提醒" in text for text in texts)
        assert not any("暂停 5 分钟" in text for text in texts)

    def test_resume_restores_and_confirms(self, app):
        instance, seen = app
        instance._on_pause_60(None, None)
        seen["notices"].clear()
        instance._on_resume(None, None)
        assert config.seconds_until_resume() == 0
        assert config.is_detection_active() is True
        assert seen["notices"], "恢复后必须有可见反馈"
        assert any("暂停 5 分钟" in text for text in _menu_texts(instance))

    def test_toggle_clean_notifies(self, app, monkeypatch):
        instance, seen = app
        instance._on_toggle_clean(None, None)
        assert seen["notices"], "切换自动清洗必须有可见反馈"
        assert any("已开启" in message for _t, message in seen["notices"])

    def test_undo_without_record_still_responds(self, app):
        """没有可撤销记录时也必须回应（原来直接 return，静默）。"""
        instance, seen = app

        class _NoUndo:
            def has_undo(self):
                return False

            def undo(self):
                return None

        instance.attach_cleaner(_NoUndo())
        instance._on_undo_clean(None, None)
        assert seen["notices"], "无可撤销时也要给出可见反馈"

    def test_support_hands_over_url_when_browser_fails(self, app, monkeypatch):
        """打不开浏览器时把地址直接给用户，而不是什么都没发生。"""
        instance, seen = app
        monkeypatch.setattr(tray, "open_url", lambda _url: False)
        instance._on_support(None, None)
        assert seen["boxes"], "打开链接失败必须把地址显示出来"
        assert tray.SUPPORT_URL in seen["boxes"][0]

    def test_support_stays_silent_when_browser_opens(self, app, monkeypatch):
        """成功打开浏览器时不该再弹窗（避免打扰）。"""
        instance, seen = app
        monkeypatch.setattr(tray, "open_url", lambda _url: True)
        instance._on_support(None, None)
        assert not seen["boxes"]

    def test_about_always_responds(self, app):
        """「关于」每次点击都要有回应（它只是只读信息框，没有任何理由静默）。"""
        instance, seen = app
        instance._on_about(None, None)
        instance._on_about(None, None)
        assert len(seen["boxes"]) == 2, "「关于」必须每次点击都给出可见响应"
        assert "永久免费" in seen["boxes"][0]

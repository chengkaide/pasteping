"""剪贴板监听「自触发抑制 + 内容哈希防抖」单元测试（无 GUI、无真实剪贴板）。

用假剪贴板与假时钟直接驱动 ``ClipboardListener._handle_update``，验证：

* 写回失败静默返回 False；
* ``set_text`` 登记抑制后，紧跟的**相同内容事件被消费且不再回调**；
* **内容不同的连续两次事件均会回调**（不被防抖误吞）；
* 同一内容重复事件被折叠；
* 模拟「写回事件」注入两次仍只被处理一次（无死循环）。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

# 保证无论从哪个工作目录运行 pytest，都能 import 到项目根目录下的模块。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import clipboard_listener  # noqa: E402
from clipboard_listener import ClipboardListener  # noqa: E402


class _FakeClock:
    """可手动推进的假时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeClipboard:
    """假剪贴板：按队列出队读取、记录写入。"""

    def __init__(self) -> None:
        self.queue: list[str] = []
        self.written: list[str] = []
        self.write_ok = True

    def read_text(self):
        if self.queue:
            return self.queue.pop(0)
        return None

    def read_html(self):
        return None

    def write_text(self, text: str) -> bool:
        self.written.append(text)
        return self.write_ok


@pytest.fixture()
def env(monkeypatch):
    """构造 (listener, fake, clock, collector) 测试环境。"""
    clock = _FakeClock()
    monkeypatch.setattr(clipboard_listener, "time", types.SimpleNamespace(monotonic=clock))
    fake = _FakeClipboard()
    monkeypatch.setattr(clipboard_listener, "clipboard_io", fake)
    collector: list[str] = []
    listener = ClipboardListener(on_text=collector.append)
    return listener, fake, clock, collector


# --------------------------------------------------------------------------- #
# 写回失败静默
# --------------------------------------------------------------------------- #
def test_set_text_returns_false_on_write_failure(env):
    listener, fake, _clock, _collector = env
    fake.write_ok = False
    assert listener.set_text("cleaned") is False


def test_set_text_returns_true_on_success(env):
    listener, fake, _clock, _collector = env
    assert listener.set_text("cleaned") is True
    assert fake.written == ["cleaned"]


# --------------------------------------------------------------------------- #
# 自触发抑制
# --------------------------------------------------------------------------- #
def test_writeback_event_is_consumed_once(env):
    listener, fake, _clock, collector = env
    listener.set_text("CLEANED")
    # 模拟 OS 因写回而投递的 WM_CLIPBOARDUPDATE。
    fake.queue.append("CLEANED")
    listener._handle_update()
    assert collector == [], "写回事件应被抑制消费，不得回调"


def test_following_same_content_event_folded(env):
    listener, fake, _clock, collector = env
    listener.set_text("CLEANED")
    fake.queue.append("CLEANED")  # 被抑制消费
    listener._handle_update()
    fake.queue.append("CLEANED")  # 同内容重复 → 被防抖折叠
    listener._handle_update()
    assert collector == []


def test_suppression_expires_after_window(env):
    listener, fake, clock, collector = env
    listener.set_text("X")
    clock.advance(clipboard_listener._SUPPRESS_WINDOW + 0.5)
    # 抑制窗口已过；此时用户真实复制同一内容 → 应被放行。
    fake.queue.append("X")
    listener._handle_update()
    assert collector == ["X"]


# --------------------------------------------------------------------------- #
# 内容哈希防抖（关键：不同内容永不被吞）
# --------------------------------------------------------------------------- #
def test_different_content_events_both_delivered(env):
    listener, fake, _clock, collector = env
    fake.queue.extend(["AAA", "BBB"])  # 内容不同
    listener._handle_update()
    listener._handle_update()
    assert collector == ["AAA", "BBB"]


def test_same_content_repeat_folded(env):
    listener, fake, _clock, collector = env
    fake.queue.extend(["AAA", "AAA"])  # 同一次复制的重复事件
    listener._handle_update()
    listener._handle_update()
    assert collector == ["AAA"]


def test_same_content_after_debounce_delivered(env):
    listener, fake, clock, collector = env
    fake.queue.append("AAA")
    listener._handle_update()
    clock.advance(clipboard_listener._DEBOUNCE_SECONDS + 0.05)
    fake.queue.append("AAA")
    listener._handle_update()
    assert collector == ["AAA", "AAA"]


# --------------------------------------------------------------------------- #
# 无死循环
# --------------------------------------------------------------------------- #
def test_no_infinite_loop_and_real_copy_still_delivered(env):
    listener, fake, _clock, collector = env
    listener.set_text("CLEANED")
    # 连续注入两次「写回事件」。
    fake.queue.extend(["CLEANED", "CLEANED"])
    listener._handle_update()
    listener._handle_update()
    assert collector == []
    # 用户紧接着真实复制了不同内容 → 必须被放行。
    fake.queue.append("REAL-USER-COPY")
    listener._handle_update()
    assert collector == ["REAL-USER-COPY"]


def test_empty_clipboard_no_callback(env):
    listener, fake, _clock, collector = env
    fake.queue.append("")
    listener._handle_update()
    assert collector == []


def test_read_html_delegates(env):
    listener, _fake, _clock, _collector = env
    assert listener.read_html() is None

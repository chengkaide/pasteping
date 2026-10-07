"""config 模块单元测试（纯逻辑，无 GUI / 无真实剪贴板依赖）。"""

from __future__ import annotations

import os
import sys
import types

import pytest

# 保证无论从哪个工作目录运行 pytest，都能 import 到项目根目录下的模块。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config  # noqa: E402


class _FakeClock:
    """可手动推进的假时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后都恢复初始状态，保证相互隔离。"""
    config.reset()
    yield
    config.reset()


@pytest.fixture()
def fake_clock(monkeypatch):
    """把 config 内部使用的 time.monotonic 替换为可控假时钟。"""
    clock = _FakeClock()
    monkeypatch.setattr(config, "time", types.SimpleNamespace(monotonic=clock))
    return clock


def test_default_state():
    assert config.get_enabled() is True
    assert config.is_detection_active() is True
    assert config.seconds_until_resume() == 0


def test_set_enabled():
    config.set_enabled(False)
    assert config.get_enabled() is False
    assert config.is_detection_active() is False
    config.set_enabled(True)
    assert config.get_enabled() is True
    assert config.is_detection_active() is True


def test_toggle_enabled():
    assert config.toggle_enabled() is False
    assert config.toggle_enabled() is True


def test_pause_makes_inactive(fake_clock):
    config.pause(300)
    assert config.is_detection_active() is False
    assert config.seconds_until_resume() == 300


def test_pause_expires(fake_clock):
    config.pause(30)
    assert config.is_detection_active() is False
    fake_clock.advance(29)
    assert config.is_detection_active() is False
    fake_clock.advance(2)
    assert config.is_detection_active() is True
    assert config.seconds_until_resume() == 0


def test_resume_clears_pause(fake_clock):
    config.pause(3600)
    assert config.is_detection_active() is False
    config.resume()
    assert config.is_detection_active() is True
    assert config.seconds_until_resume() == 0


def test_pause_zero_seconds_is_noop(fake_clock):
    config.pause(0)
    assert config.is_detection_active() is True


def test_should_emit_first_true_then_false(fake_clock):
    assert config.should_emit("hello") is True
    assert config.should_emit("hello") is False
    assert config.should_emit("world") is True


def test_should_emit_after_cooldown(fake_clock):
    assert config.should_emit("hello", cooldown=60) is True
    assert config.should_emit("hello", cooldown=60) is False
    fake_clock.advance(61)
    assert config.should_emit("hello", cooldown=60) is True


def test_should_emit_uses_hash_not_raw_text(fake_clock):
    config.should_emit("秘密内容")
    assert "秘密内容" not in config._last_hits
    assert len(config._last_hits) == 1


def test_should_emit_prunes_expired(fake_clock):
    config.should_emit("a", cooldown=10)
    config.should_emit("b", cooldown=10)
    assert len(config._last_hits) == 2
    fake_clock.advance(11)
    config.should_emit("c", cooldown=10)
    assert len(config._last_hits) == 1


def test_clear_history():
    config.should_emit("a")
    assert len(config._last_hits) == 1
    config.clear_history()
    assert config._last_hits == {}


def test_reset_is_clean():
    config.set_enabled(False)
    config.pause(100)
    config.should_emit("x")
    config.reset()
    assert config.get_enabled() is True
    assert config.is_detection_active() is True
    assert config.seconds_until_resume() == 0
    assert config._last_hits == {}

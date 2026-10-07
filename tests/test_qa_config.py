"""QA 对抗性验证 —— config 状态机与并发安全（独立于工程师的 test_config.py）。

重点验证「最易出竞态」的地方：``should_emit`` 必须把「判断 + 写入」放在同一把锁内，
20 个线程并发对同一文本调用时，恰好只有 1 个返回 True。

仅 import 纯逻辑模块 config，不触碰 pywin32 / GUI，不启动任何事件循环。
"""

from __future__ import annotations

import os
import sys
import threading
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
    """每个用例前后恢复初始状态，保证用例相互隔离。"""
    config.reset()
    yield
    config.reset()


@pytest.fixture()
def fake_clock(monkeypatch):
    """把 config 内部使用的 time.monotonic 替换为可控假时钟。"""
    clock = _FakeClock()
    monkeypatch.setattr(config, "time", types.SimpleNamespace(monotonic=clock))
    return clock


# --------------------------------------------------------------------------- #
# B.1 should_emit 降频状态机
# --------------------------------------------------------------------------- #
def test_should_emit_state_machine(fake_clock):
    assert config.should_emit("same") is True     # 首次
    assert config.should_emit("same") is False    # 60s 内同文本
    assert config.should_emit("other") is True    # 不同文本
    fake_clock.advance(60.1)                       # 超过 60s
    assert config.should_emit("same") is True      # 冷却结束后同文本再次放行


def test_should_emit_exactly_at_60s_boundary_is_after_cooldown(fake_clock):
    """恰好推进 60s：实现用 ``now - last < cooldown`` 判定冷却，60s 时应放行。"""
    assert config.should_emit("x", cooldown=60) is True
    fake_clock.advance(60)
    assert config.should_emit("x", cooldown=60) is True


# --------------------------------------------------------------------------- #
# B.2 并发安全（关键）
# --------------------------------------------------------------------------- #
def test_should_emit_concurrent_exactly_one_true():
    """20 线程并发对同一文本调用 should_emit，必须恰好只有 1 个 True。

    使用真实单调时钟（不使用假时钟），反复多轮以捕捉竞态。
    """
    rounds = 5
    n_threads = 20
    for round_index in range(rounds):
        config.reset()
        barrier = threading.Barrier(n_threads)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()  # 尽量让所有线程同时冲入
            value = config.should_emit("concurrent-same-text")
            with results_lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == n_threads
        assert results.count(True) == 1, (
            f"第 {round_index + 1} 轮出现竞态：True 个数 = {results.count(True)}"
        )


def test_should_emit_concurrent_distinct_texts_all_true():
    """并发调用不同文本时，每个都应各自放行一次（锁不误伤不同 key）。"""
    n_threads = 20
    config.reset()
    barrier = threading.Barrier(n_threads)
    results: dict[str, bool] = {}
    results_lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        value = config.should_emit(f"unique-{index}")
        with results_lock:
            results[f"unique-{index}"] = value

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == n_threads
    assert all(results.values()), f"不同文本应全部放行，实际：{results}"


# --------------------------------------------------------------------------- #
# B.3 暂停 / 恢复
# --------------------------------------------------------------------------- #
def test_pause_300_inactive_and_resume(fake_clock):
    config.pause(300)
    assert config.is_detection_active() is False
    remaining = config.seconds_until_resume()
    assert 299 <= remaining <= 300, f"剩余秒数异常：{remaining}"
    config.resume()
    assert config.is_detection_active() is True
    assert config.seconds_until_resume() == 0


# --------------------------------------------------------------------------- #
# B.4 总开关
# --------------------------------------------------------------------------- #
def test_disabled_even_without_pause_is_inactive():
    config.set_enabled(False)
    assert config.get_enabled() is False
    assert config.is_detection_active() is False
    # 未暂停时也不应可提醒
    assert config.seconds_until_resume() == 0


# --------------------------------------------------------------------------- #
# B.5 reset 后状态干净
# --------------------------------------------------------------------------- #
def test_reset_restores_clean_state():
    config.set_enabled(False)
    config.pause(100)
    config.should_emit("something")
    assert config._last_hits != {}

    config.reset()

    assert config.get_enabled() is True
    assert config.is_detection_active() is True
    assert config.seconds_until_resume() == 0
    assert config._last_hits == {}


def test_should_emit_stores_digest_not_raw_text(fake_clock):
    """降频表里只应存 sha256 摘要，不得驻留原文。"""
    config.should_emit("敏感原文-13800138000")
    assert "敏感原文-13800138000" not in config._last_hits
    assert all(len(key) == 64 for key in config._last_hits)

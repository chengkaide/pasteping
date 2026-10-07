"""诊断日志测试：写得出来，且**绝不包含剪贴板原文**。

「复制了没提示」这类问题在没有桌面会话的环境里无法复现，只能靠日志定位；
因此日志本身必须可信 —— 既要真的落盘，又不能把用户的剪贴板内容带出去。
"""

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import diagnostics  # noqa: E402


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """把 APPDATA 指到临时目录并打开日志，返回日志文件路径。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    diagnostics.set_enabled(True)
    return diagnostics.log_path()


def _read(path: str) -> str:
    """读取日志文本。"""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestLogBasics:
    """日志文件的基本行为。"""

    def test_log_lands_under_appdata(self, log_file):
        diagnostics.log("unit", "hello")
        assert os.path.isfile(log_file)
        assert "[unit] hello" in _read(log_file)

    def test_line_starts_with_timestamp(self, log_file):
        diagnostics.log("unit", "ts")
        line = _read(log_file).splitlines()[-1]
        assert line[:4].isdigit() and line[4] == "-", f"缺少时间戳：{line!r}"

    def test_path_ends_with_expected_name(self, log_file):
        assert log_file.endswith(os.path.join("PastePing", "pasteping.log"))

    def test_disabled_writes_nothing(self, log_file):
        diagnostics.set_enabled(False)
        diagnostics.log("unit", "should-not-appear")
        assert not os.path.exists(log_file)

    def test_location_hint_mentions_path(self, log_file):
        assert diagnostics.log_path() in diagnostics.location_hint()


class TestPrivacy:
    """隐私红线：日志只留指纹，不留原文。"""

    def test_describe_hides_content(self):
        secret = "11010519491231002X"
        described = diagnostics.describe(secret)
        assert secret not in described
        assert described.startswith("len=")
        assert "sha=" in described
        # 长度本身保留（便于判断「收到的是不是空内容」）。
        assert str(len(secret)) in described

    def test_describe_of_empty(self):
        assert diagnostics.describe("") == "len=0"
        assert diagnostics.describe(None) == "len=0"

    def test_clipboard_pipeline_never_logs_content(self, log_file, monkeypatch):
        """跑一遍真实处理链路：日志里不得出现剪贴板原文。"""
        import clipboard_listener

        secret = "内部资料 勿外传 11010519491231002X"
        monkeypatch.setattr(clipboard_listener.clipboard_io, "read_text", lambda: secret)
        listener = clipboard_listener.ClipboardListener(on_text=lambda text: None)
        listener._handle_update()
        content = _read(log_file)
        assert secret not in content
        assert "dispatch" in content

    def test_notifier_logs_rule_not_fragment(self, log_file):
        """通知日志只记规则名，不记命中片段。"""
        import notifier
        from detector import Hit

        fragment = "110***********02X"
        hit = Hit("sensitive", "敏感信息", "身份证", fragment, "body")
        lightweight = notifier.Notifier(
            on_flash_start=lambda: None,
            on_flash_end=lambda: None,
            on_notify=lambda message, title: None,
        )
        lightweight.notify(hit)
        content = _read(log_file)
        assert fragment not in content
        assert "身份证" in content

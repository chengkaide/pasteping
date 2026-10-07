"""PastePing 真机自检（smoke test）。

区别于一堆单元测试：**本脚本在真实的 Windows 桌面上跑真实的链路** ——
真正的 ``AddClipboardFormatListener`` 订阅、真正的剪贴板读写、真正的检测与清洗。
它回答的是一个单元测试回答不了的问题：

    「把 exe 双击起来以后，它在这台机器上到底会不会工作？」

用法::

    python tools/smoke_test.py              # 完整自检（会临时占用剪贴板，结束还原）
    python tools/smoke_test.py --quick      # 只做启动与订阅检查
    python tools/smoke_test.py --no-restore # 测试后不还原剪贴板（留下的就是最后一条用例）

退出码 0 表示全部通过，1 表示有失败项。

设计要点（避免自身成为一个坑）：

* **先备份、后还原**：测试要往剪贴板里写东西，跑完把用户原本的内容放回去，
  除非显式加 ``--no-restore``；
* **不依赖托盘**：通知回调被替换为记录器，因此在没有托盘图标的情况下也能跑；
  托盘本身是否显示请人工目视确认（见输出末尾的提示）；
* **零配置**：不需要任何令牌 / 激活码 / 配置文件 —— 全部功能免费，本脚本直接跑；
* **只打印指纹，不打印敏感内容**。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clipboard_io  # noqa: E402
import cleaner  # noqa: E402
import config  # noqa: E402
import detector  # noqa: E402
import diagnostics  # noqa: E402
from clipboard_listener import ClipboardListener  # noqa: E402
from notifier import Notifier  # noqa: E402

# 每条用例：(名称, 输入文本, 校验函数(清洗后文本) -> bool, 期望是否触发提醒)
_CASES: tuple[tuple[str, str, Callable[[str], bool], bool], ...] = (
    (
        "敏感信息 · 手机号 → 应被替换为占位符",
        "客户回访：张伟 13812345678，请本周内联系，反馈结果同步到项目群。",
        lambda out: "13812345678" not in out and "［已移除：手机号］" in out,
        True,
    ),
    (
        "敏感信息 · API Key → 应被替换为占位符",
        '接口调试片段：\nclient = OpenAI(api_key="sk-abc123XYZ456def789ghi")\n'
        "这段代码不要提交到公共仓库。",
        lambda out: "sk-abc123XYZ456def789ghi" not in out and "［已移除：API Key］" in out,
        True,
    ),
    (
        "敏感信息 · 身份证 → 应被替换为占位符",
        "员工入档资料：姓名 李芳，身份证号 320311197803154517，请核对后归档。",
        lambda out: "320311197803154517" not in out and "［已移除：身份证］" in out,
        True,
    ),
    (
        "禁用: 干净的技术文本 → 必须原样不动（不误报）",
        "采空区稳定性分析常用解析法、数值模拟法与现场监测法三类，本次采用数值模拟结合现场监测。",
        lambda out: out
        == "采空区稳定性分析常用解析法、数值模拟法与现场监测法三类，本次采用数值模拟结合现场监测。",
        False,
    ),
    (
        "禁用: 「仅供参考」无强语义 → 不得打扰",
        "以下内容仅供参考，如有疑问请随时联系我。",
        lambda out: out == "以下内容仅供参考，如有疑问请随时联系我。",
        False,
    ),
)

_SETTLE_SECONDS = 0.6


def _safe_clipboard_read() -> Optional[str]:
    """读取当前剪贴板文本（失败返回 None）。

    Returns:
      剪贴板文本或 None。
    """
    try:
        return clipboard_io.read_text()
    except Exception:
        return None


def _safe_clipboard_restore(text: Optional[str]) -> None:
    """尽力还原剪贴板（失败静默）。

    Args:
      text: 原剪贴板文本；None 表示原本不是文本（无从还原，保持现状）。
    """
    if text is None:
        return
    try:
        clipboard_io.write_text(text)
    except Exception:
        return


def _wait_subscribed(listener: ClipboardListener, timeout: float = 3.0) -> bool:
    """等待监听器完成订阅。

    Args:
      listener: 监听器实例。
      timeout: 最长等待秒数。

    Returns:
      已订阅返回 True。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listener.is_subscribed():
            return True
        time.sleep(0.05)
    return listener.is_subscribed()


def run_quick() -> int:
    """只做启动与订阅自检。

    Returns:
      退出码。
    """
    print("=" * 72)
    print("PastePing 真机自检（quick）")
    print("=" * 72)
    print(f"诊断日志：{diagnostics.log_path()}")
    listener = ClipboardListener(on_text=lambda _text: None)
    listener.start()
    ok = _wait_subscribed(listener)
    listener.stop()
    print(f"剪贴板事件订阅：{'成功' if ok else '失败'}")
    if not ok:
        print("!! 订阅失败意味着『以后复制什么都不会提醒』。")
        print("   常见原因：另一个剪贴板监听程序占用了名额，或权限受限。")
    return 0 if ok else 1


def run_full(restore: bool) -> int:
    """跑完整自检。

    Args:
      restore: 结束后是否还原剪贴板原内容。

    Returns:
      退出码。
    """
    print("=" * 72)
    print("PastePing 真机自检")
    print("=" * 72)
    print(f"诊断日志：{diagnostics.log_path()}")
    print(f"Python  ：{sys.version.split()[0]}")

    original = _safe_clipboard_read() if restore else None

    config.set_clean_enabled(True)
    config.clear_history()
    cleaner.clear_undo()

    events: list[str] = []
    notifier = Notifier(
        on_flash_start=lambda: events.append("flash-start"),
        on_flash_end=lambda: events.append("flash-end"),
        on_notify=lambda message, title: events.append(f"notify:{title}"),
    )

    def handle_text(text: str) -> None:
        """与 main.handle_text 等价的接线（这里刻意重复一份以便独立运行）。

        Args:
          text: 剪贴板文本。
        """
        if not config.is_detection_active():
            return
        hit = detector.detect(text)
        if hit is None:
            return
        if not config.should_emit(text):
            return
        if config.get_clean_enabled():
            result = cleaner.clean(text)
            if not result.skipped and result.cleaned_text != text:
                if listener.set_text(result.cleaned_text):
                    labels = [part for part in result.summary_label.split("、") if part]
                    notifier.notify_cleaned(labels)
                    return
        notifier.notify(hit)

    listener = ClipboardListener(on_text=handle_text)
    listener.start()
    if not _wait_subscribed(listener):
        print("\n!! 剪贴板事件订阅失败，后续用例无法进行。")
        listener.stop()
        return 1
    print("剪贴板事件订阅：成功")

    print("-" * 72)
    passed = 0
    for name, source, check, should_alert in _CASES:
        events.clear()
        clipboard_io.write_text(source)
        time.sleep(_SETTLE_SECONDS)
        out = _safe_clipboard_read()
        alerted = any(e.startswith("notify:") for e in events)
        ok_content = out is not None and check(out)
        ok_alert = alerted == should_alert
        ok = ok_content and ok_alert
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print(f"       剪贴板实际：{out!r}")
            print(f"       期望触发提醒={should_alert} 实际={alerted} 事件={events}")
        time.sleep(0.25)

    # 撤销能力
    print("-" * 72)
    undo_ok = cleaner.has_undo()
    if undo_ok:
        restored = cleaner.undo()
        undo_ok = restored is not None and "320311197803154517" in restored.cleaned_text
    print(f"[{'PASS' if undo_ok else 'FAIL'}] 撤销快照：可回滚到清洗前原文")

    listener.stop()
    time.sleep(0.3)

    if restore:
        _safe_clipboard_restore(original)
        print("-" * 72)
        print("剪贴板已还原为测试前的内容。" if original is not None else "测试前剪贴板非文本，未做还原。")

    total = len(_CASES) + 1
    got = passed + (1 if undo_ok else 0)
    print("=" * 72)
    print(f"结果：{got}/{total} 通过")
    print()
    print("本脚本刻意不检查的两项（请人工确认）：")
    print("  · 托盘图标是否出现（应为深灰底白色「P」，命中时变红 2 秒）")
    print("  · 系统气泡通知是否弹出（日志里出现 [notify] tray-bubble-ok 即为成功）")
    return 0 if got == total else 1


def main() -> int:
    """入口。

    Returns:
      退出码。
    """
    parser = argparse.ArgumentParser(
        description="PastePing 真机自检：在真实 Windows 上跑通剪贴板全链路。"
    )
    parser.add_argument(
        "--quick", action="store_true", help="只检查启动与剪贴板事件订阅"
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="测试结束后不还原剪贴板（默认会还原成测试前的内容）",
    )
    args = parser.parse_args()
    if args.quick:
        return run_quick()
    return run_full(restore=not args.no_restore)


if __name__ == "__main__":
    sys.exit(main())

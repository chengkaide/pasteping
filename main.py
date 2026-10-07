"""PastePing 入口。

用闭包把 配置（config）→ 检测（detector）→ 清洗（cleaner）→ 提示（notifier）→
托盘（tray）串联起来：

* 主线程运行托盘事件循环（pystray 要求）；
* 剪贴板监听运行在独立后台守护线程中，命中时回调 ``handle_text`` 闭包；
* 命中且「自动清洗已开」时，先清洗并写回剪贴板，再发「已自动清洗」提示；
  否则走原路径（仅提醒）。

⚠️ 2026-10-07 起本项目**全部功能免费**：这里**没有任何授权判断** ——
原先清洗分支写作 ``config.get_unlocked() and config.get_clean_enabled()``，
开头那 11 个字符就是整个付费墙，现在只剩用户自己的开关。整套授权模块
（``licensing`` / ``license_codec`` / ``ui_dialog``）已随之下线删除。

不做 argparse（不提供命令行配置面板）。**全部状态都在内存中**，
进程不读也不写任何配置文件。
"""

from __future__ import annotations

import sys
import time

# 依赖缺失时的中文提示关键词。
_DEPENDENCY_HINTS = (
    "win32",
    "pywin32",
    "pystray",
    "PIL",
    "Pillow",
    "win32clipboard",
    "docx",
    "openpyxl",
)


def _report_missing_dependency(error: ImportError) -> int:
    """打印友好的依赖缺失提示。

    Args:
      error: 捕获到的 ImportError。

    Returns:
      进程退出码 1。
    """
    message = str(error)
    print("PastePing 启动失败：缺少运行所需的第三方依赖。")
    if any(token in message for token in _DEPENDENCY_HINTS):
        print("请先执行：pip install -r requirements.txt")
    else:
        print(
            "请确认已安装 pywin32、pystray、Pillow、python-docx、openpyxl，"
            "可尝试：pip install -r requirements.txt"
        )
    print(f"（原始错误：{message}）")
    return 1


def main() -> int:
    """启动 PastePing。

    Returns:
      进程退出码。
    """
    try:
        import cleaner
        import config
        import detector
        import diagnostics
        from clipboard_listener import ClipboardListener
        from notifier import Notifier
        from tray import VERSION, TrayApp, pending_placeholders
    except ImportError as error:
        return _report_missing_dependency(error)

    diagnostics.install_excepthook()
    diagnostics.log(
        "start",
        "version={} python={}".format(VERSION, sys.version.split()[0]),
    )
    print(diagnostics.location_hint())

    # 面向用户的占位常量提醒（只打印到控制台，不弹窗、不阻断启动）。
    pending = pending_placeholders()
    if pending:
        print("⚠️ 以下面向用户的常量仍是占位值，发布前请替换：")
        for item in pending:
            print(f"   - {item}")
        print()

    # 托盘先就位（仅依赖 config），随后注入 notifier / cleaner 与监听器。
    tray = TrayApp()
    notifier = Notifier(
        on_flash_start=tray.flash_alert,
        on_flash_end=tray.flash_idle,
        on_notify=tray.show_notification,
    )
    tray.attach_notifier(notifier)
    tray.attach_cleaner(cleaner)

    def handle_text(text: str) -> None:
        """处理一次剪贴板文本（闭包：串联 config / detector / cleaner / notifier）。

        Args:
          text: 剪贴板文本。
        """
        if not config.is_detection_active():
            diagnostics.log("detect", "skipped-inactive")
            return
        hit = detector.detect(text)
        if hit is None:
            diagnostics.log("detect", "no-hit")
            return
        if not config.should_emit(text):
            diagnostics.log("detect", f"cooldown-skip rule={hit.rule}")
            return

        # 清洗分支：只要用户开了「自动清洗粘贴」就生效。
        # 这里**没有任何授权判断** —— 全部功能免费，不存在付费墙（有守卫测试钉住）。
        if config.get_clean_enabled():
            try:
                result = cleaner.clean(text)
            except Exception:
                result = None
            if result is not None and not result.skipped and result.cleaned_text != text:
                if listener.set_text(result.cleaned_text):
                    labels = [part for part in result.summary_label.split("、") if part]
                    diagnostics.log("clean", f"applied labels={'/'.join(labels)}")
                    notifier.notify_cleaned(labels, can_undo=True)
                    tray.rebuild_menu()
                    return

        # v0.1 原路径：仅提醒。
        diagnostics.log("detect", f"alert rule={hit.rule}")
        notifier.notify(hit)

    listener = ClipboardListener(on_text=handle_text)
    tray.attach_listener(listener)
    listener.start()

    # 启动自检：订阅失败意味着「以后复制什么都不会提醒」，必须让用户当场知道。
    deadline = time.time() + 1.5
    while time.time() < deadline and not listener.is_subscribed():
        time.sleep(0.05)
    if listener.is_subscribed():
        diagnostics.log("startup", "clipboard-subscription-ok")
    else:
        diagnostics.log("startup", "clipboard-subscription-failed")
        tray.warn_listen_failed()

    try:
        tray.icon.run()
    except Exception as error:
        diagnostics.log("crash", f"{type(error).__name__}: {error}")
        return 1
    finally:
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

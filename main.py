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

⚠️ 2026-10-07 起**只允许一个实例**：真实入口 ``run_cli()`` 会先用一个命名互斥量
抢锁，抢不到就提示用户后退出（见 :func:`_acquire_single_instance`）。
在此之前，双击两次图标会起来**两个托盘图标、两个剪贴板监听器**，
它们看起来一模一样却各写各的剪贴板，用户完全分不清哪个是哪个。
锁只加在 ``run_cli()`` 上、**不**加在 :func:`main` 里 —— ``main`` 是纯装配逻辑，
测试需要在同一个进程里反复调用它（见 ``tests/test_single_instance.py`` 的说明）。
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

# 单实例锁的互斥量名。``Local\`` = 当前登录会话即可见，不需要额外权限，
# 正好是我们想要的范围（换用户登录互不干扰，同一用户下只允许一个）。
# 带 ``.v1`` 便于日后万一改了锁的语义可以和旧版并存。
_SINGLE_INSTANCE_MUTEX = "Local\\PastePing.SingleInstance.v1"

# Win32 错误码：对象已存在（这里指互斥量已被别的实例创建）。
_ERROR_ALREADY_EXISTS = 183


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


def _acquire_single_instance(name: str = _SINGLE_INSTANCE_MUTEX):
    """尝试成为本机当前登录会话里唯一的 PastePing 实例。

    用**命名互斥量**而不是「扫窗口标题」或「写 pid 文件」，有三个理由：
      * 进程无论怎么死（崩溃 / 任务管理器结束 / 断电），内核都会自动回收，
        不会留下「锁文件还在但进程已经没了」这种需要人工清理的残留；
      * 本项目承诺**不读也不写任何配置文件**，pid 文件会破坏这条；
      * 「扫窗口标题」不可靠 —— 标题会被改，而且枚举别的进程窗口需要额外权限。

    ``bInitialOwner`` 传 False：我们**不要求拥有**这个互斥量，
    「锁」体现为「这个进程手里握着互斥量的句柄」。这样就不涉及线程归属
    （``ReleaseMutex`` 必须由创建它的线程调用），也避免了「进程被强杀后
    互斥量处于已放弃状态」这一类需要额外分支处理的语义。

    Args:
      name: 互斥量名，默认 :data:`_SINGLE_INSTANCE_MUTEX`。

    Returns:
      ``(handle, is_first)``。``is_first`` 为 True 表示本进程抢到了锁，
      调用方有义务在退出时把 ``handle`` 交给 :func:`_release_single_instance`。
      为 False 表示**已经有一个实例在跑**，调用方应当提示用户后退出。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 句柄是指针宽的：x64 下不声明 restype 会被截断成 32 位（本项目踩过同类坑，
    # 见 dialogs 里 SetWindowPos / HWND_TOPMOST 的那次修复）。
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    handle = kernel32.CreateMutexW(None, False, name)
    # 必须用 ctypes.get_last_error()（配合 use_last_error=True）：它由 ctypes 在
    # 调用返回的瞬间替我们存好，不会被中间的其他 API 调用冲掉。
    already_exists = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS

    if not handle:
        # 连互斥量都建不出来（极端情况，例如句柄耗尽）：
        # **放行**而不是拒绝 —— 宁可多开一个，也不能让程序起不来。
        return 0, True
    return handle, not already_exists


def _release_single_instance(handle) -> None:
    """释放单实例锁。

    Args:
      handle: :func:`_acquire_single_instance` 返回的句柄；0 表示没有锁可放。
    """
    if not handle:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        kernel32.CloseHandle(handle)
    except Exception:
        # 释放失败不影响正确性：进程退出时内核一样会回收。
        pass


def _already_running_text() -> str:
    """「已经有一个实例在跑」的提示正文。"""
    return (
        "PastePing 已经在运行了，所以这次不再重复启动。\n\n"
        "请到任务栏右下角的通知区域找到它的图标（可能要先点「显示隐藏的图标」），"
        "在图标上右键即可操作。\n\n"
        "（同时运行两个会让托盘里出现两个一模一样的图标，"
        "而且两个都会改写剪贴板，容易搞不清在用哪一个。）"
    )


def _report_already_running() -> None:
    """告知用户「已经有一个实例在跑」。

    用户的动作是**双击图标**：如果什么都不显示，看起来就和「没启动成功」一样，
    所以这里必须给一次看得见的回应（与「托盘菜单点击必有响应」同一条原则）。
    弹窗失败也要退回控制台提示，绝不静默。
    """
    text = _already_running_text()
    try:
        import os

        import diagnostics

        # 写进同一个日志文件：万一用户说「双击没反应」，这条能立刻区分
        # 「被单实例锁拦下了」和「压根没启动起来」。
        diagnostics.log("startup", f"single-instance-blocked pid={os.getpid()}")
    except Exception:
        pass
    print("PastePing 已经在运行，本次不再重复启动。")
    try:
        import dialogs

        dialogs.message(text, "PastePing 已在运行")
    except Exception:
        # 弹不出窗（例如没有图形会话）时上面的控制台提示已经给了交代。
        pass


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


def run_cli() -> int:
    """真实的进程入口：先抢单实例锁，再启动。

    刻意与 :func:`main` 分开：``main`` 是**纯装配逻辑**，同一个进程里可以反复调用
    （测试依赖这一点，见 ``tests/test_qa_v02_adversarial.py`` 的 ``_run_main_harness``）。
    「只能有一个」是**进程级**约束，属于入口的职责，不该塞进装配函数里 ——
    否则任何「import main 再调 main()」的地方（测试、脚本、将来的自动化）
    都会莫名其妙地被自己的第二次调用挡住。

    Returns:
      进程退出码。抢不到锁时返回 0 —— 用户只是重复点了图标，这不是错误。
    """
    handle, is_first = _acquire_single_instance()
    if not is_first:
        _report_already_running()
        return 0
    try:
        return main()
    finally:
        _release_single_instance(handle)


if __name__ == "__main__":
    sys.exit(run_cli())

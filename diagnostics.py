"""本地诊断日志（排查用；**绝不记录剪贴板原文**）。

为什么需要它：托盘渲染、系统通知、浏览器跳转、对话框这类 GUI 行为在无桌面会话的
环境里无法复现，只能靠「现场留痕」定位。本模块把关键事件追加写入
``%APPDATA%\\PastePing\\pasteping.log``；用户把该文件发回，即可判断断点在哪一步
（事件有没有收到 / 有没有命中规则 / 通知有没有发出去 / 菜单点击有没有进处理函数）。

红线（与 v0.1 产品定义一致）：

* **只记录长度与哈希前 8 位、规则名、决策结果**，不记录任何剪贴板原文、激活码或文件名；
* 纯本地文件追加，不联网、不上传；
* 任何写入失败一律静默 —— 日志绝不能反过来影响主流程。

依赖限制：仅使用 ``os`` / ``sys`` / ``time`` / ``threading`` / ``hashlib`` / ``traceback``
（受 tests 的客户端标准库白名单约束；``traceback`` 为 2026-10-06 新增，用于记录出错行号）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from typing import Optional

_LOCK = threading.Lock()

# 日志落在用户配置目录下（本程序**唯一**写盘的东西，便于排障时直接取回）。
_DIR_NAME = "PastePing"
_FILE_NAME = "pasteping.log"

# 单文件上限：超过后把当前文件改名为 ``.old``（覆盖旧档），避免无限增长。
_MAX_BYTES = 512 * 1024

# 总开关（测试可关掉，避免污染真实日志）。
_enabled = True


def set_enabled(value: bool) -> None:
    """打开 / 关闭日志。

    Args:
      value: True 打开，False 关闭。
    """
    global _enabled
    _enabled = value


def log_dir() -> str:
    """返回日志目录（``%APPDATA%\\PastePing``；无 APPDATA 时退回用户主目录）。

    Returns:
      目录绝对路径。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    if not base:
        base = os.getcwd()
    return os.path.join(base, _DIR_NAME)


def log_path() -> str:
    """返回日志文件绝对路径。

    Returns:
      文件绝对路径。
    """
    return os.path.join(log_dir(), _FILE_NAME)


def describe(text: Optional[str]) -> str:
    """把一段文本压成「不含原文」的指纹描述。

    Args:
      text: 任意文本。

    Returns:
      ``len=… sha=…`` 形式的描述；空文本返回 ``len=0``。
    """
    if not text:
        return "len=0"
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return f"len={len(text)} sha={digest[:8]}"


def _rotate(path: str) -> None:
    """日志超过上限时，把当前文件改名为 ``.old``（覆盖旧档）。

    Args:
      path: 日志文件路径。
    """
    try:
        if os.path.getsize(path) < _MAX_BYTES:
            return
        backup = path + ".old"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(path, backup)
    except Exception:
        return


def log(event: str, detail: str = "") -> None:
    """追加一条诊断记录（失败静默）。

    Args:
      event: 事件名，如 ``start`` / ``clipboard`` / ``detect`` / ``menu``。
      detail: 细节描述。**严禁传入包含剪贴板原文或激活码的字符串**；
        文本类信息请先用 :func:`describe` 压成指纹。
    """
    if not _enabled:
        return
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{event}] {detail}\n"
        with _LOCK:
            path = log_path()
            directory = os.path.dirname(path)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            _rotate(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        return


def log_exception(event: str, error: BaseException) -> None:
    """把一次异常连同**出错行号**写进日志（失败静默）。

    为什么需要行号：``--windowed`` exe 没有控制台，异常信息只有类型和消息时
    往往不足以定位（同一个 ``TypeError`` 可能来自十几个地方）。带上
    ``文件名:行号`` 就能直接落到源码某一行。

    Args:
      event: 事件名，如 ``menu/unlock``。
      error: 捕获到的异常对象。
    """
    try:
        import traceback

        frames = traceback.extract_tb(error.__traceback__)
        where = "?"
        if frames:
            last = frames[-1]
            name = os.path.basename(str(last.filename))
            where = f"{name}:{last.lineno}"
        log(event, f"error {type(error).__name__}: {error} @ {where}")
    except Exception:
        try:
            log(event, f"error {type(error).__name__}")
        except Exception:
            return


def install_excepthook() -> None:
    """把未捕获异常写入日志，同时保留原有的 stderr 行为。

    打包成 ``--windowed`` exe 后没有控制台，异常默认被完全吞掉；装上本钩子后
    日志里能看到异常类型与消息，便于定位「点了没反应」。
    """
    original = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        log("uncaught", f"{getattr(exc_type, '__name__', exc_type)}: {exc_value}")
        try:
            original(exc_type, exc_value, exc_tb)
        except Exception:
            return

    sys.excepthook = _hook

    def _thread_hook(args):
        log(
            "uncaught-thread",
            f"{getattr(args.exc_type, '__name__', args.exc_type)}: {args.exc_value}",
        )

    try:
        threading.excepthook = _thread_hook
    except Exception:
        return


def location_hint() -> str:
    """返回给用户看的日志位置提示。

    Returns:
      形如 ``诊断日志：C:\\...\\pasteping.log`` 的提示串。
    """
    return f"诊断日志：{log_path()}"

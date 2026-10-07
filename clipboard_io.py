"""剪贴板读写原语（Windows / ``win32clipboard``）。

只暴露最底层的读写能力，供 ``clipboard_listener`` 与 ``converter`` 复用：

* :func:`read_text` —— 读取 ``CF_UNICODETEXT`` 文本（带重试）；
* :func:`read_html` —— 读取 ``CF_HTML`` 富文本并切出片段（带重试）；
* :func:`write_text` —— 写回 ``CF_UNICODETEXT`` 文本（带重试）。

所有函数**失败一律静默**：读返回 ``None``、写返回 ``False``，绝不抛异常炸掉消息循环。
"""

from __future__ import annotations

import re
import time
from typing import Optional

import win32clipboard

# CF_HTML 的注册名（取格式号须运行时查询，不可硬编码）。
CF_HTML_NAME: str = "HTML Format"

# 读取 / 写入的重试次数与间隔（沿用 v0.1 重试风格）。
_READ_RETRY = 3
_READ_RETRY_INTERVAL = 0.08
_WRITE_RETRY = 5
_WRITE_RETRY_INTERVAL = 0.05

# CF_HTML 头部偏移字段正则（作用于 ASCII 头部）。
_START_FRAGMENT_RE = re.compile(rb"StartFragment:\s*(\d+)")
_END_FRAGMENT_RE = re.compile(rb"EndFragment:\s*(\d+)")
_START_HTML_RE = re.compile(rb"StartHTML:\s*(\d+)")
_END_HTML_RE = re.compile(rb"EndHTML:\s*(\d+)")

# 头部扫描范围（字节）。
_HEADER_SCAN = 4096


def html_format_id() -> int:
    """获取 ``HTML Format`` 的动态格式号。

    Returns:
      格式号；注册失败时返回 0。
    """
    try:
        return int(win32clipboard.RegisterClipboardFormat(CF_HTML_NAME))
    except Exception:
        return 0


def slice_html_fragment(raw: bytes) -> str:
    """按 CF_HTML 头部偏移切出用户实际选中的 HTML 片段。

    优先使用 ``StartFragment/EndFragment``，缺失时回退 ``StartHTML/EndHTML``。

    Args:
      raw: CF_HTML 原始字节流。

    Returns:
      解码后的 HTML 片段字符串；无法切分时返回整块解码结果。
    """
    header = raw[:_HEADER_SCAN]
    start_match = _START_FRAGMENT_RE.search(header)
    end_match = _END_FRAGMENT_RE.search(header)
    if not (start_match and end_match):
        start_match = _START_HTML_RE.search(header)
        end_match = _END_HTML_RE.search(header)
    if start_match and end_match:
        start = int(start_match.group(1))
        end = int(end_match.group(1))
        if 0 <= start <= end <= len(raw):
            return raw[start:end].decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_text() -> Optional[str]:
    """读取剪贴板中的 Unicode 文本，失败时最多重试 ``_READ_RETRY`` 次。

    Returns:
      剪贴板文本；拿不到文本（如图片/文件）或全部重试失败时返回 None。
    """
    for _ in range(_READ_RETRY):
        opened = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            if win32clipboard.IsClipboardFormatAvailable(
                win32clipboard.CF_UNICODETEXT
            ):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                if isinstance(data, str):
                    return data
            return None
        except Exception:
            pass
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        time.sleep(_READ_RETRY_INTERVAL)
    return None


def read_html() -> Optional[str]:
    """读取剪贴板中的 ``CF_HTML`` 富文本并切出片段，失败时重试。

    Returns:
      HTML 片段字符串；剪贴板无 CF_HTML 或全部重试失败时返回 None。
    """
    fmt = html_format_id()
    if not fmt:
        return None
    for _ in range(_READ_RETRY):
        opened = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            if not win32clipboard.IsClipboardFormatAvailable(fmt):
                return None
            data = win32clipboard.GetClipboardData(fmt)
            if isinstance(data, bytes):
                raw = data
            elif isinstance(data, str):
                raw = data.encode("utf-8", errors="replace")
            else:
                return None
            return slice_html_fragment(raw)
        except Exception:
            pass
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        time.sleep(_READ_RETRY_INTERVAL)
    return None


def write_text(text: str) -> bool:
    """把文本写回剪贴板（``CF_UNICODETEXT``），失败时重试。

    Args:
      text: 待写入文本。

    Returns:
      写入成功返回 True；失败静默返回 False（不改剪贴板、不崩溃）。
    """
    if text is None:
        return False
    for _ in range(_WRITE_RETRY):
        opened = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
            return True
        except Exception:
            pass
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        time.sleep(_WRITE_RETRY_INTERVAL)
    return False

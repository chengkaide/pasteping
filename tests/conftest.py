"""pytest 全局夹具。

* 默认**关闭**诊断日志：测试不应该往用户真实的 ``%APPDATA%\\PastePing\\pasteping.log``
  里写东西。需要验证日志行为的用例（``test_diagnostics.py``）自行把 ``APPDATA``
  指向临时目录并重新打开日志。
* 顺带把项目根加入 ``sys.path``，与既有测试文件顶部的路径处理保持一致。
* 让 pystray 的窗口类名**永不相同**（见 ``_unique_pystray_window_class``）——
  否则「谁先造托盘」会影响「谁能造托盘」，用例之间会互相污染。
"""

import itertools
import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture(autouse=True, scope="session")
def _unique_pystray_window_class():
    """修掉 pystray Win32 后端一个会让**测试互相污染**的坑。

    ``pystray._win32.Icon.__init__`` 会立刻注册一个窗口类，类名是
    ``'%s%dSystemTrayIcon' % (self.name, id(self))`` —— **拿对象内存地址当类名**。
    而 ``id()`` 在对象被回收后会被复用，于是：

    1. 前面某个用例造过 ``TrayApp``（于是注册了 ``PastePing<旧地址>SystemTrayIcon``），
       该对象随后被回收，窗口类却**一直留着**（pystray 只在 ``stop()`` 时注销）；
    2. 后面的用例再造 ``TrayApp``，新对象**正好落在同一个地址**上 ⇒ 类名相同
       ⇒ ``RegisterClassEx`` 失败 ⇒ ``OSError: [WinError 1410] 类已存在``。

    症状很迷惑：单独跑某个文件必过，全量跑时**时好时坏**，且失败的用例跟改动毫无关系
    （实测：多加 4 个纯逻辑用例就能稳定触发，因为堆布局一变，地址复用就落到了托盘上）。

    这里不改 pystray 的语义，只让**类名保证唯一**（详见下面的实现）。
    产品里一个进程只建一个 ``TrayApp``，本来不会撞 —— 这是纯测试卫生问题。

    实现说明：``Icon.name`` 在 pystray 里**只**用于 ``_register_class`` 拼类名
    （窗口是用 ``RegisterClassEx`` 返回的 atom 建的，见 ``_create_window``），
    所以绕过注册时临时改它是安全的。注意 ``name`` 是**只读 property**，
    真正可写的是它背后的 ``self._name``（``pystray/_base.py``）。

    演进记录（这段历史值得留着，否则会重复踩）：
      · 2026-10-06 的初版是「撞名时换后缀**重试一次**」，但它在重试分支里写的是
        ``self.name = ...`` —— 而 ``name`` 无 setter，于是重试只会抛
        ``AttributeError``，被 pytest 链式显示成原先的 ``OSError``。**该补丁从未生效过**，
        全量跑仍有约 9% 偶发，只是让人误以为「降低概率」了。
      · 2026-10-07 改为**每次注册都用唯一后缀**（写 ``self._name``），
        从根本上不再有撞名可能 —— 不依赖重试。
    """
    try:
        import pystray._win32 as win32_backend
    except Exception:  # pragma: no cover - 非 Windows / 未装 pystray 时原样跳过
        yield
        return

    original = win32_backend.Icon._register_class
    counter = itertools.count(1)

    def _register_class_with_unique_name(self):
        """注册窗口类；类名带一个进程内唯一后缀，因此永远不会撞名。"""
        keep = self._name
        self._name = f"{keep}~{next(counter)}"
        try:
            return original(self)
        finally:
            self._name = keep

    win32_backend.Icon._register_class = _register_class_with_unique_name
    try:
        yield
    finally:
        win32_backend.Icon._register_class = original


@pytest.fixture(autouse=True)
def _disable_diagnostics():
    """测试期间默认静音诊断日志。"""
    import diagnostics

    diagnostics.set_enabled(False)
    yield
    diagnostics.set_enabled(False)

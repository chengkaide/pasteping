"""托盘图标（大写 P 字形）与链接打开方式的测试。

图标不是「画出来觉得像」就算通过：这里把字形拆成**竖干 + 上环**两个结构特征分别断言，
否则把图标改成纯色方块、或者把 P 画反，测试都发现不了。
"""

import os
import sys

import pytest
from PIL import Image

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

#: exe 图标（由 tools/make_icon.py 从托盘几何生成，打包时自动刷新）。
_ICON_PATH = os.path.join(_PROJECT_ROOT, "assets", "pasteping.ico")

import tray  # noqa: E402


def _white_points(image):
    """返回字形（白色不透明像素）的坐标列表。"""
    width, height = image.size
    pixels = image.load()
    return [
        (x, y)
        for y in range(height)
        for x in range(width)
        if pixels[x, y][:3] == (255, 255, 255) and pixels[x, y][3] > 0
    ]


class TestGlyph:
    """图标字形的结构特征。"""

    def test_size_and_mode(self):
        image = tray.make_icon_image(tray.IDLE_COLOR)
        assert image.size == (64, 64)
        assert image.mode == "RGBA"

    def test_idle_and_alert_differ(self):
        """两态必须可区分（底色变红是唯一的告警视觉信号）。"""
        idle = tray.make_icon_image(tray.IDLE_COLOR).tobytes()
        alert = tray.make_icon_image(tray.ALERT_COLOR).tobytes()
        assert idle != alert

    def test_glyph_is_drawn(self):
        points = _white_points(tray.make_icon_image(tray.IDLE_COLOR))
        assert 300 < len(points) < 3000, f"字形像素数异常：{len(points)}"

    def test_glyph_has_stem_reaching_bottom(self):
        """竖干必须伸到方块下部 —— 没有它只是个圆圈，认不出是 P。"""
        points = _white_points(tray.make_icon_image(tray.IDLE_COLOR))
        assert any(y >= 50 for _, y in points), "字形未延伸到下部（竖干缺失）"

    def test_glyph_has_bowl_on_the_upper_right(self):
        """上半部右侧必须有环 —— 这是 P 区别于「l」的关键。"""
        points = _white_points(tray.make_icon_image(tray.IDLE_COLOR))
        assert any(x >= 46 and 18 <= y <= 40 for x, y in points), "字形右上没有圆环"

    def test_glyph_is_left_heavy(self):
        """P 左右不对称：竖干在左，故左半白像素必须明显多于右半。"""
        points = _white_points(tray.make_icon_image(tray.IDLE_COLOR))
        left = sum(1 for x, _ in points if x < 32)
        right = sum(1 for x, _ in points if x >= 32)
        assert left > right, f"字形应为左重右轻（左 {left} / 右 {right}）"


class TestPackagingIcon:
    """exe 图标：必须存在、必须多档尺寸、必须与托盘同源、必须被接线进打包脚本。

    为什么值得单独立一组守卫：exe 图标是**最容易悄悄出错**的一环 ——
    漏了 ``--icon`` 时 PyInstaller 会塞自己的默认图标，程序照跑、测试照过，
    只有用户看到「一个陌生图标」时才发现。而改了托盘几何却忘了重生成 ico，
    则会出现「文件图标与托盘图标长得不一样」这种更隐蔽的问题。
    """

    def test_ico_exists_with_multiple_sizes(self):
        """ico 必须含多档尺寸；只有单档时资源管理器会硬缩出锯齿。"""
        assert os.path.isfile(_ICON_PATH), (
            f"缺少 {_ICON_PATH}，请运行：python tools/make_icon.py"
        )
        with Image.open(_ICON_PATH) as image:
            sizes = sorted(image.ico.sizes())
        assert (16, 16) in sizes, f"缺少 16x16 档：{sizes}"
        assert (256, 256) in sizes, f"缺少 256x256 档：{sizes}"
        assert len(sizes) >= 5, f"尺寸档太少：{sizes}"

    def test_ico_is_same_logo_as_tray(self):
        """ico 的 256 档必须与 ``tray.make_icon_image(256)`` 逐像素一致。

        不一致 = ico 是旧的（改了托盘几何却忘了重新生成），或被人换成了另一张图。
        """
        with Image.open(_ICON_PATH) as image:
            image.size = (256, 256)
            from_ico = image.convert("RGBA").tobytes()
        from_tray = tray.make_icon_image(tray.IDLE_COLOR, 256).tobytes()
        assert from_ico == from_tray, (
            "assets/pasteping.ico 与当前托盘几何不一致，请重新运行 python tools/make_icon.py"
        )

    def test_build_script_wires_the_icon_in(self):
        """打包脚本必须真的把 --icon 传给 PyInstaller，并在缺图标时拒绝打包。"""
        source = open(
            os.path.join(_PROJECT_ROOT, "tools", "build_exe.py"), encoding="utf-8"
        ).read()
        assert '"--icon"' in source, "打包命令里没有 --icon"
        assert "pasteping.ico" in source, "打包脚本没有引用 assets/pasteping.ico"
        assert "_ensure_icon" in source, "打包前没有图标存在性检查"


class TestOpenUrl:
    """链接打开方式的三级降级。"""

    def test_falls_back_in_order(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            tray, "_open_via_startfile", lambda url: calls.append("startfile") or False
        )
        monkeypatch.setattr(
            tray, "_open_via_shell_execute", lambda url: calls.append("shell") or False
        )
        monkeypatch.setattr(
            tray, "_open_via_webbrowser", lambda url: calls.append("browser") or True
        )
        assert tray.open_url("https://example.com/") is True
        assert calls == ["startfile", "shell", "browser"]

    def test_stops_at_first_success(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            tray, "_open_via_startfile", lambda url: calls.append("startfile") or True
        )
        monkeypatch.setattr(
            tray, "_open_via_shell_execute", lambda url: calls.append("shell") or True
        )
        assert tray.open_url("https://example.com/") is True
        assert calls == ["startfile"], "首个成功的方式之后不应继续调用"

    def test_exception_does_not_break_the_chain(self, monkeypatch):
        def boom(url):
            raise RuntimeError("no shell available")

        monkeypatch.setattr(tray, "_open_via_startfile", boom)
        monkeypatch.setattr(tray, "_open_via_shell_execute", lambda url: False)
        monkeypatch.setattr(tray, "_open_via_webbrowser", lambda url: True)
        assert tray.open_url("https://example.com/") is True

    def test_all_failures_return_false(self, monkeypatch):
        monkeypatch.setattr(tray, "_open_via_startfile", lambda url: False)
        monkeypatch.setattr(tray, "_open_via_shell_execute", lambda url: False)
        monkeypatch.setattr(tray, "_open_via_webbrowser", lambda url: False)
        assert tray.open_url("https://example.com/") is False

    def test_startfile_absent_on_non_windows(self, monkeypatch):
        """非 Windows 平台（无 os.startfile）应返回 False 而不是抛异常。"""
        monkeypatch.delattr(os, "startfile", raising=False)
        assert tray._open_via_startfile("https://example.com/") is False

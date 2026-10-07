"""从托盘图标的同一套几何生成 exe 用的 .ico（外加预览图）。

为什么要有这个脚本：**exe 图标与托盘图标必须是同一个标志**。
托盘那套几何在 ``tray.make_icon_image``，这里直接复用它 ——「手绘一个 ico」
迟早会和托盘图标长得不一样，用户会以为是两个软件。

用法::

    python tools/make_icon.py              # 生成 assets/pasteping.ico（+ 256 PNG）
    python tools/make_icon.py --preview    # 额外输出各尺寸放大拼图，肉眼核对

产物：

* ``assets/pasteping.ico``      多尺寸（16/24/32/48/64/128/256），供 PyInstaller ``--icon``
* ``assets/pasteping-256.png``  256 单图，预览/文档用
* ``assets/icon-preview.png``   ``--preview`` 时输出：各尺寸放大后拼一行

退出码：0 = 成功。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PIL import Image  # noqa: E402  （必须在 sys.path 调整之后）

import tray  # noqa: E402

_ASSETS_DIR = os.path.join(_PROJECT_ROOT, "assets")

#: 供 build_exe.py 引用，保证「打包用的图标」与「这里生成的图标」是同一份。
ICO_PATH = os.path.join(_ASSETS_DIR, "pasteping.ico")
PNG_PATH = os.path.join(_ASSETS_DIR, "pasteping-256.png")
PREVIEW_PATH = os.path.join(_ASSETS_DIR, "icon-preview.png")

# ICO 内含的尺寸：16/32/48 是资源管理器常用档，256 是「大图标/详细信息」档。
SIZES: List[int] = [16, 24, 32, 48, 64, 128, 256]

_PREVIEW_BG = (245, 245, 245, 255)


def build_ico() -> str:
    """生成多尺寸 .ico 与 256 单图。

    Returns:
      生成的 .ico 路径。
    """
    os.makedirs(_ASSETS_DIR, exist_ok=True)
    base = tray.make_icon_image(tray.IDLE_COLOR, 256)
    base.save(ICO_PATH, format="ICO", sizes=[(size, size) for size in SIZES])
    base.save(PNG_PATH)
    return ICO_PATH


def build_preview() -> str:
    """把各尺寸放大后拼成一行，便于肉眼核对小尺寸是否还看得出「P」。

    放大用 ``NEAREST``：只有保留像素块才能看清 16px 下字形有没有糊掉。

    Returns:
      预览图路径。
    """
    tiles = []
    for size in SIZES:
        tile = tray.make_icon_image(tray.IDLE_COLOR, size)
        factor = max(1, 64 // size)
        tiles.append(tile.resize((size * factor, size * factor), Image.NEAREST))
    gap = 12
    width = sum(tile.width for tile in tiles) + gap * (len(tiles) - 1)
    height = max(tile.height for tile in tiles)
    canvas = Image.new("RGBA", (width, height), _PREVIEW_BG)
    x = 0
    for tile in tiles:
        canvas.paste(tile, (x, height - tile.height), tile)
        x += tile.width + gap
    canvas.save(PREVIEW_PATH)
    return PREVIEW_PATH


def main(argv: List[str] | None = None) -> int:
    """命令行入口。

    Args:
      argv: 参数列表；None 时取 ``sys.argv``。

    Returns:
      进程退出码。
    """
    parser = argparse.ArgumentParser(description="生成 PastePing 的 exe 图标")
    parser.add_argument("--preview", action="store_true", help="额外输出各尺寸放大拼图")
    args = parser.parse_args(argv)

    ico = build_ico()
    print(
        f"✅ 已生成 {os.path.relpath(ico, _PROJECT_ROOT)}"
        f"（{os.path.getsize(ico)} 字节，含 " + "/".join(str(s) for s in SIZES) + "）"
    )
    print(f"✅ 已生成 {os.path.relpath(PNG_PATH, _PROJECT_ROOT)}")

    if args.preview:
        preview = build_preview()
        print(f"✅ 已生成 {os.path.relpath(preview, _PROJECT_ROOT)}（肉眼核对用）")

    print("")
    print("下一步：python tools/build_exe.py    # 打包时会自动带上这个图标")
    return 0


if __name__ == "__main__":
    sys.exit(main())

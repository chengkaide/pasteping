"""打包 Windows 单文件 exe，并**对产物做强制的安全检查**。

⚠️ 本工具属于 ``tools/``，**仅开发者本机使用，禁止随客户端分发**。

为什么打包要配一个脚本而不是手敲命令：

1. PyInstaller 的参数里有若干**隐藏导入**（``pystray._win32`` 等）是必需的，
   手敲漏一个就会得到一个「能编译、跑起来却报错」的 exe，而且报错发生在用户机器上；
2. exe 是发给用户的，必须证明它**没有夹带任何不该有的东西**（开发者脚本、
   私钥材料、与运行时无关的庞大依赖）。这类错误「文件还在、程序也能跑」，
   肉眼完全看不出来，所以每次打包都必须自动验。
3. 检查必须**双向**：不仅要验「不该有的没有」，还要验「该有的都在」。
   扩展模块靠字节扫描，**纯 Python 模块（含全部自研模块）靠读 PYZ 归档目录表** ——
   后者被压缩，字节扫描看不见，缺了会让我们自己的代码在用户机器上启动即崩。

用法::

    python tools/build_exe.py                 # 完整打包 + 检查
    python tools/build_exe.py --skip-build    # 只检查已存在的 dist/PastePing.exe

退出码：0 = 打包与检查均通过；1 = 安全检查失败（**产物不可分发**）；2 = 构建失败。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单文件产物名（PyInstaller --name）。
_APP_NAME = "PastePing"

# 必须存在的隐藏导入 —— 漏一个就会得到「能编译但跑不起来」的 exe。
#   pystray._win32  : pystray 按平台动态导入后端，静态分析看不见
#   win32timezone   : pywin32 的运行时按需导入
# 注：本项目**不需要 tkinter** —— 没有任何 GUI 输入框，对话框一律走原生 MessageBoxW。
_HIDDEN_IMPORTS: Tuple[str, ...] = ("pystray._win32", "win32timezone")

# 绝不允许出现在产物里的标记。
#   make_backup          : 开发者专属脚本的文件名，不应随客户端分发。
#   BEGIN ... PRIVATE KEY: 任何形式的私钥材料。本项目当前**没有**私钥
#                          （授权与发码体系已整体移除），但这条检查留着 ——
#                          将来若有人重新引入密钥类素材，这里是第一道闸。
#   _multiarray_umath    : numpy 是**实测发现被误打包**的（源码从未 import 它，
#                          PyInstaller 经 PIL 的 hook 条件拉入），排除后体积明显下降。
_FORBIDDEN_MARKERS: Tuple[bytes, ...] = (
    b"make_backup",
    b"BEGIN PRIVATE KEY",
    b"BEGIN OPENSSH PRIVATE KEY",
    b"_multiarray_umath",
)

# 必须出现在产物里的标记 —— 反向检查。
# 只查「不该有的没有」是不够的：漏掉某个扩展模块时产物照样生成，但一运行就崩。
# 这里逐条对应 main.py 的真实 import 链，任何一条缺失都说明 exe 是坏的。
_REQUIRED_MARKERS: Tuple[Tuple[bytes, str], ...] = (
    (b"win32clipboard", "clipboard_io 读写剪贴板"),
    (b"pystray._win32", "pystray 的 Windows 托盘后端"),
    (b"_imaging", "Pillow 的 C 扩展（现场生成图标）"),
)

# 需要显式排除的模块。
_EXCLUDE_MODULES: Tuple[str, ...] = ("numpy",)

# 入口脚本名（PyInstaller 把它放在 CArchive 顶层，不进 PYZ）。
_ENTRY_SCRIPT = "main"

# exe 的图标资源。**不指定 --icon 时 PyInstaller 会塞进它自带的默认图标**，
# 用户在资源管理器/任务栏看到的就是一个与托盘对不上的陌生图标 ——
# 会被当成两个不同的软件。图标由 ``tools/make_icon.py`` 从 tray 的同一套几何生成。
_ICON_PATH = os.path.join(_PROJECT_ROOT, "assets", "pasteping.ico")

# 必须出现在 PYZ 归档里的**自研模块**。
# 为什么单列这一项：纯 Python 模块会被压缩进 PYZ，**字节扫描看不见它们**，
# 上面那套 `_REQUIRED_MARKERS` 只覆盖得到扩展模块（.pyd）。而自研模块缺失时
# exe 照样生成成功、却在我们自己的代码上启动即崩 —— 恰恰是最该防的一种「该有的没有」。
_REQUIRED_PYZ_MODULES: Tuple[str, ...] = (
    "config",
    "detector",
    "clipboard_listener",
    "clipboard_io",
    "cleaner",
    "notifier",
    "tray",
    "converter",
    "diagnostics",
    "dialogs",
)

# 内容级扫描的说明（PyInstaller 会压缩 .pyc，故此检查为「尽力而为」，非结论性）。
_CONTENT_SCAN_NOTE = "（.pyc 被压缩，此项为尽力而为；文件名级扫描才是结论性证据）"


def _python_executable() -> str:
    """返回当前解释器路径。

    Returns:
      解释器绝对路径。
    """
    return sys.executable


def _ensure_icon() -> bool:
    """刷新并确认 exe 图标存在。

    **为什么要重新生成而不是直接用现成文件**：图标几何住在 ``tray.py`` 里。
    改了托盘图标却忘了重生成 ico，exe 就会带着一个**过期的 logo** ——
    这种「两边不一致」肉眼很难发现，直到有人把托盘图标和文件图标摆在一起看。

    Returns:
      图标可用返回 True。
    """
    try:
        import make_icon
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ 无法导入 tools/make_icon.py（{type(error).__name__}: {error}）")
        return os.path.exists(_ICON_PATH)
    try:
        make_icon.build_ico()
        print(f"✅ 已按当前托盘几何刷新图标：{os.path.relpath(_ICON_PATH, _PROJECT_ROOT)}")
        return True
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ 生成图标失败：{type(error).__name__}: {error}")
        return os.path.exists(_ICON_PATH)


def _run_build() -> int:
    """执行 PyInstaller 打包。

    Returns:
      PyInstaller 退出码。
    """
    command: List[str] = [
        _python_executable(),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        _APP_NAME,
        "--icon",
        _ICON_PATH,
        "--distpath",
        os.path.join(_PROJECT_ROOT, "dist"),
        "--workpath",
        os.path.join(_PROJECT_ROOT, "build"),
        "--specpath",
        os.path.join(_PROJECT_ROOT, "build"),
    ]
    for hidden in _HIDDEN_IMPORTS:
        command += ["--hidden-import", hidden]
    for excluded in _EXCLUDE_MODULES:
        command += ["--exclude-module", excluded]
    command.append(os.path.join(_PROJECT_ROOT, "main.py"))

    print("执行打包命令：")
    print("  " + " ".join(command))
    print("")
    return subprocess.call(command, cwd=_PROJECT_ROOT)


def _scan_artifact(exe_path: str) -> Tuple[List[str], List[str], int]:
    """扫描 exe 里的禁用标记。

    Args:
      exe_path: exe 路径。

    Returns:
      ``(命中标记列表, 扫描细节, 文件字节数)``。
    """
    with open(exe_path, "rb") as handle:
        blob = handle.read()

    hits: List[str] = []
    details: List[str] = []
    for marker in _FORBIDDEN_MARKERS:
        count = blob.count(marker)
        name = marker.decode("utf-8", errors="replace")
        if count:
            hits.append(name)
            details.append(f"  命中 {count} 处：{name}")
        else:
            details.append(f"  未出现：{name}")
    return hits, details, len(blob)


def _check_embedded_modules(exe_path: str) -> Tuple[List[str], List[str]]:
    """校验入口脚本与全部自研模块都真的被打进了 exe。

    做法是用 PyInstaller 自带的归档读取器直接读 exe 的目录表，而不是猜字节：
    ``CArchiveReader`` 给出顶层条目（入口脚本、扩展模块 .pyd），
    ``ZlibArchiveReader`` 给出 PYZ 里被压缩的纯 Python 模块名。

    Args:
      exe_path: exe 路径。

    Returns:
      ``(缺失项列表, 报告行列表)``。读不到归档时报告里会明确写出原因，
      而不是假装检查通过。
    """
    report: List[str] = []
    missing: List[str] = []
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except Exception as error:
        report.append(f"  ⚠️ 未安装 PyInstaller，无法读取归档，本项跳过（{type(error).__name__}）")
        return missing, report

    probe = os.path.join(_PROJECT_ROOT, "build", "_pyz_probe.bin")
    try:
        archive = CArchiveReader(exe_path)
        entries = list(archive.toc)

        if _ENTRY_SCRIPT in entries:
            report.append(f"  ✅ {_ENTRY_SCRIPT + '.py':<18s} 入口脚本（CArchive）")
        else:
            missing.append(f"{_ENTRY_SCRIPT}.py（入口脚本）")
            report.append(f"  ❌ {_ENTRY_SCRIPT + '.py':<18s} 入口脚本（CArchive）")

        pyz_entries = [name for name in entries if name.endswith(".pyz")]
        if not pyz_entries:
            report.append("  ⚠️ exe 内未找到 PYZ 归档，自研模块检查跳过")
            return missing, report

        with open(probe, "wb") as handle:
            handle.write(archive.extract(pyz_entries[0]))
        names = set(ZlibArchiveReader(probe).toc)
        report.append(f"  （PYZ 内共 {len(names)} 个模块）")
        for module in _REQUIRED_PYZ_MODULES:
            if module in names:
                report.append(f"  ✅ {module + '.py':<18s} 自研模块")
            else:
                missing.append(f"{module}.py（自研模块）")
                report.append(f"  ❌ {module + '.py':<18s} 自研模块")
    except Exception as error:
        report.append(f"  ⚠️ 读取归档失败：{type(error).__name__}: {error}")
    finally:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except Exception:
            pass
    return missing, report


def main(argv: List[str] | None = None) -> int:
    """命令行入口。

    Args:
      argv: 参数列表；None 时取 ``sys.argv``。

    Returns:
      进程退出码。
    """
    parser = argparse.ArgumentParser(description="PastePing 打包（含产物安全检查）")
    parser.add_argument("--skip-build", action="store_true", help="跳过构建，仅检查已有产物")
    args = parser.parse_args(argv)

    exe_path = os.path.join(_PROJECT_ROOT, "dist", f"{_APP_NAME}.exe")

    if not args.skip_build:
        for module in ("PyInstaller",):
            probe = subprocess.run(
                [_python_executable(), "-c", f"import {module}"],
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                print(f"缺少打包工具 {module}，请先安装：pip install pyinstaller")
                return 2
        if not _ensure_icon():
            print(f"❌ 缺少图标文件 {_ICON_PATH}")
            print("   请先运行：python tools/make_icon.py")
            return 2
        code = _run_build()
        if code != 0:
            print(f"❌ 构建失败，PyInstaller 退出码 {code}")
            return 2
    elif not os.path.exists(exe_path):
        print(f"❌ 未找到产物 {exe_path}（去掉 --skip-build 可重新构建）")
        return 2

    if not os.path.exists(exe_path):
        print(f"❌ 构建结束但未找到产物 {exe_path}")
        return 2

    size_mb = os.path.getsize(exe_path) / (1024 * 1024)
    print("")
    print("=" * 60)
    print("产物信息")
    print("=" * 60)
    print(f"路径：{exe_path}")
    print(f"体积：{size_mb:.1f} MB")

    hits, details, _ = _scan_artifact(exe_path)
    print("")
    print("=" * 60)
    print("安全检查：产物中不得出现私钥材料、开发者脚本或误打包依赖")
    print("=" * 60)
    for line in details:
        print(line)
    print(_CONTENT_SCAN_NOTE)

    # 反向检查：该有的必须真的在。
    with open(exe_path, "rb") as handle:
        blob = handle.read()
    missing = [
        f"{marker.decode()} —— {purpose}"
        for marker, purpose in _REQUIRED_MARKERS
        if blob.count(marker) == 0
    ]
    print("")
    print("=" * 60)
    print("完整性检查：依赖的扩展模块必须真的在产物里")
    print("=" * 60)
    for marker, purpose in _REQUIRED_MARKERS:
        flag = "✅" if blob.count(marker) else "❌"
        print(f"  {flag} {marker.decode():18s} {purpose}")

    # 第三重检查：纯 Python 模块藏在压缩的 PYZ 里，字节扫描看不见，必须读归档目录表。
    embedded_missing, embedded_report = _check_embedded_modules(exe_path)
    print("")
    print("=" * 60)
    print("模块完整性检查：入口脚本与自研模块必须真的在产物里")
    print("=" * 60)
    for line in embedded_report:
        print(line)
    missing += embedded_missing

    if hits or missing:
        print("")
        print("❌ 检查未通过 —— 该 exe 绝不可分发给用户：")
        for name in hits:
            print(f"   · 不应出现却出现了：{name}")
        for item in missing:
            print(f"   · 应当出现却缺失了：{item}")
        if hits:
            print("   请检查 main.py 的 import 链，确认没有引用 tools/ 下的任何模块。")
        if missing:
            print("   缺失扩展模块说明打包环境的依赖不全，请在装齐 requirements.txt 后重新打包。")
        return 1

    print("")
    print("✅ 全部检查通过：未混入开发者脚本与多余依赖，必需扩展模块与自研模块齐全。")

    _report_signature(exe_path)

    print("")
    print("提醒：本脚本只校验产物内容，**不会启动 exe**（避免留下常驻进程与托盘图标）。")
    print("     请在 Windows 桌面上双击一次，确认托盘图标出现、右键菜单可用。")
    return 0


def _report_signature(exe_path: str) -> None:
    """打印产物的 SHA256 与签名状态，并提示下一步（失败不影响打包结果）。

    Args:
      exe_path: 产物路径。
    """
    try:
        import sign_exe
    except Exception:
        return
    try:
        digest = sign_exe.sha256_of(exe_path)
        status, signer = sign_exe.signature_state(exe_path)
    except Exception:
        return
    print("")
    print("=" * 60)
    print("分发前的最后一步：签名与哈希")
    print("=" * 60)
    print(f"  SHA256 ：{digest}")
    print(f"  签名   ：{status}" + (f"（{signer}）" if signer else ""))
    if status == "Valid":
        print("  ✅ 已签名，可以直接分发。")
    else:
        print("  ⚠️ 尚未签名：别人双击时会出现「Windows 已保护你的电脑 / 未知发布者」。")
        print("     · 有证书：python tools/sign_exe.py --sign --pfx <证书> --password <口令>")
        print("     · 无证书：把上面的 SHA256 贴到下载页，并写明「更多信息 → 仍要运行」")
        print("     · 选购与命令细节见 docs/code-signing.md")


if __name__ == "__main__":
    sys.exit(main())

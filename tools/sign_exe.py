"""给 ``dist/PastePing.exe`` 做 Authenticode 代码签名（含哈希与签名状态核对）。

为什么需要：未签名的 exe 在别人机器上会被 SmartScreen 拦成
「Windows 已保护你的电脑 / 未知发布者」，用户必须点「更多信息 → 仍要运行」才能启动，
实测这会劝退相当一部分下载者。

用法
----
    python tools/sign_exe.py --hash                              # 看 SHA256 与当前签名状态
    python tools/sign_exe.py --sign --pfx cert.pfx --password 口令
    python tools/sign_exe.py --sign --thumbprint <证书指纹>       # 证书已装在系统里时
    python tools/sign_exe.py --sign --pfx cert.pfx --password 口令 --dry-run

没有证书时本脚本**不会假装成功**，而是打印：当前状态、该买哪种证书、
以及拿到证书后要跑的确切命令。详见 ``docs/code-signing.md``。

注意：**每次重新打包都必须重新签名** —— PyInstaller 生成的是全新文件，
旧签名不会跟着走。签名时务必带时间戳，否则证书一过期签名即失效。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_EXE = os.path.join(_PROJECT_ROOT, "dist", "PastePing.exe")

# 时间戳服务器：带时间戳的签名在证书过期后依然有效。
_TIMESTAMP_URL = "http://timestamp.digicert.com"

# signtool 常见安装位置（Windows SDK）。
_SIGNTOOL_GLOBS = (
    r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe",
    r"C:\Program Files (x86)\Windows Kits\10\bin\*\x86\signtool.exe",
    r"C:\Program Files\Windows Kits\10\bin\*\x64\signtool.exe",
)


def _warn(text: str) -> None:
    """打印警示行。"""
    print(text)


def find_signtool() -> Optional[str]:
    """在 PATH 与 Windows SDK 目录里找 ``signtool.exe``。

    Returns:
      可执行文件路径；找不到返回 None。
    """
    found = shutil.which("signtool")
    if found:
        return found
    candidates: List[str] = []
    for pattern in _SIGNTOOL_GLOBS:
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    # 取版本号最大的一份（目录名形如 10.0.22621.0）。
    candidates.sort(reverse=True)
    return candidates[0]


def sha256_of(path: str) -> str:
    """计算文件的 SHA256。

    Args:
      path: 文件路径。

    Returns:
      十六进制小写摘要；文件不存在时返回空串。
    """
    if not os.path.exists(path):
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signature_state(path: str) -> Tuple[str, str]:
    """读取文件的签名状态与签名者。

    用 PowerShell 的 ``Get-AuthenticodeSignature``（系统自带，无需 signtool）。

    Args:
      path: 文件路径。

    Returns:
      ``(状态, 签名者)``；查询失败时返回 ``("Unknown", "")``。
    """
    if not os.path.exists(path):
        return ("Missing", "")
    command = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
        "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return ("Unknown", "")
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    status = lines[0] if lines else "Unknown"
    signer = lines[1] if len(lines) > 1 else ""
    return (status, signer)


def print_report(path: str) -> None:
    """打印产物路径、大小、SHA256 与签名状态。

    Args:
      path: exe 路径。
    """
    print("=" * 60)
    print("产物核对")
    print("=" * 60)
    if not os.path.exists(path):
        print(f"  ❌ 找不到产物：{path}")
        print("     请先运行：python tools/build_exe.py")
        return
    size_mb = os.path.getsize(path) / (1024 * 1024)
    digest = sha256_of(path)
    status, signer = signature_state(path)
    print(f"  路径   ：{path}")
    print(f"  大小   ：{size_mb:.1f} MB")
    print(f"  SHA256 ：{digest}")
    print(f"  签名   ：{status}" + (f"（{signer}）" if signer else ""))
    if status not in ("Valid",):
        print("")
        _warn("  ⚠️ 未签名/签名无效：别人双击时会被 SmartScreen 拦成「未知发布者」，")
        _warn("     需要点「更多信息 → 仍要运行」。分发时请把下面两件事一起给出：")
        _warn("       1. 上面那行 SHA256（让用户能核对文件是否被篡改）")
        _warn("       2. 一句说明：「点『更多信息』再点『仍要运行』」")
    print("")


def _signtool_command(
    exe: str,
    cert_args: List[str],
    digest: str,
    timestamp: str,
) -> List[str]:
    """拼出完整的 signtool 命令。

    Args:
      exe: 目标文件。
      cert_args: 指定证书的参数（pfx 或 thumbprint）。
      digest: 摘要算法。
      timestamp: 时间戳服务器。

    Returns:
      命令行参数列表。
    """
    return [
        "signtool", "sign",
        "/fd", digest,
        "/td", digest,
        "/tr", timestamp,
        *cert_args,
        exe,
    ]


def sign(
    exe: str,
    pfx: str,
    password: str,
    thumbprint: str,
    digest: str,
    timestamp: str,
    dry_run: bool,
) -> int:
    """用 signtool 给 exe 签名，并随后核验。

    Args:
      exe: 目标 exe。
      pfx: pfx 证书路径（可选）。
      password: pfx 口令（可选）。
      thumbprint: 证书指纹（可选，与 pfx 二选一）。
      digest: 摘要算法。
      timestamp: 时间戳服务器。
      dry_run: 只打印命令不执行。

    Returns:
      进程退出码（0 表示成功）。
    """
    if not os.path.exists(exe):
        print(f"❌ 找不到产物：{exe}（请先运行 python tools/build_exe.py）")
        return 1
    if not pfx and not thumbprint:
        print("❌ 需要 --pfx 或 --thumbprint 指定用哪张证书签名。")
        print("   还没有证书？见 docs/code-signing.md 的选购建议。")
        return 1

    cert_args: List[str] = []
    if pfx:
        cert_args += ["/f", pfx]
        if password:
            cert_args += ["/p", password]
    else:
        cert_args += ["/sha1", thumbprint]

    tool = find_signtool()
    command = _signtool_command(exe, cert_args, digest, timestamp)
    if tool:
        command[0] = tool

    print("=" * 60)
    print("签名")
    print("=" * 60)
    print("  命令：" + " ".join(f'"{part}"' if " " in part else part for part in command))
    if dry_run:
        print("  （--dry-run：只打印，不执行）")
        return 0
    if not tool:
        print("")
        print("❌ 找不到 signtool.exe（它随 Windows SDK 提供）。获取方式任选其一：")
        print("   · 安装 Windows SDK：https://developer.microsoft.com/windows/downloads/windows-sdk/")
        print("   · 或装完「Visual Studio 生成工具」后勾选 Windows SDK")
        print("   · winget install Microsoft.WindowsSDK.10.0.22621")
        print("   装好后重跑本脚本即可。")
        return 1

    result = subprocess.run(command)
    if result.returncode != 0:
        print(f"❌ 签名失败（signtool 退出码 {result.returncode}）")
        return result.returncode

    print("")
    print("核验签名：")
    verify = subprocess.run([tool, "verify", "/pa", "/v", exe])
    if verify.returncode != 0:
        print("❌ 签名核验未通过")
        return verify.returncode
    print("")
    print("✅ 签名并核验通过。")
    print_report(exe)
    return 0


def print_no_cert_guide() -> None:
    """没有证书时打印选购与操作指引（不假装成功）。"""
    print("=" * 60)
    print("还没有证书？先看这三条结论（2026-10 核实）")
    print("=" * 60)
    print("  1. 个人开发者在中国内地**用不了** Azure 工件签名（原 Trusted Signing，")
    print("     约每月 9.99 美元）—— 它对个人开放的地区只有美国和加拿大。")
    print("  2. 自签名证书只在本机有效，**对最终用户毫无帮助**，别在这上面花时间。")
    print("  3. 2024 年起 EV 证书不再「首次下载就免警告」，它与 OV 都要累积信誉，")
    print("     所以**没必要为绕过 SmartScreen 专门买贵的 EV**。")
    print("")
    print("  个人开发者的现实选项：")
    print("   · Sectigo 个人代码签名证书（IV）—— 少数明确支持个人申请、无需营业执照；")
    print("   · 或先用未签名版本分发，并在下载页写明「更多信息 → 仍要运行」+ SHA256；")
    print("   · 若项目开源，可申请 SignPath Foundation 的免费开源代码签名。")
    print("")
    print("  详见 docs/code-signing.md（含 signtool 命令、时间戳、常见坑）。")
    print("")


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    Args:
      argv: 参数列表（默认取 ``sys.argv[1:]``）。

    Returns:
      退出码。
    """
    parser = argparse.ArgumentParser(description="给 PastePing.exe 做代码签名 / 核对签名状态")
    parser.add_argument("--exe", default=_DEFAULT_EXE, help="目标 exe（默认 dist/PastePing.exe）")
    parser.add_argument("--hash", action="store_true", help="只打印大小、SHA256 与签名状态")
    parser.add_argument("--sign", action="store_true", help="执行签名")
    parser.add_argument("--pfx", default="", help="pfx/p12 证书路径")
    parser.add_argument("--password", default="", help="pfx 口令")
    parser.add_argument("--thumbprint", default="", help="证书指纹（证书已装进系统时用）")
    parser.add_argument("--digest", default="SHA256", help="摘要算法（默认 SHA256）")
    parser.add_argument("--timestamp", default=_TIMESTAMP_URL, help="时间戳服务器")
    parser.add_argument("--dry-run", action="store_true", help="只打印签名命令，不执行")
    args = parser.parse_args(argv)

    if args.sign:
        code = sign(
            args.exe,
            args.pfx,
            args.password,
            args.thumbprint,
            args.digest,
            args.timestamp,
            args.dry_run,
        )
        if code == 0 and not args.dry_run:
            return 0
        if code != 0:
            return code

    print_report(args.exe)
    if not args.sign and signature_state(args.exe)[0] not in ("Valid",):
        print_no_cert_guide()
    return 0


if __name__ == "__main__":
    sys.exit(main())

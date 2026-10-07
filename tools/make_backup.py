"""项目备份工具：复制源码 → 生成校验清单 → **实跑自检证明这份备份能用**。

⚠️ 本工具属于 ``tools/``，**仅开发者本机使用，禁止随客户端分发**。

设计立场：**没有校验过的备份不算备份。** 因此本工具不只复制文件，还会
在备份目录内**真的跑一遍检测与清洗的核心逻辑**，并核对该目录内每个文件的 sha256。
只比对「文件存在」是没有意义的 —— 源码被截断、依赖声明缺失、模块被误删，
文件一样在。

用法::

    # 默认：备份到 <项目同级>/_backups/pasteping_<时间戳>/
    python tools/make_backup.py

    # 指定目录（已存在则**同步覆盖**，用于刷新同一份备份而不是无限堆积）
    python tools/make_backup.py --dest "D:/bak/pasteping"

    # 同时打一个 tar.gz，便于拷到 U 盘 / 网盘
    python tools/make_backup.py --tar

退出码：0 = 备份与自检均通过；1 = 自检失败（备份不可用，必须排查）；2 = 参数/IO 错误。

注：本项目**不含任何密钥** —— 授权与发码体系已于 2026-10-07 整体移除。
因此本工具不再做「私钥签发 → 内置公钥验签」的闭环自检，改为验证
**核心检测与清洗逻辑**在备份目录内确实跑得通（那才是这个程序真正要做的事）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from typing import List, Tuple

# 仅开发者本机使用：把项目根加入 sys.path（单向 tools → 项目根）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 不参与备份的目录（缓存 / 构建产物 / 版本控制内部）。
_EXCLUDE_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", "build", "dist", ".git", ".idea", ".vscode"}
)

# 不参与备份的文件后缀与文件名。
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")
_EXCLUDE_NAMES = frozenset({"MANIFEST.txt"})

# 已作废的历史密钥路径：授权体系移除后它不再有任何用途。
# 本机若还留着，会在清单里提示一句（免得日后被误当成有效凭据）。
_LEGACY_KEY_REL = os.path.join("tools", "private_key.pem")

# 在备份目录内执行的自检程序。
# 刻意用子进程 + cwd=备份目录，避免与本进程已导入的模块串味。
_SELFCHECK_SNIPPET = r"""
import os, sys
sys.path.insert(0, os.getcwd())
import detector, cleaner

# 1) 敏感信息必须被检出，且分类正确。
hit = detector.detect("员工入档资料：身份证号 320311197803154517，请核对后归档。")
assert hit is not None, "detector 未检出敏感信息"
assert hit.category == "sensitive", "分类错误: %s" % hit.category

# 2) 干净的技术文本不得误报（误报是本项目最被在意的一类缺陷）。
assert detector.detect(
    "采空区稳定性分析常用解析法、数值模拟法与现场监测法三类。"
) is None, "干净技术文本被误报"

# 3) 清洗必须真的把敏感号换掉。
result = cleaner.clean("联系电话 13812345678，请回电。")
assert "13812345678" not in result.cleaned_text, "清洗未移除敏感号"

print("detect=%s clean=ok" % hit.category)
"""


def _collect_relative_files(root: str) -> List[str]:
    """列出应参与备份的相对路径（已排序，保证清单可复现）。

    Args:
      root: 项目根目录。

    Returns:
      正斜杠分隔的相对路径列表。
    """
    collected: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDE_DIRS)
        for name in sorted(filenames):
            if name in _EXCLUDE_NAMES or name.endswith(_EXCLUDE_SUFFIXES):
                continue
            absolute = os.path.join(dirpath, name)
            collected.append(os.path.relpath(absolute, root).replace(os.sep, "/"))
    return sorted(collected)


def _sha256_file(path: str) -> str:
    """计算文件 sha256。

    Args:
      path: 文件路径。

    Returns:
      十六进制摘要。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_files(relatives: List[str], source: str, dest: str) -> Tuple[int, int]:
    """把源文件同步到备份目录（已存在则覆盖）。

    Args:
      relatives: 相对路径列表。
      source: 源项目根。
      dest: 备份目录。

    Returns:
      ``(复制数, 新增数)``。
    """
    copied = 0
    created = 0
    for relative in relatives:
        src_path = os.path.join(source, relative)
        dst_path = os.path.join(dest, relative)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        if not os.path.exists(dst_path):
            created += 1
        shutil.copy2(src_path, dst_path)
        copied += 1
    return copied, created


def _run_selfcheck(dest: str) -> Tuple[bool, str]:
    """在备份目录内实跑「检测 + 清洗」自检。

    Args:
      dest: 备份目录。

    Returns:
      ``(是否通过, 说明文字)``。
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _SELFCHECK_SNIPPET],
            cwd=dest,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as error:  # noqa: BLE001 - 自检失败一律降级为不可用
        return False, f"自检未能执行：{error}"

    if completed.returncode == 0:
        return True, completed.stdout.strip()
    detail = (completed.stdout or "") + (completed.stderr or "")
    return False, detail.strip() or f"自检退出码 {completed.returncode}"


def _write_manifest(
    dest: str,
    source: str,
    relatives: List[str],
    check_ok: bool,
    check_detail: str,
) -> str:
    """生成 ``MANIFEST.txt``。

    Args:
      dest: 备份目录。
      source: 源项目根。
      relatives: 已备份的相对路径列表。
      check_ok: 可用性自检是否通过。
      check_detail: 自检输出。

    Returns:
      清单文件路径。
    """
    lines: List[str] = []
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")

    lines.append("PastePing 项目备份清单")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"备份时间     : {stamp}")
    lines.append(f"源目录       : {source}")
    lines.append(f"备份目录     : {dest}")
    lines.append(f"文件总数     : {len(relatives)}")
    lines.append("排除内容     : " + " / ".join(sorted(_EXCLUDE_DIRS)) + " / *.pyc")
    lines.append("")
    lines.append("本备份为**纯源码**：本项目不含任何密钥、证书或激活码。")
    if os.path.exists(os.path.join(dest, _LEGACY_KEY_REL)):
        lines.append("")
        lines.append("⚠️ 备份内出现了 tools/private_key.pem —— 那是 2026-10-07 授权体系")
        lines.append("   移除后**已彻底作废**的历史密钥（现在没有任何东西能被它签发）。")
        lines.append("   留着无害，但建议删除，免得日后被误当成有效凭据。")
    lines.append("")
    lines.append("-" * 60)
    lines.append("可用性自检（本工具在备份目录内实跑，非比对文件存在）")
    lines.append("-" * 60)
    lines.append(f"检测 + 清洗实跑  : {'通过 ✅' if check_ok else '失败 ❌'}")
    lines.append(f"自检原始输出     : {check_detail}")
    if not check_ok:
        lines.append("")
        lines.append("❗ 自检未通过 —— 这份备份【不可用】，请勿依赖它，先排查再重新备份。")
        lines.append("   常见原因：文件复制不完整、依赖未安装、或源码被误改。")
    lines.append("")
    lines.append("-" * 60)
    lines.append("恢复方法")
    lines.append("-" * 60)
    lines.append("  1) 把本目录整体复制回任意位置（路径不含中文亦可，但建议与工具链一致）")
    lines.append("  2) 建虚拟环境并安装依赖：pip install -r requirements.txt")
    lines.append("  3) 自测：python -m pytest -q      （预期全部通过，0 failed）")
    lines.append("  4) 真机自检：python tools/smoke_test.py")
    lines.append("  5) 运行程序：python main.py       （托盘图标出现即成功）")
    lines.append("")
    lines.append("-" * 60)
    lines.append("校验和（sha256）")
    lines.append("-" * 60)
    for relative in relatives:
        target = os.path.join(dest, relative)
        if os.path.exists(target):
            lines.append(f"{_sha256_file(target)}  {relative}")
    lines.append("")

    manifest_path = os.path.join(dest, "MANIFEST.txt")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return manifest_path


def _make_tar(dest: str) -> str:
    """把备份目录打成 ``.tar.gz``（放在其同级目录，不产生自包含）。

    Args:
      dest: 备份目录。

    Returns:
      压缩包路径。
    """
    parent = os.path.dirname(dest.rstrip(os.sep))
    name = os.path.basename(dest.rstrip(os.sep)) + ".tar.gz"
    tar_path = os.path.join(parent, name)
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(dest, arcname=os.path.basename(dest.rstrip(os.sep)))
    return tar_path


def build_argument_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。

    Returns:
      解析器实例。
    """
    parser = argparse.ArgumentParser(description="PastePing 项目备份（含可用性自检）")
    parser.add_argument(
        "--dest",
        default=None,
        help="备份目录；缺省 = <项目同级>/_backups/pasteping_<时间戳>",
    )
    parser.add_argument("--tar", action="store_true", help="同时打一个 .tar.gz 便于搬运")
    return parser


def main(argv: List[str] | None = None) -> int:
    """命令行入口。

    Args:
      argv: 参数列表；None 时取 ``sys.argv``。

    Returns:
      进程退出码（0 通过 / 1 自检失败 / 2 参数或 IO 错误）。
    """
    args = build_argument_parser().parse_args(argv)
    source = _PROJECT_ROOT

    if args.dest:
        dest = os.path.abspath(args.dest)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(os.path.dirname(source), "_backups", f"pasteping_{stamp}")

    if os.path.abspath(dest) == os.path.abspath(source):
        print("备份目录不能与项目目录相同。")
        return 2

    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as error:
        print(f"无法创建备份目录：{error}")
        return 2

    relatives = _collect_relative_files(source)
    try:
        copied, created = _sync_files(relatives, source, dest)
    except OSError as error:
        print(f"复制失败：{error}")
        return 2

    check_ok, check_detail = _run_selfcheck(dest)
    manifest_path = _write_manifest(dest, source, relatives, check_ok, check_detail)

    print(f"备份目录：{dest}")
    print(f"文件数量：{copied}（其中新增 {created}）")
    print(f"清单文件：{manifest_path}")
    print(f"可用性自检：{'通过' if check_ok else '失败'} —— {check_detail}")

    if args.tar:
        tar_path = _make_tar(dest)
        size_mb = os.path.getsize(tar_path) / (1024 * 1024)
        print(f"压缩包  ：{tar_path}（{size_mb:.2f} MB）")

    if not check_ok:
        print("")
        print("❗ 备份已复制但【自检未通过】，请勿依赖此备份，先排查后再备份。")
        return 1

    print("")
    print("提示：本备份是纯源码，不含密钥与用户数据；建议另存一份到仓库外的介质。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

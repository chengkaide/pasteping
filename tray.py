"""PastePing 系统托盘图标与右键菜单。

* 图标使用 Pillow 现场生成（不依赖任何外部图片资源）；
* 菜单为**单态**：v0.1 原有 6 项**一个不少**，全部功能（自动清洗、格式转换）
  **始终可用** —— 2026-10-07 起本项目全部功能免费，不存在任何「锁」的形态；
* 「支持开发者」是**纯自愿打赏**，与功能**完全无关**：赞助前后功能一模一样；
* **全部菜单回调统一经 :func:`_guarded` 包装**：任何异常都会被记进诊断日志并
  **弹窗告知用户**，绝不出现「点了没反应」（见 :func:`_guarded` 的说明）。

依赖方向（设计 §9.2）：``tray`` 只 import ``config`` / ``converter`` / ``dialogs`` /
``diagnostics``；``clipboard_listener`` 与 ``cleaner`` 通过 ``attach_*`` 注入（鸭子类型），
``notifier`` 通过 ``attach_notifier`` 注入，**不反向 import**。
"""

from __future__ import annotations

import ctypes
import os
import webbrowser
from typing import Optional, Tuple

import pystray
from PIL import Image, ImageDraw

import config
import converter
import dialogs
import diagnostics

VERSION = "0.2.1"

# 支持开发者链接（纯自愿打赏入口；与功能**完全无关**）。
# 取值 = 爱发电「个人主页」地址，形如：
#   https://afdian.com/a/<你的爱发电用户名>
# 注意：该主页上可能另有平台自带的可选档位，与本项目的 config.SUPPORT_PRICE_CNY 无关。
SUPPORT_URL = "https://afdian.com/a/pasteping"

# 问题反馈渠道：GitHub Issues。
# ⚠️ 2026-10-07 起本项目**不公开任何联系邮箱** —— 缺陷报告、误报样本与功能建议
#    一律走 Issues；安全问题走 SECURITY.md 里的私密 Advisory 通道。
ISSUES_URL = "https://github.com/chengkaide/pasteping/issues"

# 占位符哨兵：值中出现以下任一片段即视为「尚未填写真实值」。
PLACEHOLDER_SENTINELS: Tuple[str, ...] = ("待填", "<", "YOUR_")


def is_placeholder(value: object) -> bool:
    """判断某个面向用户的常量是否仍是占位值。

    Args:
      value: 常量取值。

    Returns:
      含占位哨兵时返回 True。
    """
    text = str(value)
    return any(sentinel in text for sentinel in PLACEHOLDER_SENTINELS)


def pending_placeholders() -> list:
    """列出仍未填写真实值的面向用户常量（供启动时提醒，避免带着占位符发布）。

    Returns:
      ``"名称=当前值"`` 形式的列表；全部填好时为空列表。
    """
    items = (
        ("tray.SUPPORT_URL", SUPPORT_URL),
        ("tray.ISSUES_URL", ISSUES_URL),
        ("config.SUPPORT_PLATFORM_NAME", config.SUPPORT_PLATFORM_NAME),
    )
    return [f"{name} = {value}" for name, value in items if is_placeholder(value)]


# 图标配色。
IDLE_COLOR: Tuple[int, int, int] = (138, 138, 138)  # #8A8A8A 深灰
ALERT_COLOR: Tuple[int, int, int] = (229, 72, 77)  # #E5484D 红

# 图标几何：圆角方块 + 白色大写「P」。
# 「P」用**几何图元拼出**（一竖干 + 一圆环），不依赖任何字体文件 ——
# 这样打包进 exe 后形态与开发机完全一致，不会因为目标机器缺字体而变成方框。
_ICON_SIZE = 64
_ICON_PADDING = 2
_ICON_RADIUS = 16
_GLYPH_COLOR: Tuple[int, int, int] = (255, 255, 255)

# 竖干（P 的左竖）。
_STEM_BOX = (15, 9, 25, 55)
_STEM_RADIUS = 5

# 圆环（P 的上环）。环的左缘与竖干重叠，两形自然合成一个字母；
# 环必须**贴住顶部**——环若落在中部，观感会变成小写「p」（竖干下伸出成降部）。
# 线宽 8 + 路径框 30x30 → 环孔 14x14：在 16px 托盘尺寸下仍能看出是个「P」。
_BOWL_BOX = (21, 9, 51, 39)
_BOWL_WIDTH = 8


TOOLTIP_IDLE = "PastePing · 剪贴板安全提醒"
TOOLTIP_PAUSED = "PastePing · 已暂停"

# MessageBox 相关的 flags 与常量统一放在 ``dialogs`` 模块里维护
# （那里还负责「同一时刻只允许一个对话框」的闸门），这里不再重复定义。

_ABOUT_TITLE = "关于 PastePing"


def _guarded(name: str):
    """把菜单回调包成「必定有响应、绝不静默」的形式。

    为什么必须包：pystray 的 Win32 后端在托盘消息循环里**直接调用**回调函数。
    回调抛出的异常在打包后的 ``--windowed`` exe 里**没有任何人接住**
    （没有控制台、没有 stderr、``sys.excepthook`` 也不会被触发，因为异常被
    消息循环吞掉了），用户的感受就是「点了菜单什么也没发生」——
    这是本项目历史上最难排查的一类故障。

    包装后每个回调都保证：

    1. **进入即记日志**，日志里能看到「点击确实被收到了」；
    2. 正常返回时记 ``done``；
    3. 抛异常时记下异常类型与**出错行号**，并弹窗把日志路径交给用户。

    Args:
      name: 回调的短名（写进日志，便于对照）。

    Returns:
      装饰器。
    """

    def decorate(func):
        def wrapper(self, icon=None, item=None):
            diagnostics.log("menu", f"{name} enter")
            try:
                result = func(self, icon, item)
            except Exception as error:  # noqa: BLE001 - 兜底就是本包装的全部意义
                diagnostics.log_exception(f"menu/{name}", error)
                self._report_action_error(name, error)
                return None
            diagnostics.log("menu", f"{name} done")
            return result

        wrapper.__name__ = getattr(func, "__name__", name)
        wrapper.__doc__ = getattr(func, "__doc__", None)
        return wrapper

    return decorate


def _about_text() -> str:
    """构造「关于」信息框文本（功能说明、免责声明、隐私声明、反馈渠道）。

    Returns:
      关于文本。
    """
    return (
        f"PastePing v{VERSION}\n\n"
        "剪贴板安全提醒：在你把内容复制到别处之前，安静地提醒你一次。\n\n"
        "· 完全本地运行，不联网，不上传任何剪贴板内容\n"
        "· 全部功能永久免费：三类检测 / 提醒 / 60 秒降频 / 暂停 / 总开关 /\n"
        "  自动清洗粘贴 / 格式转换，均无内购、无功能锁、无使用限制\n"
        f"· 问题反馈与功能建议：{ISSUES_URL}\n\n"
        "免责声明：本工具为辅助提醒，检测基于固定规则，可能存在漏检或误检；\n"
        "自动清洗与格式转换不保证 100% 还原原文语义或排版，使用前请自行确认；\n"
        "因使用本工具产生的任何直接或间接损失，开发者不承担责任。\n"
    )


def _render_icon(color: Tuple[int, int, int], size: int, scale: float) -> Image.Image:
    """按给定缩放比例把图标画到 ``size``×``size`` 画布上。

    Args:
      color: 方块底色。
      size: 画布边长（像素）。
      scale: 相对 64 基准的比例（1.0 即原尺寸，4.0 即四倍）。

    Returns:
      ``size``×``size`` 的 RGBA 图像。
    """
    pad = round(_ICON_PADDING * scale)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=max(1, round(_ICON_RADIUS * scale)),
        fill=color,
    )
    draw.rounded_rectangle(
        [round(value * scale) for value in _STEM_BOX],
        radius=max(1, round(_STEM_RADIUS * scale)),
        fill=_GLYPH_COLOR,
    )
    draw.ellipse(
        [round(value * scale) for value in _BOWL_BOX],
        outline=_GLYPH_COLOR,
        width=max(1, round(_BOWL_WIDTH * scale)),
    )
    return image


def make_icon_image(color: Tuple[int, int, int], size: int = _ICON_SIZE) -> Image.Image:
    """生成图标：圆角方块底色 + 白色大写「P」。

    底色承担状态语义（空闲深灰 / 告警红），字形承担身份识别
    （用户一眼能认出「是 PastePing 在跑」）。

    同一套几何既画托盘（64），也**生成 exe 的 .ico**（16–256，见
    ``tools/make_icon.py``）—— 文件图标与托盘图标是同一个标志，
    不会出现「两套 logo」。

    Args:
      color: 方块底色（``IDLE_COLOR`` 或 ``ALERT_COLOR``）。
      size: 输出边长（像素），默认 64（托盘用法）。

    Returns:
      ``size``×``size`` 的 RGBA 图像。
    """
    if size == _ICON_SIZE:
        # 与历史实现逐字等价（scale=1 时 round 不改变任何数值）—— 托盘观感不变。
        return _render_icon(color, size, 1.0)
    if size < _ICON_SIZE:
        # 小尺寸直接矢量绘制会锯齿糊成一团：先按 4 倍绘再降采样。
        supersampled = _render_icon(color, _ICON_SIZE * 4, 4.0)
        return supersampled.resize((size, size), Image.LANCZOS)
    return _render_icon(color, size, size / _ICON_SIZE)


def _open_via_startfile(url: str) -> bool:
    """用 ``os.startfile`` 打开链接（Windows 上最直接的方式）。

    Args:
      url: 目标链接。

    Returns:
      调用成功返回 True；非 Windows 平台返回 False。
    """
    opener = getattr(os, "startfile", None)
    if opener is None:
        return False
    opener(url)
    return True


def _open_via_shell_execute(url: str) -> bool:
    """用 ``ShellExecuteW`` 打开链接（不依赖子进程管道）。

    Args:
      url: 目标链接。

    Returns:
      返回值大于 32 视为成功（Win32 约定）。
    """
    result = ctypes.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1)
    return int(result) > 32


def _open_via_webbrowser(url: str) -> bool:
    """用标准库 ``webbrowser`` 打开链接（最后兜底）。

    Args:
      url: 目标链接。

    Returns:
      浏览器被成功调起返回 True。
    """
    return bool(webbrowser.open(url))


# 打开外部链接的候选方式，按可靠性从高到低（**每次调用时现取**，
# 便于测试替换其中任意一级，也便于将来插入新方式）。
def open_url(url: str) -> bool:
    """在默认浏览器中打开链接，三级降级。

    为什么不能只用 ``webbrowser``：打包成 ``--windowed`` exe 后进程没有控制台，
    ``webbrowser`` 内部走子进程 / 标准句柄的路径可能直接失败且不报错，
    表现为「点了菜单没任何反应」。``os.startfile`` 与 ``ShellExecuteW`` 直接委托
    系统外壳，在这种环境下更可靠。

    Args:
      url: 目标链接。

    Returns:
      任一级成功返回 True；全部失败返回 False（不抛异常）。
    """
    openers = (_open_via_startfile, _open_via_shell_execute, _open_via_webbrowser)
    for opener in openers:
        try:
            if opener(url):
                return True
        except Exception:
            continue
    return False


class TrayApp:
    """托盘图标、右键菜单与告警反馈的宿主。

    Attributes:
      icon: 底层的 ``pystray.Icon`` 实例。
    """

    def __init__(self) -> None:
        """初始化托盘图标与菜单。"""
        self._listener: Optional[object] = None
        self._notifier: Optional[object] = None
        self._cleaner: Optional[object] = None
        self.icon = pystray.Icon(
            "PastePing",
            make_icon_image(IDLE_COLOR),
            TOOLTIP_IDLE,
            menu=self._build_menu(),
        )

    # ------------------------------------------------------------------ #
    # 依赖注入（避免反向 import）
    # ------------------------------------------------------------------ #
    def attach_listener(self, listener: object) -> None:
        """注入剪贴板监听器（需具备 ``stop`` / ``set_text`` / ``read_text`` / ``read_html``）。

        Args:
          listener: 监听器对象（鸭子类型）。
        """
        self._listener = listener

    def attach_notifier(self, notifier: object) -> None:
        """注入轻提示器（需具备 ``notify_message``）。

        Args:
          notifier: 轻提示器对象。
        """
        self._notifier = notifier

    def attach_cleaner(self, cleaner: object) -> None:
        """注入清洗模块（需具备 ``has_undo`` / ``undo``）。

        Args:
          cleaner: 清洗模块对象。
        """
        self._cleaner = cleaner

    # ------------------------------------------------------------------ #
    # 菜单
    # ------------------------------------------------------------------ #
    def _build_menu(self) -> "pystray.Menu":
        """构建右键菜单（**单态**：全部功能始终可用）。

        2026-10-07 起本项目全部功能免费，原先「未解锁 / 已解锁」双态菜单取消：
        自动清洗、格式转换、打开结果文件夹**任何时候都可用**，
        不再有 ``🔒`` 占位或「点击后弹一句『请先解锁』」的入口。

        Returns:
          pystray 菜单对象。
        """
        clean_item = pystray.MenuItem(
            "自动清洗粘贴",
            self._on_toggle_clean,
            checked=lambda item: config.get_clean_enabled(),
            enabled=True,
        )

        convert_items = [
            pystray.MenuItem("Markdown → Word", self._on_convert_markdown),
            pystray.MenuItem("网页表格 → Excel", self._on_convert_html),
            pystray.MenuItem("打开结果文件夹", self._on_open_folder),
        ]
        convert_item = pystray.MenuItem("格式转换", pystray.Menu(*convert_items))

        entries: list = [
            pystray.MenuItem(
                "敏感提醒",
                self._on_toggle,
                checked=lambda item: config.get_enabled(),
            ),
            clean_item,
        ]

        # 「撤销上次清洗」：仅在确有一次清洗可撤销时出现。
        if self._cleaner is not None and self._cleaner.has_undo():
            entries.append(pystray.MenuItem("撤销上次清洗", self._on_undo_clean))

        entries.append(convert_item)
        entries.append(pystray.Menu.SEPARATOR)
        # 「暂停」与「恢复」互斥显示：暂停中给出「恢复」入口。
        # 原来只有暂停、没有恢复，点了「暂停 1 小时」之后在界面里无路可退，只能重启。
        paused_for = config.seconds_until_resume()
        if paused_for > 0:
            minutes = max(1, int(round(paused_for / 60)))
            entries.append(
                pystray.MenuItem(f"恢复提醒（暂停中，还剩约 {minutes} 分钟）", self._on_resume)
            )
        else:
            entries.append(pystray.MenuItem("暂停 5 分钟", self._on_pause_5))
            entries.append(pystray.MenuItem("暂停 1 小时", self._on_pause_60))
        entries.append(pystray.Menu.SEPARATOR)

        entries.append(
            pystray.MenuItem(
                f"支持开发者 ¥{config.SUPPORT_PRICE_CNY}（纯自愿，不影响任何功能）",
                self._on_support,
            )
        )
        entries.append(pystray.MenuItem("关于", self._on_about))
        entries.append(pystray.Menu.SEPARATOR)
        entries.append(pystray.MenuItem("退出", self._on_quit))
        return pystray.Menu(*entries)

    def rebuild_menu(self) -> None:
        """按当前状态重建菜单（暂停状态变化 / 清洗后调用），失败静默。"""
        try:
            self.icon.menu = self._build_menu()
        except Exception:
            return
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _refresh_tooltip(self) -> None:
        """根据暂停状态刷新托盘 tooltip。"""
        try:
            self.icon.title = (
                TOOLTIP_PAUSED if config.seconds_until_resume() > 0 else TOOLTIP_IDLE
            )
        except Exception:
            pass

    @staticmethod
    def _message_box(text: str, title: str) -> None:
        """弹出只读信息框（走统一闸门，同一时刻只有一个对话框）。

        Args:
          text: 正文。
          title: 标题。
        """
        dialogs.message(text, title)

    @staticmethod
    def _ask_yes_no(text: str, title: str) -> bool:
        """弹出「是/否」对话框（走统一闸门）。

        Args:
          text: 正文。
          title: 标题。

        Returns:
          用户点「是」返回 True，其余情况返回 False。
        """
        return dialogs.ask_yes_no(text, title)

    @staticmethod
    def _dialog_busy(name: str) -> bool:
        """菜单回调入口的统一检查：已有对话框时不再叠新窗口。

        Args:
          name: 回调短名（写日志用）。

        Returns:
          True 表示当前有对话框在显示、调用方应立刻返回。
        """
        return dialogs.guard(name)

    def _report_action_error(self, name: str, error: BaseException) -> None:
        """把一次失败的菜单操作**当场告诉用户**（而不是让它静默消失）。

        Args:
          name: 回调短名。
          error: 捕获到的异常。
        """
        self._message_box(
            f"这个操作没能完成：{name}\n\n"
            f"{type(error).__name__}: {error}\n\n"
            "问题不在你的操作。请把下面这个日志文件发回，即可定位：\n"
            f"{diagnostics.log_path()}",
            "PastePing · 操作失败",
        )

    # ------------------------------------------------------------------ #
    # 免费功能（与 v0.1 完全一致）
    # ------------------------------------------------------------------ #
    @_guarded("toggle-detect")
    def _on_toggle(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """切换敏感提醒总开关（并给出可见反馈）。"""
        enabled = config.toggle_enabled()
        self._refresh_tooltip()
        self.rebuild_menu()
        self._notify(
            "PastePing · 敏感提醒",
            "已开启，命中规则时会提醒你" if enabled else "已关闭，不再提醒任何内容",
        )

    @_guarded("pause-5")
    def _on_pause_5(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """暂停提醒 5 分钟。"""
        config.pause(300)
        self._refresh_tooltip()
        self.rebuild_menu()
        self._notify("PastePing · 已暂停", "5 分钟内不再提醒（可在托盘菜单恢复）")

    @_guarded("pause-60")
    def _on_pause_60(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """暂停提醒 1 小时。"""
        config.pause(3600)
        self._refresh_tooltip()
        self.rebuild_menu()
        self._notify("PastePing · 已暂停", "1 小时内不再提醒（可在托盘菜单恢复）")

    @_guarded("resume")
    def _on_resume(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """立即结束暂停、恢复提醒。

        补这个入口的原因：原来菜单里只有「暂停」没有「恢复」，
        点了「暂停 1 小时」之后**在界面里没有任何办法取消**，只能重启程序。
        """
        config.resume()
        self._refresh_tooltip()
        self.rebuild_menu()
        self._notify("PastePing · 已恢复", "提醒已恢复，命中规则时会提醒你")

    @_guarded("support")
    def _on_support(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """打开开发者支持链接（纯自愿打赏，与功能**完全无关**）。"""
        opened = open_url(SUPPORT_URL)
        diagnostics.log("open_url", f"support ok={opened}")
        if not opened:
            self._message_box(
                "无法自动打开浏览器。请手动复制下面的地址到浏览器打开：\n\n" + SUPPORT_URL,
                "PastePing · 支持开发者",
            )

    @_guarded("about")
    def _on_about(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """弹出一个只读信息框（不是设置面板，无任何可交互配置项）。

        已有对话框在显示时不再叠新窗口 —— 否则「关于」连点两次会叠两个一模一样的
        框，点一次「确定」只关掉上面那个，用户看到的就是「点了确定窗口却不消失」。
        """
        if self._dialog_busy("about"):
            return
        self._message_box(_about_text(), _ABOUT_TITLE)

    @_guarded("quit")
    def _on_quit(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """退出：先停止监听线程，再停止托盘。"""
        self.stop()

    # ------------------------------------------------------------------ #
    # 增强功能（自动清洗 / 格式转换）—— 全部免费，无任何门槛
    # ------------------------------------------------------------------ #
    @_guarded("toggle-clean")
    def _on_toggle_clean(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """切换自动清洗开关（始终可见，与任何授权状态无关）。"""
        enabled = config.toggle_clean_enabled()
        self.rebuild_menu()
        self._notify(
            "PastePing · 自动清洗",
            "已开启，命中后会先清洗再写回剪贴板" if enabled else "已关闭，命中后只提醒、不改内容",
        )

    @_guarded("undo-clean")
    def _on_undo_clean(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """撤销最近一次自动清洗：把原文写回剪贴板。"""
        if self._cleaner is None:
            self._notify("PastePing · 撤销清洗", "当前没有可撤销的清洗记录")
            return
        try:
            result = self._cleaner.undo()
        except Exception:
            result = None
        if result is None:
            self._notify("PastePing · 撤销清洗", "当前没有可撤销的清洗记录")
            return
        if self._listener is not None:
            try:
                self._listener.set_text(result.cleaned_text)
            except Exception:
                pass
        self._notify("PastePing · 已撤销清洗", "已恢复原始内容")
        self.rebuild_menu()

    @_guarded("convert-markdown")
    def _on_convert_markdown(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """把剪贴板 Markdown 转换为 Word。"""
        text = None
        if self._listener is not None:
            try:
                text = self._listener.read_text()
            except Exception:
                text = None
        result = converter.markdown_to_docx(text)
        diagnostics.log(
            "convert", f"markdown {'ok' if result.ok else 'fail'} {diagnostics.describe(text)}"
        )
        self._notify("PastePing · 格式转换", result.message)

    @_guarded("convert-html")
    def _on_convert_html(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """把剪贴板网页表格转换为 Excel。"""
        result = converter.clipboard_to_xlsx()
        diagnostics.log("convert", f"html-table {'ok' if result.ok else 'fail'}")
        self._notify("PastePing · 格式转换", result.message)

    @_guarded("open-result-folder")
    def _on_open_folder(self, icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        """打开产物目录。"""
        converter.open_result_folder()

    # ------------------------------------------------------------------ #
    # 图标反馈
    # ------------------------------------------------------------------ #
    def _notify(self, title: str, message: str) -> None:
        """优先经注入的 notifier 发通知，否则直接经托盘图标发通知。

        Args:
          title: 通知标题。
          message: 通知正文。
        """
        if self._notifier is not None:
            try:
                self._notifier.notify_message(title, message)
                return
            except Exception:
                pass
        try:
            self.show_notification(message, title)
        except Exception:
            pass

    def flash_alert(self) -> None:
        """把托盘图标切换为告警（红色）。"""
        try:
            self.icon.icon = make_icon_image(ALERT_COLOR)
        except Exception:
            pass

    def flash_idle(self) -> None:
        """把托盘图标恢复为空闲（灰色）。"""
        try:
            self.icon.icon = make_icon_image(IDLE_COLOR)
        except Exception:
            pass

    def show_notification(self, message: str, title: str) -> None:
        """通过托盘图标发出系统气泡通知。

        Args:
          message: 通知正文。
          title: 通知标题。
        """
        try:
            self.icon.notify(message, title)
            diagnostics.log("notify", "tray-bubble-ok")
        except Exception as error:
            diagnostics.log("notify", f"tray-bubble-failed {type(error).__name__}")
            raise

    def warn_listen_failed(self) -> None:
        """剪贴板监听订阅失败时的可见告警。

        否则用户只会看到「复制了完全没反应」，且无从判断是工具没在跑，还是没命中规则。
        """
        self._message_box(
            "PastePing 未能订阅剪贴板事件，复制时将不会有任何提醒。\n"
            "请退出后重新运行；若仍如此，请把下面的诊断日志发给开发者：\n\n"
            f"{diagnostics.log_path()}",
            "PastePing · 监听启动失败",
        )

    def stop(self) -> None:
        """干净退出：停止监听线程并停止托盘事件循环。"""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        try:
            self.icon.stop()
        except Exception:
            pass

"""PastePing 独立对抗性验证 + 全量回归（QA 严过关）。

本文件是 QA 独立编写、**不修改任何源码**的对抗性测试，覆盖：

* A 全量/回归结构核对（detect 签名、Hit 字段、既有常量逐字未改）
* B 免费红线（无任何授权状态下检测/提醒仍生效、剪贴板默认不动、无死按键）
* D 清洗对抗（中段命中、安全阀 59.9/60.0/60.1%、占位符、撤销、幂等、不误删、极端输入）
* E 剪贴板写回防自触发（无死循环、真实复制不被吞、同内容边界、连续复制、窗口到期）
* F 格式转换（md→docx 读回、CF_HTML 跨格读回、嵌套降级、纯文本降级、零对话框、同秒覆盖）
* G 隐私合规（零网络、剪贴板不落盘、无硬编码价格、赞助入口不改任何状态、无公开邮箱）
* H 结构核对（依赖恰 5 个、菜单单态、依赖方向、fp_benchmark 可运行）
* I 工程师自报假设/偏离裁决依据

历史变更（2026-10-07）：原 **C. 授权模块对抗** 一节整体删除 —— 本项目转为全开源、
全部功能免费，Ed25519 授权模块（``licensing`` / ``license_codec``）与激活码输入框
（``ui_dialog``）已随之下线，不再有「码」可对抗。原「免费版红线」用例保留并加强，
因为它们守的正是「不存在任何付费墙」这个契约本身。

约束：**绝不启动 pystray 的 ``run()`` / Win32 消息循环 / 真实剪贴板写入 / 模态对话框**；
全部走导入、构造、纯逻辑、假对象（monkeypatch）。
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import re
import subprocess
import sys
import types

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import cleaner  # noqa: E402
import clipboard_listener  # noqa: E402
import config  # noqa: E402
import converter  # noqa: E402
import detector  # noqa: E402

# 客户端模块（**不含 tools/**）。
CLIENT_MODULES = (
    "config.py",
    "detector.py",
    "clipboard_listener.py",
    "clipboard_io.py",
    "cleaner.py",
    "notifier.py",
    "tray.py",
    "main.py",
    "converter.py",
    "diagnostics.py",
    "dialogs.py",
)


def _read_source(name: str) -> str:
    """读取项目根下某个文件源码。"""
    with open(os.path.join(_PROJECT_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后恢复 config 初始状态与 cleaner 撤销快照。"""
    config.reset()
    cleaner.clear_undo()
    yield
    config.reset()
    cleaner.clear_undo()


# =========================================================================== #
# A. 回归与结构性核对
# =========================================================================== #
class TestARegression:
    """A：detect() 契约与既有常量逐字未改。"""

    def test_detect_signature_unchanged(self):
        sig = inspect.signature(detector.detect)
        params = list(sig.parameters.items())
        assert len(params) == 1
        name, param = params[0]
        assert name == "text"
        assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD

    def test_hit_fields_unchanged_in_order(self):
        fields = [f.name for f in dataclasses.fields(detector.Hit)]
        assert fields == ["category", "category_label", "rule", "fragment", "position"]

    def test_legacy_constants_verbatim(self):
        assert detector._EDGE_WINDOW == 200
        assert detector._HEAD_SCAN == 200
        assert detector._TAIL_SCAN == 200
        assert detector.CATEGORY_INTERNAL_NOTE == "internal_note"
        assert detector.CATEGORY_AI_RESIDUE == "ai_residue"
        assert detector.CATEGORY_SENSITIVE == "sensitive"

    def test_detect_single_hit_priority(self):
        # 同时含三类命中 → 只返回最严重的一条（sensitive）。
        text = "当然可以。身份证 11010519491231002X。勿外传。"
        hit = detector.detect(text)
        assert hit is not None
        assert hit.category == detector.CATEGORY_SENSITIVE

        # 仅内部批注 vs AI 残留 → 内部批注优先。
        text2 = "勿外传。希望对你有帮助。"
        assert detector.detect(text2).category == detector.CATEGORY_INTERNAL_NOTE

    def test_detect_returns_none_for_benign(self):
        assert detector.detect("会议纪要：本次例会明确了三点安排。") is None
        assert detector.detect(None) is None
        assert detector.detect("   ") is None

    def test_detect_edge_window_semantics(self):
        # 内部批注仅看首尾 200 字：正中段（600 字）不命中。
        mid = "甲" * 399 + "转发文案" + "乙" * 197  # 总长 600，关键词跨越尾窗口起点
        assert detector.detect(mid) is None

    def test_find_spans_is_fulltext_and_independent(self):
        # 中段命中：detect() 看不到，find_spans() 必须看到。
        text = "甲" * 300 + "。" + "勿外传" + "。" + "乙" * 300
        assert detector.detect(text) is None  # 中段，首尾窗口外
        spans = detector.find_spans(text)
        assert any(s.category == detector.CATEGORY_INTERNAL_NOTE for s in spans)


# =========================================================================== #
# B. 免费版红线（最高优先级）
# =========================================================================== #
class _FakeNotifier:
    """假通知器，记录 notify / notify_cleaned 调用。"""

    def __init__(self) -> None:
        self.hits: list = []
        self.cleaned: list = []
        self.messages: list = []

    def notify(self, hit) -> None:
        self.hits.append(hit)

    def notify_cleaned(self, labels, can_undo=True) -> None:
        self.cleaned.append((list(labels), can_undo))

    def notify_message(self, title, message) -> None:
        self.messages.append((title, message))


def _run_main_harness(monkeypatch):
    """载入真实 main.main() 并捕获 handle_text / 通知 / 剪贴板写入。

    通过 monkeypatch 关闭 pystray 事件循环、剪贴板 I/O 与**全部模态弹窗**，
    **不启动任何消息循环、不弹任何真窗口**。

    ⚠️ 2026-10-06 修正：此前本夹具没有屏蔽 ``_message_box`` / ``_ask_yes_no``，
    而 ``main.main()`` 在「剪贴板订阅自检失败」时会调用 ``warn_listen_failed()``
    弹一个**真实模态 MessageBoxW**。那个窗口在无人点击时会一直阻塞，
    使本夹具跑出过 333 秒的用例（且行为随桌面状态飘忽）。已按本文件
    顶部的约束（「绝不启动……模态对话框」）补上屏蔽。
    """
    import clipboard_io  # noqa: F401  # 确保模块已加载（供引用一致性）
    import main
    import tray

    captured: dict = {"on_text": None, "notifications": [], "writes": [], "dialogs": []}

    def fake_start(self):  # 不 spawn 监听线程
        captured["on_text"] = self._on_text
        captured["listener"] = self

    monkeypatch.setattr(clipboard_listener.ClipboardListener, "start", fake_start)
    monkeypatch.setattr(tray.pystray.Icon, "run", lambda self, *a, **k: 0)
    monkeypatch.setattr(
        tray.TrayApp,
        "show_notification",
        lambda self, message, title: captured["notifications"].append((title, message)),
    )
    # 屏蔽真实模态弹窗（只记录，不显示）。
    def _record_info(text, title):
        captured["dialogs"].append(("info", text))

    def _record_ask(text, title):
        captured["dialogs"].append(("ask", text))
        return False

    monkeypatch.setattr(tray.TrayApp, "_message_box", staticmethod(_record_info))
    monkeypatch.setattr(tray.TrayApp, "_ask_yes_no", staticmethod(_record_ask))
    monkeypatch.setattr(
        clipboard_listener.clipboard_io,
        "write_text",
        lambda text: (captured["writes"].append(text) or True),
    )

    config.reset()
    main.main()
    assert captured["on_text"] is not None, "未能捕获 handle_text"
    return captured


class TestBFreeRedline:
    """B：**不存在任何付费墙** —— 检测 / 提醒 / 清洗都必须无条件生效。"""

    def test_detection_and_notify_fire_with_no_configuration(self, monkeypatch):
        """核心契约：**零配置**（不装码、不改设置）时检测与提醒照样工作。

        2026-10-07 授权体系已整体移除，本用例的原前提「未解锁」不复存在；
        它现在证明的是更强的一条：这个程序开箱即用，没有任何东西拦在检测前面。
        """
        cap = _run_main_harness(monkeypatch)
        config.reset()  # 回到「刚启动、什么都没配」的状态
        cap["notifications"].clear()
        cap["writes"].clear()

        cap["on_text"]("身份证号 11010519491231002X 请查收")

        assert cap["notifications"], "默认状态下检测/提醒被静默（P0 红线）"
        title, body = cap["notifications"][0]
        assert "敏感信息" in title
        # 脱敏片段，不含完整号码。
        assert "11010519491231002X" not in body

    def test_cleaning_works_with_no_configuration(self, monkeypatch):
        """核心契约：**零配置时，自动清洗照样生效**。

        这是「全部功能免费」唯一的、**可执行**的定义。付费墙当年就是 ``main.py``
        里那 11 个字符（``config.get_unlocked() and``），本用例从行为侧钉死它已消失：
        「开启清洗开关 + 未做任何配置」⇒ 剪贴板必须被清洗并写回。

        比读源码断言更强 —— 它证明的是**运行时行为**，而不是文本里少了个字符串。
        """
        cap = _run_main_harness(monkeypatch)
        config.reset()
        config.set_clean_enabled(True)
        cap["writes"].clear()

        cap["on_text"]("身份证号 11010519491231002X 请查收")

        assert cap["writes"], "自动清洗未生效 —— 付费墙疑似复活"
        written = cap["writes"][0]
        assert "11010519491231002X" not in written, "敏感信息未被清洗掉"
        assert "已移除" in written, "应是替换为占位符，而不是整段删除"

    def test_clipboard_untouched_when_clean_is_off(self, monkeypatch):
        """清洗开关关闭时，即使命中也不改剪贴板。

        ⚠️ 本用例原名 ``test_clipboard_not_modified_when_locked``，当时断言的是
        「未解锁 ⇒ 不许改剪贴板」。2026-10-07 授权不再是功能闸门，那个前提消失了；
        但**「默认不动用户内容」这条保护本身仍然成立，且必须守住** ——
        它现在的依据是「清洗开关默认关闭」（``config.CLEAN_ENABLED_DEFAULT``）。
        """
        cap = _run_main_harness(monkeypatch)
        config.reset()
        assert config.get_clean_enabled() is False, "默认必须是关闭"
        cap["writes"].clear()
        cap["on_text"]("身份证号 11010519491231002X")
        assert cap["writes"] == [], "清洗开关关闭时不得改写剪贴板"

    def test_free_config_apis_behave_like_v01(self):
        assert config.is_detection_active() is True
        assert config.get_enabled() is True

        config.pause(300)
        assert config.is_detection_active() is False
        assert config.seconds_until_resume() > 0
        config.resume()
        assert config.is_detection_active() is True

        assert config.toggle_enabled() is False
        assert config.get_enabled() is False
        config.set_enabled(True)

        assert config.should_emit("同一段文本") is True
        assert config.should_emit("同一段文本") is False

    def test_dead_key_class_of_bug_is_gone(self):
        """反回归：「点一次就变死按键」那一类 bug 不得复活。

        历史（2026-10-06 用户实测报「很多按钮点击无反应」）：``_on_locked_guide``
        曾用 ``config.mark_clean_guide_shown()`` 做「会话内只弹一次」门控，而它同时挂在
        「自动清洗粘贴」「Markdown → Word」「网页表格 → Excel」**三个菜单项**上，
        于是用户点过一次之后这三个按钮一起变成静默 ``return``。

        2026-10-07 起全部功能免费，该函数与它服务的「未解锁引导」**整体删除**。
        本用例改为守住三件事：
          1. 那个门控状态变量不得复活；
          2. 「未解锁引导」函数不得复活 —— 有它就说明又出现了「功能入口会被拦住」的形态；
          3. 菜单回调里唯一的早退理由必须仍是**受审计的模态闸门**（它挡的是叠窗，
             挡下时会把已有窗口提到前台并留日志，而不是「少打扰」）。
        """
        config.reset()
        for name in ("is_clean_guide_shown", "mark_clean_guide_shown"):
            assert not hasattr(config, name), f"门控函数 {name} 不应存在"
        # 只看**纯代码视图**（剥掉注释）：文档里说明这段历史是允许的。
        code = "\n".join(
            line.split("#", 1)[0] for line in _read_source("config.py").splitlines()
        )
        assert "_clean_guide_shown" not in code, "config.py 里不应再出现该状态变量"

        tray_src = _read_source("tray.py")
        assert "def _on_locked_guide" not in tray_src, (
            "「未解锁引导」不得复活：全部功能免费后，不存在任何「功能入口被拦住」的形态"
        )
        # 上面那个例外只认 `dialogs.guard` 这一种实现，防止有人把它改成空操作。
        assert "dialogs.guard(" in tray_src, "受审计的模态闸门必须走 dialogs.guard"
        guard_body = _read_source("dialogs.py").split("def guard(", 1)[1].split("\ndef ", 1)[0]
        assert "reforeground()" in guard_body, (
            "被模态闸门挡下时没有把已有对话框提到前台，用户仍会觉得「点了没反应」"
        )
        assert "diagnostics.log(" in guard_body, "被模态闸门挡下时必须留日志"

    def test_no_unlock_gate_in_main(self):
        """反回归：功能路径**不得**依赖支持者标识 —— 否则又退化回 open core。

        2026-10-07 开源前，清洗分支写作
        ``config.get_unlocked() and config.get_clean_enabled()``；
        开头那 11 个字符就是**整个付费墙**，删掉它之后全部功能才真正免费。

        现在 main.py 里**不应再出现任何** ``get_unlocked`` 引用。日后加新功能时
        若有人顺手加了授权判断，本用例会立刻拦下。
        """
        src = _read_source("main.py")
        # 取「纯代码视图」：剥掉三引号字符串（含模块 docstring）与 ``#`` 注释。
        # 理由：文档里**说明**这段历史是必要且有价值的（否则后来者不懂为什么不能加回来），
        # 但代码里不得有任何**可执行**的授权判断 —— 所以守卫只针对可执行部分。
        parts = src.split('"""')
        code_only = "".join(parts[::2])
        code_only = "\n".join(
            line.split("#", 1)[0] for line in code_only.splitlines()
        )
        assert "get_unlocked" not in code_only, (
            "main.py 的功能路径不得依赖支持者标识，否则开源版就成了「开源但锁功能」"
        )
        # 清洗分支现在只由用户开关决定。
        assert "if config.get_clean_enabled():" in code_only

    def test_no_length_cap_or_lock_gate_in_client(self):
        # 全客户端不得出现「未解锁 → 限制长度 / 跳过 / 降级」的编码。
        for name in CLIENT_MODULES:
            src = _read_source(name)
            assert "MAX_TEXT_LEN" not in src
            assert "truncate_for_free" not in src


# =========================================================================== #
# C. （本节已删除）授权模块对抗
#
#     2026-10-07：本项目转为全开源、全部功能免费，Ed25519 授权模块（licensing /
#     license_codec）与激活码输入框（ui_dialog）已整体下线。原先本节守的
#     「篡改码 / 伪造 license.json / 坏公钥 / 过期码」等场景已无攻击面，故整节移除。
#
#     但两条**仍然有效的不变量**被迁到别处，不能随本节一起消失：
#       1. 客户端完全不含私钥材料 —— TestHStructure.test_client_has_no_private_key_material
#       2. 客户端零网络能力       —— TestGPrivacy.test_zero_network_in_client
# =========================================================================== #

# =========================================================================== #
# D. 清洗模块对抗性
# =========================================================================== #
class TestDCleaner:
    """D：清洗模块的独立补正反例。"""

    def test_middle_hit_is_cleaned_fulltext(self):
        # 命中放在正中（前后各 >500 字），必须被清洗（CLN-4 全文生效）。
        text = "前" * 600 + "。" + "身份证 11010519491231002X" + "。" + "后" * 600
        result = cleaner.clean(text)
        assert result.skipped is False
        assert "11010519491231002X" not in result.cleaned_text
        assert "［已移除：身份证］" in result.cleaned_text

    def test_middle_internal_note_cleaned(self):
        text = "前" * 600 + "。" + "勿外传" + "。" + "后" * 600
        result = cleaner.clean(text)
        assert result.skipped is False
        assert "勿外传" not in result.cleaned_text

    def test_safety_valve_below_exactly_above(self):
        pad = "甲"
        # 59.2%：74 个 AI 尾句共移除 592 字 / 1000 总长 → 应用。
        # 降误报修订（2026-10-06）：裸 16 位数字「6222021234567890」不再被判为银行卡
        # （Luhn 校验不通过且邻近无「卡」类上下文词），故不再计入移除量；
        # 原期望值 0.599 随之调整为 0.592（差 7 字 = 银行卡占位的净移除量）。
        t_below = "希望对你有帮助。" * 74 + "6222021234567890" + pad * 392
        r_below = cleaner.clean(t_below)
        assert r_below.removed_ratio == pytest.approx(0.592, abs=1e-9)
        assert r_below.skipped is False, "59.2% 不应触发安全阀"

        # 恰好 60.0% → 应用（阈值语义为「严格大于」）。
        t_exact = "希望对你有帮助。" * 75 + pad * 400
        r_exact = cleaner.clean(t_exact)
        assert r_exact.removed_ratio == pytest.approx(0.6, abs=1e-9)
        assert r_exact.skipped is False, "恰好 60% 不应被放弃（off-by-one）"

        # 60.06%（>60%）→ 放弃清洗、仅提醒。
        t_above = "希望对你有帮助。" * 75 + pad * 399
        r_above = cleaner.clean(t_above)
        assert r_above.removed_ratio > 0.6
        assert r_above.skipped is True, ">60% 必须放弃清洗"

    def test_placeholder_and_no_raw_digits(self):
        text = "联系人 13800138000，证件 11010519491231002X，卡 6222021234567890。"
        result = cleaner.clean(text)
        assert result.skipped is False
        assert re.search(r"\d{6,}", result.cleaned_text) is None, "清洗后仍残留长数字串"
        assert "［已移除：" in result.cleaned_text

    def test_undo_byte_exact_and_context(self):
        text = "🚀 电话 13800138000 结束。\n第二行 勿外传。"
        result = cleaner.clean(text)
        assert result.cleaned_text != text
        assert cleaner.has_undo() is True
        undone = cleaner.undo()
        assert undone is not None
        assert undone.cleaned_text == text, "撤销未能逐字节恢复原文"
        # 只能撤销最近一次。
        assert cleaner.has_undo() is False
        assert cleaner.undo() is None

    def test_undo_snapshot_not_persisted(self):
        # cleaner 模块不留任何磁盘写入。
        src = _read_source("cleaner.py")
        assert "open(" not in src
        assert "write" not in src.replace("#", "")

    def test_clean_idempotent(self):
        text = "电话 13800138000。希望对你有帮助。这是正文。"
        first = cleaner.clean(text)
        second = cleaner.clean(first.cleaned_text)
        assert second.cleaned_text == first.cleaned_text, "二次清洗造成二次破坏"
        assert second.removed == []
        assert second.skipped is False

    def test_benign_text_returns_unchanged(self):
        text = "会议纪要：本次例会明确了三点安排，各部门按计划推进即可。"
        result = cleaner.clean(text)
        assert result.cleaned_text == text
        assert result.removed == []
        assert cleaner.has_undo() is False

    def test_extreme_inputs(self):
        assert cleaner.clean(None).cleaned_text == ""
        assert cleaner.clean("").cleaned_text == ""
        assert cleaner.clean("   \n  ").skipped is False
        big = "正文内容。" * 25000  # 12.5 万字
        result = cleaner.clean(big)
        assert result.cleaned_text == big

    def test_long_text_performance(self):
        import time

        big = "正文内容。" * 25000
        start = time.time()
        cleaner.clean(big)
        assert time.time() - start < 1.0, "超长文本清洗耗时过高"


# =========================================================================== #
# E. 剪贴板写回防自触发
# =========================================================================== #
class _Clock:
    def __init__(self, start=1000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _Clipboard:
    def __init__(self):
        self.queue: list[str] = []
        self.written: list[str] = []

    def read_text(self):
        return self.queue.pop(0) if self.queue else None

    def read_html(self):
        return None

    def write_text(self, text):
        self.written.append(text)
        return True


class TestESuppression:
    """E：写回防自触发 + 内容哈希防抖。"""

    def _env(self, monkeypatch):
        clock = _Clock()
        monkeypatch.setattr(clipboard_listener, "time", types.SimpleNamespace(monotonic=clock))
        clip = _Clipboard()
        monkeypatch.setattr(clipboard_listener, "clipboard_io", clip)
        collected: list[str] = []
        listener = clipboard_listener.ClipboardListener(collected.append)
        return listener, clip, clock, collected

    def test_writeback_does_not_trigger_reclean(self, monkeypatch):
        listener, clip, _clock, collected = self._env(monkeypatch)
        listener.set_text("CLEANED")
        clip.queue.append("CLEANED")  # OS 因写回投递的事件
        listener._handle_update()
        assert collected == [], "写回事件被二次处理（潜在死循环）"

    def test_real_copy_of_different_content_never_swallowed(self, monkeypatch):
        for delay in (0.0, 0.1, 0.25, 2.0):
            listener, clip, clock, collected = self._env(monkeypatch)
            listener.set_text("CLEANED")
            clip.queue.append("CLEANED")
            listener._handle_update()
            clock.advance(delay)
            clip.queue.append("REAL-USER-COPY")
            listener._handle_update()
            assert collected == ["REAL-USER-COPY"], f"delay={delay} 时真实复制被吞"

    def test_same_content_as_writeback_within_debounce_is_folded(self, monkeypatch):
        # 偏离 #4 的实际行为：写回后 200ms 内复制「与写回内容完全相同」的文本会被折叠。
        for delay, expect_delivered in ((0.0, False), (0.1, False), (0.25, True), (2.0, True)):
            listener, clip, clock, collected = self._env(monkeypatch)
            listener.set_text("CLEANED")
            clip.queue.append("CLEANED")
            listener._handle_update()  # 消费写回事件（并登记 _last_digest）
            clock.advance(delay)
            clip.queue.append("CLEANED")
            listener._handle_update()
            delivered = collected == ["CLEANED"]
            assert delivered is expect_delivered, f"delay={delay} 行为与预期不符"

    def test_rapid_distinct_copies_all_delivered(self, monkeypatch):
        listener, clip, _clock, collected = self._env(monkeypatch)
        clip.queue.extend(["A", "B", "C", "D"])
        for _ in range(4):
            listener._handle_update()
        assert collected == ["A", "B", "C", "D"]

    def test_suppression_window_expiry(self, monkeypatch):
        listener, clip, clock, collected = self._env(monkeypatch)
        listener.set_text("X")
        clock.advance(clipboard_listener._SUPPRESS_WINDOW + 0.5)
        clip.queue.append("X")
        listener._handle_update()
        assert collected == ["X"]

    def test_writeback_event_injected_twice_processed_once(self, monkeypatch):
        listener, clip, _clock, collected = self._env(monkeypatch)
        listener.set_text("CLEANED")
        clip.queue.extend(["CLEANED", "CLEANED"])
        listener._handle_update()
        listener._handle_update()
        assert collected == []


# =========================================================================== #
# F. 格式转换对抗性 + 产物读回
# =========================================================================== #
class TestFConverter:
    """F：转换产物读回校验。"""

    @pytest.fixture()
    def outdir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter, "CONVERT_OUTPUT_DIR", str(tmp_path))
        return tmp_path

    def test_markdown_full_readback(self, outdir):
        import docx

        md = (
            "# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6\n"
            "段落 **加粗** 与 *斜体* 与 `code`。\n"
            "- 无序甲\n- 无序乙\n"
            "1. 有序甲\n2. 有序乙\n"
            "> 引用内容\n"
            "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n"
            "```python\nprint(1)\n```\n"
            "末尾段落。\n"
        )
        result = converter.markdown_to_docx(md)
        assert result.ok is True
        document = docx.Document(result.path)

        # 标题 1~6 级。
        heading_pairs = [
            (p.text, p.style.name)
            for p in document.paragraphs
            if p.style.name.startswith("Heading")
        ]
        assert heading_pairs == [(f"H{i}", f"Heading {i}") for i in range(1, 7)]

        # 行内加粗 / 斜体 / 等宽。
        para = next(p for p in document.paragraphs if "加粗" in p.text)
        assert any(r.bold for r in para.runs)
        assert any(r.italic for r in para.runs)
        assert any(r.font.name == "Consolas" for r in para.runs)

        # 列表样式。
        styles = {p.style.name for p in document.paragraphs}
        assert "List Bullet" in styles
        assert "List Number" in styles
        assert "Intense Quote" in styles

        # 代码块等宽。
        code_p = next(p for p in document.paragraphs if p.text == "print(1)")
        assert code_p.runs[0].font.name == "Consolas"

        # 表格读回。
        assert len(document.tables) == 1
        table = document.tables[0]
        assert (len(table.rows), len(table.columns)) == (2, 2)
        assert [c.text for c in table.rows[0].cells] == ["列A", "列B"]

    def test_cf_html_table_colspan_rowspan_readback(self, outdir, monkeypatch):
        import openpyxl

        html = (
            "<table>"
            "<tr><th colspan='2'>跨列表头</th><th>C</th></tr>"
            "<tr><td rowspan='2'>跨行</td><td>b1</td><td>c1</td></tr>"
            "<tr><td>b2</td><td>c2</td></tr>"
            "</table>"
        )
        monkeypatch.setattr(converter, "clipboard_io", _FakeClip(html=html))
        result = converter.clipboard_to_xlsx()
        assert result.ok is True
        sheet = openpyxl.load_workbook(result.path).active
        assert sheet.max_row == 3
        assert sheet.max_column == 3
        assert [sheet.cell(1, c).value for c in (1, 2, 3)] == ["跨列表头", "跨列表头", "C"]
        assert sheet.cell(2, 1).value == "跨行"
        assert sheet.cell(3, 1).value in (None, "")  # rowspan 向下补空占位
        assert sheet.cell(3, 2).value == "b2"

    def test_nested_table_degrades_with_note(self, outdir, monkeypatch):
        # BUG-1 修复后：嵌套表应「降级铺格 + 提示」，且顶层仅 1 张表（不误报多张）。
        nested = "<table><tr><td>x<table><tr><td>y</td></tr></table></td></tr></table>"
        monkeypatch.setattr(converter, "clipboard_io", _FakeClip(html=nested))
        result = converter.clipboard_to_xlsx()
        assert result.ok is True, f"嵌套表未降级产出（message={result.message!r}）"
        assert "铺格" in result.message
        assert "多张表格" not in result.message, "嵌套表被误判为多张表"

    def test_nested_table_fragment_keeps_outer_close(self):
        # 修复后：外层闭合标签必须保留，片段能进入解析器并触发 degraded。
        nested = "<table><tr><td>x<table><tr><td>y</td></tr></table></td></tr></table>"
        fragment, count = converter._extract_first_table(nested)
        assert count == 1
        assert fragment is not None
        assert fragment.count("<table") == 2
        assert fragment.count("</table>") == 2, "外层 </table> 仍被截断"
        parser = converter.HtmlTableParser()
        parser.feed(fragment)
        parser.close()
        assert parser.degraded is True
        assert parser.rows == [["xy"]]

    def test_plain_table_tab_and_pipe(self, outdir, monkeypatch):
        monkeypatch.setattr(converter, "clipboard_io", _FakeClip(text="A\tB\n1\t2"))
        assert converter.clipboard_to_xlsx().ok is True
        monkeypatch.setattr(
            converter, "clipboard_io", _FakeClip(text="| A | B |\n| --- | --- |\n| 1 | 2 |")
        )
        assert converter.clipboard_to_xlsx().ok is True

    def test_no_table_fails_clearly(self, outdir, monkeypatch):
        monkeypatch.setattr(converter, "clipboard_io", _FakeClip(text="只是一些普通文字。"))
        r = converter.clipboard_to_xlsx()
        assert r.ok is False
        assert r.message == "剪贴板没有可识别的表格"

        monkeypatch.setattr(converter, "clipboard_io", _FakeClip())
        r2 = converter.clipboard_to_xlsx()
        assert r2.ok is False
        assert r2.message == "剪贴板为空"

    def test_zero_dialog(self):
        src = _read_source("converter.py")
        for token in ("filedialog", "askstring", "askopenfilename", "asksaveasfilename"):
            assert token not in src

    def test_filename_pattern_and_no_overwrite(self, outdir, monkeypatch):
        # OBS-1 修复后：同一秒内两次转换**绝不覆盖**已有产物（追加序号）。
        import time as _time

        real_strftime = _time.strftime

        def _fake_strftime(fmt, *args, **kwargs):
            if fmt == converter.CONVERT_FILENAME_PATTERN:
                return "PastePing_20260101_120000"
            return real_strftime(fmt, *args, **kwargs)

        monkeypatch.setattr(converter.time, "strftime", _fake_strftime)
        md = "# 标题\n正文。\n"
        r1 = converter.markdown_to_docx(md)
        r2 = converter.markdown_to_docx(md)
        assert os.path.basename(r1.path) == "PastePing_20260101_120000_md2docx.docx"
        assert r1.path != r2.path, "同秒二次转换覆盖了前一个产物"
        assert re.search(r"PastePing_\d{8}_\d{6}_md2docx(_\d+)?\.docx$", r2.path)
        assert os.path.exists(r1.path) and os.path.exists(r2.path)

    def test_converter_does_not_write_clipboard(self):
        src = _read_source("converter.py")
        assert "write_text" not in src
        assert "SetClipboardData" not in src

    def test_clipboard_io_slice_fragment(self):
        import clipboard_io

        body = "<html><body><table><tr><td>A</td></tr></table></body></html>"
        prefix = "Version:0.9\r\nStartHTML:0000000000\r\nStartFragment:{sf}\r\nEndFragment:{ef}\r\n"
        # 先占位算偏移，再回填（偏移为整块字节偏移）。
        header0 = prefix.format(sf="0000000000", ef="0000000000")
        start = len(header0.encode("utf-8"))
        end = start + len(body.encode("utf-8"))
        header = prefix.format(sf=f"{start:010d}", ef=f"{end:010d}")
        raw = (header + body).encode("utf-8")
        sliced = clipboard_io.slice_html_fragment(raw)
        assert sliced == body

    def test_converter_failure_does_not_crash(self, outdir, monkeypatch):
        class Boom:
            def read_html(self):
                raise RuntimeError("boom")

            def read_text(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(converter, "clipboard_io", Boom())
        r = converter.clipboard_to_xlsx()
        assert r.ok is False  # 不崩溃


class _FakeClip:
    def __init__(self, html=None, text=None):
        self._html = html
        self._text = text

    def read_html(self):
        return self._html

    def read_text(self):
        return self._text


# =========================================================================== #
# F+. 终轮：验证工程师两轮修复 + 对新增 _extract_first_table 的对抗性试探
#
#     约定：以 ``test_char_`` 开头的用例是 **characterization（特征固化）**用例，
#     固化的是「当前实现的实际行为」而非「应当如此」；一旦相关实现变更，需显式
#     翻转这些断言（本次 BUG-1 修复即因未标记而一度与真回归混淆）。
# =========================================================================== #
class TestFConverterFixVerify:
    """F+：BUG-1 / OBS-1 修复复验 + 新代码对抗性试探。"""

    @pytest.fixture()
    def outdir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(converter, "CONVERT_OUTPUT_DIR", str(tmp_path))
        return tmp_path

    @staticmethod
    def _convert(html=None, text=None, outdir=None):
        import openpyxl

        original = converter.clipboard_io
        converter.clipboard_io = _FakeClip(html=html, text=text)
        try:
            result = converter.clipboard_to_xlsx()
        finally:
            converter.clipboard_io = original
        rows = None
        if result.ok:
            sheet = openpyxl.load_workbook(result.path).active
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        return result, rows

    # ---------------- 一. 修复验证 ---------------- #
    def test_parallel_tables_first_only(self, outdir):
        html = (
            "<table><tr><th>A1</th><th>A2</th></tr><tr><td>a</td><td>b</td></tr></table>"
            "<table><tr><td>B1</td><td>B2</td></tr></table>"
        )
        result, rows = self._convert(html=html, outdir=outdir)
        assert result.ok is True
        assert "检测到多张表格，已转换第 1 张" in result.message
        assert rows == [["A1", "A2"], ["a", "b"]], "第二张表内容混入或列数错"
        assert all("B1" not in str(cell) for row in rows for cell in row)

    def test_nested_plus_parallel_both_notes_coexist(self, outdir):
        html = (
            "<table><tr><td>x<table><tr><td>y</td></tr></table></td></tr></table>"
            "<table><tr><td>B</td></tr></table>"
        )
        result, rows = self._convert(html=html, outdir=outdir)
        assert result.ok is True
        assert "铺格" in result.message
        assert "检测到多张表格" in result.message

    def test_malformed_only_open(self, outdir):
        result, _rows = self._convert(html="<table><tr><td>x</td></tr>", outdir=outdir)
        assert result.ok is False
        assert result.message  # 文案明确、不崩溃

    def test_malformed_only_close(self, outdir):
        result, _rows = self._convert(html="<tr><td>x</td></tr></table>", outdir=outdir)
        assert result.ok is False
        assert result.message

    def test_plain_single_table_regression(self, outdir):
        # 复跑上一轮 colspan/rowspan 用例，确认未被两轮修复破坏。
        html = (
            "<table><tr><th colspan='2'>H</th><th>C</th></tr>"
            "<tr><td rowspan='2'>R</td><td>b1</td><td>c1</td></tr>"
            "<tr><td>b2</td><td>c2</td></tr></table>"
        )
        result, rows = self._convert(html=html, outdir=outdir)
        assert result.ok is True
        assert rows == [["H", "H", "C"], ["R", "b1", "c1"], [None, "b2", "c2"]]

    # ---------------- 二. 新代码对抗性试探 ---------------- #
    def test_tableau_prefix_not_matched(self):
        # \b 生效：<tableau> 不是表格标签，不应被提取。
        fragment, count = converter._extract_first_table("<tableau>hi</tableau>")
        assert fragment is None and count == 0

    def test_char_table_dash_prefix_is_matched(self):
        # ⚠️CHARACTERIZATION：`\b` 对 '-' 成立，故 <table-x> **会**被当作表格标签。
        # 属低危误匹配（自定义元素在复制的 CF_HTML 中极罕见）；改动需翻转本断言。
        fragment, count = converter._extract_first_table("<table-x><tr><td>a</td></tr></table-x>")
        assert fragment == "<table-x><tr><td>a</td></tr></table-x>"
        assert count == 1

    def test_uppercase_and_attrs_and_space_close(self):
        for html in (
            "<TABLE><TR><TD>a</TD></TR></TABLE>",
            '<table border="1" cellpadding=0><tr><td>a</td></tr></table >',
            "<TaBlE><tr><td>a</td></tr></TaBlE>",
        ):
            fragment, count = converter._extract_first_table(html)
            assert count == 1, f"未处理：{html}"

    def test_escaped_entities_not_matched(self):
        # 转义文本 &lt;table&gt; 不算标签；真实表仍被正确提取。
        html = (
            "<p>&lt;table&gt;&lt;tr&gt;&lt;td&gt;x&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</p>"
            "<table><tr><td>real</td></tr></table>"
        )
        fragment, count = converter._extract_first_table(html)
        assert count == 1
        assert "real" in fragment and "&lt;table&gt;" not in fragment

    def test_depth_never_negative(self):
        # 以多余的 </table> 开头：深度不得变负，真实表仍被正确提取。
        fragment, count = converter._extract_first_table(
            "</table></table><table><tr><td>a</td></tr></table>"
        )
        assert count == 1
        assert "<tr><td>a</td></tr>" in fragment

    def test_char_comment_table_is_treated_as_first(self, outdir):
        # ⚠️CHARACTERIZATION：正则不识别 HTML 注释；注释内的 <table> 会被当成表格。
        # 若注释表在真实表之前 → 会转换「注释里的表」。属低危（CF_HTML 罕见注释表）；
        # 改动需翻转本断言。
        html = (
            "<!-- <table><tr><td>OLD</td></tr></table> -->"
            "<table><tr><td>REAL</td></tr></table>"
        )
        result, rows = self._convert(html=html, outdir=outdir)
        assert result.ok is True
        assert rows == [["OLD"]], "实际行为：转换了注释中的表"
        assert "检测到多张表格" in result.message

    def test_table_without_rows_fails_clearly(self, outdir):
        # 表内无 <tr>/<td>（文本直接置于 table）→ 明确失败，不崩溃。
        result, _rows = self._convert(html="<table>just text</table>", outdir=outdir)
        assert result.ok is False

    def test_empty_vs_no_table_messages(self, outdir):
        r_empty, _ = self._convert(html=None, text=None, outdir=outdir)
        assert r_empty.message == "剪贴板为空"
        r_blank, _ = self._convert(html=None, text="   ", outdir=outdir)
        assert r_blank.message == "剪贴板为空"
        r_prose, _ = self._convert(html=None, text="普通文字，无表格。", outdir=outdir)
        assert r_prose.message == "剪贴板没有可识别的表格"


# =========================================================================== #
# G. 隐私与合规（静态核实）
# =========================================================================== #
class TestGPrivacy:
    """G：隐私与合规的静态核实。"""

    def test_zero_network_in_client(self):
        for name in CLIENT_MODULES:
            src = _read_source(name)
            for token in ("import requests", "import urllib", "import socket", "http.client",
                          "import httpx", "urlopen"):
                assert token not in src, f"{name} 出现网络能力：{token}"

    def test_no_persistent_state_file(self):
        """客户端不得写任何持久化状态文件 —— 原先那个 ``license.json`` 已随授权体系移除。

        本用例是**反回归**：若日后有人又给程序加了「记住设置 / 记住状态」的落盘文件，
        它会立刻失败，逼我们先想清楚「这份文件里会不会出现剪贴板内容」。
        """
        assert "STORE_PATH" not in _read_source("config.py"), (
            "config 里不得再有任何持久化文件路径常量"
        )
        # 唯一允许写盘的客户端模块是 diagnostics（只记长度与规则名，绝不记原文）。
        for name in CLIENT_MODULES:
            if name == "diagnostics.py":
                continue
            assert not re.search(r"\bopen\s*\([^)]*['\"]w", _read_source(name)), (
                f"{name} 出现写文件行为；本项目除诊断日志外不写任何文件"
            )

    @staticmethod
    def _code_only(src: str) -> str:
        """去掉整行注释与行尾注释，得到「纯代码」视图。

        价格检查应该针对**代码里的字面量**：注释里提到平台自带的档位（例如主页上
        另有按月赞助档）不属于硬编码问题，而出现在字符串里的金额才是真问题。
        """
        return "\n".join(line.split("#", 1)[0] for line in src.splitlines())

    def test_no_hardcoded_price_outside_config(self):
        for name in CLIENT_MODULES:
            if name == "config.py":
                continue
            code = self._code_only(_read_source(name))
            # 2026-10-06 修正：原实现直接断言裸子串 "39" 不在源码中，会被**坐标、行号**
            # 等无关数字误伤（tray.py 的图标几何参数 (21, 9, 51, 39) 即被误判为金额）。
            # 守卫的意图是「金额不得硬编码」，故改为按**金额书写形态 + 纯代码视图**判定 ——
            # 既不误伤注释与坐标，也照样拦得住 `¥100` / `100 元` / `￥100` 这类真正的问题写法。
            assert not re.search(r"[¥￥]\s*[0-9]", code), f"{name} 出现硬编码金额"
            assert not re.search(r"[0-9]+\s*元", code), f"{name} 出现硬编码金额"

        # 自检：确认上面两条正则**真的抓得住**问题写法 —— 否则守卫被削弱也无人察觉。
        assert re.search(r"[¥￥]\s*[0-9]", 'x = "限时价 ¥100"') is not None
        assert re.search(r"[0-9]+\s*元", 'x = "100 元一份"') is not None
        assert re.search(
            r"[¥￥]\s*[0-9]", self._code_only('# 注释里提到 ¥5 不算\nx = 1')
        ) is None
        # 金额常量只应**定义**在 config；tray 只能引用它，不得写死数字。
        assert "SUPPORT_PRICE_CNY" in _read_source("config.py")
        assert "SUPPORT_PRICE_CNY" in _read_source("tray.py")

    def test_support_entry_changes_no_state(self, monkeypatch):
        """赞助入口必须**只打开一个链接**，不得改变程序任何状态。

        2026-10-06 起打开链接走「os.startfile → ShellExecuteW → webbrowser」三级降级
        （打包 exe 无控制台时 webbrowser 不可靠），故在 ``open_url`` 这一层替换。

        原先这条守的是「打赏不能顺手把人解锁」；授权体系移除后它升级为更强的一条：
        点赞助之后，检测开关、清洗开关、暂停状态**统统不变**。
        """
        import tray

        opened: list = []
        monkeypatch.setattr(tray, "open_url", lambda url: opened.append(url) or True)
        try:
            app = tray.TrayApp()
        except Exception as exc:  # pragma: no cover - 无 GUI 环境
            pytest.skip(f"无法构造 TrayApp：{exc}")

        config.reset()

        def snapshot() -> tuple:
            """取一份「程序对外可见状态」的快照。"""
            return (
                config.get_enabled(),
                config.get_clean_enabled(),
                config.is_detection_active(),
                config.seconds_until_resume(),
            )

        before = snapshot()
        app._on_support(None, None)
        assert opened == [tray.SUPPORT_URL]
        assert snapshot() == before, "赞助入口改变了程序状态 —— 它应该只打开一个链接"

    def test_free_wording_is_current(self):
        """免费声明必须与代码一致 —— 这条守卫的**方向在 2026-10-07 反转了**。

        历史：v0.2 引入付费墙时，本用例断言 README 与 tray **不得**出现「完全免费」
        之类的措辞 —— 因为那时说「完全免费」是**假话**（增强功能要解锁才能用）。

        2026-10-07 项目转为全开源 + 全部功能免费，付费墙（``main.py`` 的
        ``get_unlocked() and``）已被移除，于是两侧互换：
          · 暗示「只有基础功能免费」的措辞必须消失；
          · 免费声明必须出现。

        **守卫的意图从未改变：让文档说的和代码做的一致。** 变的只是哪一侧才是真话。
        """
        tray_src = _read_source("tray.py")
        readme = _read_source("README.md")
        readme_en = _read_source("README.en.md")
        # 不得再暗示存在付费档（中英 README 是同一份对外文案，一并守）。
        for name, src in (("tray.py", tray_src), ("README.md", readme),
                          ("README.en.md", readme_en)):
            assert "基础提醒功能永久免费" not in src, (
                f"{name} 仍在暗示「只有基础功能免费」，与全部功能免费的事实不符"
            )
            assert "需一次性赞助后赠送激活码解锁" not in src, (
                f"{name} 仍在描述付费解锁流程"
            )
        # 免费声明必须出现。
        assert "永久免费" in tray_src
        assert "无内购" in tray_src
        assert "无功能锁" in tray_src
        assert "全部功能" in readme and "免费" in readme
        # 英文 README 必须给出等价的免费声明，否则英文受众会以为存在付费档。
        assert "completely free" in readme_en
        assert "no in-app purchases" in readme_en
        assert "no feature locks" in readme_en
        assert "unlocks nothing" in readme_en

    def test_about_has_disclaimer_and_privacy(self):
        import tray

        text = tray._about_text()
        assert "免责声明" in text
        assert "不联网" in text
        assert "本地上" in text or "本地运行" in text
        assert "永久免费" in text
        # 反馈渠道走 GitHub Issues；本项目不公开任何联系邮箱。
        assert tray.ISSUES_URL in text


# =========================================================================== #
# H. 结构性核对
# =========================================================================== #
class TestHStructure:
    """H：依赖、菜单、依赖方向。"""

    def test_requirements_exactly_five_deps(self):
        """运行时依赖恰好 5 个。授权体系移除后 ``cryptography`` 已不再是依赖。"""
        raw = _read_source("requirements.txt")
        deps = [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        names = [re.split(r"[><=]", d)[0].strip() for d in deps]
        assert names == [
            "pywin32",
            "pystray",
            "Pillow",
            "python-docx",
            "openpyxl",
        ]

    def test_no_seventh_third_party_import(self):
        third_party = {"win32clipboard", "pystray", "PIL", "docx", "openpyxl"}
        allowed_stdlib = {
            "ctypes", "hashlib", "json", "os", "re", "sys", "threading", "time",
            "webbrowser", "html", "datetime", "dataclasses", "typing",
            "collections", "argparse", "unicodedata",
            # 2026-10-06 新增：diagnostics.log_exception 用它取异常出错行号。
            # 无控制台的 --windowed exe 里只有类型和消息不足以定位问题，行号是关键。
            "traceback",
        }
        project = {
            "config", "detector", "clipboard_listener", "clipboard_io", "cleaner",
            "notifier", "tray", "main", "converter", "diagnostics", "dialogs",
        }
        for name in CLIENT_MODULES:
            src = _read_source(name)
            for line in src.splitlines():
                match = re.match(r"^\s*(?:import|from)\s+([A-Za-z_][\w\.]*)", line)
                if not match:
                    continue
                top = match.group(1).split(".")[0]
                if top in project or top == "__future__":
                    continue
                assert top in third_party or top in allowed_stdlib, (
                    f"{name} 引入未声明依赖：{top}"
                )

    def _menu_texts(self, app):
        out = []

        def walk(items, depth=0):
            for item in items:
                text = item.text if isinstance(item.text, str) else item.text(item)
                out.append("  " * depth + str(text))
                if item.submenu is not None:
                    walk(item.submenu.items, depth + 1)

        walk(app.icon.menu.items)
        return out

    def test_menu_is_single_state_and_all_features_available(self):
        """菜单单态：**全部功能始终可用**，且不存在任何「码 / 解锁 / 购买」入口。

        2026-10-07 起取消「未解锁 / 已解锁」双态，并进一步移除了整个授权层。
        本用例把两件事同时钉死 —— 这是「全部功能免费」在界面上唯一可静态验证的证据。
        """
        import tray

        config.reset()
        try:
            app = tray.TrayApp()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"无法构造 TrayApp：{exc}")
        app.rebuild_menu()
        texts = self._menu_texts(app)

        # v0.1 六项一个不少。
        for legacy in ("敏感提醒", "暂停 5 分钟", "暂停 1 小时", "关于", "退出"):
            assert legacy in texts, f"缺少 v0.1 菜单项：{legacy}"
        # 全部增强功能入口恒在。
        assert any("自动清洗粘贴" in t for t in texts)
        assert any("格式转换" in t for t in texts)
        assert any("Markdown → Word" in t for t in texts)
        assert any("网页表格 → Excel" in t for t in texts)
        assert any("打开结果文件夹" in t for t in texts)
        # 唯一的付费相关入口是纯自愿的赞助，且必须写明它不影响功能。
        assert any("支持开发者" in t and "不影响任何功能" in t for t in texts)
        # 不得出现任何「码 / 解锁 / 购买」形态的入口。
        for banned in ("支持者码", "解锁", "购买", "🔒"):
            assert not any(banned in t for t in texts), (
                f"菜单里仍出现 {banned!r} —— 收费解锁入口必须彻底消失"
            )

    def test_undo_item_appears_only_with_undo(self):
        import tray

        class FakeCleaner:
            def __init__(self, has):
                self._has = has

            def has_undo(self):
                return self._has

        config.reset()
        try:
            app = tray.TrayApp()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"无法构造 TrayApp：{exc}")

        app.attach_cleaner(FakeCleaner(False))
        app.rebuild_menu()
        assert not any("撤销上次清洗" in t for t in self._menu_texts(app))

        app.attach_cleaner(FakeCleaner(True))
        app.rebuild_menu()
        assert any("撤销上次清洗" in t for t in self._menu_texts(app))

    @staticmethod
    def _imports(name: str) -> set:
        """提取某模块真实 import 的顶层模块名（排除注释/文档字符串误伤）。"""
        src = _read_source(name)
        found = set()
        for line in src.splitlines():
            match = re.match(r"^\s*(?:import|from)\s+([A-Za-z_][\w\.]*)", line)
            if match:
                found.add(match.group(1).split(".")[0])
        return found

    def test_dependency_direction(self):
        """依赖方向必须严格单向 —— 这是本项目可测试、可替换性的基础。"""
        project_modules = {
            "config", "detector", "clipboard_listener", "clipboard_io", "cleaner",
            "notifier", "tray", "main", "converter", "diagnostics", "dialogs",
        }
        # 三个「零依赖」模块：不 import 本项目任何其他模块，可离线单测。
        for pure in ("config.py", "detector.py", "diagnostics.py"):
            leaked = self._imports(pure) & (project_modules - {pure[:-3]})
            assert not leaked, f"{pure} 反向依赖了本项目模块：{sorted(leaked)}"

        # tray 通过 attach_* 注入 cleaner / listener，不直接 import。
        assert "cleaner" not in self._imports("tray.py")
        assert "clipboard_listener" not in self._imports("tray.py")
        # 各模块不反向 import 上层。
        assert "tray" not in self._imports("cleaner.py")
        assert "tray" not in self._imports("notifier.py")
        # clipboard_io 不许反向 import clipboard_listener。
        assert "clipboard_listener" not in self._imports("clipboard_io.py")

    def test_client_has_no_private_key_material(self):
        """客户端全部模块不得含任何私钥材料。

        本项目现在**完全没有密钥**（授权与发码体系已整体移除），所以这条守的是未来：
        若有人重新引入签名/加密，必须确保私钥永远留在 ``tools/`` 并被 .gitignore 挡住，
        客户端那一侧只进公钥（最好什么都不进）。
        """
        for name in CLIENT_MODULES:
            src = _read_source(name)
            assert "BEGIN PRIVATE KEY" not in src, f"{name} 含 PEM 私钥材料"
            assert "BEGIN OPENSSH PRIVATE KEY" not in src, f"{name} 含 OpenSSH 私钥材料"
            assert "load_pem_private_key" not in src, f"{name} 在读 PEM 私钥"
            assert "private_key.pem" not in src, f"{name} 引用了私钥文件"
            imported = [item.split(".")[0] for item in re.findall(r"^\s*(?:import|from)\s+([\w\.]+)", src, re.M)]
            assert "tools" not in imported, f"{name} 疑似 import tools/"

    def test_gitignore_still_blocks_key_material(self):
        """`.gitignore` 必须继续挡住密钥材料 —— 一次 `git add .` 就可能泄漏。"""
        gitignore = _read_source(".gitignore")
        assert "*.pem" in gitignore or "private_key.pem" in gitignore
        assert "*.key" in gitignore

    def test_fp_benchmark_runs(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(_PROJECT_ROOT, "tools", "fp_benchmark.py")],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"fp_benchmark 运行失败：{proc.stderr}"


# =========================================================================== #
# I. 工程师自报假设/偏离的裁决依据
# =========================================================================== #
class TestIDeviations:
    """I：为工程师自报的偏离提供可复核证据。"""

    def test_deviation4_last_digest_registered_on_suppress(self, monkeypatch):
        # 偏离 #4：抑制消费分支会登记 _last_digest（使紧随其后的同内容事件被折叠）。
        clock = _Clock()
        monkeypatch.setattr(clipboard_listener, "time", types.SimpleNamespace(monotonic=clock))
        clip = _Clipboard()
        monkeypatch.setattr(clipboard_listener, "clipboard_io", clip)
        listener = clipboard_listener.ClipboardListener(lambda _t: None)

        listener.set_text("CLEANED")
        clip.queue.append("CLEANED")
        listener._handle_update()
        assert listener._last_digest == clipboard_listener._digest("CLEANED")

    def test_deviation3_undo_item_conditional(self):
        # 偏离 #3：「撤销上次清洗」按需出现，默认态与 PRD §6 mock 一致（见 H 用例）。
        import tray

        src = _read_source("tray.py")
        assert "has_undo()" in src

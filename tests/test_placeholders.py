"""面向用户的占位常量检查，以及「收费解锁不得复活」的反回归守卫。

本文件守两件事：

1. **占位符**：保证「带着占位值发布」不会静默发生 —— 启动时会在控制台打印提醒
   （见 `main.py`），本文件在测试阶段也会失败；
2. **反回归**：本项目 2026-10-07 起全开源、全部功能免费，对外**只保留一个纯自愿的
   赞助入口**。下面那组用例把「不许把收费解锁加回来」钉死 —— 包括常量名、
   商品页链接、以及任何对外公开的联系邮箱。
"""

from __future__ import annotations

import os
import re
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config  # noqa: E402
import tray  # noqa: E402


def _read_source(name: str) -> str:
    """读取项目根下某个文件源码。

    Args:
      name: 相对项目根的文件名。

    Returns:
      文件内容。
    """
    with open(os.path.join(_PROJECT_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------- #
# 占位符机制
# --------------------------------------------------------------------------- #
def test_is_placeholder_detection():
    assert tray.is_placeholder("（待填：你的仓库地址）") is True
    assert tray.is_placeholder("https://afdian.com/a/<你的爱发电用户名>") is True
    assert tray.is_placeholder("https://github.com/YOUR_GITHUB_USERNAME/pasteping") is True
    assert tray.is_placeholder("爱发电") is False
    assert tray.is_placeholder("https://afdian.com/a/pasteping") is False


def test_support_platform_name_is_filled():
    """平台名已确定为「爱发电」，不得回落为占位。"""
    assert config.SUPPORT_PLATFORM_NAME == "爱发电"
    assert tray.is_placeholder(config.SUPPORT_PLATFORM_NAME) is False


def test_pending_placeholders_reports_only_known_gaps():
    """待填清单必须精确反映真实状态 —— 填好一项就少一项。

    2026-10-07：面向用户的常量已**全部填毕**，故此处收紧为「必须为空列表」。
    本用例是这条链路上唯一的前置告警：**新增占位项但忘了填**时会立刻变红，
    发布前若有占位符残留也会在这里被拦下。

    （此前只允许 ``tray.ISSUES_URL`` 待填；该缺口已于 2026-10-07 填上真实仓库地址。
    之所以要收紧：单个链接类用例遇到占位符是 ``skip`` 而非 ``fail``，
    等于给「把地址改回占位符」留了一条静默通过的路，这里补上硬闸。）
    """
    pending = tray.pending_placeholders()
    assert all(isinstance(item, str) for item in pending)
    assert pending == [], f"仍有未填写的面向用户常量：{pending}"


def test_pending_placeholders_returns_list_of_strings():
    pending = tray.pending_placeholders()
    assert isinstance(pending, list)
    assert len(pending) <= 3


# --------------------------------------------------------------------------- #
# 对外链接：只有一个赞助入口 + 一个 Issue 入口
# --------------------------------------------------------------------------- #
# 爱发电的两个域名（afdian.com 主域名 / ifdian.net 备用域名）。
_AFDIAN_HOSTS = ("https://afdian.com/", "https://ifdian.net/")


def test_support_url_points_to_afdian_and_is_a_creator_homepage():
    """赞助入口必须落在爱发电域名下、且指向创作者主页。

    指向主页而非商品页是有意的：本项目已无任何可售商品，点进去只应看到「发电」。
    """
    value = tray.SUPPORT_URL
    if tray.is_placeholder(value):
        pytest.skip(f"SUPPORT_URL 仍是占位值，尚未填写：{value!r}")
    assert value.startswith(_AFDIAN_HOSTS), f"SUPPORT_URL = {value} 不是爱发电链接"
    assert re.search(r"/a/[^/?#]+", value), (
        f"SUPPORT_URL = {value} 不像创作者主页；正确格式 https://afdian.com/a/<用户名>"
    )
    assert "/item" not in value, f"SUPPORT_URL = {value} 指向商品页；本项目没有任何商品"


def test_issues_url_points_to_github_issues():
    """反馈入口必须是 GitHub Issues 地址 —— 本项目不公开任何联系邮箱。

    2026-10-07：仓库地址已填入真实用户名，故**不再 skip**，直接硬断言。
    （此前遇到占位符就 ``pytest.skip``，会让「地址被改回占位符」静默通过；
    对公开仓库而言，一个未替换的地址就是坏构件，应当失败而非跳过。）
    """
    value = tray.ISSUES_URL
    assert not tray.is_placeholder(value), f"ISSUES_URL 仍是占位值：{value!r}"
    assert re.match(r"https://github\.com/[^/]+/[^/]+/issues/?$", value), (
        f"ISSUES_URL = {value} 不是 GitHub Issues 地址"
    )


# --------------------------------------------------------------------------- #
# 反回归：收费解锁不得复活
# --------------------------------------------------------------------------- #
# 已彻底移除的常量名（任何一个复活都意味着付费墙又回来了）。
_GONE_CONSTANTS = (
    "UNLOCK_PRICE_CNY",
    "UNLOCK_PRICE_LABEL",
    "PURCHASE_PLATFORM_NAME",
    "LICENSE_STORE_PATH",
    "LICENSE_BIND_MACHINE",
    "SELF_USE_SERIAL_FLOOR",
    "AUTHOR_SERIAL",
    "SUPPORT_CONTACT",
)


def test_paywall_constants_are_gone_from_config():
    """config 里不得再有任何购买 / 解锁 / 定价 / 联系邮箱常量。"""
    for name in _GONE_CONSTANTS:
        assert not hasattr(config, name), f"config.{name} 不应存在（收费解锁相关）"


def test_paywall_identifiers_absent_from_client_code():
    """**纯代码视图**里不得出现任何解锁标识符。

    只看可执行代码：docstring 与注释中说明「这段历史已移除」是必要且有价值的，
    不能被误伤。
    """
    code_only = "\n".join(
        line.split("#", 1)[0] for line in _read_source("tray.py").splitlines()
    )
    code_only = "".join(code_only.split('"""')[::2])
    for token in ("UNLOCK_URL", "get_unlocked", "licensing", "ui_dialog", "SUPPORT_CONTACT"):
        assert token not in code_only, f"tray.py 的可执行代码里仍引用 {token}"


def test_no_goods_page_link_anywhere():
    """会发布的文件里不得再出现爱发电**商品页**链接（``/item/``）—— 它意味着有东西在卖。

    英文 README 与中文 README 是同一份对外文案，必须一并纳入守卫 ——
    **新增对外文案文件时必须同步加进本列表**，否则守卫覆盖不到它。
    """
    for name in ("tray.py", "config.py", "README.md", "README.en.md", "PRIVACY.md"):
        src = _read_source(name)
        assert "afdian.com/item" not in src and "plan_id=" not in src, (
            f"{name} 仍含爱发电商品页链接；本项目已无可售商品"
        )


def test_no_email_is_published():
    """会发布的文件里不得硬编码真实邮箱 —— 2026-10-07 起反馈一律走 GitHub Issues。

    允许 ``noreply`` / 示例域名，因为它们是平台自有或占位地址，不代表可联系的真实身份。
    """
    allowed = ("noreply", "example.com")
    for name in ("config.py", "tray.py", "main.py", "README.md", "README.en.md",
                 "SECURITY.md", "PRIVACY.md"):
        src = _read_source(name)
        for found in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", src):
            if any(token in found for token in allowed):
                continue
            raise AssertionError(f"{name} 里出现真实邮箱 {found!r}；请改用 GitHub Issues")


def test_support_price_is_a_voluntary_amount():
    """赞助金额只用于菜单文案，必须是个正整数（不得出现 0 或负数）。"""
    assert isinstance(config.SUPPORT_PRICE_CNY, int)
    assert config.SUPPORT_PRICE_CNY > 0

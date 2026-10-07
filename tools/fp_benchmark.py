#!/usr/bin/env python
"""PastePing 误报率基准（报告型工具，**不是** pytest 断言）。

把「常见办公 / 日常剪贴板文本是否被误报」固化成可重复运行的资产，
供用户与后续迭代持续观测。本工具**只读取**结果、不改判分逻辑，
也绝不修改任何源码。

设计立场：本产品核心成功标准是「不误报」，因此样本集刻意偏向
真实办公场景中**高频出现、却不应触发提醒**的短语（寒暄、免责声明、
引用块、破折号、常规业务表达、含 16+ 连续数字的业务 token）。

仅依赖标准库 + 项目内纯逻辑模块 ``detector``；不触碰 GUI / 剪贴板 / 网络。

运行：
    python tools/fp_benchmark.py
"""

from __future__ import annotations

import os
import sys
import unicodedata
from collections import Counter
from typing import List, Tuple

# 保证无论从哪个工作目录运行，都能 import 到项目根目录下的模块。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import detector  # noqa: E402

Sample = Tuple[str, str]  # (分组, 文本)

# --------------------------------------------------------------------------- #
# 期望「静默」的干净样本（产品预期：不应提醒）。刻意覆盖真实办公高频场景。
# --------------------------------------------------------------------------- #
SILENT_SAMPLES: List[Sample] = [
    # 正常会议纪要 / 周报
    ("会议纪要", "会议纪要：本次例会明确了三点安排，各部门按计划推进即可。"),
    ("会议纪要", "今天下午三点的评审记得带笔记本电脑，会议室在 5 楼。"),
    ("周报", "周报：本周完成接口联调，下周进入集成测试阶段。"),
    ("周报", "本周 KPI 完成率 92%，详见附件。"),
    # 邮件 / 正文
    ("邮件正文", "附件是本季度的销售数据，请查收后同步给团队。"),
    ("邮件正文", "以上是本次迭代的需求清单，如有疑问随时找我。"),
    ("邮件正文", "大家看一下需求文档第 3 节，下午同步进度。"),
    ("邮件正文", "已按你的要求更新了文档，请查看。"),
    # 群聊寒暄
    ("群聊寒暄", "没问题，我稍后处理这个工单。"),
    ("群聊寒暄", "好的我知道了，这就去办。"),
    ("群聊寒暄", "你想吃什么？需要我帮你带饭吗？"),
    ("群聊寒暄", "收到，辛苦啦，早点休息。"),
    ("群聊寒暄", "抱歉刚看到消息，现在回复你。"),
    ("群聊寒暄", "午饭订 12 点，老地方见。"),
    # 通知公告
    ("通知公告", "【公告】系统将于本周六凌晨进行维护，请提前保存工作。"),
    ("通知公告", "【通知】请各部门于周五前提交月度总结。"),
    # 合规免责声明（含“仅供参考”）
    ("免责声明", "本报告数据仅供参考，不构成任何投资建议。"),
    ("免责声明", "以上内容仅供参考，最终以现场实际情况为准。"),
    # 正常业务批注用语
    ("正常批注", "注：本表格数据截至 2024 年第三季度。"),
    ("正常批注", "备注：请提前十分钟到达会议室。"),
    # 邮件引用 / Markdown 引用（行首 >）
    ("引用块", "> 以下为上一封邮件的引用内容，请参考。"),
    ("引用块", "> 原始邮件内容如下，请相关部门知悉。"),
    # 中文破折号（——）的正常用法
    ("破折号", "本次合作意向已初步达成 —— 后续细节另行沟通。"),
    ("破折号", "行程安排：北京——上海——杭州，三天两晚。"),
    # 正常业务表达（请确认 / 请审阅）
    ("业务表达", "请确认您已阅读并同意用户协议。"),
    ("业务表达", "请张三确认后提交至审批流程。"),
    ("业务表达", "请审阅后回复意见，谢谢配合。"),
    # 含 16+ 连续数字的业务 token（订单号 / 流水号 / 会话 id / 时间戳拼接）
    ("业务token", "订单号 202409181234567890 请核对。"),
    ("业务token", "流水号 8888160012345678 已生成。"),
    ("业务token", "会话 ID 5500123456789012 已建立。"),
    ("业务token", "时间戳拼接串 1695000000123456 请忽略。"),
    ("业务token", "批次号 3210987654321098765 已入库。"),
    # 日常其他
    ("日常", "发票抬头请开公司全称，税号稍后发你。"),
    ("日常", "这段代码我已经 review 过了，可以合并。"),
    ("日常", "明天记得带身份证复印件，谢谢。"),
    ("日常", "这次活动预算大约三万，超支需提前报备。"),
]

# --------------------------------------------------------------------------- #
# 期望「命中」的正样本（产品预期：应当提醒）。三类规则均覆盖关键词版与模式版。
# --------------------------------------------------------------------------- #
ALERT_SAMPLES: List[Sample] = [
    # 第一类 内部批注
    ("内部批注·关键词", "转发文案：请在群内统一发布新品。"),
    ("内部批注·关键词", "内部资料，请勿外传。"),
    ("内部批注·关键词", "本文件仅供内部参考，谢谢配合。"),
    ("内部批注·模式", "【内部批注】这段不要对外发送。"),
    ("内部批注·模式", "> 这是引用过来的内部批注，请勿外传。"),
    ("内部批注·模式", "价格待定 —— 待财务确认后再对外公布。"),
    # 第二类 AI 残留
    ("AI残留·开头", "当然可以，下面是为你整理的方案："),
    ("AI残留·开头", "以下是整理好的内容，供你参考。"),
    ("AI残留·开头", "好的我马上帮你处理这个问题。"),
    ("AI残留·结尾", "项目的结论是这样。" + "补充说明" * 60 + "希望对你有帮助"),
    ("AI残留·结尾", "这里是一些资料。" + "参考内容" * 60 + "你可以随时告诉我"),
    # 第三类 敏感信息
    ("敏感·身份证", "身份证号 11010519491231002X 请核对。"),
    ("敏感·手机号", "联系电话 13800138000 谢谢。"),
    ("敏感·APIKey", "key = sk-abcdefgh12345678"),
    ("敏感·APIKey", "token ghp_abcdefghijklmnopqrstuvwxyz0123"),
    ("敏感·银行卡", "卡号 6222021234567890 请转账。"),
]


# --------------------------------------------------------------------------- #
# 终端对齐辅助（中日韩全角字符按 2 列宽计算）。
# --------------------------------------------------------------------------- #
def _char_width(ch: str) -> int:
    """返回单个字符的显示宽度。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    """返回字符串的终端显示宽度。"""
    return sum(_char_width(ch) for ch in text)


def _clip(text: str, width: int) -> str:
    """按显示宽度截断字符串，超长时追加省略号。"""
    out: List[str] = []
    used = 0
    for ch in text:
        w = _char_width(ch)
        if used + w > width - 1:
            out.append("…")
            break
        out.append(ch)
        used += w
    return "".join(out)


def _pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格。"""
    return text + " " * max(0, width - _display_width(text))


# --------------------------------------------------------------------------- #
# 评测核心。
# --------------------------------------------------------------------------- #
def _evaluate(samples: List[Sample], expected: str) -> List[dict]:
    """对一组样本执行 detect，并给出 TP/FP/FN/TN 判定。

    Args:
      samples: (分组, 文本) 列表。
      expected: ``"alert"`` 或 ``"silent"``。

    Returns:
      每行结果字典列表。
    """
    rows: List[dict] = []
    for group, text in samples:
        hit = detector.detect(text)
        actual = "alert" if hit is not None else "silent"
        if expected == "alert":
            verdict = "TP" if actual == "alert" else "FN"
        else:
            verdict = "FP" if actual == "alert" else "TN"
        rows.append(
            {
                "group": group,
                "text": text,
                "expected": expected,
                "actual": actual,
                "rule": hit.rule if hit is not None else "-",
                "category": hit.category if hit is not None else "-",
                "verdict": verdict,
            }
        )
    return rows


def _print_table(rows: List[dict]) -> None:
    """打印「样本 | 期望 | 实际 | 规则 | 判定」表格。"""
    headers = ["样本", "期望", "实际", "规则", "判定"]
    widths = [40, 6, 6, 22, 4]
    header_line = " | ".join(_pad(h, w) for h, w in zip(headers, widths))
    print(header_line)
    print("-" * _display_width(header_line))
    for row in rows:
        cells = [
            _pad(_clip(row["text"], widths[0]), widths[0]),
            _pad(row["expected"], widths[1]),
            _pad(row["actual"], widths[2]),
            _pad(_clip(row["rule"], widths[3]), widths[3]),
            _pad(row["verdict"], widths[4]),
        ]
        print(" | ".join(cells))


def main() -> int:
    """运行基准并打印报告。"""
    silent_rows = _evaluate(SILENT_SAMPLES, "silent")
    alert_rows = _evaluate(ALERT_SAMPLES, "alert")
    all_rows = silent_rows + alert_rows

    total_tn = sum(r["verdict"] == "TN" for r in silent_rows)
    total_fp = sum(r["verdict"] == "FP" for r in silent_rows)
    total_tp = sum(r["verdict"] == "TP" for r in alert_rows)
    total_fn = sum(r["verdict"] == "FN" for r in alert_rows)

    silent_total = len(silent_rows)
    alert_total = len(alert_rows)

    print("=" * 100)
    print("PastePing 误报率基准 (MVP) —— 正类 = alert（应当提醒）")
    print("=" * 100)
    print("\n[1] 样本明细\n")
    _print_table(all_rows)

    print("\n[2] 汇总\n")
    print(f"  样本总数      : {len(all_rows)}  (干净 {silent_total} / 应报 {alert_total})")
    print(f"  TP(正报对)    : {total_tp}")
    print(f"  FP(误报)      : {total_fp}")
    print(f"  FN(漏报)      : {total_fn}")
    print(f"  TN(静默对)    : {total_tn}")
    fp_rate = total_fp / silent_total if silent_total else 0.0
    fn_rate = total_fn / alert_total if alert_total else 0.0
    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    print(f"  总误报率(FPR) : {fp_rate:.1%}  ({total_fp}/{silent_total})")
    print(f"  总漏报率(FNR) : {fn_rate:.1%}  ({total_fn}/{alert_total})")
    print(f"  精确率        : {precision:.1%}")
    print(f"  召回率        : {recall:.1%}")
    print(f"  F1            : {f1:.3f}")

    print("\n[3] 按规则分组的误报统计（仅统计 FP 样本）\n")
    fp_by_rule = Counter(r["rule"] for r in silent_rows if r["verdict"] == "FP")
    if not fp_by_rule:
        print("  （无）")
    else:
        print(f"  {'规则':24s} {'误报数':>6s} {'占干净样本':>10s}")
        for rule, count in fp_by_rule.most_common():
            print(f"  {_pad(rule, 24)} {count:>6d} {count / silent_total:>9.1%}")

    print("\n[4] 疑似误报样本清单（KNOWN_FALSE_POSITIVE）\n")
    fp_rows = [r for r in silent_rows if r["verdict"] == "FP"]
    if not fp_rows:
        print("  （无）")
    else:
        for row in fp_rows:
            print(f"  - [{row['group']}] {row['text']}")
            print(f"      触发规则: {row['rule']}  (类别: {row['category']})")

    print("\n[5] 漏报样本清单（KNOWN_FALSE_NEGATIVE）\n")
    fn_rows = [r for r in alert_rows if r["verdict"] == "FN"]
    if not fn_rows:
        print("  （无）")
    else:
        for row in fn_rows:
            print(f"  - [{row['group']}] {row['text']}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""PastePing 降误报杠杆实验台（报告型工具，**不是** pytest 断言）。

对 ``detector.TIGHTEN`` 的 5 个降误报杠杆做**全组合穷举**（2^5 = 32 组），
在 ``tools/fp_benchmark.py`` 的 52 条基准样本上逐一实测，输出：

* 每个杠杆的**单独**边际效果（相对「全关」基线）；
* 全部 32 组组合的 FPR / FNR / 精确率 / 召回率；
* **Pareto 前沿**（不存在同时在误报与漏报上都不劣于它的组合）；
* 满足目标（FPR ≤ 25% 且 召回 ≥ 95%）的**杠杆数最少**的组合。

本工具**只读不写**：只改内存中的 ``detector.TIGHTEN`` 字典，跑完恢复原值，
不修改任何源码文件、不落盘。

运行::

    python tools/fp_lab.py
"""

from __future__ import annotations

import itertools
import os
import sys
from typing import Dict, List, Sequence, Tuple

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS_DIR)
for _path in (_ROOT, _TOOLS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import detector  # noqa: E402
import fp_benchmark  # noqa: E402

# 目标口径（与最终采用的「稳健档」一致）。
TARGET_FPR = 0.25
TARGET_RECALL = 0.95

# 杠杆顺序（用于组合枚举与显示）。
LEVERS: Tuple[str, ...] = (
    "ai_boundary",
    "disclaimer",
    "structure",
    "bracket",
    "bankcard",
    "low_signal_keywords",
)

# 杠杆中文说明。
LEVER_LABELS: Dict[str, str] = {
    "ai_boundary": "AI 特征贴身首尾",
    "disclaimer": "「仅供参考」需语义词",
    "structure": "引用块/破折号需语义词",
    "bracket": "「【】」需语义词",
    "bankcard": "银行卡 Luhn/上下文",
    "low_signal_keywords": "低区分度关键词需语义词",
}


def _measure(combo: Sequence[str]) -> dict:
    """在指定杠杆组合下评测全部样本。

    Args:
      combo: 需要**开启**的杠杆名序列。

    Returns:
      指标字典（fpr / fnr / precision / recall / fn_samples）。
    """
    for lever in LEVERS:
        detector.TIGHTEN[lever] = lever in combo

    silent_rows = fp_benchmark._evaluate(fp_benchmark.SILENT_SAMPLES, "silent")
    alert_rows = fp_benchmark._evaluate(fp_benchmark.ALERT_SAMPLES, "alert")

    fp = sum(r["verdict"] == "FP" for r in silent_rows)
    tn = sum(r["verdict"] == "TN" for r in silent_rows)
    tp = sum(r["verdict"] == "TP" for r in alert_rows)
    fn = sum(r["verdict"] == "FN" for r in alert_rows)

    silent_total = len(silent_rows)
    alert_total = len(alert_rows)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {
        "fpr": fp / silent_total if silent_total else 0.0,
        "fnr": fn / alert_total if alert_total else 0.0,
        "precision": precision,
        "recall": recall,
        "fp": fp,
        "tn": tn,
        "tp": tp,
        "fn": fn,
        "fp_samples": [r for r in silent_rows if r["verdict"] == "FP"],
        "fn_samples": [r for r in alert_rows if r["verdict"] == "FN"],
    }


def _restore(original: Dict[str, bool]) -> None:
    """恢复 ``detector.TIGHTEN`` 的原始取值。

    Args:
      original: 原始取值副本。
    """
    detector.TIGHTEN.update(original)


def _fmt_combo(combo: Sequence[str]) -> str:
    """把组合格式化为可读短标签。

    Args:
      combo: 杠杆名序列。

    Returns:
      形如 ``ai+disc`` 的短标签；空组合返回 ``(全关)``。
    """
    if not combo:
        return "(全关)"
    short = {
        "ai_boundary": "ai",
        "disclaimer": "disc",
        "structure": "struct",
        "bracket": "brkt",
        "bankcard": "bank",
        "low_signal_keywords": "kw",
    }
    return "+".join(short[lever] for lever in combo)


def main() -> int:
    """运行全组合实验并打印报告。

    Returns:
      进程退出码。
    """
    original = dict(detector.TIGHTEN)
    try:
        results: List[Tuple[Tuple[str, ...], dict]] = []
        for size in range(len(LEVERS) + 1):
            for combo in itertools.combinations(LEVERS, size):
                results.append((combo, _measure(combo)))
        baseline = results[0][1]

        print("=" * 100)
        print("PastePing 降误报杠杆实验台 —— 基准集 52 条（干净 36 / 应报 16）")
        print("=" * 100)

        # [1] 单杠杆边际效果
        print("\n[1] 单杠杆边际效果（相对「全关」基线）\n")
        print(f"  {'杠杆':26s} {'FPR':>8s} {'误报数':>7s} {'召回':>8s} {'漏报数':>7s} {'Δ误报':>7s}")
        print(f"  {'（基线：全关）':24s} {baseline['fpr']:>7.1%} {baseline['fp']:>7d} "
              f"{baseline['recall']:>7.1%} {baseline['fn']:>7d} {'—':>7s}")
        for combo, data in results:
            if len(combo) != 1:
                continue
            label = LEVER_LABELS[combo[0]]
            delta = data["fp"] - baseline["fp"]
            print(f"  {label:26s} {data['fpr']:>7.1%} {data['fp']:>7d} "
                  f"{data['recall']:>7.1%} {data['fn']:>7d} {delta:>+7d}")

        # [2] 全部组合（按 FPR 升序，其次按杠杆数升序）
        print("\n[2] 全部 32 组组合（按 FPR 升序）\n")
        print(f"  {'组合':18s} {'杠杆数':>5s} {'FPR':>7s} {'FNR':>7s} {'精确率':>7s} {'召回率':>7s} {'F1':>6s}")
        ranked = sorted(results, key=lambda item: (item[1]["fpr"], len(item[0])))
        for combo, data in ranked:
            precision, recall = data["precision"], data["recall"]
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
            print(f"  {_fmt_combo(combo):18s} {len(combo):>5d} {data['fpr']:>6.1%} "
                  f"{data['fnr']:>6.1%} {precision:>6.1%} {recall:>6.1%} {f1:>6.3f}")

        # [3] Pareto 前沿：不存在 FPR 与 FN 数都不劣于它的组合
        front = [
            (combo, data)
            for combo, data in results
            if not any(
                other["fpr"] <= data["fpr"]
                and other["fn"] <= data["fn"]
                and (other["fpr"] < data["fpr"] or other["fn"] < data["fn"])
                for _, other in results
            )
        ]
        print("\n[3] Pareto 前沿（误报与漏报不可同时改进的组合）\n")
        print(f"  {'组合':18s} {'FPR':>7s} {'漏报数':>7s} {'召回率':>7s}  说明")
        for combo, data in sorted(front, key=lambda item: (item[1]["fpr"], item[1]["fn"])):
            note = "、".join(LEVER_LABELS[lever] for lever in combo) or "基线（不降误报）"
            print(f"  {_fmt_combo(combo):18s} {data['fpr']:>6.1%} {data['fn']:>7d} "
                  f"{data['recall']:>6.1%}  {note}")

        # [4] 满足目标的最小成本组合
        print(f"\n[4] 达标组合（FPR ≤ {TARGET_FPR:.0%} 且 召回 ≥ {TARGET_RECALL:.0%}），按杠杆数升序\n")
        feasible = [
            (combo, data)
            for combo, data in results
            if data["fpr"] <= TARGET_FPR and data["recall"] >= TARGET_RECALL
        ]
        if not feasible:
            print("  （无组合达标）")
        else:
            for combo, data in sorted(feasible, key=lambda item: (len(item[0]), item[1]["fpr"]))[:6]:
                note = "、".join(LEVER_LABELS[lever] for lever in combo) or "无（基线即达标）"
                print(f"  {_fmt_combo(combo):18s} 杠杆数 {len(combo)}  FPR {data['fpr']:>6.1%}  "
                      f"召回 {data['recall']:>6.1%}  ← {note}")

        # [5] 深度：达标组合的漏报明细（如有）
        best_combo, best_data = min(feasible, key=lambda item: (len(item[0]), item[1]["fpr"])) \
            if feasible else (results[0][0], baseline)
        print(f"\n[5] 推荐组合 {_fmt_combo(best_combo)} 的明细\n")
        print(f"  FPR {best_data['fpr']:.1%}（{best_data['fp']}/36）  召回 {best_data['recall']:.1%}  "
              f"精确率 {best_data['precision']:.1%}")
        if best_data["fn_samples"]:
            print("  漏报样本：")
            for row in best_data["fn_samples"]:
                print(f"    - [{row['group']}] {row['text']}")
        else:
            print("  漏报样本：（无）")
        if best_data["fp_samples"]:
            print("  仍存在的误报样本：")
            for row in best_data["fp_samples"]:
                print(f"    - [{row['group']}] {row['text']}  ← 触发规则 {row['rule']}")
        else:
            print("  仍存在的误报样本：（无）")

        print()
        return 0
    finally:
        _restore(original)


if __name__ == "__main__":
    sys.exit(main())

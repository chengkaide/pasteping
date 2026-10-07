"""检测样例集必须与检测器的**真实行为**一致（行为契约）。

``tools/gen_test_samples.py`` 内置 34 条带预期标签的样例，其中 13 条是
「**不该提醒**」的误报防线。这些样例是给用户复制测试用的，同时也是一份契约：
检测器一旦被改动导致某条样例行为变化（例如降误报门槛被调松、正则被改写），
这里立刻失败。

没有这层守护，「样例集」就只是一堆**没人验证过的文本** —— 用户照着测却发现
结果与文档不符，比没有样例更糟。
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import detector  # noqa: E402


def _load_tool():
    """把 ``tools/gen_test_samples.py`` 当模块加载（它不是包的一部分）。

    Returns:
      模块对象。
    """
    path = os.path.join(_PROJECT_ROOT, "tools", "gen_test_samples.py")
    spec = importlib.util.spec_from_file_location("pasteping_gen_test_samples", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


def _actual(text: str):
    """返回 ``detector.detect()`` 的可比较结果。

    Args:
      text: 待检测文本。

    Returns:
      命中时 ``(category, rule)``；未命中 None。
    """
    hit = detector.detect(text)
    return None if hit is None else (hit.category, hit.rule)


@pytest.mark.parametrize("sample", TOOL.SAMPLES, ids=[s["id"] for s in TOOL.SAMPLES])
def test_sample_matches_expectation(sample):
    """每条样例的实测结果必须与标注的预期完全一致。"""
    actual = _actual(sample["text"])
    assert actual == sample["expect"], (
        f"{sample['id']}（{sample['title']}）：预期 {sample['expect']}，实测 {actual}"
    )


def test_verify_reports_no_mismatch():
    """生成器的自检函数必须报「零不一致」。"""
    results, mismatches = TOOL.verify()
    assert len(results) == len(TOOL.SAMPLES)
    assert not mismatches, f"存在不一致样例：{[m['sample']['id'] for m in mismatches]}"


def test_every_sample_has_an_explanation():
    """每条样例都必须写明「为什么会（不）触发」—— 这是交付文档的正文，不能空着。"""
    for sample in TOOL.SAMPLES:
        assert sample["why"].strip(), f"{sample['id']} 缺少 why 说明"
        assert sample["title"].strip()
        assert sample["group"].strip()


def test_sample_ids_are_unique():
    """ID 唯一，否则文档表格与 --clip 参数都会歧义。"""
    ids = [sample["id"] for sample in TOOL.SAMPLES]
    assert len(ids) == len(set(ids))


def test_corpus_covers_all_three_categories():
    """三类检测都必须有正例，否则样例集覆盖不全。"""
    covered = {expect[0] for expect in (s["expect"] for s in TOOL.SAMPLES) if expect}
    assert detector.CATEGORY_SENSITIVE in covered
    assert detector.CATEGORY_INTERNAL_NOTE in covered
    assert detector.CATEGORY_AI_RESIDUE in covered


def test_corpus_has_enough_negative_controls():
    """误报防线不能少于正例的一半 —— 否则「不打扰」这条标准就没被验证。"""
    positives = [s for s in TOOL.SAMPLES if s["expect"] is not None]
    negatives = [s for s in TOOL.SAMPLES if s["expect"] is None]
    assert len(negatives) >= 8, f"负例只有 {len(negatives)} 条，覆盖不足"
    assert len(negatives) * 2 >= len(positives), "负例相对正例过少"


def test_expectations_only_use_known_categories():
    """预期里只允许出现已知类别，防止写错类别枚举值还被「一致」放过。"""
    known = {
        detector.CATEGORY_SENSITIVE,
        detector.CATEGORY_INTERNAL_NOTE,
        detector.CATEGORY_AI_RESIDUE,
    }
    for sample in TOOL.SAMPLES:
        if sample["expect"] is not None:
            assert sample["expect"][0] in known, f"{sample['id']} 类别非法"


def test_doc_is_generated_and_covers_every_sample():
    """交付文档必须已生成，且包含每一条样例 —— 防止改了样例却忘了重跑生成器。"""
    doc = os.path.join(_PROJECT_ROOT, "docs", "test-samples.md")
    assert os.path.exists(doc), "docs/test-samples.md 不存在，请运行 tools/gen_test_samples.py"
    with open(doc, encoding="utf-8") as handle:
        content = handle.read()
    for sample in TOOL.SAMPLES:
        assert f"### {sample['id']}" in content, f"文档里缺少样例 {sample['id']}"

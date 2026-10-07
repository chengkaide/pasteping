"""生成「带敏感内容的测试段落」并**实跑检测器核对**，输出 docs/test-samples.md。

⚠️ 本工具属于 ``tools/``，**仅开发者本机使用，禁止随客户端分发**。

为什么不能用「照着规则手写几段」了事
------------------------------------

检测器有一层**降误报门槛**（``detector.TIGHTEN``）：同一句「备注：…」，
在「本方案待定…」的上下文里会提醒、在「会议改到周三」的上下文里**故意不提醒**。
所以「这段话会不会触发」不能靠肉眼推，必须**实跑**。

本脚本因此做三件事：

1. 内置一份**带预期标签**的样例集（每条写明「期望命中什么」或「期望不命中」）；
2. 逐条调用真实的 ``detector.detect()`` 并**比对预期**，任何一条对不上就以退出码 1 报错
   —— 这样样例集本身是一份可回归的「行为契约」，而不是一坨无从验证的文本；
3. 把**实测结果**（而不是我声称的结果）写进 ``docs/test-samples.md``。

用法::

    python tools/gen_test_samples.py            # 核对 + 写出文档
    python tools/gen_test_samples.py --check     # 只核对，不写文档
    python tools/gen_test_samples.py --clip ID   # 把某条样例复制到剪贴板（方便真机试）

退出码：0 = 全部样例与预期一致；1 = 有不一致（样例或检测器需要复核）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import detector  # noqa: E402

# 输出文档。
_DOC_PATH = os.path.join(_PROJECT_ROOT, "docs", "test-samples.md")

# 预期结果的类型：(类别, 规则名) 或 None（不提醒）。
Expect = Optional[Tuple[str, str]]

# 长文本填充段，用于验证「首尾 200 字窗口」与「全文扫描」的差别。
_LONG_PREFIX = "矿山开采设计说明书第二节：采场结构参数与回采顺序的安排。" * 8
_LONG_SUFFIX = "以上为第三节内容，具体参数以最终评审稿为准，请勿擅自引用。" * 8


# =========================================================================== #
# 样例集
#
# 每条：id / group / title / text / expect / why
#   expect = (类别枚举值, 规则名) 或 None
#   why    = 为什么它会（或不会）触发 —— 这段文字会原样写进交付文档，供用户理解
# =========================================================================== #
SAMPLES: List[Dict[str, Any]] = [
    # ------------------------------------------------------------------ #
    # 一、敏感信息（法定敏感信息，**全文扫描**，优先级最高）
    # ------------------------------------------------------------------ #
    {
        "id": "S1",
        "group": "敏感信息 · 身份证",
        "title": "入档资料（18 位身份证）",
        "text": "员工入档资料：姓名 李芳，身份证号 320311197803154517，请核对后归档。",
        "expect": ("sensitive", "身份证"),
        "why": "18 位身份证走固定格式正则，全文扫描即命中，不受任何降误报门槛影响。",
    },
    {
        "id": "S2",
        "group": "敏感信息 · 手机号",
        "title": "客户回访安排",
        "text": "客户回访：张伟 13812345678，请本周内联系，反馈结果同步到项目群。",
        "expect": ("sensitive", "手机号"),
        "why": "11 位手机号（1 开头、第二位 3–9）自动命中，且要求前后不是数字，避免从长号码里切出一段。",
    },
    {
        "id": "S3",
        "group": "敏感信息 · API Key",
        "title": "接口调试代码（sk- 开头）",
        "text": (
            "接口调试片段：\n"
            "client = OpenAI(api_key=\"sk-abc123XYZ456def789ghi\")\n"
            "这段代码不要提交到公共仓库。"
        ),
        "expect": ("sensitive", "API Key"),
        "why": "识别 sk- / ghp_ / gho_ / AKIA / xoxb- / xoxp- 六类常见密钥前缀；提醒时只保留前缀与尾 2 位。",
    },
    {
        "id": "S4",
        "group": "敏感信息 · API Key",
        "title": "云服务配置（AKIA 开头）",
        "text": "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n请把这段配置从工单里删掉。",
        "expect": ("sensitive", "API Key"),
        "why": "AKIA 是 AWS 访问密钥前缀（此处用的是官方文档的示例值，非真实密钥）。",
    },
    {
        "id": "S5",
        "group": "敏感信息 · 银行卡（Luhn 校验通过）",
        "title": "联调用的公开测试卡号",
        "text": "这份联调数据里的 4111111111111111 是公开测试号，可以随便用。",
        "expect": ("sensitive", "银行卡"),
        "why": "附近**没有**任何「卡 / 银行 / 账户」这类上下文词，仍被命中 —— 因为该号码通过了 Luhn 校验。",
    },
    {
        "id": "S6",
        "group": "敏感信息 · 银行卡（靠上下文词命中）",
        "title": "绑卡提示",
        "text": "银行卡 6222021234567890 已绑定成功，请确认。",
        "expect": ("sensitive", "银行卡"),
        "why": "该号码**通不过** Luhn 校验，但邻近出现「银行卡」这个上下文词，因此仍然命中。",
    },
    {
        "id": "S7",
        "group": "敏感信息 · 长文中段仍然命中",
        "title": "长文档中段的手机号（验证全文扫描）",
        "text": _LONG_PREFIX + "\n对接人电话 13812345678，有疑问直接联系。\n" + _LONG_SUFFIX,
        "expect": ("sensitive", "手机号"),
        "why": "手机号落在第 200 字之后。**敏感信息是全文扫描**，所以照样命中 —— 与 AI 残留不同（见 N8）。",
    },
    # ------------------------------------------------------------------ #
    # 二、内部批注（**只看首尾各 200 字**）
    # ------------------------------------------------------------------ #
    {
        "id": "I1",
        "group": "内部批注 · 固定关键词（无门槛）",
        "title": "内部资料 + 勿外传",
        "text": "这是一份内部资料，请勿外传。\n\n本周采区验收进度：已完成 3 个采区，剩余 1 个待复测。",
        "expect": ("internal_note", "关键词:内部资料"),
        "why": "「内部资料 / 内部使用 / 转发文案 / 勿外传」是固定关键词，**不设共现门槛**，命中即提醒。",
    },
    {
        "id": "I2",
        "group": "内部批注 · 固定关键词（无门槛）",
        "title": "转发文案",
        "text": "转发文案：本次更新的三点注意事项如下……",
        "expect": ("internal_note", "关键词:转发文案"),
        "why": "「转发文案」属固定关键词，同样不设门槛。",
    },
    {
        "id": "I3",
        "group": "内部批注 · 有门槛（会命中）",
        "title": "备注 + 强语义词「待定 / 不对外」",
        "text": "备注：本方案待定，暂不对外发布，等评审通过再同步。",
        "expect": ("internal_note", "关键词:备注："),
        "why": "「备注：」区分度低，**必须同屏出现强语义词**（内部/保密/草案/待定/初稿…）。这里有「待定」「不对外」，故命中。",
    },
    {
        "id": "I4",
        "group": "内部批注 · 有门槛（会命中）",
        "title": "注：+ 强语义词「机密」",
        "text": "注：以下数据属机密，请勿外泄，阅后删除。",
        "expect": ("internal_note", "关键词:注："),
        "why": "「注：」同样需要强语义词共现，「机密」满足条件。",
    },
    {
        "id": "I5",
        "group": "内部批注 · 有门槛（会命中）",
        "title": "仅供参考 + 强语义词「草案」",
        "text": "本稿为草案，以下内容仅供参考，最终以正式版为准。",
        "expect": ("internal_note", "关键词:仅供参考"),
        "why": "「仅供参考」单独出现不再提醒，需同屏有强语义词（此处为「草案」）。",
    },
    {
        "id": "I6",
        "group": "内部批注 · 有门槛（会命中）",
        "title": "请确认 + 强语义词「待确认」",
        "text": "本方案为待确认稿，请尽快确认后回复。",
        "expect": ("internal_note", "关键词:请确认"),
        "why": "「请…确认」需强语义词共现，本例为「待确认」。",
    },
    {
        "id": "I7",
        "group": "内部批注 · 有门槛（会命中）",
        "title": "【方括号】内文含强语义词",
        "text": "【待定】付款方式还需再议，等财务口径统一后再定。",
        "expect": ("internal_note", "模式:【内联批注】"),
        "why": "方括号需**内文**含批注语义词才算命中 —— 内文是「待定」，满足。",
    },
    {
        "id": "I8",
        "group": "内部批注 · 有门槛（会命中）",
        "title": "行首引用块 + 强语义词「初稿」",
        "text": "> 以下为初稿内容，请补充意见。",
        "expect": ("internal_note", "模式:引用块"),
        "why": "行首 `>` 引用块需同屏有批注语义词，本例为「初稿」。",
    },
    {
        "id": "I9",
        "group": "内部批注 · 有门槛（会命中）",
        "title": "破折号补充说明 + 强语义词「保密」",
        "text": "——以下为保密补充说明，阅后即焚。",
        "expect": ("internal_note", "模式:补充说明"),
        "why": "`——` 补充说明需同屏有批注语义词，本例为「保密」。",
    },
    # ------------------------------------------------------------------ #
    # 三、AI 生成残留（**只看首尾各 200 字**）
    # ------------------------------------------------------------------ #
    {
        "id": "A1",
        "group": "AI 残留 · 开头特征",
        "title": "「当然可以」开头",
        "text": "当然可以，我把矿山通风改造的要点整理如下：\n\n一、先做风量测定；二、再核算阻力；三、最后定风机型号。",
        "expect": ("ai_residue", "开头特征:当然可以"),
        "why": "AI 残留开头特征共 4 个：当然可以 / 以下是 / 好的我 / 没问题。落在前 200 字内即命中。",
    },
    {
        "id": "A2",
        "group": "AI 残留 · 开头特征",
        "title": "「好的我」开头",
        "text": "好的我明白了，马上按这个思路继续处理，稍后给你结果。",
        "expect": ("ai_residue", "开头特征:好的我"),
        "why": "「好的我」是典型的对话助手口吻残留在正文开头。",
    },
    {
        "id": "A3",
        "group": "AI 残留 · 结尾特征",
        "title": "「希望对你有帮助」结尾",
        "text": "以上是我整理的采场结构参数对照表。希望对你有帮助。",
        "expect": ("ai_residue", "结尾特征:希望对你有帮助"),
        "why": "结尾特征共 5 个：希望对你有帮助 / 需要我帮你 / 要不要我帮你 / 如果你需要我 / 你可以随时告诉我。",
    },
    {
        "id": "A4",
        "group": "AI 残留 · 结尾特征",
        "title": "「需要我帮你」结尾",
        "text": "方案先按这版推进，细节我下周补。需要我帮你把附件也整理一份吗？",
        "expect": ("ai_residue", "结尾特征:需要我帮你"),
        "why": "同上，属结尾特征；落在末 200 字内即命中。",
    },
    # ------------------------------------------------------------------ #
    # 四、命中优先级
    # ------------------------------------------------------------------ #
    {
        "id": "P1",
        "group": "优先级 · 敏感信息 > 内部批注 > AI 残留",
        "title": "三类同时命中，只报最严重的一条",
        "text": "以下是本周纪要草稿，请勿外传。\n\n对接联系人：王强 13812345678。",
        "expect": ("sensitive", "手机号"),
        "why": "这段同时命中「AI 残留(以下是)」「内部批注(勿外传)」「敏感信息(手机号)」。"
               "提醒优先级为 敏感信息 > 内部批注 > AI 残留，故只报手机号。",
    },
    # ------------------------------------------------------------------ #
    # 五、**不该提醒**的样例（误报防线）
    # ------------------------------------------------------------------ #
    {
        "id": "N1",
        "group": "不该提醒 · 低区分度词无强语义",
        "title": "「仅供参考」但没有语境词",
        "text": "以下内容仅供参考，如有疑问请随时联系我。",
        "expect": None,
        "why": "「仅供参考」单独出现**故意不提醒** —— 这是把误报率从 58.3% 压到 8.3% 的关键取舍。",
    },
    {
        "id": "N2",
        "group": "不该提醒 · 低区分度词无强语义",
        "title": "「备注：」但没有语境词",
        "text": "备注：会议时间改到周三下午三点。",
        "expect": None,
        "why": "日常「备注」绝大多数是普通备忘，需强语义词共现才提醒。",
    },
    {
        "id": "N3",
        "group": "不该提醒 · 低区分度词无强语义",
        "title": "引用块但没有语境词",
        "text": "> 今天的天气不错，适合出门。",
        "expect": None,
        "why": "行首 `>` 只说明是引用，不代表是内部批注。",
    },
    {
        "id": "N4",
        "group": "不该提醒 · 低区分度词无强语义",
        "title": "【方括号】内文无批注语义",
        "text": "【重要】记得带上身份证复印件。",
        "expect": None,
        "why": "「【重要】」这类强调标记很常见，内文不含批注语义词故不提醒；"
               "另外「身份证」只是词，没有号码，不构成敏感信息。",
    },
    {
        "id": "N5",
        "group": "不该提醒 · 低区分度词无强语义",
        "title": "破折号但没有语境词",
        "text": "——明天记得提前十分钟到。",
        "expect": None,
        "why": "`——` 是极常见的行文符号，单独出现不该打扰。",
    },
    {
        "id": "N6",
        "group": "不该提醒 · 普通业务文本",
        "title": "纯技术段落",
        "text": "采空区稳定性分析常用解析法、数值模拟法与现场监测法三类，本次采用数值模拟结合现场监测。",
        "expect": None,
        "why": "普通业务文本，应当完全安静 —— 这是「不误报打扰」这条成功标准的基线用例。",
    },
    {
        "id": "N7",
        "group": "不该提醒 · 数字形态不满足",
        "title": "手机号被空格分段",
        "text": "客户电话记成了 138 1234 5678 这种分段写法，中间有空格。",
        "expect": None,
        "why": "手机号要求 **11 位连续数字**，被空格拆开后不命中。"
               "（如果你常遇到这种情况，值得反馈 —— 属于已知的漏报形态。）",
    },
    {
        "id": "N8",
        "group": "不该提醒 · 超出扫描窗口",
        "title": "AI 特征出现在正文中段",
        "text": _LONG_PREFIX + "\n当然可以。\n" + _LONG_SUFFIX,
        "expect": None,
        "why": "**AI 残留与内部批注只看文本首尾各 200 字**（产品规格如此），"
               "「当然可以」落在第 200 字之后，故不提醒。"
               "注意与 S7 对比：敏感信息是**全文扫描**，中段也会命中。",
    },
    {
        "id": "N9",
        "group": "不该提醒 · 数字形态不满足",
        "title": "16 位订单号（非卡号）",
        "text": "订单号 1234567890123456 已发货，物流单号稍后同步。",
        "expect": None,
        "why": "16 位数字既通不过 Luhn 校验，附近也没有「卡 / 银行 / 账户」类上下文词，"
               "故判定为订单号而非卡号 —— 这正是银行卡杠杆要拦的误报。",
    },
    {
        "id": "N10",
        "group": "不该提醒 · 全角数字",
        "title": "全角数字写的身份证号",
        "text": "身份证号：１１０１０５１９４９１２３１００２Ｘ（这是全角写法）",
        "expect": None,
        "why": "敏感信息**只按 ASCII 数字识别**。全角数字不视为敏感信息，"
               "以免与中文全角标点混用时产生误分类。",
    },
    {
        "id": "N11",
        "group": "不该提醒 · 低区分度词无强语义",
        "title": "正式通知里的「注：」",
        "text": "注：以上安排以最新通知为准。",
        "expect": None,
        "why": "办公文案里「注：」几乎无处不在。没有强语义词（内部/保密/待定…）共现时，"
               "它只是一句普通说明，不该打扰。",
    },
    {
        "id": "N12",
        "group": "不该提醒 · 普通业务文本",
        "title": "办证材料清单（提到「身份证」但没有号码）",
        "text": "办证材料清单：身份证复印件、学历证明、近期照片两张。",
        "expect": None,
        "why": "「身份证」只是**词**，不含号码就不构成敏感信息 —— 这类清单在日常办公里极常见。",
    },
    {
        "id": "N13",
        "group": "不该提醒 · 低区分度词无强语义",
        "title": "仓管单据里的「请确认」",
        "text": "物料编号 202401150001 已入库，请仓管确认。",
        "expect": None,
        "why": "「请…确认」区分度低（日常单据里满是「请确认」），需强语义词共现才提醒；"
               "12 位编号也不满足银行卡的 16–19 位，故整段安静。",
    },
]


def _actual(hit: Optional[detector.Hit]) -> Expect:
    """把 ``Hit`` 折算成可比较的 ``(类别, 规则名)``。

    Args:
      hit: ``detector.detect()`` 的返回值。

    Returns:
      命中时返回 ``(category, rule)``；未命中返回 None。
    """
    if hit is None:
        return None
    return (hit.category, hit.rule)


def verify() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """逐条实跑检测器并比对预期。

    Returns:
      ``(结果列表, 不一致列表)``。结果列表每项含 ``sample / hit / ok``。
    """
    results: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []
    for sample in SAMPLES:
        hit = detector.detect(sample["text"])
        entry = {"sample": sample, "hit": hit, "actual": _actual(hit)}
        entry["ok"] = entry["actual"] == sample["expect"]
        results.append(entry)
        if not entry["ok"]:
            mismatches.append(entry)
    return results, mismatches


def _fmt_expect(expect: Expect) -> str:
    """把预期结果格式化成中文描述。

    Args:
      expect: ``(类别, 规则名)`` 或 None。

    Returns:
      便于阅读的中文描述。
    """
    if expect is None:
        return "不提醒"
    labels = {
        detector.CATEGORY_SENSITIVE: "敏感信息",
        detector.CATEGORY_INTERNAL_NOTE: "内部批注",
        detector.CATEGORY_AI_RESIDUE: "AI 残留",
    }
    return f"{labels.get(expect[0], expect[0])} · {expect[1]}"


def _fmt_actual(hit: Optional[detector.Hit]) -> str:
    """把实际命中格式化成「类别 · 规则（脱敏片段）」。

    Args:
      hit: 命中结果或 None。

    Returns:
      中文描述；命中敏感信息时附带已脱敏的片段以示证据。
    """
    if hit is None:
        return "不提醒"
    text = f"{hit.category_label} · {hit.rule}"
    if hit.category == detector.CATEGORY_SENSITIVE:
        text += f"（脱敏后：{hit.fragment}）"
    return text


def write_doc(results: List[Dict[str, Any]]) -> None:
    """把样例与**实测结果**写成 Markdown 文档。

    Args:
      results: :func:`verify` 返回的结果列表。
    """
    lines: List[str] = [
        "# PastePing 检测样例集（可直接复制测试）",
        "",
        "> 本文件由 `python tools/gen_test_samples.py` **自动生成**，请勿手工编辑。",
        "> 每条样例都标注了「预期命中」，并附上**实跑 `detector.detect()` 的真实结果**。",
        "",
        "## 怎么用",
        "",
        "1. 确保 PastePing 正在运行（托盘图标为深灰底白色「P」）；",
        "2. 从下面任选一段，**整段复制**（Ctrl+C）；",
        "3. 期望行为：图标**变红 2 秒** + 一条系统通知；",
        "4. 同一段内容 60 秒内只提醒一次（降频），重复测试请换一段或等一会儿。",
        "",
        "> ⚠️ 下面所有号码、密钥都是**编造的测试值**，不是真实信息。",
        "> 其中 `AKIAIOSFODNN7EXAMPLE` 是 AWS 官方文档里的示例值、",
        "> `4111111111111111` 是公开的测试卡号。",
        "",
        "## 为什么有的「敏感词」反而不提醒",
        "",
        "检测器有一层**降误报门槛**：像「备注：」「仅供参考」「> 引用块」「——补充」",
        "这类日常文案里极常见的写法，**单独出现不提醒**，必须同屏出现",
        "「内部 / 勿外传 / 保密 / 草案 / 初稿 / 待定 / 待确认 …」这类强语义词才提醒。",
        "这是把误报率从 58.3% 压到 8.3% 换来的取舍。",
        "",
        "另外两条边界值得知道：",
        "",
        "* **AI 残留与内部批注只看文本首尾各 200 字**（产品规格如此）——特征落在中段不会提醒；",
        "* **敏感信息是全文扫描**——号码藏在长文中段也照样命中。",
        "",
        "---",
        "",
        "## 样例一览",
        "",
        "| ID | 分组 | 预期结果 | 实测 | 一致 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in results:
        sample = entry["sample"]
        lines.append(
            f"| {sample['id']} | {sample['group']} | {_fmt_expect(sample['expect'])} "
            f"| {_fmt_actual(entry['hit'])} | {'✅' if entry['ok'] else '❌'} |"
        )

    lines += ["", "---", "", "## 逐条详情", ""]
    for entry in results:
        sample = entry["sample"]
        lines.append(f"### {sample['id']}　{sample['title']}")
        lines.append("")
        lines.append(f"**分组**：{sample['group']}")
        lines.append("")
        lines.append(f"**预期**：{_fmt_expect(sample['expect'])}")
        lines.append("")
        lines.append(f"**实测**：{_fmt_actual(entry['hit'])}")
        lines.append("")
        lines.append("**待复制文本**：")
        lines.append("")
        lines.append("```text")
        lines.append(sample["text"])
        lines.append("```")
        lines.append("")
        lines.append(f"**说明**：{sample['why']}")
        lines.append("")

    lines += [
        "---",
        "",
        "## 自检命令",
        "",
        "```bash",
        "# 重新核对全部样例与预期是否一致（并刷新本文件）",
        "python tools/gen_test_samples.py",
        "",
        "# 只核对，不改文件",
        "python tools/gen_test_samples.py --check",
        "",
        "# 把某条样例复制到剪贴板，直接去真机试",
        "python tools/gen_test_samples.py --clip S3",
        "```",
        "",
        "任何一条与预期不符都会以**退出码 1** 报错 —— 样例集是一份可回归的行为契约，",
        "不是一段随手写的文本。",
        "",
    ]
    os.makedirs(os.path.dirname(_DOC_PATH), exist_ok=True)
    with open(_DOC_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _copy_to_clipboard(sample_id: str) -> int:
    """把指定样例写入剪贴板，便于真机测试。

    Args:
      sample_id: 样例 ID（如 ``S3``）。

    Returns:
      进程退出码。
    """
    sample = next((item for item in SAMPLES if item["id"] == sample_id), None)
    if sample is None:
        print(f"找不到样例 {sample_id}；可用 ID：{', '.join(s['id'] for s in SAMPLES)}")
        return 1
    try:
        import clipboard_io  # noqa: PLC0415 - 仅此分支需要，避免顶层引入 Win32 依赖

        if not clipboard_io.write_text(sample["text"]):
            print("写入剪贴板失败。")
            return 1
    except Exception as error:  # noqa: BLE001 - CLI 顶层需给出友好提示
        print(f"写入剪贴板失败：{error}")
        return 1
    print(f"已把样例 {sample_id} 写入剪贴板（{sample['title']}）。")
    print(f"预期：{_fmt_expect(sample['expect'])}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    Args:
      argv: 参数列表；None 时取 ``sys.argv``。

    Returns:
      进程退出码。
    """
    parser = argparse.ArgumentParser(
        description="生成检测样例集并实跑核对（仅开发者本机使用）"
    )
    parser.add_argument("--check", action="store_true", help="只核对，不写文档")
    parser.add_argument("--clip", metavar="ID", default=None, help="把指定样例复制到剪贴板")
    args = parser.parse_args(argv)

    if args.clip:
        return _copy_to_clipboard(args.clip)

    results, mismatches = verify()

    print(f"样例总数：{len(results)}　一致：{len(results) - len(mismatches)}"
          f"　不一致：{len(mismatches)}")
    print("")
    for entry in results:
        sample = entry["sample"]
        flag = "✅" if entry["ok"] else "❌"
        print(f"  {flag} {sample['id']:<4} {_fmt_actual(entry['hit'])}"
              f"　（{sample['group']}）")
    print("")

    if mismatches:
        print("=" * 60)
        print("以下样例与预期不一致：")
        for entry in mismatches:
            sample = entry["sample"]
            print(f"  ❌ {sample['id']} {sample['title']}")
            print(f"     预期：{_fmt_expect(sample['expect'])}")
            print(f"     实测：{_fmt_actual(entry['hit'])}")
        print("=" * 60)

    if not args.check:
        write_doc(results)
        print(f"已写出：{_DOC_PATH}")

    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())

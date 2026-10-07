# 贡献指南

感谢你愿意花时间。PastePing 是一个**规则的集合**，它的价值几乎完全取决于规则质量 ——
所以**最有价值的贡献是你遇到的误报和漏报样本**，哪怕你不写一行代码。

## 目录

- [最有价值的贡献：样本](#最有价值的贡献样本)
- [开发环境](#开发环境)
- [项目结构](#项目结构)
- [跑测试](#跑测试)
- [开发约定](#开发约定)
- [改检测规则](#改检测规则)
- [提交与 PR](#提交与-pr)
- [行为准则](#行为准则)

## 最有价值的贡献：样本

检测类工具最大的瓶颈是**真实语料**。一条真实误报，比重构一百行代码有价值。

### 报告误报（本该不提醒，却提醒了）

点此新建：[误报样本 Issue](https://github.com/chengkaide/pasteping/issues/new?template=false_positive.yml)

请尽量提供：

1. **触发提醒的原文**（可脱敏，但请保留**触发词及其上下文** —— 上下文是判定的关键）；
2. 托盘图标是否变红 / 通知文案；
3. 你认为它**不该**被提醒的理由。

### 报告漏报（本该提醒，却没提醒）

用普通 Issue 即可，同样请给出**完整的最小文本**。

### 更理想：直接提交样例

34 条样例集是可执行的**行为契约**，由 `tools/gen_test_samples.py` 生成，
每条都实跑 `detector.detect()` 核对：

```bash
python tools/gen_test_samples.py           # 核对全部样例并刷新 docs/test-samples.md
python tools/gen_test_samples.py --clip S3 # 把样例 S3 复制到剪贴板，去真机试
```

若你新增一条样例而它不通过，说明**规则与样例集中至少有一个是错的** ——
这正是我们要找的东西。请把新样例与（必要时）规则修改一并提交。

## 开发环境

- **Python 3.13**（`pyproject` 未引入前以 `requirements.txt` 为准）
- **Windows 10 / 11**（本项目依赖 Win32 API，无法在 Linux / macOS 上跑起来）

```bash
git clone https://github.com/chengkaide/pasteping.git
cd pasteping
python -m pip install -r requirements.txt
python -m pip install pytest           # 可选：跑测试
python main.py                         # 运行
```

## 项目结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | 入口：串联各模块、启动托盘与监听线程 |
| `config.py` | 进程内全局状态（单把 `RLock` 保护）+ 面向用户的可改常量 |
| `detector.py` | 三类检测规则（**冻结契约**，改动需谨慎） |
| `cleaner.py` | 自动清洗与撤销（含 60% 移除比例安全阀） |
| `converter.py` | Markdown → Word / 网页表格 → Excel |
| `clipboard_io.py` | 剪贴板读写原语（含 `CF_HTML` 切片） |
| `clipboard_listener.py` | 剪贴板监听线程 |
| `notifier.py` | 图标闪烁 + 系统通知 + 60 秒降频 |
| `tray.py` | 托盘图标与右键菜单 |
| `dialogs.py` | 模态弹窗闸门（防止叠窗） |
| `diagnostics.py` | 诊断日志（只记摘要，**不记原文**） |

**模块依赖方向严格单向，按层组织**（设计约束，PR 会检查）：
**第 N 层只许 import 比它低的层，禁止反向**。要查某模块 import 了谁，直接找它那一行：

```
第 0 层   零本项目依赖
          config · detector · diagnostics

第 1 层   只依赖第 0 层
          cleaner            → detector
          notifier           → detector, diagnostics
          dialogs            → diagnostics
          clipboard_io       → （仅 win32clipboard）

第 2 层   依赖第 1 层
          clipboard_listener → clipboard_io, diagnostics
          converter          → clipboard_io

第 3 层   托盘与装配
          tray               → config, converter, dialogs, diagnostics
          main               → tray, notifier, clipboard_listener,
                               cleaner, config, detector, diagnostics
```

- `config` 不 import 本项目任何模块；
- `detector` 不 import `config`；
- `notifier` 不 import `tray`；
- `tray` 不 import `cleaner` / `clipboard_io` / `clipboard_listener` ——
  这些对象由 `main` 用 `attach_cleaner()` / `attach_listener()` / `attach_notifier()`
  **注入**（鸭子类型），所以 `tray` 的 import 列表里看不到它们。

## 跑测试

```bash
python -m pytest -q                     # 全量
python -m pytest tests/test_detector.py -q
python -m pytest -q -k "detector and not tighten"
```

**提 PR 前请确保全量测试通过。** 本项目的测试里有一批「反回归守卫」，
它们会读源码做字面断言（例如「全部功能不得依赖任何授权状态」「不得再出现任何收费解锁
相关常量」「客户端不得内置任何私钥材料」）。
如果你看到这类失败，通常不是测试写错了，而是**改动触碰了一条项目红线** ——
请先读测试里的注释，它一般会写明这条约束的由来。

> 注意：涉及系统托盘的用例会真实创建窗口。若遇到
> `WinError 1410 类已存在` 之类的偶发失败，重跑一次即可；
> `tests/conftest.py` 已有针对窗口类名冲突的处理。

## 开发约定

这些约定来自踩过的坑，请尽量遵守：

1. **异常必须静默且安全** —— 清洗、转换、剪贴板操作失败一律不得崩进程，
   也不得损坏已有状态。宁可少做一件事，不要挂掉。
2. **敏感信息的数字位统一用 ASCII `[0-9]`**，不要用 Unicode `\d` ——
   否则全角数字会被误分类。
3. **不做无反馈的操作** —— 用户主动点击的菜单项必须每次都有可见响应，
   不允许出现「点了没反应」。
4. **诊断日志只记摘要** —— 记长度、哈希前 8 位、命中的规则名与决策，
   **严禁**记录剪贴板原文或任何码值。
5. **不新增全局状态** —— 可变状态一律进 `config.py`，由那把 `RLock` 保护。
6. **面向用户的文案不硬编码金额** —— 统一引用 `config` 里的常量。
7. **客户端不得引入网络库** —— 这是产品的核心承诺之一，PR 会检查。

## 改检测规则

- `detector.detect()` 的签名、`Hit` 的字段、以及 `_EDGE_WINDOW` / `_HEAD_SCAN` /
  `_TAIL_SCAN` / `_DEBOUNCE_SECONDS` / `_READ_RETRY` 这些常量属于**冻结契约**，
  改动必须单独说明理由与影响面；
- 调整`detector.TIGHTEN` 档位前，**先跑** `python tools/fp_lab.py`
  （64 组合穷举 + Pareto 前沿），**别凭直觉开杠杆**；
- 有效的杠杆通常是「**语义词共现门槛**」，而不是「位置收紧」——
  实测发现，误报样本的触发词往往就在第 0 字，靠位置收窄基本无效；
- 改完档位后请同步更新 `docs/false-positive-benchmark.md` 与 README 中的表格。

## 提交与 PR

- **一个 PR 只做一件事**。混在一起的改动很难评审。
- **提交信息**建议 `类型: 简述`，例如：

  ```
  fix: 修复「关于」弹窗可被连续点击叠出多个窗口
  feat: 新增不可见字符检测
  docs: 补充 SmartScreen 解除锁定步骤
  test: 为清洗安全阀补边界用例
  ```

- PR 描述里请写清：**动机 → 改动 → 如何验证**。
- 若你的改动**改变了任何既有行为**，请在描述里显式列出（哪怕是「应该没人依赖」的小改动）。
- 新增客户端模块时，记得把它同时加入 QA 测试的模块白名单，否则守卫不会覆盖它。

## 行为准则

- 就事论事，不针对人；
- 对新手友好 —— 每个人都曾经不知道 `RLock` 是什么；
- 报告误报时请附真实样本，不要只说「不准」；
- 维护者是**业余时间**在做这件事，响应可能不及时，请给一点耐心。

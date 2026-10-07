# PastePing · Clipboard Safety Reminder

[简体中文](README.md) | **English**

> A quiet one-time nudge before you paste internal notes, AI leftovers, or sensitive data somewhere they shouldn't go.

**Every feature is completely free — no in-app purchases, no feature locks, no trial period.** Runs entirely locally with **zero network requests**; clipboard contents are never written to disk.

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-lightgrey.svg)](#installation)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776ab.svg)](https://www.python.org/)
[![No network](https://img.shields.io/badge/Network-none-success.svg)](#why-you-can-trust-this-not-just-a-promise)

[Download the latest release](https://github.com/chengkaide/pasteping/releases/latest) ·
[User guide](docs/user-guide.md) ·
[Privacy policy](PRIVACY.md) ·
[Contributing](CONTRIBUTING.md) ·
[Security policy](SECURITY.md)

> **Note on language:** PastePing is built primarily for Chinese-speaking users, so most documentation
> under `docs/`, plus `PRIVACY.md` / `CONTRIBUTING.md` / `SECURITY.md`, is written in Chinese.
> This file is the complete English overview.

---

## Table of contents

- [What problem does this solve?](#what-problem-does-this-solve)
- [Everything is free](#everything-is-free)
- [Why you can trust this (not just a promise)](#why-you-can-trust-this-not-just-a-promise)
- [Feature overview](#feature-overview)
- [Installation](#installation)
- [First run: you may see a SmartScreen warning](#first-run-you-may-see-a-smartscreen-warning)
- [Usage](#usage)
- [Detection rules](#detection-rules)
- [Auto-cleaning rules](#auto-cleaning-rules)
- [Format conversion](#format-conversion)
- [Development](#development)
- [Known limitations](#known-limitations)
- [License](#license)

---

## What problem does this solve?

Three kinds of accidents, all of which happen in the few seconds between **copy** and **paste**:

| Scenario | Consequence |
| --- | --- |
| Sending content with **internal annotations** to a client | "Do not distribute" / "for internal reference only" goes out along with the body text |
| Sending an **AI-generated leftover** as your own words | Phrases like "Hope this helps!" or "Let me know if you'd like me to…" stay in the message verbatim |
| Pasting a **phone number / ID number / API key** into a chat box | Sensitive data leaks |

PastePing sits in the system tray, checks **the moment you copy**, and when something matches it turns the tray icon red for 2 seconds and shows a notification.
It never pops up a modal, never changes your keyboard habits (keep using `Ctrl+C` / `Ctrl+V` as usual), and never talks to the network.

## Everything is free

**Every feature of this project is completely free.**

- All three detection classes, reminders, rate limiting, pause, master switch — free
- Auto-clean before pasting, plus undo — free
- Markdown → Word, web table → Excel — free

**No in-app purchases, no feature locks, no trial period, no usage-count or length limits.**

If the tool helps you, you may optionally sponsor development (the "Support the developer" item in the tray menu).
**Sponsoring unlocks nothing and changes nothing** — you already have every feature before you sponsor.
It simply supports continued maintenance: [Afdian](https://afdian.com/a/pasteping).

> This project is licensed under **GPL-3.0**, so nobody can turn it into a "paid features + closed source" version
> (derivative works must be released under the same license, with source freely available). See [License](#license).

## Why you can trust this (not just a promise)

The most common trap with privacy tools is claiming "it doesn't connect to the internet" and asking you to take their word for it.
**Open source is what turns that sentence into something you can verify.**

This project:

1. **Zero network requests** — the client does not `import` any networking library. There is no telemetry, no auto-update,
   and **no license-validation server either** (the project has no activation or licensing mechanism at all, so there is nothing to call home to);
2. **Clipboard contents are never written to disk** — they are processed in memory and discarded;
3. **Exactly one file is ever written locally**: the diagnostic log at `%APPDATA%\PastePing\pasteping.log`.
   It contains only text length, the first 8 characters of a hash, matched rule names, and the decision taken —
   **never the clipboard text itself**, and no key material of any kind.

**You can verify all three yourself:**

```bash
# 1) Search for network calls: there should be no output
grep -rnE "^\s*(import|from)\s+(socket|ssl|urllib|http|requests|ftplib|smtplib|telnetlib)\b" *.py

# 2) Search for any disk-write behaviour
grep -rnE "open\(|\.write\(|os\.remove|shutil\." *.py

# 3) Unplug your network cable, or block all of its outbound connections in your firewall — functionality is unaffected
```

See [PRIVACY.md](PRIVACY.md) for details.

## Feature overview

| Capability | Description |
| --- | --- |
| Three detection classes | Internal annotations / AI leftovers / sensitive data |
| Notify on copy | Tray icon turns red for 2 seconds + system balloon notification |
| 60-second rate limit | The same text never nags you twice |
| Pause for 5 minutes / 1 hour | Temporarily silent, resumes automatically |
| Master switch | Disable detection with one click |
| Clean before pasting | Writes the cleaned text back to the clipboard; "Undo last clean" available |
| Markdown → Word | Produces `.docx` |
| Web table → Excel | Produces `.xlsx` |
| Open output folder | Jump straight to converted files |

**Features deliberately not included:**

- **No command-line or graphical settings panel** — the only configuration entry point is the tray context menu, for zero learning curve;
- **No "AI watermark removal"** — stripping invisible characters is data hygiene, but circumventing vendor
  statistical watermarks engages the transparency obligations of Article 50 of the EU AI Act. This project does not do it.
  See the [AI watermark detection assessment](docs/ai-watermark-detection.md).

## Installation

### Option 1: Download the prebuilt binary (recommended, no Python required)

1. Download `PastePing.exe` from [Releases](https://github.com/chengkaide/pasteping/releases/latest);
2. **"Unblock" the file before running it for the first time** (see the next section for why);
3. Double-click it. If the tray icon appears, you're done.

### Option 2: Package manager

```powershell
# If/when it is published to winget / scoop (still a TODO)
winget install PastePing
```

> A package-manager install does **not** attach the mark-of-the-web, so it normally runs directly.

### Option 3: Run from source

```bash
git clone https://github.com/chengkaide/pasteping.git
cd pasteping
python -m pip install -r requirements.txt
python main.py
```

There are **5** runtime dependencies: `pywin32` / `pystray` / `Pillow` / `python-docx` / `openpyxl`.

## First run: you may see a SmartScreen warning

An unsigned `.exe` downloaded through a browser will make Windows show:

> **Windows protected your PC** — Microsoft Defender SmartScreen prevented an unrecognized app from starting.

![SmartScreen warning](docs/img/smartscreen-block.png)

**Just click "More info" → "Run anyway".**

This screen has nothing to do with the program itself. **It is triggered by the mark-of-the-web, not by the lack of a signature.**
So there are two cleaner ways around it:

**Option A (recommended): Unblock the file first**

Right-click `PastePing.exe` → Properties → tick **"Unblock"** at the bottom → OK.
Or in one line:

```powershell
Unblock-File .\PastePing.exe
```

**Option B: Install via a package manager** (no browser involved, so no mark-of-the-web).

> This project has applied for the free code-signing service that [SignPath Foundation](https://signpath.org/)
> offers to open-source projects. Once the certificate is issued, release builds will carry a proper signature
> and the prompt above will no longer appear.
> Background and measurements: [docs/code-signing.md](docs/code-signing.md).

## Usage

After launch, the app lives in the system tray (you may need to drag it out of the "hidden icons" area).

> **Only one instance runs at a time.** Launching it again does not start a second copy —
> it tells you the app is already running. Two instances would leave two identical tray
> icons and both would rewrite the clipboard.

**What the tray icon means:**

- Dark grey background with a white "P" = running, idle;
- **Red background with a white "P"** = content was just detected, 2 seconds until it fades back to grey.

> The icon turning red is this tool's **most reliable signal** — system balloon notifications can be muted by Focus Assist / Do Not Disturb.

**Tray menu:**

- ☑ Sensitive reminders: master switch (checkbox)
- ☑ Clean before pasting: when ticked, matched content is cleaned and written back to the clipboard (off by default)
- Undo last clean: appears only when there is something to undo
- Format conversion ▸: Markdown → Word / Web table → Excel / Open output folder
- Pause for 5 minutes / Pause for 1 hour
- Support the developer ¥3 (purely voluntary; does not affect any feature)
- About / Quit

**Debugging "I copied but got no reminder"**: check `%APPDATA%\PastePing\pasteping.log`.
The log records only lengths, the first 8 characters of hashes, matched rule names and decisions —
**never the clipboard text** — so it is safe to share. The console prints this path at startup too.

## Detection rules

| Class | Scope | Example rules |
| --- | --- | --- |
| Internal annotations | Reminder: first/last 200 characters each; cleaning: whole text | forward this message / for internal use / do not distribute / 备注： (note:) / 仅供…参考 (for reference only) / 【批注】 (annotation) / `>` block quotes / —— 补充 (addendum) |
| AI leftovers | Reminder: first/last 200 characters each; cleaning: whole text | Openers: 当然可以 (of course) / 以下是 (below is) / 好的我 (okay, I) / 没问题 (no problem); Closers: 希望对你有帮助 (hope this helps) / 需要我帮你 (would you like me to) … |
| Sensitive data | Whole text | ID numbers / mobile numbers / API keys / bank cards (fragments are automatically redacted) |

**Priority**: sensitive data > internal annotations > AI leftovers. The same text is reminded about only once every 60 seconds.

### False-positive thresholds

`detector.TIGHTEN` is a set of **independently switchable** tightening thresholds. The current profile cuts the
false-positive rate from 58.3% to **8.3%** while keeping recall at 100%
(methodology: [docs/false-positive-benchmark.md](docs/false-positive-benchmark.md)):

| Lever | Threshold | Default |
| --- | --- | :---: |
| `disclaimer` | "For … reference only" must co-occur with annotation keywords in the same window | on |
| `structure` | Line-leading `>` block quotes / `——` addenda must co-occur with annotation keywords | on |
| `bracket` | `【…】` must contain annotation keywords **inside the brackets** | on |
| `bankcard` | A 16–19 digit number must pass the Luhn check, or appear near card-related context words | on |
| `low_signal_keywords` | `注：` / `备注：` / `请确认` / `请审阅` must co-occur with a **strong** semantic keyword | on |
| `ai_boundary` | AI markers must be directly adjacent to the start/end of the text (this narrows the 200-character window to 8) | **off** |

> "Semantic keywords" means unambiguous internal-context words such as 内部 / 勿外传 / 保密 / 草案 / 待定 /
> 待确认 / 批注 / 备注 (internal / do not distribute / confidential / draft / pending / to be confirmed / annotation / note).
> Before changing the profile, run `python tools/fp_lab.py` (exhaustive combination search + Pareto frontier).

### Self-test samples (copy-paste ready)

To see "what actually triggers and what doesn't" without guessing, use the ready-made sample set:

```bash
python tools/gen_test_samples.py            # verify all samples and regenerate the doc
python tools/gen_test_samples.py --clip S3  # copy one sample to the clipboard to try for real
```

34 samples (21 that *should* trigger + 13 that should not) each annotate the expected result and the reason, generating
[docs/test-samples.md](docs/test-samples.md). The generator **actually calls `detector.detect()` on every sample** and exits
with code 1 on any mismatch — so this set is a **behavioural contract**, not casually written text, and it is
continuously guarded by `tests/test_test_samples.py`.

## Auto-cleaning rules

| Class | Treatment |
| --- | --- |
| AI leftovers | **Delete** the whole sentence containing a matched opener/closer marker |
| Internal annotations | **Delete** matched keywords / `【批注】` / line-leading `>` quotes / `——` addendum sentences |
| Sensitive data | **Replace with a placeholder** `［已移除：身份证 / 手机号 / API Key / 银行卡］` |

**Safety valve**: if cleaning would remove more than 60% of the characters, cleaning is **abandoned and only a reminder is shown**
(to avoid deleting large blocks of content by mistake).
After cleaning, the original text can be restored via "Undo last clean" in the menu (in-memory only; the original is never written to disk).

## Format conversion

- **Markdown → Word**: supports headings, ordered/unordered lists, bold/italic, inline code, quotes, pipe tables and code blocks;
- **Web table → Excel**: parses `<table>` from the clipboard's `CF_HTML` first (`colspan` filled to the right, `rowspan` padded with empty cells downward);
  falls back to tab / pipe-separated parsing when there is no `CF_HTML`;
- Output is saved automatically to `%USERPROFILE%\Documents\PastePing\` as
  `PastePing_YYYYMMDD_HHMMSS_<suffix>.(docx|xlsx)`, **with no dialogs at all**;
- Tables too irregular to align are degraded to "lay out cells in order of appearance" with a note — **no error, no crash**.

## Development

```bash
# Environment
python -m pip install -r requirements.txt
python -m pip install pytest            # testing (optional)
python -m pip install pyinstaller       # packaging (optional)

# Full test suite
python -m pytest -q

# Real-hardware smoke test: exercises the whole clipboard pipeline on real Windows
# (it briefly takes over the clipboard and restores it afterwards)
python tools/smoke_test.py

# Build the single-file exe
python tools/make_icon.py               # generate the exe icon (refreshed automatically at build time; usually not needed)
python tools/build_exe.py               # build + automatic artifact safety checks

# False-positive benchmark / tuning
python tools/fp_benchmark.py            # measure the current profile
python tools/fp_lab.py                  # exhaustive combination search + Pareto frontier
```

**Architecture**: the dependency direction is strictly one-way, organised into **layers** —
**layer N may only import from layers below it, never the other way round**.
To see what a given module imports, just find its row below:

```
Layer 0   No project dependencies (unit-testable offline; no Windows / tray / clipboard needed)
          config        global state + a single RLock
          detector      the three detection classes (pure logic; no GUI, no Windows)
          diagnostics   local diagnostic log (fingerprints only, never the raw text)

Layer 1   Depends only on layer 0
          cleaner            -> detector
          notifier           -> detector, diagnostics
          dialogs            -> diagnostics
          clipboard_io       -> (win32clipboard only; no project dependencies)

Layer 2   Depends on layer 1
          clipboard_listener -> clipboard_io, diagnostics
          converter          -> clipboard_io

Layer 3   Tray and assembly
          tray               -> config, converter, dialogs, diagnostics
          main               -> tray, notifier, clipboard_listener,
                                cleaner, config, detector, diagnostics
```

> `tray` lives in layer 3 but uses the layer-1 `cleaner` and the layer-2 `clipboard_listener` at runtime.
> Those objects are **injected** by `main` via `attach_cleaner()` / `attach_listener()` / `attach_notifier()`
> (duck typing), which is why they do not appear in `tray`'s import list — and therefore no reverse dependency is formed.

Four inviolable rules:

- `config` imports no other project module;
- `detector` does not import `config` (so it can run offline on tens of thousands of samples without runtime state);
- `notifier` does not import `tray` (notifications are decoupled from the tray, so tests can swap the tray for a recorder);
- `tray` does not import `cleaner` / `clipboard_io` / `clipboard_listener` (injection only).

**Before changing code, read the [code tour (docs/code-tour.md)](docs/code-tour.md)** — it explains the runtime
thread model, the three data flows, the frozen contracts, which file to touch for common changes, and a list of
pitfalls previous contributors have already hit.

**Development conventions** (read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR):

- Exceptions must be **silent and safe** — no failure may crash the process;
- Digit classes in sensitive-data patterns always use ASCII `[0-9]`, **never** Unicode `\d` (otherwise full-width digits are misclassified);
- Changing the signature of `detector.detect()`, the `Hit` fields, or existing constants counts as a **frozen-contract change**
  and must be called out explicitly in the PR.

## Known limitations

- **Windows only** (it relies on Win32 APIs and the system tray);
- **Detection still has about an 8.3% false-positive rate** (3/36), concentrated in everyday pleasantries such as
  "no problem" / "okay, I" / "would you like me to"；
  the cost of tightening is on the **missed-detection** side: a lone `备注：` / `注：` / `仅供参考` / `> quote block` / `—— addendum`
  now needs a co-occurring word such as 内部 / 勿外传 / 保密 / 草案 / 待定 before it triggers;
- Detection is based on **fixed rules, not a model**, so **misses are inevitable** — please do not treat it as your only line of defence for data protection;
- Opening external links uses a three-level fallback of `os.startfile → ShellExecuteW → webbrowser` (see `tray.open_url`);
- An unsigned exe downloaded through a browser will hit SmartScreen on first run (see [above](#first-run-you-may-see-a-smartscreen-warning)).

## License

[GNU General Public License v3.0](LICENSE) (GPL-3.0).

```
Copyright (C) 2026 PastePing

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
```

**What this means**: you are free to use, modify and distribute this software, including for a fee;
but **derivative works must also be open-sourced under GPL-3.0** — you cannot turn it into a closed-source paid version.

## Disclaimer

PastePing is an **assistive reminder** tool and does not constitute any form of data-protection or compliance guarantee.
Detection is rule-based and **may both miss things (no reminder) and over-trigger (false reminder)**.
Auto-cleaning and format conversion are best-effort, "what you see is what you get", and **do not guarantee 100% preservation
of the original wording or layout** — always review cleaned content before pasting. The developers accept no liability for any
direct or indirect loss arising from the use of this tool.

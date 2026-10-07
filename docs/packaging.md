# 打包与分发（Windows 单文件 exe）

> 产物：`dist/PastePing.exe`
> 一键命令：`python tools/build_exe.py`
> 本文记录**实测结论**，包括第三节那套「正反双向 + PYZ 归档」的产物检查。

---

## 一、怎么打包

```bash
# 装一次打包工具（不进 requirements.txt，它不是运行时依赖）
pip install pyinstaller

# 完整打包 + 自动检查
python tools/build_exe.py

# 只检查已有产物，不重新构建
python tools/build_exe.py --skip-build
```

实测环境：Python 3.13.12（托管版）+ PyInstaller 6.22.3 + pyinstaller-hooks-contrib 2026.8，
构建耗时约 40~60 秒，产物 **20.8 MB**（21,795,332 字节）。

**为什么打包要用脚本而不是手敲命令**——两条都不是洁癖：

1. PyInstaller 需要若干**隐藏导入**（`pystray._win32` 是 pystray 按平台**动态**导入的后端，
   静态分析看不见）。漏一个的结果是「编译成功、用户一运行就崩」，而且崩在你验证不到的机器上。
2. exe 是要发给用户的，必须证明它**没有夹带发码私钥**。这个错误「文件在、程序也能跑」，
   肉眼看不出来，所以每次打包都必须自动验（见第三节）。

---

## 二、产物里装了什么

| 类别 | 内容 | 说明 |
| --- | --- | --- |
| 自研模块 | `config` `detector` `clipboard_listener` `clipboard_io` `converter` `cleaner` `notifier` `tray` `dialogs` `diagnostics` `main` | 全部已核对在场（共 11 个；`dialogs` 为弹窗闸门，`diagnostics` 为诊断日志） |
| 运行时依赖 | `pystray`（+`_win32` 后端）、`PIL`（+`_imaging`）、`pywin32`（仅 `win32clipboard`）、`python-docx`、`openpyxl` | 与 `requirements.txt` 一致（共 5 个） |
| **刻意排除** | **`numpy`** | 源码从未 import 它（PyInstaller 经 PIL 的 hook 条件拉入）。排除后体积 **34.6 MB → 24.4 MB**；2026-10-07 移除授权体系（连带 `cryptography` / `_cffi_backend`）后进一步降到 **20.8 MB** |
| **绝不包含** | `tools/private_key.pem`、`tools/make_backup.py`、`tools/build_exe.py`、`tests/` | 开发者脚本与密钥材料一律不进客户端（私钥虽已随授权体系作废，但检查保留） |

---

## 三、产物检查（每次打包自动执行）

脚本做的是**正反双向**检查 —— 只查「不该有的没有」不够：漏掉一个扩展模块时产物照样生成，
但一运行就崩。

**不许出现**：`private_key.pem`、`make_backup`、
`BEGIN PRIVATE KEY`、`BEGIN OPENSSH PRIVATE KEY`、`_multiarray_umath`

**必须出现**：

| 标记 | 对应能力 |
| --- | --- |
| `win32clipboard` | 剪贴板读写 |
| `pystray._win32` | Windows 托盘后端 |
| `_imaging` | Pillow C 扩展（现场生成托盘图标） |

> 说明：PyInstaller 会压缩 `.pyc`，所以「文件内容级」扫描属**尽力而为**；
> 结论性证据是**文件名级**扫描（CArchive 的表项名不压缩）。

**第三重检查（2026-10-06 新增）：自研模块必须真的在 PYZ 归档里。**

上面那套字节扫描只覆盖得到扩展模块（`.pyd` 的名字不压缩）。**纯 Python 模块（含全部自研模块）
被压缩进 PYZ，字节扫描完全看不见它们** —— 而它们缺失时 exe 照样生成成功，
却会在用户机器上、在我们自己的代码里启动即崩。所以脚本改用 PyInstaller 自带的归档读取器
直接读目录表：

```python
CArchiveReader(exe)                      # 顶层：入口脚本 main、各 .pyd
ZlibArchiveReader(<解出的 PYZ 文件>)      # PYZ 内：被压缩的纯 Python 模块名
```

当前需逐个核对在场的是 **11 个自研模块** + 入口脚本 `main`。若读不到归档（例如未安装 PyInstaller），
脚本会**明确打印「本项跳过」**，而不是假装检查通过。

> 这项检查同样做过反向验证：把清单里塞一个不存在的模块名，脚本确实报出缺失 ——
> 一个不可能失败的守卫等于没有守卫。

---

## 四、弹窗：只剩 MessageBox，不再有自绘输入框（2026-10-07）

**曾经有一层**：为了让用户在托盘里键入「支持者码」，`ui_dialog.py` 自绘了一个
Win32 输入框（`CreateWindowExW` + `EDIT` + 三个按钮），并为「打包环境缺 Tcl/Tk」
专门做了三级通道降级（自绘 → tkinter → `MessageBoxW`）。
2026-10-07「付费解锁」整层移除后，**这个输入框连同它的测试文件
`tests/test_ui_dialog_native.py` 一起删除** —— 现在程序里不再有任何需要用户**键入**
的弹窗，只剩确认框与只读的「关于」，统一走 `dialogs` 的 `MessageBoxW`。

所以：

* `_HIDDEN_IMPORTS` 里**不再需要 `tkinter`**；构建环境缺 Tcl/Tk **完全不影响功能**；
* 弹窗能力清单只剩两个：`dialogs.message()`（只读信息）与 `dialogs.ask_yes_no()`（二选一），
  两者都带 `MB_SETFOREGROUND | MB_TOPMOST`，并受「同一时刻只允许一个框」的闸门保护
  （`dialogs.begin()` / `end()`，由 `tests/test_dialogs.py` 守护）。

### ⚠️ 留下的通用教训：`ctypes.windll.user32` 与 `ctypes.WinDLL("user32")` 是两个对象

这段教训出自已删除的 `ui_dialog`，但**至今仍适用于 `dialogs.py`** ——
它用 `ctypes.windll.user32` 调 `EnumThreadWindows`，而 `clipboard_listener.py`
用的是 `WinDLL("user32")`。

ctypes 的 **DLL 对象并不共享函数原型**：

```python
ctypes.windll.user32 is ctypes.WinDLL("user32")   # → False，两个不同的 DLL 实例
```

于是两边给同一个函数声明的 `argtypes`（64 位句柄原型）**互相看不见、还可能互相覆盖** ——
结果是 64 位句柄被静默截断成低 32 位：表现为「拿到一个假句柄」，而且**不报错**。

**做法**：需要可靠原型时，**自己持有 DLL 句柄，并在每次调用前重新声明原型**，
不要依赖全局缓存对象。

---

## 五、分发时会遇到的三件事（都不是 bug）

1. **SmartScreen 拦截**：未做代码签名的 exe 在别人机器上首次运行会被 Windows 拦
   （「Windows 已保护你的电脑 / 未知发布者」），用户需点「更多信息 → 仍要运行」。
   ⚠️ **触发条件是「网络标记」(MotW)，而不是"未签名"本身** —— 本仓库 2026-10-07 已实测：
   带标记必被拦、去掉标记必放行、`asInvoker` 清单下**不会有 UAC 提权框**
   （完整记录见 `docs/code-signing.md` 第七节）。所以除了买证书，
   还有一条**零成本**的路：让用户「解除锁定」。
   这是**陌生人分发时的真实摩擦**，会很影响转化。自用/给熟人则无所谓。
2. **首次启动慢几秒**：`--onefile` 要先把自己解压到临时目录再运行，属正常现象。
3. **看不到控制台输出**：用了 `--windowed`（托盘应用不该有黑框），
   于是 `main.py` 里的 `print`（占位常量提醒、缺依赖提示）在 exe 中**不可见**。
   排障时请改用带控制台的构建：`--windowed` 换成 `--console`。

---

## 六、分发前清单

- [ ] `python tools/build_exe.py` 全部检查通过（私钥未混入、扩展模块齐全、11 个自研模块都在 PYZ 里）
- [ ] **在真实 Windows 桌面双击一次**，确认托盘图标出现、右键菜单完整
- [ ] 点一次「关于」→ 点「确定」→ 确认**窗口真的关掉**；再连点两次「关于」确认**只会有 1 个窗口**
- [ ] 确认「自动清洗粘贴」「格式转换」「撤销上次清洗」**点了都直接生效**（全部功能免费，无任何门槛）
- [ ] 点一次「支持开发者」→ 确认浏览器打开了赞助页（或弹窗里给出了完整地址）
- [ ] 决定签名路线，并按需执行 `python tools/sign_exe.py --sign ...`（见 `docs/code-signing.md`）
- [ ] 未签名时，把 `python tools/sign_exe.py --hash` 得到的 **SHA256 贴到下载页**
- [ ] 拔掉源码目录（或换一台机器）运行一次，确认 exe 自包含、不依赖本机 Python
- [ ] 确认 `tools/private_key.pem` **没有**随 exe 一起发给任何人
- [ ] 确认仓库里**没有任何内部运营文档**（`.gitignore` 已列，但请 `git ls-files` 复核一次）

> 上述「双击运行」类验证**只能由你来跑**：脚本能证明的仅是「产物生成成功、不含私钥、
> 必需扩展齐全、自研模块都在包里」，**不能证明它真能启动**。

---

## 七、源码备份

```bash
# 默认备份到 <项目同级>/_backups/pasteping_<时间戳>/
python tools/make_backup.py

# 指定目录（已存在则同步覆盖）+ 同时打 tar.gz
python tools/make_backup.py --dest "D:/bak/pasteping" --tar
```

备份工具会生成 `MANIFEST.txt`（逐文件 sha256 + 恢复步骤），并**在备份目录内实跑一次
「检测 + 清洗」自检**：把一段含身份证号的文本喂给 `detector.detect()` 必须命中 `sensitive`，
再喂一段干净的技术段落必须**不**命中，最后跑一遍 `cleaner.clean()` 确认敏感号确实被移除。
只比对「文件存在」是没有意义的 —— 文件被截断、编码损坏、依赖漂移时它一样在，但代码已经跑不动了。

> 备份工具还会顺手检查备份里是否混入了**已作废的** `tools/private_key.pem`（本项目已不再需要私钥），
> 若有则在清单里提示你删除 —— 免得把一份**再也不需要、却仍具敏感性**的文件一路带下去。

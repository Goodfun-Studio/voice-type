# Voice Type — Windows 客户端（Python）

macOS 版（`app/Sources/*.swift`）的 Windows 移植，只做客户端；转写引擎仍是仓库里的
`engine/engine.py`（faster-whisper 后端），本目录不含引擎代码，只在启动时把它拉起来。

> ## ⚠️ 诚实的边界（请先读这一段）
>
> **这份代码只在 macOS 上做过 `python3 -m py_compile` 语法检查，从未在 Windows 上运行过。**
> 没有任何一行在真实 Windows 上执行、点击或联调过。所有 win32 逻辑（低级键盘钩子、
> `SendInput`、`WS_EX_NOACTIVATE`、剪贴板 API）都是照文档写的，**未经运行时验证**。
> 请把它当作「结构完整、待验证」的初稿，第一次在 Windows 上跑时预留调试时间。
>
> 预期最可能出问题的三处（按风险排序，详见文末「三大风险」）：
> 1. **低级键盘钩子的双击 / 吞键行为** —— 钩子回调超时会被系统摘掉，双击窗口、
>    吞 Esc 的时机都可能需要在真机上微调。
> 2. **`WS_EX_NOACTIVATE` 是否真的不抢焦点** —— 抢了焦点，粘贴就会贴到 HUD 上。
> 3. **目标程序以管理员身份运行时 `SendInput` 失效** —— UIPI 权限隔离，粘贴发不出去。

## 交互（与 macOS 版一致）

1. **双击右 Ctrl** 开始录音；再双击一次 → 停止 → 转写 → 粘贴进当前光标。
   双击窗口 **450ms**（与 macOS `FnHotkey.doubleTapWindow` 相同）。
2. **录音中按 Esc = 取消**：音频当场丢弃，不转写、不粘贴。
   **转写中按 Esc 也有效**，结果到达时被丢弃（generation 计数保证张冠李戴不会发生）。
3. 录音时屏幕**底部中间**显示**悬浮电平条**：圆角胶囊、置顶、半透明、**不抢焦点**，
   右侧写着「esc 取消」。
4. 转写完成后把文字**敲进当前光标**：写剪贴板 + `SendInput` 模拟 `Ctrl+V`；
   粘贴前若前台窗口变了，先 `SetForegroundWindow` 恢复。
5. **系统托盘图标**（`pystray`，**可选依赖**）：切换语言 / 重启引擎 / 看日志 / 看历史 /
   退出。缺 `pystray` 或 `Pillow` 时**退化成无声运行，不会因缺依赖崩**。
6. 历史按天落 `%APPDATA%/VoiceType/history/YYYY-MM-DD.md`。

## 目录

```
client-windows/
├── voicetype_win/
│   ├── __init__.py        包元信息
│   ├── __main__.py        入口：状态机(Controller) + 托盘 + App 装配 + --selftest
│   ├── winsys.py          受保护的 ctypes 封装（钩子/焦点/SendInput/剪贴板/扩展样式）
│   ├── hotkey.py          WH_KEYBOARD_LL 低级键盘钩子（双击右 Ctrl + 录音期吞 Esc）
│   ├── recorder.py        sounddevice → 16kHz 单声道 16-bit WAV，边录边算 RMS
│   ├── hud.py             tkinter 圆角胶囊电平条（WS_EX_NOACTIVATE，不抢焦点）
│   ├── inserter.py        写剪贴板 + Ctrl+V 粘贴 + 按天写历史
│   ├── engine_client.py   引擎 TCP 客户端（一行一个 JSON）+ 自动拉起引擎
│   └── config.py          路径 + config.json 读写 + 端点发现
├── requirements.txt
├── install.ps1            建 venv、装依赖、写配置、注册开机自启
├── run.bat
└── README.md
```

## 装 / 跑

```powershell
# 安装（建 venv、装依赖、写配置、注册自启）
powershell -ExecutionPolicy Bypass -File .\install.ps1

# 前台运行（带控制台，方便看日志）
.\run.bat

# 依赖自检（任何平台都能跑，非 Windows 上会优雅退出）
python -m voicetype_win --selftest
```

配置文件 `%APPDATA%\VoiceType\config.json`（不存在就生成默认）：

```json
{
  "python": "C:\\path\\.venv\\Scripts\\pythonw.exe",
  "engine": "C:\\...\\engine\\engine.py",
  "host": "127.0.0.1", "port": 8765, "transport": "tcp",
  "model": "small", "device": "cpu", "compute_type": "int8",
  "language": "zh", "beam_size": 5, "sound": true
}
```

**CPU 优先**：默认 `device=cpu` / `compute_type=int8` / `model=small`（faster-whisper
在 CPU 上能跑得动）。有 N 卡改成 `device=cuda` / `compute_type=float16` /
`model=large-v3-turbo`。

**`beam_size` 必须默认 5，不要改成 1**：实测 beam=1 时 whisper 会把提示词原样复读
出来（引擎端 `is_prompt_echo` 会丢弃，但仍应避免）。

## 引擎协议（已冻结）

启动引擎（Windows 上默认就是 tcp，显式传参更稳）：

```
python engine.py --serve --transport tcp --port 8765
```

- **端点发现**：引擎写 `%APPDATA%\VoiceType\engine.json`，客户端读它拿 `host`/`port`；
  读不到就用配置默认端口。引擎可能绑了别的端口，所以优先信 `engine.json`。
- **协议**：一行一个 JSON（`\n` 结尾），一条请求一条响应。
  - `{"cmd":"ping"}` → `{"ok":true,"model":"faster-whisper:small@cpu/int8","backend":"faster-whisper","warm":true}`
  - `{"cmd":"transcribe","wav":"C:\\Users\\x\\a.wav","language":"zh"}` →
    `{"ok":true,"text":"...","raw":"...","elapsedMs":1234,"audioSeconds":7.06,"rms":0.12,"rtf":0.3}`
    （`language` 用 `"auto"` 表示自动识别）
  - 失败：`{"error":"..."}`
  - **`{"ok":true,"text":"","reason":"silence"}` 是正常结果**（reason 可能是
    `silence`/`hallucination`/`no-words`/`prompt-echo`）。客户端**不当错误处理，也不粘贴空串**。
- 客户端职责：先 `ping`，不通就 `subprocess` 拉起引擎（用配置里的 python 路径），
  **最多等 90 秒**。

## 几个实现要点（踩坑记录）

- **热键不用 `keyboard` 库**：它要装内核驱动，常被安全软件拦。这里用
  `user32.SetWindowsHookExW(WH_KEYBOARD_LL, …)`。钩子回调里**只做状态判断 + 投递队列**，
  真正的开工在 worker 线程；Windows 会摘掉超时的钩子，回调里绝不能干耗时活。
  - 不录音时**绝不吞任何按键**（一律 `CallNextHookEx` 放行）；**只在录音/转写期间吞 Esc**。
- **HUD 必须加 `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`**（`GWL_EXSTYLE = -20`），
  否则置顶窗口一出现就把焦点抢走，`Ctrl+V` 会贴到 HUD 自己身上。
- **`SendInput` 的 `INPUT` 结构体在 64 位下是 40 字节**：用 `ctypes` 的自然对齐定义
  （`ULONG_PTR = c_size_t`），不要手工打包。`--selftest` 会核对 `sizeof(INPUT)`。
- **右键 Ctrl 不吞**：双击右 Ctrl 只用来触发，按键照常放行，不影响别处的右键 Ctrl。
- **win32 相关导入全部 try/except**：缺库/非 Windows 时给清晰提示，不崩栈。

## 验证（在 macOS 上做的）

```bash
cd client-windows
python3 -m py_compile voicetype_win/*.py      # 语法检查，全过
python3 -m voicetype_win --selftest           # 依赖自检，非 Windows 上优雅退出
```

`--selftest` 逐项打印 ✓/✗：python 版本、`sounddevice`、`numpy`、`pystray`、`PIL`、
`tkinter`、win32 层、引擎路径、配置目录可写、本地 TCP、引擎可达性/端口占用、
`engine.json`。在 macOS 上 win32/HUD 相关项会打印「仅 Windows 可用 / skipped」而不是崩。

## 三大风险（我预期最可能出问题的地方）

1. **低级键盘钩子的双击与吞键行为。**
   Windows 对低级钩子有超时预算，钩子回调若被拖慢会被**静默摘掉**（表现为热键突然失灵，
   而进程还活着）。另外双击窗口 450ms、自动重复键的去重、以及「Esc 只吞录音期、
   其余一律放行」这三件事的**边界时机**在真机上需要用日志 `voicetype.log` 核对。
   *验证方法*：连按/长按右 Ctrl、录音时按 Esc、录音时狂敲其它键，看日志与目标窗口行为。

2. **`WS_EX_NOACTIVATE` 是否真的让 HUD 不抢焦点。**
   Tk 的 `overrideredirect` 窗口在 `winfo_id()` 上是否就是最终顶层 HWND、扩展样式是否
   生效于映射前，都会影响结果。若失效，症状是**粘贴贴到 HUD 上 / 目标窗口丢焦点**。
   *验证方法*：录音时在记事本里连续打字，看光标是否还在记事本里。

3. **目标程序以管理员身份运行时 `SendInput` 失效。**
   Windows UIPI 不允许普通权限进程向高完整性级别窗口注入按键。若你正在管理的程序里
   说话，剪贴板会是好的（文字已复制），但 `Ctrl+V` 发不进去。
   *验证方法*：在管理员权限的编辑器里测一次；必要时以管理员身份运行 Voice Type。
   代码里的兜底是：**粘贴发不出去时文字已在剪贴板**（toast 会提示「已复制到剪贴板」）。

### 其它已知未验证点

- `install.ps1` / `run.bat` 未在 Windows 上执行过。
- 托盘图标（pystray 的 `run()` 在独立线程里）在 Windows 上的消息循环行为未验证。
- 开机自启用 `HKCU\...\Run` + `pythonw.exe` + 直接跑 `voicetype_win\__main__.py`
  （`__main__.py` 里做了 `sys.path` bootstrap，直接跑也能 `import voicetype_win`）。
- 音频设备用系统默认输入（`sounddevice` 默认设备）；多麦克风时需要手动指定。

## 与 macOS 版的对应关系

| macOS (`app/Sources`) | Windows (`client-windows/voicetype_win`) |
|---|---|
| `main.swift` (`AppDelegate`) | `__main__.py` (`Controller` / `App`) |
| `Hotkey.swift` (`FnHotkey`) | `hotkey.py` (`LowLevelKeyboardHook`) — 双击 **右 Ctrl** |
| `Recorder.swift` | `recorder.py`（同样 16kHz 单声道 16-bit） |
| `HUD.swift` | `hud.py`（tkinter 胶囊 + `WS_EX_NOACTIVATE`） |
| `EngineClient.swift` | `engine_client.py`（unix socket → loopback TCP） |
| 辅助功能权限 | 无需授权，但可能需要管理员权限才能粘进管理员窗口 |

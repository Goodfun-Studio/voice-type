# Voice Type

本机语音转写：说完整句，字直接落进当前光标。识别全程本地跑，**不联网、不留云端副本**。

macOS 版已可用；Windows 版客户端代码在 `client-windows/`，**尚未在 Windows 上实机验证过**。

## 怎么用

1. **双击 Fn**（备选 ⌥空格）→ 屏幕下方出现电平条，开始说话
2. **再双击 Fn** → 停录，约 1 秒后文字自动敲进你正在编辑的地方
3. 原文同时留在剪贴板，随时 ⌘V 备用
4. 说错了想反悔 → **按 Esc**：音频当场丢掉，**不转写、也不粘贴**
   （转写的那 1 秒内按 Esc 也有效，结果会被丢弃）

菜单栏麦克风图标：切换语言（中文 / English / 自动识别）、重启引擎、看日志、看历史。
历史记录按天落在 `~/Library/Application Support/VoiceType/history/`。

## 首次运行

要去 系统设置 → 隐私与安全性 给 VoiceType 两个权限：

- **辅助功能** —— 监听 Fn 按键 + 模拟 ⌘V 粘贴（不给的话字只会进剪贴板）
- **麦克风** —— 录音

启动时会自动弹授权框，把它打开即可。

## 实测

| 场景 | 结果 |
|---|---|
| 7 秒中文语音 | 1.0 秒出字（7 倍实时） |
| 20 秒中文语音 | 1.6 秒出字（12.8 倍实时） |
| 模型加载（冷启动） | 2 秒，之后常驻内存 |

## 术语表

`~/Library/Application Support/VoiceType/vocabulary.txt`，一行一个词：

```
识鱼
鱼缸
抠图
```

两件事会用它：

1. 喂进解码器当上下文提示；
2. 更管用的那半 —— **按拼音纠同音误听**。whisper 常把「识鱼」听成更常见的「食鱼」，
   这里按音节匹配回填。代价是：如果你真说了个和表里词完全同音的普通词，也会被改写。
   所以只放确实要保住的专有名词。

## 装 / 重装

```bash
./install.sh     # python 环境 + 模型 + 编译 app + 开机自启
./test.sh        # 不需要麦克风的端到端自检（合成一句话再转写）
./status.sh      # 现在跑没跑、引擎响应没响应
```

网络注意（这台机器实测）：

- `huggingface.co` 连不上 → 模型走 `hf-mirror.com`，引擎启动时会自己探测并切换
- `pypi.tuna.tsinghua.edu.cn` 对部分 wheel 返回 403 → 用阿里云镜像
- launchd 启动的进程 PATH 很干净，引擎自己会把 `/opt/homebrew/bin` 加回去

## 结构

```
engine/engine.py       常驻转写守护进程：传输 + 闸门 + 清理 + 术语纠正
engine/backends.py     识别后端抽象：MLX（Apple）/ faster-whisper（跨平台）
engine/cleanup.py      规则清理 + 标点全角化 + 术语同音纠正
models/                whisper-large-v3-turbo（MLX，1.5G）
app/Sources/*.swift    macOS 菜单栏 app
client-windows/        Windows 客户端（Python，未实机验证）
```

**分层是刻意的**：清理规则、幻觉过滤、术语表这些"业务逻辑"全在引擎里，跟平台无关；
两个客户端只是各自的"壳"（热键、录音、悬浮条、粘贴）。换平台只重写壳。

### 引擎

引擎常驻内存，模型只加载一次，由客户端在需要时拉起。

```bash
python engine.py --serve                      # macOS 默认走 unix socket
python engine.py --serve --transport tcp --port 8765   # Windows 走本机 TCP
python engine.py --once file.wav zh           # 单文件跑一次，方便排查
```

启动后会把怎么连它写进 `<配置目录>/engine.json`，客户端读它就行，不用猜传输方式或端口：

```json
{"transport":"tcp","backend":"faster-whisper","pid":123,"host":"127.0.0.1","port":8765}
```

协议就是一行一个 JSON，请求一条回一条：

```
{"cmd":"ping"}                                    → {"ok":true,"backend":"mlx","warm":true}
{"cmd":"transcribe","wav":"/tmp/x.wav","language":"zh"}
                                                  → {"text":"...","elapsedMs":995,"rtf":0.14}
```

`text` 为空但带 `reason` 是**正常结果**（`silence` / `hallucination` / `no-words` / `prompt-echo`），
客户端不要当错误、更不要粘贴空串。

### 识别后端

| 后端 | 用在哪 | 模型 | 推理耗时 | RTF |
|---|---|---|---|---|
| `mlx` | Apple Silicon（默认） | large-v3-turbo | 1.37s | **0.19** |
| `faster-whisper` | Windows / Linux / Intel Mac | small int8 · CPU | 2.12s | **0.30** |
| `faster-whisper` | 同上 | medium int8 · CPU | 5.87s | 0.83 |
| `faster-whisper` | 同上 | large-v3-turbo int8 · CPU | 7.72s | 1.09 |

（7 秒中文音频，Apple M4。RTF < 1 = 比实时快。）

**这张表决定了 Windows 该配什么机器**：CPU 上 **只有 `small` 能用**（约 3 倍实时）；
`medium` 已经接近实时（RTF 0.83），`large-v3-turbo` **比实时还慢**（1.09）—— 听写时你会等它
一个字一个字往外挤。而 M4 的单核性能比大多数 Windows 笔记本强，所以真实机器上还要再打折。
**结论：Windows 上没 NVIDIA 独显就用 `small`；想要大模型的准确率，必须上 GPU。**

切后端：`VOICETYPE_BACKEND=faster-whisper`；没有 GPU 时默认 `small` + int8，
有 NVIDIA 卡时自动切 `large-v3-turbo` + float16。**别为了省 CPU 把 beam 调到 1**：
实测 beam=1 时模型会把提示词原样复读出来（引擎有护栏会丢弃，但白说一句）。

## 设计取舍

- **引擎选 MLX Whisper large-v3-turbo**：M4 走 Metal，实测 7～13 倍实时；中文标点稳。代价是常驻约 2G 内存。
- **录音直接落 16kHz 单声道 WAV**：whisper 要的就是这个格式，省一次转码。
- **强制简体**：whisper 对普通话经常吐繁体（实测不加提示就是繁体）。用 initial_prompt 把解码器拽到简体 + 标点。
- **清理走规则不走模型**：毫秒级、零成本、结果可预测。规则写得很保守——拿不准就不动，宁可留个「呃」也不改句子。
- **粘贴用 ⌘V 模拟**：比逐字敲进去了事，中文不出乱码，也不污染撤销栈。

## 跨平台的坑（都实测过）

- **`av` 必须钉版本**：faster-whisper 声明 `av>=11` 却没有上限，而 av 19 移除了 `metadata_errors`
  参数，装上 av 19 会直接崩。Windows 依赖见 `engine/requirements-windows.txt`。
- **huggingface.co 在部分网络不可达**：引擎启动时会探测，不通就自动切 hf-mirror。
- **Windows 上没有 Fn 键**：Fn 是笔记本 EC/固件层的，系统收不到它的按键事件，
  所以 Windows 版用**双击右 Ctrl**取代双击 Fn。
- **Windows 的权限模型不一样**：没有 macOS 的"辅助功能授权"那套，SendInput 默认可⽤；
  但如果目标程序以管理员运行，客户端也必须提权才能往里打字。

## 卸载

```bash
launchctl unload ~/Library/LaunchAgents/com.goodfunstudio.voicetype.plist
rm -rf ~/Library/Application\ Support/VoiceType ~/Library/LaunchAgents/com.goodfunstudio.voicetype.plist
```

## 两个坑（都是 macOS 系统层面，不是工具本身）

**双击 Fn 弹出表情面板**：Fn/🌐 键默认被系统绑成「显示表情与符号」，双击就是这个动作。
已把 `AppleFnUsageType` 设为 `0`（Do Nothing）解决。还原：
系统设置 → 键盘 → 「按下 🌐 键时」。表情面板快捷键 ⌃⌘空格 不受影响。

**文字只进剪贴板、不敲进光标**：模拟 ⌘V 需要「辅助功能」权限。没权限时菜单栏图标是 ⚠️，
点它有直达设置页。授权后 app 每 2 秒自查，不用重启。

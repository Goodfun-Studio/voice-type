# Voice Type

本机语音转写：说完整句，字直接落进当前光标。识别全程本地跑，**不联网、不留云端副本**。

## 怎么用

1. **双击 Fn**（备选 ⌥空格）→ 屏幕下方出现电平条，开始说话
2. **再双击 Fn** → 停录，约 1 秒后文字自动敲进你正在编辑的地方
3. 原文同时留在剪贴板，随时 ⌘V 备用

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
engine/engine.py      常驻转写守护进程（unix socket，一行一个 JSON）
engine/cleanup.py     规则清理 + 标点全角化 + 术语同音纠正
models/               whisper-large-v3-turbo（MLX，1.5G）
app/Sources/*.swift   菜单栏 app：Fn 双击、录音、悬浮电平条、自动粘贴
app/build.sh          编译到 bin/VoiceType.app
```

引擎常驻内存，模型只加载一次；app 启动时若发现引擎没起来会自己拉起来。

```
{"cmd":"ping"}                                    → {"ok":true,"warm":true}
{"cmd":"transcribe","wav":"/tmp/x.wav","language":"zh"}
                                                  → {"text":"...","elapsedMs":995,"rtf":0.14}
```

## 设计取舍

- **引擎选 MLX Whisper large-v3-turbo**：M4 走 Metal，实测 7～13 倍实时；中文标点稳。代价是常驻约 2G 内存。
- **录音直接落 16kHz 单声道 WAV**：whisper 要的就是这个格式，省一次转码。
- **强制简体**：whisper 对普通话经常吐繁体（实测不加提示就是繁体）。用 initial_prompt 把解码器拽到简体 + 标点。
- **清理走规则不走模型**：毫秒级、零成本、结果可预测。规则写得很保守——拿不准就不动，宁可留个「呃」也不改句子。
- **粘贴用 ⌘V 模拟**：比逐字敲进去了事，中文不出乱码，也不污染撤销栈。

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

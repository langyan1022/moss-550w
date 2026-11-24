# HoloJarvis 整体功能和二开评估

2026 年 11 月 21 日 · Windows 11 · 保留 550W 界面与现有 OmniVoice 音色

建议继续把 HoloJarvis 作为对话内核和功能实现的上游，以 MOSS 的服务作为日常启动入口。可以逐项接齐 HoloJarvis 面向用户的能力。直接采用上游的主程序会把现有播放器、字幕频谱、审批恢复和模型配置的迁移一起变成上线条件。现阶段可以用相同的 550W 与音色在独立实例检验需要直接沿用的模块。

## 当前工程沿用了哪些源码

MOSS 从 `chat.ps1` 启动 `prototype/serve.py`。对话使用由上游 `jarvis/brain.py` 改写的 `holojarvis_runtime/brain.py`。实际沿用流式对话、工具 schema 转换、增量工具调用合并及调度；模型传输、权限范围、SQLite 会话、UI、播放器与语音由 MOSS 提供。草稿批准与重启续办继续用 Pydantic AI Slim。Jev Windows 的拟回复是另一项接入，源项目定位为聊天辅助。

本次用 GitHub 的公开 Git 引用核对两边主分支。HoloJarvis 为 `7473f91e755b3683173de025c5ee32032dbdd75a`，与正在使用的内核上游一致。Jev Windows 为 `c6d3a10474ce1ff3b7f8f6402b3b5e0e58fad5f1`。HoloJarvis 使用 MIT 许可。引用的源代码均固定到提交。

[当前启动入口](../chat.ps1) · [当前内核来源](../prototype/holojarvis_runtime/README.md) · [HoloJarvis 源码与许可](https://github.com/wqq64842-commits/holojarvis/tree/7473f91e755b3683173de025c5ee32032dbdd75a) · [Jev Windows](https://github.com/jev-chat/jev-chat-windows/tree/c6d3a10474ce1ff3b7f8f6402b3b5e0e58fad5f1)

## 17 项内置工具

按原仓工具的用途分类。11 月 21 日 10:00 发布 Windows 系统媒体会话后，已有 14 项日常用途对应入口，另外 2 项部分覆盖，并有 1 项尚无对应的发送入口。上游的 17 项 schema 与其实际分发表已经在本机对照。

| 原仓工具 | MOSS 现状 | 接入决定 |
| --- | --- | --- |
| `open_app`、`open_url`、`web_search` | 三项都有对应入口。Windows 应用按已安装名称解析，网址和显式网络搜索走浏览器。 | 保留现有入口，让本地媒体先查本机和 NAS。 |
| `set_volume`、`get_time`、`get_weather` | 三项都有。天气支持区县、经纬度、日期追问。 | 保留原有查询及中文输出，避免常用操作等待模型。 |
| `set_timer` | 支持延时、指定时间、查询、取消，并集成 Windows 通知。 | 保留重启恢复及提醒去重。 |
| `take_screenshot` | 浏览器内选择窗口、预览和保存。 | 沿用用户选窗，截图交给明确授权的后续动作。 |
| `system_power`、`move_to_trash` | 两项都有确认入口，分别处理锁屏或睡眠、回收单个本地文件。确认及取消可恢复，锁屏和睡眠实查到确认流程。 | 支持从文件整理预览点名归类一份。多文件整理继续先显示变化清单。实际设备状态变化需验证。 |
| `remember`、`forget`、`list_directory` | 三项都有。记忆可按项更正、撤回或到期，目录支持本机与 UNC 路径。 | 以 SQLite 的现有记忆和授权范围为准。 |
| `control_music` | 已覆盖日常控制。MOSS 可定位曲目、联动字幕频谱并核对自己播放器的实际回执。也支持明确指令操作 Windows 当前媒体会话。 | 分开测试系统播放器和本机曲库，实际回执区分收到指令与状态变化。`双播放器验收` |
| `read_screen` | 选窗 OCR、选文问答、摘要和窗口画面问答均已接入。画面问答从用户选定的窗口或标签页取得一帧，预览并确认后交给百炼 `qwen4-max`。 | 实际百炼请求正确识别无文字合成图中的方位和数量。界面只在确认后提交本轮截图和问题。图片及视觉问答不进入普通模型历史。`真实视觉验收` |
| `run_shell` | MOSS 可预览用户明确提供的 PowerShell，确认后执行并读回结果。也可以说「按类型整理这个目录，先给我看计划」，直接查看本机或共享目录的分类建议、去向和冲突。 | 从本机预览点名归类一份时，确认后执行同卷、不覆盖的文件移动并读回身份。PowerShell 继续走现有确认流程。 |
| `send_wechat` | 拟回复、选取来信、复制和填入 MOSS 自带的 Windows 草稿窗均有入口。 | 草稿填入遵循用户确认、目标核对和逐字读回。接入独立聊天应用时，分别验收其会话标识、草稿控件以及应用版本变化后的拒写行为。 |

[17 项注册及执行](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/tools.py) · [原仓工具调用调度](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/brain.py) · [Windows 媒体与微信实现](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/winops.py) · [当前 MOSS 日常入口](../prototype/README.md)

## 原仓还有哪些交互能力

| 能力 | 原仓实现 | 保留 550W 后的接入方式 |
| --- | --- | --- |
| 桌面常驻、点击唤醒、拼音容错 | Windows 用 `sounddevice` 连续录音，Whisper 在本机识别唤醒，唤醒后可选讯飞转写。主循环还处理播报和工具调用。 | 已接托盘闭页唤醒和可选的下一句接听。真实 Windows、Edge 与 SenseVoice 用录音夹具验证了闭页冷启、前台单持有、指令提交前释放麦克风和缓存播报。实体麦克风、远场误唤醒与 Windows 默认浏览器仍待验收。 |
| 逐句播报 | 模型按句交给阻塞的 `tts.speak()`。原仓支持 GPT-SoVITS、XTTS 或系统朗读。 | 现有 OmniVoice 补丁已在独立原仓实测。生产沿用浏览器播放、字幕时序、频谱和打断，通过一套语音接口取音频，保留现有参考音色和诗词读法。 |
| 桌宠 HUD、笔记和遥测 | Tk HUD 提供时钟、天气、CPU、电量、磁盘、对话字幕、笔记。浏览器 HUD 通过 SSE 接收事件。 | 550W 主窗口直接显示当前时间、CPU、可用内存、电池和系统盘可用量，也可打开置顶小窗查看详细读数。主窗与 Edge 原生画中画共用一次本机采样，主窗最小化后小窗继续更新。便签和按需天气保持独立入口。 `主窗` · `原生小窗` |
| 手势与 3D 形态 | MediaPipe 在浏览器识别张手、握拳、捏合和双手张合，处理结果直接改变粒子形态。 | 日常 550W 设置已接入「手势切换机位」。手动启用后，竖食指切到下一机位，收手后可再次触发。真实 Edge 和 550W 场景采用模拟摄像头复验，实体摄像头待验。`手势入口验收` |
| 模型、长期记忆和多步动作 | 兼容 OpenAI 的单一地址；内存保存对话，`memory.json` 保存事实。工具可在一个回答中继续调用，敏感操作多靠提示词约束。 | 保留 DeepSeek、百炼、Kimi、按需本机 Qwen 和 SQLite。引入固定范围的多步计划，把可执行动作交给现有确认和回执，避免模型自行认定写入、播放或发送结果。 |
| MCP 工具 | 配置的 stdio 服务器在启动时打开，发现的工具进入模型常规工具列表。 | MOSS 已经验证本机媒体、通用只读工具和自然话术调用。可扩展让模型按本轮任务查找已授权工具，之后按权限执行、取消并回收；副作用工具接确认。 |
| Jev 的聊天辅助 | 独立 Windows 助手，捕获指定聊天窗、本机 OCR、会话去重、候选排序和填入，发送由用户操作。 | 继续以 Jev 补聊天辅助，避免把常驻采集嵌入 MOSS 的普通对话。当前已接选窗与手动拟回复。隔离 Windows 编辑框已验证填入和读回，微信本身尚待适配。 |

[主程序](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/__main__.py) · [语音配置](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/config.py) · [桌宠与遥测](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/pet.py) · [浏览器事件](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/holo/bridge.py) · [手势](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/holo/index.html) · [MCP 桥](https://github.com/wqq64842-commits/holojarvis/blob/7473f91e755b3683173de025c5ee32032dbdd75a/jarvis/mcp_bridge.py) · [Jev Windows 源码](https://github.com/jev-chat/jev-chat-windows/tree/c6d3a10474ce1ff3b7f8f6402b3b5e0e58fad5f1)

## 整仓二开会牵动哪些关键路径

1. **会话和工具事件。** 上游的浏览器 SSE 传语音状态、字幕、日志和遥测。550W 同时依赖对话和轮次标识、媒体命令、真实播放器回执、待确认操作、取消及重启恢复。直接换主程序需要重接这些事件并保留同一份数据。
2. **首段、首声与开播。** 上游每次自然语言请求进入模型，常规请求带上 17 个工具 schema 和已启用的 MCP 工具。MOSS 的报时、固定操作、点歌和本地查询可绕过模型。可独立复用上游的拼音匹配、摄像头识别模块和逐句切分。用同一台机器、同一模型比较首段、首声、出站次数与持续资源占用，才可判断主程序是否值得切换。
3. **后台运行。** 上游 Windows 支持是真实实现，README 标注 Python 3.12。其启动需要模型地址和密钥，语音循环会初始化本机识别，随系统登录启动仍在上游路线图。MOSS 日常实例使用锁定的 Python 3.11 和 SQLite。后台唤醒可作为独立进程，用本机接口连接当前会话，避免为了后台能力迁移播放器、会话和音色。

## 现有实测边界

- 在 Windows 11 的 Python 3.12.13 上直接载入未修改的原仓源码，确认 17 个工具注册及分发表一致。原仓报时和系统时钟一致，读出了隔离目录中的中文文件名。CPU、电池、系统启动时间和磁盘空间由实际 Win32 或文件系统读取。此次针对原仓的核查用了 0 次云模型请求。`原仓 Windows 核查`
- 独立原仓已经通过本机 HTTP 复用 MOSS 的 OmniVoice。动态句生成后通过 SenseVoice 和静音 Edge 核验。把原仓直接接管 550W 的完整播放、取消、记忆与确认流程尚无整套对照结果。`音色接入与实测`
- 上游便签、设备状态和选窗文字问答已在日常 MOSS 上线。设备状态 9 次 HTTP 复验通过，窗口问答用两次实际 DeepSeek 请求，便签通过浏览器和重启备份恢复。`日常发布`
- 关闭浏览器和服务后，隔离实验用真实音频、SenseVoice、Windows 命名管道和 Edge，恢复到了原会话。重复触发复用同一标签。电脑实体麦克风、长时间监听及真实前台焦点还未验收。WPF 隔离窗口可通过 Windows UI Automation 接受文字并读回，错窗和人工草稿会拒绝写入。测试没有操作个人微信。`闭页接力` · `合成窗口填入`
- Windows 自带中文识别器在限词唤醒实验中接受了两份称呼，拒绝了六份音频，进程工作集约 34 MiB。两份正例来自同一合成音，样本仍需用真实麦克风、不同说话距离和背景声扩充。`唤醒候选验证`

下一项验收是在独立 Windows 实例接上常驻唤醒入口，用同一份 550W、OmniVoice 和测试会话完成一次唤醒、一次点歌、一次打断及一次重复唤醒。这组用例也能比较整仓主程序与模块接入各自需要改动的接口。

# MOSS 550W · Local-First Desktop & LAN AI Companion

MOSS 550W 是一个以**本地运行与隐私安全**为核心的个人 AI 伴侣与 3D 科幻控制台原型，集成了**本地 Qwen 推理与多云端故障转移**、**Soul / SAG / RSI 长期记忆系统**、**OmniVoice 拟人语音合成与 SenseVoice 本地识别**、**Three.js WebGL 550W 实时机身与游戏级 FUI**、**本地/NAS/Navidrome/开源曲库融合播放**、**光学摄像头接入与手势控制**以及**局域网 HTTPS 4 位授权门禁（CS/C 权限分级）**。

> **仓库范围说明 / Repository Scope**
> 本公开仓库仅保留**最终验证通过并实际采用的可运行代码、运行时静态资源与架构评测文档**。实验过程中的废弃对比框架（`alternatives/`、`langgraph/`、`nanobot/`、`pydantic/`）、运行时数据库与聊天记录（`prototype/data/`）、私人媒体库索引（`prototype/local_media/data/`、`sources.json`）、本地临时运行产物（`runs/`、`logs/`）以及所有云端/服务密钥均已通过 `.gitignore` 严格隔离，**不包含任何个人隐私数据或 API 密钥**。

---

## 核心能力与设计亮点

- **混合推理引擎（Local + Cloud Failover）**：基于选型实测采用轻量的 Pydantic AI Slim（`pydantic-ai-slim[openai]==2.71.0`），支持本机 `llama-server`（CUDA / Vulkan / CPU）及阿里云百炼、DeepSeek、Moonshot Kimi 等兼容 OpenAI 接口的云端模型，支持工具并行调用、中断安全取消与云端静默重试/故障切换。
- **Soul / SAG / RSI 三层长期记忆**：本地 SQLite 单一事实源（SSoT）维护记忆条目、画像特征、情景索引与自我改进规则；内置敏感密钥与凭证自动脱敏机制（导出与写入归档前剥离常见 Token 与密钥形态）。
- **语音链路与智能麦克风切换**：对接独立 OmniVoice 语音克隆后端与 SenseVoice ASR；前端 `mic-selector.js` 根据设备类型（独立 USB 麦 / 阵列麦 / 蓝牙 HFP / 虚拟混音陷阱）自动评分选优并支持热拔插实时无缝接管。
- **融合音乐与媒体管道**：
  - 本地 / SMB NAS 媒体库（支持内嵌/外挂字幕全文检索及视频帧文字 OCR 检索）。
  - 融合在线曲库中心（`online_music.py`）：统一路由私有 **Navidrome**（Subsonic 协议 + Salt/MD5 Token 鉴权 + DSF/AIFF 等浏览器不支持格式自动服务端转码流式代理）、**Audius**、**Internet Archive**、**Radio Browser** 实时电台、可选 **Jamendo**（需用户自行填入 Client ID）以及 **LRCLIB** 同步/纯文本歌词缓存。
  - 所有外部公共音频代理均强制执行 **DNS 钉扎防重绑定与 SSRF 校验**（拦截私有网段、回环与 CGNAT 地址），私有 Navidrome 凭证使用 **Windows DPAPI** 本机加密落盘。
- **视觉接入、手势切换与 FUI**：支持 3D 550W 机位漫游、摄像头光学画面采集（CAM-IN）与采集卡面板一键开启 MediaPipe 五种手势识别（流克隆复用，不抢占视频轨）。
- **局域网安全与权限分级（Auth Gate）**：支持 `--lan-host` + `--tls-port` 动态签发自签名 SAN HTTPS 证书以解锁局域网麦克风/摄像头；提供 4 位授权码登录与 **CS（管理员）/ C（普通访客）** RBAC 隔离，C 级访客无法读写系统模型、媒体库、在线曲库或 Home Assistant 配置。

---

## 目录结构

| 路径 | 用途 |
| --- | --- |
| [`prototype/`](./prototype/README.md) | MOSS 550W 主运行时代码、Web FUI、媒体库、记忆引擎与全链路验证套件 |
| [`chat.ps1`](./chat.ps1) / [`打开MOSS.cmd`](./打开MOSS.cmd) | Windows 一键启动 / 停止与环境同步脚本 |
| [`architecture/MOSS架构评估.md`](./architecture/MOSS架构评估.md) | 个人助手底座框架评估与能力边界分析 |
| [`框架选型实测.md`](./框架选型实测.md) | Pydantic AI Slim / LangGraph / nanobot 工具执行、取消与重启恢复实测对照 |
| [`architecture/MOSS技术演进路线.md`](./architecture/MOSS技术演进路线.md) | 从原型到 v5.5+ 的全量迭代记录与实测数据归档说明 |
| [`architecture/MOSS下一阶段演进规划_v5.6-v6.0.md`](./architecture/MOSS下一阶段演进规划_v5.6-v6.0.md) | `v5.5+` 开源后深度复盘与 `v5.6` → `v6.0` 分阶段演进规划 |
| [`architecture/MOSS本机语音候选比较.md`](./architecture/MOSS本机语音候选比较.md) | 本机 TTS / ASR / 打断与 Turn-Taking 候选方案实测对比 |
| [`architecture/HoloJarvis整体二开评估.md`](./architecture/HoloJarvis整体二开评估.md) | HoloJarvis 运行时与 550W FUI 融合改造评估 |
| [`architecture/MOSS实体硬件与HomeAssistant对接协议.md`](./architecture/MOSS实体硬件与HomeAssistant对接协议.md) | Home Assistant 与硬件遥测/控制 HTTP 接口约定 |
| [`architecture/MOSS日常验收标准.md`](./architecture/MOSS日常验收标准.md) | 响应时延、语音卫生、热拔插与回执准确度验收基准 |
| [`architecture/MOSS交互失败回放.md`](./architecture/MOSS交互失败回放.md) | 历史典型故障复盘与防回归规则 |
| [`run_lab.py`](./run_lab.py) / [`run.ps1`](./run.ps1) / [`summarize.py`](./summarize.py) | 选型对比基准脚本与数据汇总工具 |

---

## 快速开始（Windows）

### 1. 环境准备

- **Python / 包管理**：Windows 11 + [`uv`](https://docs.astral.sh/uv/)（工程锁定 `Python 3.11.15`）
- **浏览器**：Microsoft Edge（推荐，FUI 与 Playwright 自动化验收基于 Edge）
- **可选外部能力**：
  - 本机 GGUF 推理：`llama-server`（例如 LM Studio Vulkan/CPU 后端）
  - 私人音乐服务器：[Navidrome](https://www.navidrome.org/)（Subsonic API）
  - 高保真拟人语音（OmniVoice TTS）：独立本地服务目录（可通过环境变量 `VOICESTUDIO_ROOT` 指定，默认见源码说明）

```powershell
# 进入 prototype 目录检查或同步锁定依赖
cd prototype
uv sync --frozen --python 3.11.15
```

### 2. 一键启动服务

```powershell
# 在仓库根目录启动桌面服务并打开浏览器（默认 http://127.0.0.1:18221/）
.\chat.ps1 -Open

# 停止后台服务
.\chat.ps1 -Stop
```

如需局域网访问并启用麦克风 / 摄像头手势（需 HTTPS）：

```powershell
cd prototype
.\.venv\Scripts\python.exe -u serve.py --lan-host 192.168.1.100 --tls-port 18443
```

### 3. 安全配置 API Key 与私人曲库（严禁写入仓库）

所有敏感配置均保存在**仓库外或 `.gitignore` 忽略目录**中，**切勿提交到 Git**：

1. **云端模型 API Key**：启动后打开页面 **设置 → 模型与提供商** 添加并保存（加密/持久化写入本机 `%LOCALAPPDATA%\MOSS-550W\chat-providers.json`）。
2. **本地媒体目录**：复制 [`prototype/local_media/sources.example.json`](./prototype/local_media/sources.example.json) 为 `prototype/local_media/sources.json`（已被 `.gitignore` 忽略），或直接在网页 **设置 → 媒体库** 添加本地/NAS 路径。
3. **Navidrome 与开源在线曲库**：在网页 **设置 → 语音与音频 → 在线开源曲库与私有 Navidrome** 中填写地址、账号密码或可选 Jamendo Client ID；密码与 Key 经本机 DPAPI 加密后存入 `prototype/data/online-music.json`（不入仓，前端 GET 永不回传明文）。

---

## 验证与质量门禁

详细命令与分模块单元测试说明请参阅 [`prototype/README.md`](./prototype/README.md)。运行静态与单元门禁（无需等待语音显卡时可只跑 `static,unit`）：

```powershell
cd prototype
.\.venv\Scripts\python.exe -u release_gate.py --stages static,unit
```

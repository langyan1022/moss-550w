# MOSS 550W Prototype Runtime

本目录包含 MOSS 550W 的完整服务后端、3D WebGL FUI 控制台、记忆引擎、媒体/在线曲库网关、硬件与 Home Assistant 桥接以及自动化门禁测试套件。

---

## 核心模块一览

| 模块 / 目录 | 职责说明 |
| --- | --- |
| [`serve.py`](./serve.py) | `aiohttp` 主 HTTP/HTTPS + SSE 服务，托管 FUI、媒体流（HTTP Range）、授权门禁（CS/C RBAC）、SSRF 防护流代理及优雅退出 |
| [`core.py`](./core.py) | 核心对话编排：本地/云端路由、记忆读写、桌面动作二次确认、媒体路由与 RSI 串行化蒸馏锁 |
| [`assistant_brain.py`](./assistant_brain.py) | Pydantic AI Slim 工具调度封装、并行工具执行、云端切换与上下文管理 |
| [`soul_memory.py`](./soul_memory.py) | Soul / SAG / RSI 三层长期记忆存储、衰减/强化、BM25 检索与敏感信息脱敏导出 |
| [`online_music.py`](./online_music.py) | 融合在线曲库中心：Navidrome（Subsonic）、Audius、Internet Archive、Radio Browser、Jamendo 与 LRCLIB 歌词缓存；内建 `resolve_public_url` + 钉扎解析器防 DNS 重绑定与内网 SSRF |
| [`media_chat.py`](./media_chat.py) & [`local_media/`](./local_media/README.md) | 本地与 SMB2 NAS 曲库/视频/字幕检索、口语点歌解析与只读 MCP 服务 `mcp_media.py` |
| [`music_prefs.py`](./music_prefs.py) | 听歌偏好统计、共有 LRC 解析器 `parse_lrc_text` 与睡眠定时状态 |
| [`voice_input.py`](./voice_input.py) & [`voice_output.py`](./voice_output.py) | SenseVoice 本地语音识别、OmniVoice 合成调度、首响预热与超前切片拼接 |
| [`console_settings.py`](./console_settings.py) & [`auth_gate.py`](./auth_gate.py) | 模型/提供商持久化（`%LOCALAPPDATA%\MOSS-550W\`）、媒体源热重载与 4 位授权码身份校验 |
| [`iot_bridge.py`](./iot_bridge.py) & [`holo_kernel.py`](./holo_kernel.py) | Home Assistant 服务调用、实体硬件遥测及云端静默重试封装 |
| [`fui/public/`](./fui/public/index.html) | 3D 550W WebGL 控制台、FUI HUD 弹窗、光学摄像头与 MediaPipe 五手势控制（含摄像头采集面板快捷开启入口 `#rail-cam-gesture`）、智能实时最佳麦克风选择与切换 (`js/mic-selector.js`) |

---

## 安全与隐私设计

1. **零仓库凭据**：云端 LLM API Key 保存在 `%LOCALAPPDATA%\MOSS-550W\chat-providers.json`；Navidrome 密码、Jamendo Client ID 与 Home Assistant Token 经过 DPAPI 加密后保存在 `prototype/data/`（已被 `.gitignore` 忽略），API 读取配置时仅返回布尔标志（如 `has_password`）。
2. **授权隔离（CS vs C）**：开启局域网授权门禁时，所有敏感 GET/POST 系统配置端点（提供商管理、媒体源管理、在线曲库配置与探活、IoT 配置、Soul 修改）严格限定 `CS` 管理员；访客（`C` 级）仅可调整工牌、定位展示、频谱样式与陪伴语气，且未登录时阻止通过 `Esc` 或点击遮罩关闭必需的授权门禁弹窗。
3. **SSRF 与流媒体防护**：
   - `/api/online-music/stream` 对公共音源统一验证 `http`/`https` 协议，阻断环回地址、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16`（元数据）及 `100.64.0.0/10`（CGNAT），并在真正建连前采用 DNS 钉扎解析器（`_PinnedResolver`）避免 TOCTOU 重绑定；遇到 `.m3u/.m3u8/.pls` 播放列表时会在服务端逐跳二次校验目标 URL。
   - 遇到仅本地配置私有 Navidrome（默认禁止公网入口泄露）时，显式允许配置的 Navidrome 主机走内网流式代理，并会在切换主机或用户名时清空旧密文凭证，防止凭证意外重放给新主机。

---

## 依赖安装与启动

### 1. 同步虚拟环境

```powershell
cd prototype
uv sync --frozen --python 3.11.15

# 如需运行 Edge/Playwright UI 与 SMB 隔离介质回归测试，请附带可选分组：
uv sync --frozen --group browser --group media-test --python 3.11.15
```

### 2. 运行服务

```powershell
# 本地标准启动（HTTP 18221）
.\.venv\Scripts\python.exe -u serve.py

# 局域网协同启动（并行监听局域网 IP + 自动签发 HTTPS SAN 证书端口 18443）
.\.venv\Scripts\python.exe -u serve.py --lan-host 192.168.1.100 --tls-port 18443
```

### 3. 本地媒体与在线音乐配置

- **本地音乐示例**：将 [`local_media/sources.example.json`](./local_media/sources.example.json) 复制为 `local_media/sources.json` 并填入你的媒体文件夹，或在控制台中通过 **设置 → 媒体库** 新增。
- **在线曲库与 Navidrome**：在 Web UI 打开 **设置 → 语音与音频 → 在线开源曲库与私有 Navidrome（融合接入）**；可按需开启 Navidrome（Subsonic）、Audius、Internet Archive、Radio Browser、LRCLIB 与 Jamendo。
- **手势控制快捷入口**：展开右侧 **光学采集 / 摄像头画面** 卡片，可直接点击顶栏 **手势** 开关复用同一摄像头视频流驱动 3D 机位/形态交互。

---

## 自动化验证套件

> 所有验证脚本均支持完全离线隔离（使用临时 SQLite 库、Mock 上游或无声 Edge），不会污染个人日常对话记录或真实外网服务。

### 单项回归

```powershell
cd prototype
$env:PYTHONUTF8 = '1'

# 在线融合曲库（意图过滤、SSRF 拦截、DPAPI、Range、RBAC）
.\.venv\Scripts\python.exe verify_online_music.py

# v5.5 核心能力（并行工具、云端 Failover、文档提取、RBAC、语音进程回收）
.\.venv\Scripts\python.exe verify_v55_units.py

# Soul / SAG / RSI 长期记忆、脱敏、Sassy 预设与麦克风热备选算法
.\.venv\Scripts\python.exe verify_soul_memory.py

# 本地媒体与点歌管道回归
.\.venv\Scripts\python.exe verify_media_changes.py
```

### 全量 4 阶段发布门禁

包含：Stage 1 静态语法 / JS 契约 / EN-ZH 词条严格对齐 / FUI Lint / 3D 模型清单校验；Stage 2 黄金样本与单元回归；Stage 3 语音延迟对照与显卡预热；Stage 4 无声 Edge 全点击端到端 UI 与热拔插验收。

```powershell
# 完整 4 阶段（需 Edge 浏览器；Stage 3 语音检查会调用本地 VoiceStudio 环境如已安装）
.\.venv\Scripts\python.exe -u release_gate.py --stages static,unit,audio,ui --tag dev-check

# 无 GPU 语音环境时仅运行静态 + 单元
.\.venv\Scripts\python.exe -u release_gate.py --stages static,unit
```

> 本地 FUI 辅助审查脚本位置可通过环境变量 `MOSS_FUI_LINT`、`MOSS_FUI_PROBES` 与 `MOSS_FUI_SCRIPTS` 自定义（默认回退到 `~/.gemini/config/skills/wandering-earth-fui-design/scripts/`）。

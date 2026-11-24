# MOSS 550W · Wandering Earth FUI Console (`F:\moss-fui`)

独立于 `F:\moss` 之外重写的电影级 FUI 控制台，基于 `wandering-earth-fui-design` 技能规范构建。不修改 `F:\moss` 内任何文件，通过本地反向代理 (`http://127.0.0.1:18331`) 无缝接入现有 MOSS 后端 (`http://127.0.0.1:18221`)，同时内置完整离线演练态。

## 启动方式

- **一键双击启动**：运行 `打开MOSS-FUI.cmd`（自动拉起或复用 `F:\moss` 后端 18221，启动 FUI 代理 18331 并打开浏览器）。
- **PowerShell 启动**：
  - 仅启动 FUI 控制台并连接已有后端：`.\start.ps1 -Open`
  - 离线演示模式（不连 18221）：`.\start.ps1 -Offline -Open`
  - 停止 FUI 服务：`.\start.ps1 -Stop`

## 架构与核心特性

1. **零空洞三栏 FUI 控制台（`.fui-console`）**：
   - **左栏**：550W 六机位模块矩阵（`.fui-matrix`）+ 方位/俯仰读数 + 本机硬件分段仪表（CPU/内存/电量/系统盘，带真实 warn/crit 阈值）+ 点阵地球仪（`fui-geo.js`）与**当前定位点实时天气**（Open-Meteo 实况 + 3 天/逐小时预报弹窗）。
   - **中栏**：唯一蓝图视口（`.fui-field.fui-field--marks` + 四角标尺/准星）承载真实 550W 三维机身、交互式结构简图与资料/媒体投影三模切换，顶部动态态势铭牌（`.fui-plate`），底部流光正弦声纹（`fui-audio.js` `drawOrbit`）与实时字幕；下方停靠双通道音频包络（MIC / MOSS）、紧凑运行日志、日程提醒与媒体队列。
   - **右栏**：常驻 MOSS 对话终端（`.fui-chat` 左对齐无气泡通讯日志，不再与遥测互斥），完整接入 SSE、流式/思考态、草稿预览与确认、Windows 桌面确认、已核验回执、历史/记忆/便签/工具示例/Jev 草稿窗与本机语音录播。
2. **完整保留原版 550W 两段式开场动画**：550W 描边填充 SVG + `moss-opening.mp4` 缩放影片，支持跳过、ESC 关闭、点击左上 550W 标志或设置中重播，并自动遵守 `prefers-reduced-motion`。
3. **中英双语实时切换 (`EN | 中文`)**：全部界面文案纳入消息目录，遥测数字/单位/编号/坐标跨语言一致。
4. **场景演练快捷键**：`[` / `]` 切换 6 个故事板场景态（`nominal` / `listening` / `thinking` / `speaking` / `degraded` / `critical`）；`Ctrl+K` 聚焦对话输入；`Alt+M` 切换麦克风；支持 URL 参数 `?lang=en|zh-CN`、`?beat=<id>`、`?paused=1`、`?scene=off`、`?skipOpening=1`。

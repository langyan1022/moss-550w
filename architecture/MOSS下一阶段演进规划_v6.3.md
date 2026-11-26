# MOSS 550W 架构演进与质量沉淀报告（v6.3）

> 版本：`v6.3`（全量完成 `v6.3-A` / `v6.3-B` / `v6.3-C` 三阶段，保留 GitHub 手动同步策略）
> 基线：`v6.2` 全面三层审计 + 实机运行遥测复盘 + 双端真实模型/四阶段发布门禁闭环

---

## 一、演进背景与系统级复盘结论

在完成 `v6.2` 后，本轮对运行态遥测、服务端/控制面、语音音频、多端 FUI/Mobile 与核心认知循环进行了三视角穿透审校，并叠加真实模型（Aliyun 云端 + 本地 CUDA `Qwen3.5-4B`）与 Playwright 全链路回归，共定位并彻底消除以下核心短板：

1. **认知闭环与模型预算冲突**：
   - 单意图语句偶发误触 `compound_request`、本地 4B 动态工具 top-10 中管理型动词工具（如 `cancel_calendar_event`）曾被其它 240 分辅助工具挤出、`COMPOUND` 非复合请求未放空 `terminal`、`eff_playbook` 截断前已误记 `applied_count`、`_fit_memory_json_budget` 在存在 `_media_summary` 后缀时裁剪失效。
   - 本地 `Qwen3.5-4B` 在 `_rule_verbosity_polarity` 漏判「不超过50字/精简」与 `select_playbook` 仅过滤 `scope=="tone"` 规则时，导致「不要精简，请用200字左右详细拆解」仍受历史极简规则干扰；已通过完整极性识别 + 跨 scope 详略门控与动作动词优先级评分彻底解决。
2. **记忆、Soul 与 RSI 自我迭代边界**：
   - `work_city` 过宽正则、引用语句（`“...”`/`「...」`/`"..."`）、否定语句（`不过敏`/`不再忌口`）与疑问句被误蒸馏进 Soul 画像或情景记忆；临时记忆被永久替换后残留 `expires_at`；跨会话/同文错问误记 `implicit_reask`；`evaluate_dpo_episodes` 存在 `corr_s` 泄漏；记忆修改未级联清理过期 SAG / Soul 特征。
3. **后台整合阻塞、多租户泄露与脉搏孤岛**：
   - 夜间睡眠整合长期在主事件循环共享 SQLite 连接直接写入；`ProactivePulseEngine` 与 `telemetry_totals` / `proactive_alerts` 缺少 `owner_code` 隔离且临近午夜（23:xx）跨天出行未纳入 6 小时降水联动窗口；`/api/events` 未将 `brain-alert` 按归属租户推送；自定义 Skill 缺少 `required_tools` 与定时只读白名单门禁。
4. **音视频、安全与多端 FUI 健壮性**：
   - `voice_input.py` 缺少 Windows JobObject 随宿主强退护栏、`voice/asr_engine.py` `preview` 提交误清空流缓冲；`online_music.py` 缺少连接池与 `asyncio.to_thread` 缓存卸载；`doc_extract.py` / `sag_knowledge.py` / `background_jobs.py` / `iot_bridge.py` / `brain/web_tools.py` / `console_settings.py` 在 ZIP/zlib 边界、`.ssh`/凭据无尾斜杠匹配、重定向 SSRF/DNS 重绑定、IoT `entity_id` 越权覆盖及登录暴破计数方面存在加固空间。
   - `#comms-drawer` (`z-index: 28`) 曾遮挡 `#pulse-toast-dismiss`；Stage 3D 在 `#stage-reload-3d` 重载时直接复用已执行 `transferControlToOffscreen()` 的旧 `<canvas>` 触发 `InvalidStateError`（已被本轮新增 UI 17/17 用例实际捕获并修复）。

---

## 二、v6.3 三阶段核心落地清单

### 阶段 1（`v6.3-A`）：P0 认知、多租户、语音热备与 UI 止血
- **认知与路由精度（[router.py](file:///f:/moss/agent-lab/prototype/brain/router.py), [prompts.py](file:///f:/moss/agent-lab/prototype/brain/prompts.py), [engine.py](file:///f:/moss/agent-lab/prototype/brain/engine.py), [holojarvis_runtime/brain.py](file:///f:/moss/agent-lab/prototype/holojarvis_runtime/brain.py)）**：
  - 收紧 `COMPOUND_OTHER_INTENT`（排除问候逗号前缀）与 `TOOL_SUBJECT`，避免单个控制请求或「你好，查一下……」误触发或漏触发意图。
  - 引入 `_TOOL_GUIDES` 按本轮 `available_tools` 动态装配云端工具指引（含 `delete_recurring_task` / `get_volume` / `draft_chat_reply`），保持 `_SYSTEM_TOOLS_FULL` 完整性。
  - 使用 `json.JSONDecoder().raw_decode` 修复 `_fit_memory_json_budget`；将 `moss.rsi.mark_applied` 与 `_rsi_applied_ids` 移至 `eff_playbook` 预算裁剪后（并跳过 `_scheduled_readonly`）；`compound_request=True` 清空 `terminal`；`used` / `holo_tools` 改在 `handler` 成功返回后记录；`allowed_tools` 同步约束 `dispatch`、`select_tools_for_turn` 与前置快速路径。
  - 限制 `unlock_tools` 上限（本地 16 / 云端 24），扩展 `_ro_cache` 错误前缀拒绝缓存，并在无工具降级 `_tool_free_messages` 中合并同角色消息以保持 strict alternation。
- **数学与日期时区修正（[math_date_tools.py](file:///f:/moss/agent-lab/prototype/brain/math_date_tools.py), [assistant_timers.py](file:///f:/moss/agent-lab/prototype/assistant_timers.py)）**：
  - `_WORLD_ZONE_LOOKUP` 英文别名前后加 ASCII 词边界且优先完整匹配 `norm.casefold()`（修复「太平洋时间」被剥尾截断）；`pow()` 指数严格限制 `abs(args[1]) <= 1000` 并捕获 `OverflowError`；`DAY` 与 `DAY_OFFSETS` 补充 `大后天 (+3)` 及 `昨天/前天/大前天`。
- **CSS 层叠修复（[console.css](file:///f:/moss/agent-lab/prototype/fui/public/styles/console.css)）**：
  - 为 `.moss-pulse-toast` 设置 `position: relative; z-index: 30`（高于停靠抽屉 `z-index: 28` 且不遮挡浮动弹窗 `z-index: 36`）与 `.moss-pulse-toast[hidden] { display: none; }`。

### 阶段 2（`v6.3-B`）：自迭代闭环、非阻塞整合、自主脉搏与安全加固
- **RSI、记忆与 Soul 去噪（[rsi_engine.py](file:///f:/moss/agent-lab/prototype/rsi_engine.py), [soul_memory.py](file:///f:/moss/agent-lab/prototype/soul_memory.py), [memory_store.py](file:///f:/moss/agent-lab/prototype/memory_store.py), [handlers.py](file:///f:/moss/agent-lab/prototype/brain/handlers.py), [core.py](file:///f:/moss/agent-lab/prototype/core.py)）**：
  - 排除个人事实误触 `EXPLICIT_TEACH_SIGNAL`、收紧 `SELF_ITERATE_QUERY` 中「复盘」上下文限定（避免「星环复盘会议」误召回复盘工具）、扩展 `_rule_verbosity_polarity` 识别 `不超过50字/以内/精简`，并在 `select_playbook` 中对所有矛盾极性的规则执行 `wants_detail`/`wants_brief` 互斥门控。
  - `run_iteration_cycle` 禁止跨会话及不同日期/实体配对误触发 `implicit_reask`；`evaluate_dpo_episodes` 移除 `corr_s` 泄漏；`_observe_rsi_feedback` 检查 `previous_turns[-4:-1]`；记忆更新/替换时级联清理过期 SAG 与 Soul trait，并在永久替换时清除旧 `expires_at`。
  - Soul 蒸馏跳过引号引用、否定表达与疑问句，尊重 `source == "user_edit"` 的人工编辑条目。
- **自主脉搏、多租户隔离与非阻塞 WAL 整合（[proactive_pulse.py](file:///f:/moss/agent-lab/prototype/proactive_pulse.py), [sleep_consolidator.py](file:///f:/moss/agent-lab/prototype/sleep_consolidator.py), [brain_iot_routes.py](file:///f:/moss/agent-lab/prototype/brain_iot_routes.py), [serve.py](file:///f:/moss/agent-lab/prototype/serve.py)）**：
  - 为 `proactive_alerts` 增加 `owner_code` 列与索引，`telemetry_totals` / `status_snapshot` / `proactive_dismiss` / SSE `brain-alert`（含重连未消告警补发）全部按 `owner_code` 隔离；`evaluate_tick` 降水出行联动扩展 `commute_end = max(today_end, win_start + 6 * 3600)` 覆盖跨午夜外出日程。
  - `run_consolidation_async` 对文件型数据库通过 `asyncio.to_thread` 打开短生命周期 WAL SQLite 连接运行（`:memory:` 库走原连接兼容回退）。
  - `ask_scheduled_prompt` 引入 `extra_allowed_tools` 且受 `SCHEDULED_SAFE_SKILL_TOOLS` 约束，`skill.required_tools` 直传例行/技能调度执行。
- **语音、在线音乐、全栈安全与 Stage 3D 热恢复（[cognitive_utils.py](file:///f:/moss/agent-lab/prototype/cognitive_utils.py), [voice_input.py](file:///f:/moss/agent-lab/prototype/voice_input.py), [voice/asr_engine.py](file:///f:/moss/agent-lab/prototype/voice/asr_engine.py), [online_music.py](file:///f:/moss/agent-lab/prototype/online_music.py), [doc_extract.py](file:///f:/moss/agent-lab/prototype/doc_extract.py), [iot_bridge.py](file:///f:/moss/agent-lab/prototype/iot_bridge.py), [web_tools.py](file:///f:/moss/agent-lab/prototype/brain/web_tools.py), [console_settings.py](file:///f:/moss/agent-lab/prototype/console_settings.py), [stage.js](file:///f:/moss/agent-lab/prototype/fui/public/js/stage.js), [gestures.js](file:///f:/moss/agent-lab/prototype/fui/public/js/gestures.js)）**：
  - `cognitive_utils.py`（`196` 行）沉淀公共原语：`attach_kill_on_close_job`（Win32 JobObject `PROCESS_SET_QUOTA | PROCESS_TERMINATE` `0x0101` + `wintypes.HANDLE` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，附 `close_job_handle`）、`is_sensitive_fs_path`、`normalize_owner_code`、`is_truly_public_ip`、`resolve_public_url`、`make_pinned_connector`。
  - `voice_input.py` 挂载并及时回收 JobObject、`stream_commit(preview=True)` 惰性不冷启；`voice_output.py` 识别锁让渡退出（`yielded`）避免误杀热常驻进程；`voice/asr_engine.py` 对 `base64.b64decode(..., validate=True)` 捕获格式异常且在 `preview=True` 时保留 `stream_buffers[sid]`。
  - `online_music.py` 统一使用 `is_truly_public_ip`、IDNA 主机名规范化、事件循环绑定会话复用、SQLite WAL 并异步卸载缓存写盘。
  - `doc_extract.py` 增加 `16 MiB` `_safe_zip_read` 与 `4 MiB` PDF 流解压上限及整文件读上限；`iot_bridge.py` 限制服务名 `^[a-z0-9_]{1,64}$`、禁止覆盖 `entity_id`、关闭重定向；`web_tools.py` 处理非法端口 `ValueError`、punycode 转换与 `trust_env=False` 固定连接器防 DNS 重绑定；`console_settings.py` 防未知操作码清空失败计数、限流访客自注册（`30/min`）并在注销/删户时清理 cookie 属性、隔离槽位与历史脉搏告警。
  - `stage.js` + `gestures.js` + `opening.js` + `dialogue.js` + `mobile.js`：统一加载 `/conversation-assets/moss-view.js?entry=fui-console&lifecycle=1` 并持有卸载句柄，`#stage-reload-3d` 增加 `loadEpoch` 并发防抖、清理 `moss-optics` 旧监听并在已转让画布时克隆替换 `<canvas>`（同步修复 `dialogue.js` 在 `replaceWith()` 后 `cursor` 失效导致的 `NotFoundError`，保障流式首段/本机概览语音预合成正常工作）；`auth-prefs.js` / `badge.js` 修复 503 门禁死锁与 C 角色徽章首帧闪现。

### 阶段 3（`v6.3-C`）：模块化拆解、二次对抗复核与 QA 盲区全面闭合
- **大模块瘦身拆分（保持对外公共契约完全向后兼容）**：
  - [serve.py](file:///f:/moss/agent-lab/prototype/serve.py)：由 `1,871` 行精简至 **`1,395` 行**，拆出：
    - [runtime_loops.py](file:///f:/moss/agent-lab/prototype/runtime_loops.py)（`332` 行：`install_runtime_loops`、`proactive_heartbeat_loop`、`tail_logs_loop`、Skill/Routine 周期驱动与告警帧载荷修正）
    - [chat_attachment_routes.py](file:///f:/moss/agent-lab/prototype/chat_attachment_routes.py)（`242` 行：`install_chat_attachment_routes`、`_scan_chat_rows`、`chats_search`、`chat_export`、`chat_attach` 及 UTF-8 CJK 安全截断）
  - [core.py](file:///f:/moss/agent-lab/prototype/core.py)：由 `3,084` 行精简至 **`2,818` 行**，拆出：
    - [core_media_mixin.py](file:///f:/moss/agent-lab/prototype/core_media_mixin.py)（`307` 行：`_MossMediaMixin` + `RESUME_READINGS`，并由 `core.py` 保持重导出）
- **QA 门禁与对抗复核扩展**：
  - [release_gate.py](file:///f:/moss/agent-lab/prototype/release_gate.py)：`audit_js_contracts` 全面校验 **25 个控制器解构绑定**与 **584 个 DOM ID**（支持多参数控制器与反引号路由/`status.html` 扫描），并将路由稽核升级为精确编译后端路由正则；`fui_lint.py` 缺失即失败；Stage 2 新增 `verify_fui_voice_longchat.py`。
  - [verify_brain_cognitive_suite.py](file:///f:/moss/agent-lab/prototype/verify_brain_cognitive_suite.py)：扩展 **`G12_v63_arch`** 至 **10 项**（含 SSRF/Win32 JobObject/端口校验/数学溢出/时区边界等），套件总量升至 **140/140**。
  - [verify_fui_voice_longchat.py](file:///f:/moss/agent-lab/prototype/verify_fui_voice_longchat.py)（`235` 行）：实测桌面端 >45 轮长对话独立时间戳正序、EN<->ZH 语言切换保留 `.moss-gen-widget` 并联动实时计算（`1,600`）、两轮 `#comms-mic` 切换、移动端 C 角色 `0` 个 `403` 与内部滚动约束、以及 `/api/auth/status -> 503` 无死锁离线降级。
  - [verify_v53_gate_ui.mjs](file:///f:/moss/agent-lab/prototype/verify_v53_gate_ui.mjs)：扩展至 **UI 17/17**（`UI 16/17` 双标签页 `MOSS` vs `OP63` 隔离与真实 `POST 200` 等待；`UI 17/17` Stage 3D `#stage-reload-3d` 故障恢复解锁与同步双击并发校验）。
  - [verify_future_capabilities.py](file:///f:/moss/agent-lab/prototype/verify_future_capabilities.py)：同步 `sync_note_sag` 即时 SSoT 校验条件（8/8 全通过）。

### 阶段 4（`v6.3-r2`）：二次零容忍深度复盘与全栈闭环加固
在 `v6.3-C` 收口后，团队从**服务端并发安全与 Git 卫生**、**认知/路由/Soul/RSI 与记忆一致性**、**多端 FUI / Mobile / Stage 3D / QA 契约**三个正交视角再次执行独立盲审，定位并清零全部 confirmed P0/P1/P2 问题：
- **服务端、RBAC 与并发异步修复**：
  - [console_settings.py](file:///f:/moss/agent-lab/prototype/console_settings.py) / [serve.py](file:///f:/moss/agent-lab/prototype/serve.py)：增加 `ADMIN_WRITE_PATHS` 对 `/api/context/ocr` 的管理员写门禁；LAN 模式采用默认 `MOSS` 授权码时启动输出明确安全告警，支持通过 `/api/auth/config` 开启 `lan_cs_local_only`（或 `MOSS_LAN_CS_LOCAL_ONLY=1`）或设置 PBKDF2 摘要校验的非回环 `admin_pass`（或 `MOSS_ADMIN_PASS`）；退役 `AsyncOpenAI` 客户端在存在活跃后台任务时延迟平滑关闭。
  - [runtime_log.py](file:///f:/moss/agent-lab/prototype/runtime_log.py) / [runtime_loops.py](file:///f:/moss/agent-lab/prototype/runtime_loops.py)：新增受锁保护的 `subscribe` / `unsubscribe` / `snapshot_subscribers`，消除日志订阅集合并发迭代竞争。
  - [online_music.py](file:///f:/moss/agent-lab/prototype/online_music.py) / [online_music_routes.py](file:///f:/moss/agent-lab/prototype/online_music_routes.py)：统一将 `resolve_cover_target` / `resolve_stream_target` 与 SQLite 歌词/曲目缓存读写卸载至 `asyncio.to_thread`；禁止 C 级访客访问 `allow_internal=True` 的私有 Navidrome 封面与音频代理。
  - [brain_iot_routes.py](file:///f:/moss/agent-lab/prototype/brain_iot_routes.py) / [iot_bridge.py](file:///f:/moss/agent-lab/prototype/iot_bridge.py) / [chat_attachment_routes.py](file:///f:/moss/agent-lab/prototype/chat_attachment_routes.py) / [background_jobs.py](file:///f:/moss/agent-lab/prototype/background_jobs.py) / [voice_output.py](file:///f:/moss/agent-lab/prototype/voice_output.py)：`vision_to_sag` 异步化并收敛 `503` 错误映射；`ha_list_entities` 强制校验 `ha_enabled` 与列表类型、追踪异步 Webhook 任务并在 `close()` 清零；图片附件使用 `_IMG_PIXEL_LOCK` 保护全局像素上限且投递失败自动回滚孤儿 SAG 节点；`folder_digest` 与 watcher 跳过符号链接並规范 `resolve()` 路径；`voice_output.py` 增加后台任务跟踪与文件读取卸载。
  - [core.py](file:///f:/moss/agent-lab/prototype/core.py) / [sleep_consolidator.py](file:///f:/moss/agent-lab/prototype/sleep_consolidator.py) / [music_prefs.py](file:///f:/moss/agent-lab/prototype/music_prefs.py) / [semantic_memory.py](file:///f:/moss/agent-lab/prototype/semantic_memory.py)：全部租户存储统一经由 `cognitive_utils.normalize_owner_code` 归一化（`"cs"` → `"MOSS"`）；`SleepConsolidator` 全量改用 `delete_sag_events`；`_schedule_rsi_learning` 排除定时器/窗口/附件与 `other_person_query` 轮次；新增 `_await_finishing_slot` 消除回复刚刚通过 SSE 推送为 `replied` 瞬间的 `409` 尾槽竞态；[cognitive_utils.py](file:///f:/moss/agent-lab/prototype/cognitive_utils.py) 扩展敏感文件名拦截（`chat-providers.json` / `online-music.json` / `online_music_cache.sqlite3`）。
- **认知内核、路由、Soul 与 RSI 二次精修**：
  - [handlers.py](file:///f:/moss/agent-lab/prototype/brain/handlers.py) / [registry.py](file:///f:/moss/agent-lab/prototype/brain/registry.py)：补齐 `TOOL_HANDLERS["get_device_status"]` 使工具注册表与执行表严格一一对应；按角色收敛 C 级访客 `list_capabilities` 描述；`_h_search_user_memory` 强制校验 `allow_own_personal`、`_h_search_knowledge` 在非私人上下文限定仅检索 `SAG_PUBLIC_SOURCES`；`_h_set_music_sleep_timer` 严格校整数参数；收紧 `_h_play_song` 避免概念问答被顺延播放劫持。
  - [router.py](file:///f:/moss/agent-lab/prototype/brain/router.py) / [weather_flow.py](file:///f:/moss/agent-lab/prototype/brain/weather_flow.py) / [web_tools.py](file:///f:/moss/agent-lab/prototype/brain/web_tools.py) / [memory_store.py](file:///f:/moss/agent-lab/prototype/memory_store.py) / [sag_knowledge.py](file:///f:/moss/agent-lab/prototype/sag_knowledge.py)：修复 `COMPOUND_OTHER_INTENT`（要求并列连词触发而非问候逗号误触发）并在 `is_compound_request` 叠加跨意图组合检出（去除「推荐…对比差异」误触）；扩展 `WEATHER_TOPIC` 与第三方称谓 `PERSON_SUBJECT` 识别；本地 4B 纯闲聊轮次（`local plain_prose` 且无语义候选）下发零工具集合；`web_search` / `get_news_headlines` 输出前置只读外源不可信声明；清理未使用私有 Token 常量并彻底收敛 `auto_recall(allow_private=False)` 至 `SAG_PUBLIC_SOURCES`。
  - [engine.py](file:///f:/moss/agent-lab/prototype/brain/engine.py) / [holojarvis_runtime/brain.py](file:///f:/moss/agent-lab/prototype/holojarvis_runtime/brain.py) / [soul_memory.py](file:///f:/moss/agent-lab/prototype/soul_memory.py) / [rsi_engine.py](file:///f:/moss/agent-lab/prototype/rsi_engine.py) / [assistant_timers.py](file:///f:/moss/agent-lab/prototype/assistant_timers.py) / [recurring_tasks.py](file:///f:/moss/agent-lab/prototype/recurring_tasks.py)：统一 `_record_done()` 补齐天气早退轮次 `reply_ms`；非幂等写工具执行后清空 `_ro_cache` 并支持超短对话 `sys_prompt` 预算裁剪；Soul `distill_turns` 排除定时器动词误建 `name` 特征与他人/附件噪声且 `build_soul_prompt(..., max_chars=360)` 仅对入围特征累加访问计数；RSI 修紧 `CORRECTION_SIGNAL` / `EXPLICIT_TEACH_SIGNAL` / `_rule_verbosity_polarity`（过滤否定「不要过于简短」） / `_rule_matches_turn` / 退休条目预算让渡；区分周期性「每(个)礼拜/每周」与单次「下礼拜一」定时解析。
- **前端安全 XSS、C 级视图收敛、Mobile 与 QA 回归加固**：
  - [utils.js](file:///f:/moss/agent-lab/prototype/fui/public/js/hud/utils.js) / [intel-image-doc.js](file:///f:/moss/agent-lab/prototype/fui/public/js/hud/intel-image-doc.js) / [badge.js](file:///f:/moss/agent-lab/prototype/fui/public/js/badge.js) / [brain-deck.js](file:///f:/moss/agent-lab/prototype/fui/public/js/brain-deck.js) / [server.mjs](file:///f:/moss/agent-lab/prototype/fui/server.mjs)：`escapeHtml` 补充单引号 `&#39;` 转义；Intel 幻灯片与工牌头像全面经过协议白名单过滤；拒绝 `//` 协议相对 `mobile_url`；`server.mjs` 支持剥离 IPv6 `[::1]` 方括号并正确放行 `::1` 本地请求。
  - [index.html](file:///f:/moss/agent-lab/prototype/fui/public/index.html) / [dialogue.js](file:///f:/moss/agent-lab/prototype/fui/public/js/dialogue.js) / [telemetry.js](file:///f:/moss/agent-lab/prototype/fui/public/js/telemetry.js) / [weather-host-view.js](file:///f:/moss/agent-lab/prototype/fui/public/js/hud/weather-host-view.js) / [hud-popups.js](file:///f:/moss/agent-lab/prototype/fui/public/js/hud-popups.js) / [modals.js](file:///f:/moss/agent-lab/prototype/fui/public/js/modals.js) / [console-settings.js](file:///f:/moss/agent-lab/prototype/fui/public/js/console-settings.js) / [voice.js](file:///f:/moss/agent-lab/prototype/fui/public/js/voice.js) / [duplex-voice.js](file:///f:/moss/agent-lab/prototype/fui/public/js/duplex-voice.js) / [app.js](file:///f:/moss/agent-lab/prototype/fui/public/js/app.js)：为全部管理员专用操作加 `data-cs-only="true"` 与 C 角色调用守卫（消除 `/api/prepare-speech`、`/api/online-music/scrobble`、`/api/soul`、审批/摘录便签/忽略提醒 403 噪声），并在 CS→C 切换时清空日志视图与 Soul/Notes 缓存；同步打开设置时的 `#set-scene-3d` 状态。
  - [mobile.js](file:///f:/moss/agent-lab/prototype/fui/public/js/mobile.js) / [mobile.html](file:///f:/moss/agent-lab/prototype/fui/public/mobile.html) / [status.html](file:///f:/moss/agent-lab/prototype/fui/public/status.html) / [comms-drawer.js](file:///f:/moss/agent-lab/prototype/fui/public/js/comms-drawer.js) / [gestures.js](file:///f:/moss/agent-lab/prototype/fui/public/js/gestures.js) / [stage.js](file:///f:/moss/agent-lab/prototype/fui/public/js/stage.js)：修复 `/mobile` 切歌响应渲染、上/下一首原始索引映射、录音停止重入锁 `stoppingMic`、认证遮罩 `inert` 无障碍隔离与窄屏媒体栏折行；`/status` 认证补充网络 `try/catch` 与 `inert`；手势 Worker 异常立即 `terminate()` 并清理过期位图；`stage.js` 关闭媒体音频分析时安全断开 `_fuiSource` 并在重播时按需重连。
  - 同步升级 [verify_brain_cognitive_suite.py](file:///f:/moss/agent-lab/prototype/verify_brain_cognitive_suite.py)（`142/142`，`G12` 扩至 `12/12`）、[verify_v55_units.py](file:///f:/moss/agent-lab/prototype/verify_v55_units.py)、[verify_v53_gate_ui.mjs](file:///f:/moss/agent-lab/prototype/verify_v53_gate_ui.mjs)、[verify_fui_speech_matrix.py](file:///f:/moss/agent-lab/prototype/verify_fui_speech_matrix.py)、[verify_fui_speech_latency.py](file:///f:/moss/agent-lab/prototype/verify_fui_speech_latency.py)、[verify_fui_full_acceptance.py](file:///f:/moss/agent-lab/prototype/verify_fui_full_acceptance.py)、[fui/verify.py](file:///f:/moss/agent-lab/prototype/fui/verify.py) 与 [README.md](file:///f:/moss/agent-lab/README.md)。

---

## 三、最终验证实测记录（全部基于最后一次代码定稿之后执行）

| 验证套件 / 通道 | 实测结果 | 关键产物路径 |
|---|---|---|
| **四阶段发布门禁** `release_gate.py --stages static,unit,audio,ui --tag v63-r2` | **PASSED (`176.99s`)** | [gate_summary.json](file:///f:/moss/agent-lab/prototype/runs/release-gate-v63-r2-20261125-130902/gate_summary.json) |
| Stage 1 静态与契约审计 | `py_compile` 94 文件、`node --check` 36 文件、i18n `818/818` 零差、JS 契约 `25` 控制器/`584` ID/`0` 断链、`fui_lint`、`check_model_release` 全过 | 同上 |
| Stage 2 单元与多端专项 | 语音数词、`dialogue_quality`、`erhua_tone`、`golden_200 (260/260)`、`cognitive_suite (142/142, G12: 12/12)`、`v5.3`、`v5.5`、`soul_memory`、`online_music`、`gesture_replay`、`fui_voice_longchat` 全过 | 同上 |
| Stage 3 语音同款对测 & 3070 Ti 热启动 | `verify_fui_speech_latency --mode parity (9/9, 43.18s)`；CUDA 预热 `warm_s=1.007s`、`head_s=0.09s`、`max_pause_s=0.34s` | 同上 |
| Stage 4 Edge 实机 UI & 像素审计 | `verify_v53_gate_ui.mjs (53.37s, 17/17 checks, XSS/OP63 C-role/Stage-3D reload, 0 errors)`；4×2 共 8 个 Intel 徽标像素审计全过；`HOT_UNPLUG_RECOVERY_OK` | 同上 |
| **云端真模型压测** `--provider aliyun --tag v63-r2-live` | **`16/16` 全通过** | [result.json](file:///f:/moss/agent-lab/prototype/runs/brain-v6-gauntlet-aliyun-v63-r2-live-20261125-131235-970147/result.json) |
| **本地 CUDA 4B 真模型压测** `--provider local --qwen-backend cuda --tag v63-r2-4b` | **`16/16` 全通过** | [result.json](file:///f:/moss/agent-lab/prototype/runs/brain-v6-gauntlet-local-v63-r2-4b-20261125-131358-600353/result.json) |
| 未来能力端到端验证 `verify_future_capabilities.py` | **`8/8` 全通过**（脉冲/睡眠冲突/后台技能/HA安全闸/550W硬件帧/视觉/FUI组件/Edge移动端） | [result.json](file:///f:/moss/agent-lab/prototype/runs/future-capabilities-20261125-131216-263224/result.json) |
| 专项与全验收回归套件 | `verify_calendar_mutations.py` / `verify_reminder_queries.py` / `verify_memory_browser_local.py` / `verify_fui_speech_matrix.py` / `verify_fui_full_acceptance.py (A-F 全项)` / `fui/verify.py` 全通过 | [calendar result.json](file:///f:/moss/agent-lab/prototype/runs/calendar-mutations-20261125-131233-659923/result.json) · [acceptance result.json](file:///f:/moss/agent-lab/prototype/runs/fui-full-acceptance-20261125-130756-735010/result.json) |

# MOSS 550W 下一阶段深度演进规划（v6.2 · P8–P11）

> 基于 `v6.1`（P4–P7）全量收敛基线之后的三路源码深水区审计（认知内核 / 服务端与全双工语音 / 前端 UX 与测试矩阵），结合实机与测试门禁验证结果沉淀。

---

## 一、v6.1 基线复核与新暴露的核心痛点

1. **运行时沉默故障（Heartbeat 30s 崩溃 & 定时工具空转）**：
   - `serve.py:347` `proactive_heartbeat_loop()` 引用了重构前遗留变量 `subscribers`（已拆为 `subscribers_by_owner`），在首个空闲 tick（30s）触发 `NameError` 并永久退出心跳，连带导致 `ProactivePulseEngine.evaluate_tick()`、例行任务调度和空闲睡眠整理停摆。
   - `core.py:2945` `SCHEDULED_READONLY_TOOLS` 列出 `"get_local_clock"`、`"diagnose_system"`、`"inspect_home_assistant"` 三个不存在的工具名，导致后台定时推演无法调用真实的 `get_time` / `get_device_status` / `ha_list_devices`。
   - `serve.py:1600` `start_local()` 一次性消费 `local_engine == "standby"`：本地 4B `llama-server` 异常退出后无法自动拉起，切回云端模型后也永久丧失 `can_warm_microphone()`。
2. **多租户隔离漏洞与资产孤岛**：
   - `sag_knowledge.py:463-482` 在 `owner_clause` 过滤前先对全库执行 FTS5 `LIMIT 30` 与实体扫描，主操作员事件较多时 C 级操作员检索会被饿死。
   - `brain/handlers.py:655-664` `_h_recall_turn_context` SQL 未按 `owner_code` 过滤且误用 `ctx.allow_personal` 锁死 C 级操作员自检索；`sleep_consolidator.py:316` 遗漏 `owner_code=owner`；`console_settings.py:426` 删除 C 级用户时未级联清理 SAG / Soul / RSI / Routines / MusicPrefs / WorldModel 数据；`serve.py:1127` `runtime_stream` 非 tail 流缺少 CS 角色校核；`serve.py:344` 空闲探测仅检查 Host 主会话而非所有并发槽位。
3. **本地 4B（4096 ctx）工具韧性与统一预算**：
   - `holojarvis_runtime/brain.py` 缺少轻量 JSON 自修复、Schema 参数纠偏提示、缺失必需工具 / 空回复的 1 次性纠错重试、合成期跨轮工具结果滚动压缩与空/重 `tc.id` 容错。
   - `brain/engine.py` 对 `playbook_block` 与 `personality_block` 未做本地预算限制，且把关键提示 `"多步核对要求"` 放在易被 `_fit_lines_budget(..., 320)` 截断的末尾；`brain/router.py` `wide=True` 复合请求简单按前 10 个截断会挤掉靠后的 `create_timer` / `ha_control_device` 等工具。
4. **认知与自我迭代飞轮深度瓶颈**：
   - `soul_memory.py` `diet_avoid` / `project_focus` / `tech_stack` 等多值特征发生静默单槽覆盖；`memory_store.select_memories` 在有单字重叠时完全跳过同义词/概念桥；`sag_knowledge` 缺少度中心性阻尼与真正的多跳（2-hop）穿透。
   - `rsi_engine.py` 中性轮次误增 `helped_count` 导致低质规则虚假晋升，缺少针对 `rsi-dpo-episodes.jsonl` 的真实反事实对照生成（`counterfactual=True`）与自动化回归评测器 `evaluate_dpo_episodes()`。
   - `SleepConsolidator` 产出的 `world_model` 多数维度未被 `brain/engine.py` 消费，且缺少冗余 Episode 合并、时间衰减与面向活跃操作员的后台整理。
5. **全双工流式 ASR 断连与移动端/长会话 UX 缺口**：
   - `LocalVoice.stream_chunk/commit/reset` 未接入 `serve.py` HTTP/SSE 路由与前端 `agent-voice.moss.js` / `mobile.js`；移动端与媒体播放期存在回声自打断与 `targetsPlayback(instruction)` 谓词退化缺陷。
   - `dialogue.js` `turnSignature` 遗漏 `reply_ms` / `token_usage` 且每帧深度序列化全量字段，刷新重建 `.moss-gen-widget` 会丢失滑块/复选框状态；`mobile.html` 内联脚本未纳入 `node --check`，缺会话切换、主动告警、媒体进度条与 Markdown 渲染。
   - `verify_brain_v6_gauntlet.py --provider local` 仍传占位 URL `http://127.0.0.1:0/v1`，未打通真实 Qwen3.5-4B `llama-server` 端到端测试。

---

## 二、分阶段演进路线（P8 → P11）

### P8 · 运行时稳定性、多租户安全与局部引擎恢复（优先止血）
1. **修复心跳死循环与定时推演工具集**：
   - `serve.py`: 修复 `proactive_heartbeat_loop()` 使用 `subscribers_by_owner`，按 tick 捕获异常避免循环退出；抽象 `moss.any_active_turn()` 检查所有 `_slots`；按活跃操作员调度 `idle_sleep` 与 `run_rsi_cycle`，记录异常日志。
   - `core.py` & `recurring_tasks.py`: 修复 `SCHEDULED_READONLY_TOOLS` 为真实工具名并补齐只读联网/计算工具；支持 `action_kind="skill"`；支持定时 AI 推演输出 `NO_ALERT` 时静默不打扰。
2. **多租户检索 / 回收 / 安全闭环**：
   - `sag_knowledge.py`: FTS5 与 `ent_scores` 查询内联 `owner_clause`，消除 C 级租户饿死。
   - `brain/handlers.py`: `_h_recall_turn_context` 增加 SQL 级 `owner_code` 过滤并改用 `ctx.allow_own_personal`（保留越权隔离）。
   - `sleep_consolidator.py`: 传参 `owner_code=owner` 给 `get_profile_trait("work_city")`。
   - `console_settings.py`: `DELETE /api/auth/users/{code}` 级联清理 7 张认知子表及 `rsi-dpo-episodes-{code}.jsonl`。
   - `serve.py` / `online_music_routes.py` / `fui/server.mjs`: `runtime_stream` 强制 `CS` 权限校验；`chat_attach` 增加像素上限与解压校验；播放列表跳转校验 `allowed_netloc`；`/fui/shutdown` 校验 `Origin`/`Referer`。
   - 保持 `verify_v55_units.py` 与 `G7_guest_diag_scoped_no_leak` 现有 RBAC 契约 100% 兼容。
3. **本地 4B 引擎自愈与冷启动**：
   - `serve.py`: 记录并探测本地 `llama-server` 子进程真实存活状态；崩溃后自动复位回 `standby` 并可重新 `start_local()`；空闲释放后同步恢复麦克风语音预热资格；适当放宽唤醒期超时并保护 `--parallel 1` 槽位竞争。

### P9 · 4B 推理韧性、上下文统一预算与认知自我迭代闭环
1. **工具调用自愈与 4096 统一预算 (`holojarvis_runtime/brain.py`, `brain/engine.py`, `brain/router.py`, `core.py`)**：
   - 增加轻量 JSON 自修复、参数 Schema 类型纠偏（字符串转整数/布尔）、必需字段提示、空/重 `tc.id` 合成、必需工具漏调 / 空文字的 1 次纠错重试、以及多轮工具结果滚动折叠。
   - 在 `brain/engine.py` 对本地 4B 实行统一的 system + playbook + personality + history 字符/Token 上限控制，将 `"多步核对要求"` 前置防止被截断。
   - `brain/router.py` 对候选工具按「显式要求 > 领域高优 > 历史关联 > 通用兜底」打分排序后再截断，避免 `wide=True` 复合指令截断关键工具；净化 `TOOL_SUBJECT` 减少日常寒暄误触发；`memory_store.memory_command` 增加复合意图保护。
2. **记忆与 SAG 结构化检索升级 (`soul_memory.py`, `memory_store.py`, `sag_knowledge.py`, `sleep_consolidator.py`)**：
   - 区分单值槽位与多值叠加槽位（`diet_avoid`、`drink_pref`、`project_focus`、`tech_stack`、`hobby`）；修复 `user_edit` 阻塞 `memory_mirror` 刷新及条件假设句误提取；为 `memories` 与 `soul_profile` 增加访问计数/时间衰减清理。
   - `memory_store.select_memories` 始终融合概念桥接与同义词得分；`sag_knowledge` 引入实体度中心性阻尼 `1 / math.log1p(degree)` 与真实 2-hop 邻域扩展（按 ID 补全窗口外节点），`touch=False` 默认不污染访问热度。
   - `SleepConsolidator` 合并重复 `episode`、将 `world_model` 的 `focus_topics`/`key_collaborators`/`open_loops` 注入上下文、并支持可选异步 LLM 梦境总结。
3. **RSI 真实归因、反事实 DPO 与离线评测飞轮 (`rsi_engine.py`, `core.py`, `brain_iot_routes.py`)**：
   - 分离 `exposure_count`（被动曝光）与 `helped_count`（正向验证/非重复追问通过），纠偏惩罚优先精准归因 `t-1`；`_is_meta_ack` 场景自动开启 `counterfactual=True` 生成真实 `(prompt, rejected, chosen)` DPO 对并兼容本地 4B JSON 评判；新增 `evaluate_dpo_episodes()` 与 `POST /api/rsi/eval` 接口量化规则准确率与胜率。

### P10 · 全双工流式 ASR、回声防御、服务端异步化与前端体验升级
1. **端到端流式 ASR 与回声屏蔽 (`serve.py`, `voice_input.py`, `voice/asr_engine.py`, `voice_output.py`, `agent-voice.moss.js`, `app.js`, `voice.js`)**：
   - 新增 `/api/transcribe/stream/start|chunk|commit|reset` 路由并打通浏览器分片上传，修复 `stream_buffers` 奇数长度保护与 TTL 淘汰及空闲计时器误杀问题；`voice_output.py` 加锁保护 `lines` LRU 并允许真实播报打断麦克风预热锁。
   - 修复 `app.js` `targetsPlayback(instruction)` 丢失参数导致媒体播放期无法语音打断问题，统一桌面与移动端在录音期间自动闪避媒体音量（ducking）与清理 `playbackSegments`。
2. **服务端并发与 SSE 精准唤醒 (`core.py`, `online_music.py`, `runtime_log.py`)**：
   - `select_model` / `_record_transport` 按操作员精准唤醒 SSE；`online_music` 使用异步 DNS 解析与线程卸载 SQLite 缓存。
3. **桌面与移动端 UX 完备 (`fui/public/`)**：
   - 将 `mobile.html` 内联脚本拆出至 `fui/public/js/mobile.js` 纳入静态语法与契约审计；补齐移动端的历史会话切换、主动告警横幅、媒体进度条/时长/倍速、Markdown 与 Generative Widget 渲染、复制/重试/朗读及 TTS 回声防护。
   - 优化 `dialogue.js` `turnSignature`（补充 `reply_ms` / `token_usage` / `error` / `failover_provider`，轻量化签名避免全量深度序列化，跨更新保留交互式 `.moss-gen-widget` DOM 状态，增加长对话分段渲染与 `content-visibility: auto`，重试支持编辑/重发与已取消轮次）；修复 `brain-deck.js` `#pulse-toast` `dataset.state` 匹配、完善告警面板、累计 Token/延迟统计、Skill 删除与 Watcher/Routine 创建 UI、Soul 编辑与分文件 Tab 预览。

### P11 · 真实 Qwen 4B 测试矩阵、端口卫生与全门禁回归
1. 统一替换 `serve.py` / `browser_lab.py` / 高频 `verify_*.py` 裸 `socket` 绑定为 `testkit.safe_free_port`；`verify_brain_v6_gauntlet.py --provider local` 支持 `--model-url` 及自动拉起本地 `llama-server.exe` 执行真实 4B 认知/工具回归。
2. 补齐 `G11` 认知单测、`verify_v55_units` 流式 ASR / 心跳 / DPO 评测 / 级联注销单测、`verify_v53_gate_ui.mjs` 桌面与移动端 P8–P10 全项 UI 断言。
3. 全量运行 `release_gate.py --stages static,unit,audio,ui`、专项脚本与云端/本地双 Gauntlet，重启实时服务并验收健康状态。

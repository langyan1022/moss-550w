"""Declarative ToolSpec registry — single source of truth for schemas, routing tags, RBAC, and idempotency."""
from __future__ import annotations

from dataclasses import dataclass
import sys


def ab(name: str, default):
    """Resolve optional test-monkeypatched symbol from legacy `assistant_brain` facade."""
    facade = sys.modules.get("assistant_brain")
    return getattr(facade, name, default) if facade else default


def schema(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class ToolSpec:
    schema: dict
    domain: str
    is_readonly: bool = False
    parallel_safe: bool = False
    host_only: bool = False
    cloud_core: bool = False
    local_always: bool = False

    @property
    def name(self) -> str:
        return self.schema["name"]


SONG = schema(
    "play_song",
    "在本机/NAS 曲库（优先）及在线开源曲库（Audius、Internet Archive、Radio Browser 直播电台）或私有 Navidrome 中搜索并播放音乐。仅在用户明确要求放歌、点歌、开音乐电台、在线搜歌或追问没放出来时调用，绝不在用户闲聊或倾诉失眠/疲惫/情绪时主动调用。可按歌名(title)、歌手(artist)、分类/氛围(category)或来源(source)点歌；仅查看版本时 play=false。",
    {
        "title": {"type": "string", "description": "歌曲名或电台关键词，可留空（仅按歌手、分类或随机点歌时）。"},
        "artist": {"type": "string", "description": "歌手名，可留空。"},
        "category": {
            "type": "string",
            "enum": ["", "助眠", "纯音乐", "流行", "国风", "儿歌"],
            "description": "心情或分类歌单，例如助眠/舒缓选助眠或纯音乐。",
        },
        "source": {
            "type": "string",
            "enum": ["", "auto", "local", "online", "navidrome", "audius", "archive", "radiobrowser", "jamendo"],
            "description": "指定音源，默认 auto（本地优先、无结果或离线自动回落在线曲库）；听电台传 radiobrowser，听私有库传 navidrome。",
        },
        "play": {"type": "boolean"},
    },
    [],
)
CATALOG = schema(
    "list_media_catalog",
    "读取本机和 NAS 媒体库的真实曲目总数、分类歌单和主要歌手列表。回答曲库有哪些歌手或歌曲前必须先调用。",
)
WEB_SEARCH = schema(
    "web_search",
    "实时联网搜索公开网页与新闻摘要（资讯、金价、股价、百科、技术文档、背景调查）。询问最新动态、事实核查或追问某条新闻背景时直接调用。",
    {"query": {"type": "string", "description": "精炼的核心检索关键词（不要堆砌具体日期如2026年11月23日）。"}},
    ["query"],
)
GET_NEWS_TOOL = schema(
    "get_news_headlines",
    "直连实时新闻源（中新网/IT之家/今日头条热榜）获取今日国内要闻、国际、财经、科技新闻头条或热搜榜（附带原文链接）。用户询问今天新闻、国内新闻top5、热点、热搜或科技资讯时优先调用。",
    {
        "category": {
            "type": "string",
            "enum": ["china", "scroll", "world", "finance", "tech", "hot"],
            "description": "china=国内要闻，scroll=即时滚动，world=国际新闻，finance=财经金融，tech=科技数码，hot=今日热搜。",
        },
        "count": {"type": "integer", "description": "返回条数（1-10，默认 5）。"},
    },
    [],
)
FETCH_WEB_TOOL = schema(
    "fetch_web_page",
    "抓取公开网页或新闻原文 URL 的正文内容（自动去导航脚本、识别中英文编码）。当用户要求展开某条新闻详情、或直接给出 http/https 链接要求总结时调用。",
    {"url": {"type": "string", "description": "要抓取的公开 http 或 https 完整链接。"}},
    ["url"],
)
CALC_MATH_TOOL = schema(
    "calculate_math",
    "精确计算数学表达式、折扣、利息、汇率、分摊和复利，彻底避免心算误差。凡涉及多位数字乘除、百分比或财务金额计算时必须先调用。",
    {"expression": {"type": "string", "description": "标准算术表达式，例如 18650 * 0.128 * 47 / 365 或 round(1280 * 0.85, 2)。"}},
    ["expression"],
)
CALC_DATE_TOOL = schema(
    "calculate_date",
    "精确计算日期间隔天数、往后/往前推算 N 天是几月几号星期几，或查询某天是星期几，彻底避免日期和星期心算偏差。",
    {
        "operation": {
            "type": "string",
            "enum": ["diff", "offset", "weekday"],
            "description": "diff=计算 base_date 到 target_date 的相隔天数；offset=base_date 加减 offset_days 天后的日期和星期；weekday=查某天星期几。",
        },
        "base_date": {"type": "string", "description": "基准日期 YYYY-MM-DD，留空默认为今天。"},
        "target_date": {"type": "string", "description": "仅 operation=diff 时必填，目标日期 YYYY-MM-DD。"},
        "offset_days": {"type": "integer", "description": "仅 operation=offset 时必填，正数向后推、负数向前推。"},
    },
    ["operation"],
)
READ_FILE_TOOL = schema(
    "read_local_file",
    "只读读取用户提供的本地文本、Markdown、代码、JSON、CSV、日志或 docx 文档内容（最多 6000 字）。当用户给出本地文件路径要求阅读、总结或分析时调用。",
    {"path": {"type": "string", "description": "本地文件的绝对路径。"}},
    ["path"],
)
LIST_DIR_TOOL = schema(
    "list_local_directory",
    "只读列出本地某个目录下的文件与子目录（按最新修改时间降序，附大小与修改时间）。用户想看某文件夹里有什么或要求读最新文件时先调用。",
    {"path": {"type": "string", "description": "本地目录的绝对路径。"}},
    ["path"],
)
SAVE_NOTE = schema(
    "save_note",
    "把用户要求记录的信息、摘要、待办或上一轮内容写入本机便签库。",
    {"text": {"type": "string", "description": "要保存的便签正文（1-1200 字）。"}},
    ["text"],
)
SEARCH_NOTES = schema(
    "search_notes",
    "查询本机已有便签（留空则返回最新便签列表）。",
    {"query": {"type": "string", "description": "可选搜索关键词，留空列出最新便签。"}},
    [],
)
MANAGE_NOTE = schema(
    "manage_note",
    "修改、归档或恢复指定编号的本机便签（建议先通过 search_notes 确认编号）。",
    {
        "action": {"type": "string", "enum": ["edit", "archive", "restore"], "description": "edit=改内容；archive=归档删除；restore=恢复。"},
        "note_id": {"type": "integer", "description": "便签编号 ID。"},
        "text": {"type": "string", "description": "仅 action=edit 时必填，新正文。"},
    },
    ["action", "note_id"],
)
SAVE_MEMORY_TOOL = schema(
    "save_user_memory",
    "把用户自然提及的长期个人事实、口味忌口、过敏、工作城市、技术栈或个人数据持久化写入长期记忆库；当用户搬家、换工作、改偏好等覆盖旧事实时，同时传 replace_keyword 自动淘汰旧条目。",
    {
        "fact": {"type": "string", "description": "简洁完整的一条个人长期事实，例如「用户现在居住在杭州」（4-160字）。"},
        "replace_keyword": {"type": "string", "description": "选填：若该事实取代旧记忆（如从深圳搬到杭州），填旧记忆中的关键词（如「深圳」）以自动清理旧条目。"},
    },
    ["fact"],
)
SEARCH_MEMORY_TOOL = schema(
    "search_user_memory",
    "主动检索用户长期记忆库中的个人偏好、忌口、工作与生活事实（留空则返回全部生效长期记忆）。",
    {"query": {"type": "string", "description": "可选检索关键词，留空返回全部长期记忆。"}},
    [],
)
FORGET_MEMORY_TOOL = schema(
    "forget_user_memory",
    "当用户要求忘掉、删除或作废某条长期记忆或旧个人偏好（如「忘掉我不吃香菜那条」「删掉旧住址」）时调用，按关键词彻底移除并同步清理知识库。",
    {"keyword": {"type": "string", "description": "要遗忘的旧记忆关键词（如「香菜」「深圳」）。"}},
    ["keyword"],
)
CREATE_TIMER = schema(
    "create_timer",
    "创建一个相对倒计时本地提醒（例如 15 分钟后喝水、30 分钟后开会）。",
    {
        "minutes": {"type": "number", "description": "多少分钟后提醒（0.5 到 1440）。"},
        "label": {"type": "string", "description": "提醒事项内容。"},
    },
    ["minutes", "label"],
)
RESCHEDULE_TIMER = schema(
    "reschedule_timer",
    "改期、推迟或提前一条待触发的本地提醒（可按 timer_id 或 keyword 匹配；支持相对偏移 delay_minutes 或指定新时间 new_time）。",
    {
        "timer_id": {"type": "integer", "description": "提醒编号，不确定时留 0 改用 keyword。"},
        "keyword": {"type": "string", "description": "提醒事项关键词（如「收衣服」「带工牌」）。"},
        "delay_minutes": {"type": "number", "description": "相对原定时间的偏移分钟数（正数推迟/延后，负数提前；如延后25分钟填 25，提前10分钟填 -10）。"},
        "new_time": {"type": "string", "description": "直接改到的新时间，例如「明天 08:30」「2026-11-24 09:00」「18:30」或「30分钟后」（与 delay_minutes 二选一）。"},
    },
    [],
)
CANCEL_TIMER = schema(
    "cancel_timer",
    "取消一条待触发的本地提醒（可按提醒编号 timer_id 或事项关键词 keyword 匹配）。",
    {
        "timer_id": {"type": "integer", "description": "提醒编号（如 2），不确定时留 0 改用 keyword。"},
        "keyword": {"type": "string", "description": "提醒事项关键词（如「收衣服」「回电话」）。"},
    },
    [],
)
DEVICE_STATUS = schema("get_device_status", "读取这台电脑当前的真实 CPU、GPU 负载与温度、内存余量、电池电量和磁盘余量。")
QUERY_CALENDAR = schema(
    "query_calendar",
    "只读查询本机日历中的日程安排，支持今天、明天、后天、本周、未来 7 天或下周。",
    {"day": {"type": "string", "enum": ["today", "tomorrow", "day_after_tomorrow", "this_week", "next_7_days", "next_week"]}},
    ["day"],
)
CREATE_CALENDAR_TOOL = schema(
    "create_calendar_event",
    "在本机日历中新增一条未来的日程/会议，并自动检测是否与已有日程时间冲突。",
    {
        "title": {"type": "string", "description": "日程或会议名称（1-80字）。"},
        "start_time": {"type": "string", "description": "开始时间，例如「2026-11-24 15:00」或「明天 15:00」「明天下午3点」。"},
        "end_time": {"type": "string", "description": "结束时间，例如「2026-11-24 16:00」或「明天 16:00」「16:00」（省略日期时沿用开始日期）。"},
    },
    ["title", "start_time", "end_time"],
)
RESCHEDULE_CALENDAR_TOOL = schema(
    "reschedule_calendar_event",
    "修改/改期一条尚未结束的本机日历日程（按名称关键词匹配，重名时可附带原日期 filtering）。",
    {
        "keyword": {"type": "string", "description": "要改期的日程名称或关键词。"},
        "new_start_time": {"type": "string", "description": "新的开始时间，如「2026-11-26 15:00」或「后天 15:00」。"},
        "new_end_time": {"type": "string", "description": "新的结束时间，如「2026-11-26 16:00」或「16:00」。"},
        "date": {"type": "string", "description": "选填：用于区分同名日程的原日期，如「今天」「明天」。"},
    },
    ["keyword", "new_start_time", "new_end_time"],
)
CANCEL_CALENDAR_TOOL = schema(
    "cancel_calendar_event",
    "取消/删除一条尚未结束的本机日历日程（按名称关键词匹配，重名时可附日期过滤）。",
    {
        "keyword": {"type": "string", "description": "要取消的日程名称或关键词。"},
        "date": {"type": "string", "description": "选填：用于区分同名日程的日期，如「2026-11-24」「今天」「明天」。"},
    },
    ["keyword"],
)
CLOCK = schema(
    "get_time",
    "读取本机当前日期、星期与时间，或查询全球主要城市/时区的实时当地时间及与北京时间的时差（查询本机时间时 location 留空）。",
    {"location": {"type": "string", "description": "可选：城市或时区名（如 纽约、伦敦、东京、悉尼、洛杉矶、巴黎、新加坡、UTC），查本机时间留空。"}},
    [],
)
WEATHER = schema(
    "get_weather",
    "按城市、区县或用户提供的经纬度查询当前天气或当地今天、明天、后天的逐日预报。包括天气状况、最低和最高气温、降水概率。",
    {
        "city": {"type": "string", "description": "保留用户提供的城市和区县全名，或原样传入经纬度。"},
        "period": {
            "type": "string",
            "enum": ["current", "today", "tomorrow", "day_after_tomorrow", "three_days"],
            "description": "按该城市的当地日期选择。未指定日期时用 current；跨多天时用 three_days。",
        },
    },
    ["city", "period"],
)
JEV = schema("draft_chat_reply", "起草当前用户要回复的原话，选一条候选给用户复制。")
VOLUME = schema("get_volume", "读取此 Windows 电脑的当前系统音量。")
REMINDERS = schema("list_timers", "读取用户已有的本地待办提醒。")
RECORD_LESSON = schema(
    "record_self_lesson",
    "当用户纠正了 MOSS 的错误、指出回复毛病或教导新规则时，沉淀一条可验证的长期自我迭代规则（RSI）。",
    {
        "scope": {"type": "string", "enum": ["routing", "tone", "preference", "tool", "fact"],
                  "description": "规则适用域：routing(路由点歌)、tone(语气长度)、preference(个人偏好)、tool(工具调用)、fact(事实习惯)。"},
        "trigger": {"type": "string", "description": "触发场景关键词（6-40字）。"},
        "rule": {"type": "string", "description": "下次必须遵守的具体可执行规则（10-120字）。"},
        "mistake": {"type": "string", "description": "本次做错或引发不满的地方（选填）。"},
    },
    ["scope", "trigger", "rule"],
)
REVIEW_LESSONS = schema(
    "review_self_lessons",
    "查看 MOSS 已学习的自我迭代经验、对近期对话执行一次 RSI 复盘迭代，或淘汰指定编号的旧经验。",
    {
        "action": {"type": "string", "enum": ["list", "iterate", "retire"],
                   "description": "list 查看生效经验；iterate 复盘近期对话并沉淀/清理规则；retire 淘汰指定 lesson_id。"},
        "lesson_id": {"type": "integer", "description": "仅 action=retire 时需要，规则编号。"},
    },
    ["action"],
)
SEARCH_KNOWLEDGE = schema(
    "search_knowledge",
    "在 MOSS 本机 SQLite-SAG（混合向量 + FTS5 + 事件实体动态超边多跳）知识库中检索相关资料、项目笔记与关联线索。",
    {"query": {"type": "string", "description": "要查询的问题、实体、项目名或主题。"}},
    ["query"],
)
INGEST_KNOWLEDGE = schema(
    "ingest_knowledge",
    "将一段结构化知识、项目资料、会议结论或用户文档写入本机 SAG 知识库，自动抽取实体并建立动态超边关联。",
    {
        "title": {"type": "string", "description": "知识标题（4-60字）。"},
        "content": {"type": "string", "description": "知识正文内容（10-2000字）。"},
    },
    ["title", "content"],
)
RECALL_POINTER_TOOL = schema(
    "recall_turn_context",
    "根据早期压缩摘要或 SAG 线索里的来源指针（如 turn:a1b2c3d4 或 E#12）回查完整原文细节，避免摘要压缩导致遗忘失真。",
    {"pointer": {"type": "string", "description": "来源指针，如 turn:a1b2c3d4、a1b2c3d4 或 E#12。"}},
    ["pointer"],
)
BRIEFING_TOOL = schema(
    "get_situational_briefing",
    "生成当前时刻的 550W 态势简报（融合实时时间、今日日程、待办提醒、便签、主机状态、RSI/SAG 与冲突提醒）。",
)
CONSOLIDATE_TOOL = schema(
    "consolidate_world_model",
    "执行一次夜间做梦式深度整理：同步便签/记忆/RSI 至 SAG 知识库，重建个人世界模型并排查项目时间冲突。",
)
BG_JOB_TOOL = schema(
    "submit_background_job",
    "提交一个异步后台长任务（不阻塞当前对话，进度实时刷屏到小屏，完成后自动入库 SAG 并推送通知）。",
    {
        "kind": {"type": "string", "enum": ["web_research", "folder_digest"],
                 "description": "web_research=后台多轮联网深度调研并入库 SAG；folder_digest=扫描本地目录文档分块入库 SAG。"},
        "title": {"type": "string", "description": "任务标题（4-60字）。"},
        "topic_or_path": {"type": "string", "description": "web_research 填调研主题；folder_digest 填本地绝对目录或文件路径。"},
    },
    ["kind", "title", "topic_or_path"],
)
FUI_WIDGET_TOOL = schema(
    "render_fui_widget",
    "在控制台对话区即时生成一块流浪地球风可交互 FUI 活组件（支持实时滑块计算器、对比表格、指标卡片或可勾选清单）。",
    {
        "kind": {"type": "string", "enum": ["calculator", "comparison", "checklist", "metrics"],
                 "description": "calculator=交互滑块计算器；comparison=多方案对比表；checklist=可勾选执行清单；metrics=核心指标看板。"},
        "title": {"type": "string", "description": "组件标题（4-40字）。"},
        "subtitle": {"type": "string", "description": "副标题或说明（选填）。"},
        "spec_json": {
            "type": "string",
            "description": (
                "严格合法 JSON 字符串。"
                "calculator 格式：{\"inputs\":[{\"id\":\"p\",\"label\":\"本金(万)\",\"min\":10,\"max\":500,\"step\":5,\"value\":100}],\"outputs\":[{\"label\":\"月供(元)\",\"expr\":\"p*10000*0.004\",\"unit\":\"元\"}]}；"
                "comparison 格式：{\"columns\":[\"方案\",\"成本\",\"亮点\"],\"rows\":[[\"A\",\"低\",\"稳\"]] }；"
                "checklist 格式：{\"items\":[{\"text\":\"步骤1\",\"done\":false}]}；"
                "metrics 格式：{\"items\":[{\"label\":\"指标\",\"value\":\"96\",\"unit\":\"分\",\"state\":\"ok\"}]}。"
            ),
        },
    },
    ["kind", "title", "spec_json"],
)
SAVE_SKILL_TOOL = schema(
    "save_custom_skill",
    "把用户常做的工作流或快捷指令沉淀为一条可一键点击触发的专属技能（Skill）。",
    {
        "name": {"type": "string", "description": "技能名称（2-20字）。"},
        "description": {"type": "string", "description": "用途简介（选填）。"},
        "prompt": {"type": "string", "description": "点击该技能时要发送给 MOSS 执行的完整指令。"},
        "required_tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该技能需要解锁的工具名称列表（选填，不填则由系统自动推断）。",
        },
    },
    ["name", "prompt"],
)
RUN_SKILL_TOOL = schema(
    "run_custom_skill",
    "按技能名称或编号调出用户已保存的专属技能（Custom Skill）指令模板，并在本轮立即按该技能内容继续调用工具完成。",
    {"name_or_id": {"type": "string", "description": "技能名称关键词或编号；留空则列出全部已保存技能。"}},
)
CREATE_ROUTINE_TOOL = schema(
    "create_recurring_task",
    "创建一条持久化的周期例行任务或每日/每周早报（如每天 08:30 早报、工作日 09:00 简报、每 45 分钟起身提醒、每周五 17:00 周报整理）。",
    {
        "name": {"type": "string", "description": "周期任务名称（2-40字）。"},
        "schedule": {"type": "string", "description": "周期计划，如 'daily 08:30'、'weekdays 08:30'、'weekly 4 17:00'、'interval 45m' 或中文 '每天8:30'/'工作日8点半'/'每45分钟'。"},
        "action_kind": {"type": "string", "enum": ["reminder", "briefing", "dream", "prompt", "skill"],
                        "description": "reminder=周期提醒；briefing=自动推送态势早报；dream=定期做梦整理；prompt=自定义AI只读推演；skill=执行已保存技能名称或ID。"},
        "payload": {"type": "string", "description": "提醒正文或执行内容（选填）。"},
    },
    ["name", "schedule"],
)
LIST_ROUTINES_TOOL = schema(
    "list_recurring_tasks",
    "列出当前所有已配置的周期例行任务、定时早报及启停状态。",
)
DELETE_ROUTINE_TOOL = schema(
    "delete_recurring_task",
    "按编号或名称关键词删除一条周期例行任务。",
    {"target": {"type": "string", "description": "周期任务编号或名称关键词。"}},
    ["target"],
)
MUSIC_PREF_TOOL = schema(
    "manage_music_pref",
    "收藏当前播放的歌曲、将不喜欢歌曲加入跳过黑名单、取消标记或查看已收藏歌单。",
    {
        "action": {"type": "string", "enum": ["favorite", "blocked", "clear", "list"],
                   "description": "favorite=收藏当前/指定曲目；blocked=以后不再推荐；clear=取消标记；list=查看收藏列表。"},
        "media_id": {"type": "string", "description": "曲目编号（留空默认取当前正在播放或最近点播的曲目）。"},
    },
    ["action"],
)
SLEEP_TIMER_TOOL = schema(
    "set_music_sleep_timer",
    "设置或取消音乐睡眠定时（到时自动渐弱停播）。minutes=0 表示取消睡眠定时。",
    {"minutes": {"type": "integer", "description": "多少分钟后停播（1-240；0 表示取消）。"}},
    ["minutes"],
)
HA_LIST_TOOL = schema(
    "ha_list_devices",
    "查询 Home Assistant 智能家居设备列表与传感器状态（灯光、空调、开关、温湿度、场景等；未填令牌时返回 dry_run 演练设备）。",
    {"domain": {"type": "string", "description": "可选过滤域：light/switch/climate/sensor/scene/fan/cover，留空列全部。"}},
)
HA_CONTROL_TOOL = schema(
    "ha_control_device",
    "通过 Home Assistant 控制智能家居设备（如开关灯、调亮度、调空调温度、触发场景）。调用前建议先确认 entity_id。",
    {
        "entity_id": {"type": "string", "description": "设备 ID，例如 light.living_room、climate.study_ac、scene.night_focus。"},
        "service": {"type": "string", "description": "服务名，例如 turn_on、turn_off、toggle、set_temperature。"},
        "data_json": {"type": "string", "description": "可选 JSON 参数，例如 {\"brightness_pct\":30} 或 {\"temperature\":24}。"},
    },
    ["entity_id", "service"],
)
HW_MOSS_TOOL = schema(
    "control_physical_moss",
    "向实体 550W 硬件协议桥（moss-hw-v2）下发灯光模式、舵机云台姿态/动作或小屏幕显示文本。",
    {
        "mode": {"type": "string", "enum": ["standby", "listening", "thinking", "speaking", "alert", "dnd"],
                 "description": "红眼/氛围灯光模式（选填）。"},
        "yaw_deg": {"type": "number", "description": "水平舵机角度 -60 到 60（选填）。"},
        "pitch_deg": {"type": "number", "description": "俯仰舵机角度 -30 到 30（选填）。"},
        "gesture": {"type": "string", "enum": ["idle", "nod", "shake", "look_user", "patrol", "reset"],
                    "description": "舵机动作预设（选填）。"},
        "screen_text": {"type": "string", "description": "同步到实体机箱小屏的短文本（选填）。"},
    },
)
DESKTOP_UIA_TOOL = schema(
    "inspect_desktop_ui",
    "只读采集当前 Windows 活动前景窗口的无障碍 UI Automation（UIA）控件树与坐标（按钮、输入框、标签页），并在识别稀疏时给出视觉 SoM 兜底建议。",
    {"max_elements": {"type": "integer", "description": "最多返回控件数（5-50，默认 24）。"}},
)
CAPABILITIES = schema("list_capabilities", "查看当前 MOSS 已经接通的能力和入口。")

ALL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(CLOCK, "clock", is_readonly=True, parallel_safe=True, cloud_core=True, local_always=True),
    ToolSpec(WEATHER, "weather", is_readonly=True, cloud_core=True, local_always=True),
    ToolSpec(GET_NEWS_TOOL, "news", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(FETCH_WEB_TOOL, "web", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(WEB_SEARCH, "web", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(CALC_MATH_TOOL, "math", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(CALC_DATE_TOOL, "date", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(READ_FILE_TOOL, "files", is_readonly=True, parallel_safe=True, host_only=True),
    ToolSpec(LIST_DIR_TOOL, "files", is_readonly=True, parallel_safe=True, host_only=True),
    ToolSpec(SAVE_NOTE, "notes", host_only=True, cloud_core=True),
    ToolSpec(SEARCH_NOTES, "notes", is_readonly=True, parallel_safe=True, host_only=True, cloud_core=True),
    ToolSpec(MANAGE_NOTE, "notes", host_only=True),
    ToolSpec(SAVE_MEMORY_TOOL, "memory", cloud_core=True),
    ToolSpec(SEARCH_MEMORY_TOOL, "memory", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(FORGET_MEMORY_TOOL, "memory"),
    ToolSpec(SEARCH_KNOWLEDGE, "sag", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(INGEST_KNOWLEDGE, "sag", host_only=True),
    ToolSpec(RECALL_POINTER_TOOL, "memory", is_readonly=True, parallel_safe=True),
    ToolSpec(REMINDERS, "timers", is_readonly=True, parallel_safe=True, host_only=True),
    ToolSpec(CREATE_TIMER, "timers", host_only=True, cloud_core=True),
    ToolSpec(RESCHEDULE_TIMER, "timers", host_only=True),
    ToolSpec(CANCEL_TIMER, "timers", host_only=True),
    ToolSpec(QUERY_CALENDAR, "calendar", is_readonly=True, parallel_safe=True, host_only=True),
    ToolSpec(CREATE_CALENDAR_TOOL, "calendar", host_only=True),
    ToolSpec(RESCHEDULE_CALENDAR_TOOL, "calendar", host_only=True),
    ToolSpec(CANCEL_CALENDAR_TOOL, "calendar", host_only=True),
    ToolSpec(BRIEFING_TOOL, "briefing", is_readonly=True, parallel_safe=True),
    ToolSpec(CONSOLIDATE_TOOL, "dream", host_only=True),
    ToolSpec(BG_JOB_TOOL, "jobs", host_only=True),
    ToolSpec(FUI_WIDGET_TOOL, "widget"),
    ToolSpec(SAVE_SKILL_TOOL, "skills", host_only=True),
    ToolSpec(RUN_SKILL_TOOL, "skills", host_only=True),
    ToolSpec(CREATE_ROUTINE_TOOL, "routines", host_only=True),
    ToolSpec(LIST_ROUTINES_TOOL, "routines", is_readonly=True, parallel_safe=True, host_only=True),
    ToolSpec(DELETE_ROUTINE_TOOL, "routines", host_only=True),
    ToolSpec(SONG, "media"),
    ToolSpec(CATALOG, "media", is_readonly=True, parallel_safe=True),
    ToolSpec(MUSIC_PREF_TOOL, "media", host_only=True),
    ToolSpec(SLEEP_TIMER_TOOL, "media"),
    ToolSpec(DEVICE_STATUS, "device", is_readonly=True, parallel_safe=True, cloud_core=True),
    ToolSpec(VOLUME, "volume", is_readonly=True, parallel_safe=True),
    ToolSpec(CAPABILITIES, "capabilities", is_readonly=True, parallel_safe=True),
    ToolSpec(JEV, "jev"),
    ToolSpec(RECORD_LESSON, "rsi", host_only=True),
    ToolSpec(REVIEW_LESSONS, "rsi", host_only=True),
    ToolSpec(HA_LIST_TOOL, "iot", is_readonly=True, parallel_safe=True, host_only=True),
    ToolSpec(HA_CONTROL_TOOL, "iot", host_only=True),
    ToolSpec(HW_MOSS_TOOL, "hw", host_only=True),
    ToolSpec(DESKTOP_UIA_TOOL, "desktop_uia", is_readonly=True, parallel_safe=True, host_only=True),
)

TOOL_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in ALL_TOOL_SPECS}
PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset(spec.name for spec in ALL_TOOL_SPECS if spec.parallel_safe)
IDEMPOTENT_READONLY_TOOLS: frozenset[str] = frozenset(spec.name for spec in ALL_TOOL_SPECS if spec.is_readonly)
HOST_ONLY_TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in ALL_TOOL_SPECS if spec.host_only)
CLOUD_CORE_TOOLS: tuple[dict, ...] = tuple(spec.schema for spec in ALL_TOOL_SPECS if spec.cloud_core)

"""Unified domain classification and dynamic tool assembly (derived from ToolSpec registry)."""
from __future__ import annotations

import re
import unicodedata

import numpy as np

from brain.prompts import FAREWELL_RE, is_emotion_vent
from sag_knowledge import embed_text_local
from brain.registry import (
    BG_JOB_TOOL,
    BRIEFING_TOOL,
    CALC_DATE_TOOL,
    CALC_MATH_TOOL,
    CANCEL_CALENDAR_TOOL,
    CANCEL_TIMER,
    CAPABILITIES,
    CATALOG,
    CLOCK,
    CLOUD_CORE_TOOLS,
    CONSOLIDATE_TOOL,
    CREATE_CALENDAR_TOOL,
    CREATE_ROUTINE_TOOL,
    CREATE_TIMER,
    DELETE_ROUTINE_TOOL,
    DESKTOP_UIA_TOOL,
    DEVICE_STATUS,
    FETCH_WEB_TOOL,
    FORGET_MEMORY_TOOL,
    FUI_WIDGET_TOOL,
    GET_NEWS_TOOL,
    HA_CONTROL_TOOL,
    HA_LIST_TOOL,
    HOST_ONLY_TOOL_NAMES,
    HW_MOSS_TOOL,
    INGEST_KNOWLEDGE,
    LIST_DIR_TOOL,
    LIST_ROUTINES_TOOL,
    MANAGE_NOTE,
    MUSIC_PREF_TOOL,
    QUERY_CALENDAR,
    READ_FILE_TOOL,
    RECALL_POINTER_TOOL,
    RECORD_LESSON,
    REMINDERS,
    RESCHEDULE_CALENDAR_TOOL,
    RESCHEDULE_TIMER,
    REVIEW_LESSONS,
    RUN_SKILL_TOOL,
    SAVE_MEMORY_TOOL,
    SAVE_NOTE,
    SAVE_SKILL_TOOL,
    SEARCH_KNOWLEDGE,
    SEARCH_MEMORY_TOOL,
    SEARCH_NOTES,
    SLEEP_TIMER_TOOL,
    SONG,
    VOLUME,
    WEATHER,
    WEB_SEARCH,
)
from brain.weather_flow import WEATHER_TOPIC
from media_chat import NEGATION
from rsi_engine import CORRECTION_SIGNAL, EXPLICIT_TEACH_SIGNAL, SELF_ITERATE_QUERY, detect_correction_signal
from weather_location import NO_LOOKUP


SONG_TOOL_REQUEST = re.compile(
    r"(?:^(?:(?:算了|好吧|行吧|那就|那|先|再|顺便|我说|我是说|请问|请你?|麻烦你?|帮我|给我|现在)[\s，,、]*)*"
    r"(?:播放|点歌|播(?:放|一首|首|一下)?|放(?:一首|首|点|一下)?|(?:我想|我要|想|要)?听|(?:再|先)?来(?:一首|首|点)|"
    r"(?:打开|收听|开启).{0,10}(?:电台|广播电台|收音机|fm)|"
    r"(?:找找|搜(?:索)?).{0,24}(?:来听|来放|能听的)?(?:歌|音乐|曲子|音频|电台)|"
    r"推荐.{0,18}(?:好听的|轻|纯|舒缓|助眠)?(?:歌|歌曲|音乐|曲子|电台)(?!.{0,24}(?:对比|区别|差异|优缺点|分析|比较|历史|原理|介绍))))|"
    r"(?:放(?:一?首|点)|来(?:一?首|点)|听(?:一?首|个|点)).{0,18}(?:歌|音乐|曲子|纯音乐|助眠|舒缓|轻音乐|电台)|"
    r"\b(?:audius|navidrome|jamendo|radio\s*browser)\b",
    re.IGNORECASE,
)
MEDIA_CATALOG_TOPIC = re.compile(r"媒体库|多媒体库|曲库|歌库|歌单|歌手|有谁的歌|有什么歌|本地音乐|多少首歌")
WEB_SEARCH_TOPIC = re.compile(
    r"联网|在线(?:搜|查)|网上(?:搜|查)|搜(?:索|一下|一搜)|查(?:一下|一查).{0,18}(?:新闻|资讯|价格|金价|股价|汇率|比分|攻略|最新|资料|百科|官网)|"
    r"最新(?:消息|新闻|动态|价格|版本|发布|进展)|今天.{0,10}(?:新闻|热搜|金价|股价|油价|大事)|"
    r"国际金价|金价走势|黄金价格|多少钱|什么行情|市值|口碑|测评|热搜|头条|百度|必应|谷歌|wikipedia|维基",
    re.I,
)
URL_TOPIC = re.compile(r"https?://|网址|链接|原文|正文|详细读|点开看", re.I)
DATE_CALC_TOPIC = re.compile(
    r"还有(?:多[少久]|几天)|隔了?几天|相距|相差几天|星期几|周几|礼拜几|哪一天|往后推|往前推|天[之后以]?后|倒计时.*天|几天后|几天前",
    re.I,
)
WORLD_CLOCK_TOPIC = re.compile(
    r"(?:纽约|伦敦|东京|悉尼|洛杉矶|旧金山|巴黎|柏林|新加坡|首尔|莫斯科|迪拜|多伦多|温哥华|芝加哥|UTC|GMT).{0,8}(?:几点|时间|时差)",
    re.I,
)
MEMORY_TOPIC = re.compile(
    r"记住|记得|记忆|忘掉|忘记|删掉.*记忆|别记了|我(?:平时|一般|通常|最)?(?:喜欢|偏爱|不吃|过敏|忌口|住在|坐标|定居|工作|从事|常用)|"
    r"我的(?:工号|职位|城市|公司|习惯)|下周出差|吃什么|喝什么|点什么|穿什么|怎么选",
    re.I,
)
DIET_CADENCE_ONLY_TOPIC = re.compile(r"^(?:今天|中午|晚上|明早)?(?:该|应该)?吃(?:点儿?)?(?:啥|什么)[？?呢]?$")
NEWS_TOPIC = re.compile(r"新闻|热搜|头条|热点|要闻|资讯|大事|top\s*\d+", re.I)
MATH_TOPIC = re.compile(r"计算|算一下|精确算|利息|年利率|复利|折扣|折后|汇率|均摊|分摊|总额|合计|百分比|保留.{0,3}位小数|[0-9]+\s*[*×/÷+\-]\s*[0-9]+")
FILE_READ_TOPIC = re.compile(r"[A-Za-z]:\\|/\S+\.(?:md|txt|json|csv|tsv|log|py|js|ts|yaml|yml|docx|xlsx|pdf)|读(?:一下|取)?(?:这个|本地)?文件|看看.{0,18}目录|总结(?:一下)?(?:这个)?(?:文件|文档|报告)", re.I)
NOTE_TOPIC = re.compile(r"便签|记(?:一下|下来|到便签|进便签|一条)|备忘|存起来|先存|帮我存|归档|恢复便签")
SAG_TOPIC = re.compile(r"知识库|SAG|资料库|文档库|入库|存入知识|多跳|关联线索|项目资料|档案|笔记里|红线|分工|先前|之前说过|讨论过|预算|规范|结论|文档|会议|纪要|老[张王李陈杨赵]|turn:[a-f0-9]{4,}|查(?:一下|查)?(?:我(?:的)?)?(?:资料|知识|档案|记录|项目)")
BRIEFING_TOPIC = re.compile(r"态势简报|综合简报|每日简报|早间简报|早安简报|今日简报|晚间简报|值守简报|(?:汇报|播报)(?:一下|一份)?(?:今天|今日|目前)?(?:的)?(?:综合)?态势")
DREAM_TOPIC = re.compile(r"夜间整理|做梦整理|深度整理|整理(?:我的)?(?:知识|记忆|世界模型)|个人世界模型|排查(?:知识)?冲突|知识冲突")
BG_JOB_TOPIC = re.compile(r"后台(?:帮我|去|深度)?(?:调研|研究|查|搜|整理|索引|扫描)|深度调研|批量入库|挂后台|异步(?:任务|调研)")
FUI_WIDGET_TOPIC = re.compile(r"计算器|对比表|表格组件|比较(?:一下)?.{0,20}(?:区别|差异|参数|性价比|优缺点)|生成.{0,12}(?:看板|组件|面板|界面|清单|表格|计算)|交互(?:组件|界面|计算)|房贷|提前还款|复利|checklist|清单", re.I)
SKILL_TOPIC = re.compile(r"(?:封装|沉淀|保存|新建|创建|运行|执行).{0,10}(?:技能|快捷(?:指令|按钮)|skill|工作流|早报巡检)", re.I)
ROUTINE_TOPIC = re.compile(r"每天|每日|每个月|每月|工作日|每周|每星期|每(?:个?)?礼拜|每隔|例行|早报|晚报|周期")
TIMER_TOPIC = re.compile(r"提醒|闹钟|倒计时|(?<![\u4e00-\u9fff])(?:延后|推迟|提前|改期|取消)(?!.*(?:日程|日历|会议|排期|便签|备忘|例行|每周|每天))")
MUSIC_PREF_TOPIC = re.compile(r"收藏|红心|喜欢这首|拉黑|别再放|睡眠定时|停歌")
HA_TOPIC = re.compile(
    r"home\s*assistant|智能家居|客厅.{0,8}(?:灯|空调|插座|窗帘)|书房.{0,8}(?:灯|台灯|空调|温度|湿度|热|冷|闷)|"
    r"屋里.{0,8}(?:暗|亮|热|冷|闷)|(?:准备)?睡觉模式|"
    r"(?:打开|关闭|关掉|开启|调暗|调亮|熄灭).{0,8}(?:灯|台灯|主灯|空调|风扇|窗帘|插座|开关|场景)|"
    r"把.{0,12}(?:灯|空调|风扇|窗帘|插座|开关).{0,6}(?:打开|关掉|关闭|调)|家里(?:温度|湿度|设备|灯光)|空调调到|室内(?:温度|湿度)",
    re.I,
)
HW_MOSS_TOPIC = re.compile(r"实体\s*(?:moss|550w)|舵机|云台|点点头|摇摇头|红眼|镜头转向|抬头看|看我|机箱灯|实体小屏|专注中|勿扰模式|硬件协议", re.I)
DESKTOP_UI_TOPIC = re.compile(r"前景窗口|桌面控件|界面控件|窗口布局|无障碍树|uia|屏幕上有(?:什么|哪些)(?:按钮|控件|入口)", re.I)
CALENDAR_TOPIC = re.compile(r"日程|日历|安排|计划|会议|行程|排期|改期|今天下午|明天上午|明天下午|后天(?:上午|下午|晚上)?|大后天|下周")
DEVICE_TOPIC = re.compile(r"CPU|GPU|显卡|内存|硬盘|磁盘|电量|发烫|发热|温度|卡顿|有点卡|好卡|电脑状态|硬件", re.I)
COMPOUND_OTHER_INTENT = re.compile(
    r"顺便|顺带|并且|同时|然后|接着|再把|再帮|再查|再看|再定|再放|再听|还要|另外|以及|并(?:放|播|查|记)|"
    r"[、，,;；]\s*(?:(?:请|帮我|给我|那就)\s*)?(?:并|再|还|也|顺便|我想|我要)(?:提醒|设个?闹钟|倒计时|记(?:一下|下来|进)|查|搜|算|开|关|放|播|听|控|写)"
)
_LEADING_GREETING_VOCATIVE = re.compile(
    r"^(?:(?:(?:MOSS|莫斯|摩斯|你好|您好|早安|早上好|下午好|晚上好|哈喽|嗨|嘿|喂|在吗|请问|麻烦(?:你|您)?|对了|那个|话说)[呀啊呢吧]*)[，,、\s!！:：]*)+"
)
CAPABILITY_QUERY = re.compile(
    r"能做什么|会做什么|能干什么|有哪些(?:工具|功能|能力)|什么(?:工具|功能|能力)|"
    r"能用哪些|支持什么|怎么用|帮助|在线搜索|联网搜索|能不能.*(?:搜|查|播|放|打开)|"
    r"哪个模型|大脑状态|自身状态|什么模型在运行|系统自感知|状态自检|健康状态"
)
TOOL_SUBJECT = re.compile(
    r"查询|查一下|搜索|联网|在线|实时|本机|当前|现在几点|今天几号|几点钟|日期|星期几|"
    r"新闻|热搜|热点|头条|金价|股价|汇率|油价|计算|算一下|精确算|利息|折扣|分摊|均摊|复利|"
    r"播放|点歌|歌曲|歌手|音乐|音轨|视频|媒体|曲库|音量|声音大小|提醒|闹钟|倒计时|"
    r"便签|备忘|存起来|记下来|日程|日历|CPU|GPU|显卡|内存|电量|温度|发烫|有点卡|"
    r"智能家居|主灯|台灯|客厅|书房|空调|关掉.{0,6}(?:灯|空调|风扇|窗帘)|"
    r"打开(?:https?|网页|文件|目录|.{0,6}(?:灯|空调|风扇|窗帘|电台))|关闭.{0,6}(?:灯|空调|风扇|窗帘)|"
    r"知识库|查.{0,4}项目|项目资料|红线要求|部署文档|"
    r"实体|舵机|云台|简报|整理.{0,8}(?:文件|目录|资料|知识|记忆)|调研|组件|看板|表格|"
    r"窗口|屏幕|剪贴板|文件|目录|草稿|工具|展开说说|查一下背景",
    re.I,
)
_CONCEPTUAL_MUSIC_COMPARE = re.compile(
    r"对比|区别|差异|优缺点|分析|对比表|比较|为什么|原理|科普|介绍|哪张专辑|谁唱的|什么是"
)


def is_compound_request(colloquial_text: str, weather_text: str = "") -> bool:
    """Detect genuine multi-intent turns while stripping leading greetings/vocatives like 'MOSS，...'."""
    stripped = _LEADING_GREETING_VOCATIVE.sub("", str(colloquial_text or "")).strip()
    if COMPOUND_OTHER_INTENT.search(stripped or str(colloquial_text or "")):
        return True
    w_body = _LEADING_GREETING_VOCATIVE.sub("", str(weather_text or colloquial_text or "")).strip()
    has_song = bool(SONG_TOOL_REQUEST.search(stripped) and not _CONCEPTUAL_MUSIC_COMPARE.search(stripped))
    if has_song and any(
        pat.search(w_body)
        for pat in (WEATHER_TOPIC, CALENDAR_TOPIC, NOTE_TOPIC, TIMER_TOPIC, HA_TOPIC, MATH_TOPIC, DATE_CALC_TOPIC)
    ):
        return True
    if re.search(r"[、，,;；]", stripped):
        domain_hits = sum(
            1 for pat in (
                WEATHER_TOPIC, CALENDAR_TOPIC, NOTE_TOPIC, TIMER_TOPIC,
                HA_TOPIC, MATH_TOPIC, DATE_CALC_TOPIC, WEB_SEARCH_TOPIC,
            )
            if pat.search(w_body if pat is WEATHER_TOPIC else stripped)
        ) + (1 if has_song else 0)
        if domain_hits >= 2:
            return True
    return False


def has_any_tool_topic(colloquial_text: str, weather_text: str, text: str, earlier: dict | None = None) -> bool:
    if any(
        pat.search(colloquial_text)
        for pat in (
            TOOL_SUBJECT, CALENDAR_TOPIC, NEWS_TOPIC, WEB_SEARCH_TOPIC, URL_TOPIC, DATE_CALC_TOPIC,
            WORLD_CLOCK_TOPIC, MEMORY_TOPIC, CAPABILITY_QUERY, FILE_READ_TOPIC, MATH_TOPIC, NOTE_TOPIC, SAG_TOPIC,
            BRIEFING_TOPIC, DREAM_TOPIC, BG_JOB_TOPIC, FUI_WIDGET_TOPIC, SKILL_TOPIC, ROUTINE_TOPIC,
            TIMER_TOPIC, MUSIC_PREF_TOPIC, HA_TOPIC, HW_MOSS_TOPIC, DEVICE_TOPIC, SONG_TOOL_REQUEST,
            MEDIA_CATALOG_TOPIC, CORRECTION_SIGNAL, EXPLICIT_TEACH_SIGNAL, SELF_ITERATE_QUERY,
        )
    ):
        return True
    if WEATHER_TOPIC.search(weather_text):
        return True
    if detect_correction_signal(text, earlier):
        return True
    return False


def _add_shared_packs(
    bag: list[dict],
    text: str,
    *,
    wide: bool = False,
    earlier_tools: set[str] = frozenset(),
    song_requested: bool = False,
    follow_play_hint: bool = False,
    earlier_music: bool = False,
    has_followup_ref: bool = False,
    earlier: dict | None = None,
) -> None:
    conceptual_music = bool(_CONCEPTUAL_MUSIC_COMPARE.search(text) and not song_requested)
    if (song_requested or (follow_play_hint and not conceptual_music) or (earlier_music and has_followup_ref)) and not (
        NEGATION.search(text) and not song_requested
    ):
        bag.extend([SONG, MUSIC_PREF_TOOL, SLEEP_TIMER_TOOL])
    if (MEDIA_CATALOG_TOPIC.search(text) and not conceptual_music) or "list_media_catalog" in earlier_tools:
        bag.append(CATALOG)
    if NEWS_TOPIC.search(text) or "get_news_headlines" in earlier_tools:
        bag.extend([GET_NEWS_TOOL, FETCH_WEB_TOOL, WEB_SEARCH])
    if WEB_SEARCH_TOPIC.search(text) or URL_TOPIC.search(text) or "web_search" in earlier_tools or "fetch_web_page" in earlier_tools:
        bag.extend([WEB_SEARCH, FETCH_WEB_TOOL])
    if MATH_TOPIC.search(text) or wide:
        bag.append(CALC_MATH_TOOL)
    if DATE_CALC_TOPIC.search(text) or CALENDAR_TOPIC.search(text) or wide:
        bag.append(CALC_DATE_TOOL)
    if WORLD_CLOCK_TOPIC.search(text):
        bag.append(CLOCK)
    if FILE_READ_TOPIC.search(text) or wide or "list_local_directory" in earlier_tools or "read_local_file" in earlier_tools:
        bag.extend([LIST_DIR_TOOL, READ_FILE_TOOL])
    if NOTE_TOPIC.search(text) or wide or any(x in earlier_tools for x in ("save_note", "search_notes", "manage_note")):
        bag.extend([SAVE_NOTE, SEARCH_NOTES, MANAGE_NOTE])
    if MEMORY_TOPIC.search(text) or wide or any(x in earlier_tools for x in ("save_user_memory", "search_user_memory", "forget_user_memory")):
        bag.extend([SAVE_MEMORY_TOOL, SEARCH_MEMORY_TOOL, FORGET_MEMORY_TOOL, RECALL_POINTER_TOOL])
    if SAG_TOPIC.search(text) or wide or "search_knowledge" in earlier_tools or "turn:" in text.lower():
        bag.extend([SEARCH_KNOWLEDGE, INGEST_KNOWLEDGE, RECALL_POINTER_TOOL])
    if CALENDAR_TOPIC.search(text) or wide or any(x in earlier_tools for x in ("query_calendar", "create_calendar_event", "reschedule_calendar_event", "cancel_calendar_event")):
        bag.extend([QUERY_CALENDAR, CREATE_CALENDAR_TOOL, RESCHEDULE_CALENDAR_TOOL, CANCEL_CALENDAR_TOOL])
    if TIMER_TOPIC.search(text) or wide or any(x in earlier_tools for x in ("create_timer", "list_timers", "reschedule_timer", "cancel_timer")):
        bag.extend([REMINDERS, CREATE_TIMER, RESCHEDULE_TIMER, CANCEL_TIMER])
    if BRIEFING_TOPIC.search(text) or "简报" in text:
        bag.append(BRIEFING_TOOL)
    if DREAM_TOPIC.search(text):
        bag.append(CONSOLIDATE_TOOL)
    if BG_JOB_TOPIC.search(text):
        bag.append(BG_JOB_TOOL)
    if FUI_WIDGET_TOPIC.search(text):
        bag.append(FUI_WIDGET_TOOL)
    if SKILL_TOPIC.search(text) or "run_custom_skill" in earlier_tools:
        bag.extend([SAVE_SKILL_TOOL, RUN_SKILL_TOOL])
    if ROUTINE_TOPIC.search(text):
        bag.extend([CREATE_ROUTINE_TOOL, LIST_ROUTINES_TOOL, DELETE_ROUTINE_TOOL])
    if MUSIC_PREF_TOPIC.search(text) or (earlier_music and not conceptual_music):
        bag.extend([MUSIC_PREF_TOOL, SLEEP_TIMER_TOOL])
    if HA_TOPIC.search(text) or wide or "ha_list_devices" in earlier_tools or "ha_control_device" in earlier_tools:
        bag.extend([HA_LIST_TOOL, HA_CONTROL_TOOL])
    if HW_MOSS_TOPIC.search(text):
        bag.append(HW_MOSS_TOOL)
    if DESKTOP_UI_TOPIC.search(text):
        bag.append(DESKTOP_UIA_TOOL)
    if DEVICE_TOPIC.search(text):
        bag.append(DEVICE_STATUS)
    if CAPABILITY_QUERY.search(text):
        bag.append(CAPABILITIES)
    if re.search(r"音量|声音大小", text):
        bag.append(VOLUME)
    if CORRECTION_SIGNAL.search(text) or EXPLICIT_TEACH_SIGNAL.search(text) or detect_correction_signal(text, earlier):
        bag.append(RECORD_LESSON)
    if SELF_ITERATE_QUERY.search(text):
        bag.extend([REVIEW_LESSONS, RECORD_LESSON])


_SEMANTIC_PROTOTYPES: tuple[tuple[str, tuple[dict, ...]], ...] = (
    ("开关灯光 调节室内亮度 屋里太暗了 客厅台灯照明 空调温度太热风扇 回家离开睡眠场景", (HA_LIST_TOOL, HA_CONTROL_TOOL)),
    ("长期记住我的个人偏好 工作城市 过敏禁忌 工号职位 习惯", (SAVE_MEMORY_TOOL, SEARCH_MEMORY_TOOL)),
    ("查询或创建日程 会议排期 出差安排 改期取消", (QUERY_CALENDAR, CREATE_CALENDAR_TOOL)),
    ("检查项目资料 历史记录 团队分工 架构红线 笔记文档", (SEARCH_KNOWLEDGE, SEARCH_NOTES)),
    ("联网搜索 最新消息 实时新闻 网页百科资料", (WEB_SEARCH, GET_NEWS_TOOL)),
)
_SEMANTIC_VECS: list[tuple[np.ndarray, tuple[dict, ...]]] | None = None


def _semantic_tool_candidates(text: str, *, threshold: float = 0.44) -> list[dict]:
    global _SEMANTIC_VECS
    clean = str(text or "").strip()
    if len(clean) < 4 or _CONCEPTUAL_MUSIC_COMPARE.search(clean) or is_emotion_vent(clean):
        return []
    if _SEMANTIC_VECS is None:
        _SEMANTIC_VECS = [(embed_text_local(proto), schemas) for proto, schemas in _SEMANTIC_PROTOTYPES]
    q_vec = embed_text_local(clean)
    added: list[dict] = []
    for p_vec, schemas in _SEMANTIC_VECS:
        if float(np.dot(q_vec, p_vec)) >= threshold:
            added.extend(schemas)
    return added


def select_tools_for_turn(
    model_id: str,
    text: str,
    *,
    earlier: dict | None = None,
    required: dict | None = None,
    ignore_memory: bool = False,
    is_host_owner: bool = True,
    song_requested: bool = False,
    follow_play_hint: bool = False,
    earlier_music: bool = False,
    day_follow: bool = False,
    previous_weather: dict | None = None,
    literal_repeat: bool = False,
    compound_request: bool = False,
    one_action: bool = False,
    length=None,
    plain_prose: bool = False,
    allowed_tools: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Intent-aware dynamic tool assembly: keeps cloud lean, brings local 4B to parity, and preserves compound tools."""
    if required and not compound_request:
        return [required]
    if literal_repeat or one_action or (length and plain_prose and not required):
        return []
    plain = unicodedata.normalize("NFKC", str(text or "")).strip()
    if plain_prose and not compound_request and not required and FAREWELL_RE.fullmatch(plain):
        return []
    if model_id == "local" and plain_prose and not compound_request and not required and not _semantic_tool_candidates(text):
        return []

    earlier_tools = set((earlier or {}).get("holo_tools") or ())
    earlier_reply = str((earlier or {}).get("reply") or "")
    has_followup_ref = bool(re.search(r"第[一二三四五六七八九十\d]+[条项个首]|刚才|那个|上一条|那条|那份|再查|展开|改|删|取消|推迟|延后|提前", text))

    shared_kw = {
        "earlier_tools": earlier_tools,
        "song_requested": song_requested,
        "follow_play_hint": follow_play_hint,
        "earlier_music": earlier_music,
        "has_followup_ref": has_followup_ref,
        "earlier": earlier,
    }
    if model_id != "local":
        tools = [required] if required else []
        tools.extend(CLOUD_CORE_TOOLS)
        _add_shared_packs(tools, text, wide=compound_request, **shared_kw)
        tools.extend(_semantic_tool_candidates(text))
        if "需要" in earlier_reply and "背景" in text:
            tools.append(FETCH_WEB_TOOL)
    else:
        prioritized: list[tuple[int, dict]] = []
        if required:
            prioritized.append((1000, required))
        clock_score = 260 if (WORLD_CLOCK_TOPIC.search(text) or re.search(r"几点|日期|星期几|几号", text)) else 45
        weather_score = 260 if (WEATHER_TOPIC.search(text) or day_follow) else 45
        prioritized.append((clock_score, CLOCK))
        prioritized.append((weather_score, WEATHER))
        if song_requested or (follow_play_hint and not _CONCEPTUAL_MUSIC_COMPARE.search(text)):
            prioritized.append((250, SONG))

        explicit_tools: list[dict] = []
        _add_shared_packs(explicit_tools, text, wide=False, **shared_kw)
        _has_cal = bool(CALENDAR_TOPIC.search(text))
        _has_timer = bool(re.search(r"提醒|闹钟|倒计时", text))
        _has_note = bool(re.search(r"便签|备忘", text))
        _want_cancel = bool(re.search(r"取消|删掉?|撤掉|作废|移除", text))
        _want_resched = bool(re.search(r"改期|推迟|延后|提前|改到|换到", text))
        _want_query = bool(re.search(r"查|有哪些|有什么|安排|列表|多少|几点", text))
        _want_forget = bool(re.search(r"忘掉|忘记|删掉.*记忆|别记了", text))
        _want_note_mgmt = bool(re.search(r"归档|(?:恢复|删(?:除|掉)?|改).{0,6}(?:便签|备忘)", text))
        _mgmt_hint = bool(_want_cancel or _want_resched or _want_forget or _want_note_mgmt or re.search(r"收藏|拉黑|睡眠|列表|有哪些", text))
        _aux_tools = frozenset({
            "list_timers", "reschedule_timer", "cancel_timer",
            "reschedule_calendar_event", "cancel_calendar_event",
            "manage_note", "forget_user_memory", "search_user_memory",
            "manage_music_pref", "set_music_sleep_timer",
            "list_recurring_tasks", "consolidate_world_model",
        })
        for t in explicit_tools:
            tname = t["name"]
            if tname == "cancel_calendar_event" and _want_cancel and (_has_cal or "create_calendar_event" in earlier_tools or "query_calendar" in earlier_tools):
                score = 268
            elif tname == "reschedule_calendar_event" and _want_resched and (_has_cal or "create_calendar_event" in earlier_tools or "query_calendar" in earlier_tools):
                score = 268
            elif tname == "query_calendar" and _want_query and _has_cal and not (_want_cancel or _want_resched):
                score = 258
            elif tname == "cancel_timer" and _want_cancel and (_has_timer or (not _has_cal and "create_timer" in earlier_tools)):
                score = 268
            elif tname == "reschedule_timer" and _want_resched and (_has_timer or "create_timer" in earlier_tools or "list_timers" in earlier_tools):
                score = 268
            elif tname == "list_timers" and _want_query and _has_timer and not (_want_cancel or _want_resched):
                score = 258
            elif tname == "forget_user_memory" and _want_forget:
                score = 268
            elif tname == "manage_note" and _want_note_mgmt and (_has_note or not _has_cal):
                score = 268
            elif tname == "ingest_knowledge" and not re.search(r"入库|存入知识|录入知识|收进知识", text):
                score = 175
            elif tname == "recall_turn_context" and not re.search(r"turn:[a-f0-9]{4,}|\[E#|原文|原话|当时怎么说|展开回忆", text, re.I):
                score = 175
            elif tname in _aux_tools:
                score = 235 if _mgmt_hint else 195
            else:
                score = 240
            prioritized.append((score, t))

        for t in _semantic_tool_candidates(text):
            prioritized.append((140, t))

        if "需要" in earlier_reply and "背景" in text:
            prioritized.append((160, FETCH_WEB_TOOL))

        if compound_request:
            wide_tools: list[dict] = []
            _add_shared_packs(wide_tools, text, wide=True, **shared_kw)
            for t in wide_tools:
                prioritized.append((30, t))

        best_by_name: dict[str, tuple[int, int, dict]] = {}
        for idx, (score, t) in enumerate(prioritized):
            name = t["name"]
            prev = best_by_name.get(name)
            if prev is None or score > prev[0]:
                best_by_name[name] = (score, prev[1] if prev else idx, t)
        tools = [item[2] for item in sorted(best_by_name.values(), key=lambda x: (-x[0], x[1]))]

    if ignore_memory:
        tools = [
            t for t in tools if t not in (
                SAVE_MEMORY_TOOL, SEARCH_MEMORY_TOOL, FORGET_MEMORY_TOOL,
                SAVE_NOTE, SEARCH_NOTES, MANAGE_NOTE,
                RECALL_POINTER_TOOL, SEARCH_KNOWLEDGE, INGEST_KNOWLEDGE, CONSOLIDATE_TOOL,
            )
        ]
    if not is_host_owner:
        tools = [t for t in tools if t["name"] not in HOST_ONLY_TOOL_NAMES]
    if (day_follow and not previous_weather) or NO_LOOKUP.search(text):
        tools = [t for t in tools if t["name"] != "get_weather"]
    if allowed_tools is not None:
        tools = [t for t in tools if t["name"] in allowed_tools]

    seen_tools = set()
    unique_tools = [t for t in tools if not (t["name"] in seen_tools or seen_tools.add(t["name"]))]
    return unique_tools[:10] if model_id == "local" else unique_tools

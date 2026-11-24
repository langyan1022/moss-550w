import asyncio
from contextlib import closing, suppress
from datetime import datetime, timedelta, timezone
from functools import cached_property
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from random import shuffle
import re
import sqlite3
import sys
from time import monotonic
import unicodedata
from urllib.parse import urlsplit

from aiohttp import web

from colloquial_zh import normalize_colloquial_erhua
from everyday import greeting_reply, reaction_reply
from local_media import frame_text
from local_media.library import (BROWSER_UNSUPPORTED_AUDIO, DEFAULT_CONFIG, DEFAULT_DB, MediaLibrary,
                                 SourceUnavailableError, access_state, artist_credit, compact, same_song,
                                 song_title_key, transport_errors)
from local_media.playback import apply as apply_playback

KINDS = {"音频": "audio", "歌曲": "audio", "音乐": "audio", "歌": "audio",
         "视频": "video", "影片": "video", "电影": "video", "图片": "image", "照片": "image", "图像": "image"}
LABELS = {"audio": "音频", "video": "视频", "image": "图片"}
SOURCE_ISSUES = {"denied": "读取权限受限", "stale": "文件或目录已变化，请更新媒体索引",
                 "new": "尚未建立索引", "partial": "只有部分目录可读", "offline": "当前不可用"}
RESUME_MAX_BYTES = 64 * 1024 * 1024
RESUME_PREPARE_SECONDS = 8
INDEX_READ_WAIT = 0.25
INDEX_LOOKUP_WAIT = 0.075


def describe_sources(problems):
    return "。".join(f"来源 {s['id']} {SOURCE_ISSUES.get(s['state'], '需要更新索引')}" for s in problems) + "。"


FILTER = "|".join(KINDS)
NUMBER = "零一二三四五六七八九十"
ORDINAL = r"(?:第\s*)?(?P<number>\d{1,3}|[零一二两三四五六七八九十]{1,3})\s*(?P<unit>首|个|条|段)?"
REFERENCE = re.compile(r"(?:(?:那就|就|请|帮我|给我|麻烦)\s*)*"
                       r"(?P<verb>切换到|换成|换到|切到|换|播放|试听|预览|选择|选|我想听|我要听|我听|想听|听|放|播)?\s*(?:一下)?"
                       r"(?P<target>上一首|下一首|上首|下首|上一个找到的|上一个|上一段|这个|这首|那首|刚才的|上次的|"
                       + ORDINAL + r")\s*(?:的)?(?P<tail>片段|歌曲|音频|歌)?")
SONG_CORRECTION = re.compile(r"(?:(?:先|暂时)\s*)?(?:不是|不要|别|不用)\s*([^,;。]+?)\s*[,;。]\s*(.+)")
SONG_DENIAL = re.compile(r"(?:(?:还是|那就|先|暂时|请)\s*)*(?:别|不要|不用|不准|不必|请勿)\s*(?:再|自动)?(.+)")
PLAY_REFERENCE_VERBS = {"切换到", "换成", "换到", "切到", "换", "播放", "试听", "我想听", "我要听", "我听", "想听", "听", "放", "播"}
MEDIA_REF = re.compile(r"歌|曲|音轨|专辑|采样率|片段|版本|歌单|选中|候选|刚才选|刚才听")
QUESTION = re.compile(r"[？?]|吗[。!！]?$|能否|可不可以|能不能|是否|如何|怎么|为什么")
NEGATION = re.compile(r"不(?:要|用|必|许|需要|想|准|再)|别|禁止|先不|暂不")
DELEGATED_PICK = r"随便|随意|任意|随机"
PLAY_PREFIX = rf"(?:(?:请|那就|那|先|再|顺便|赶紧|帮我|给我|麻烦|直接|{DELEGATED_PICK})\s*)*"
POLITE_AUDIO_REQUEST = re.compile(
    r"(?:(?:你|您)\s*)?(?:(?:能不能|可不可以|是否能|能否|可以|能|请|麻烦(?:你|您)?)\s*)?"
    rf"(?P<request>(?:帮我|给我)\s*(?:(?:{DELEGATED_PICK})\s*)?(?:播放|播|放|来|选|挑|搜索|查找|找找|搜|找)\s*[^，,；;。！？?\n]+?)"
    r"\s*(?:(?:可以|行)吗|好吗|吗)?"
)
ANY_SONG = r"(?:随便|随意|任意)(?:给我|帮我)?(?:播放|放|来|播|选|挑)?(?:一[首手]|首)(?:歌曲|歌|音乐)?"
SONG_PICK = re.compile(
    PLAY_PREFIX + r"(?:再\s*)?"
    r"(?:来|放|播(?:放)?|(?:我想|我要|想|要)?听|选|挑)\s*(?:一?[首手个]|首|个)\s*"
    r"(?P<contextual>(?:(?:适合|合适)(?:现在|此时此刻|此刻|这个时候|眼下|这会儿|当下)(?:听)?|"
    r"(?:现在|此时此刻|此刻|这个时候|眼下|这会儿|当下)(?:适合|合适)?听)的?)?"
    r"(?:歌曲|歌|音乐|曲子)(?:听(?:听|一下))?(?:吧|呗|呀|啊|好了)?"
)
MUSIC_TASTE_PICK = re.compile(
    r"(?:(?:请|帮我)\s*)?(?:按(?:照)?|照着?|根据)"
    r"我(?:(?:平时)?的(?:音乐|听歌)?(?:偏好|喜好|口味)|平时(?:爱|喜欢)听的)[，,\s]*"
    r"(?:(?:帮我|给我)\s*)?(?:放|播(?:放)?|来|选|挑)\s*(?:一首|首)(?:歌|歌曲|音乐)?(?:吧|呗|呀|啊)?"
)
SONG_BACKGROUND = re.compile(
    r"(?:(?:请|那就|帮我|给我|再|随便|随意|随机)\s*)*"
    r"(?:来|放|播(?:放)?|(?:我想|我要|想|要)?听)\s*(?:一点|点|一些)\s*"
    r"(?:歌曲|歌|音乐|曲子)(?:听(?:听|一下))?(?:吧|呗|呀|啊|好了)?"
)
SONG_SWITCH = re.compile(
    r"(?:(?:请|那就|帮我|给我|再|随机)\s*)*(?:换|再来)\s*(?:一首|首)\s*"
    r"(?P<noun>歌曲|歌|音乐)?(?:吧|呗|呀|啊|好了)?"
)
ARTIST_ANY_SONG = re.compile(
    r"(?:(?:就|那就|那|我想听|想听|我要听)\s*)?"
    r"(?P<artist>[^《》“”‘’「」『』\n，,；;。！？?]{1,64}?)的(?:是|歌曲|歌|音乐)?[，,：:\s]*"
    + ANY_SONG + r"(?:吧|呗|好了|呀|啊)?"
)
ARTIST_PICK = re.compile(
    r"(?:(?:那|就)\s*)?" + PLAY_PREFIX
    + r"(?P<play>(?:再)?(?:来|放|播(?:放)?|(?:我想|我要|想|要)?听|选|挑)\s*(?:一[首手]|首)\s*)?"
    r"(?P<artist>[^《》“”‘’「」『』\n，,；;。！？?]{1,64}?)的(?:吧|呗|呀|啊|就行|也行)?"
)
TRACK_LABEL = re.compile(r"(?:(?:请|帮我)?(?P<verb>播放|试听|我想听|我听|搜索|查找)\s*)?"
                         r"(?P<title>[^·•\n，,。！？?]{1,72}?)\s+[·•]\s+(?P<artist>[^·•\n，,。！？?]{1,48})")
PLAY_CLAIM = re.compile(
    r"(?:已经|正在|已|刚刚|刚才)(?:为(?:您|你)|帮(?:您|你)|给(?:您|你))?(?:开始|成功)?(?:播放|播出|放了)|"
    r"(?:播放|播出)(?:了《|成功|完毕)|"
    r"(?:给|帮|为)(?:您|你)(?:放|播(?:放)?)了?(?:一?首)?《|"
    r"(?:这就|马上|现在就)(?:给|帮|为)?(?:您|你)?(?:放|播(?:放)?)了?(?:一?首)?《"
)
DISCOURSE_LEADIN = re.compile(
    r"^(?:(?:算了|好吧|行吧|那好吧|没事|没关系|对了|顺便|嗯|哎|唉|我说|我是说|我想说|那就|那)\s*[，,、。！!：:\s]*)+"
)
CATALOG_QUERY = re.compile(
    r"(?:(?:请问|那(?:么)?|顺便|帮我|给我)?\s*)?"
    r"(?:看看|查一下|查查看|列出|告诉我|说说|介绍一下)?\s*"
    r"(?:(?:你的?|我的?|本机|本地|当前|这里|你这里)(?:的)?)?"
    r"(?:媒体库|多媒体库|曲库|歌库|本地音乐|曲目库|音乐库)"
    r"(?:里(?:面)?|中)?(?:目前|现在|一共|总共|都)?"
    r"(?:都有?(?:哪些|什么|谁的?)(?:歌(?:曲|手|单)?|音乐|分类|内容|曲目)?|有哪些(?:歌手|分类|歌(?:曲|单)?|音乐|曲目)|有多少(?:首(?:歌|曲)?|歌|曲目|歌手))"
    r"(?:呀|啊|呢|吗)?$"
)
LOCAL_PLAY_ANY = re.compile(
    r"(?:(?:请|那就|帮我|给我|麻烦|直接|随便|随意|随机)\s*)*"
    r"(?:播放|放|播|听|来)\s*(?:一?[首点些个]?)?\s*(?:本地|本机)(?:的)?(?:音乐|歌曲|歌|曲子)(?:吧|呗|呀|啊|好了)?"
)
CALM_MOOD_RE = re.compile(r"助眠|睡前|睡眠|催眠|安静|舒缓|放松|解压|疗愈|轻音乐")
CALM_EXCLUDE_KEYWORDS = ("鼓", "锣", "战", "摇滚", "进行曲", "DJ", "劲爆", "冲锋")
CALM_PREFER_KEYWORDS = ("班得瑞", "钢琴", "吉他", "古筝", "长笛", "催眠", "雨", "月光", "风", "梦", "夜", "温柔", "秋", "春", "海")
MOOD_CATEGORIES = (
    (re.compile(r"助眠|睡前|睡眠|催眠|安静|舒缓|放松|解压|疗愈|轻音乐|纯音乐|钢琴曲?|背景音乐|白噪音"), "2-纯音乐", "纯音乐"),
    (re.compile(r"国风|古风|中国风"), "3-国风", "国风"),
    (re.compile(r"流行(?:歌曲?|音乐)?"), "1-流行", "流行"),
    (re.compile(r"儿歌|童谣|儿童歌曲?"), "4-儿歌", "儿歌"),
)
MOOD_SONG_REQUEST = re.compile(
    PLAY_PREFIX + r"(?:再\s*)?"
    r"(?:换(?:成|到)?|切(?:换)?(?:成|到)?|来|放|播(?:放)?|(?:我想|我要|想|要)?听|选|挑)\s*"
    r"(?:一?[首点些个]\s*)?"
    r"(?P<mood>助眠|睡前|睡眠|催眠|安静|舒缓|放松|解压|疗愈|轻音乐|纯音乐|钢琴曲?|背景音乐|白噪音|国风|古风|中国风|流行|儿歌|童谣)"
    r"(?:的)?(?:轻音乐|纯音乐|背景音乐|钢琴曲?|歌曲|歌|音乐|曲子)?(?:听(?:听|一下))?(?:吧|呗|呀|啊|好了)?"
)


SUBTITLE_SEARCH = re.compile(
    r"^(?:(?:请|帮我|麻烦|给我)\s*)*(?:(?:在|从)(?:本地|我的)?(?:视频库|媒体库|视频|电影)(?:里|中)?\s*)?"
    r"(?:搜(?:索|一下)?|检索|查找|查一下|找(?:一下|一找|找)?)(?:这句|这段|一句|一段)?"
    r"(?:视频|电影)?(?:字幕|台词)(?:里|中)?(?:的)?\s*[：:]?\s*(.+)$")
CUE_REFERENCE = re.compile(
    r"(?:(?:那就|就|再|请|帮我|给我|麻烦)\s*)*"
    r"(?P<verb>切换到|换成|换到|播放|打开|看|放|播|预览|选择|选中|选)?\s*"
    r"(?:第\s*(?P<number>\d{1,3}|[零一二两三四五六七八九十]{1,3})\s*(?:段(?:台词|字幕|片段)?|条(?:台词|字幕)?)"
    r"|(?P<offset>[上下])(?:一段(?:台词|字幕|片段)?|一条(?:台词|字幕)|一个片段)"
    r"|(?:这|那|刚才那)段(?:台词|字幕|片段)?)(?:吧|呗|好了)?"
)
FRAME_SEARCH = re.compile(
    r"(?:(?:请|帮我|麻烦)\s*)*(?:(?:搜(?:索)?|检索|查(?:找|一下)?|找(?:一下)?)(?:一下)?(?:本地视频|视频)?|在)\s*"
    r"《(?P<title>[^《》\n]{1,100})》\s*(?:从(?:第)?(?P<start>\d{1,6})\s*秒(?:开始|起)?)?\s*(?:的)?画面"
    r"(?:里(?:面)?|上|中)?(?:的)?\s*(?:(?:查找|查|找|搜索|出现过)?(?:的)?(?:文字|字样)|(?:查找|找|搜索)(?=[“‘「『'\"]))\s*[：:]?\s*"
    r"(?P<query>[^\n]{0,120})")


def subtitle_time(seconds):
    minutes, ms = divmod(round(seconds * 1000), 60000)
    hours, minutes = divmod(minutes, 60)
    prefix = f"{hours:d}:" if hours else ""
    return f"{prefix}{minutes:02d}:{ms//1000:02d}.{ms%1000:03d}"


def subtitle_query(text):
    match = (SUBTITLE_SEARCH.fullmatch(text) or re.fullmatch(r"(?:字幕|台词)\s*[：:]\s*(.+)", text)
             or re.fullmatch(r"(?:哪部|哪个)(?:视频|电影)(?:里|中)?(?:出现过|说过|提到过|有)\s*[“‘「《'\"](.+)[”’」》'\"](?:这句台词)?", text)
             or re.fullmatch(r"[“‘「《'\"](.+)[”’」》'\"](?:这句)?(?:台词)?(?:出自|出现在哪|是哪|在哪)(?:哪)?(?:一)?(?:部|个)?(?:视频|电影|片段)(?:里|中|里的台词)?", text))
    return match[1].strip(" \t“”‘’「」《》'\"") if match else ""


def same_media_choice(item, selected):
    return bool(item.get("media_id")) and item["media_id"] == selected.get("media_id") and item.get("subtitle") == selected.get("subtitle")


def cue_list(items):
    return bool(items and all(item.get("kind") == "video" and item.get("subtitle") for item in items))


def track_label(text, context):
    value = unicodedata.normalize("NFKC", text).strip().rstrip("。!").strip()
    match = TRACK_LABEL.fullmatch(value)
    if not match or NEGATION.search(value) or QUESTION.search(value) or re.search(r"草稿|保存|写入", value):
        return None
    title, artist = match["title"].strip(" 《》“”"), match["artist"].strip()
    if not title or not artist:
        return None
    verb = match["verb"]
    return {"action": "track_label", "title": title, "artist": artist, "mode": "excerpt" if verb == "试听" else "track",
            "play": verb in {"播放", "试听", "我想听", "我听"} or (not verb and bool(context and context.get("items")))}


def track_key(text):
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def browser_audio_supported(item):
    return Path(item.get("path") or "").suffix.lower() not in BROWSER_UNSUPPORTED_AUDIO


DIRECT_PLAY = re.compile(r"(?:(?:请|那就|现在|直接|就|赶紧)\s*)+(?:播(?:放)?|放)(?:这(?:一)?首|它)?(?:吧|啊|呀|呗)?"
                         r"(?:[,，。!！\s]+(?:别废话了?|不用再问了?|别问了?|不用问了?))?[。!！]*")


def followup_song(text, context, turns, player=None, queued_player=None, *, identified_audio=None):
    if not turns or not DIRECT_PLAY.fullmatch(text.strip()):
        return None
    chat = turns[-1].get("chat_id")
    for turn in reversed(turns[-4:]):
        if turn.get("chat_id") != chat or turn.get("status") != "replied":
            break
        media = turn.get("media") or {}
        identified = identified_audio if turn is turns[-1] else None
        if identified and "unavailable_ordinal" in identified:
            return {"action": "select", "current": True, "play": True, "mode": "track"}
        selected = identified or media.get("suggested") or media.get("selected")
        if selected and selected.get("kind") == "audio":
            key = selected["media_id"]
            if player and player.get("media_id") == key and player.get("state") in {"playing", "buffering"}:
                state = "正在本页播放" if player["state"] == "playing" else "正在本页缓冲"
                return {"action": "playback_note", "note": f"《{selected['title']}》{state}。", "identified_audio": identified}
            if queued_player and queued_player.get("media_id") == key:
                return {"action": "playback_note", "note": f"《{selected['title']}》正在本页等待开播。", "identified_audio": identified}
            if identified:
                return {"action": "select", "current": True, "play": True, "mode": "track"}
            items = (context or {}).get("items") or []
            index = next((i for i, item in enumerate(items, 1) if item.get("media_id") == key), None)
            if index is not None:
                return {"action": "select", "index": index, "play": True, "mode": "track"}
            return {"action": "named_song", "title": selected["title"], "artist": selected.get("artist", ""),
                    "play": True, "mode": "track"}
        if DIRECT_PLAY.fullmatch(turn["user"].strip()):
            continue
        if not turn.get("source") and len(turn["user"].strip()) <= 80:
            return {"action": "retry_named", "text": turn["user"]}
        break
    return {"action": "playback_note", "note": "这段对话里暂时没有待播放的曲目。"}


def version_label(item, items):
    peers = [p for p in items if track_key(p.get("title")) == track_key(item.get("title"))
             and track_key(p.get("artist")) == track_key(item.get("artist"))]
    if len(peers) < 2 or not item.get("path"):
        return ""
    path = Path(item["path"])
    if sum(Path(p.get("path") or "").name.casefold() == path.name.casefold() for p in peers) > 1:
        return str(Path(path.parent.name) / path.name)
    return path.name


def recover_artist_request(text, turns, chat_id, latest_start=None):
    reactions = {"啥玩意", "什么玩意", "哎", "唉"}
    retry = {"怎么没放", "怎么没播", "怎么没播放", "那就放啊", "那就播啊", "再试一次"}
    def wording(s):
        return s.strip().rstrip("。！？!?…").strip()
    followup = wording(text)
    if followup not in reactions | retry:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    for turn in reversed(turns[-3:]):
        if turn.get("chat_id") != chat_id or turn.get("status") != "replied":
            return None
        try:
            if datetime.fromisoformat(turn["created_at"]) < cutoff:
                return None
        except (KeyError, ValueError, TypeError):
            return None
        original = wording(turn.get("user", ""))
        media = turn.get("media") or {}
        started = (turn.get("browser_playback") or {}).get("event") == "started"
        if turn.get("retried_artist_turn") and followup in retry:
            note = "上次重试已收到开始播放的回执。" if started else "上次重试还没有收到开始播放的回执。"
            return {"action":"playback_note", "note":note}
        if media.get("autoplay") or started or media.get("playback") or turn.get("receipt"):
            return None
        actions = set(turn.get("media_actions") or ())
        tools_used = {entry.get("tool") for entry in turn.get("native_receipts") or ()}
        if actions - {"search"} or tools_used - {"search_media"}:
            return None
        if original in reactions:
            continue
        if not (actions or tools_used) or not media.get("items"):
            return None
        intent = classify(original, None)
        if not intent or intent["action"] != "listen" or not intent.get("artist"):
            return None
        if latest_start and latest_start.get("at"):
            try:
                if datetime.fromisoformat(latest_start["at"]) >= datetime.fromisoformat(turn["created_at"]):
                    return None
            except (TypeError, ValueError):
                return None
        if followup in reactions:
            return {"action":"playback_note", "note":"刚才只收到了歌曲检索结果，播放器没有确认切歌。"}
        return {**intent, "retried_artist_turn":turn["id"]}
    return None


def verified_media_answer(answer, media=None, receipts=()):
    if not PLAY_CLAIM.search(answer) and not re.search(r"(?:源文件|音轨)时长已(?:经)?(?:估算|推算)", answer):
        return answer
    view = media or {}
    played = next((record["result"] for record in receipts if record["tool"] == "play_media"
                   and record["result"].get("status") == "played"), None)
    if played is None and view.get("autoplay") and (view.get("playback") or {}).get("status") == "played":
        played = view["playback"]
    if played:
        return f"测试播放器已播完《{played['title']}》的 {played['clip_s']:g} 秒片段。"
    if view.get("autoplay") and view.get("play_mode") == "track" and view.get("selected"):
        return f"已向浏览器请求播放《{view['selected']['title']}》。"
    return "本轮没有收到播放器的播放回执。"


def ordinal_number(value):
    if value.isdigit():
        return int(value)
    value = value.replace("两", "二")
    if "十" in value:
        tens, _, ones = value.partition("十")
        if len(tens) > 1 or len(ones) > 1 or "十" in ones:
            return 0
        return (NUMBER.index(tens) if tens else 1) * 10 + (NUMBER.index(ones) if ones else 0)
    return NUMBER.index(value) if len(value) == 1 else 0


def reference_position(match):
    if match["number"]:
        return {"index": ordinal_number(match["number"])}
    if match["target"] in {"上一首", "上首", "下一首", "下首"}:
        return {"offset": -1 if match["target"].startswith("上") else 1}
    return {"current": True}


def audio_reference(match):
    return "首" in match["target"] or match["tail"] in {"歌曲", "音频", "歌"} or match["verb"] == "试听"


def selection_intent(match):
    verb, target, unit, tail = (match[key] for key in ("verb", "target", "unit", "tail"))
    mode = "excerpt" if tail == "片段" or unit == "段" or target == "上一段" or verb == "试听" else "track"
    intent = {"action": "select", "play": verb not in {"预览", "选择", "选"}, "mode": mode, **reference_position(match)}
    if audio_reference(match):
        intent["kind"] = "audio"
    return intent


def corrected_song_reference(value, context, focused):
    clauses = SONG_CORRECTION.fullmatch(value)
    if not clauses:
        return None
    first, last = clauses.groups()
    rejected = REFERENCE.fullmatch(first.strip())
    if not rejected or not audio_reference(rejected):
        return None
    final_clause = re.split(r"[,;。]", last)[-1].strip()
    denial = SONG_DENIAL.fullmatch(final_clause)
    if denial:
        denied_text = denial[1].rstrip("?").removesuffix("吗").removesuffix("了").strip()
        target = REFERENCE.fullmatch(denied_text)
        if denied_text in PLAY_REFERENCE_VERBS or (target and audio_reference(target)):
            return {"action": "no_play"}
    if re.match(r"(?:只是问|只问|只是想问)\s*(?:这首|上一首|下一首|第[零一二两三四五六七八九十0-9]+首)", final_clause):
        return {"action": "no_play"}
    requested = REFERENCE.fullmatch(last.rstrip("?").removesuffix("吗").strip())
    if not requested or not audio_reference(requested) or requested["verb"] not in PLAY_REFERENCE_VERBS:
        return None
    if QUESTION.search(last):
        return {"action": "no_play"}
    if not focused:
        return {"action": "playback_note", "note": "当前对话没有可切换的歌曲列表。"}
    rejected_position, requested_position = map(reference_position, (rejected, requested))

    def index(position):
        if "index" in position:
            return position["index"]
        selected = context.get("selected") or {}
        if position.get("current"):
            selected = context.get("suggested") or selected
        return next((n + position.get("offset", 0) for n, item in enumerate(context.get("items") or (), 1)
                     if same_media_choice(item, selected)), None)

    rejected_index = index(rejected_position)
    if rejected_position == requested_position or (rejected_index is not None and rejected_index == index(requested_position)):
        return {"action": "no_play"}
    return selection_intent(requested)


def media_reference(text, context, focused):
    value = unicodedata.normalize("NFKC", text).strip().rstrip("。!").strip()
    items = (context or {}).get("items") or []
    cue = CUE_REFERENCE.fullmatch(value)
    if cue and cue_list(items):
        if not focused or QUESTION.search(text):
            return None
        intent = {"action":"select", "play":cue["verb"] not in {"预览", "选择", "选中", "选"},
                  "mode":"video", "cue_navigation":True}
        if cue["number"]:
            return {**intent, "index":ordinal_number(cue["number"])}
        if cue["offset"]:
            return {**intent, "offset":-1 if cue["offset"] == "上" else 1}
        return {**intent, "current":True}
    music = bool(context and ((context.get("selected") or {}).get("kind") == "audio"
                 or any(i.get("kind") == "audio" for i in context.get("items", ()))))
    focused = bool(focused and music)
    about_name = r"叫(?:什么|啥)(?:名字|歌曲?|曲子)?|名称(?:是)?什么|是(?:什么歌|哪首歌?)|歌名(?:是)?(?:什么|啥)"
    about_artist = r"(?:是)?谁唱的|歌手(?:是)?谁|(?:是)?(?:哪位|哪个)歌手唱的"
    field = f"(?:{about_name}|{about_artist})"
    detail = re.fullmatch(r"(?:那(?:么)?\s*)?(?:只(?:告诉我|问一下))?\s*" + ORDINAL
                          + r"(?:歌曲?)?(?:的)?(?:(?:歌名和歌手|曲名和歌手)(?:分别)?(?:是)?什么|"
                          + field + r"(?:\s*(?:[,;、?]|以及|和|还有)\s*" + field + r")?)"
                          r"(?:[，,]?\s*(?:别|不要|不用)播放)?[?]?", value)
    if detail and (detail["unit"] == "首" or focused):
        intent = {"action": "describe", "index": ordinal_number(detail["number"])}
        if detail["unit"] == "首":
            intent["kind"] = "audio"
        return intent
    if ((focused or re.search(r"歌|曲", value)) and
            re.fullmatch(r"(?:请问|请告诉我)?(?:这首|当前这首)(?:歌曲|歌|曲子)?(?:的)?" + field + r"[?]?", value)):
        return {"action": "identify", "playback_question": "current", "allow_selection": True}
    if (music and re.fullmatch(r"(?:我|我们)?(?:刚才|之前|上次|当前|现在)?(?:选中|选了|选的|选择了|选的是|选中的是)"
                               r"(?:的|是)?(?:哪一?首|什么歌|哪个)(?:歌|歌曲|音轨)?(?:来着)?[?]?", value)):
        return {"action": "identify"}
    track_question = r"(?:的)?(?:是)?(?:哪一?首(?:歌曲|歌|曲子)?|什么(?:歌曲|歌|音乐|曲子)?)(?:来着)?[?]?"
    if ((music or re.search(r"歌|曲|首|音乐", value)) and
            re.fullmatch(r"(?:我们|我)?(?:(?:刚才|刚刚|之前|上次|最近|最后一次)(?:实际)?"
                         r"(?:听|播(?:放)?|放)(?:过|到|了)?|听了|播放了)" + track_question, value)):
        return {"action": "identify", "playback_question": "history"}
    if (re.search(r"现在|当前|此刻|正在|在播|在放|在听|放的|播的|听的", value) and
            re.fullmatch(r"(?:我们|我)?(?:现在|当前|此刻)?(?:正在|在)?(?:播(?:放)?|放|听)" + track_question, value)):
        return {"action": "identify", "playback_question": "current"}
    value = re.sub(r"(?:吧|呗|好了)$", "", value).strip()
    if correction := corrected_song_reference(value, context, focused):
        return correction
    if NEGATION.search(value):
        if focused and re.fullmatch(r"(?:请)?(?:先|暂时)?(?:不要|不用|别|不必)(?:再)?(?:播(?:放)?|放|切换)(?:第?[零一二两三四五六七八九十0-9]+[首条个]?|上一首|下一首|这首)?[。!]?", value):
            return {"action": "no_play"}
        return None
    if QUESTION.search(value):
        return None
    if focused and value in {"我听", "就听这首", "我听这个", "就它", "就它了"}:
        value = "播放这首"
    replay = re.fullmatch(
        r"(?:(?:请|帮我|给我|麻烦|那就|就)\s*)*"
        r"(?:(?:把|将)\s*(?P<ba_ref>刚才(?:那首|这首|的)?|这首|那首|当前(?:这首)?)(?P<ba_noun>歌曲|歌|曲子|音频)?\s*"
        r"(?:再(?:播(?:放)?|放|听|来)(?:一[遍次]|一下)|重(?:播|放)(?:一下)?)"
        r"|(?:重(?:播|放)|再(?:播(?:放)?|放|听|来)(?:一[遍次]|一下))\s*(?:一下)?\s*"
        r"(?P<after_ref>刚才(?:那首|这首|的)?|这首|那首|当前(?:这首)?)?(?P<after_noun>歌曲|歌|曲子|音频)?)",
        value,
    )
    if replay and (focused or replay["ba_noun"] or replay["after_noun"]
                   or "首" in (replay["ba_ref"] or "") or "首" in (replay["after_ref"] or "")):
        return {"action": "select", "current": True, "play": True, "mode": "track", "kind": "audio"}
    match = REFERENCE.fullmatch(value)
    if not match:
        return None
    explicit = bool(audio_reference(match) or match["tail"] or match["verb"] == "播放" or match["target"] == "上一段")
    if not explicit and not focused:
        return None
    return selection_intent(match)


def music_guess(text, previous, context):
    value = unicodedata.normalize("NFKC", text).strip().rstrip("。!?").strip()
    if not re.fullmatch(r"(?:你(?:来)?猜(?:猜|一下|看)?(?:呢)?|猜猜(?:看)?)", value) or not previous or not context:
        return None
    items = context.get("items") or []
    audio_indices = [i for i, item in enumerate(items, 1) if item.get("kind") == "audio"
                     and item.get("source_kind") in {"local", "nas"} and item.get("media_id")]
    if not audio_indices:
        return None
    last = previous[-1]
    reply = last.get("reply") or ""
    asked = re.search(r"(?:请|能否|可以)?告诉我.{0,12}(?:歌名|歌手)|"
                      r"(?:想听|要听|播放|选)(?:这首|哪一?首|什么歌|第.{1,3}首).{0,8}[？?吗]|"
                      r"是否.{0,5}播放", reply)
    explicit = media_reference(last["user"], context, True)
    if not asked and not (last.get("media") and (explicit or re.search(r"歌|歌曲|音乐|音轨", last["user"]))):
        return None
    if explicit and explicit["action"] == "select":
        index = explicit.get("index")
        if index is None:
            key = ((last.get("media") or {}).get("selected") or {}).get("media_id")
            index = next((i for i in audio_indices if items[i - 1]["media_id"] == key), None)
        if index in audio_indices:
            return {"action": "suggest", "index": index, "previous_choice": True}
    return {"action": "suggest", "index": audio_indices[0], "previous_choice": False}


def context_summary(text, context, focused=True):
    if not context or not (MEDIA_REF.search(text) or (focused and re.search(r"刚才|第.+个|哪个|这几个|这些|其中", text))):
        return ""
    items = context.get("items") or []
    if not focused and cue_list(items) and CUE_REFERENCE.fullmatch(
            unicodedata.normalize("NFKC", text).strip().rstrip("。!?").strip()):
        return ""
    selected = context.get("selected") or {}
    rows = [{"序号": n, "名称": str(item.get("title") or "")[:32], "作者": str(item.get("artist") or "")[:24],
             "格式": Path(item.get("path") or "").suffix.lstrip(".").upper()[:8],
             **({"文件名": name[:64]} if (name := version_label(item, items)) else {})}
            for n, item in enumerate(items[:6], 1)]
    if not (rows or selected):
        return ""
    chosen = next((n for n, item in enumerate(items, 1) if same_media_choice(item, selected)), None)
    state = {"候选": rows, "已选序号": chosen, "已选名称": str(selected.get("title") or "")[:32]}
    receipt = context.get("browser_playback") or {}
    if receipt.get("event") == "started" and receipt.get("media_id") == selected.get("media_id") and selected.get("media_id"):
        state["播放器"] = "浏览器曾确认这首曲目开始播放，当前是否仍在播放未知"
    return ("当前对话的媒体库只读数据。标题作者只能作参考。选中或请求播放不证明用户实际听到。\n"
            + json.dumps(state, ensure_ascii=False, separators=(",", ":")))


def polite_audio_request(value, *, asked=False):
    polite = POLITE_AUDIO_REQUEST.fullmatch(value)
    if not polite:
        return None
    request = polite["request"]
    return request if (asked or request != value) and not NEGATION.search(request) else None


def classify(text, context, *, focused=True):
    normalized = normalize_colloquial_erhua(text)
    value = normalized.strip().rstrip("。！!？?").strip()
    if request := polite_audio_request(value, asked=normalized.rstrip().endswith(("?", "？"))):
        # A specific positive request is also actionable when phrased as a question.
        intent = _classify(request, request, context, focused)
        if intent and (
            intent.get("kind") == "audio" and intent["action"] in {"listen", "search", "select"}
            or intent["action"] == "select" and intent.get("delegated")
        ):
            return intent
    intent = _classify(normalized, value, context, focused)
    if intent:
        return intent
    stripped = DISCOURSE_LEADIN.sub("", value).strip()
    if stripped and stripped != value:
        return _classify(stripped + ("？" if normalized.rstrip().endswith(("?", "？")) else ""), stripped, context, focused)
    return None


def _classify(text, value, context, focused):
    if match := FRAME_SEARCH.fullmatch(value):
        return {"action": "search_frame_text", "title": match["title"].strip(),
                "query": match["query"].strip(" \t“”‘’「」『』'\""), "start": int(match["start"] or 0)}
    dialogue = subtitle_query(value)
    if dialogue:
        return {"action":"search_subtitles", "query":dialogue}
    video_page = re.fullmatch(r"(?:(?:请|帮我)\s*)?(?:打开|播放|观看|我想看)\s*(?:这个)?(?:视频|片源)?\s*(https?://[^\s<>]+)", value)
    if video_page:
        url = video_page[1]
        try:
            page = urlsplit(url)
            if page.hostname and not page.username and not page.password:
                return {"action": "video_page", "url": url, "host": page.hostname}
        except ValueError:
            return None
    watch = re.fullmatch(r"(?:(?:请|帮我)\s*)?(?:播放|打开|我想看|我要看|想看)\s*(?:本地)?(?:视频|电影|影片)\s*[：:]?\s*(.*?)", value)
    named_video = re.fullmatch(r"(?:我想看|我要看|想看)\s*《([^《》]+)》", value)
    if (watch or named_video) and not (QUESTION.search(text) or NEGATION.search(text)):
        return {"action":"watch", "kind":"video", "query":(watch or named_video)[1].strip(" 《》"), "online":False}
    videos = focused and context and any(item.get("kind") == "video" for item in context.get("items", []))
    if videos and not QUESTION.search(text):
        pick = re.fullmatch(r"(?:请)?(?:播放|打开|看)?\s*第\s*(\d{1,3}|[零一二两三四五六七八九十]{1,3})\s*(?:个|条|部)(?:视频|影片|电影)?", value)
        if pick:
            return {"action":"select", "index":ordinal_number(pick[1]), "play":True, "mode":"track"}
    pick = SONG_PICK.fullmatch(value)
    any_song = (re.fullmatch(r"(?:(?:请|那就|帮我|给我|播放|放|来)\s*)*" + ANY_SONG + r"(?:吧|呗|好了)?", value)
                or pick or SONG_BACKGROUND.fullmatch(value))
    switch_song = SONG_SWITCH.fullmatch(value)
    random_song = "随机" in value
    if not QUESTION.search(text) and not NEGATION.search(value):
        if mood_match := MOOD_SONG_REQUEST.fullmatch(value):
            mood_text = mood_match["mood"]
            for pattern, folder, label in MOOD_CATEGORIES:
                if pattern.search(mood_text):
                    calm = bool(CALM_MOOD_RE.search(mood_text))
                    return {"action": "listen", "kind": "audio", "query": "", "folder_hint": folder,
                            "category_label": label, "online": False, "mode": "track", "random_song": True,
                            "exclude_keywords": CALM_EXCLUDE_KEYWORDS if calm else (),
                            "prefer_keywords": CALM_PREFER_KEYWORDS if calm else (),
                            "avoid_recent_song": bool(context and context.get("browser_playback"))}
    if not random_song and not QUESTION.search(text) and ((pick and pick["contextual"]) or MUSIC_TASTE_PICK.fullmatch(value)):
        return {"action": "listen", "kind": "audio", "query": "", "online": False, "mode": "track",
                "contextual_pick": True}
    if random_song and any_song and not QUESTION.search(text) and context and context.get("_music_preference_versions"):
        return {"action": "listen", "kind": "audio", "query": "", "online": False, "mode": "track",
                "avoid_recent_song": bool(context.get("browser_playback")), "random_song": True}
    if not QUESTION.search(text) and not NEGATION.search(value) and LOCAL_PLAY_ANY.fullmatch(value):
        return {"action": "listen", "kind": "audio", "query": "", "online": False, "local_only": True, "mode": "track",
                "avoid_recent_song": bool(context and context.get("browser_playback")), "random_song": random_song}
    if focused and context and not QUESTION.search(text):
        choose = re.fullmatch(r"(?:那就|请|再)?(?:(?:你(?:来|帮我|给我)?|帮我)(?:选|挑|决定)(?:一首|一个)?(?:歌)?|(?:随便|随意)(?:放|来|播|选|挑)(?:一首|首|一个)(?:歌)?|(?:换|再来)(?:一首|首|一个|个)(?:歌)?)(?:吧|呗)?", value)
        audio = [item for item in context.get("items", []) if item.get("kind") == "audio"
                 and item.get("source_kind") in {"local", "nas", "online"}]
        if (choose or any_song or switch_song) and audio:
            query = compact(context.get("query") or "")
            artist_scope = query and all(compact(item.get("artist") or "") == query for item in audio)
            fallback_local = bool(any_song or switch_song) and not artist_scope
            change = "换" in value or bool((any_song or "再来" in value) and context.get("browser_playback"))
            return {"action": "select", "delegated": True, "change_track": change, "require_different": "换" in value,
                    "fallback_local": fallback_local, "avoid_recent_song": fallback_local and change,
                    "random_song": random_song, "play": True, "mode": "track"}
    if (any_song or (switch_song and switch_song["noun"])) and not QUESTION.search(text):
        return {"action": "listen", "kind": "audio", "query": "", "online": False, "mode": "track",
                "avoid_recent_song": bool(context and context.get("browser_playback")), "random_song": random_song}
    label = track_label(text, context if focused else None)
    if label:
        return label
    reference = media_reference(text, context, focused)
    if reference:
        return reference
    if not QUESTION.search(text) and re.fullmatch(r"(?:请)?(?:先|暂时)?(?:别|不要|不用|不必)(?:再|自动)?"
                                                  r"(?:播放|放|播)[^，,；;。！？?\n]{1,64}的(?:歌曲|音乐|歌)(?:了)?", value):
        return {"action": "no_play"}
    if re.fullmatch(r"(?:请)?(?:打开|查看|列出|看看)?(?:我的|本地)?(?:媒体库|多媒体库|媒体索引)(?:状态|有哪些来源)?|索引状态|列出库", value):
        return {"action": "status"}
    if CATALOG_QUERY.fullmatch(value) and not NEGATION.search(value):
        return {"action": "catalog"}
    artist_request = re.fullmatch(
        PLAY_PREFIX + r"(?:(?P<play>我想听|我要听|想听|要听|播放|播|放|(?:再)?来(?:一[首手]|几首|首)|(?:再)?换(?:成|到|一首|一个|首|个)?)|只看看|只看|看看|只列出|列出|搜索|查找)"
        r"\s*(?:(?:一下|一[首手]|几首|首)\s*)?(?:本地音乐\s+)?(?P<artist>[^，,；;。！？?\n]{1,64}?)的(?:歌曲|音乐|歌)"
        r"(?:吧|呗|啊|呀)?(?P<hold>[，,\s]*(?:但)?(?:先)?(?:别|不要|不用)(?:自动)?(?:播放|播|放))?", value)
    if not artist_request:
        artist_request = re.fullmatch(
            r"(?:(?:那就|就|请|帮我|麻烦|给我)\s*)*"
            r"(?P<artist>[^《》“”‘’「」『』'\"\n，,；;。！？?]{1,64}?)的(?:歌曲|音乐|歌)[，,\s]*"
            r"(?P<play>(?:(?:请|先|直接|给我|帮我|随便|随意|任意)\s*)*(?:再\s*)?"
            r"(?:来|放|播(?:放)?|听|选|挑|换)\s*(?:一[首手]|首))"
            r"(?:吧|呗|啊|呀|好了)?(?P<hold>[，,\s]*(?:但)?(?:先)?(?:别|不要|不用)(?:自动)?(?:播放|播|放))?",
            value)
        if artist_request and NEGATION.search(artist_request["artist"]):
            artist_request = None
    correction = (re.fullmatch(r"(?P<artist>[^，,；;。！？?\n]{1,64}?)是(?:一名|一位|个)?歌手[，,、\s]+"
                              + ANY_SONG + r"(?:吧|呗|好了)?", value) or ARTIST_ANY_SONG.fullmatch(value))
    if correction and not (QUESTION.search(text) or NEGATION.search(text)):
        artist = correction["artist"].strip()
        return {"action": "listen", "kind": "audio", "query": artist, "artist": artist, "online": False, "mode": "track",
                "change_track": bool((context or {}).get("browser_playback"))}
    if not artist_request and (shorthand := ARTIST_PICK.fullmatch(value)) and shorthand["play"]:
        artist_request = shorthand
    if artist_request and not QUESTION.search(text):
        artist = artist_request["artist"].strip()
        if artist in {"你", "您", "我"} or re.search(r"推荐|适合|评价|意见|看法|喜欢|好听|你唱|您唱", artist):
            return None
        listen = bool(artist_request["play"]) and not artist_request.groupdict().get("hold")
        return {"action": "listen" if listen else "search", "kind":"audio", "query":artist,
                "artist":artist, "online":False, "mode":"track",
                "require_different":listen and "换" in artist_request["play"],
                "change_track":listen and ("换" in artist_request["play"] or
                                           ("再来" in artist_request["play"] and bool((context or {}).get("browser_playback"))))}
    if re.fullmatch(r"(?:更新|刷新|重新扫描|扫描)(?:一下)?(?:本地)?(?:媒体索引|媒体库|多媒体库)", value):
        return {"action": "scan"}
    root = re.fullmatch(r"(?:在|看看)?([A-Za-z]:[\\/].+?|\\\\[^，,]+?)(?:\s*(?:里边有|里面有|里有|里面|里边))", value)
    if root:
        return {"action": "set_library", "root": root[1].strip()}
    if context and not (QUESTION.search(text) or NEGATION.search(text)) and re.search(
            r"(?:检索|查|找)(?:得到|到了|到|着).*(?:直接|就)(?:播|放)", value):
        return {"action": "play_match", "query": context.get("query", "")}
    listen = re.fullmatch(r"(?:我)?(?:想听|要听|想试听)(?:听)?\s*(.+?)(?:的(?:歌曲|音乐|歌))?", value)
    if listen and re.search(r"歌|音乐|音频|曲子|专辑|《|片段", value) and not (QUESTION.search(text) or NEGATION.search(text)):
        listen_q = re.sub(r"(?:的)?片段$", "", listen[1]).strip()
        if re.fullmatch(r"(?:歌(?:曲|单)?|音乐|曲子|音频)", listen_q):
            listen_q = ""
        return {"action": "listen", "query": listen_q,
                "kind": "audio", "online": False, "mode": "excerpt" if "片段" in value or "试听" in value else "track"}
    segment = re.sub(r"^(?:(?:请|麻烦|帮我|替我|只查本地|仅查本地|只在本地|只搜本地|不要联网|不联网|本地|允许联网|联网)[\s，,、]*)+", "", value)
    search = re.match(r"^(?:搜索|检索|查找|找找|找一下|找|搜|查)\s*(.*)$", segment)
    local_only = any(token in value for token in ("别联网", "不要联网", "不联网", "只查本地", "只搜本地", "只在本地", "仅查本地"))
    if search or local_only:
        segment = search[1].strip() if search else segment
        kind = next((kind for name, kind in KINDS.items() if name in segment), None)
        if kind or "媒体" in segment or "多媒体" in segment:
            segment = re.sub(r"^(?:(?:一下|本地|库里|关于|我的|有没有|与)\s*)+", "", segment)
            segment = re.sub(r"^(?:多媒体|媒体|" + FILTER + r")\s*[：:、，,]?\s*", "", segment)
            segment = re.sub(r"(?:相关的|相关|的)?(?:多媒体|媒体|" + FILTER + r")$", "", segment)
            segment = segment.strip(" ：:、，,《》「」“”")
            online = bool(re.match(r"^(?:(?:请|麻烦|帮我|替我)\s*)*(?:允许联网|联网)(?:搜索|检索|查找|搜|找)", value)) and not local_only
            return {"action": "search", "query": segment, "kind": kind, "online": online}
    return None


async def json_command(cmd):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-X", "utf8", "-m", *cmd, cwd=Path(__file__).resolve().parent,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()
        raise
    if proc.returncode and not out:
        raise ValueError(err.decode("utf-8", errors="replace")[-240:])
    return json.loads(out.decode("utf-8"))


class MediaChat:
    def __init__(self, data):
        self.db = Path(os.environ.get("MOSS_MEDIA_DB") or DEFAULT_DB).resolve()
        self.player_db = Path(data).resolve() / "media-playback.sqlite3"

    @cached_property
    def library(self):
        library = MediaLibrary(self.db, timeout=INDEX_READ_WAIT)
        if self.db == DEFAULT_DB and not library.sources():
            cfg_path = DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else DEFAULT_CONFIG.with_name("sources.example.json")
            if cfg_path.exists():
                library.configure(json.loads(cfg_path.read_text(encoding="utf-8")))
        return library

    @cached_property
    def lookup_library(self):
        return MediaLibrary(self.db, read_only=True, timeout=INDEX_LOOKUP_WAIT)

    def _local_search(self, query, kind, limit, artist=None, browser_audio=False, *, exclude_song=None, random_order=False,
                      preferred_artists=(), recent_audio=(), folder_hint=None, exclude_keywords=(), prefer_keywords=()):
        return self.library.route(query, kind=kind, limit=limit, artist=artist, browser_audio=browser_audio,
                                  exclude_song=exclude_song, random_order=random_order,
                                  preferred_artists=preferred_artists, recent_audio=recent_audio,
                                  folder_hint=folder_hint, exclude_keywords=exclude_keywords,
                                  prefer_keywords=prefer_keywords)

    def format_catalog(self):
        summary = self.library.catalog_summary()
        total = summary["total_audio"]
        playable = summary["playable_audio"]
        unsupported = summary["unsupported_audio"]
        if not total:
            return "本地媒体库目前还没有索引到音频文件，可以在设置中扫描或添加音乐目录。", summary
        source_names = "、".join(s["label"] for s in summary["sources"]) or "本机音乐"
        parts = [f"本机媒体库（{source_names}）共索引 {total} 首音频，其中 {playable} 首可在本页直接播放"]
        if unsupported:
            parts[0] += f"（另有 {unsupported} 首为 DSF/DFF 无损格式，浏览器暂不支持直放）"
        parts[0] += "。"
        if summary["categories"]:
            cats = "、".join(f"{c['name']}（{c['playable']} 首）" for c in summary["categories"] if c["playable"] > 0)
            if cats:
                parts.append(f"分类歌单：{cats}。")
        playable_artists = [a for a in summary["top_artists"] if a["playable"] > 0]
        if playable_artists:
            shown = "、".join(a["name"] for a in playable_artists[:18])
            parts.append(f"主要歌手（共 {summary['artist_count']} 位）包括：{shown}等。")
        parts.append("直接说歌手名、歌名或“来一首纯音乐”即可播放。")
        return "".join(parts), summary

    def _indexed_songs(self, title, artist=""):
        needle = compact(title)
        if not needle:
            return []
        matches = []
        with closing(self.lookup_library.connect()) as db:
            rows = db.execute("""SELECT m.*, s.config FROM media m JOIN sources s ON s.id=m.source
                                 WHERE m.kind='audio' AND instr(m.searchable,?)>0""", (needle,))
            for row in rows:
                source = json.loads(row["config"])
                edition = compact(row["title"]) != needle
                if edition and song_title_key(row["title"]) != needle:
                    continue
                credit = artist_credit(row, source, artist) if artist else None
                if artist and not credit:
                    continue
                parts = Path(row["rel"]).parts
                name = row["artist"] or (artist if credit == "folder" else "")
                if not name and len(parts) == 2:
                    name, credit = parts[0], "folder"
                rank = (source["kind"] != "local", edition, credit == "folder", row["path"].casefold())
                matches.append((rank, {"media_id": row["media_id"], "title": row["title"], "artist": name,
                                       "artist_source": credit or ("tag" if row["artist"] else ""),
                                       "source": row["source"], "source_kind": source["kind"]}))
        return [item for _, item in sorted(matches, key=lambda pair: pair[0])]

    def _indexed_artist(self, name):
        with closing(self.lookup_library.connect()) as db:
            rows = db.execute("""SELECT m.rel, m.artist, s.config FROM media m JOIN sources s ON s.id=m.source
                                 WHERE m.kind='audio' AND instr(m.searchable,?)>0""", (compact(name),))
            return any(artist_credit(row, json.loads(row["config"]), name) for row in rows)

    async def named_request(self, text, *, focused=False, artist_collection=False):
        value = unicodedata.normalize("NFKC", normalize_colloquial_erhua(text)).strip().rstrip("。!")
        value = polite_audio_request(value.rstrip("?").rstrip(), asked=value.endswith("?")) or value
        value = DISCOURSE_LEADIN.sub("", value).strip() or value
        if not 1 <= len(value) <= 100 or re.match(r"(?:别|不要|不用|不能|不必|不想|请勿|为什么|如何|怎么)", value):
            return None
        if reaction_reply(text) or (not focused and greeting_reply(text)):
            return None
        listing = re.match(r"(?:(?:请|帮我|麻烦|给我)\s*)*(?:(?:只|仅)(?:列出?|看(?:看)?)|列出)\s*", value)
        if listing:
            value = value[listing.end():]
            value = re.sub(r"\s*[,;]?\s*(?:先|暂时)?(?:别|不要|不用|不必|请勿)(?:自动)?(?:播放|播|放)$", "", value)
            value = re.sub(r"(?:的)?(?:全部|所有|不同|各个)?版本$", "", value.strip()).strip()
        if not value or re.search(r"[?,，;；\n]", value):
            return None
        if not listing and not NEGATION.search(value) and (pick := ARTIST_PICK.fullmatch(value)) and (focused or pick["play"]):
            artist = pick["artist"].strip()
            if artist and await asyncio.to_thread(self._indexed_artist, artist):
                return {"action": "listen", "kind": "audio", "query": artist, "artist": artist, "online": False, "mode": "track"}
        match = re.fullmatch(r"(?:(?:请|麻烦|帮我|给我)\s*)?(?P<verb>我想听|我要听|想听|播放(?:\s*一?首)?|"
                             r"放(?:\s*(?:一?首|一下))?|播(?:\s*(?:一?首|一下))?|来\s*一首|搜索|查找)?"
                             r"\s*(?P<name>.+)", value)
        if not match:
            return None
        phrase = match["name"].strip()
        quoted = phrase.startswith("《") and phrase.endswith("》")
        phrase = phrase.strip(" 《》")
        if not phrase or phrase in {"我在", "谢谢", "再见", "好的", "算了", "嗯", "好"} and not (quoted or match["verb"]):
            return None
        verb = re.sub(r"\s+", "", match["verb"] or "")
        bare_play_verb = verb in {"放", "播"}
        def lookup_phrase(candidate):
            hits = self._indexed_songs(candidate)
            if hits:
                return candidate, ""
            for split in (m.start() for m in re.finditer("的", candidate)):
                artist, title = candidate[:split].strip(), candidate[split + 1:].strip(" 《》")
                if not (artist and title):
                    continue
                if artist_collection:
                    matches = bool(self._indexed_songs(title, artist))
                else:
                    matches = bool(self._indexed_songs(title)) and self._indexed_artist(artist)
                if matches:
                    return title, artist
            return None
        def lookup():
            found = lookup_phrase(phrase)
            if found:
                return found
            trimmed = re.sub(r"(?:吧|呗|呀|啊|好了)$", "", phrase).strip(" 《》")
            if trimmed and trimmed != phrase and (verb or quoted):
                return lookup_phrase(trimmed)
            return None
        target = await asyncio.to_thread(lookup)
        if not target and bare_play_verb and not quoted:
            # Bare 放/播 without 一首/一下/《 only triggers on verified indexed songs to avoid false matches like 放心/放松.
            return None
        trimmed_phrase = re.sub(r"(?:吧|呗|呀|啊|好了)$", "", phrase).strip(" 《》") or phrase
        if not target and verb and verb not in {"搜索", "查找"} and (
                quoted or (re.fullmatch(r"(?:播放|放|播|来)一?首", verb)
                           and not re.search(
                               r"随便|随机|合适|适合|好听|^(?:首|个|那个|这|第)|的(?:歌|音乐|歌曲)$|"
                               r"^(?:纯音乐|轻音乐|助眠|睡前|舒缓|放松|背景音乐|国风|古风|流行|儿歌)(?:的?(?:歌|音乐|歌曲))?$",
                               trimmed_phrase))):
            target = trimmed_phrase, ""
        if not target:
            return None
        title, artist = target
        play = not listing and verb not in {"搜索", "查找"} and bool(verb or artist or focused or quoted)
        return {"action": "named_song", "title": title, "artist": artist, "mode": "track", "play": play}

    def _available_songs(self, title, artist, *, browser_audio=False, exact_title=False, source_kind=None):
        rows = self._indexed_songs(title, artist)
        if exact_title:
            rows = [row for row in rows if track_key(row["title"]) == track_key(title)]
        if source_kind:
            rows = [row for row in rows if row["source_kind"] == source_kind]
        items, sources = [], []
        problems = {}
        unsupported = set()
        for row in rows:
            if items and items[0]["source_kind"] != row["source_kind"]:
                break
            if row["source"] not in sources:
                sources.append(row["source"])
            if problems.get(row["source"], {}).get("state") == "offline":
                continue
            try:
                item, opened = self.library.open_record(row["media_id"])
                with opened as stream:
                    if not stream.read(1):
                        continue
            except OSError as exc:
                problems[row["source"]] = {"id": row["source"], "state": access_state(exc)}
                continue
            if browser_audio and not browser_audio_supported(item):
                unsupported.add(Path(item["path"]).suffix.lower())
                continue
            item.update(artist=row["artist"], artist_source=row["artist_source"])
            items.append(item)
            if len(items) == 6:
                break
        return {"items":items, "consulted_sources":sources, "indexed":bool(rows),
                "unavailable_sources":list(problems.values()), "unsupported_audio":sorted(unsupported)}

    def recover_track_copy(self, selected):
        try:
            with transport_errors():
                _, handle = self.library.open_record(selected["media_id"], kinds={"audio"})
                with handle as stream:
                    if stream.read(1):
                        return None
            reason = "stale"
        except OSError as exc:
            reason = access_state(exc)
        alternate = "nas" if selected["source_kind"] == "local" else "local"
        found = self._available_songs(selected["title"], selected["artist"], browser_audio=True,
                                      exact_title=True, source_kind=alternate)
        item = next((row for row in found["items"] if row.get("artist_source") != "folder" and
                     track_key(row["artist"]) == track_key(selected["artist"])), None)
        if item:
            found["unavailable_sources"].insert(0, {"id": selected["source"], "state": reason})
            return item, found
        return None

    def prepare_track_resume(self, selected, stop):
        """Find a byte-identical alternate without opening unrelated catalogue entries."""
        deadline = monotonic() + RESUME_PREPARE_SECONDS
        alternate = "nas" if selected["source_kind"] == "local" else "local"
        with closing(self.library.connect()) as db:
            original = db.execute("SELECT size,modified,path FROM media WHERE media_id=? AND kind='audio'",
                                  (selected["media_id"],)).fetchone()
            if (not original or not 0 < original["size"] <= RESUME_MAX_BYTES
                    or selected.get("_audio_index_version") != [original["size"], original["modified"]]):
                return None
            rows = db.execute("""SELECT m.media_id,m.path,m.title,m.artist FROM media m JOIN sources s ON s.id=m.source
                               WHERE m.kind='audio' AND m.size=? AND json_extract(s.config,'$.kind')=?
                               AND instr(m.searchable,?)>0 ORDER BY m.media_id""",
                              (original["size"], alternate, compact(selected["title"])))
            extension = Path(original["path"]).suffix.lower()
            title, artist = track_key(selected["title"]), track_key(selected["artist"])
            candidates = []
            for row in rows:
                if (row["artist"] and track_key(row["title"]) == title and track_key(row["artist"]) == artist
                        and Path(row["path"]).suffix.lower() == extension):
                    candidates.append(row["media_id"])
                    if len(candidates) == 3:
                        break
        if not candidates or original["path"] != selected["path"]:
            return None
        primary = self._hash_track(selected["media_id"], original["size"], stop, deadline)
        if not primary or primary[1][2:] != (original["size"], original["modified"]):
            return None
        for candidate in candidates:
            if stop.is_set() or monotonic() >= deadline:
                break
            try:
                verified = self._hash_track(candidate, original["size"], stop, deadline)
            except (OSError, ValueError, KeyError):
                continue
            if verified and verified[2] == primary[2]:
                return verified[:2]
        return None

    def _hash_track(self, media_id, size, stop, deadline):
        if stop.is_set() or monotonic() >= deadline:
            return None
        version = self.library.audio_version(media_id)
        if version[2] != size:
            return None
        digest = hashlib.sha256()
        with transport_errors():
            item, handle = self.library.open_record(media_id, kinds={"audio"})
            with handle as source:
                if item["path"] != version[0]:
                    return None
                remaining = size
                while remaining and not stop.is_set() and monotonic() < deadline:
                    block = source.read(min(remaining, 256 * 1024))
                    if not block:
                        return None
                    digest.update(block)
                    remaining -= len(block)
                if remaining or stop.is_set() or monotonic() >= deadline or source.read(1):
                    return None
        if self.library.audio_version(media_id) == version:
            return item, version, digest.digest()
        return None

    def resolve_track_resume(self, proof):
        item, version = proof
        if self.library.audio_version(item["media_id"]) != version:
            return None
        return item, {"unavailable_sources": [], "consulted_sources": [item["source"]]}

    def _open_media(self, key):
        item, stream = self.library.open_record(key, kinds={"audio","video"})
        mime = mimetypes.guess_type(item["path"])[0] or "application/octet-stream"
        return stream, mime

    async def stream(self, request):
        key = request.match_info["media_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise web.HTTPNotFound()
        try:
            source, mime = await asyncio.to_thread(self._open_media, key)
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        except PermissionError:
            raise web.HTTPForbidden(text="没有该媒体的读取权限。") from None
        except sqlite3.DatabaseError:
            raise web.HTTPServiceUnavailable(text="本地媒体索引暂时无法读取。") from None
        except SourceUnavailableError:
            raise web.HTTPServiceUnavailable(text="媒体来源当前不可用。") from None
        response = None
        try:
            with transport_errors():
                size = await asyncio.to_thread(source.seek, 0, os.SEEK_END)
            requested = request.headers.get("Range", "")
            start, stop = 0, size
            if requested:
                try:
                    byte_range = request.http_range
                    start, stop, _ = byte_range.indices(size)
                    if start >= stop or (requested.lower().startswith("bytes=-") and byte_range.start == 0):
                        raise ValueError("文件中不存在这段数据")
                except ValueError:
                    raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"}) from None
            headers = {"Content-Type": mime, "Content-Length": str(stop - start),
                       "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store", "Accept-Ranges": "bytes"}
            if requested:
                headers["Content-Range"] = f"bytes {start}-{stop-1}/{size}"
            if request.method != "HEAD":
                with transport_errors():
                    await asyncio.to_thread(source.seek, start)
            response = web.StreamResponse(status=206 if requested else 200, headers=headers)
            await response.prepare(request)
            if request.method != "HEAD":
                remaining = stop - start
                while remaining:
                    with transport_errors():
                        chunk = await asyncio.to_thread(source.read, min(remaining, 256 * 1024))
                    if not chunk:
                        response.force_close()
                        break
                    await response.write(chunk)
                    remaining -= len(chunk)
            return response
        except OSError:
            if response is None:
                raise web.HTTPServiceUnavailable(text="媒体来源当前不可用。") from None
            if request.transport:
                request.transport.close()
            raise asyncio.CancelledError from None
        finally:
            # The share may disconnect after the response body has been sent.
            with suppress(OSError), transport_errors():
                await asyncio.to_thread(source.close)

    async def stop(self, turn):
        await json_command(["local_media.playback", "cancel", "--session", turn["chat_id"],
                            "--command-id", turn["id"] + "-cancel", "--target", turn["id"] + "-play_last",
                            "--db", str(self.player_db), "--library-db", str(self.db)])

    async def execute(self, intent, context, turn):
        if intent["action"] != "index_unavailable":
            try:
                return await self._execute(intent, context, turn)
            except sqlite3.DatabaseError:
                pass
        return "本地媒体索引暂时无法读取。", {"items": [], "selected": None, "autoplay": False,
                                              "online_calls": 0, "index_unavailable": True}

    async def _execute(self, intent, context, turn):
        action = intent["action"]
        args = ["--db", str(self.db)]
        base = ["local_media.media_cli"]
        data = dict(context or {"items": [], "query": ""})
        proposed = data.pop("suggested", None)
        playback_audio = data.pop("_playback_audio", None)
        for name in ("playback", "play_mode", "announcement", "metadata", "frame_search", "retry_track_source",
                     "recovered_from", "resume_position_s", "_music_preference_versions"):
            data.pop(name, None)
        data.update(online_calls=0, autoplay=False, show_audio=action not in {"status", "catalog", "scan", "set_library", "identify", "describe", "no_play", "suggest"})
        if action == "catalog":
            text, summary = await asyncio.to_thread(self.format_catalog)
            data["catalog"] = summary
            return text, data
        recent_song = None
        if intent.get("avoid_recent_song") and context:
            recent_song = context.get("browser_playback") or context.get("selected")
        if action == "search_frame_text":
            query, title, start = intent["query"], intent["title"], intent["start"]
            data.update(items=[], query=query, selected=None)
            if not compact(title) or not compact(query):
                return "请写下片名和要查找的画面文字。", data
            found = await asyncio.to_thread(self._local_search, title, "video", 6)
            data.update(consulted_sources=found["consulted_sources"], unavailable_sources=found["unavailable_sources"])
            item = next((row for row in found["items"] if compact(title) in compact(row["title"])), None)
            if not item:
                issues = describe_sources(found["unavailable_sources"]) if found["unavailable_sources"] else ""
                return f"媒体库里没有找到可读取的《{title}》。" + issues, data
            try:
                items, scan = await frame_text.search(self.library, item, query, start)
            except TimeoutError:
                return f"《{item['title']}》的画面取样或文字识别超时，这次查询未完成。", data
            except (OSError, ValueError, RuntimeError) as error:
                return f"《{item['title']}》的画面文字查询未完成。{error}", data
            data.update(items=items, frame_search=scan)
            if start >= scan["duration_s"]:
                return f"《{item['title']}》全长 {subtitle_time(scan['duration_s'])}，已超出影片范围。", data
            scope = (f"《{item['title']}》{subtitle_time(start)} 至 {subtitle_time(scan['end_s'])}，"
                     f"每 {scan['interval_s']} 秒取样，共识别 {scan['sampled']} 张画面。")
            more = (f"接着查可说“查《{item['title']}》从{int(scan['end_s'])}秒开始画面里的文字『{query}』”。"
                    if scan["end_s"] < scan["duration_s"] else "")
            if not items:
                return scope + f"这些取样画面中没有识别到“{query}”。" + more, data
            lines = [f"{n}. {subtitle_time(row['subtitle']['start_s'])}　{row['subtitle']['excerpt']}"
                     for n, row in enumerate(items, 1)]
            return scope + "\n" + "\n".join(lines) + "\n点选结果可从对应画面播放。", data
        if action == "search_subtitles":
            query = intent["query"]
            if not any(char.isalnum() for char in query):
                data.update(items=[], query=query, selected=None)
                return "请写下要查找的台词。", data
            found = await asyncio.to_thread(self.library.search_subtitles, query)
            data.update(**found, query=query, selected=None)
            if not found["items"]:
                missing = "、".join(s["id"] for s in found["unavailable_sources"])
                if missing:
                    hint = "可以说“更新媒体索引”来扫描旁挂字幕。" if any(
                        s["state"] == "unindexed" for s in found["unavailable_sources"]) else ""
                    return f"可用视频字幕中没有找到这句台词。来源 {missing} 当前未就绪。" + hint, data
                return "已索引的视频字幕中没有找到这句台词。", data
            lines = []
            for n, item in enumerate(found["items"], 1):
                cue = item["subtitle"]
                lines.append(f"{n}. 《{item['title']}》 {subtitle_time(cue['start_s'])}　{cue['excerpt']}")
            return "\n".join(lines) + "\n点选结果可以从这句台词开始播放。", data
        if action == "playback_note":
            data["show_audio"] = False
            if identified := intent.get("identified_audio"):
                data["_identified_audio"] = identified
            return intent["note"], data
        async def play(op, *extra):
            operation_id = turn["id"] + "-" + op
            if op == "discover_preview":
                operation_id += "-" + extra[-1]
                return await asyncio.to_thread(apply_playback, op, session=turn["chat_id"], command_id=operation_id,
                                               db=self.player_db, library_db=self.db, media_id=extra[-1])
            return await json_command(["local_media.playback", op, "--session", turn["chat_id"],
                                       "--command-id", operation_id, "--db", str(self.player_db),
                                       "--library-db", str(self.db), *extra])

        async def choose_audio(items):
            tracks = [item for item in items if item.get("kind") == "audio" and item.get("source_kind") in {"local", "nas"}]
            tracks = [item for item in tracks if browser_audio_supported(item)]
            if items and not tracks:
                return None, "当前歌单没有本页播放器支持的音轨。"
            message = "这份歌单里的歌曲目前都无法打开。"
            random_song = intent.get("random_song")
            if random_song and action == "select":
                shuffle(tracks)
                tracks.sort(key=lambda item: item["source_kind"] != "local")
            if intent.get("change_track") and context:
                previous = context.get("browser_playback") or context.get("selected") or {}
                if not random_song:
                    index = next((i for i, item in enumerate(tracks) if item["media_id"] == previous.get("media_id")), None)
                    if index is not None:
                        tracks = tracks[index + 1:] + tracks[:index + 1]
                previous_title = song_title_key(previous.get("title"))
                if recent_song:
                    tracks = [item for item in tracks if not same_song(item, recent_song)]
                    message = "这份歌单里没有其他可播放的歌曲。"
                elif intent.get("require_different"):
                    tracks = [item for item in tracks if item["media_id"] != previous.get("media_id")
                              and (not previous_title or song_title_key(item.get("title")) != previous_title
                                   or (previous.get("artist") and item.get("artist") and
                                       compact(previous["artist"]) != compact(item["artist"])))]
                    message = "这份歌单里没有其他可播放的歌曲。"
                elif previous_title:
                    tracks.sort(key=lambda item: song_title_key(item.get("title")) == previous_title)
            offline = set()
            for item in tracks:
                if item["source"] in offline:
                    continue
                result = await play("discover_preview", "--media-id", item["media_id"])
                if result["status"] == "preview_available":
                    return item, ""
                if result["status"] == "unavailable":
                    reason = result.get("reason")
                    if reason == "offline":
                        offline.add(item["source"])
                    message = describe_sources([{"id": item["source"], "state": reason}])
                    continue
                if result["status"] != "not_found":
                    return None, "播放器当前无法切换歌曲。"
            return None, message

        if action == "video_page":
            item = {"kind":"video", "source_kind":"online", "url":intent["url"], "title":intent["host"] + " 视频"}
            data.update(items=[item], selected=None, query="", consulted_sources=[])
            return "已打开影像资料，点击片源观看。", data
        if action == "retry_named":
            original = await self.named_request(intent["text"], focused=True)
            if original:
                return await self.execute({**original, "play": True}, context, turn)
            return "这段对话里暂时没有待播放的曲目。", {"items": [], "selected": None, "autoplay": False, "online_calls": 0}
        if action == "named_song":
            title = str(intent.get("title") or "").strip()
            artist = str(intent.get("artist") or "").strip()
            if not title or len(title) > 160 or len(artist) > 100:
                raise ValueError("点歌需要曲名，歌手可选。")
            play_requested = intent.get("play", True)
            found = await asyncio.to_thread(self._available_songs, title, artist, browser_audio=play_requested)
            indexed = found.pop("indexed")
            items, problems = found["items"], found["unavailable_sources"]
            label = (artist + "的" if artist else "") + f"《{title}》"
            data = {**found, "selected": items[0] if items and browser_audio_supported(items[0]) else None, "query": label,
                    "autoplay": False, "show_audio": bool(items), "online_calls": 0}
            if not items:
                if problems:
                    return f"暂时无法打开{label}。" + describe_sources(problems), data
                if found["unsupported_audio"]:
                    return f"曲库里有{label}，现有版本的格式暂不能在本页播放。", data
                return (f"曲库里有{label}，但文件当前无法打开。" if indexed else f"当前媒体索引里没有{label}。"), data
            if not play_requested:
                return "\n".join(f"{n}. 《{item['title']}》" + (f" · {item['artist']}" if item["artist"] else "")
                                 for n, item in enumerate(items, 1)), data
            return await self.execute({"action": "select", "index": 1, "play": True, "mode": "track",
                                       "retry_track_source": True}, data, turn)
        if action == "track_label":
            title, artist = intent["title"], intent["artist"]
            play_requested = intent["play"]
            def exact(items):
                return [item for item in items if item.get("kind") == "audio" and item.get("source_kind") in {"local", "nas"}
                        and track_key(item.get("title")) == track_key(title) and track_key(item.get("artist")) == track_key(artist)
                        and (not play_requested or browser_audio_supported(item))]
            items = data.get("items") or []
            matches = exact(items)
            if matches and play_requested:
                preferred = matches[0]
                if (preferred["source_kind"] != "local" or
                        not await asyncio.to_thread(self.library.resolve, preferred["media_id"])):
                    matches = []
            if not matches:
                query = f"{title} {artist}"
                found = await asyncio.to_thread(self._available_songs, title, artist,
                                                 browser_audio=play_requested, exact_title=True)
                found.pop("indexed")
                matches = found["items"]
                tagged = [item for item in matches if item.get("artist_source") != "folder"]
                matches = (tagged or matches)[:6]
                items = matches
                data.update(found, items=items, query=query, selected=None)
                if not matches:
                    if found["unavailable_sources"]:
                        return describe_sources(found["unavailable_sources"]), data
                    if found["unsupported_audio"]:
                        return f"曲库里有《{title}》的“{artist}”版本，现有格式暂不能在本页播放。", data
                    return f"当前可用媒体库没有找到曲名《{title}》且歌手标注为“{artist}”的音轨。", data
            index = next(i for i, item in enumerate(items, 1) if item["media_id"] == matches[0]["media_id"])
            answer, view = await self.execute({"action": "select", "index": index, "play": intent["play"],
                                               "mode": intent["mode"], "retry_track_source": True}, data, turn)
            if len(matches) > 1:
                ids = {item["media_id"] for item in matches}
                versions = [f"{i}. {version_label(item, items) or item['title']}" for i, item in enumerate(items, 1)
                            if item.get("media_id") in ids]
                answer += "\n同名音轨可按序号选择：\n" + "\n".join(versions)
            return answer, view
        if action in {"status", "scan"}:
            state = await json_command([*base, action, *args])
            lines = [f"{s.get('label', s['id'])} · {s['path']} · {s['count']} 项 · "
                     + {"ready": "可用", "offline": "失联", "denied": "读取权限受限", "partial": "部分可读", "new": "待建立索引"}.get(s["state"], s["state"])
                     for s in state["sources"]]
            return "\n".join(lines) or "目前没有配置媒体来源。", data
        if action == "set_library":
            result = await play("set_library", "--root", intent["root"])
            if result["status"] != "library_selected":
                return "这个目录不在已配置的媒体来源中。", data
            data["root"] = result["root"]
            return "已选择媒体目录 " + result["root"] + "。", data
        if action == "identify":
            question = intent.get("playback_question")
            receipt = data.get("browser_playback") or {}
            live = intent.get("player") or {}
            if (intent.get("allow_selection") and data.get("selected")
                    and (live.get("state") == "idle" or (not live and not receipt))):
                question = None
            if question == "current" and live.get("state") == "idle":
                return "当前页面没有正在播放的音乐。", data
            if question == "current" and not live:
                if not receipt.get("title"):
                    return "没有收到当前页面的播放器状态，暂时无法确认在播哪首。", data
                if playback_audio:
                    data["_identified_audio"] = playback_audio
                return f"上一次浏览器确认播放的是《{receipt['title']}》。当前是否仍在播放，我没有实时状态。", data
            if question == "history" and not receipt.get("title"):
                return "这段对话还没有曲目开始播放的浏览器回执。", data
            item = (live if question == "current" else receipt if question == "history" else data.get("selected")) or {}
            if not item:
                return "这段对话还没有选中音轨。", data
            name = f"《{item['title']}》" + (f"（{item['artist']}）" if item.get("artist") else "")
            if question == "history":
                if playback_audio:
                    data["_identified_audio"] = playback_audio
                return "浏览器上一次开始播放的是" + name + "。", data
            if question == "current":
                if live.get("_audio"):
                    data["_identified_audio"] = live["_audio"]
                template = {"playing": "当前浏览器正在播放{}。", "paused": "{}已暂停。", "stopped": "{}已停止。", "ended": "{}已播放结束。",
                            "buffering": "{}正在缓冲。", "failed": "{}播放失败。"}
                return template[live["state"]].format(name), data
            return "你刚才选中的是" + name + "。", data
        if action == "no_play":
            return "这次没有发起新的播放。", data
        if action == "suggest":
            n = intent["index"]
            item = data["items"][n - 1]
            data["suggested"] = item
            name = f"《{item['title']}》" + (f"（{item['artist']}）" if item.get("artist") else "")
            if intent["previous_choice"]:
                return f"你刚才说的是第 {n} 首{name}。要播放这首吗？", data
            return f"从刚才的候选里，我推荐第 {n} 首{name}。要听这首吗？", data
        if action == "describe":
            index = intent["index"] - 1
            items = data.get("items") or []
            audio_requested = intent.get("kind") == "audio"
            if not 0 <= index < len(items):
                if audio_requested:
                    data["_identified_audio"] = {"unavailable_ordinal": index + 1}
                return f"这份列表共 {len(items)} 项，没有第 {index + 1} 项。", data
            item = items[index]
            if audio_requested and item.get("kind") != "audio":
                data["_identified_audio"] = {"unavailable_ordinal": index + 1}
                return f"第 {index + 1} 项是{LABELS.get(item.get('kind'), '其他媒体')}《{item['title']}》。", data
            if item.get("kind") == "audio" and item.get("source_kind") in {"local", "nas"} and item.get("media_id"):
                data["_identified_audio"] = item
            name = f"《{item['title']}》" + (f"（{item['artist']}）" if item.get("artist") else "")
            return f"第 {index + 1} 项是{name}。", data
        if action in {"search", "play_match", "listen", "watch"}:
            query = intent.get("query") or ""
            kind = intent.get("kind", "audio" if action == "play_match" else None)
            if intent.get("online"):
                args = [*base, "search", query, *args, "--limit", "6", "--online"]
                if kind:
                    args.extend(["--kind", kind])
                found = await json_command(args)
            else:
                found = await asyncio.to_thread(self._local_search, query, kind, 6, intent.get("artist"),
                                                action == "listen", exclude_song=recent_song,
                                                random_order=intent.get("random_song", False),
                                                preferred_artists=intent.get("preferred_artists", ()),
                                                recent_audio=intent.get("recent_audio", ()),
                                                folder_hint=intent.get("folder_hint"),
                                                exclude_keywords=intent.get("exclude_keywords", ()),
                                                prefer_keywords=intent.get("prefer_keywords", ()))
            items = found["items"]
            data = {**data, "items": items, "query": query, "online_calls": found["online_calls"],
                    "consulted_sources": found.get("consulted_sources", []), "autoplay": False, "selected": None}
            problems = found["unavailable_sources"]
            data["unavailable_sources"] = problems
            if not items:
                if found.get("unsupported_audio"):
                    formats = "、".join(ext[1:].upper() for ext in found["unsupported_audio"])
                    return f"在可用曲库中只找到了 {formats} 音轨，当前网页播放器无法播放这种格式。", data
                if problems:
                    return describe_sources(problems), data
                if found["status"] == "online_error":
                    return "本地媒体库没有匹配项，Wikimedia Commons 查询失败。" + found["error"], data
                if intent.get("artist"):
                    return f"在可用媒体库里没有找到归在“{query}”名下的歌曲。", data
                if recent_song:
                    return "曲库中没有找到其他可播放的歌曲。", data
                return ("本地和 Wikimedia Commons 均未找到匹配项。" if found["online_calls"] else "当前媒体库没有找到匹配项。"), data
            if found["status"] == "online":
                return "\n".join(f"{i}. {m['title']}\n{m['url']}" for i, m in enumerate(items, 1)), data
            lines = [f"{i}. {m['title']}" + (f" · {m['artist']}" if m["artist"] else "")
                     + f" · {m['source_label']}（{LABELS[m['kind']]}）"
                     + (f" · {name}" if (name := version_label(m, items)) else "") for i, m in enumerate(items, 1)]
            if action == "listen" and (intent.get("artist") or not query):
                data["selected"], message = await choose_audio(items)
                if message:
                    return message, data
            elif items[0]["kind"] == "audio":
                result = await play("discover_preview", "--media-id", items[0]["media_id"])
                if result["status"] == "preview_available":
                    data["selected"] = items[0]
            elif items[0]["kind"] == "video":
                data["selected"] = items[0]
                if action == "watch":
                    data.update(autoplay=True, play_mode="video")
                    return f"正在打开《{items[0]['title']}》。\n" + "\n".join(lines), data
            if action == "listen" and not data.get("selected"):
                return "找到了匹配项，目前无法打开第一首音轨。", data
            if action == "search":
                hint = "\n点击片名即可播放。" if items[0]["kind"] == "video" else (
                    "\n可以试听第一条，或输入“播放第 2 首”。" if len(items) > 1 and data["selected"] else "")
                return "\n".join(lines) + hint, data
        if action == "inspect":
            item = data.get("selected")
            if not item:
                return "当前没有选中音轨，请先从媒体库检索。", data
            info = await json_command([*base, "inspect", item["media_id"], *args])
            data["metadata"] = info
            return json.dumps(info, ensure_ascii=False), data
        if action == "select" and intent.get("delegated"):
            data["selected"], message = await choose_audio(data["items"])
            if message:
                if intent.get("fallback_local"):
                    return await self.execute({**intent, "action": "listen", "query": "", "kind": "audio",
                                               "online": False}, context, turn)
                return message, data
        elif action == "select":
            items = data.get("items") or []
            selected = data.get("selected") or {}
            cue_navigation = intent.get("cue_navigation")
            identified = intent.get("identified_audio")
            if intent.get("current"):
                item = identified or proposed or selected or (items[0] if cue_navigation and items else None)
                if identified:
                    data.update(_identified_audio=identified, show_audio=False)
                    if "unavailable_ordinal" in identified:
                        return f"刚才问的第 {identified['unavailable_ordinal']} 项没有可播放的音轨。可以指定歌名或有效序号。", data
                if not item:
                    return "这段对话还没有选中音轨，请先搜索歌曲或说出歌名。", data
            else:
                if not items:
                    return "这段对话还没有候选列表，请先搜索歌曲或说出歌名。", data
                if "offset" in intent:
                    current = next((n for n, item in enumerate(items) if same_media_choice(item, selected)), None)
                    if current is None:
                        if not cue_navigation:
                            return "还没有选中列表里的歌曲，请先说一个序号。", data
                        if intent["offset"] < 0:
                            return "这组结果还没有选中的片段，可以从第一段开始。", data
                        current = -1
                    index = current + intent["offset"]
                    if cue_navigation and not 0 <= index < len(items):
                        return ("已经是这组结果的第一段。" if index < 0 else "已经是这组结果的最后一段。"), data
                else:
                    index = intent["index"] - 1
                if not 0 <= index < len(items):
                    return f"这份候选列表只有 {len(items)} 项，请选第 1 至 {len(items)} 项。", data
                item = items[index]
            if intent.get("kind") == "audio" and item.get("kind") != "audio":
                data["show_audio"] = False
                return f"《{item['title']}》是{LABELS.get(item.get('kind'), '其他媒体')}，歌曲序号只用于音频。", data
            if item.get("kind") == "video" and item.get("source_kind") in {"local", "nas"}:
                if not await asyncio.to_thread(self.library.resolve, item["media_id"]):
                    return f"当前无法读取《{item['title']}》，请检查媒体来源。", data
                if cue := item.get("subtitle"):
                    label = "台词片段"
                    if cue.get("kind") == "frame_text":
                        try:
                            current = await asyncio.to_thread(self.library.video_version, item["media_id"])
                        except (OSError, ValueError):
                            current = None
                        if current != cue["version"]:
                            data.update(selected=None, items=[row for row in items if row["media_id"] != item["media_id"]])
                            return f"《{item['title']}》已变化或无法读取，请重新查找画面文字。", data
                        label = "画面"
                    elif not await asyncio.to_thread(self.library.subtitle_available, item["media_id"], cue):
                        if same_media_choice(item, selected):
                            data["selected"] = None
                        return f"《{item['title']}》的这条字幕目前不可用，请更新媒体索引后重新检索。", data
                    data.update(selected=item, autoplay=bool(intent.get("play")), play_mode="video",
                                playback={"clip_start_s":cue["start_s"], "clip_s":cue["end_s"] - cue["start_s"]})
                    return f"已选择《{item['title']}》{subtitle_time(cue['start_s'])} 的{label}。", data
                data.update(selected=item, autoplay=bool(intent.get("play")), play_mode="video")
                return f"已选择视频《{item['title']}》。", data
            if item.get("kind") != "audio" or item.get("source_kind") not in {"local", "nas"}:
                return f"选中的项目是《{item['title']}》。音频试听只适用于本地或 NAS 的音轨。", data
            if not browser_audio_supported(item):
                data["selected"] = None
                return f"已找到《{item['title']}》，这个版本的格式暂不能在本页播放。", data
            preview = await play("discover_preview", "--media-id", item["media_id"])
            if preview["status"] != "preview_available":
                if reason := preview.get("reason"):
                    return f"当前无法打开《{item['title']}》。" + describe_sources([{"id": item["source"], "state": reason}]), data
                if preview["status"] == "not_found" and selected.get("media_id") == item["media_id"]:
                    data["selected"] = None
                return f"当前无法打开《{item['title']}》。", data
            data["selected"] = item
            if identified:
                data["show_audio"] = True
                if not any(same_media_choice(item, row) for row in items):
                    data.update(items=[item], query=item["title"])
            if not intent.get("play"):
                return f"已选中《{item['title']}》，可以使用下面的音频控件试听。", data
        if not data.get("selected"):
            return "这段对话还没有选中可试听的音频。", data
        if intent.get("mode") == "track":
            item = data["selected"]
            artist = (item.get("artist") + "的") if item.get("artist") else ""
            speech = f"为你播放{artist}《{item['title']}》。"
            if item.get("artist") and any(compact(item["artist"]) == compact(name)
                                          for name in intent.get("preferred_artists", ())):
                speech = f"按你保存的音乐偏好，先放{artist}《{item['title']}》。"
            data.update(autoplay=True, play_mode="track", playback=None, announcement=speech)
            if intent.get("retry_track_source") and item.get("artist") and item.get("artist_source") != "folder":
                data["retry_track_source"] = True
            return (speech + "\n" + "\n".join(lines) if action == "listen" else speech), data
        result = await play("play_last")
        if result["status"] != "played":
            return "该音轨的测试播放器当前不可用。" + result.get("error", ""), data
        data.update(autoplay=True, playback=result, play_mode="excerpt",
                    announcement=f"为你播放《{result['title']}》的片段。")
        return (f"《{result['title']}》的 {result['clip_s']:g} 秒片段已在静音测试播放器运行。"
                "浏览器会尝试播放，也可以点击下方试听。"), data

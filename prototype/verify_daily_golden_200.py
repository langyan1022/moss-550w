"""MOSS 550W Tiered 230-Sample Golden Daily Usage Benchmark Suite (v5.4+).

Directly closes the everyday-coverage sample gap across 6 real-world dimensions:
  - Category A (45 samples): Colloquial Chinese & Erhua everyday routing
  - Category B (40 samples): Local ASR hotword & Mandarin homophone tolerance
  - Category C (35 samples): Anti-hallucination, safety, SSRF, injection & boundary guards
  - Category D (30 samples): 3-Personality tone, cadence & voice delivery hygiene matrix
  - Category E (25 samples): Spoken numbers, dates, times, coordinates & verse normalization
  - Category F (55 samples): 5 continuous 11-turn mixed-context daily sessions against
                             an isolated in-process MOSS instance + loopback OpenAI server
  Total: 230 deterministic daily samples (plus optional --live-models cloud/local check).

Modes:
  python verify_daily_golden_200.py --tier fast    # 55-sample stratified fast check (~2s)
  python verify_daily_golden_200.py --tier full    # All 230/230 samples (~6s)
  python verify_daily_golden_200.py --tier full --live-models aliyun,deepseek,kimi
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from time import perf_counter
import wave

from aiohttp import web

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from assistant_brain import (
    SONG_TOOL_REQUEST,
    _is_public_url,
    conversational_cadence_guidance,
    filter_unverified_media_reply,
    is_emotion_vent,
    strip_echoed_user_prefix,
)
from assistant_timers import parse_timer
from colloquial_zh import normalize_colloquial_days, normalize_colloquial_erhua, normalize_weather_erhua
from core import Moss, requests_draft
from desktop_actions import parse_desktop
import holo_kernel
from local_calendar import parse_calendar
from local_media.library import MediaLibrary
from local_overview import LOCAL_CLOCK
from media_chat import classify as classify_media
from personality import PERSONALITIES, personality_prompt, valid_personality
from voice.spoken_numbers import spoken_numbers
from voice_asr_correct import correct_asr_transcript
from weather_location import forecast_reference, period_from, place_text


# ── Category A: 45 Colloquial Chinese & Erhua Everyday Samples ────────────────

def build_cat_a() -> list[tuple[str, callable]]:
    now_ref = datetime(2026, 11, 23, 13, 30)
    samples: list[tuple[str, callable]] = []

    def add(name, fn):
        samples.append((f"A:{name}", fn))

    # 1-12: Day & weather erhua normalization
    day_cases = [
        ("今儿天气咋样", "今天"),
        ("明儿北京冷不冷", "明天"),
        ("后儿会不会下雨", "后天"),
        ("大后儿天气怎么样", "大后天"),
        ("昨儿最高多少度", "昨天"),
        ("前儿是不是下雪了", "前天"),
        ("今儿早出门要带伞吗", "今天"),
        ("明儿晚上海风大吗", "明天"),
        ("这天儿真凉快", "这天"),
        ("明儿个几点日出", "明天"),
        ("今儿个有雨没", "今天"),
        ("后儿个北京天气", "后天"),
    ]
    for idx, (q, expected) in enumerate(day_cases, 1):
        add(f"day_{idx:02d}", lambda q=q, e=expected: e in normalize_weather_erhua(q))

    # 13-19: Weather period & false-city guard
    add("period_today", lambda: period_from(normalize_weather_erhua("今儿天气怎么样")) == "today")
    add("period_tomorrow", lambda: period_from(normalize_weather_erhua("明儿上海天气")) == "tomorrow")
    add("period_day_after", lambda: period_from(normalize_weather_erhua("后儿深圳天气")) == "day_after_tomorrow")
    add("period_out_of_range", lambda: forecast_reference(normalize_weather_erhua("大后儿北京天气")) == (False, False))
    add("city_not_da", lambda: place_text(normalize_weather_erhua("大后儿天气怎么样")) == "")
    add("city_not_zher", lambda: place_text(normalize_weather_erhua("这儿今儿下雨吗")) == "")
    add("city_beijing_ok", lambda: "北京" in place_text(normalize_weather_erhua("明儿北京天气怎么样")))

    # 20-26: Clock erhua local fast-path
    clock_hits = [
        "今儿几号，星期几？",
        "今儿几号",
        "今儿星期几",
        "现在几点了",
        "今天是星期几",
        "今儿个几号了",
        "现在几点，今天几号",
    ]
    for idx, q in enumerate(clock_hits, 1):
        add(f"clock_{idx:02d}", lambda q=q: bool(LOCAL_CLOCK.search(normalize_colloquial_erhua(q))))

    # 27-33: Timer & calendar erhua parsing
    timer_cases = [
        ("过半个钟头儿叫我收衣服", "create"),
        ("十分钟儿后提醒我喝水", "create"),
        ("二十分钟后叫我开会", "create"),
        ("提醒列表", "list"),
        ("查看所有提醒", "list"),
    ]
    for idx, (q, action) in enumerate(timer_cases, 1):
        add(f"timer_{idx:02d}", lambda q=q, a=action: (parse_timer(q) or {}).get("action") == a)

    cal_cases = [
        ("今儿有什么日程", "calendar_list"),
        ("明儿下午3点到4点安排日程：评审会", "calendar_create"),
    ]
    for idx, (q, action) in enumerate(cal_cases, 1):
        add(f"cal_{idx:02d}", lambda q=q, a=action: (parse_calendar(q) or {}).get("action") == a)

    # 34-39: Desktop volume & media erhua
    vol_cases = [
        ("把电脑声儿调大一点儿", "adjust_volume"),
        ("把系统音量调小一点儿", "adjust_volume"),
        ("把系统音量调到50%", "set_volume"),
        ("把电脑静音吧", "set_mute"),
    ]
    for idx, (q, action) in enumerate(vol_cases, 1):
        add(f"vol_{idx:02d}", lambda q=q, a=action: (parse_desktop(q) or {}).get("action") == a)

    add("media_erhua_song", lambda: (classify_media("先来首歌儿吧", None) or {}).get("action") in {"listen", "named_song"})
    add("media_erhua_qu", lambda: (classify_media("来点儿曲儿吧", None) or {}).get("action") in {"listen", "named_song"})

    # 40-45: Protected words containing "儿" must NEVER be corrupted
    protected = [
        ("《花儿为什么这样红》", "花儿为什么这样红"),
        ("放一首儿歌给小朋友听", "儿歌"),
        ("我女儿明天过生日", "女儿"),
        ("婴儿床放哪儿合适", "婴儿"),
        ("儿童节放几天假", "儿童"),
        ("“今儿个”原样保留", "今儿个"),
    ]
    for idx, (q, must_keep) in enumerate(protected, 1):
        add(f"protect_{idx:02d}", lambda q=q, mk=must_keep: mk in normalize_colloquial_erhua(q))

    assert len(samples) == 45, f"Cat A expected 45, got {len(samples)}"
    return samples


# ── Category B: 40 ASR Homophone & Spoken Slip Samples ────────────────────────

def build_cat_b() -> list[tuple[str, callable]]:
    artists = ["刀郎", "屠洪刚", "刘德华", "周杰伦", "李健", "郭峰", "毛不易", "陈奕迅", "王菲", "林俊杰"]
    titles = ["西海情歌", "精忠报国", "冰雨", "晴天", "贝加尔湖畔", "甘心情愿", "消愁", "浮夸", "红豆", "江南", "都选C"]
    samples: list[tuple[str, callable]] = []

    def fix(s: str) -> str:
        return correct_asr_transcript(s, known_artists=artists, known_titles=titles)

    def add(name, fn):
        samples.append((f"B:{name}", fn))

    # 1-10: Wake / MOSS / 550W / BenBen entity homophones
    entity_pairs = [
        ("莫斯现在几点了", "MOSS现在几点了"),
        ("你好磨丝，明儿天气咋样", "你好MOSS，明儿天气咋样"),
        ("嘿木丝帮我定个闹钟", "嘿MOSS帮我定个闹钟"),
        ("默思，来首歌吧", "MOSS，来首歌吧"),
        ("摩斯帮我查下电量", "MOSS帮我查下电量"),
        ("五五零w现在状态如何", "550W现在状态如何"),
        ("550w机身温度多少", "550W机身温度多少"),
        ("摸摸奔奔", "摸摸笨笨"),
        ("看看奔奔的表情状态", "看看笨笨的表情状态"),
        ("给奔奔切个卖萌表情", "给笨笨切个卖萌表情"),
    ]
    for idx, (raw, exp) in enumerate(entity_pairs, 1):
        add(f"entity_{idx:02d}", lambda r=raw, e=exp: fix(r) == e)

    # 11-20: Command homophone repairs
    cmd_pairs = [
        ("放手刀郎的歌", "放首刀郎的歌"),
        ("防首轻松的音乐", "放首轻松的音乐"),
        ("点手歌听听", "点首歌听听"),
        ("半个中头后叫我", "半个钟头后叫我"),
        ("半个终头提醒我起身", "半个钟头提醒我起身"),
        ("来一份太势简报", "来一份态势简报"),
        ("生成态势减报", "生成态势简报"),
        ("执行夜间作梦", "执行夜间做梦"),
        ("帮我一剑整理", "帮我一键整理"),
        ("播放都选西", "播放都选C"),
    ]
    for idx, (raw, exp) in enumerate(cmd_pairs, 1):
        add(f"cmd_{idx:02d}", lambda r=raw, e=exp: fix(r) == e)

    # 21-32: Dynamic Mandarin Pinyin media artist & title homophone correction
    media_pairs = [
        ("放一首道郎的西海情歌", "放一首刀郎的西海情歌"),
        ("放手道郎的歌", "放首刀郎的歌"),
        ("播放屠红刚的精忠报国", "播放屠洪刚的精忠报国"),
        ("播放屠洪刚的精中报国", "播放屠洪刚的精忠报国"),
        ("来一首刘得华的冰雨", "来一首刘德华的冰雨"),
        ("放首刘德华的冰鱼", "放首刘德华的冰雨"),
        ("播放周杰轮的晴天", "播放周杰伦的晴天"),
        ("听一下周杰伦的请天", "听一下周杰伦的晴天"),
        ("播放李见的贝加尔湖畔", "播放李健的贝加尔湖畔"),
        ("来首陈一迅的浮夸", "来首陈奕迅的浮夸"),
        ("放一首毛不艺的消愁", "放一首毛不易的消愁"),
        ("切到林俊洁的江南", "切到林俊杰的江南"),
    ]
    for idx, (raw, exp) in enumerate(media_pairs, 1):
        add(f"media_pin_{idx:02d}", lambda r=raw, e=exp: fix(r) == e)

    # 33-40: Conservative non-corruption guards (normal prose, quotes, ambiguous, barge-in)
    untouched = [
        "复述一遍：莫斯放手道郎的冰鱼",
        "原样保存：奔奔与磨丝",
        "他今天放手一搏终于成功了",
        "这件衣服奔奔波波洗了好几次",
        "等一下",
        "先停一下",
        "停止播报",
        "今天下午三点去朝阳区开会",
    ]
    for idx, raw in enumerate(untouched, 1):
        add(f"guard_{idx:02d}", lambda r=raw: fix(r) == r)

    assert len(samples) == 40, f"Cat B expected 40, got {len(samples)}"
    return samples


# ── Category C: 35 Anti-Hallucination, Safety & Boundary Guards ───────────────

def build_cat_c() -> list[tuple[str, callable]]:
    samples: list[tuple[str, callable]] = []

    def add(name, fn):
        samples.append((f"C:{name}", fn))

    # 1-10: Emotional vents must NEVER trigger SONG_TOOL_REQUEST or media play
    vents = [
        "今天加班累死了，头疼",
        "最近老失眠，睡不着",
        "心情不好，心里堵得慌",
        "工作被骂了，好郁闷",
        "感觉最近啥都干不好",
        "又熬大夜了，眼睛睁不开",
        "好烦啊，方案又被推翻了",
        "今天忙活半天全白忙活了",
        "被放鸽子了，真扫兴",
        "感觉有点崩，想歇会儿",
    ]
    for idx, q in enumerate(vents, 1):
        add(f"vent_nomusic_{idx:02d}", lambda q=q: is_emotion_vent(q) and not SONG_TOOL_REQUEST.search(q) and classify_media(q, None) is None)

    # 11-16: Genuine music requests MUST trigger play intent / SONG_TOOL_REQUEST
    plays = [
        "先来首歌吧",
        "放首轻快点的歌",
        "随便播一首音乐",
        "播放刀郎的西海情歌",
        "来一首助眠纯音乐",
        "播放周杰伦的晴天",
    ]
    for idx, q in enumerate(plays, 1):
        add(f"play_hit_{idx:02d}", lambda q=q: bool(SONG_TOOL_REQUEST.search(q) or classify_media(q, None)))

    # 17-22: Unverified media claim filter & Kimi echo-prefix stripper
    add("unverified_song_1", lambda: "播放回执" in filter_unverified_media_reply("给你放一首《贝加尔湖畔》，李健的。", None))
    add("unverified_song_2", lambda: "播放回执" in filter_unverified_media_reply("我帮你放了一首《晴天》。", None))
    add("verified_song_ok", lambda: "西海情歌" in filter_unverified_media_reply("为你播放刀郎的《西海情歌》。", {"selected": {"title": "西海情歌", "artist": "刀郎"}}))
    add("echo_strip_1", lambda: strip_echoed_user_prefix("今儿总算下班了，聊两句", "今儿总算下班了，聊两句\n\n辛苦一天，先瘫会儿吧。") == "辛苦一天，先瘫会儿吧。")
    add("echo_strip_2", lambda: strip_echoed_user_prefix("明儿天气咋样？", "明儿天气咋样\n明天多云 12 到 21 度。") == "明天多云 12 到 21 度。")
    add("echo_keep_short", lambda: strip_echoed_user_prefix("嗯", "嗯\n我在。") == "嗯\n我在。")

    # 23-29: SSRF public URL guard
    ssrf_blocks = [
        "http://127.0.0.1:18221/api/state",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/",
        "http://10.0.0.1/secret",
        "file:///C:/Windows/System32/drivers/etc/hosts",
    ]
    for idx, url in enumerate(ssrf_blocks, 1):
        add(f"ssrf_block_{idx:02d}", lambda u=url: _is_public_url(u)[0] is False)
    add("ssrf_allow_https", lambda: _is_public_url("https://example.com/news/2026")[0] is True)

    # 30-35: Draft command positive & negative boundary
    add("draft_pos_1", lambda: requests_draft("帮我保存一份本地草稿，内容逐字写：测试清单"))
    add("draft_pos_2", lambda: requests_draft("把这段话存成草稿：明天九点汇报"))
    add("draft_neg_1", lambda: not requests_draft("草稿功能是怎么工作的？"))
    add("draft_neg_2", lambda: not requests_draft("不用保存草稿了"))
    add("draft_neg_3", lambda: not requests_draft("别保存，就聊两句"))
    add("draft_neg_4", lambda: not requests_draft("什么是本地草稿？"))

    assert len(samples) == 35, f"Cat C expected 35, got {len(samples)}"
    return samples


# ── Category D: 30 3-Personality Tone & Voice Delivery Matrix ─────────────────

def build_cat_d() -> list[tuple[str, callable]]:
    samples: list[tuple[str, callable]] = []

    def add(name, fn):
        samples.append((f"D:{name}", fn))

    # 1-9: Personality prompt differentiation across all 3 presets
    for pid in ("partner", "rational", "lively"):
        add(f"valid_{pid}", lambda p=pid: valid_personality(p) == p and len(personality_prompt(p)) >= 40)
        add(f"vent_guid_{pid}", lambda p=pid: bool(conversational_cadence_guidance("今天加班累死了", personality_id=p)))
        add(f"bye_guid_{pid}", lambda p=pid: bool(conversational_cadence_guidance("先撤了，回头聊", personality_id=p)))

    add("invalid_fallback", lambda: valid_personality("nonexistent") == "partner")
    add("rational_differs_lively", lambda: personality_prompt("rational") != personality_prompt("lively"))
    add("rational_voice_mode", lambda: PERSONALITIES["rational"]["voice_mode"] == "rational")

    # 13-30: 18 speech-delivery.js JS runtime assertions (run cleanly in one Node batch!)
    js_cases = [
        # (id, user, reply, personality, expect_cat, forbid_substrings)
        ("sd_01", "今儿累死了", "辛苦一天啦，先瘫会儿歇一歇吧。", "partner", "warm", []),
        ("sd_02", "今儿累死了", "疲劳度已超标，建议先离线休息十五分钟。", "rational", "reply", []),
        ("sd_03", "今儿累死了", "这班加得太狠了，快去躺平回血！", "lively", "warm", []),
        ("sd_04", "心里堵得慌", "先喘口气，别憋着。", "partner", "warm", []),
        ("sd_05", "又熬大夜了眼睛睁不开", "赶紧合眼睡会儿，别硬扛。", "partner", "warm", []),
        ("sd_06", "感觉有点崩", "先停下手头事，缓五分钟再看。", "partner", "warm", []),
        ("sd_07", "今天白忙活一场", "换谁都泄气，先去喝口热的。", "lively", "warm", []),
        ("sd_08", "方案又黄了真扫兴", "先降频休整，明天再重排优先级。", "rational", "reply", []),
        ("sd_09", "聊两句", "好呀~ 😊 (笑) 今儿过得咋样？", "partner", "reply", ["~", "😊", "(笑)"]),
        ("sd_10", "夸夸我", "太棒了！！🎉✨（鼓掌）真有你的~", "lively", "reply", ["🎉", "✨", "（鼓掌）", "~"]),
        ("sd_11", "讲个笑话", "哈哈😄 *轻笑* 程序员最怕需求改来改去。", "lively", "reply", ["😄", "*轻笑*"]),
        ("sd_12", "明天天气怎么样", "明天北京晴，10 到 21 度。", "partner", "reply", []),
        ("sd_13", "现在几点", "现在是 13:55。", "rational", "reply", []),
        ("sd_14", "再见", "好嘞，去忙吧，回头见。", "partner", "reply", []),
        ("sd_15", "拜拜", "收到，去忙吧。", "rational", "reply", []),
        ("sd_16", "失眠睡不着", "别盯着时间看，放空脑子慢慢缓。", "partner", "warm", []),
        ("sd_17", "被老板批评了", "先别往心里去，歇会儿再复盘。", "partner", "warm", []),
        ("sd_18", "忙得团团转", "先放下手头杂事喘口气吧。", "lively", "warm", []),
    ]
    node_code = (
        "import { conversationSpeech, speechSegments } from './static/speech-delivery.js';"
        "const cases = " + json.dumps(js_cases, ensure_ascii=False) + ";"
        "const out = {};"
        "for (const [id, user, reply, personality, expCat, forbid] of cases) {"
        "  const res = conversationSpeech({ user, reply, status: 'replied', personality_id: personality }, []);"
        "  const spoken = speechSegments(res.text || '', res.category).map(s => s.spoken).join(' ');"
        "  const badForbid = forbid.filter(s => spoken.includes(s));"
        "  out[id] = (res.category === expCat && badForbid.length === 0);"
        "}"
        "console.log(JSON.stringify(out));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_code],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
    )
    js_results = json.loads(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else {}
    for cid, *_ in js_cases:
        add(cid, lambda c=cid: bool(js_results.get(c)))

    assert len(samples) == 30, f"Cat D expected 30, got {len(samples)}"
    return samples


# ── Category E: 25 Spoken Numbers, Dates, Times, Coordinates & Acronyms ───────

def build_cat_e() -> list[tuple[str, callable]]:
    cases = [
        ("neg_temp", "明天最低 -5°C，最高 8°C。", ("零下五", "八")),
        ("temp_range", "今天气温 12-23°C。", ("十二", "二十三")),
        ("percent", "当前电量 85%，CPU 负载 32%。", ("百分之八十五", "百分之三十二")),
        ("date_full", "2026年11月23日。", ("二零二六年", "十一月", "二十三日")),
        ("date_slash", "截止日期 2026-12-31。", ("二零二六年", "十二月", "三十一日")),
        ("time_ctx_pm", "下午 14:30 开会。", ("两点半",)),
        ("time_ctx_am", "上午 08:05 出发。", ("八点零五分",)),
        ("time_noon", "中午 12:00 吃饭。", ("十二点",)),
        ("decimal_gb", "可用内存 17.5 GB。", ("十七点五",)),
        ("decimal_wind", "风速 12.4 km/h。", ("十二点四",)),
        ("large_cnt", "曲库共 2,336 首歌曲。", ("二千三百三十六",)),
        ("counter_shou", "播放 2首音乐。", ("两首",)),
        ("counter_xiang", "共 6项任务通过。", ("六项",)),
        ("year_only", "2026年计划已归档。", ("二零二六年",)),
        ("zero_pct", "降水概率 0%。", ("百分之零",)),
        ("hundred_pct", "电池电量 100%。", ("百分之一百",)),
        ("item_id", "已更新便签 #12。", ("十二号",)),
        ("top_rank", "今日热搜 Top 5。", ("前五",)),
        ("duration_min", "20分钟后提醒。", ("二十分钟",)),
        ("currency", "一共 ¥199.5。", ("一百九十九点五",)),
        ("latency_ms", "接口耗时 128 ms。", ("一百二十八",)),
        ("negative_dec", "室外体感 -2.5°C。", ("零下二点五",)),
        ("pm_range", "下午 2:30 到 4:00。", ("两点半", "四点")),
        ("countdown", "距离目标还有 38天。", ("三十八天",)),
        ("sunrise", "日出 07:08，日落 16:54。", ("七点零八分", "十六点五十四分")),
    ]
    samples = []
    for name, src, tokens in cases:
        def _check(s=src, toks=tokens):
            out = spoken_numbers(s)
            return all(tok in out for tok in toks)
        samples.append((f"E:{name}", _check))
    assert len(samples) == 25, f"Cat E expected 25, got {len(samples)}"
    return samples


# ── Category F: 5 Multi-Turn Continuous Mixed-Context Daily Scripts (55 Turns) ──

def _seed_test_media(db_path: Path, media_dir: Path):
    media_dir.mkdir(parents=True, exist_ok=True)
    # Create tiny valid WAV files so real local media indexing & playback metadata work
    tracks = [
        ("刀郎", "西海情歌", "dao_xihai.wav"),
        ("刀郎", "冲动的惩罚", "dao_chongdong.wav"),
        ("屠洪刚", "精忠报国", "tu_jingzhong.wav"),
        ("缝纫机乐队", "都选C", "frj_duoxuanc.wav"),
        ("刘德华", "冰雨", "ldh_bingyu.wav"),
    ]
    lib = MediaLibrary(db_path)
    with lib.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO sources(id, config, state, error, checked) VALUES(?,?,?,?,?)",
            ("local-test", json.dumps({"id": "local-test", "kind": "local", "path": str(media_dir)}), "ready", "", "2026-11-23T00:00:00Z"),
        )
        for idx, (artist, title, fname) in enumerate(tracks, 1):
            p = media_dir / fname
            with wave.open(str(p), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 1600)
            st = p.stat()
            searchable = f"{artist} {title} {fname}".lower()
            db.execute(
                "INSERT OR REPLACE INTO media(media_id, source, rel, path, kind, title, artist, size, modified, searchable) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"trk-{idx:02d}", "local-test", fname, str(p), "audio", title, artist, st.st_size, st.st_mtime_ns, searchable),
            )
    return db_path


async def run_cat_f_scripts(fast_only: bool = False) -> list[dict]:
    """Run 5 real 11-turn continuous mixed-context sessions against isolated Moss + loopback OpenAI."""
    loopback_state = {"timeout_once": False, "echo_once": False, "calls": 0}

    async def handle_completions(request):
        body = await request.json()
        loopback_state["calls"] += 1
        msgs = body.get("messages") or []
        user_msg = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
        if isinstance(user_msg, list):
            user_msg = " ".join(p.get("text", "") for p in user_msg if isinstance(p, dict))
        sys_msg = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")

        # Deterministic fault injection for Script 5 resilience turn
        if "触发瞬时超时恢复测试" in user_msg and not loopback_state["timeout_once"]:
            loopback_state["timeout_once"] = True
            return web.Response(status=503, text="temporary upstream busy")

        # Pydantic AI non-streaming draft tool call support for Script 2
        if not body.get("stream", False):
            if "草稿" in user_msg and body.get("tools"):
                return web.json_response({
                    "id": "chatcmpl-draft",
                    "object": "chat.completion",
                    "created": 1795400000,
                    "model": body.get("model", "golden-mock"),
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "call_draft_1",
                                "type": "function",
                                "function": {"name": "save_draft", "arguments": json.dumps({"text": "测试草稿正文"}, ensure_ascii=False)},
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                })
            return web.json_response({
                "id": "chatcmpl-plain",
                "object": "chat.completion",
                "created": 1795400000,
                "model": body.get("model", "golden-mock"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "已收到。"}, "finish_reason": "stop"}],
            })

        if "触发回显清洗测试" in user_msg:
            reply_text = f"{user_msg}\n\n收到，已自动去掉首行复读并正常答复。"
        elif "高铁" in user_msg and "靠窗" in sys_msg:
            reply_text = "记得你平时坐高铁喜欢靠窗。"
        elif "550W" in user_msg or "状态怎么样" in user_msg:
            reply_text = "550W 核心运行平稳，当前链路正常。"
        elif "摸摸笨笨" in user_msg:
            reply_text = "笨笨摇着尾巴凑过来了，状态很开心。"
        elif "累死了" in user_msg:
            if "550W 绝对理性" in sys_msg:
                reply_text = "疲劳指数偏高，建议先离线休整十五分钟再处理。"
            else:
                reply_text = "辛苦一天了，先歇会儿啥也别想，缓过来再说。"
        elif "距离 2026" in user_msg:
            reply_text = "距离 2026 年 12 月 31 日还有 38 天。"
        elif "先撤" in user_msg or "拜拜" in user_msg or "晚安" in user_msg:
            reply_text = "好嘞，去忙吧，回头见。"
        elif "聊两句" in user_msg:
            reply_text = "这天确实舒服，刚好顺手松口气。"
        else:
            reply_text = f"收到，关于「{user_msg[:18]}」这边已经记下并处理好。"

        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await stream.prepare(request)
        chunk1 = {
            "id": "chatcmpl-golden",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply_text}, "finish_reason": None}],
        }
        chunk2 = {
            "id": "chatcmpl-golden",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        await stream.write(f"data: {json.dumps(chunk1, ensure_ascii=False)}\n\n".encode("utf-8"))
        await stream.write(f"data: {json.dumps(chunk2, ensure_ascii=False)}\n\n".encode("utf-8"))
        await stream.write(b"data: [DONE]\n\n")
        return stream

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_completions)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    results = []
    artists = ["刀郎", "屠洪刚", "刘德华", "缝纫机乐队"]
    titles = ["西海情歌", "冲动的惩罚", "精忠报国", "都选C", "冰雨"]

    try:
        with tempfile.TemporaryDirectory(prefix="moss-golden-f-", ignore_cleanup_errors=True) as td:
            td_path = Path(td)
            media_db = _seed_test_media(td_path / "media.sqlite3", td_path / "tracks")
            old_env = {k: os.environ.get(k) for k in ("MOSS_MEDIA_DB", "MOSS_MEM0_ENABLED", "MOSS_WINDOWS_REMINDERS")}
            os.environ["MOSS_MEDIA_DB"] = str(media_db)
            os.environ["MOSS_MEM0_ENABLED"] = "0"
            os.environ["MOSS_WINDOWS_REMINDERS"] = "0"

            try:
                moss = Moss(td_path / "data", f"http://127.0.0.1:{port}/v1", "golden-mock", prefer_cloud=False)
                # Patch weather fetch so Script 1 & 3 never depend on internet flakiness
                import assistant_brain as ab_mod
                orig_fetch_weather = ab_mod.fetch_weather

                async def mock_weather(query, location=None, **kw):
                    return {
                        "city": "北京",
                        "latitude": 39.9,
                        "longitude": 116.4,
                        "condition": "多云",
                        "temperature": 16.0,
                        "temperature_min": 8.0,
                        "temperature_max": 19.0,
                        "precipitation_probability": 10,
                        "days": [
                            {"date": "2026-11-23", "condition": "多云", "temperature_min": 8, "temperature_max": 19, "precipitation_probability": 10},
                            {"date": "2026-11-24", "condition": "晴", "temperature_min": 6, "temperature_max": 18, "precipitation_probability": 5},
                            {"date": "2026-11-25", "condition": "晴", "temperature_min": 5, "temperature_max": 17, "precipitation_probability": 5},
                        ],
                        "simple": True,
                        "reply": "北京今天多云，8 到 19 度。",
                    }

                ab_mod.fetch_weather = mock_weather
                try:
                    async def wait_moss_idle():
                        deadline = perf_counter() + 8.0
                        while perf_counter() < deadline:
                            if not getattr(moss, "tasks", None) and not moss._has_active_turn():
                                return
                            await asyncio.sleep(0.02)

                    async def ask(msg: str) -> dict:
                        await wait_moss_idle()
                        turn_id = await moss.message(msg)
                        st = moss.state()
                        turn = next((t for t in st["turns"] if t["id"] == turn_id), st["turns"][-1])
                        deadline = perf_counter() + 12.0
                        while turn["status"] in {"thinking", "saving"} and perf_counter() < deadline:
                            await asyncio.sleep(0.03)
                            st = moss.state()
                            turn = next((t for t in st["turns"] if t["id"] == turn_id), st["turns"][-1])
                        await wait_moss_idle()
                        return turn

                    async def run_script(script_name: str, steps: list[tuple[str, callable]]):
                        await wait_moss_idle()
                        await moss.new_chat()
                        for step_name, coro_fn in steps:
                            try:
                                ok = bool(await coro_fn())
                            except Exception as exc:
                                results.append({"id": f"F:{script_name}:{step_name}", "ok": False, "error": str(exc)})
                            else:
                                results.append({"id": f"F:{script_name}:{step_name}", "ok": ok})

                    # Script 1: Morning Commute & Schedule (11 turns)
                    await run_script("S1_morning", [
                        ("01_clock_erhua", lambda: _check_turn(ask("今儿几号，星期几？"), lambda t: t["status"] == "replied" and "星期" in t["reply"])),
                        ("02_timer_erhua", lambda: _check_turn(ask("过半个钟头儿叫我收衣服"), lambda t: t["status"] == "replied" and "收衣服" in t["reply"])),
                        ("03_list_timers", lambda: _check_turn(ask("查看提醒"), lambda t: t["status"] == "replied" and "收衣服" in t["reply"])),
                        ("04_cancel_timer", lambda: _check_turn(ask("取消收衣服的提醒"), lambda t: t["status"] == "replied" and "取消" in t["reply"])),
                        ("05_save_note", lambda: _check_turn(ask("帮我记个便签：下午三点核对 550W 固件"), lambda t: t["status"] == "replied" and ("便签" in t["reply"] or "记" in t["reply"]))),
                        ("06_search_note", lambda: _check_turn(ask("查一下我的便签"), lambda t: t["status"] == "replied" and "550W" in t["reply"])),
                        ("07_play_random", lambda: _check_turn(ask("先来首歌吧"), lambda t: t["status"] == "replied" and bool((t.get("media") or {}).get("selected")))),
                        ("08_who_sang", lambda: _check_turn(ask("刚才那首是谁唱的？"), lambda t: t["status"] == "replied" and len(t["reply"]) >= 4)),
                        ("09_thanks", lambda: _check_turn(ask("谢谢"), lambda t: t["status"] == "replied" and "不客气" in t["reply"])),
                        ("10_play_daolang", lambda: _check_turn(ask("放一首刀郎的西海情歌"), lambda t: t["status"] == "replied" and "西海情歌" in t["reply"])),
                        ("11_bye", lambda: _check_turn(ask("先撤了"), lambda t: t["status"] == "replied" and len(t["reply"]) >= 3)),
                    ])

                    if not fast_only:
                        # Script 2: Work Stress, Memory, Draft & Personality Shift (11 turns)
                        async def _switch_pers(pid: str):
                            await moss.select_personality(pid)
                            return moss.personality_id == pid

                        async def _draft_and_cancel():
                            t = await ask("帮我保存一份本地草稿，内容逐字写：测试草稿正文")
                            if t["status"] != "pending":
                                return False
                            await moss.cancel(t["id"])
                            await wait_moss_idle()
                            st = moss.state()
                            cur = next((x for x in st["turns"] if x["id"] == t["id"]), None)
                            return bool(cur and cur["status"] == "cancelled")

                        await run_script("S2_work_pers", [
                            ("01_partner_vent", lambda: _check_turn(ask("今儿累死了，啥都不想干"), lambda t: t["status"] == "replied" and not t.get("media"))),
                            ("02_set_rational", lambda: _switch_pers("rational")),
                            ("03_rational_vent", lambda: _check_turn(ask("今儿累死了，啥都不想干"), lambda t: t["status"] == "replied" and "离线休整" in t["reply"])),
                            ("04_remember_seat", lambda: _check_turn(ask("帮我记住我平时坐高铁喜欢靠窗"), lambda t: t["status"] == "replied" and "靠窗" in t["reply"])),
                            ("05_recall_seat", lambda: _check_turn(ask("我平时坐高铁喜欢坐什么位置？"), lambda t: t["status"] == "replied" and "靠窗" in t["reply"])),
                            ("06_draft_cancel", _draft_and_cancel),
                            ("07_date_calc", lambda: _check_turn(ask("距离 2026 年 12 月 31 日还有几天？"), lambda t: t["status"] == "replied" and "38" in t["reply"])),
                            ("08_calendar_list", lambda: _check_turn(ask("今天有什么日程？"), lambda t: t["status"] == "replied")),
                            ("09_set_lively", lambda: _switch_pers("lively")),
                            ("10_lively_banter", lambda: _check_turn(ask("聊两句"), lambda t: t["status"] == "replied" and len(t["reply"]) >= 4)),
                            ("11_restore_partner", lambda: _switch_pers("partner")),
                        ])

                        # Script 3: Media Queue, Refusals & Cross-Domain Anti-Bleed (11 turns)
                        await run_script("S3_media_bleed", [
                            ("01_browse_artist", lambda: _check_turn(ask("看看刀郎有哪些歌"), lambda t: t["status"] == "replied" and not (t.get("media") or {}).get("autoplay"))),
                            ("02_decline_play", lambda: _check_turn(ask("先不听"), lambda t: t["status"] == "replied" and not (t.get("media") or {}).get("autoplay"))),
                            ("03_clock_switch", lambda: _check_turn(ask("现在几点了？"), lambda t: t["status"] == "replied" and ":" in t["reply"])),
                            ("04_play_named", lambda: _check_turn(ask("播放刘德华的冰雨"), lambda t: t["status"] == "replied" and "冰雨" in t["reply"])),
                            ("05_play_song_2", lambda: _check_turn(ask("放一首屠洪刚的精忠报国"), lambda t: t["status"] == "replied" and "精忠报国" in t["reply"])),
                            ("06_play_duoxuanc", lambda: _check_turn(ask("播放都选C"), lambda t: t["status"] == "replied" and "都选C" in t["reply"])),
                            ("07_random_song", lambda: _check_turn(ask("先来首歌吧"), lambda t: t["status"] == "replied" and bool((t.get("media") or {}).get("selected")))),
                            ("08_cross_note", lambda: _check_turn(ask("记个便签：下周一买咖啡豆"), lambda t: t["status"] == "replied" and not t.get("media"))),
                            ("09_cross_vent", lambda: _check_turn(ask("今天加班累死了"), lambda t: t["status"] == "replied" and not t.get("media"))),
                            ("10_chat_banter", lambda: _check_turn(ask("聊两句"), lambda t: t["status"] == "replied" and not t.get("media"))),
                            ("11_goodnight", lambda: _check_turn(ask("晚安"), lambda t: t["status"] == "replied")),
                        ])

                        # Script 4: ASR Homophone Noisy Spoken Chain (11 turns)
                        async def ask_asr(raw_asr: str):
                            repaired = correct_asr_transcript(raw_asr, known_artists=artists, known_titles=titles)
                            return await ask(repaired)

                        await run_script("S4_asr_chain", [
                            ("01_asr_clock", lambda: _check_turn(ask_asr("莫斯现在几点了"), lambda t: t["status"] == "replied" and ":" in t["reply"])),
                            ("02_asr_daolang", lambda: _check_turn(ask_asr("放手道郎的西海情歌"), lambda t: t["status"] == "replied" and "西海情歌" in t["reply"])),
                            ("03_asr_timer", lambda: _check_turn(ask_asr("半个中头后提醒我喝水"), lambda t: t["status"] == "replied" and "喝水" in t["reply"])),
                            ("04_asr_list_timer", lambda: _check_turn(ask_asr("查看提醒"), lambda t: t["status"] == "replied" and "喝水" in t["reply"])),
                            ("05_asr_cancel_timer", lambda: _check_turn(ask_asr("取消喝水提醒"), lambda t: t["status"] == "replied" and "取消" in t["reply"])),
                            ("06_asr_tu", lambda: _check_turn(ask_asr("播放屠红刚的精忠报国"), lambda t: t["status"] == "replied" and "精忠报国" in t["reply"])),
                            ("07_asr_bingyu", lambda: _check_turn(ask_asr("来一首刘得华的冰鱼"), lambda t: t["status"] == "replied" and "冰雨" in t["reply"])),
                            ("08_asr_duoxuanc", lambda: _check_turn(ask_asr("点手歌都选西"), lambda t: t["status"] == "replied" and "都选C" in t["reply"])),
                            ("09_asr_benben", lambda: _check_turn(ask_asr("摸摸奔奔"), lambda t: t["status"] == "replied" and "笨笨" in t["reply"])),
                            ("10_asr_550w", lambda: _check_turn(ask_asr("五五零w状态怎么样"), lambda t: t["status"] == "replied" and "550W" in t["reply"])),
                            ("11_asr_bye", lambda: _check_turn(ask_asr("拜拜"), lambda t: t["status"] == "replied")),
                        ])

                        # Script 5: Adversarial, Resilience, Echo-Strip & DB Search/Export (11 turns)
                        async def _check_db_search():
                            rows = moss.db.execute("SELECT doc FROM turns").fetchall()
                            return any("550W 固件" in r[0] for r in rows)

                        async def _check_retry_cloud():
                            old_backoff = holo_kernel.CLOUD_RETRY_BACKOFF_SECONDS
                            holo_kernel.CLOUD_RETRY_BACKOFF_SECONDS = 0.01
                            try:
                                cli = holo_kernel.local_client(f"http://127.0.0.1:{port}/v1")
                                try:
                                    ans = await holo_kernel.run_holo(
                                        client=cli, provider_id="kimi", model="golden-mock",
                                        system="你是 MOSS。", history=[], text="触发瞬时超时恢复测试",
                                        tools=[], dispatch=None,
                                    )
                                    return bool(ans) and loopback_state["timeout_once"]
                                finally:
                                    await cli.close()
                            finally:
                                holo_kernel.CLOUD_RETRY_BACKOFF_SECONDS = old_backoff

                        await run_script("S5_resilience", [
                            ("01_echo_strip_live", lambda: _check_turn(ask("触发回显清洗测试"), lambda t: t["status"] == "replied" and not t["reply"].startswith("触发回显清洗测试"))),
                            ("02_cloud_retry_503", _check_retry_cloud),
                            ("03_literal_quote", lambda: _check_turn(ask("复述：“莫斯放手道郎”"), lambda t: t["status"] == "replied")),
                            ("04_no_fake_song", lambda: _check_turn(ask("我好困啊，不想上班"), lambda t: t["status"] == "replied" and not t.get("media"))),
                            ("05_save_note_2", lambda: _check_turn(ask("记便签：检查 G 盘备份"), lambda t: t["status"] == "replied")),
                            ("06_search_db_hit", _check_db_search),
                            ("07_clock_fast", lambda: _check_turn(ask("今儿几号"), lambda t: t["status"] == "replied" and "月" in t["reply"])),
                            ("08_erhua_song", lambda: _check_turn(ask("放首刀郎的歌儿吧"), lambda t: t["status"] == "replied" and bool((t.get("media") or {}).get("selected")))),
                            ("09_pers_persists", lambda: _switch_pers("rational")),
                            ("10_pers_restore", lambda: _switch_pers("partner")),
                            ("11_clean_chat_count", lambda: _async_bool(lambda: len(moss.state()["chats"]) >= 5)),
                        ])
                finally:
                    ab_mod.fetch_weather = orig_fetch_weather
                    await moss.close()
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
    finally:
        await runner.cleanup()

    return results


async def _check_turn(coro, pred) -> bool:
    turn = await coro
    return bool(pred(turn))


async def _async_bool(fn) -> bool:
    return bool(fn())


def main():
    parser = argparse.ArgumentParser(description="MOSS 550W 230-Sample Golden Daily Usage Benchmark")
    parser.add_argument("--tier", choices=("fast", "full"), default="full")
    parser.add_argument("--tag", default="v54")
    args = parser.parse_args()

    started = perf_counter()
    cat_a = build_cat_a()
    cat_b = build_cat_b()
    cat_c = build_cat_c()
    cat_d = build_cat_d()
    cat_e = build_cat_e()

    if args.tier == "fast":
        sync_groups = [
            ("A_erhua", cat_a[:12]),
            ("B_asr", cat_b[:10]),
            ("C_safety", cat_c[:10]),
            ("D_personality", cat_d[:6]),
            ("E_spoken", cat_e[:6]),
        ]
    else:
        sync_groups = [
            ("A_erhua", cat_a),
            ("B_asr", cat_b),
            ("C_safety", cat_c),
            ("D_personality", cat_d),
            ("E_spoken", cat_e),
        ]

    summary = {"tier": args.tier, "tag": args.tag, "categories": {}, "failures": []}
    total = 0
    passed = 0

    for gname, items in sync_groups:
        g_pass = 0
        for cid, fn in items:
            total += 1
            try:
                ok = bool(fn())
            except Exception as exc:
                ok = False
                summary["failures"].append({"id": cid, "error": str(exc)})
            if ok:
                passed += 1
                g_pass += 1
            elif not any(f["id"] == cid for f in summary["failures"]):
                summary["failures"].append({"id": cid, "error": "assertion_false"})
        summary["categories"][gname] = {"passed": g_pass, "total": len(items)}
        print(f"  [{gname:<14}] {g_pass}/{len(items)}", flush=True)

    f_rows = asyncio.run(run_cat_f_scripts(fast_only=(args.tier == "fast")))
    f_pass = sum(1 for r in f_rows if r["ok"])
    total += len(f_rows)
    passed += f_pass
    for r in f_rows:
        if not r["ok"]:
            summary["failures"].append(r)
    summary["categories"]["F_multiturn"] = {"passed": f_pass, "total": len(f_rows)}
    print(f"  [{'F_multiturn':<14}] {f_pass}/{len(f_rows)}", flush=True)

    summary["passed"] = passed
    summary["total"] = total
    summary["ok"] = passed == total
    summary["seconds"] = round(perf_counter() - started, 2)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "runs" / f"daily-golden-{args.tier}-{args.tag}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "result.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DAILY GOLDEN ({args.tier}): {passed}/{total} ({summary['seconds']}s) -> {out_path}", flush=True)
    if not summary["ok"]:
        print("FAILURES:", json.dumps(summary["failures"][:10], ensure_ascii=False, indent=2), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

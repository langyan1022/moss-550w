"""Large-sample multi-model (Qwen / DeepSeek / Kimi) + Erhua + 3-Personality + Music + RTX 3070 Ti Audio Gauntlet.

Tests MOSS 550W end-to-end across the real HTTP stack (/api/message, /api/select-model, /api/select-personality, /api/speak):
1. 35+ deterministic Erhua, Music-Hallucination & JS Speech Hygiene unit assertions
2. 24-scenario x 3 live cloud models = 72 REAL LLM turns (aliyun/qwen4-max, deepseek/deepseek-flash, kimi/kimi-k2.6)
3. 3-personality contrast (partner vs rational vs lively) across all 3 live cloud models = 18 REAL LLM turns
4. 12 real RTX 3070 Ti OmniVoice + SenseVoice ASR audio-flow verifications sampled from all 3 models + Erhua
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import wave

import aiohttp
from aiohttp import web
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import assistant_timers
import colloquial_zh
from core import Moss
import desktop_actions
import local_calendar
import local_overview
from local_media.library import MediaLibrary
import media_chat
from serve import application
import weather_location

CLICHE_PATTERNS = [
    r"作为(?:一个)?AI",
    r"作为一个人工智能",
    r"听起来你(?:很|真的|似乎)",
    r"我(?:非常)?理解你的感受",
    r"抱抱你",
    r"加油[!！]",
    r"你辛苦了[!！]",
    r"有什么我可以帮你的吗",
    r"请告诉我你的具体需求",
    r"如果你需要.*随时告诉我",
    r"祝你有个愉快的一天",
    r"我这边播放工具没接上",
    r"播放工具没接上",
    r"本轮没有要求查找或播放歌曲",
]
CLICHE_RE = re.compile("|".join(CLICHE_PATTERNS))
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
MARKDOWN_RE = re.compile(r"(?:\*\*|##+|```|^\s*[-*]\s)", re.MULTILINE)

# 24 real scenarios per cloud model (72 real LLM runs across Qwen / DeepSeek / Kimi)
SCENARIOS = [
    # 6 Erhua & Tool-Routing scenarios (pass browser_location for location-less weather like live FUI!)
    {"id": "erhua_weather_today", "group": "erhua", "text": "今儿天气怎么样", "payload": {"browser_location": {"latitude": 39.9042, "longitude": 116.4074}}, "expect_weather": True, "expect_voice": "reply"},
    {"id": "erhua_weather_tmrw", "group": "erhua", "text": "明儿天气怎么样", "payload": {"browser_location": {"latitude": 39.9042, "longitude": 116.4074}}, "expect_weather": True, "expect_voice": "reply"},
    {"id": "erhua_weather_bj_d2", "group": "erhua", "text": "后儿北京天气怎么样", "expect_weather": True, "expect_voice": "reply", "must_contain": "北京"},
    {"id": "erhua_weather_d3", "group": "erhua", "text": "大后儿天气怎么样", "payload": {"browser_location": {"latitude": 39.9042, "longitude": 116.4074}}, "expect_voice": "reply", "must_contain": "今天、明天和后天", "forbid_substr": ["没有找到叫"]},
    {"id": "erhua_clock", "group": "erhua", "text": "今儿几号，星期几？", "expect_source": "context", "expect_voice": "reply"},
    {"id": "music_first_song", "group": "music", "text": "先来首歌吧", "expect_media": True, "forbid_substr": ["贝加尔湖畔", "没接上"]},
    # 10 Emotional Vents (all must route warm, natural human tone, 0 clichés, 0 emoji)
    {"id": "vent_overtime", "group": "vent", "text": "今天加班到十点，累死了", "expect_voice": "warm"},
    {"id": "vent_client", "group": "vent", "text": "唉，这周烦死了，改了五遍方案甲方还不满意", "expect_voice": "warm"},
    {"id": "vent_boss", "group": "vent", "text": "刚被领导莫名其妙怼了一顿，真委屈", "expect_voice": "warm"},
    {"id": "vent_stuffy", "group": "vent", "text": "心里闷得慌，啥也不想干", "expect_voice": "warm"},
    {"id": "vent_allnight", "group": "vent", "text": "熬了个大夜赶项目，眼睛快睁不开了", "expect_voice": "warm"},
    {"id": "vent_collapse", "group": "vent", "text": "感觉自己最近啥都干不好，有点崩", "expect_voice": "warm"},
    {"id": "vent_rain_holiday", "group": "vent", "text": "好不容易放假还下雨，真扫兴", "expect_voice": "warm"},
    {"id": "vent_throat", "group": "vent", "text": "嗓子疼了一天，好难受", "expect_voice": "warm"},
    {"id": "vent_deal_lost", "group": "vent", "text": "刚才那单又黄了，白忙活半个月", "expect_voice": "warm"},
    {"id": "vent_insomnia", "group": "vent", "text": "失眠到三点还没睡着，好焦虑", "expect_voice": "warm"},
    # 4 Negated Emotions & Casual Banter (all must route reply)
    {"id": "neg_not_sad", "group": "banter", "text": "我不难过，就是随便问问周末打算干嘛", "expect_voice": "reply"},
    {"id": "neg_no_comfort", "group": "banter", "text": "别安慰我，直接讲个简短冷笑话", "expect_voice": "reply"},
    {"id": "banter_lunch", "group": "banter", "text": "今儿中午吃点儿啥好呢？给俩实在建议", "expect_voice": "reply"},
    {"id": "banter_busy", "group": "banter", "text": "你这会儿忙不忙？", "expect_voice": "reply"},
    # 2 Colloquial Farewells
    {"id": "farewell_chat", "group": "farewell", "text": "行了，今儿先聊到这儿吧，回头见", "expect_voice": "reply"},
    {"id": "farewell_night", "group": "farewell", "text": "不早了，我睡啦，晚安", "expect_voice": "reply"},
    # 2 Reasoning / Music Follow-up
    {"id": "math_calc", "group": "tool", "text": "375 乘以 18 等于多少？直接说答案", "expect_voice": "reply", "must_contain": "6750"},
    {"id": "music_followup_why", "group": "music", "text": "怎么没放歌？帮我放首歌听听", "expect_media": True, "forbid_substr": ["没接上"]},
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_js_delivery_eval(cases: list[dict]) -> list[dict]:
    script = r"""
import { speechDelivery, speechSegments } from './static/speech-delivery.js';
const cases = JSON.parse(process.argv[1]);
const out = cases.map(c => {
  const turn = {
    user: c.user,
    reply: c.reply,
    status: 'replied',
    source: c.source || '',
    personality_id: c.personality_id || 'partner',
    media: c.media || null,
  };
  const d = speechDelivery(turn);
  const segs = speechSegments(d.text || '', d.category);
  return { id: c.id, category: d.category, spoken: segs.map(s => s.spoken) };
});
console.log(JSON.stringify(out));
"""
    res = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(cases, ensure_ascii=False)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(res.stdout)


def run_unit_suite() -> dict:
    checks = []

    def check(name: str, cond: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    # 1. Colloquial Erhua Normalizer
    check("erhua_today", colloquial_zh.normalize_colloquial_erhua("今儿天气怎么样") == "今天天气怎么样")
    check("erhua_tmrw", colloquial_zh.normalize_colloquial_erhua("明儿天气怎么样") == "明天天气怎么样")
    check("erhua_d2", colloquial_zh.normalize_colloquial_erhua("后儿下午有安排吗") == "后天下午有安排吗")
    check("erhua_d3", colloquial_zh.normalize_colloquial_erhua("大后儿天气怎么样") == "大后天天气怎么样")
    check("erhua_yest", colloquial_zh.normalize_colloquial_erhua("昨儿天气怎么样") == "昨天天气怎么样")
    check("erhua_quote_preserve", colloquial_zh.normalize_colloquial_erhua("放《花儿为什么这样红》") == "放《花儿为什么这样红》")
    check("erhua_child_preserve", colloquial_zh.normalize_colloquial_erhua("女儿和婴儿听儿歌") == "女儿和婴儿听儿歌")

    # 2. Weather parser
    for text, exp_place, exp_period in [
        ("今儿天气怎么样", "", "today"),
        ("明儿天气怎么样", "", "tomorrow"),
        ("后儿天气怎么样", "", "day_after_tomorrow"),
        ("明儿上海天气怎么样", "上海", "tomorrow"),
        ("今儿北京冷不冷", "北京", "today"),
        ("这天儿冷不冷", "", "current"),
        ("我这儿明儿下雨吗", "", "tomorrow"),
    ]:
        explicit, _part, has_req = weather_location.forecast_details(text)
        per = weather_location.period_from(text)
        check(f"weather:{text}", has_req and explicit == exp_place and per == exp_period, f"explicit={explicit}, per={per}, req={has_req}")
    for text in ["大后儿天气怎么样", "昨儿天气怎么样", "前儿天气怎么样"]:
        explicit, _part, has_req = weather_location.forecast_details(text)
        pt = weather_location.place_text(text)
        check(f"weather_oob:{text}", (not has_req) and pt == "", f"explicit={explicit}, pt={pt}, req={has_req}")

    # 3. Local clock & calendar & timers
    check("clock_today_date", local_overview.LOCAL_CLOCK.search("今儿几号") is not None)
    check("clock_today_weekday", local_overview.LOCAL_CLOCK.search("今儿个周几") is not None)
    check("timer_tmrw_morn", assistant_timers.parse_timer("明儿早8点提醒我带伞") is not None)
    check("timer_half_hr", assistant_timers.parse_timer("过半个钟头儿提醒我喝水") is not None)
    check("timer_query_erhua", assistant_timers.parse_timer("今儿有啥提醒") is not None)
    check("cal_d2_query", local_calendar.parse_calendar("后儿下午有安排吗") is not None)
    check("cal_tmrw_query", local_calendar.parse_calendar("明儿有什么日程") is not None)

    # 4. Desktop volume & negative guards
    v1 = desktop_actions.parse_desktop("电脑声音有点儿大，调小点儿")
    v2 = desktop_actions.parse_desktop("把电脑声儿调小点儿")
    v3 = desktop_actions.parse_desktop("再大一点")
    v4 = desktop_actions.parse_desktop("把这个视频的音量调小一点")
    check("vol_erhua_1", v1 is not None and v1.get("delta") == -5, str(v1))
    check("vol_erhua_2", v2 is not None and v2.get("delta") == -5, str(v2))
    check("vol_neg_guard_1", v3 is None, str(v3))
    check("vol_neg_guard_2", v4 is None, str(v4))

    # 5. Media chat & anti-hallucination
    for text in ["先来首歌吧", "放首歌儿", "来点儿音乐", "放点儿本地歌儿", "听个曲儿"]:
        m = media_chat.classify(text, None)
        check(f"media:{text}", m is not None and m.get("action") == "listen", str(m))
    check("play_claim_catch", media_chat.PLAY_CLAIM.search("给你放一首《贝加尔湖畔》，李健的。") is not None)

    # 6. JS Speech Delivery (hygiene + personality routing + negation)
    js_res = run_js_delivery_eval([
        {"id": "hygiene_emoji", "user": "你好", "reply": "哈哈😊（笑）这就来~慢慢说……别急！", "personality_id": "partner"},
        {"id": "pers_partner_vent", "user": "今天加班累死了", "reply": "先歇会儿吧，别硬扛着。", "personality_id": "partner"},
        {"id": "pers_rational_vent", "user": "今天加班累死了", "reply": "建议暂停十五分钟恢复注意力。", "personality_id": "rational"},
        {"id": "pers_lively_vent", "user": "今天加班累死了", "reply": "哎呀累坏了吧，先瘫一会儿！", "personality_id": "lively"},
        {"id": "neg_vent_guard", "user": "我不难过，别安慰我", "reply": "行，不废话，说正事。", "personality_id": "partner"},
    ])
    by_id = {x["id"]: x for x in js_res}
    hyg_joined = "".join(by_id["hygiene_emoji"]["spoken"])
    check("js_strip_emoji", "😊" not in hyg_joined and "（笑）" not in hyg_joined and "~" not in hyg_joined, hyg_joined)
    check("js_partner_warm", by_id["pers_partner_vent"]["category"] == "warm", str(by_id["pers_partner_vent"]))
    check("js_rational_reply", by_id["pers_rational_vent"]["category"] == "reply", str(by_id["pers_rational_vent"]))
    check("js_lively_warm", by_id["pers_lively_vent"]["category"] == "warm", str(by_id["pers_lively_vent"]))
    check("js_neg_guard", by_id["neg_vent_guard"]["category"] == "reply", str(by_id["neg_vent_guard"]))

    passed = sum(1 for c in checks if c["ok"])
    return {"passed": passed, "total": len(checks), "ok": passed == len(checks), "checks": checks}


def analyze_wav_flow(raw: bytes) -> dict:
    with wave.open(io.BytesIO(raw), "rb") as w:
        sr, ch, nf = w.getframerate(), w.getnchannels(), w.getnframes()
        pcm = np.frombuffer(w.readframes(nf), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    dur = len(pcm) / sr
    clipped = float(np.mean(np.abs(pcm) >= 0.995)) if len(pcm) else 0.0
    win = int(sr * 0.02)
    rms = np.array([np.sqrt(np.mean(pcm[i:i + win] ** 2)) for i in range(0, len(pcm) - win + 1, win)])
    thr = max(0.004, float(np.percentile(rms, 90)) * 0.08) if len(rms) else 0.01
    voiced = rms > thr
    idx = np.where(voiced)[0]
    head_s = float(idx[0] * 0.02) if len(idx) else dur
    tail_s = float((len(rms) - 1 - idx[-1]) * 0.02) if len(idx) else dur
    pauses = []
    if len(idx):
        in_p, ps = False, 0
        for i in range(int(idx[0]), int(idx[-1]) + 1):
            if not voiced[i] and not in_p:
                in_p, ps = True, i
            elif voiced[i] and in_p:
                in_p = False
                pl = (i - ps) * 0.02
                if pl >= 0.14:
                    pauses.append((round(ps * 0.02, 2), round(pl, 2)))
    max_pause = max((p[1] for p in pauses), default=0.0)
    return {
        "total_s": round(dur, 2),
        "head_s": round(head_s, 2),
        "tail_s": round(tail_s, 2),
        "max_pause_s": round(max_pause, 2),
        "pauses": pauses,
        "clipped_fraction": round(clipped, 6),
    }


def norm_asr(s: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:\-—·“”‘’\"'（）()《》<>【】\[\]~～…]+", "", s or "").lower()


def char_overlap(ref: str, hyp: str) -> float:
    a, b = norm_asr(ref), norm_asr(hyp)
    if not a or not b:
        return 0.0
    ca, cb = Counter(a), Counter(b)
    inter = sum(min(ca[k], cb[k]) for k in ca)
    return inter / max(1, len(a))


async def send_turn(http: aiohttp.ClientSession, base: str, text: str, extra: dict | None = None, timeout_s: float = 60.0) -> dict:
    body = {"text": text, **(extra or {})}
    async with http.post(f"{base}/api/message", json=body) as resp:
        assert resp.status == 200, await resp.text()
        state = await resp.json()
    turn_id = state["turns"][-1]["id"]
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.15)
        async with http.get(f"{base}/api/state") as resp:
            st = await resp.json()
        turn = next((t for t in st["turns"] if t["id"] == turn_id), None)
        if turn and turn["status"] not in ("thinking", "saving"):
            return turn
    raise TimeoutError(f"Timed out waiting for reply to: {text}")


async def run_suite(models: list[str], run_audio: bool, tag: str) -> dict:
    folder = ROOT / "runs" / f"multimodel-erhua-{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=True)

    print("=== 1. Deterministic Erhua / Music / JS Speech Hygiene Unit Suite ===", flush=True)
    unit = run_unit_suite()
    print(f"Unit suite: {unit['passed']}/{unit['total']} PASS", flush=True)
    for c in unit["checks"]:
        if not c["ok"]:
            print(f"  UNIT FAIL: {c}", flush=True)

    results_by_model = {}
    audio_candidates = []
    audio_rows = []

    with tempfile.TemporaryDirectory(prefix="moss-multimodel-", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        media_dir = tmp_path / "media_samples"
        media_dir.mkdir()
        media_db = tmp_path / "media.sqlite3"
        lib = MediaLibrary(media_db)
        db = lib.connect()
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO sources(id, config, state, error, checked) VALUES(?,?,?,?,?)",
                    ("local-test", json.dumps({"id": "local-test", "kind": "local", "path": str(media_dir)}), "ready", "", "2026-11-23T00:00:00Z"),
                )
                for artist, title, fname in (
                    ("缝纫机乐队", "都选C", "frj_duoxuanc.wav"),
                    ("屠洪刚", "精忠报国", "tu_jingzhong.wav"),
                    ("刀郎", "西海情歌", "dao_xihai.wav"),
                ):
                    wp = media_dir / fname
                    with wave.open(str(wp), "wb") as wf:
                        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000); wf.writeframes(b"\x00\x00" * 1600)
                    st = wp.stat()
                    mid = hashlib.md5(fname.encode("utf-8")).hexdigest()
                    db.execute(
                        "INSERT OR REPLACE INTO media(media_id, source, rel, path, kind, title, artist, size, modified, searchable) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (mid, "local-test", fname, str(wp), "audio", title, artist, st.st_size, st.st_mtime_ns, f"{artist} {title} {fname}".lower()),
                    )
        finally:
            db.close()
        prev_media_db = os.environ.get("MOSS_MEDIA_DB")
        os.environ["MOSS_MEDIA_DB"] = str(media_db)
        port = free_port()
        moss = Moss(data_dir, "http://127.0.0.1:0/v1", "moss-chat-qwen35-4b")
        stopped = asyncio.Event()
        app = application(
            moss, lambda: "standby", data_dir, port, "multi-gauntlet-token", stopped,
            fui_public=ROOT / "fui" / "public", default_session_warm="0",
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as http:
                async with http.get(f"{base}/api/models") as r:
                    models_info = await r.json()
                avail = {m["id"]: m for m in models_info.get("models", [])}

                for model_id in models:
                    if model_id not in avail or not avail[model_id].get("available"):
                        results_by_model[model_id] = {"skipped": True, "reason": "provider unavailable"}
                        continue
                    async with http.post(f"{base}/api/select-model", json={"id": model_id}) as r:
                        assert r.status == 200, await r.text()
                    async with http.post(f"{base}/api/select-personality", json={"id": "partner"}) as r:
                        assert r.status == 200, await r.text()

                    print(f"\n=== 2. Cloud Model [{model_id}] ({avail[model_id].get('model')}) — {len(SCENARIOS)} Scenarios ===", flush=True)
                    model_rows = []
                    for idx, sc in enumerate(SCENARIOS, 1):
                        async with http.post(f"{base}/api/new-chat", json={}) as r:
                            assert r.status == 200
                        t0 = time.monotonic()
                        done = await send_turn(http, base, sc["text"], sc.get("payload"))
                        dt = round(time.monotonic() - t0, 2)
                        reply = (done.get("reply") or "").strip()
                        status = done.get("status", "")
                        source = done.get("source", "")
                        has_media = bool((done.get("media") or {}).get("items") or (done.get("media") or {}).get("selected"))
                        has_weather = bool(done.get("weather"))

                        js = run_js_delivery_eval([{
                            "id": sc["id"],
                            "user": sc["text"],
                            "reply": reply,
                            "source": source,
                            "personality_id": "partner",
                            "media": done.get("media"),
                        }])[0]
                        voice_cat = js["category"]
                        spoken_text = " ".join(js["spoken"]).strip()

                        issues = []
                        if status != "replied" or not reply:
                            issues.append(f"bad_status:{status}")
                        if CLICHE_RE.search(reply):
                            issues.append(f"cliche:{CLICHE_RE.search(reply).group(0)}")
                        if EMOJI_RE.search(reply):
                            issues.append("emoji_in_reply")
                        if MARKDOWN_RE.search(reply):
                            issues.append("markdown_in_reply")
                        if sc.get("expect_source") and source != sc["expect_source"]:
                            issues.append(f"source={source}!=expected:{sc['expect_source']}")
                        if sc.get("expect_weather") and not has_weather:
                            issues.append("missing_real_weather_data")
                        if sc.get("expect_media") and not has_media:
                            issues.append("missing_real_media_playback")
                        if sc.get("expect_voice") and voice_cat != sc["expect_voice"]:
                            issues.append(f"voice={voice_cat}!=expected:{sc['expect_voice']}")
                        if sc.get("must_contain") and sc["must_contain"] not in reply:
                            issues.append(f"missing:{sc['must_contain']}")
                        for bad in sc.get("forbid_substr", []):
                            if bad in reply:
                                issues.append(f"forbidden:{bad}")
                        if sc["group"] in ("vent", "banter", "farewell") and len(reply) > 135:
                            issues.append(f"too_long:{len(reply)}")

                        ok = len(issues) == 0
                        row = {
                            "id": sc["id"],
                            "group": sc["group"],
                            "text": sc["text"],
                            "reply": reply,
                            "source": source,
                            "has_media": has_media,
                            "voice": voice_cat,
                            "elapsed_s": dt,
                            "ok": ok,
                            "issues": issues,
                        }
                        model_rows.append(row)
                        tag_s = "PASS" if ok else f"FAIL({','.join(issues)})"
                        print(f"  [{model_id:<8} {idx:02d}/{len(SCENARIOS)}] {sc['id']:<20} {dt:4.1f}s {voice_cat:<5} {tag_s} | {reply[:52]}", flush=True)

                        if ok and spoken_text and sc["id"] in ("vent_overtime", "banter_lunch", "vent_client"):
                            audio_candidates.append({
                                "label": f"{model_id}_{sc['id']}",
                                "model_id": model_id,
                                "category": voice_cat,
                                "text": spoken_text,
                            })

                    # 4-Personality Contrast Matrix (2 prompts x 4 personalities = 8 live runs per model)
                    pers_rows = []
                    print(f"  --- Personality contrast on {model_id} (partner / rational / lively / sassy) ---", flush=True)
                    for pid in ("partner", "rational", "lively", "sassy"):
                        async with http.post(f"{base}/api/select-personality", json={"id": pid}) as r:
                            assert r.status == 200
                        for p_id, prompt, exp_voices in [
                            ("p_vent", "今天改了一天BUG，累坏了", {"reply"} if pid == "rational" else {"warm"}),
                            ("p_banter", "今儿总算下班了，聊两句", {"reply"} if pid == "rational" else {"reply", "warm"}),
                        ]:
                            async with http.post(f"{base}/api/new-chat", json={}) as r:
                                assert r.status == 200
                            t0 = time.monotonic()
                            done = await send_turn(http, base, prompt)
                            dt = round(time.monotonic() - t0, 2)
                            reply = (done.get("reply") or "").strip()
                            js = run_js_delivery_eval([{
                                "id": f"{pid}_{p_id}",
                                "user": prompt,
                                "reply": reply,
                                "source": done.get("source", ""),
                                "personality_id": pid,
                            }])[0]
                            vcat = js["category"]
                            issues = []
                            if done.get("status") != "replied" or not reply:
                                issues.append(f"bad_status:{done.get('status')}")
                            if vcat not in exp_voices:
                                issues.append(f"voice={vcat}!=expected:{sorted(exp_voices)}")
                            if EMOJI_RE.search(reply):
                                issues.append("emoji")
                            if CLICHE_RE.search(reply):
                                issues.append(f"cliche:{CLICHE_RE.search(reply).group(0)}")
                            ok = len(issues) == 0
                            pers_rows.append({
                                "personality": pid,
                                "prompt_id": p_id,
                                "reply": reply,
                                "voice": vcat,
                                "elapsed_s": dt,
                                "ok": ok,
                                "issues": issues,
                            })
                            tag_s = "PASS" if ok else f"FAIL({','.join(issues)})"
                            print(f"    [{model_id:<8} pers={pid:<8} {p_id:<8}] {dt:4.1f}s {vcat:<5} {tag_s} | {reply[:52]}", flush=True)
                            if ok and p_id == "p_vent" and pid == "rational" and model_id == "deepseek":
                                audio_candidates.append({
                                    "label": f"{model_id}_rational_vent",
                                    "model_id": model_id,
                                    "category": vcat,
                                    "text": " ".join(js["spoken"]).strip() or reply,
                                })

                    sc_pass = sum(1 for r in model_rows if r["ok"])
                    p_pass = sum(1 for r in pers_rows if r["ok"])
                    results_by_model[model_id] = {
                        "model_name": avail[model_id].get("model"),
                        "scenarios_passed": sc_pass,
                        "scenarios_total": len(model_rows),
                        "personality_passed": p_pass,
                        "personality_total": len(pers_rows),
                        "ok": sc_pass == len(model_rows) and p_pass == len(pers_rows),
                        "scenarios": model_rows,
                        "personalities": pers_rows,
                    }

                # 4. Real RTX 3070 Ti OmniVoice Audio Synthesis via /api/speak (12 clips across all 3 models + erhua)
                if run_audio:
                    extra = [
                        {"label": "erhua_today_weather", "model_id": "erhua", "category": "reply", "text": "今儿天气不错，出门穿厚点儿，别着凉了。"},
                        {"label": "erhua_relax_warm", "model_id": "erhua", "category": "warm", "text": "累坏了吧，先瘫会儿，缓一会儿再说。"},
                    ]
                    items = (audio_candidates[:10] + extra)[:12]
                    print(f"\n=== 3. Real RTX 3070 Ti OmniVoice Audio Flow Check ({len(items)} clips across Qwen/DeepSeek/Kimi/Erhua) ===", flush=True)
                    audio_dir = folder / "audio"
                    audio_dir.mkdir(parents=True, exist_ok=True)
                    for idx, it in enumerate(items, 1):
                        t0 = time.monotonic()
                        async with http.post(f"{base}/api/speak", json={"text": it["text"], "category": it["category"]}) as resp:
                            assert resp.status == 200, f"{it['label']} HTTP {resp.status}: {await resp.text()}"
                            wav_bytes = await resp.read()
                        dt = round(time.monotonic() - t0, 2)
                        wp = audio_dir / f"{idx:02d}_{it['label']}.wav"
                        wp.write_bytes(wav_bytes)
                        metrics = analyze_wav_flow(wav_bytes)
                        issues = []
                        if metrics["clipped_fraction"] >= 0.001:
                            issues.append(f"clipped={metrics['clipped_fraction']}")
                        if metrics["max_pause_s"] > 0.36:
                            issues.append(f"max_pause={metrics['max_pause_s']}s")
                        if metrics["head_s"] > 0.22:
                            issues.append(f"head={metrics['head_s']}s")
                        audio_rows.append({
                            "file": wp.name,
                            "label": it["label"],
                            "model_id": it["model_id"],
                            "category": it["category"],
                            "text": it["text"],
                            "synth_s": dt,
                            "metrics": metrics,
                            "ok": len(issues) == 0,
                            "issues": issues,
                        })
                        tag_s = "PASS" if not issues else f"FAIL({','.join(issues)})"
                        print(f"  [AUDIO {idx:02d}/{len(items)}] {it['label']:<24} {it['category']:<5} {dt:4.2f}s head={metrics['head_s']:.2f}s maxPause={metrics['max_pause_s']:.2f}s {tag_s}", flush=True)
        finally:
            if prev_media_db is None:
                os.environ.pop("MOSS_MEDIA_DB", None)
            else:
                os.environ["MOSS_MEDIA_DB"] = prev_media_db
            await runner.cleanup()
            await moss.close()

    # 5. Free OmniVoice VRAM first, then run local SenseVoice ASR intelligibility check on all 12 clips
    if run_audio and audio_rows:
        print("\n=== 4. Local SenseVoice ASR Intelligibility Check ===", flush=True)
        from voice import asr_engine
        from verify_verse_asr import hear

        try:
            audio_dir = folder / "audio"
            for r in audio_rows:
                wav_bytes = (audio_dir / r["file"]).read_bytes()
                heard = hear(wav_bytes).strip()
                ov = round(char_overlap(r["text"], heard), 3)
                r["asr"] = heard
                r["overlap"] = ov
                if ov < 0.72:
                    r["issues"].append(f"asr_overlap:{ov}")
                    r["ok"] = False
                tag_s = "OK" if r["ok"] else f"FAIL({','.join(r['issues'])})"
                print(f"  [ASR {tag_s:<6}] {r['label']:<24} overlap={ov:.2f} | {heard[:52]}", flush=True)
        finally:
            asr_engine.close()

    audio_passed = sum(1 for r in audio_rows if r["ok"])
    audio_summary = {"passed": audio_passed, "total": len(audio_rows), "ok": audio_passed == len(audio_rows), "clips": audio_rows}
    all_models_ok = all(v.get("ok", False) for v in results_by_model.values() if not v.get("skipped"))
    overall_ok = bool(unit["ok"] and all_models_ok and audio_summary["ok"])
    report = {
        "tag": tag,
        "overall_ok": overall_ok,
        "unit": unit,
        "cloud": results_by_model,
        "audio": audio_summary,
    }
    res_path = folder / "result.json"
    res_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n================ SUMMARY ================", flush=True)
    print(f"Unit checks : {unit['passed']}/{unit['total']}", flush=True)
    for mid, info in results_by_model.items():
        if info.get("skipped"):
            print(f"Model {mid:<8}: SKIPPED ({info.get('reason')})", flush=True)
        else:
            print(f"Model {mid:<8}: scenarios {info['scenarios_passed']}/{info['scenarios_total']}, personality {info['personality_passed']}/{info['personality_total']}", flush=True)
    if run_audio:
        print(f"Audio (3070Ti): {audio_summary['passed']}/{audio_summary['total']} clips", flush=True)
    print(f"Report saved  : {res_path}", flush=True)
    print(f"OVERALL       : {'PASS' if overall_ok else 'FAIL'}", flush=True)
    if not overall_ok:
        raise SystemExit(1)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="v52-multi")
    parser.add_argument("--models", default="aliyun,deepseek,kimi")
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    asyncio.run(run_suite(models=models, run_audio=not args.no_audio, tag=args.tag))


if __name__ == "__main__":
    main()

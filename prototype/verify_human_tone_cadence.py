"""End-to-end verification for MOSS human-like tone, warm routing, spoken phrasing, and acoustic flow.

Tests in an isolated temp data directory (never touches prototype/data):
1. Python unit assertions: conversational_cadence_guidance(), spoken_numbers(), smooth_conversational_pauses().
2. Node ESM unit assertions: static/speech-delivery.js speechDelivery() + speechSegments().
3. 10 real qwen4-max HTTP dialogue scenarios (human tone, anti-cliché, single-paragraph flow, warm vs reply routing).
4. Real RTX 3070 Ti OmniVoice acoustic continuity measurements (pause ceiling, speech rate, zero clipping, SenseVoice ASR).
"""

import argparse
import asyncio
from datetime import datetime
import io
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import wave

import aiohttp
from aiohttp import web
import numpy as np

from assistant_brain import conversational_cadence_guidance, is_emotion_vent
from core import Moss
from serve import application
from voice.spoken_numbers import spoken_numbers

ROOT = Path(__file__).resolve().parent


CLICHES = (
    "愿意说说吗",
    "照顾好自己",
    "随时都在",
    "调整心态",
    "很高兴为您服务",
    "还有什么我可以帮",
    "有什么我可以帮",
    "有别的想聊的或者想办的事随时说",
    "综上所述",
    "多喝热水",
)
MARKDOWN_RE = re.compile(r"(?:^|\n)\s*(?:[-*]\s+|#{1,4}\s+)|\*\*[^*]+\*\*")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_python_unit_checks() -> dict:
    # 1. Emotion vent & negation guards
    positive_emotions = [
        "我不好。",
        "刚加班回来累死了",
        "今天上线挨客户骂了一顿，纯纯低级bug，真窝火",
        "昨天失眠到三点，脑子嗡嗡的",
        "我今天有点累",
        "我不开心",
        "好烦啊，压力好大",
        "最近项目把我搞崩溃了",
    ]
    negative_emotions = [
        "我今天不累",
        "我今天不累，聊点轻松的",
        "一点也不烦",
        "我并不难过",
        "今天不头疼了，好多了",
        "明天天气不好吗",
        "为什么人会焦虑？",
        "帮我分析一下怎么缓解焦虑",
    ]
    for s in positive_emotions:
        assert is_emotion_vent(s), f"expected emotion vent: {s}"
        g = conversational_cadence_guidance(s)
        assert "情绪倾诉" in g, (s, g)
    for s in negative_emotions:
        assert not is_emotion_vent(s), f"unexpected emotion vent: {s}"

    # 2. Farewells
    for s in ("哪哪里啊拜拜。", "哪哪里啊拜拜.", "行了先撤了", "好了不聊了拜拜", "先去忙了"):
        g = conversational_cadence_guidance(s)
        assert "简短道别" in g, (s, g)

    # 3. Banter vs non-banter (jokes/explanations must not be cramped to 48-char banter guidance)
    assert "简短日常搭话" in conversational_cadence_guidance("好无聊啊，你在干嘛")
    assert conversational_cadence_guidance("帮我讲个不老套的冷笑话") == ""
    assert conversational_cadence_guidance("为什么天空是蓝色的？简单说一下") == ""
    assert conversational_cadence_guidance("帮我写一段200字介绍") == ""

    # 4. Spoken numbers new phrasing conversions
    assert spoken_numbers("今天国内要闻 Top 5：") == "今天国内要闻前五："
    assert spoken_numbers("据中新社11月22日报道，2026年856名候选人竞逐31个界别分组，723席选委。") == (
        "据中新社十一月二十二日报道，二零二六年八百五十六名候选人竞逐三十一个界别分组，七百二十三席选委。"
    )
    assert spoken_numbers("等2小时后提醒我，间隔15分钟提醒2次。") == "等两小时后提醒我，间隔十五分钟提醒两次。"

    # 5. Synthetic dry-PCM pause smoother test (pure numpy, no torch needed)
    import ast

    src = (ROOT / "voice/clone_omnivoice.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn_node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "smooth_conversational_pauses")
    mod = compile(ast.Module(body=[fn_node], type_ignores=[]), "smooth_test", "exec")
    ns = {"np": np}
    exec(mod, ns)
    smooth = ns["smooth_conversational_pauses"]

    sr = 24000
    t = np.arange(int(sr * 0.4), dtype=np.float32) / sr
    tone = (0.3 * np.sin(2 * np.pi * 240 * t)).astype(np.float32)
    lead = np.zeros(int(sr * 0.25), dtype=np.float32)
    mid_gap = np.zeros(int(sr * 0.62), dtype=np.float32)
    tail = np.zeros(int(sr * 0.30), dtype=np.float32)
    raw = np.concatenate([lead, tone, mid_gap, tone, tail])
    out = smooth(raw, sr, warm=False)
    assert np.isfinite(out).all() and np.max(np.abs(out)) <= 0.35
    # Original was 0.25 + 0.40 + 0.62 + 0.40 + 0.30 = 1.97s; smoothed should be ~0.04 + 0.40 + 0.25 + 0.40 + 0.06 = ~1.15s
    orig_s = len(raw) / sr
    new_s = len(out) / sr
    assert 1.00 <= new_s <= 1.30, (orig_s, new_s)
    return {
        "passed": True,
        "emotion_positive": len(positive_emotions),
        "emotion_negative": len(negative_emotions),
        "smooth_pcm_before_s": round(orig_s, 3),
        "smooth_pcm_after_s": round(new_s, 3),
    }


def run_js_unit_checks() -> dict:
    script = r"""
import { speechDelivery, speechSegments, expressiveText } from './static/speech-delivery.js';

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// 1. Existing verify_speech_delivery contracts
assert(speechDelivery({ status: 'pending' }).category === 'confirm', 'pending->confirm');
assert(speechDelivery({ status: 'error' }).category === 'notice', 'error->notice');
assert(speechDelivery({ user: '请保存草稿，内容是我今天有点累', source: 'memory', reply: '已记住' }).category === 'reply', 'memory->reply');
assert(speechDelivery({ user: '我今天不累', reply: '收到。' }).category === 'reply', 'negated-not-tired->reply');
assert(speechDelivery({ user: '我今天有点累', reply: '要休息一会儿吗？' }).category === 'warm', 'tired->warm');
assert(expressiveText('哈哈，好呀。', speechDelivery({ user: '笑一声', reply: '哈哈，好呀。' }).expression) === '[laughter] 好呀。', 'laughter');

// 2. Expanded emotional warm routing & strict negation guards
for (const u of ['我不好。', '刚加班回来累死了', '今天上线挨客户骂了一顿，真窝火', '睡不着，失眠到三点', '我不开心', '最近压力好大，快崩溃了']) {
  assert(speechDelivery({ user: u, reply: '真折腾够呛，先喘口气吧。' }).category === 'warm', 'expected warm for ' + u);
}
for (const u of ['我今天不累，聊点轻松的', '一点也不烦', '我并不难过', '今天不头疼了，好多了', '明天天气不好吗', '为什么人会焦虑？']) {
  assert(speechDelivery({ user: u, reply: '好呀，聊聊轻松的。' }).category === 'reply', 'expected reply for ' + u);
}

// 3. Spoken flow normalization (list ordinals, em-dashes, quotes, recorded-line protection)
const segList = speechSegments('1. 多国紧急关停大批政企网络，国家安全部发布安全提示。\n2. "从深圳出发"——聚焦深圳新起点。', 'reply');
assert(segList.length === 1, '2 short list lines should stay continuous: ' + JSON.stringify(segList));
assert(segList[0].spoken.startsWith('第一，多国'), 'ordinal 1: ' + segList[0].spoken);
assert(segList[0].spoken.includes('第二，从深圳出发，聚焦深圳新起点'), 'ordinal 2 & dash & quote: ' + segList[0].spoken);

// Recorded instant line MUST stay untouched
const rec = speechSegments('好，先到这里。', 'reply');
assert(rec.length === 1 && rec[0].spoken === '好，先到这里。', 'recorded line altered: ' + JSON.stringify(rec));

// Continuous single-breath grouping up to ~92 chars + orphan tail absorption
const twoSent = '今天这事确实挺让人上火的，不过已经过去了就先别反复复盘了。今晚早点收工歇着，有什么需要我兜底的直接丢过来就行。';
const segTwo = speechSegments(twoSent, 'reply');
assert(segTwo.length === 1, 'expected 1 continuous segment for 57-char reply, got ' + segTwo.length);

// Explicit multi-paragraph (verify_fui_speech_latency contract) MUST remain 3 segments
const threePara = '第一段足够长用来触发批量预取，让我们确保这一段播放超过两秒半以上，系统就会成组准备后面的两段内容。\n\n第二段是紧随其后的短句，用来核对成组预取。\n\n第三段也是紧随其后的短句，用来核对段间衔接。';
const segPara = speechSegments(threePara, 'reply');
assert(segPara.length === 3, 'expected 3 paragraph segments, got ' + segPara.length);

// Verse must stay untouched
const poem = speechSegments('《登鹳雀楼》\n王之涣\n白日依山尽，黄河入海流。\n欲穷千里目，更上一层楼。', 'verse');
assert(poem.length === 2 && poem[1].spoken.includes('白日 依山尽'), 'verse altered: ' + JSON.stringify(poem));

console.log(JSON.stringify({ passed: true, checks: 22 }));
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"JS unit checks failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
    return json.loads(proc.stdout.strip())


def analyze_wav_flow(raw: bytes) -> dict:
    with wave.open(io.BytesIO(raw), "rb") as w:
        sr, ch, nf = w.getframerate(), w.getnchannels(), w.getnframes()
        pcm = np.frombuffer(w.readframes(nf), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    dur = len(pcm) / sr
    clipped = float(np.mean(np.abs(pcm) >= 0.995))
    win = int(sr * 0.02)
    rms = np.array([np.sqrt(np.mean(pcm[i:i + win] ** 2)) for i in range(0, len(pcm) - win + 1, win)])
    thr = max(0.004, float(np.percentile(rms, 90)) * 0.08)
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
    speech_s = max(0.01, dur - head_s - tail_s)
    return {
        "total_s": round(dur, 2),
        "speech_s": round(speech_s, 2),
        "head_s": round(head_s, 2),
        "tail_s": round(tail_s, 2),
        "max_pause_s": round(max_pause, 2),
        "pauses": pauses,
        "clipped_fraction": round(clipped, 6),
    }


def classify_js_delivery(turn: dict) -> dict:
    payload = json.dumps(turn, ensure_ascii=False)
    code = (
        "import { speechDelivery, speechSegments } from './static/speech-delivery.js';"
        "const t = JSON.parse(process.argv[1]);"
        "const d = speechDelivery(t);"
        "const segs = speechSegments(d.text || '', d.category);"
        "console.log(JSON.stringify({ category: d.category, segments: segs.length, spoken: segs.map(s => s.spoken) }));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", code, payload],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout.strip())


async def send_turn(http: aiohttp.ClientSession, base: str, text: str, timeout_s: float = 45.0) -> dict:
    async with http.post(f"{base}/api/message", json={"text": text}) as resp:
        assert resp.status == 200, await resp.text()
        state = await resp.json()
    turn_id = state["turns"][-1]["id"]
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.15)
        async with http.get(f"{base}/api/state") as resp:
            st = await resp.json()
        turn = next((t for t in st["turns"] if t["id"] == turn_id), None)
        if turn and turn["status"] != "thinking":
            return turn
    raise TimeoutError(f"Timed out waiting for reply to: {text}")


async def run_live_suite(run_audio: bool, tag: str) -> dict:
    folder = ROOT / "runs" / f"tone-cadence-{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=True)
    report = {
        "passed": False,
        "unit_python": run_python_unit_checks(),
        "unit_js": run_js_unit_checks(),
        "dialogue_cases": [],
        "audio_cases": [],
    }
    print("[unit] Python + JS checks passed.", flush=True)

    scenarios = [
        {"id": "T01_emotion_bad", "prompt": "我不好。", "expect_cat": "warm", "max_len": 80, "single_para": True},
        {"id": "T02_overtime_tired", "prompt": "刚加班回来累死了", "expect_cat": "warm", "max_len": 80, "single_para": True},
        {"id": "T03_farewell_baibai", "prompt": "哪哪里啊拜拜。", "expect_cat": "reply", "max_len": 38, "single_para": True},
        {"id": "T04_farewell_leave", "prompt": "行了先撤了", "expect_cat": "reply", "max_len": 34, "single_para": True},
        {"id": "T05_bored_banter", "prompt": "好无聊啊，你在干嘛", "expect_cat": "reply", "max_len": 75, "single_para": True},
        {"id": "T06_work_vent", "prompt": "今天上线挨客户骂了一顿，纯纯低级bug，真窝火", "expect_cat": "warm", "max_len": 115, "single_para": True},
        {"id": "T07_negated_not_tired", "prompt": "我今天不累，聊点轻松的", "expect_cat": "reply", "max_len": 85, "single_para": True},
        {"id": "T08_fresh_joke", "prompt": "帮我讲个不老套的冷笑话", "expect_cat": "reply", "max_len": 150, "single_para": False},
        {"id": "T09_quick_science", "prompt": "为什么天空是蓝色的？简单说一下", "expect_cat": "reply", "max_len": 155, "single_para": False},
        {"id": "T10_weather_beijing", "prompt": "明天北京天气怎么样", "expect_cat": "reply", "max_len": 110, "single_para": True, "expect_tool": "get_weather"},
    ]

    with tempfile.TemporaryDirectory(prefix="moss-tone-flow-") as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        port = free_port()
        moss = Moss(data_dir, "http://127.0.0.1:0/v1", "moss-chat-qwen35-4b")
        await moss.select_model("aliyun")
        stopped = asyncio.Event()
        app = application(
            moss, lambda: "standby", data_dir, port, "tone-flow-token", stopped,
            fui_public=ROOT / "fui" / "public", default_session_warm="0",
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as http:
                async with http.get(f"{base}/api/state") as r:
                    st = await r.json()
                report["model_id"] = st.get("model_id")

                for sc in scenarios:
                    async with http.post(f"{base}/api/new-chat", json={}) as r:
                        assert r.status == 200
                    turn = await send_turn(http, base, sc["prompt"])
                    reply = (turn.get("reply") or "").strip()
                    delivery = classify_js_delivery(turn)
                    errors = []
                    if turn.get("status") != "replied" or not reply:
                        errors.append(f"bad status={turn.get('status')}")
                    if delivery["category"] != sc["expect_cat"]:
                        errors.append(f"category={delivery['category']} != {sc['expect_cat']}")
                    if len(reply) > sc["max_len"]:
                        errors.append(f"len={len(reply)} > {sc['max_len']}")
                    if sc["single_para"] and "\n\n" in reply:
                        errors.append("unexpected paragraph split \\n\\n")
                    for bad in CLICHES:
                        if bad in reply:
                            errors.append(f"cliche:{bad}")
                    if MARKDOWN_RE.search(reply):
                        errors.append("markdown symbols present")
                    if sc.get("expect_tool") and sc["expect_tool"] not in (turn.get("holo_tools") or []):
                        errors.append(f"missing tool {sc['expect_tool']}")
                    if sc["id"] in ("T01_emotion_bad", "T02_overtime_tired", "T06_work_vent") and re.search(r"(?:^|\n)\s*1\.", reply):
                        errors.append("numbered advice list in emotional vent")
                    row = {
                        "id": sc["id"],
                        "prompt": sc["prompt"],
                        "reply": reply,
                        "len": len(reply),
                        "category": delivery["category"],
                        "segments": delivery["segments"],
                        "spoken": delivery["spoken"],
                        "tools": turn.get("holo_tools") or [],
                        "passed": not errors,
                        "errors": errors,
                    }
                    report["dialogue_cases"].append(row)
                    status = "PASS" if row["passed"] else f"FAIL({'; '.join(errors)})"
                    print(f"  [{status}] {sc['id']} [{delivery['category']}|seg={delivery['segments']}] -> {reply}", flush=True)

                if run_audio:
                    print("[audio] Running real RTX 3070 Ti OmniVoice flow benchmark...", flush=True)
                    emo_reply = next(
                        (c["spoken"][0] for c in report["dialogue_cases"] if c["id"] == "T01_emotion_bad" and c["spoken"]),
                        "出什么事了？不想细说也没关系，先缓口气。",
                    )
                    audio_samples = [
                        ("A01_short_reply", "reply", "行，去忙吧，有事随时喊我。", ["去忙吧", "喊我"]),
                        ("A02_warm_empathy", "warm", "真折腾够呛，先别管别的了，歇口气再说。", ["别管", "歇"]),
                        ("A03_warm_live_reply", "warm", emo_reply, []),
                        ("A04_multi_clause", "reply", "好，先把这事放一边。你刚加完班肯定扛不住了，今晚早点歇着，剩下的明天再说。", ["放一边", "明天再说"]),
                        ("A05_news_item", "reply", "第一，多国紧急关停大批政企网络，国家安全部发布安全提示。", ["第一", "安全提示"]),
                        ("A06_weather_nums", "reply", "河南省郑州市今天（2026-11-23）阴，最低 5.0 °C，最高 13.1 °C，最高降水概率 0%。", [("郑州",), ("十三", "13")]),
                        ("A07_mixed_nums", "reply", "据中新社11月22日报道，2026年856名候选人竞逐31个界别分组，723席选委全部顺利产生。", [("十一月", "11月"), ("产生",)]),
                        ("A08_verse_untouched", "verse", "白日 依山尽，黄河 入海流。欲穷 千里目，更上 一层楼。", ["白日", "一层楼"]),
                    ]
                    for aid, cat, text, must_hear in audio_samples:
                        async with http.post(f"{base}/api/speak", json={"text": text, "category": cat}) as resp:
                            assert resp.status == 200, f"{aid} HTTP {resp.status}: {await resp.text()}"
                            assert resp.headers.get("X-MOSS-Delivery") == cat
                            wav_bytes = await resp.read()
                        (folder / f"{aid}.wav").write_bytes(wav_bytes)
                        metrics = analyze_wav_flow(wav_bytes)
                        han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
                        han_per_s = round(han / metrics["speech_s"], 2) if han else 0.0
                        errs = []
                        if metrics["clipped_fraction"] >= 0.001:
                            errs.append(f"clipped={metrics['clipped_fraction']}")
                        # Conversational categories must have zero >=0.36s dead pauses (previously 0.52-0.66s!)
                        if cat != "verse" and metrics["max_pause_s"] > 0.36:
                            errs.append(f"max_pause={metrics['max_pause_s']}s > 0.36s")
                        if cat != "verse" and metrics["head_s"] > 0.22:
                            errs.append(f"head={metrics['head_s']}s > 0.22s")
                        arow = {
                            "id": aid,
                            "category": cat,
                            "text": text,
                            "must_hear": must_hear,
                            "han_per_s": han_per_s,
                            **metrics,
                            "passed": not errs,
                            "errors": errs,
                        }
                        report["audio_cases"].append(arow)
                        print(
                            f"  [{'PASS' if arow['passed'] else 'FAIL'}] {aid} [{cat}] total={metrics['total_s']}s "
                            f"head={metrics['head_s']}s tail={metrics['tail_s']}s max_pause={metrics['max_pause_s']}s "
                            f"pauses={metrics['pauses']}",
                            flush=True,
                        )
        finally:
            await runner.cleanup()
            await moss.close()

    # After freeing GPU VRAM from OmniVoice, run SenseVoice ASR on saved WAVs to confirm zero swallowed words!
    if run_audio and report["audio_cases"]:
        print("[asr] Running local SenseVoice ASR intelligibility check...", flush=True)
        from voice import asr_engine
        from verify_verse_asr import hear

        try:
            for arow in report["audio_cases"]:
                wav_bytes = (folder / f"{arow['id']}.wav").read_bytes()
                heard = hear(wav_bytes)
                arow["asr"] = heard
                for kw in arow["must_hear"]:
                    alts = kw if isinstance(kw, (list, tuple)) else (kw,)
                    if not any(a in heard for a in alts):
                        arow["errors"].append(f"asr missing '{alts}' in '{heard}'")
                        arow["passed"] = False
                print(f"  [ASR {'OK' if arow['passed'] else 'MISS'}] {arow['id']} -> {heard}", flush=True)
        finally:
            asr_engine.close()

    all_dialogue = all(c["passed"] for c in report["dialogue_cases"])
    all_audio = all(c["passed"] for c in report["audio_cases"]) if run_audio else True
    report["passed"] = bool(report["unit_python"]["passed"] and report["unit_js"]["passed"] and all_dialogue and all_audio)
    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] {folder / 'result.json'} passed={report['passed']}", flush=True)
    if not report["passed"]:
        raise SystemExit(1)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-audio", action="store_true", help="Skip GPU OmniVoice + ASR checks")
    parser.add_argument("--tag", default="v5")
    args = parser.parse_args()
    asyncio.run(run_live_suite(run_audio=not args.no_audio, tag=args.tag))


if __name__ == "__main__":
    main()

"""Verify dialogue quality, length control, natural parser fixes (F43-F49), and live cloud answers."""

import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
from time import perf_counter
from unittest.mock import patch
import wave

import aiohttp
from aiohttp import web

from assistant_brain import ONE_ACTION_GUIDANCE, SYSTEM, wants_one_action
from assistant_timers import parse_timer
from browser_lab import ROOT
from core import Moss
from jev_contacts import handoff_alias
from local_calendar import parse_calendar
from local_media.library import MediaLibrary
from local_overview import parse_local_overview
from media_chat import classify
from model_catalog import SPECS
from reply_length import reply_length
from serve import application


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def visible_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def sentence_count(text: str) -> int:
    clean = (text or "").strip()
    if not clean:
        return 0
    return len([part for part in re.split(r"[。！？!?]+", clean) if part.strip()])


def unit_parser_checks():
    results = {}

    # F43: reply_length colloquial quantifiers & verb-after-length
    f43_pos = [
        ("帮我写一段大概100字的出行提醒", (100, 100, True)),
        ("写一段差不多120字的介绍", (120, 120, True)),
        ("请写大致80字的说明", (80, 80, True)),
        ("不超过90字总结一下今天的会议", (None, 90, False)),
        ("100字左右概括一下这个方案", (100, 100, True)),
        ("用90字以内说明一下原因", (None, 90, False)),
        ("请写一段约100字的周末整理计划", (100, 100, True)),
        ("帮我用180到220字分3步说明并举个例子", (180, 220, False)),
    ]
    f43_neg = [
        "这篇文章超过90字了吗",
        "每段50字写两段",
        "大概要花100分钟",
        "“约100字”怎么翻译",
        "这篇大概100字的文章是谁写的？",
    ]
    f43_cases = []
    for text, expected in f43_pos:
        got = reply_length(text)
        got_tuple = (got.lower, got.upper, got.approximate) if got else None
        f43_cases.append({"text": text, "expected": list(expected), "actual": list(got_tuple) if got_tuple else None,
                          "ok": got_tuple == expected})
    for text in f43_neg:
        got = reply_length(text)
        f43_cases.append({"text": text, "expected": None,
                          "actual": [got.lower, got.upper, got.approximate] if got else None, "ok": got is None})
    approx_prompt = "帮我写一段大概100字的出行提醒，分3个要点"
    approx_spec = reply_length(approx_prompt)
    approx_guidance = approx_spec.guidance(approx_prompt) if approx_spec else ""
    guidance_ok = bool(approx_spec and "分 3 项" in approx_guidance and "不要少于" in approx_guidance and "不要超过" in approx_guidance)
    f43_cases.append({"text": "guidance_approx_budget_and_bounds", "ok": guidance_ok, "guidance": approx_guidance})
    results["F43_reply_length"] = {"passed": all(c["ok"] for c in f43_cases), "cases": f43_cases}

    # F44: local_calendar 把-cancel & 有没有 query
    f44_pos = [
        ("把刚才的日程取消掉", "calendar_cancel"),
        ("把刚才的日程删掉", "calendar_cancel"),
        ("将刚才安排的会议撤销掉", "calendar_cancel"),
        ("将明天上午的产品评审日程取消", "calendar_cancel"),
        ("把明天上午的产品评审日程移除掉", "calendar_cancel"),
        ("明天有没有日程", "calendar_list"),
        ("明天有没有安排", "calendar_list"),
        ("看看明天有没有日程", "calendar_list"),
        ("帮我看看后天下午有没有日程", "calendar_list"),
        ("今天还有没有日程", "calendar_list"),
    ]
    f44_neg = [
        "取消明天的日程吗？",
        "把刚才的日程取消掉吗？",
        "明天有没有日程是什么意思",
    ]
    f44_cases = []
    for text, expected in f44_pos:
        got = parse_calendar(text)
        action = got.get("action") if isinstance(got, dict) else None
        f44_cases.append({"text": text, "expected": expected, "actual": action, "ok": action == expected})
    for text in f44_neg:
        got = parse_calendar(text)
        f44_cases.append({"text": text, "expected": None, "actual": got, "ok": got is None})
    results["F44_local_calendar"] = {"passed": all(c["ok"] for c in f44_cases), "cases": f44_cases}

    # F45: assistant_timers 把-cancel & 有没有 query
    f45_pos = [
        ("看看明天有没有提醒", "list"),
        ("明天有没有闹钟", "list"),
        ("我还有没有提醒", "list"),
        ("今天下午有没有提醒", "list"),
        ("帮我查一下有没有提醒", "list"),
        ("把刚才的提醒取消掉", "cancel"),
        ("把那个闹钟关掉", "cancel"),
        ("把第1个提醒删掉", "cancel"),
        ("把1号提醒取消", "cancel"),
        ("将喝水提醒删掉", "cancel"),
    ]
    f45_neg = [
        "提醒我明天有没有开会",
        "把刚才的提醒取消掉吗？",
        "明天有没有提醒是什么意思",
    ]
    f45_cases = []
    for text, expected in f45_pos:
        got = parse_timer(text)
        action = got.get("action") if isinstance(got, dict) else None
        f45_cases.append({"text": text, "expected": expected, "actual": action, "ok": action == expected})
    for text in f45_neg:
        got = parse_timer(text)
        f45_cases.append({"text": text, "expected": None, "actual": got, "ok": got is None})
    results["F45_assistant_timers"] = {"passed": all(c["ok"] for c in f45_cases), "cases": f45_cases}

    # F46: local_overview 并/并且 & 有没有提醒/日程
    f46_pos = [
        ("查看明天日程并告诉我现在几点", ["calendar", "clock"]),
        ("电脑还有多少电，顺便看看明天有没有提醒", ["battery", "reminders"]),
        ("看看CPU并查一下可用内存", ["cpu", "memory"]),
        ("查一下系统音量并告诉我北京今天天气", ["volume", "weather"]),
        ("看看明天有没有日程，顺便查一下现在几点", ["calendar", "clock"]),
        ("帮我查一下磁盘空间并且告诉我现在几点", ["disk", "clock"]),
    ]
    f46_neg = [
        "把“查看明天日程并告诉我现在几点”翻译成英文",
        "明天日程并提醒是什么意思？",
    ]
    f46_cases = []
    for text, expected_kinds in f46_pos:
        got = parse_local_overview(text)
        kinds = [q["kind"] for q in got["queries"]] if isinstance(got, dict) else None
        f46_cases.append({"text": text, "expected": expected_kinds, "actual": kinds, "ok": kinds == expected_kinds})
    for text in f46_neg:
        got = parse_local_overview(text)
        f46_cases.append({"text": text, "expected": None, "actual": got, "ok": got is None})
    results["F46_local_overview"] = {"passed": all(c["ok"] for c in f46_cases), "cases": f46_cases}

    # F47: media_chat replay phrasing
    audio_ctx = {
        "items": [{"media_id": "m1", "title": "天空之城", "artist": "测试歌手", "kind": "audio"}],
        "selected": {"media_id": "m1", "title": "天空之城", "artist": "测试歌手", "kind": "audio"},
    }
    f47_pos = [
        "把刚才那首再放一遍",
        "重放这首歌",
        "再放一遍刚才那首歌",
        "把这首歌重播一下",
        "再来一遍这首歌",
        "再听一遍刚才的歌",
    ]
    f47_neg = [
        "别把刚才那首再放一遍",
        "不要再放一遍刚才那首歌",
        "重放这首歌是什么意思？",
        "“把刚才那首再放一遍”怎么翻译",
    ]
    f47_cases = []
    for text in f47_pos:
        got = classify(text, audio_ctx)
        ok = (isinstance(got, dict) and got.get("action") == "select" and got.get("current") is True
              and got.get("play") is True and got.get("mode") == "track")
        f47_cases.append({"text": text, "actual": got, "ok": ok})
    for text in f47_neg:
        got = classify(text, audio_ctx)
        ok = got is None or got.get("action") == "no_play"
        f47_cases.append({"text": text, "actual": got, "ok": ok})
    results["F47_media_replay"] = {"passed": all(c["ok"] for c in f47_cases), "cases": f47_cases}

    # F48: assistant_brain one_action & factual/spatial/honesty prompt rules
    f48_pos = [
        "我找不到笔了，给一个马上能做的动作。",
        "我现在有点走神，只告诉我一个现在就能做的动作。",
        "桌子上太乱了，给我一个立刻能做的简单动作，一句话就行。",
        "我现在很困，只给一个马上能做的办法",
        "帮我给一个现在就能做的具体动作，不用解释",
    ]
    f48_neg = [
        "请总结这段话并给一个动作",
        "翻译“只告诉我一个现在就能做的动作”",
        "请举例说明一个动作",
    ]
    f48_cases = []
    for text in f48_pos:
        got = wants_one_action(text)
        f48_cases.append({"text": text, "expected": True, "actual": got, "ok": got is True})
    for text in f48_neg:
        got = wants_one_action(text)
        f48_cases.append({"text": text, "expected": False, "actual": got, "ok": got is False})
    prompt_rules_ok = (
        ("未提到" in ONE_ACTION_GUIDANCE or "没有给出" in ONE_ACTION_GUIDANCE)
        and "不编造" in ONE_ACTION_GUIDANCE
        and "空间" in SYSTEM
        and "未记录" in SYSTEM
    )
    f48_cases.append({"text": "prompt_rules_grounded_spatial_honesty", "ok": prompt_rules_ok})
    results["F48_dialogue_guidance"] = {"passed": all(c["ok"] for c in f48_cases), "cases": f48_cases}

    # F49: jev_contacts colloquial reply handoff
    f49_pos = [
        ("把刚才的拟回复交给张三", "张三"),
        ("把刚才生成的回复交给张三", "张三"),
        ("把这份回复草稿交给张三", "张三"),
        ("请帮我把这条回复草稿交给“小王”", "小王"),
    ]
    f49_neg = [
        "把刚才生成的回复发给张三",
        "把这份回复草稿发送给张三",
        "不要把刚才生成的回复交给张三",
        "把这份回复草稿转发给张三",
    ]
    f49_cases = []
    for text, expected in f49_pos:
        got = handoff_alias(text)
        f49_cases.append({"text": text, "expected": expected, "actual": got, "ok": got == expected})
    for text in f49_neg:
        got = handoff_alias(text)
        f49_cases.append({"text": text, "expected": None, "actual": got, "ok": got is None})
    results["F49_jev_contact_phrasing"] = {"passed": all(c["ok"] for c in f49_cases), "cases": f49_cases}

    return results


async def unit_http_roundtrip(folder: Path):
    """End-to-end isolated HTTP verification of F44/F45/F46/F47 without touching any cloud model."""
    music = folder / "music" / "测试歌手"
    music.mkdir(parents=True)
    with wave.open(str(music / "天空之城.wav"), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(32000))
    catalog = MediaLibrary(folder / "library.sqlite3")
    catalog.configure([{"id": "fixture", "label": "隔离曲库", "kind": "local", "path": str(folder / "music")}])
    catalog.scan()

    model_hits = 0

    async def reject_model(reader, writer):
        nonlocal model_hits
        model_hits += 1
        writer.close()
        await writer.wait_closed()

    checks = {}
    env = {
        "MOSS_MEDIA_DB": str(catalog.db_path),
        "MOSS_PROVIDER_DIR": str(folder / "providers"),
        "LOCALAPPDATA": str(folder / "profile"),
        "MOSS_MEM0_ENABLED": "0",
        "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_SPEECH_PREPARE": "0",
    }
    with patch.dict(os.environ, env):
        async with await asyncio.start_server(reject_model, "127.0.0.1", 0) as sink:
            model_url = f"http://127.0.0.1:{sink.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = model_url
            moss = Moss(folder / "data", model_url, "isolated-unused", prefer_cloud=False)
            port = free_port()
            app = application(moss, lambda: "standby", folder / "data", port, "dq-unit", asyncio.Event())
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            base = f"http://127.0.0.1:{port}"
            try:
                async with aiohttp.ClientSession() as session:
                    async def post(path, payload):
                        async with session.post(base + path, json=payload) as resp:
                            return resp.status, await resp.json()

                    async def wait_done(turn_id):
                        async with asyncio.timeout(15):
                            while True:
                                async with session.get(base + "/api/state") as resp:
                                    state = await resp.json()
                                turn = next(t for t in state["turns"] if t["id"] == turn_id)
                                if turn["status"] not in {"thinking", "saving"}:
                                    return turn
                                await asyncio.sleep(0.02)

                    async def ask(text):
                        code, state = await post("/api/message", {"text": text})
                        assert code == 200, (text, code, state)
                        return await wait_done(state["turns"][-1]["id"])

                    # 1. Calendar create -> confirm -> query 有没有 -> cancel 把刚才的日程取消掉 -> confirm -> query empty
                    c_create = await ask("明天上午10点到11点安排日程：产品评审")
                    assert c_create["status"] == "pending", f"unexpected status: c_create={c_create!r} now={datetime.now().isoformat()}"
                    code, _ = await post("/api/approve", {"turn_id": c_create["id"]})
                    assert code == 200
                    c_saved = await wait_done(c_create["id"])
                    c_query1 = await ask("看看明天有没有日程")
                    c_cancel = await ask("把刚才的日程取消掉")
                    c_cancel_ok = c_cancel["status"] == "pending"
                    if c_cancel_ok:
                        code, _ = await post("/api/approve", {"turn_id": c_cancel["id"]})
                        assert code == 200
                        await wait_done(c_cancel["id"])
                    c_query2 = await ask("明天有没有日程")
                    checks["F44_calendar_http"] = {
                        "created": c_saved["status"] in {"saved", "replied"},
                        "query_found": c_query1["source"] == "calendar" and "产品评审" in (c_query1.get("reply") or ""),
                        "cancel_pending": c_cancel_ok,
                        "query_after_cancel_empty": c_query2["source"] == "calendar" and "产品评审" not in (c_query2.get("reply") or ""),
                    }
                    checks["F44_calendar_http"]["ok"] = all(checks["F44_calendar_http"].values())

                    # 2. Timer create -> query 有没有 -> cancel 把刚才的提醒取消掉 -> query empty
                    t_create = await ask("明天上午9点提醒我发周报")
                    t_query1 = await ask("看看明天有没有提醒")
                    t_cancel = await ask("把刚才的提醒取消掉")
                    t_query2 = await ask("我还有没有提醒")
                    checks["F45_timer_http"] = {
                        "created": t_create["source"] == "timer" and t_create["status"] == "replied",
                        "query_found": t_query1["source"] == "timer" and "发周报" in (t_query1.get("reply") or ""),
                        "cancelled": t_cancel["source"] == "timer" and "取消" in (t_cancel.get("reply") or ""),
                        "query_after_cancel_empty": t_query2["source"] == "timer" and "发周报" not in (t_query2.get("reply") or ""),
                    }
                    checks["F45_timer_http"]["ok"] = all(checks["F45_timer_http"].values())

                    # 3. Compound local overview with 并 and 有没有提醒
                    ov1 = await ask("查看明天日程并告诉我现在几点")
                    ov2 = await ask("电脑还有多少电，顺便看看明天有没有提醒")
                    ov1_kinds = [row.get("kind") for row in (ov1.get("local_overview") or ())]
                    ov2_kinds = [row.get("kind") for row in (ov2.get("local_overview") or ())]
                    checks["F46_overview_http"] = {
                        "calendar_and_clock": ov1["source"] == "context" and ov1["status"] == "replied" and ov1_kinds == ["calendar", "clock"],
                        "battery_and_reminders": ov2["source"] == "context" and ov2["status"] == "replied" and ov2_kinds == ["battery", "reminders"],
                    }
                    checks["F46_overview_http"]["ok"] = all(checks["F46_overview_http"].values())

                    # 4. Media play then colloquial replay
                    m1 = await ask("播放天空之城")
                    m2 = await ask("把刚才那首再放一遍")
                    checks["F47_media_replay_http"] = {
                        "initial_played": (m1.get("media") or {}).get("selected", {}).get("title") == "天空之城",
                        "replayed": (m2.get("media") or {}).get("selected", {}).get("title") == "天空之城"
                                    and bool((m2.get("media") or {}).get("autoplay")),
                        "reply": m2.get("reply"),
                    }
                    checks["F47_media_replay_http"]["ok"] = checks["F47_media_replay_http"]["initial_played"] and checks["F47_media_replay_http"]["replayed"]

                    checks["model_connections"] = model_hits
            finally:
                await runner.cleanup()
                await moss.close()
    return checks


async def run_live(folder: Path, provider: str):
    """Real cloud-model dialogue quality verification via isolated MOSS HTTP API."""
    catalog = MediaLibrary(folder / "library.sqlite3")
    env = {
        "MOSS_MEDIA_DB": str(catalog.db_path),
        "MOSS_MEM0_ENABLED": "0",
        "MOSS_WINDOWS_REMINDERS": "0",
        "MOSS_SPEECH_PREPARE": "0",
    }
    cases = []
    with patch.dict(os.environ, env):
        moss = Moss(folder / "data", "http://127.0.0.1:1/v1", "unused-local", prefer_cloud=True)
        port = free_port()
        app = application(moss, lambda: "standby", folder / "data", port, "dq-live", asyncio.Event())
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(base + "/api/models") as resp:
                    models_info = await resp.json()
                available = [m["id"] for m in models_info.get("models", []) if m.get("available")]
                selected = provider if provider in available else next(
                    (m for m in ("aliyun", "deepseek", "kimi") if m in available), None
                )
                if not selected:
                    raise RuntimeError(f"没有可用的云端模型，当前可用：{available}")
                async with session.post(base + "/api/select-model", json={"id": selected}) as resp:
                    assert resp.status == 200
                    sel_state = await resp.json()
                model_name = sel_state.get("model") or sel_state.get("model_name")

                async def fresh():
                    async with session.post(base + "/api/new-chat", json={}) as resp:
                        assert resp.status == 200

                async def ask(text):
                    started = perf_counter()
                    async with session.post(base + "/api/message", json={"text": text}) as resp:
                        assert resp.status == 200, await resp.text()
                        state = await resp.json()
                    turn_id = state["turns"][-1]["id"]
                    async with asyncio.timeout(90):
                        while True:
                            async with session.get(base + "/api/state") as resp:
                                state = await resp.json()
                            turn = next(t for t in state["turns"] if t["id"] == turn_id)
                            if turn["status"] not in {"thinking", "saving"}:
                                return turn, round((perf_counter() - started) * 1000, 1)
                            await asyncio.sleep(0.15)

                # Case 1 & 2: 180-220 chars, 3 steps + 1 example (2 repeats)
                p_range = "请用180到220字写一份睡前整理书桌的方案，分3个步骤，并举一个具体例子。只输出正文，不要标题。"
                for repeat in (1, 2):
                    await fresh()
                    turn, ms = await ask(p_range)
                    reply = turn.get("reply") or ""
                    n = visible_chars(reply)
                    no_md = not re.search(r"[#*_`]", reply)
                    ok = turn["status"] == "replied" and 180 <= n <= 220 and no_md
                    cases.append({"case": f"range_180_220_r{repeat}", "prompt": p_range, "reply": reply,
                                  "visible_chars": n, "no_markdown": no_md, "ms": ms, "ok": ok})

                # Case 3 & 4: 約 100 字 (2 repeats, must stay within 85..115 and check 90..110)
                p_approx = "请写一段约100字的周末早晨散步建议，只输出正文。"
                for repeat in (1, 2):
                    await fresh()
                    turn, ms = await ask(p_approx)
                    reply = turn.get("reply") or ""
                    n = visible_chars(reply)
                    no_md = not re.search(r"[#*_`]", reply)
                    ok = turn["status"] == "replied" and 85 <= n <= 115 and no_md
                    cases.append({"case": f"approx_100_r{repeat}", "prompt": p_approx, "reply": reply,
                                  "visible_chars": n, "strict_90_110": 90 <= n <= 110, "no_markdown": no_md, "ms": ms, "ok": ok})

                # Case 5: F43 colloquial 大概100字
                p_f43_approx = "帮我写一段大概100字的雨天出门提醒，只输出正文。"
                await fresh()
                turn, ms = await ask(p_f43_approx)
                reply = turn.get("reply") or ""
                n = visible_chars(reply)
                ok = turn["status"] == "replied" and 85 <= n <= 115 and not re.search(r"[#*_`]", reply)
                cases.append({"case": "F43_colloquial_approx_100", "prompt": p_f43_approx, "reply": reply,
                              "visible_chars": n, "ms": ms, "ok": ok})

                # Case 6: F43 verb-after-length 不超过90字总结一下
                p_f43_cap = "不超过90字总结一下：今天上午完成了语音首声优化、高负载取消竞态修复和手势切换机位接入，下午继续跑全量回归并准备上线。"
                await fresh()
                turn, ms = await ask(p_f43_cap)
                reply = turn.get("reply") or ""
                n = visible_chars(reply)
                ok = turn["status"] == "replied" and 15 <= n <= 90 and not re.search(r"[#*_`]", reply)
                cases.append({"case": "F43_cap_90_summary", "prompt": p_f43_cap, "reply": reply,
                              "visible_chars": n, "ms": ms, "ok": ok})

                # Case 7: F48 One action, zero hallucinated unstated items
                p_one = "我找不到笔了，只告诉我一个现在就能做的动作。"
                await fresh()
                turn, ms = await ask(p_one)
                reply = turn.get("reply") or ""
                banned = [w for w in ("水杯", "杯子", "抽屉", "书包", "笔筒", "键盘", "笔记本", "手机", "外套", "口袋") if w in reply]
                sc = sentence_count(reply)
                ok = turn["status"] == "replied" and sc == 1 and not banned and visible_chars(reply) <= 55
                cases.append({"case": "F48_one_action_no_hallucination", "prompt": p_one, "reply": reply,
                              "sentences": sc, "hallucinated_items": banned, "visible_chars": visible_chars(reply),
                              "ms": ms, "ok": ok})

                # Case 8: Spatial consistency
                p_spatial = "我面对北墙站着，桌上有台灯在左手边、水杯在右手边。如果我向后转面向南墙，台灯和水杯分别在我的哪一边？请一句话回答。"
                await fresh()
                turn, ms = await ask(p_spatial)
                reply = turn.get("reply") or ""
                sc = sentence_count(reply)
                lamp_right = bool(re.search(r"台灯.{0,8}右", reply))
                cup_left = bool(re.search(r"水杯.{0,8}左", reply))
                ok = turn["status"] == "replied" and sc <= 2 and lamp_right and cup_left
                cases.append({"case": "spatial_consistency", "prompt": p_spatial, "reply": reply,
                              "sentences": sc, "lamp_right": lamp_right, "cup_left": cup_left, "ms": ms, "ok": ok})

                # Case 9: Unrecorded preference honesty (empty SQLite memory)
                p_honest = "按我之前告诉你的咖啡偏好，今天下午该喝什么？"
                await fresh()
                turn, ms = await ask(p_honest)
                reply = turn.get("reply") or ""
                admits = bool(re.search(r"没(?:有)?(?:记录|记下|告诉|保存|查到|留下)|不确定|不清楚|不知道|还未|尚未|还没有", reply))
                cases.append({"case": "unrecorded_preference_honesty", "prompt": p_honest, "reply": reply,
                              "admits_missing": admits, "ms": ms, "ok": turn["status"] == "replied" and admits})

                # Case 10: Grounded memory recall
                await fresh()
                mem_save, _ = await ask("记住：我平时喝黑咖啡不加糖。")
                await fresh()
                p_mem = "按我记录的咖啡偏好，今天下午该喝什么？一句话告诉我。"
                turn, ms = await ask(p_mem)
                reply = turn.get("reply") or ""
                ok = (mem_save["source"] == "memory" and turn["status"] == "replied"
                      and "黑咖啡" in reply and "不加糖" in reply)
                cases.append({"case": "grounded_memory_recall", "prompt": p_mem, "reply": reply,
                              "memory_saved": mem_save["source"] == "memory", "ms": ms, "ok": ok})

                return {"provider": selected, "model_name": model_name, "available_models": available,
                        "passed": all(c["ok"] for c in cases),
                        "passed_count": sum(c["ok"] for c in cases), "total": len(cases), "cases": cases}
        finally:
            await runner.cleanup()
            await moss.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("unit", "live"), default="unit")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--provider", default="aliyun")
    args = parser.parse_args()

    tag = f"dialogue-quality-{args.mode}" + ("-baseline" if args.baseline else "-candidate")
    folder = ROOT / "runs" / datetime.now().strftime(f"{tag}-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    result_path = folder / "result.json"
    report = {"mode": args.mode, "baseline": args.baseline, "passed": False}

    if args.mode == "unit":
        parsers = unit_parser_checks()
        http = await unit_http_roundtrip(folder)
        report["parsers"] = parsers
        report["http"] = http
        report["passed"] = (
            all(section["passed"] for section in parsers.values())
            and all(v["ok"] for k, v in http.items() if isinstance(v, dict))
            and http["model_connections"] == 0
        )
    else:
        live = await run_live(folder, args.provider)
        report.update(live)

    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result_path, "passed=", report["passed"])
    if args.mode == "unit":
        for name, sec in report["parsers"].items():
            print(f"  {name}: passed={sec['passed']} ({sum(c['ok'] for c in sec['cases'])}/{len(sec['cases'])})")
        for name, sec in report["http"].items():
            if isinstance(sec, dict):
                print(f"  http.{name}: ok={sec['ok']}")
        print(f"  model_connections={report['http']['model_connections']}")
    else:
        print(f"  provider={report['provider']} model={report['model_name']} ({report['passed_count']}/{report['total']})")
        for c in report["cases"]:
            print(f"  [{ 'PASS' if c['ok'] else 'FAIL' }] {c['case']} chars={c.get('visible_chars')} -> {c['reply']!r}")
    if not args.baseline and not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

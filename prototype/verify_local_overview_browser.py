import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

from cn2an import an2cn
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated
from verify_local_overview import QUESTION
from verify_memory import new_chat, send
from verify_speech_long_reading import PROBE


UI_PROBE = """
if (localStorage.getItem("moss.voice.enabled") === null) localStorage.setItem("moss.voice.enabled", "on");
window.statusOpenEvents = [];
window.consoleSpeechPhases = [];
window.panelSpeechPhases = [];
window.firstCompleteReply = null;
new MutationObserver(() => {
  if (firstCompleteReply) return;
  const row = document.querySelector('[data-testid="turn"] [data-testid="final"]');
  const text = row?.textContent.trim();
  if (text && !row.hidden && row.getClientRects().length)
    window.firstCompleteReply = {text,at:performance.now()};
}).observe(document, {subtree:true, childList:true, characterData:true, attributes:true,
                      attributeFilter:["hidden", "data-open"]});
for (const event of ["pending","start","end"]) {
  window.addEventListener("moss-speech-"+event, () => queueMicrotask(() => {
    consoleSpeechPhases.push({event,label:document.querySelector("#console-phase-label")?.textContent});
    const panel = document.querySelector("#local-status-panel");
    const label = panel?.querySelector("#local-status-progress");
    if (panel?.matches(":modal") && label && !label.hidden)
      panelSpeechPhases.push({event,label:label.textContent});
  }));
}
new MutationObserver(records => {
  for (const record of records) {
    if (record.target.id === "local-status-panel" && record.oldValue === null)
      statusOpenEvents.push(performance.now());
  }
}).observe(document, {subtree:true, attributes:true, attributeOldValue:true, attributeFilter:["open"]});
"""

AUDIO_RESUME_GATE = """
window.audioResumeGate = {requested:null, released:null, ready:null};
const allowResume = new Promise(resolve => {
  window.releaseAudioResume = () => {
    audioResumeGate.released = performance.now();
    resolve();
  };
});
window.AudioContext = class extends window.AudioContext {
  get state() { return this.resumeFinished ? super.state : "suspended"; }
  async resume() {
    audioResumeGate.requested ??= performance.now();
    await Promise.all([super.resume(), allowResume]);
    this.resumeFinished = true;
    audioResumeGate.ready = performance.now();
  }
};
"""


TYPED_PREPARATION_PROBE = """
window.typedPreparation = {ready:false, edits:[], calls:[]};
window.addEventListener('moss-agent-state', event => {
  if (event.detail.speech_prepare) typedPreparation.ready = true;
});
for (const type of ['input','compositionend']) document.addEventListener(type, event => {
  if (event.target.id !== 'message') return;
  typedPreparation.edits.push({type, at:performance.now(), trusted:event.isTrusted,
    composing:event.isComposing ?? null, inputType:event.inputType ?? null});
}, true);
const typedOriginalFetch = window.fetch;
window.fetch = function(url, options) {
  if (['/api/prepare-speech','/api/message','/api/speak','/api/speak-batch'].includes(url))
    typedPreparation.calls.push({url, at:performance.now(), hidden:document.hidden,
      hasFocus:document.hasFocus(), focused:document.activeElement?.id});
  return typedOriginalFetch.apply(this, arguments);
};
"""


def spoken_words(text):
    return "".join(c for c in text if c.isalnum() or c == "%")


def expected_speech(items):
    parts = []
    for item in items:
        reply = item["reply"]
        if item["kind"] == "clock":
            hours, minutes, _ = map(int, re.search(r"(\d\d):(\d\d):(\d\d)", reply).groups())
            period = next(name for end, name in ((6, "凌晨"), (12, "上午"), (13, "中午"), (18, "下午"), (24, "晚上"))
                          if hours < end)
            hour = hours if hours <= 12 else hours - 12
            minute = ("零" if minutes < 10 else "") + an2cn(minutes) + "分" if minutes else ""
            reply = f"现在{period}{'两' if hour == 2 else an2cn(hour)}点{minute}。"
        elif item["kind"] == "battery":
            reply = re.sub(r"^电量 (\d{1,3}%)（([^）]+)）。$", r"电量\1，\2。", reply)
        elif item["kind"] == "volume":
            reply = reply.replace("系统音量为 ", "系统音量").replace("，当前", "，")
        parts.append(reply)
    return spoken_words("".join(parts))


def expected_reading(item):
    kind, reply = item["kind"], item["reply"]
    if kind == "clock":
        clock = re.search(r"\d\d:\d\d:\d\d", reply)
        assert clock, reply
        return clock[0], reply[:clock.start()].rstrip("，, ")
    percent = re.search(r"\d+%", reply)
    if not percent:
        return reply, ""
    return percent[0], reply[percent.end():].strip("，,。．. ()（）")


async def inspect_panel(page, turn):
    panel = page.get_by_test_id("local-status-panel")
    await panel.wait_for()
    assert await panel.evaluate("node => node.tagName === 'DIALOG' && node.matches(':modal') && node.open")
    rows = panel.locator("[data-reading-kind]")
    assert await rows.count() == len(turn["local_overview"])
    actual = []
    for index, item in enumerate(turn["local_overview"]):
        row = rows.nth(index)
        kind = await row.get_attribute("data-reading-kind")
        value = await row.locator(".status-reading-value").inner_text()
        details = await row.locator(".status-reading-detail").all_inner_texts()
        assert (kind, value, "".join(details)) == (item["kind"], *expected_reading(item)), (item, value, details)
        actual.append({"kind": kind, "value": value, "detail": "".join(details)})
    assert [r["kind"] for r in actual] == ["clock", "battery", "volume"], actual
    return actual


async def check_typed_preparation(context, server, report):
    cases = (
        ("paused_typing", True, True), ("reviewed_bulk_input", True, False), ("immediate_bulk_submit", False, True),
        ("replace_before_debounce", False, False), ("synthetic_input", False, False),
        ("voice_disabled", False, False), ("hidden_before_debounce", False, False),
        ("blur_before_debounce", False, False), ("pending_draft", False, False), ("ime_commit", True, False),
    )

    async def settled(page):
        await page.wait_for_timeout(750)
        return await page.evaluate("typedPreparation")

    for name, should_prepare, should_submit in cases:
        page = await context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        await page.add_init_script(TYPED_PREPARATION_PROBE)
        await page.add_init_script("localStorage.setItem('moss.voice.enabled', '" +
                                   ("off" if name == "voice_disabled" else "on") + "')")
        try:
            await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for(state="attached")
            await page.wait_for_function("typedPreparation.ready && document.hasFocus() && !document.hidden")
            composer = page.get_by_test_id("composer")
            await composer.click()
            before = len(report["prepare_requests"])
            if name == "pending_draft":
                state = await server.state()
                state["turns"].append({"id":"typed-preparation-pending", "status":"pending", "user":"待确认的草稿"})
                await page.evaluate("state => window.mossSpeech.observe(state)", state)
            if name == "paused_typing":
                await page.keyboard.insert_text("现在几")
                assert not (await settled(page))["calls"]
                await page.keyboard.insert_text("点")
            elif name == "synthetic_input":
                await composer.evaluate("""(node, text) => {
                    node.value = text;
                    node.dispatchEvent(new Event('input', {bubbles:true}));
                    node.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true, data:text}));
                }""", QUESTION)
            elif name == "ime_commit":
                cdp = await context.new_cdp_session(page)
                await cdp.send("Input.imeSetComposition", {"text":QUESTION, "selectionStart":len(QUESTION), "selectionEnd":len(QUESTION)})
                assert not (await settled(page))["calls"]
                await page.keyboard.insert_text(QUESTION)
                assert await composer.input_value() == QUESTION
                await cdp.detach()
            else:
                await page.keyboard.insert_text(QUESTION)
            if name == "replace_before_debounce":
                await composer.press("Control+a")
                await page.keyboard.insert_text("把音量调到30%")
            elif name == "blur_before_debounce":
                await composer.evaluate("node => node.blur()")
            elif name == "hidden_before_debounce":
                await page.evaluate("""() => {
                    Object.defineProperty(document, 'hidden', {get:() => true, configurable:true});
                    Object.defineProperty(document, 'visibilityState', {get:() => 'hidden', configurable:true});
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
            elif name == "immediate_bulk_submit":
                await composer.press("Enter")

            trace = await settled(page)
            preflight = [call for call in trace["calls"] if call["url"] == "/api/prepare-speech"]
            assert len(preflight) == int(should_prepare), (name, trace)
            assert trace["edits"] and any(edit["trusted"] for edit in trace["edits"]) == (name != "synthetic_input"), trace
            assert report["prepare_requests"][before:] == ([{}] if should_prepare else []), (name, report["prepare_requests"])
            assert all(call["hasFocus"] and not call["hidden"] and call["focused"] == "message" for call in preflight), preflight
            if name == "ime_commit":
                assert any(edit["type"] == "input" and edit["trusted"] and edit["composing"] for edit in trace["edits"]), trace
                assert trace["edits"][-1]["type"] == "compositionend", trace
            if name != "immediate_bulk_submit":
                assert trace["calls"] == preflight, (name, trace)
            if name == "paused_typing":
                await page.keyboard.insert_text("、电脑还有多少电、音量多大？")
                await composer.press("Enter")
            if should_submit:
                await page.locator('#local-status-panel[data-phase="complete"]').wait_for()
                turn = (await server.state())["turns"][-1]
                assert turn["user"] == QUESTION and turn["cloud_requests"]["total"] == 0, turn
                await inspect_panel(page, turn)
                await page.wait_for_function("collectReadingMetrics().events.some(e => e.type === 'end')")
                trace = await page.evaluate("typedPreparation")
                assert sum(call["url"] == "/api/message" for call in trace["calls"]) == 1, trace
                assert sum(call["url"] == "/api/prepare-speech" for call in trace["calls"]) == int(should_prepare), trace
                assert any(call["url"] == "/api/speak" for call in trace["calls"]), trace
            report["typed_cases"][name] = trace
        finally:
            await page.close()


async def verify(real_speech, gate_audio_resume=False, typed_preparation=False):
    folder = ROOT / "runs" / datetime.now().strftime("local-overview-browser-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    report = {"passed": False, "mode": "真实首轮合成" if real_speech else "已有语音文件回放",
              "model_connections": 0, "speech_requests": [], "speech_responses": [],
              "outside_requests": [], "page_errors": []}
    if typed_preparation:
        report.update(mode="键入本机读数的预导入边界", prepare_requests=[], typed_cases={})
    result = folder / "result.json"
    print(result, flush=True)
    catalog = folder / "空索引.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"),
                      LOCALAPPDATA=str(folder / "profile"), MOSS_MEM0_ENABLED="0", MOSS_WINDOWS_REMINDERS="0",
                      MOSS_SPEECH_PREPARE="1" if typed_preparation else "0", MOSS_SPEECH_STANDBY="0")
    server = isolated(folder / "app")
    fixture = (ROOT / "voice" / "lines" / "hello.wav").read_bytes()
    if not real_speech:
        report["fixture"] = {"name": "voice/lines/hello.wav", "sha256": hashlib.sha256(fixture).hexdigest()}
    expect_speech = True

    async def model_guard(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async def route_request(route):
        url = urlsplit(route.request.url)
        if url.scheme not in {"http", "https"}:
            await route.continue_()
            return
        if url.hostname != "127.0.0.1" or url.port != server.port:
            report["outside_requests"].append({"host": url.hostname, "path": url.path})
            await route.abort()
            return
        if typed_preparation and url.path == "/api/prepare-speech":
            report["prepare_requests"].append(route.request.post_data_json)
            await route.fulfill(status=204)
            return
        if url.path not in {"/api/speak", "/api/speak-batch"}:
            await route.continue_()
            return
        payload = route.request.post_data_json
        report["speech_requests"].append({"path": url.path, "payload": payload, "enabled_round": expect_speech})
        if not expect_speech:
            await route.abort()
        elif real_speech:
            await route.continue_()
        elif url.path == "/api/speak":
            await route.fulfill(status=200, content_type="audio/wav", body=fixture)
        else:
            await route.fulfill(status=405, content_type="application/json", body='{"error":"单段文件验收"}')

    async def inspect_turn(page):
        turn = (await server.state())["turns"][-1]
        assert turn["source"] == "context" and turn["status"] == "replied", turn
        assert turn["cloud_requests"]["total"] == report["model_connections"] == 0
        row = page.locator(f'[data-turn-id="{turn["id"]}"]')
        assert (await row.get_by_test_id("final").inner_text()).strip() == turn["reply"].strip()
        assert await row.get_by_test_id("open-local-status").count() == 1
        return turn, row

    try:
        async with await asyncio.start_server(model_guard, "127.0.0.1", 0) as guard:
            server.model_url = f"http://127.0.0.1:{guard.sockets[0].getsockname()[1]}/v1"
            for _, prefixes, _, _ in SPECS.values():
                for prefix in prefixes:
                    os.environ[prefix + "_BASE_URL"] = server.model_url
            await server.start()
            async with async_playwright() as playwright:
                browser = await open_browser(playwright)
                try:
                    await confirm_muted_edge(browser)
                    context = await browser.new_context(viewport={"width": 1280, "height": 850},
                                                        reduced_motion="reduce", service_workers="block")
                    await context.route("**/*", route_request)
                    await context.add_init_script(PROBE + UI_PROBE + (AUDIO_RESUME_GATE if gate_audio_resume else ""))
                    if typed_preparation:
                        await check_typed_preparation(context, server, report)
                        assert not report["outside_requests"] and not report["page_errors"] and not report["model_connections"], report
                        report["passed"] = True
                        return
                    page = await context.new_page()
                    page.set_default_timeout(10000)
                    responses = []
                    page.on("response", lambda response: responses.append(response)
                            if urlsplit(response.url).path in {"/api/speak", "/api/speak-batch"} else None)
                    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
                    await page.locator(".connection.online").wait_for()
                    original_chat = (await server.state())["chat_id"]
                    await send(page, server, QUESTION)
                    if gate_audio_resume:
                        held = await page.evaluate("""() => ({
                            ...audioResumeGate, events:collectReadingMetrics().events.map(event => event.type)
                        })""")
                        report["audio_resume_before_release"] = held
                        assert held["requested"] is not None and held["ready"] is None, held
                        assert held["events"] == ["pending"] and not report["speech_requests"], held
                        status_label = page.locator("#local-status-panel:modal #local-status-progress")
                        assert await status_label.inner_text(timeout=2000) == "正在准备播报"
                        await page.evaluate("releaseAudioResume()")
                    first, row = await inspect_turn(page)
                    report["api_readings"] = first["local_overview"]
                    report["first_panel"] = await inspect_panel(page, first)
                    opened = await page.evaluate("statusOpenEvents")
                    assert len(opened) == 1, opened
                    await page.screenshot(path=folder / "桌面状态面板.png", animations="disabled")
                    await page.get_by_test_id("local-status-panel").screenshot(path=folder / "本机状态弹窗.png", animations="disabled")
                    await page.wait_for_function("readingFinished()", timeout=180000 if real_speech else 15000)
                    trace = await page.evaluate("collectReadingMetrics()")
                    complete = await page.evaluate("firstCompleteReply")
                    assert complete and complete["text"] == first["reply"].strip(), complete
                    starts = [event for event in trace["events"] if event["type"] == "start" and event["id"] == first["id"]]
                    assert starts and all(event["type"] != "error" for event in trace["events"]), trace["events"]
                    if gate_audio_resume:
                        resumed = report["audio_resume_gate"] = await page.evaluate("audioResumeGate")
                        assert resumed["requested"] < resumed["released"] <= resumed["ready"] <= starts[0]["at"], resumed
                    active = [clip for clip in trace["clips"] if clip.get("first") is not None]
                    assert active, trace["clips"]
                    clip = active[0]
                    clock = clip["output"]
                    native_clock = 0 < clock["performanceTime"] and clock["contextTime"] <= clip["start"]
                    pcm_at = ((clip["first"] - clock["contextTime"]) * 1000 + clock["performanceTime"] if native_clock
                              else (clip["first"] - clip["start"]) * 1000 + clip["scheduled"])
                    assert responses and all(response.ok for response in responses), [r.status for r in responses]
                    for index, response in enumerate(responses, 1):
                        raw = await response.body()
                        path = urlsplit(response.url).path
                        if path == "/api/speak":
                            assert response.headers.get("content-type", "").startswith("audio/wav") and raw[:4] == b"RIFF"
                            if not real_speech:
                                assert raw == fixture
                            else:
                                (folder / f"状态播报-{index}.wav").write_bytes(raw)
                        stages = dict((name, float(value)) for name, value in
                                      re.findall(r"([a-z_]+);dur=([\d.]+)", response.headers.get("server-timing", "")))
                        if real_speech and path == "/api/speak":
                            assert all(name in stages for name in ("tts", "spawn", "imports", "load", "generate", "mix")), stages
                        report["speech_responses"].append({"path": path, "status": response.status, "bytes": len(raw),
                                                           "sha256": hashlib.sha256(raw).hexdigest(), "server_timing_ms": stages})
                    texts = []
                    for request in report["speech_requests"]:
                        payload = request["payload"]
                        texts.extend(payload["texts"] if "texts" in payload else [payload["text"]])
                    assert spoken_words("".join(texts)) == expected_speech(first["local_overview"]), (texts, first["reply"])
                    phases = await page.evaluate("consoleSpeechPhases")
                    for event, label in [("pending", "正在准备播报"), ("start", "正在播报"), ("end", "待机")]:
                        assert {"event": event, "label": label} in phases, phases
                    panel_phases = report["panel_speech_phases"] = await page.evaluate("panelSpeechPhases")
                    for event, label in [("pending", "正在准备播报"), ("start", "正在播报"), ("end", "读取完成")]:
                        assert {"event": event, "label": label} in panel_phases, panel_phases
                    report["speech"] = {"payload_matches_reply": True, "start_events": len(starts),
                                        "decoded_active_clips": len(active), "native_output_clock": native_clock,
                                        "observed_phases": phases,
                                        "complete_text_visible_ms": round(complete["at"] - trace["submitted"], 1),
                                        "first_pcm_ms": round(pcm_at - trace["submitted"], 1),
                                        "start_event_ms": round(starts[0]["at"] - trace["submitted"], 1),
                                        "panel_ms": round(opened[0] - trace["submitted"], 1)}
                    if not real_speech and not gate_audio_resume:
                        requests_before = len(report["speech_requests"])
                        await page.evaluate("""async () => {
                          const entry = [...document.scripts].find(s=>s.src.includes('/static/agent-terminal.js'));
                          await (await import(entry.src)).submitRecognizedText('再念一遍');
                        }""")
                        repeated = page.get_by_test_id("turn").last.get_by_test_id("final")
                        await page.get_by_test_id("turn").nth(1).get_by_test_id("final").wait_for()
                        second = (await server.state())["turns"][-1]
                        assert second["source"] == "readback" and second["readback_of"] == first["id"], second
                        assert second["cloud_requests"]["total"] == 0 and second["reply"] == first["reply"]
                        assert (await repeated.inner_text()).strip() == first["reply"].strip()
                        await page.wait_for_function("""id=>collectReadingMetrics().events.some(
                            e=>e.id===id&&e.type==='end')""", arg=second["id"])
                        repeated_text = []
                        for request in report["speech_requests"][requests_before:]:
                            payload = request["payload"]
                            repeated_text.extend(payload["texts"] if "texts" in payload else [payload["text"]])
                        assert spoken_words("".join(repeated_text)) == expected_speech(first["local_overview"])
                        assert await inspect_panel(page, first) == report["first_panel"]
                        assert len(await page.evaluate("statusOpenEvents")) == 1
                        report["spoken_repeat"] = {"exact_saved_reply":True, "correct_clock_pronunciation":True,
                                                   "new_popup":False, "tts_fixture":True}
                    expect_speech = False
                    panel = page.get_by_test_id("local-status-panel")
                    close = panel.get_by_role("button", name="关闭", exact=True)
                    await page.keyboard.press("Escape")
                    await panel.wait_for(state="hidden")
                    assert await page.get_by_test_id("composer").is_visible()
                    assert await page.locator("#dialogue").get_attribute("data-open") is not None
                    await row.get_by_test_id("open-local-status").click()
                    assert await inspect_panel(page, first) == report["first_panel"]
                    await close.click()
                    await panel.wait_for(state="hidden")
                    assert await row.get_by_test_id("open-local-status").evaluate("el => document.activeElement === el")
                    report["escape_kept_chat_and_manual_reopen"] = True

                    opens_before = len(await page.evaluate("statusOpenEvents"))
                    previous_pid = server.pid
                    saved = [(turn["id"], turn["reply"]) for turn in (await server.state())["turns"]]
                    await server.stop()
                    await page.locator(".connection:not(.online)").wait_for()
                    await server.start()
                    await page.locator(".connection.online").wait_for()
                    restored = [(turn["id"], turn["reply"]) for turn in (await server.state())["turns"]]
                    assert server.pid != previous_pid and restored == saved
                    assert not await panel.is_visible() and len(await page.evaluate("statusOpenEvents")) == opens_before
                    report["reconnected_pids"] = [previous_pid, server.pid]
                    await page.reload(wait_until="domcontentloaded")
                    await page.locator(".connection.online").wait_for()
                    await row.get_by_test_id("open-local-status").wait_for()
                    assert not await panel.is_visible() and not await page.evaluate("statusOpenEvents")
                    await new_chat(page, server)
                    await page.get_by_test_id("history").click()
                    await page.locator(f'[data-testid="chat-item"][data-chat-id="{original_chat}"]').click()
                    await row.get_by_test_id("open-local-status").wait_for()
                    assert not await panel.is_visible() and not await page.evaluate("statusOpenEvents")
                    report["reload_and_chat_switch_without_popup"] = True

                    await page.locator(".preferences summary").click()
                    await page.get_by_role("checkbox", name="MOSS 语音播报").uncheck()
                    await page.locator(".preferences summary").click()
                    assert await page.evaluate("localStorage.getItem('moss.voice.enabled')") == "off"
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await send(page, server, QUESTION)
                    second, _ = await inspect_turn(page)
                    assert second["id"] != first["id"]
                    report["muted_mobile_panel"] = await inspect_panel(page, second)
                    bounds = await panel.evaluate("""el => {
                        const rect = el.getBoundingClientRect();
                        return {x:rect.x, y:rect.y, right:rect.right, bottom:rect.bottom,
                                view:[innerWidth, innerHeight], client:el.clientWidth, scroll:el.scrollWidth,
                                rows:[...el.querySelectorAll('[data-reading-kind]')].map(row => {
                                    const r = row.getBoundingClientRect();
                                    return [r.left,r.right,r.top,r.bottom,row.clientWidth,row.scrollWidth];
                                })};
                    }""")
                    assert bounds["x"] >= 0 and bounds["y"] >= 0 and bounds["right"] <= bounds["view"][0] + 1
                    assert bounds["bottom"] <= bounds["view"][1] + 1 and bounds["scroll"] <= bounds["client"] + 1
                    assert all(bounds["x"] <= left < right <= bounds["right"] + 1 and
                               bounds["y"] <= top < bottom <= bounds["bottom"] + 1 and scroll <= client + 1
                               for left, right, top, bottom, client, scroll in bounds["rows"]), bounds
                    assert not await page.evaluate("collectReadingMetrics().events.some(e=>e.type==='pending'||e.type==='start')")
                    assert all(request["enabled_round"] for request in report["speech_requests"])
                    report["mobile_bounds"] = bounds
                    await page.screenshot(path=folder / "窄屏状态面板.png", animations="disabled")
                    assert not report["outside_requests"] and not report["page_errors"], report
                    assert report["model_connections"] == 0
                    report["passed"] = True
                finally:
                    await browser.close()
    except Exception as error:
        report["error"] = repr(error)
        raise
    finally:
        try:
            await server.stop()
        finally:
            result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-speech", action="store_true", help="首轮使用真实本机合成")
    parser.add_argument("--gate-audio-resume", action="store_true", help="读数完成后再放行浏览器的音频启动")
    parser.add_argument("--typed-preparation", action="store_true", help="固定音频下检查键入本机问句的预导入边界")
    args = parser.parse_args()
    if args.typed_preparation and (args.real_speech or args.gate_audio_resume):
        parser.error("键入边界验收使用固定音频，不与语音冷启动或音频门限验收合跑")
    asyncio.run(verify(args.real_speech, args.gate_audio_resume, args.typed_preparation))

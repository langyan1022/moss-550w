import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
import wave

import aiohttp
import numpy as np
from playwright.async_api import async_playwright, expect
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from local_media.library import MediaLibrary
from verify_context_reply import free_port
from verify_wake_activation import DRAFT, MARKER, rejected
from windows_reminders import WindowsReminders


FIXTURES = ROOT / "runs/voice-wake-20261121-080850-467228"
ORIGIN_OPT_IN = "moss.voice.nativeNext"
SPEECH = "你在吗"
PROBE = """
if (location.protocol === 'http:' && navigator.mediaDevices?.getUserMedia) {
localStorage.setItem('moss.voice.enabled', 'on');
window.nativeWakeQA = {attempts:[], tracks:[], requests:[], speech:[]};
for (const kind of ['pending','start','end','error','stop','skipped']) {
  window.addEventListener('moss-speech-'+kind, ({detail}) => {
    nativeWakeQA.speech.push({kind, id:detail?.id, text:detail?.text, at:performance.now(),
      live:nativeWakeQA.tracks.filter(track=>track.readyState==='live').length});
  });
}
const getAudio = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async constraints => {
  const probe = nativeWakeQA;
  probe.attempts.push({at:performance.now(), visible:!document.hidden,
    focused:document.hasFocus(), activated:navigator.userActivation.hasBeenActive});
  const stream = await getAudio(constraints);
  probe.tracks.push(...stream.getAudioTracks());
  return stream;
};
const original = window.fetch;
window.fetch = async function(url, options) {
  const path = new URL(url, location.href).pathname;
  let call;
  if (path.startsWith('/api/wake/') || ['/api/prepare-voice','/api/transcribe','/api/message','/api/speak'].includes(path)) {
    call = {path, at:performance.now(), live:nativeWakeQA.tracks.filter(t=>t.readyState==='live').length};
    if (path === '/api/message') call.text = JSON.parse(options.body).text;
    if (path === '/api/speak') call.spoken = JSON.parse(options.body);
    nativeWakeQA.requests.push(call);
  }
  const response = await original.apply(this, arguments);
  if (call) {
    call.status = response.status;
    call.finished = performance.now();
    if (path === '/api/transcribe') call.transcript = (await response.clone().json()).text;
    if (path === '/api/wake/claim' && response.ok) call.listen_next = (await response.clone().json()).listen_next;
    if (path === '/api/speak' && response.ok) {
      call.server_timing = response.headers.get('Server-Timing');
      const buffer = await response.clone().arrayBuffer();
      const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', buffer));
      call.sha256 = Array.from(digest, byte=>byte.toString(16).padStart(2,'0')).join('');
    }
  }
  return response;
};
}
"""


def command_wav(folder):
    source = FIXTURES / "command.wav"
    with wave.open(str(source), "rb") as clip:
        assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) == (1, 2, 16000)
        command = np.frombuffer(clip.readframes(clip.getnframes()), dtype="<i2")
    samples = np.zeros(8 * 16000, dtype="<i2")
    start = 14400
    samples[start:start + len(command)] = np.rint(command * .05).astype("<i2")
    name = folder / "command-microphone.wav"
    with wave.open(str(name), "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(samples.tobytes())
    return name


async def recognize_native_wav(folder):
    sys.path.insert(0, str(ROOT.parent / "alternatives/native-wake"))
    from build_recognizer import recognizer
    from verify_candidate import convert

    binary = await asyncio.to_thread(recognizer)
    sample = folder / "native-wake.wav"
    convert(FIXTURES / "wake.wav", sample)
    process = await asyncio.create_subprocess_exec(str(binary), "--wav", str(sample),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        # The native recognizer treats stdin EOF as an explicit stop request.
        async with asyncio.timeout(15):
            output = await process.stdout.read()
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    finally:
        process.stdin.close()
    lines = output.decode("utf-8-sig").splitlines()
    assert process.returncode == 0 and "READY" in lines and lines[-1] == "WAKE", (process.returncode, lines)
    return {"pid": process.pid, "exited": True, "code": process.returncode, "output": lines,
            "fixture_sha256": sha256(sample.read_bytes()).hexdigest()}


@asynccontextmanager
async def edge_with_fake_microphone(playwright, folder, audio):
    # With a genuine CDP-connected Edge profile, foreground focus is not emulated by Playwright.
    executable = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    data = folder / "edge-profile"
    launcher = await asyncio.create_subprocess_exec(str(executable), "--headless=new", "--enable-automation", "--mute-audio", "--disable-gpu",
        "--disable-background-networking", "--no-first-run", "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0", f"--user-data-dir={data}", "--use-fake-device-for-media-stream",
        "--use-file-for-fake-audio-capture=" + str(audio), "--autoplay-policy=document-user-activation-required",
        "about:blank", creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        active_port = data / "DevToolsActivePort"
        try:
            async with asyncio.timeout(30):
                while not active_port.is_file():
                    if launcher.returncode is not None:
                        raise RuntimeError(f"隔离 Edge 提前退出，返回码 {launcher.returncode}。")
                    await asyncio.sleep(.1)
        except TimeoutError as error:
            raise TimeoutError("隔离 Edge 未在 30 秒内生成 DevTools 端口文件。") from error
        port, path = active_port.read_text(encoding="ascii").splitlines()
        browser = await playwright.chromium.connect_over_cdp(f"ws://127.0.0.1:{int(port)}{path}", no_defaults=True)
        await confirm_muted_edge(browser)
        session = await browser.new_browser_cdp_session()
        flags = (await session.send("Browser.getBrowserCommandLine"))["arguments"]
        assert "--use-fake-device-for-media-stream" in flags
        assert "--use-file-for-fake-audio-capture=" + str(audio) in flags
        assert not any(flag.startswith("--use-fake-ui-for-media-stream") for flag in flags)
        yield browser.contexts[0]
    finally:
        if browser is not None:
            session = await browser.new_browser_cdp_session()
            await session.send("Browser.close")
            await browser.close()
        if launcher.returncode is None:
            try:
                await asyncio.wait_for(launcher.wait(), 5)
            except TimeoutError:
                launcher.terminate()
                await launcher.wait()


async def inspect(cdp, expression):
    result = await cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
        "awaitPromise": True, "userGesture": False})
    assert "exceptionDetails" not in result, result.get("exceptionDetails")
    return result["result"].get("value")


async def wait_for(cdp, expression, timeout=25):
    async with asyncio.timeout(timeout):
        while not (value := await inspect(cdp, expression)):
            await asyncio.sleep(.10)
    return value


async def probe(cdp):
    return await inspect(cdp, """(() => ({visible:!document.hidden, focused:document.hasFocus(),
        activated:navigator.userActivation.hasBeenActive, attempts:nativeWakeQA.attempts,
        tracks:nativeWakeQA.tracks.map(t=>t.readyState), requests:nativeWakeQA.requests,
        speech:nativeWakeQA.speech, status:document.getElementById('status')?.textContent,
        optedIn:document.getElementById('native-wake-next')?.checked, link:document.querySelector('main')?.dataset.consoleLink}))()""")


async def native_cli(server, *options):
    process = await asyncio.create_subprocess_exec(sys.executable, "-X", "utf8", "-B", str(ROOT / "wake_activation.py"),
        "--profile", str(server.data), *options, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        output, error = await asyncio.wait_for(process.communicate(), 58)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode in (0, 2) and not error, error.decode("utf-8")
    return process.returncode, json.loads(output)


def checked_url(server, receipt):
    url = urlsplit(urljoin(server.url, receipt["activation_url"]))
    assert url.netloc == urlsplit(server.url).netloc and url.scheme == "http" and url.path == "/"
    assert parse_qs(url.query).get("chat") == ["1"]
    ticket, = parse_qs(url.fragment)["native-wake"]
    assert ticket
    # The production light scene skips 550W loading. Both scenes use the same wake controller.
    query = f"{url.query}&scene=off" if "scene" not in parse_qs(url.query) else url.query
    return urlunsplit(url._replace(query=query)), ticket


async def permission(cdp):
    return await inspect(cdp, "navigator.permissions.query({name:'microphone'}).then(result=>result.state)")


async def delivered(server, cdp, base_turns, base_attempts, base_requests):
    request_count = len(base_requests)
    await wait_for(cdp, f"""nativeWakeQA.requests.slice({request_count}).some(r=>
        (r.path==='/api/message' && r.status===200) || (r.path==='/api/transcribe' && r.status>=400))""", timeout=110)
    async with asyncio.timeout(8):
        state = await server.state()
        while len(state["turns"]) < base_turns + 1 or state["turns"][-1]["status"] != "replied":
            await asyncio.sleep(.1)
            state = await server.state()
    assert len(state["turns"]) == base_turns + 1, state["turns"]
    turn = state["turns"][-1]
    assert turn["user"].rstrip("！？。?!.") == SPEECH and turn["reply"] == "我在。", turn
    assert turn["cloud_requests"]["total"] == 0, turn["cloud_requests"]
    await wait_for(cdp, f"nativeWakeQA.speech.some(e=>e.kind==='end' && e.id==={json.dumps(turn['id'])})", timeout=16)
    trace = await probe(cdp)
    calls = trace["requests"][request_count:]
    (claimed,) = (r for r in calls if r["path"] == "/api/wake/claim")
    (shown,) = (r for r in calls if r["path"] == "/api/wake/shown")
    (transcript,) = (r for r in calls if r["path"] == "/api/transcribe")
    (submitted,) = (r for r in calls if r["path"] == "/api/message")
    (voice,) = (r for r in calls if r["path"] == "/api/speak")
    (preparation,) = (r for r in calls if r["path"] == "/api/prepare-voice")
    (opened,) = trace["attempts"][base_attempts:]
    speech = [event for event in trace["speech"] if event["id"] == turn["id"]]
    assert claimed["status"] == 200 and claimed["listen_next"] is True and shown["status"] == 204
    assert opened["visible"] and opened["focused"] and shown["at"] <= opened["at"] < transcript["at"] <= submitted["at"]
    assert transcript["status"] == 200 and transcript["transcript"].rstrip("！？。?!.") == SPEECH
    assert not transcript["live"] and not submitted["live"] and submitted["text"] == turn["user"]
    assert trace["tracks"] and all(track == "ended" for track in trace["tracks"])
    assert [event["kind"] for event in speech] == ["pending", "start", "end"], speech
    assert not any(event["live"] for event in speech) and speech[1]["text"] == "我在。", speech
    assert submitted["at"] <= voice["at"] < speech[1]["at"] < speech[2]["at"] and not voice["live"]
    assert voice["status"] == 200 and voice["spoken"]["text"] == "我在。" and voice["spoken"]["category"] == "warm", voice
    assert "cache;dur=" in voice["server_timing"] and "spawn;dur=" not in voice["server_timing"], voice
    assert voice["sha256"] == sha256((ROOT / "voice/lines/here.wav").read_bytes()).hexdigest()
    assert not (server.data / "voice-prompts").exists(), "固定音频触发了语音合成进程"
    return {"answer": turn["reply"], "asr": transcript["transcript"], "cloud_requests": turn["cloud_requests"],
        "first_capture": opened, "capture_to_submitted_ms": round(submitted["at"] - opened["at"], 1),
        "asr_prepare_ms": round(preparation["finished"] - preparation["at"], 1),
        "asr_http_ms": round(transcript["finished"] - transcript["at"], 1),
        "shown_status": shown["status"], "track_before_transcribe": transcript["live"], "tracks": trace["tracks"],
        "speech_events": speech, "speech_server_timing": voice["server_timing"], "speech_asset_sha256": voice["sha256"]}


async def no_recording(server, cdp, *cli, listen_expected=None):
    before = await probe(cdp)
    turn_count = len((await server.state())["turns"])
    code, receipt = await native_cli(server, *cli)
    if code != 0 or receipt["result"] != "reused":
        after = await probe(cdp)
        new_requests = after["requests"][len(before["requests"]):]
        raise AssertionError({"receipt": receipt, "visible": after["visible"], "focused": after["focused"],
                              "status": after["status"], "requests": new_requests})
    await asyncio.sleep(.65)
    after = await probe(cdp)
    assert after["attempts"] == before["attempts"] and after["tracks"] == before["tracks"], after
    assert len((await server.state())["turns"]) == turn_count
    calls = after["requests"][len(before["requests"]):]
    assert [r["path"] for r in calls] == ["/api/wake/claim", "/api/wake/shown"], calls
    assert calls[0].get("listen_next", False) is listen_expected and calls[1]["status"] == 204, calls
    return {"result": receipt["result"], "new_microphone_calls": 0, "new_turns": 0}


async def exercise(context, server, profile, report):
    await context.add_init_script(PROBE)
    address = server.url + "/?chat=1&scene=off"
    attempted_external = []
    page_errors = []

    async def local_only(route):
        target = urlsplit(route.request.url)
        if target.scheme in {"http", "https"} and target.netloc != urlsplit(server.url).netloc:
            attempted_external.append(target.hostname)
            await route.abort()
        else:
            await route.continue_()

    async def new_page():
        item = await context.new_page()
        item.on("pageerror", lambda error: page_errors.append(str(error)))
        await item.emulate_media(reduced_motion="reduce")
        await item.bring_to_front()
        return item, await context.new_cdp_session(item)

    await context.route("**/*", local_only)
    controls, _ = await new_page()
    await controls.goto(address, wait_until="domcontentloaded")
    await controls.locator(".preferences summary").click()
    setting = controls.locator("#native-wake-next")
    await expect(setting).not_to_be_checked()
    await context.grant_permissions(["microphone"], origin=server.url)
    await setting.check()
    assert await controls.evaluate(f"localStorage.getItem('{ORIGIN_OPT_IN}')") == "on"
    await controls.close()
    async with asyncio.timeout(4):
        while await native_cli(server, "--browser-ready") != (2, {"page_connected": False}):
            await asyncio.sleep(.1)
    sole = await server.state()
    old_pid = server.pid
    await server.stop()
    assert profile.owner() is None

    report["native_recognizer"] = await recognize_native_wav(server.folder)
    code, cold = await native_cli(server, "--probe", "--listen-next")
    assert code == 0 and cold["result"] == "default_browser_required" and cold["pid"] != old_pid
    url, ticket = checked_url(server, cold)
    restored = await server.state()
    assert restored["chat_id"] == sole["chat_id"] and restored["turns"] == sole["turns"]
    primary, current = await new_page()
    await primary.goto(url, wait_until="domcontentloaded")
    try:
        report["cold_page"] = await delivered(server, current, len(sole["turns"]), 0, [])
    except Exception:
        report["cold_diagnostics"] = {**await probe(current), "permission": await permission(current), "page_errors":page_errors}
        raise
    assert not report["cold_page"]["first_capture"]["activated"]
    report["cold_service"] = {"pid_before": old_pid, "pid_after": cold["pid"], "profile_restored": True,
                              "startup_ms":cold["server_ms"]}

    replay, replay_cdp = await new_page()
    await replay.goto(url, wait_until="domcontentloaded")
    await wait_for(replay_cdp, "nativeWakeQA.requests.some(r=>r.path==='/api/wake/claim' && r.status===410)")
    assert not (await probe(replay_cdp))["attempts"]
    async with aiohttp.ClientSession() as client:
        report["replayed_ticket"] = await rejected(client, server, "claim", {"ticket": ticket}, 410, origin=server.url)
        report["forged_origin"] = await rejected(client, server, "claim", {"ticket": ticket}, 403, origin="http://example.invalid")
    await replay.close()

    background, hidden = await new_page()
    await background.goto(address, wait_until="domcontentloaded")
    await wait_for(hidden, "document.querySelector('.connection.online') !== null")
    await primary.bring_to_front()
    await wait_for(current, "!document.hidden && document.hasFocus()")
    await wait_for(hidden, "document.hidden && !document.hasFocus()")
    before = await probe(current)
    code, warm = await native_cli(server, "--probe", "--listen-next")
    assert code == 0 and warm["result"] == "reused" and warm["pid"] == cold["pid"], warm
    try:
        report["foreground_page"] = await delivered(server, current, len(sole["turns"]) + 1,
            len(before["attempts"]), before["requests"])
    except Exception:
        report["foreground_diagnostics"] = {**await probe(current), "permission":await permission(current), "page_errors":page_errors}
        raise
    back = await probe(hidden)
    assert not back["visible"] and not back["focused"] and not back["attempts"] and not back["requests"], back
    report["background_page"] = {"foreground": False, "claims": 0, "microphone_calls": 0}

    composer = primary.get_by_test_id("composer")
    await composer.fill(DRAFT)
    report["existing_draft"] = await no_recording(server, current, "--probe", "--listen-next", listen_expected=True)
    await expect(composer).to_have_value(DRAFT)
    await composer.fill("")
    report["ordinary_open"] = await no_recording(server, current, "--probe", listen_expected=False)

    await context.clear_permissions()
    assert await permission(current) != "granted"
    report["ungranted"] = await no_recording(server, current, "--probe", "--listen-next", listen_expected=True)
    await context.grant_permissions(["microphone"], origin=server.url)
    assert await permission(current) == "granted"

    await primary.locator(".preferences summary").click()
    await primary.locator("#native-wake-next").uncheck()
    await primary.locator(".preferences summary").click()
    assert await inspect(current, f"localStorage.getItem('{ORIGIN_OPT_IN}')") != "on"
    report["opted_out"] = await no_recording(server, current, "--probe", "--listen-next", listen_expected=True)
    assert not attempted_external and not page_errors, (attempted_external, page_errors)
    final = await server.state()
    assert len(final["turns"]) == len(sole["turns"]) + 2 and final["turns"][0] == sole["turns"][0]
    report["turns_added"] = 2
    report["no_external_web_requests"] = True


async def run():
    folder = ROOT / "runs" / datetime.now().strftime("native-wake-next-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    mic = command_wav(folder)
    catalog = folder / "catalog.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_MEDIA_DB=str(catalog), MOSS_PROVIDER_DIR=str(folder / "providers"), MOSS_KEV_ENABLED="0",
        MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0", HF_HUB_OFFLINE="1", CUDA_VISIBLE_DEVICES="-1")
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1", model="isolated-unused")
    profile = WindowsReminders(server.data, server.port)
    report = {"passed": False, "physical_microphone": False, "used_default_browser": False,
              "microphone_fixture_sha256": sha256(mic.read_bytes()).hexdigest(), "light_scene": True}
    try:
        await server.start()
        profile.launch_file.write_text(json.dumps({"port":server.port, "model_url":"http://127.0.0.1:9/v1",
            "model":"isolated-unused", "media_db":str(catalog), "speech_prepare":"0"}), encoding="utf-8")
        code, seeded = await server.post("/api/message", {"text": MARKER})
        assert code == 200, seeded
        async with asyncio.timeout(10):
            while (await server.state())["turns"][-1]["status"] != "replied":
                await asyncio.sleep(.1)
        async with async_playwright() as playwright:
            async with edge_with_fake_microphone(playwright, folder, mic) as context:
                await exercise(context, server, profile, report)
        report["passed"] = True
    finally:
        if server.proc is not None:
            await server.stop()
        elif owner := profile.owner():
            assert owner["port"] == server.port
            process = psutil.Process(owner["pid"])
            assert (await server.post("/api/shutdown", {"token":owner["token"]}))[0] == 200
            await asyncio.to_thread(process.wait, 20)
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder / "result.json", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(run())

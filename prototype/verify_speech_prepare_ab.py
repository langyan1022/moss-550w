import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from statistics import median
import subprocess
from types import SimpleNamespace

import psutil
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from model_catalog import CloudHTTPClient, Profile
from verify_chat_providers import OBSERVE, limited_server
from verify_memory import send
from verify_speech_continuity import MEASURE


QUESTION = "请朗诵辛弃疾《破阵子·为陈同甫赋壮词以寄之》的前两句，只读这两句。"
VERSE = "醉里挑灯看剑梦回吹角连营"
PROBE = """
localStorage.setItem('moss.voice.enabled','on');
const timedFetch=window.fetch;
window.fetch=async function(url, options){
  if (url==='/api/prepare-speech') window.prepareAt=performance.now();
  const r=await timedFetch.apply(this, arguments);
  if(url==='/api/speak'){
    window.speechBody=r.clone().arrayBuffer().then(async b=>({
      ms:performance.now(), hash:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b)),x=>x.toString(16).padStart(2,'0')).join('')
    }));
  }
  return r;
}
"""


def free_gpu():
    result = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, encoding="utf-8", check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return int(result.stdout.strip().splitlines()[0])


def other_speaker():
    for process in psutil.process_iter(["name"]):
        if (process.info["name"] or "").lower() in {"python.exe", "pythonw.exe"}:
            try:
                if any(Path(a).name in {"managed_worker.py", "clone_omnivoice.py"} for a in process.cmdline()):
                    return True
            except psutil.Error:
                pass
    return False


async def measured(browser, folder, source_cache, on):
    port = 18970
    server = BrowserServer(folder, port)
    shutil.copytree(source_cache, server.data / "voice-prompts")
    ledger = folder / "paid-limit.json"
    ledger.write_text('{"allowed": 1, "outbound": 0}', encoding="utf-8")
    saved_client = Profile.client
    def client_for(profile):
        client = saved_client(profile)
        assert profile.id == "deepseek" and isinstance(client._client, CloudHTTPClient)
        async def check_public_poem(request):
            payload = json.loads(request.content)
            messages = payload["messages"]
            assert [m["content"] for m in messages if m["role"] == "user"] == [QUESTION]
            assert all(m["role"] in {"system", "user"} for m in messages)
        client._client.event_hooks["request"].append(check_public_poem)
        return client
    Profile.client = client_for
    cfg = SimpleNamespace(port=port, model_url="http://127.0.0.1:9/v1", paid_cap=1, resume_server=True)
    task = asyncio.create_task(limited_server(cfg, folder))
    owner = context = None
    try:
        async with asyncio.timeout(30):
            while not (server.data / "instance.json").is_file():
                if task.done():
                    await task
                await asyncio.sleep(.05)
        owner = json.loads((server.data / "instance.json").read_text(encoding="utf-8"))
        assert (await server.post("/api/select-model", {"id": "deepseek"}))[0] == 200
        state = await server.state()
        assert not state["turns"] and not state["memories"]
        context = await browser.new_context(reduced_motion="reduce")
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script(MEASURE + OBSERVE + PROBE)
        await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
        submitted = await send(page, server, QUESTION)
        assert re.sub(r"[^\u3400-\u9fff]", "", submitted) == VERSE, submitted
        await page.wait_for_function("() => speechTest.events.some(e=>e.type==='end'||e.type==='error')", timeout=80000)
        data = await page.evaluate("""async () => {
          const {start,first,final}=bench, clips=speechTest.clips, events=speechTest.events;
          const clip=clips[0], http=await speechBody;
          return {reply:document.querySelector('[data-testid=final]').textContent, first_visible_ms:first, final_visible_ms:final,
            prepare_request_ms:window.prepareAt==null?null:window.prepareAt-start, speak_request_ms:speechTest.requests[0].ms-start,
            wav_ready_ms:http.ms-start, wav_sha256:http.hash, web_audio_start_ms:clip.scheduled-start,
            first_voiced_ms:clip.scheduled-start+1000*(clip.first-clip.start), leading_quiet_ms:1000*(clip.first-clip.start),
            duration_s:clip.duration, spoken:speechTest.requests.map(r=>r.text), events:events.map(e=>e.type),
            error:events.find(e=>e.type==='error')?.message};
        }""")
        assert len(data["spoken"]) == 1 and data["events"][-1] == "end" and not errors
        outbounds = json.loads(ledger.read_text(encoding="utf-8"))["outbound"]
        assert outbounds == 1
        return {"prepare_enabled": on, "paid_requests": outbounds, **data}
    finally:
        if context:
            await context.close()
        if owner and not task.done():
            await server.post("/api/shutdown", {"token": owner["token"]})
        try:
            await task
        finally:
            Profile.client = saved_client


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("speech-prepare-ab-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    source_cache = ROOT / "runs/verse-reference-20261120-221137-572766/data/voice-prompts"
    references = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_cache.glob("*.pt")}
    assert references
    rows = []
    report = {"passed": False, "question": QUESTION, "reference_prompts": references, "arms": rows}
    previous = os.environ.get("MOSS_SPEECH_PREPARE")
    try:
        async with async_playwright() as playwright:
            browser = await open_browser(playwright)
            try:
                await confirm_muted_edge(browser)
                for index, on in enumerate((False, True, True, False), 1):
                    free = free_gpu()
                    assert free >= 3800 and psutil.virtual_memory().available >= 6 * 2**30 and not other_speaker()
                    os.environ["MOSS_SPEECH_PREPARE"] = str(int(on))
                    row = await measured(browser, folder / f"{index}-{'on' if on else 'off'}", source_cache, on)
                    row["start_free_gpu_mib"] = free
                    rows.append(row)
                    (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(index, on, row["first_voiced_ms"], row["speak_request_ms"], flush=True)
            finally:
                await browser.close()
        assert len({r["wav_sha256"] for r in rows}) == 1 and len({r["spoken"][0] for r in rows}) == 1
        paired = [rows[0]["first_voiced_ms"] - rows[1]["first_voiced_ms"], rows[3]["first_voiced_ms"] - rows[2]["first_voiced_ms"]]
        report.update(passed=True, paired_saved_ms=paired, median_saved_ms=median(paired))
    finally:
        if previous is None:
            os.environ.pop("MOSS_SPEECH_PREPARE", None)
        else:
            os.environ["MOSS_SPEECH_PREPARE"] = previous
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())

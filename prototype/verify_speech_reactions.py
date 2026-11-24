import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
import wave

from aiohttp import ClientSession, web

from core import Moss
from local_media.library import MediaLibrary
from serve import application
from voice_output import MossSpeech


REACTIONS = {
    "帮我叹口气": ("唉。", "[sigh]"),
    "请你叹一口气。": ("唉。", "[sigh]"),
    "请你帮我叹口气好吗？": ("唉。", "[sigh]"),
    "ＭＯＳＳ，能不能轻轻地叹口气？": ("唉。", "[sigh]"),
    "叹口气": ("唉。", "[sigh]"),
    "轻轻笑一下": ("哈哈。", "[laughter]"),
    "麻烦你轻轻地笑一下吧！": ("哈哈。", "[laughter]"),
    "能不能笑下？": ("哈哈。", "[laughter]"),
}
OTHER_TEXT = [
    "请解释“叹口气”的意思。",
    "翻译成英语：请你叹口气",
    "帮我写一句台词：轻轻笑一下。",
    "她刚才轻轻笑了一下。",
    "请你不要叹口气。",
    "请你叹口气，然后说说今天过得怎么样。",
    "请用“笑一下”造句。",
]

NODE = """
import {readFileSync} from "node:fs";
const input = JSON.parse(readFileSync(0, "utf8"));
const moduleResponse = await fetch(input.url + "/static/speech-delivery.js");
if (!moduleResponse.ok) throw Error(await moduleResponse.text());
const source = await moduleResponse.text();
const {conversationSpeech, speechSegments, expressiveText} = await import(
  "data:text/javascript," + encodeURIComponent(source)
);
const cases = [];
for (const item of input.cases) {
  const delivery = conversationSpeech(item.turn, [item.turn]);
  if (!item.expectedTag) {
    if (delivery.expression) throw Error(JSON.stringify({item, delivery}));
    continue;
  }
  const parts = speechSegments(delivery.text, delivery.category);
  const text = expressiveText(parts[0].spoken, delivery.expression);
  if (parts.length !== 1 || parts[0].caption !== item.turn.reply || text !== item.expectedTag)
    throw Error(JSON.stringify({item, parts, text}));
  const res = await fetch(input.url + "/api/speak", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text, category: delivery.category, prefer_cached: false})
  });
  if (!res.ok || res.headers.get("X-MOSS-Delivery") !== "reply") throw Error(await res.text());
  await res.arrayBuffer();
  cases.push({text: item.turn.user, caption: parts[0].caption, tag: text, speech_http_status: res.status});
}
process.stdout.write(JSON.stringify(cases));
"""


def fixture_catalog(folder):
    root = folder / "music"
    root.mkdir()
    for title in ("叹口气", "轻轻笑一下"):
        with wave.open(str(root / f"{title}.wav"), "wb") as track:
            track.setparams((1, 2, 16000, 0, "NONE", "NONE"))
            track.writeframes(b"\0\0" * 16000)
    library = MediaLibrary(folder / "catalog.sqlite3")
    library.configure([{"id": "test", "label": "合成音乐", "kind": "local", "path": str(root)}])
    library.scan()
    return library.db_path


async def run_cases(url, model_calls, report):
    client_input = []

    async with ClientSession(trust_env=False) as http:
        async def send(text):
            start = perf_counter()
            before = len(model_calls)
            async with http.post(url + "/api/message", json={"text": text}) as response:
                state = await response.json()
                assert response.status == 200, (text, state)
            turn = state["turns"][-1]
            async with asyncio.timeout(8):
                while turn["status"] == "thinking":
                    await asyncio.sleep(.02)
                    async with http.get(url + "/api/state") as response:
                        turn = (await response.json())["turns"][-1]
            assert turn["user"] == text
            return turn, {"text": text, "source": turn.get("source"), "status": turn["status"],
                          "model_probe_calls": len(model_calls) - before,
                          "complete_ms": round((perf_counter() - start) * 1000, 2)}

        for text, (caption, tag) in REACTIONS.items():
            turn, case = await send(text)
            assert turn["status"] == "replied" and turn["source"] == "smalltalk" and turn["reply"] == caption, turn
            assert not turn.get("media") and not turn.get("receipt") and not case["model_probe_calls"]
            client_input.append({"turn": turn, "expectedTag": tag})
            report["reactions"].append(case)

        for text in OTHER_TEXT:
            turn, case = await send(text)
            assert turn["source"] == "holo" and case["model_probe_calls"] == 1, turn
            assert not turn.get("media") and not turn.get("receipt")
            client_input.append({"turn": {**turn, "status": "replied", "reply": "这是一段普通文字。"}})
            report["ordinary_text"].append(case)

        turn, case = await send("播放《叹口气》")
        assert turn["status"] == "replied" and turn.get("media", {}).get("autoplay")
        assert turn["media"]["selected"]["title"] == "叹口气" and not case["model_probe_calls"]
        report["explicit_media"] = case

    node = await asyncio.create_subprocess_exec("node", "--input-type=module", "-e", NODE,
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    async with asyncio.timeout(12):
        stdout, stderr = await node.communicate(json.dumps({"url": url, "cases": client_input},
                                                            ensure_ascii=False).encode("utf-8"))
    assert node.returncode == 0, stderr.decode("utf-8", errors="replace")
    report["browser_module_requests"] = json.loads(stdout)


async def verify():
    report = {"reactions": [], "ordinary_text": [], "speech_worker_input": [], "cloud_model_calls": 0,
              "audio_method": "production speech HTTP API and normalization with an inert rendering probe"}
    model_calls = []

    async def model_offline(request):
        model_calls.append(request.path)
        return web.json_response({"error": {"message": "isolated no-inference probe"}}, status=503)

    class SpeechProbe(MossSpeech):
        async def _render_many(self, texts, category, disconnected, cached_only):
            report["speech_worker_input"].extend({"text": text, "category": category} for text in texts)
            return [b"" for text in texts], "fixture"

    model = web.Application()
    model.router.add_route("*", "/{path:.*}", model_offline)
    model_runner = web.AppRunner(model, access_log=None)
    await model_runner.setup()
    model_site = web.TCPSite(model_runner, "127.0.0.1", 0)
    await model_site.start()
    model_port = model_site._server.sockets[0].getsockname()[1]
    try:
        with TemporaryDirectory(prefix="moss-speech-reaction-") as directory, socket.socket() as listener:
            folder = Path(directory)
            os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_KEV_ENABLED="0", MOSS_SPEECH_PREPARE="0",
                              MOSS_MEDIA_DB=str(fixture_catalog(folder)))
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            data = folder / "data"
            moss = Moss(data, f"http://127.0.0.1:{model_port}/v1", "offline-probe")
            with patch("serve.MossSpeech", SpeechProbe):
                app = application(moss, lambda: "offline-probe", data, port, "isolated", asyncio.Event())
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.SockSite(runner, listener).start()
            try:
                await run_cases(f"http://127.0.0.1:{port}", model_calls, report)
            finally:
                await runner.cleanup()
                await moss.close()
    finally:
        await model_runner.cleanup()
    assert [item["text"] for item in report["speech_worker_input"]] == [tag for _, tag in REACTIONS.values()]
    assert all(item["category"] == "reply" for item in report["speech_worker_input"])
    report["passed"] = True
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()
    report = json.dumps(asyncio.run(verify()), ensure_ascii=False, indent=2) + "\n"
    if options.report:
        options.report.write_text(report, encoding="utf-8")
    print(report)

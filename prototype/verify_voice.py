import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import subprocess

import aiohttp
from browser_lab import BrowserServer, ROOT


async def run(folder):
    server = BrowserServer(folder, 18332)
    await server.start()
    results = {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=130)) as http:
            for name in ("silence", "speech", "music-0", "music-30", "music-120"):
                async with http.post(server.url + "/api/transcribe", data=(folder / f"{name}.wav").read_bytes(),
                                     headers={"Content-Type": "audio/wav"}) as response:
                    value = await response.json()
                    row = {"http": response.status, "characters": len(value.get("text", "")),
                           "empty": value.get("text") == "", "punctuation": value.get("text") in {".", "。"}}
                    if name == "speech":
                        row["text"] = value.get("text")
                    results[name] = row
                    print(name, row, flush=True)
        results["new_turns_from_transcription"] = len((await server.state())["turns"])
        results["passed"] = (all(results[n]["http"] == 200 for n in ("silence", "speech", "music-0", "music-30", "music-120"))
                             and results["silence"]["empty"] and "媒体库" in (results["speech"]["text"] or "")
                             and results["new_turns_from_transcription"] == 0)
    finally:
        (folder / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        await server.stop()
    return results["passed"]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=ROOT / "runs" / datetime.now().strftime("voice-%Y%m%d-%H%M%S"))
    args = p.parse_args()
    args.out.mkdir(exist_ok=False, parents=True)
    subprocess.run(["powershell", "-NoProfile", "-File", str(ROOT / "voice/speech_control.ps1"),
                    str(args.out / "speech.wav")], check=True)
    subprocess.run([r"E:\VoiceStudio\project\.venv\Scripts\python.exe", "-B", str(ROOT / "voice/make_controls.py"),
                    str(args.out)], check=True, cwd=ROOT)
    raise SystemExit(0 if asyncio.run(run(args.out)) else 1)

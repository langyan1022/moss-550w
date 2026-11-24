import argparse
import asyncio
from datetime import datetime
import hashlib
import io
import json
from time import perf_counter

import aiohttp
import soundfile as sf

from browser_lab import BrowserServer, ROOT
from verify_verse_asr import compare, hear
from verify_verse_voice import POEMS, acoustic, frontend_plans
from voice import asr_engine
from voice_output import SPEECH_RATES


async def run(excerpts):
    folder = ROOT / "runs" / datetime.now().strftime("verse-reference-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18643, model_url="http://127.0.0.1:9/v1")
    if excerpts:
        poems = {"春望节选": "国破山河在，城春草木深。感时花溅泪，恨别鸟惊心。",
                 "破阵子节选": "醉里挑灯看剑，梦回吹角连营。"}
        prompts = {"春望节选":"请朗诵杜甫《春望》的前四句。", "破阵子节选":"请朗诵辛弃疾《破阵子》的头两句。"}
    else:
        poems = {name: POEMS[name] for name in ("登鹳雀楼", "如梦令·昨夜雨疏风骤")}
        prompts = {}
    plans = frontend_plans(poems, prompts)
    report = {"passed": False, "speed": SPEECH_RATES["verse"], "poems": {}, "references": {}, "prompts": prompts}
    try:
        assert SPEECH_RATES["verse"] == SPEECH_RATES["verse-warm"]
        await server.start()
        for name in ("reference.wav", "reference-warm.wav"):
            report["references"][name] = hashlib.sha256((ROOT / "voice" / name).read_bytes()).hexdigest()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as client:
            for poem, plan in plans.items():
                assert plan["category"] == "verse" and len(plan["verse"]) == 1
                spoken = plan["verse"][0]["spoken"]
                rows = report["poems"][poem] = {}
                for category, label in (("verse", "朗诵"), ("verse-warm", "轻柔朗诵")):
                    started = perf_counter()
                    async with client.post(server.url + "/api/speak", json={"category": category, "text": spoken}) as res:
                        res.raise_for_status()
                        assert res.headers["X-MOSS-Delivery"] == category
                        wave = await res.read()
                    seconds = perf_counter() - started
                    pcm, rate = sf.read(io.BytesIO(wave), dtype="float32", always_2d=True)
                    path = folder / f"{poem}-{label}.wav"
                    path.write_bytes(wave)
                    rows[category] = {"spoken": spoken, "file": path.name, "sha256": hashlib.sha256(wave).hexdigest(),
                                      "http_ms": round(1000 * seconds), "acoustic": acoustic(pcm, rate)}
                assert rows["verse"]["sha256"] != rows["verse-warm"]["sha256"]
        await server.stop()
        for rows in report["poems"].values():
            for row in rows.values():
                # 先释放显存，再用本机独立识别器核对正文。保留识别分歧供试听。
                row["asr"] = hear((folder / row["file"]).read_bytes())
                row["diff"] = compare(row["spoken"], row["asr"])
                assert row["asr"] and row["acoustic"]["clipped_sample_fraction"] < .001
        report["passed"] = True
    finally:
        asr_engine.close()
        await server.stop()
        folder.mkdir(exist_ok=True)
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--excerpts", action="store_true", help="对照五言诗和宋词节选")
    asyncio.run(run(parser.parse_args().excerpts))

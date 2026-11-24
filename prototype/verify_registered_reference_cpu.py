"""Verify stock warm and neutral OmniVoice reference prompts without CUDA."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
from unittest.mock import patch

from browser_lab import BrowserServer, ROOT
from device_status import GIB, memory_sample
from local_media.library import MediaLibrary
from verify_context_reply import free_port
from verify_omnivoice_optional_imports import render, watch
from verify_registered_reference import MIB, MODE, bundled_manifest, cache_manifest
from verify_speech_prepare_ab import other_speaker


TEXT = "喝点水吧。"


async def trial(folder, enabled, row):
    memory = memory_sample()
    assert min(memory.available, memory.available_pagefile) >= 8 * GIB and not other_speaker(), \
        "冷启动实验需要 8 GiB 物理内存和提交余量，且不能与其他语音进程并行。"
    resources = row["resources"] = {
        "start_free_mib": memory.available // MIB,
        "min_free_mib": memory.available // MIB,
        "min_commit_mib": memory.available_pagefile // MIB,
        "start_gpu_free_mib": None,
        "min_gpu_free_mib": None,
        "peak_worker_rss_mib": 0,
    }
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    cache = server.data / "voice-prompts"
    cache.mkdir(parents=True)
    catalog = folder / "media.sqlite3"
    MediaLibrary(catalog)
    environment = {MODE: str(int(enabled)), "CUDA_VISIBLE_DEVICES": "-1",
                   "MOSS_SPEECH_PREPARE": "0", "MOSS_SPEECH_STANDBY": "0",
                   "MOSS_WINDOWS_REMINDERS": "0", "MOSS_MEM0_ENABLED": "0", "MOSS_KEV_ENABLED": "0",
                   "LOCALAPPDATA": str(folder / "appdata"), "MOSS_MEDIA_DB": str(catalog),
                   "MOSS_PROVIDER_DIR": str(folder / "providers")}
    try:
        with patch.dict(os.environ, environment):
            await server.start()
        assert not list(cache.iterdir()), cache
        task = asyncio.create_task(watch(server, render(server, TEXT, "warm", folder / "warm.wav"), resources))
        try:
            while not task.done():
                memory = memory_sample()
                resources["min_commit_mib"] = min(resources["min_commit_mib"], memory.available_pagefile // MIB)
                if min(memory.available, memory.available_pagefile) < 8 * GIB:
                    raise RuntimeError("合成实验低于 8 GiB RAM 或可用提交余量。")
                await asyncio.wait({task}, timeout=.5)
            row["render"] = task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        timing = row["render"]["server_timing_ms"]
        assert "spawn" in timing and "reference" in timing and "cache" not in timing, timing
        profile_contents = list(cache.iterdir())
        if enabled:
            assert not profile_contents, profile_contents
            row["reference_cache"] = bundled_manifest("cpu")
        else:
            row["reference_cache"] = cache_manifest(cache)
            assert set(row["reference_cache"]) == {"reply", "warm"} and len(list(cache.glob("*.pt"))) == 2
        row["profile_cache_files"] = [asset.name for asset in profile_contents]
        row["reference_source"] = "stock assets" if enabled else "fresh profile encoding"
        log = (folder / "web-1.log").read_text(encoding="utf-8", errors="replace")
        row["stock_seed_logs"] = re.findall(r"OmniVoice reference seed=stock style=(\w+) encoder=(\w+)", log)
        row["device_and_decoder"] = re.findall(r"OmniVoice weights=\S+ device=(\w+) decoder=(\w+)", log)
        expected = [("neutral", "cpu"), ("warm", "cpu")] if enabled else []
        assert row["stock_seed_logs"] == expected, row["stock_seed_logs"]
        assert row["device_and_decoder"] == [("cpu", "cpu")], row["device_and_decoder"]
        assert not (await server.state())["turns"]
    finally:
        await server.stop()


def storage_hashes(manifest):
    return {key: value for key, value in manifest["prompt_payload"].items() if key.startswith("data/")}


async def main(cuda_path):
    folder = ROOT / "runs" / datetime.now().strftime("registered-reference-cpu-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "text": TEXT, "style": "warm", "trials": []}
    try:
        for enabled in (False, True):
            row = {"enabled": enabled}
            report["trials"].append(row)
            await trial(folder / ("seed" if enabled else "encode"), enabled, row)
            print(f"seed={enabled} {row['render']['body_ms']} ms", flush=True)
        before, after = report["trials"]
        for style in ("reply", "warm"):
            assert before["reference_cache"][style]["prompt_payload"] == after["reference_cache"][style]["prompt_payload"], style
        assert before["render"]["sha256"] == after["render"]["sha256"]
        report["matching_wav"] = after["render"]["sha256"]
        report["saved_body_ms"] = round(before["render"]["body_ms"] - after["render"]["body_ms"], 1)
        if cuda_path:
            cuda = json.loads(cuda_path.read_text(encoding="utf-8"))
            assert cuda["passed"] and all(arm["device_and_decoder"] == [["cuda", "cuda"]] for arm in cuda["trials"])
            cpu_storage = storage_hashes(after["reference_cache"]["warm"])
            cuda_storage = storage_hashes(cuda["trials"][0]["reference_cache"]["warm"])
            assert cpu_storage != cuda_storage, (cpu_storage, cuda_storage)
            report["different_warm_cpu_cuda_storage"] = {"cpu": cpu_storage, "cuda": cuda_storage}
        report["passed"] = True
    except (Exception, asyncio.CancelledError) as error:
        report["error"] = repr(error)
        raise
    finally:
        result = folder / "result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cuda-report", type=Path, help="Optional passing CUDA comparison result.json")
    asyncio.run(main(parser.parse_args().cuda_report))

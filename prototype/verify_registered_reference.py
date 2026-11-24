"""Compare stock OmniVoice reference seeds against freshly encoded references."""

import asyncio
from datetime import datetime
from hashlib import sha256
import json
import os
import re
from statistics import median
from time import perf_counter
from unittest.mock import patch
from zipfile import ZipFile

from browser_lab import BrowserServer, ROOT
from device_status import GIB, memory_sample
from local_media.library import MediaLibrary
from verify_context_reply import free_port
from verify_omnivoice_optional_imports import render, watch
from verify_speech_prepare_ab import free_gpu, other_speaker


MODE = "MOSS_OMNIVOICE_REGISTERED_REFERENCE"
STOCK_DIR = ROOT / "voice/reference-seeds"
STOCK_FILES = {
    "reply": "64df87f7993b205fa42ff5df905f7371888aeed1a198d3767df33f9c68357bf7.pt",
    "warm": "17090e87aa9a40cfbd63eea46744119e83b8aac76ec7c449ce9ed050f8ef9376.pt",
}
TEXT = {
    "reply": "雨停了，树叶上的水珠在阳光里闪着光。",
    "warm": "今天辛苦了，先坐一会儿，慢慢喝口水。",
}
TRIALS = ((False, ("reply", "warm")), (True, ("reply", "warm")),
          (True, ("warm", "reply")), (False, ("warm", "reply")))
MIB = 2**20


def sha(data):
    return sha256(data).hexdigest()


def prompt_manifest(assets):
    entries = {}
    for style, asset in assets.items():
        if not asset.is_file():
            continue
        # PyTorch's archive root and serialization ID depend on the temporary
        # filename. Compare the serialized prompt and tensor storage payloads.
        with ZipFile(asset) as archive:
            payload = {}
            for name in archive.namelist():
                _, _, relative = name.partition("/")
                if relative == "data.pkl" or relative.startswith("data/"):
                    payload[relative] = sha(archive.read(name))
        assert "data.pkl" in payload and any(name.startswith("data/") for name in payload), asset
        entries[style] = {"file": asset.name, "archive_sha256": sha(asset.read_bytes()), "prompt_payload": payload}
    return entries


def cache_manifest(cache):
    return prompt_manifest({style: cache / filename for style, filename in STOCK_FILES.items()})


def bundled_manifest(warm_encoder):
    entries = prompt_manifest({"reply": STOCK_DIR / "neutral-cpu.pt",
                               "warm": STOCK_DIR / f"warm-{warm_encoder}.pt"})
    assert set(entries) == set(STOCK_FILES), entries
    return entries


async def wait_for_headroom():
    deadline = perf_counter() + 25
    while True:
        gpu = await asyncio.to_thread(free_gpu)
        memory = memory_sample()
        if gpu >= 3200 and min(memory.available, memory.available_pagefile) >= 8 * GIB and not other_speaker():
            return gpu, memory
        if perf_counter() >= deadline:
            raise RuntimeError(f"合成实验资源不足，显存 {gpu} MiB，RAM {memory.available // MIB} MiB，"
                               f"可用提交 {memory.available_pagefile // MIB} MiB。")
        await asyncio.sleep(1)


async def guarded_render(server, type_, path, resources):
    task = asyncio.create_task(watch(server, render(server, TEXT[type_], type_, path), resources))
    try:
        while not task.done():
            memory = memory_sample()
            resources["min_commit_mib"] = min(resources["min_commit_mib"], memory.available_pagefile // MIB)
            if min(memory.available, memory.available_pagefile) < 8 * GIB:
                raise RuntimeError("合成实验低于 8 GiB RAM 或可用提交余量。")
            await asyncio.wait({task}, timeout=.5)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def trial(folder, enabled, categories, row):
    gpu, memory = await wait_for_headroom()
    resources = row["resources"] = {
        "start_free_mib": memory.available // MIB,
        "min_free_mib": memory.available // MIB,
        "start_gpu_free_mib": gpu,
        "min_gpu_free_mib": gpu,
        "min_commit_mib": memory.available_pagefile // MIB,
        "peak_worker_rss_mib": 0,
    }
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    cache = server.data / "voice-prompts"
    cache.mkdir(parents=True)
    assert not list(cache.iterdir()), cache
    catalog = folder / "media.sqlite3"
    MediaLibrary(catalog)
    experiment = {MODE: str(int(enabled)), "MOSS_SPEECH_PREPARE": "0",
                  "MOSS_SPEECH_STANDBY": "0", "MOSS_WINDOWS_REMINDERS": "0",
                  "MOSS_MEM0_ENABLED": "0", "MOSS_KEV_ENABLED": "0",
                  "LOCALAPPDATA": str(folder / "appdata"), "MOSS_MEDIA_DB": str(catalog),
                  "MOSS_PROVIDER_DIR": str(folder / "providers")}
    row["results"] = []
    try:
        with patch.dict(os.environ, experiment):
            await server.start()
        assert not list(cache.iterdir()), cache
        for index, type_ in enumerate(categories):
            out = await guarded_render(server, type_, folder / (type_ + ".wav"), resources)
            out.update(style=type_, position="first" if index == 0 else "second")
            row["results"].append(out)
            timing = out["server_timing_ms"]
            assert ("spawn" in timing) == (index == 0) and "cache" not in timing, timing
            actual = cache_manifest(cache)
            if enabled:
                assert not list(cache.iterdir()), list(cache.iterdir())
            else:
                expected = set(STOCK_FILES) if index == 1 or type_ == "warm" else {"reply"}
                assert set(actual) == expected and len(list(cache.glob("*.pt"))) == len(expected), actual
            out["reference_files"] = [entry["file"] for entry in actual.values()]
            print(f"{folder.name} {type_} {out['body_ms']} ms", flush=True)
        row["reference_cache"] = bundled_manifest("cuda") if enabled else cache_manifest(cache)
        row["reference_source"] = "stock assets" if enabled else "fresh profile encoding"
        log = (folder / "web-1.log").read_text(encoding="utf-8", errors="replace")
        row["stock_seed_logs"] = re.findall(r"OmniVoice reference seed=stock style=(\w+) encoder=(\w+)", log)
        row["device_and_decoder"] = re.findall(r"OmniVoice weights=\S+ device=(\w+) decoder=(\w+)", log)
        expected_seeds = [("neutral", "cpu"), ("warm", "cuda")] if enabled else []
        assert row["stock_seed_logs"] == expected_seeds, row["stock_seed_logs"]
        assert row["device_and_decoder"] == [("cuda", "cuda")], row["device_and_decoder"]
        assert not (await server.state())["turns"]
    finally:
        await server.stop()


def compare(arms):
    pairs = [(arms[0], arms[1]), (arms[3], arms[2])]
    metrics = {"paired_saved_body_ms": {}, "paired_saved_load_reference_ms": {}, "paired_saved_generation_ms": {},
               "matching_prompt_payload": {}, "matching_wav": {}}
    for style in TEXT:
        assets = [arm["reference_cache"][style] for arm in arms]
        assert all(asset["prompt_payload"] == assets[0]["prompt_payload"] for asset in assets), assets
        metrics["matching_prompt_payload"][style] = assets[0]["prompt_payload"]
        metrics["matching_wav"][style] = {}
        for old, new in pairs:
            before, = (item for item in old["results"] if item["style"] == style)
            after, = (item for item in new["results"] if item["style"] == style)
            position = before["position"]
            assert after["position"] == position and before["sha256"] == after["sha256"], (before, after)
            metrics["matching_wav"][style][position] = before["sha256"]
            if position == "first":
                metrics["paired_saved_body_ms"][style] = round(before["body_ms"] - after["body_ms"], 1)
                for metric, key in (("reference", "load_reference"), ("generate", "generation")):
                    metrics[f"paired_saved_{key}_ms"][style] = round(before["server_timing_ms"][metric]
                                                                    - after["server_timing_ms"][metric], 1)
    metrics["first_body_median_ms"] = {
        label: round(median(arm["results"][0]["body_ms"] for arm in arms if arm["enabled"] == enabled), 1)
        for label, enabled in (("without_seed", False), ("with_seed", True))
    }
    return metrics


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("registered-reference-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder, flush=True)
    report = {"passed": False, "text": TEXT, "trials": []}
    try:
        for index, (enabled, categories) in enumerate(TRIALS, 1):
            row = {"enabled": enabled, "categories": categories}
            report["trials"].append(row)
            await trial(folder / f"{index}-{'seed' if enabled else 'encode'}", enabled, categories, row)
        report.update(compare(report["trials"]))
        report["passed"] = True
    except (Exception, asyncio.CancelledError) as error:
        report["error"] = repr(error)
        raise
    finally:
        result = folder / "result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())

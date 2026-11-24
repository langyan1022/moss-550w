import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
import psutil

from browser_lab import BrowserServer, ROOT
from core import Moss
from device_status import GIB, memory_sample
from serve import application
from verify_context_reply import free_port
from voice_output import MossSpeech


def workers(server):
    active = []
    for child in psutil.Process(server.pid).children(recursive=True):
        try:
            if any(Path(arg).name == "managed_worker.py" for arg in child.cmdline()):
                active.append(child.pid)
        except psutil.NoSuchProcess:
            pass
    return sorted(active)


def gpu_processes():
    command = ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return {int(line) for line in result.stdout.splitlines() if line.strip().isdigit()}


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("speech-import-lease-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18973, model_url="http://127.0.0.1:9/v1")
    report = {"passed": False}
    previous = os.environ.get("MOSS_SPEECH_PREPARE")
    os.environ["MOSS_SPEECH_PREPARE"] = "0"
    try:
        await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                for _ in range(2):
                    async with client.post(server.url + "/api/prepare-speech", json={}) as response:
                        assert response.status == 204
            await asyncio.sleep(.65)
            assert not workers(server)
            state = await server.state()
            assert not state["turns"] and state["speech_prepare"] is False
            report["opt_out_requests_without_worker_or_turn"] = 2
        finally:
            await server.stop()
        os.environ.pop("MOSS_SPEECH_PREPARE", None)
        await server.start()
        assert (await server.state())["speech_prepare"] is True
    finally:
        if previous is None:
            os.environ.pop("MOSS_SPEECH_PREPARE", None)
        else:
            os.environ["MOSS_SPEECH_PREPARE"] = previous
    try:
        async with aiohttp.ClientSession() as client:
            async def prepare(data):
                async with client.post(server.url + "/api/prepare-speech", json=data) as response:
                    return response.status

            assert await prepare({"text": "不应合成"}) == 400
            assert not workers(server)
            memory = memory_sample()
            report["physical_before_prepare_mib"] = memory.available // 2**20
            report["commit_before_prepare_mib"] = memory.available_pagefile // 2**20
            if min(memory.available, memory.available_pagefile) < 8 * GIB:
                raise RuntimeError("HTTP 默认预导入复验低于 8 GiB，需等可用内存恢复。")
            start = perf_counter()
            statuses = await asyncio.gather(*(prepare({}) for _ in range(4)))
            report["prepare_http_ms"] = round((perf_counter() - start) * 1000)
            assert statuses == [204] * 4
            async with asyncio.timeout(4):
                while not (first := workers(server)):
                    await asyncio.sleep(.1)
            assert len(first) in (1, 2)
            # Windows 虚拟环境可能有一个启动器和一个实际解释器，两者应属于同一进程链。
            if len(first) == 2:
                assert any(psutil.Process(pid).ppid() in first for pid in first)
            report["processes"] = first
            await asyncio.sleep(7)
            assert await prepare({}) == 204
            reused = workers(server)
            report["reused_processes"] = reused
            assert set(first) <= set(reused) and len(reused) in (1, 2)
            if len(reused) == 2:
                assert any(psutil.Process(pid).ppid() in reused for pid in reused)
            report["worker_rss_mib"] = [round(psutil.Process(pid).memory_info().rss / 2**20, 1) for pid in reused]
            report["worker_cuda_compute_pids"] = sorted(gpu_processes().intersection(reused))
            assert not report["worker_cuda_compute_pids"]
            async with client.post(server.url + "/api/speak", json={"text": "不客气。", "category": "reply"}) as response:
                assert response.status == 200
                assert await response.read() == (ROOT / "voice/lines/welcome.wav").read_bytes()
                assert response.headers["X-MOSS-Batch-Capable"] == "0"
            async with asyncio.timeout(8):
                while workers(server):
                    await asyncio.sleep(.12)
            report["released_ms"] = round((perf_counter() - start) * 1000)
            assert 11500 <= report["released_ms"] < 15500
            assert not (server.data / "voice-prompts").exists()
            assert await prepare({}) == 204
            await asyncio.sleep(.35)
            second = workers(server)
            assert second
            await server.stop()
            assert not any(psutil.pid_exists(pid) for pid in second)
            report["shutdown_released_pids"] = second
            report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"])


async def check_low_memory():
    folder = ROOT / "runs" / datetime.now().strftime("speech-import-memory-limit-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, free_port(), model_url="http://127.0.0.1:9/v1")
    report = {"passed": False, "observed": []}
    try:
        os.environ["MOSS_SPEECH_PREPARE"] = "1"
        await server.start()
        assert (await server.state())["speech_prepare"]
        async with aiohttp.ClientSession() as client:
            for _ in range(2):
                memory = memory_sample()
                row = {"physical_mib": memory.available // 2**20, "commit_mib": memory.available_pagefile // 2**20}
                report["observed"].append(row)
                if min(memory.available, memory.available_pagefile) >= 8 * GIB:
                    raise RuntimeError("运行环境当前高于内存门限，无法核验真实低内存行为。")
                async with client.post(server.url + "/api/prepare-speech", json={}) as response:
                    row["http"] = response.status
                    assert response.status == 204
        report["workers"] = workers(server)
        assert not report["workers"] and not (server.data / "voice-prompts").exists()
        assert not (await server.state())["turns"]
        report["passed"] = True
    finally:
        await server.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


class FakeVoiceWorkerSpeech(MossSpeech):
    """Exercise the real lease, render and lock with controlled worker replies."""

    def __init__(self, data):
        super().__init__(data, idle_seconds=30)
        self.spawns = []
        self.stops = []
        self.requests = []
        self.spawn_started = asyncio.Event()
        self.prepare_started = asyncio.Event()
        self.render_started = asyncio.Event()
        self.worker_stopped = asyncio.Event()
        self.allow_spawn = asyncio.Event()
        self.allow_prepare = asyncio.Event()
        self.allow_render = asyncio.Event()
        self.allow_spawn.set()
        self.allow_prepare.set()
        self.allow_render.set()
        self.prepare_code = 0
        self.preload_refused = False
        self.audio = (ROOT / "voice/lines/hello.wav").read_bytes()

    async def _start(self):
        if self.worker and self.worker.returncode is None:
            return
        self.worker = SimpleNamespace(returncode=None)
        self.spawns.append(self.worker)
        self.spawn_started.set()
        await self.allow_spawn.wait()

    async def _stop(self):
        worker, self.worker = self.worker, None
        self._batch_capable = False
        if worker and worker.returncode is None:
            worker.returncode = 0
            self.stops.append(worker)
            self.allow_spawn.set()
            self.allow_prepare.set()
            self.allow_render.set()
            self.worker_stopped.set()

    async def _worker_reply(self, request, cancel, disconnected, timeout):
        worker = self.worker
        assert worker is not None and worker.returncode is None, request
        self.requests.append((worker, request))
        if request.get("prepare"):
            assert request["device"] == "cuda" and request["cuda_min_free_mib"] == 3800, request
            assert not any(field in request for field in ("text", "output", "lines", "cancel_file")), request
            self.prepare_started.set()
            await self.allow_prepare.wait()
            if worker.returncode is not None:
                raise RuntimeError("测试语音进程已退出")
            if self.prepare_code:
                return {"code": self.prepare_code, "preload_refused": self.preload_refused}
            return {"code": 0, "info": {"device": "cuda", "batch_capable": True}}
        self.render_started.set()
        await self.allow_render.wait()
        assert worker.returncode is None, request
        Path(request["output"]).write_bytes(self.audio)
        return {"code": 0, "info": {"mode": "single", "batch_capable": True}}


@asynccontextmanager
async def fake_voice(folder):
    speech = FakeVoiceWorkerSpeech(folder)
    try:
        yield speech
    finally:
        speech.allow_spawn.set()
        speech.allow_prepare.set()
        speech.allow_render.set()
        await speech.close()


async def check_mic_http(folder):
    mic = "a" * 32
    rejected = ({"mic": ""}, {"mic": 1}, {"mic": mic, "release": False}, {"mic": mic, "text": "测试"})
    settings = {"MOSS_SPEECH_PREPARE": "1", "MOSS_PROVIDER_DIR": str(folder / "providers"),
                "MOSS_WINDOWS_REMINDERS": "0"}
    for configured in ("0", "1", "activity"):
        enabled = configured != "0"
        data = folder / configured
        mode = "standby"
        with patch.dict(os.environ, settings):
            os.environ["MOSS_SPEECH_MIC_WARM"] = configured
            moss = Moss(data, "http://127.0.0.1:9/v1", "unused")
            try:
                moss.model_id = "deepseek"
                async with fake_voice(data) as speech:
                    port = free_port()
                    url = f"http://127.0.0.1:{port}"
                    with patch("serve.MossSpeech", return_value=speech):
                        app = application(moss, lambda: mode, data, port, "isolated", asyncio.Event())
                    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
                    await runner.setup()
                    try:
                        await web.TCPSite(runner, "127.0.0.1", port).start()
                        async with aiohttp.ClientSession(headers={"Origin": url}) as client:
                            async def post(body, *, activity_header=True):
                                headers = {"X-MOSS-Voice-Activity": "1"} if activity_header and configured == "activity" else {}
                                async with client.post(url + "/api/prepare-speech", json=body, headers=headers) as response:
                                    return response.status

                            async with client.get(url + "/api/state") as response:
                                snapshot = await response.json()
                                assert snapshot["speech_mic_warm"] is enabled
                                assert snapshot["speech_mic_warm_on_activity"] is (configured == "activity")
                            if configured == "activity":
                                assert await post({"mic": "e" * 32}, activity_header=False) == 204
                                assert not speech.spawns and not speech.requests
                            assert await post({"mic": mic}) == 204
                            if not enabled:
                                assert not speech.spawns
                                continue
                            assert speech.worker is speech.spawns[0]
                            assert speech.requests[0][1]["prime"] is (configured == "1")
                            assert await post({"mic": mic, "release": True}) == 204
                            assert speech.worker is None
                            for body in rejected:
                                assert await post(body) == 400, body
                            mode = "cuda"
                            assert await post({"mic": "b" * 32}) == 204 and len(speech.spawns) == 1
                            mode = "standby"
                            assert await post({"mic": "c" * 32}) == 204 and len(speech.spawns) == 2
                            async with client.post(url + "/api/select-model", json={"id": "local"}) as response:
                                assert response.status == 200 and (await response.json())["model_id"] == "local"
                            assert speech.worker is None and len(speech.stops) == 2
                            assert await post({"mic": "d" * 32}) == 204 and len(speech.spawns) == 2
                            assert not moss.clients
                    finally:
                        await runner.cleanup()
            finally:
                await moss.close()


async def check_mic_lifecycle():
    folder = ROOT / "runs" / datetime.now().strftime("speech-mic-lease-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    report = {"passed": False, "gpu": False, "cases": []}
    abundant = SimpleNamespace(available=12 * GIB, available_pagefile=12 * GIB)
    low_memory = SimpleNamespace(available=7 * GIB, available_pagefile=10 * GIB)
    low_commit = SimpleNamespace(available=10 * GIB, available_pagefile=7 * GIB)

    def eligible():
        return True

    try:
        async with asyncio.timeout(12):
            with patch("voice_output.memory_sample", return_value=abundant):
                async with fake_voice(folder / "gates") as speech:
                    await speech.prepare_microphone("wrong-provider", eligible=lambda: False)
                    for limits in (low_memory, low_commit):
                        with patch("voice_output.memory_sample", return_value=limits):
                            await speech.prepare_microphone("no-headroom", eligible=eligible)
                    for limits in (SimpleNamespace(available=11 * GIB, available_pagefile=12 * GIB),
                                   SimpleNamespace(available=12 * GIB, available_pagefile=11 * GIB)):
                        with patch("voice_output.memory_sample", return_value=limits):
                            await speech.prepare_microphone("activity-headroom", eligible=eligible, prime=False)
                    assert not speech.spawns and not speech.requests
                report["cases"].append("ineligible_or_low_physical_or_commit_memory")

                async with fake_voice(folder / "cuda-declined") as speech:
                    speech.prepare_code = 75
                    await speech.prepare_microphone("cuda-declined", eligible=eligible)
                    assert len(speech.requests) == len(speech.spawns) == len(speech.stops) == 1
                    assert speech.worker is None
                    assert await speech.render("现在需要正式播报。", lambda: False) == speech.audio
                    assert len(speech.requests) == len(speech.spawns) == 2
                report["cases"].append("cuda_rejection_does_not_leave_worker_or_prevent_normal_playback")

                async with fake_voice(folder / "activity-refused") as speech:
                    speech.prepare_code = 75
                    speech.preload_refused = True
                    assert not await speech.prepare_microphone("activity-refused", eligible=eligible, prime=False)
                    imported = speech.worker
                    assert imported is not None and speech.release and not speech.stops
                    await speech.release_microphone("activity-refused")
                    assert await speech.render("现在需要正式播报。", lambda: False) == speech.audio
                    assert [worker for worker, _ in speech.requests] == [imported, imported]
                    assert len(speech.spawns) == 1 and not speech.stops
                report["cases"].append("confirmed_activity_preload_refusal_retains_imports_for_near_render")

                with patch("voice_output.IMPORT_PREPARE_SECONDS", .08):
                    async with fake_voice(folder / "activity-refused-unused") as speech:
                        speech.prepare_code = 69
                        speech.preload_refused = True
                        assert not await speech.prepare_microphone("activity-refused-unused", eligible=eligible, prime=False)
                        await speech.worker_stopped.wait()
                        assert speech.worker is None and len(speech.spawns) == len(speech.stops) == 1
                report["cases"].append("unused_import_after_preload_refusal_is_released")

                async with fake_voice(folder / "activity-error") as speech:
                    speech.prepare_code = 75
                    assert not await speech.prepare_microphone("activity-error", eligible=eligible, prime=False)
                    assert speech.worker is None and len(speech.stops) == 1
                report["cases"].append("other_activity_failures_discard_worker")

                async with fake_voice(folder / "ordering") as speech:
                    await speech.release_microphone("prior")
                    await speech.prepare_microphone("prior", eligible=eligible)
                    assert not speech.spawns and not speech.requests
                    await speech.prepare_imports()
                    existing = speech.worker
                    await asyncio.gather(
                        speech.prepare_microphone("owner", eligible=eligible),
                        speech.prepare_microphone("owner", eligible=eligible),
                        speech.prepare_microphone("foreign", eligible=eligible),
                    )
                    assert len(speech.spawns) == len(speech.requests) == 1
                    assert speech.worker is existing
                    await speech.release_microphone("foreign")
                    assert speech.worker is existing and not speech.stops
                    await speech.release_microphone("owner")
                    assert speech.worker is None and speech.stops == [existing]
                report["cases"].append("release_before_prepare_reuse_import_worker_and_single_owner")

                for stage in ("spawn", "prepare"):
                    async with fake_voice(folder / f"closing-{stage}") as speech:
                        reached = getattr(speech, f"{stage}_started")
                        unblock = getattr(speech, f"allow_{stage}")
                        unblock.clear()
                        preparation = asyncio.create_task(speech.prepare_microphone("closing", eligible=eligible))
                        await reached.wait()
                        release = asyncio.create_task(speech.release_microphone("closing"))
                        await asyncio.sleep(0)
                        unblock.set()
                        await asyncio.gather(preparation, release)
                        assert speech.worker is None and len(speech.spawns) == len(speech.stops) == 1
                        await speech.prepare_microphone("closing", eligible=eligible)
                        assert len(speech.spawns) == 1
                    report["cases"].append(f"closed_during_{stage}_cannot_restore_old_lease")

                with patch("voice_output.MIC_WARM_SECONDS", .08):
                    async with fake_voice(folder / "expiry") as speech:
                        await speech.prepare_microphone("expires", eligible=eligible)
                        worker = speech.worker
                        assert worker is not None
                        cached = await speech.render("不客气。", lambda: False)
                        assert cached == (ROOT / "voice/lines/welcome.wav").read_bytes()
                        await speech.worker_stopped.wait()
                        assert speech.worker is None and speech.stops == [worker]
                        assert len(speech.requests) == 1
                    report["cases"].append("unused_lease_expires_with_cached_playback")

                    async with fake_voice(folder / "expiry-during-warmup") as speech:
                        speech.allow_prepare.clear()
                        preparation = asyncio.create_task(speech.prepare_microphone("timed-out", eligible=eligible))
                        await speech.prepare_started.wait()
                        await speech.worker_stopped.wait()
                        await preparation
                        assert speech.worker is None and len(speech.spawns) == len(speech.stops) == 1
                    report["cases"].append("deadline_reclaims_still_loading_worker")

                    async with fake_voice(folder / "takeover") as speech:
                        speech.allow_prepare.clear()
                        speech.allow_render.clear()
                        preparation = asyncio.create_task(speech.prepare_microphone("live", eligible=eligible, prime=False))
                        await speech.prepare_started.wait()
                        reserved = speech.worker
                        rendering = asyncio.create_task(speech.render("此轮需要实时合成。", lambda: False))
                        await asyncio.sleep(0)
                        await speech.release_microphone("live")
                        assert speech.worker is reserved and not speech.stops
                        speech.allow_prepare.set()
                        await speech.render_started.wait()
                        release = asyncio.create_task(speech.release_microphone())
                        await asyncio.sleep(0)
                        speech.allow_render.set()
                        await asyncio.gather(preparation, release)
                        assert await rendering == speech.audio
                        assert len(speech.spawns) == 1 and len(speech.requests) == 2
                        assert [worker for worker, _ in speech.requests] == [reserved, reserved]
                        assert speech.requests[0][1]["prepare"] is True and speech.requests[0][1]["prime"] is False
                        assert "prepare" not in speech.requests[1][1]
                        await asyncio.sleep(.10)
                        assert speech.worker is reserved and not speech.stops
                    report["cases"].append("actual_render_takes_over_inflight_lease")
                await check_mic_http(folder / "http")
                report["cases"].append("http_default_off_validation_cloud_admission_and_local_model_release")
        report["passed"] = True
    except (Exception, asyncio.CancelledError) as error:
        report["error"] = repr(error)
        raise
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-low-memory", action="store_true")
    modes.add_argument("--check-mic-lifecycle", action="store_true")
    args = parser.parse_args()
    if args.check_low_memory:
        asyncio.run(check_low_memory())
    elif args.check_mic_lifecycle:
        asyncio.run(check_mic_lifecycle())
    else:
        asyncio.run(main())

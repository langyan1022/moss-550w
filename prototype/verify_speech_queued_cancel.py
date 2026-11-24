import asyncio
from datetime import datetime
import json
from time import perf_counter
from uuid import uuid4

import psutil

from browser_lab import ROOT
from verify_speech_prepare import gpu_processes
from voice_output import MossSpeech


async def verify():
    folder = ROOT / "runs" / datetime.now().strftime("speech-queued-cancel-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    speech = MossSpeech(folder)
    output = folder / "cancelled.wav"
    cancel = folder / "request.cancel"
    cancel.touch()
    report = {"passed": False, "requests": []}
    try:
        started = perf_counter()
        await speech._start()
        parent = psutil.Process(speech.worker.pid)
        for _ in range(2):
            request_id = uuid4().hex
            request = {"request_id": request_id, "cancel_file": str(cancel), "output": str(output),
                       "device": "auto", "steps": 16, "room": True, "text": "这句已经取消。"}
            response = await speech._worker_reply(request, cancel, lambda: False, timeout=60)
            assert response == {"request_id": request_id, "code": 130}
            report["requests"].append({"code": response["code"], "ms": round((perf_counter() - started) * 1000)})
        workers = {parent.pid, *(child.pid for child in parent.children(recursive=True))}
        assert speech.worker.returncode is None and workers
        active_gpu = workers.intersection(await asyncio.to_thread(gpu_processes))
        assert not output.exists() and not speech.cache.exists() and not active_gpu
        report["gpu_processes"] = sorted(active_gpu)
        report["worker_pids"] = sorted(workers)
        report["passed"] = True
    finally:
        await speech.close()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(verify())

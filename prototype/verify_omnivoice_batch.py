from datetime import datetime
import gc
import hashlib
import json
import os
from pathlib import Path
import threading
from time import perf_counter
from types import SimpleNamespace
from urllib.request import build_opener, ProxyHandler

import numpy as np
import psutil
import pynvml as nvml


ROOT = Path(__file__).resolve().parent
MIB = 1024**2
LINES = ["清风 入短亭。", "细雨 随舟至，江灯 逐浪明。"]


def active_speakers():
    names = {"managed_worker.py", "clone_omnivoice.py"}
    pids = []
    for process in psutil.process_iter(["name"], ad_value=None):
        if (process.info["name"] or "").lower() not in {"python.exe", "pythonw.exe"}:
            continue
        try:
            args = process.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(Path(a).name in names for a in args) or any("omnivoice_subprocess" in a for a in args):
            pids.append(process.pid)
    return pids


def waveform(data, rate):
    pcm = np.asarray(data, dtype="float32").reshape(-1)
    assert np.isfinite(pcm).all() and len(pcm) >= rate/3
    rms = float(np.sqrt(np.mean(pcm**2)))
    assert rms > .001
    return {"seconds":round(len(pcm)/rate,3), "rms":round(rms,5), "sha256_float32":hashlib.sha256(pcm.tobytes()).hexdigest()}


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_DEACTIVATE_ASYNC_LOAD"] = "1"
    directory = ROOT / "runs" / datetime.now().strftime("omnivoice-batch-%Y%m%d-%H%M%S-%f")
    directory.mkdir()
    saved = directory/"result.json"
    studio = Path(os.environ.get("VOICESTUDIO_ROOT", r"E:\VoiceStudio"))
    models = studio/"models/models--k2-fsa--OmniVoice"
    model = models/"snapshots"/(models/"refs/main").read_text(encoding="utf-8").strip()
    reference = ROOT/"voice/reference.wav"
    report = {"passed":False, "started":datetime.now().astimezone().isoformat(), "texts":LINES,
              "scope":"同进程单模型，直接调用 generate；整段原始波形可在 CPU 取得为终点，包括解码，不含网络、房间音效和播放。"}
    nvml.nvmlInit()
    device = nvml.nvmlDeviceGetHandleByIndex(0)
    def free_mib():
        return nvml.nvmlDeviceGetMemoryInfo(device).free//MIB
    quit_watch = threading.Event()
    unsafe = threading.Event()
    watches = {"phase":"preflight", "minimum_free_mib":{}, "reason":None}
    watcher = None
    try:
        local = build_opener(ProxyHandler({}))
        with local.open("http://127.0.0.1:18221/api/state", timeout=3) as response:
            state = json.load(response)
        busy = any(t["status"] in {"thinking","saving","pending"} for t in state["turns"])
        speakers = active_speakers()
        weights = sum((model/name).stat().st_size for name in ("model.safetensors", "audio_tokenizer/model.safetensors"))
        required = (weights+MIB-1)//MIB+1024
        available = free_mib()
        report["preflight"] = {"active_daily_turn":busy, "active_voice_processes":len(speakers),
                               "free_mib":available, "free_ram_mib":psutil.virtual_memory().available//MIB,
                               "weight_and_workspace_guard_mib":required}
        if busy or speakers or available < required or psutil.virtual_memory().available < 7*1024*MIB:
            report["skipped"] = "日常有任务或内存余量不足"
            return
        import torch
        from voice.clone_omnivoice import load_voice
        assert torch.cuda.is_available()
        total = nvml.nvmlDeviceGetMemoryInfo(device).total/MIB
        torch.cuda.set_per_process_memory_fraction((available-640)/total)
        torch.set_num_threads(2)
        def observe():
            while not quit_watch.wait(.18):
                memory = free_mib()
                phase = watches["phase"]
                minimum = watches["minimum_free_mib"]
                minimum[phase] = min(memory, minimum.get(phase, memory))
                if memory < 512 or active_speakers():
                    watches["reason"] = "显存余量低于 512 MiB 或有其他语音 worker 启动"
                    unsafe.set()
                    return
        def check(*_):
            if unsafe.is_set():
                raise RuntimeError(watches["reason"])
        watches["phase"] = "load"
        watcher = threading.Thread(target=observe, daemon=True)
        watcher.start()
        args = SimpleNamespace(model=model, reference=reference, transcript="感谢您十七年来的辛苦付出，恭喜您，明天可以离港回家了。",
                               prompt_cache=None, decoder="auto")
        voice, prompt = load_voice(args, "cuda")
        report.update(model_revision=model.name, reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
                      sample_rate=voice.sampling_rate, model_device=str(voice.device), codec_device=str(voice.audio_tokenizer.device),
                      num_step=16, speed=.9, gpu_name=torch.cuda.get_device_name())
        def generate(text):
            check()
            values = voice.generate(text=text, language="zh", voice_clone_prompt=prompt, speed=.9, num_step=16)
            result = [v.detach().float().cpu().numpy() if isinstance(v, torch.Tensor) else np.asarray(v) for v in values]
            torch.cuda.synchronize()
            return result
        def start_arm(name):
            check()
            torch.manual_seed(2049)
            torch.cuda.reset_peak_memory_stats()
            watches["phase"] = name
            return perf_counter()
        def finish_arm(name, start, first, finished, outputs):
            assert len(outputs) == len(LINES)
            report[name] = {"first_wave_ms":round((first-start)*1000,1), "both_waves_ms":round((finished-start)*1000,1),
                            "peak_allocated_mib":round(torch.cuda.max_memory_allocated()/MIB,1),
                            "peak_reserved_mib":round(torch.cuda.max_memory_reserved()/MIB,1),
                            "free_after_mib":free_mib(),
                            "outputs":[waveform(o,voice.sampling_rate) for o in outputs]}
        with voice.register_forward_pre_hook(check), torch.inference_mode():
            watches["phase"] = "warmup"
            generate("就绪。")
            start = start_arm("serial")
            first, = generate(LINES[0])
            first_ready = perf_counter()
            second, = generate(LINES[1])
            finish_arm("serial",start,first_ready,perf_counter(),[first,second])
            start = start_arm("batch")
            outputs = generate(LINES)
            complete = perf_counter()
            finish_arm("batch",start,complete,complete,outputs)
        report["batch_calls"] = 1
        report["passed"] = True
        del voice, prompt
        gc.collect()
        torch.cuda.empty_cache()
    finally:
        quit_watch.set()
        if watcher:
            watcher.join()
        report["monitor"] = watches
        report["finished"] = datetime.now().astimezone().isoformat()
        saved.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(saved, report["passed"], flush=True)
        nvml.nvmlShutdown()


if __name__ == "__main__":
    main()

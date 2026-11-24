"""Generate local, reference-conditioned Mandarin speech with VoiceStudio's OmniVoice."""

import argparse
from collections import OrderedDict
import gc
import hashlib
import json
import math
import os
import pickle
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from time import perf_counter

os.environ.setdefault("HF_HUB_OFFLINE", "1")
# 依次加载权重，降低语音与对话模型共存时 Windows 的内存提交峰值。
os.environ.setdefault("HF_DEACTIVATE_ASYNC_LOAD", "1")

# Reserve stdout for worker replies before third-party modules can write to it.
if __name__ == "__main__" and "--worker" in sys.argv:
    reply_stream = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

imports_started = perf_counter()
import numpy as np
import soundfile as sf
import torch
from omnivoice import OmniVoice
from omnivoice.models.omnivoice import OmniVoiceGenerationConfig, VoiceClonePrompt, _get_time_steps, _gumbel_sample
from omnivoice.utils.dtype import tts_dtype_name

IMPORT_SECONDS = round(perf_counter() - imports_started, 3)
REGISTERED_CPU_REFERENCE_DRAWS = 12
CUDA_GRAPH_MAX_C = 768
CUDA_GRAPH_MAX_ENTRIES = 8
CUDA_GRAPH_MIN_FREE_BYTES = 384 * 1024 * 1024
CUDA_GRAPH_PRIME_BUCKETS = ((256, 64), (320, 128), (384, 192), (448, 256), (512, 320))


@contextmanager
def opening_priority(enabled):
    if not enabled or os.name != "nt":
        yield False
        return
    import ctypes
    from subprocess import ABOVE_NORMAL_PRIORITY_CLASS, NORMAL_PRIORITY_CLASS

    # 伪句柄只指向当前语音子进程，系统和用户配置过的调度级别保持原值。
    kernel = ctypes.windll.kernel32
    current = ctypes.c_void_p(-1)
    previous = kernel.GetPriorityClass(current)
    raised = previous == NORMAL_PRIORITY_CLASS and bool(kernel.SetPriorityClass(current, ABOVE_NORMAL_PRIORITY_CLASS))
    try:
        yield raised
    finally:
        if raised and kernel.GetPriorityClass(current) == ABOVE_NORMAL_PRIORITY_CLASS:
            if not kernel.SetPriorityClass(current, previous):
                print("语音子进程恢复原调度级别失败。", file=sys.stderr)


def reference_prompt(args, model, warm_voice=False):
    # 温和参考取自同一段原声结尾的完整祝贺句。
    reference = args.reference.with_name("reference-warm.wav") if warm_voice else args.reference
    transcript = "恭喜您，明天可以离港回家了。" if warm_voice else args.transcript
    cache_file = None
    if args.prompt_cache:
        # The snapshot, exact transcript, and source samples all affect the prompt.
        key = hashlib.sha256(json.dumps([str(args.model.resolve()), transcript],
                                         ensure_ascii=False).encode("utf-8"))
        reference_audio = reference.read_bytes()
        key.update(reference_audio)
        args.prompt_cache.mkdir(parents=True, exist_ok=True)
        cache_file = args.prompt_cache / (key.hexdigest() + ".pt")
        if cache_file.is_file():
            try:
                return VoiceClonePrompt.load(str(cache_file))
            except (EOFError, KeyError, OSError, RuntimeError, ValueError, pickle.UnpicklingError):
                print("Rebuilding unreadable reference prompt.")
        if __package__:
            from .stock_voice_prompt import registered_prompt
        else:
            from stock_voice_prompt import registered_prompt
        style = "warm" if warm_voice else "neutral"
        encoder = model.audio_tokenizer.device
        cuda_name = torch.cuda.get_device_name(encoder) if encoder.type == "cuda" else None
        seed = registered_prompt(args.model, style, transcript, reference_audio, encoder.type, cuda_name)
        if seed:
            try:
                prompt = VoiceClonePrompt.load(str(seed))
            except (EOFError, KeyError, OSError, RuntimeError, ValueError, pickle.UnpicklingError):
                pass
            else:
                if model.device.type == "cpu":
                    # The registered encoder advances the CPU generator by twelve scalar draws.
                    # Match that state before starting speech generation from the saved reference.
                    for _ in range(REGISTERED_CPU_REFERENCE_DRAWS):
                        torch.rand((), device="cpu")
                print(f"OmniVoice reference seed=stock style={style} encoder={encoder.type}", file=sys.stderr, flush=True)
                return prompt
    samples, rate = sf.read(reference, dtype="float32", always_2d=True)
    mono = torch.from_numpy(samples.mean(axis=1)).unsqueeze(0)
    prompt = model.create_voice_clone_prompt(ref_audio=(mono, rate), ref_text=transcript)
    if cache_file:
        with tempfile.NamedTemporaryFile(dir=cache_file.parent, suffix=".pt", delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            prompt.save(str(temp_path))
            os.replace(temp_path, cache_file)
        finally:
            temp_path.unlink(missing_ok=True)
    return prompt


def place_codec_for_decode(codec, device):
    free_bytes, _ = torch.cuda.mem_get_info(device)
    codec_bytes = sum(t.numel() * t.element_size() for t in (*codec.parameters(), *codec.buffers()))
    # Allow the generator and decoder to allocate temporary tensors after the move.
    if free_bytes < codec_bytes + 256 * 1024**2:
        return
    try:
        codec.to(device)
        return
    except torch.cuda.OutOfMemoryError:
        pass
    # Module.to() may have moved only some parameters before raising.
    codec.to("cpu")
    torch.cuda.empty_cache()


def load_voice(args, device, durations=None):
    if durations is None:
        durations = {}
    started = perf_counter()
    if __package__:
        from .weight_snapshot import select_weights
    else:
        from weight_snapshot import select_weights
    dtype = tts_dtype_name(device)
    original = args.model.resolve()
    weights = select_weights(original, device, dtype)
    direct = getattr(args, "direct_cuda_load", False)
    if direct and (device != "cuda" or args.decoder != "auto" or weights == original):
        raise ValueError("直接加载到 GPU 的测量需要已登记的半精度权重和自动解码")
    durations["select_weights_s"] = round(perf_counter() - started, 3)
    torch.manual_seed(2049)
    # Windows 上 CAD 与语音共用提交内存。只在冷载入时限制权重转换的 CPU 并行度。
    threads = torch.get_num_threads()
    started = perf_counter()
    try:
        if os.name == "nt":
            torch.set_num_threads(getattr(args, "load_threads", 1))
        model = OmniVoice.from_pretrained(
            str(weights), device_map=device if direct else "cpu", dtype=getattr(torch, dtype),
        ).eval()
    finally:
        if os.name == "nt":
            torch.set_num_threads(threads)
    durations["load_checkpoint_and_codec_s"] = round(perf_counter() - started, 3)
    # 掩码解码每一步都重算完整序列，生成时不会读取上一步的键值。
    model.llm.config.use_cache = False
    started = perf_counter()
    if device == "cuda" and not direct:
        # Encoding a new reference is faster on this machine's CPU.
        codec = model.audio_tokenizer
        model.audio_tokenizer = None
        model.to("cuda")
        model.audio_tokenizer = codec
    durations["place_generator_s"] = round(perf_counter() - started, 3)
    with torch.inference_mode():
        started = perf_counter()
        prompt = reference_prompt(args, model)
        if device == "cuda":
            prompt.ref_audio_tokens = prompt.ref_audio_tokens.to(model.device)
        durations["reference_prompt_s"] = round(perf_counter() - started, 3)
        started = perf_counter()
        if device == "cuda" and args.decoder == "auto" and not direct:
            place_codec_for_decode(model.audio_tokenizer, device)
        durations["place_decoder_s"] = round(perf_counter() - started, 3)
    print(f"OmniVoice weights={'registered-fp16' if weights != original else 'original'} "
          f"device={device} decoder={model.audio_tokenizer.device.type}", file=sys.stderr, flush=True)
    return model, prompt


def smooth_conversational_pauses(pcm, rate, *, warm=False):
    """Tighten unnaturally long interior dead-air pauses on dry PCM before room reverb.

    Preserves natural prosodic hierarchy (periods remain slightly longer than commas)
    and uses cosine overlap-add inside quiet valleys so splices are click-free.
    Never called for poetry ('verse' / 'verse-warm').
    """
    n = len(pcm)
    win = max(1, int(round(rate * 0.01)))
    if n < win * 12:
        return pcm
    n_frames = n // win
    trimmed = pcm[:n_frames * win].reshape(n_frames, win)
    rms = np.sqrt(np.mean(trimmed * trimmed, axis=1))
    p90 = float(np.percentile(rms, 90))
    if p90 < 0.008:
        return pcm
    sil_thr = max(0.0022, p90 * 0.045)
    voiced = rms > sil_thr
    voiced_idx = np.flatnonzero(voiced)
    if voiced_idx.size < 2:
        return pcm

    # Require >= 3 consecutive voiced frames (30ms) for true speech onset so a tiny pre-breath
    # puff cannot preserve a 200ms dead gap before the first spoken syllable.
    first_v = int(voiced_idx[0])
    for idx_v in voiced_idx:
        k = int(idx_v)
        if k + 2 < n_frames and voiced[k] and voiced[k + 1] and voiced[k + 2]:
            first_v = k
            break
    last_v = int(voiced_idx[-1])
    lead_keep_f = 5 if warm else 4     # 50ms / 40ms dry lead-in before first voiced frame
    trail_keep_f = 7 if warm else 6    # 70ms / 60ms dry decay before room reverb adds tail
    start_sample = max(0, (first_v - lead_keep_f) * win)
    end_sample = min(n, (last_v + 1 + trail_keep_f) * win)

    # Interior pause compression parameters (in frames of 10ms)
    max_pause_f = 18 if warm else 16   # pauses <= 180ms (warm) / 160ms (reply) stay 100% untouched
    ceil_pause_f = 26 if warm else 24  # cap extreme 500-660ms stalls to 260ms / 240ms
    slope = 0.22 if warm else 0.20
    xfade = max(1, int(round(rate * 0.012)))  # 12ms cosine crossfade

    # Collect interior silent runs longer than max_pause_f
    cuts = []
    i = first_v
    while i <= last_v:
        if not voiced[i]:
            j = i
            while j <= last_v and not voiced[j]:
                j += 1
            gap_f = j - i
            if gap_f > max_pause_f:
                keep_f = min(ceil_pause_f, max_pause_f + int(round((gap_f - max_pause_f) * slope)))
                cut_f = gap_f - keep_f
                if cut_f >= 2:
                    left_f = i + keep_f // 2
                    right_f = j - (keep_f - keep_f // 2)
                    l_s = left_f * win
                    r_s = right_f * win
                    if r_s - l_s > xfade * 2:
                        cuts.append((l_s, r_s))
            i = j
        else:
            i += 1

    if not cuts and start_sample == 0 and end_sample == n:
        return pcm

    # Assemble dry waveform with smooth Hann crossfades across trimmed dead-air centers
    fade_in = 0.5 * (1.0 - np.cos(np.pi * np.arange(xfade, dtype=np.float32) / xfade))
    fade_out = 1.0 - fade_in
    pieces = []
    cursor = start_sample
    for l_s, r_s in cuts:
        if l_s < cursor + xfade:
            continue
        pieces.append(pcm[cursor:l_s])
        blend = pcm[l_s:l_s + xfade] * fade_out + pcm[r_s - xfade:r_s] * fade_in
        pieces.append(blend)
        cursor = r_s
    pieces.append(pcm[cursor:end_sample])
    out = np.concatenate(pieces).astype(np.float32, copy=False)

    # Gentle 6ms edge fade on trimmed dry boundaries so zero DC jump enters room reverb
    edge = min(int(round(rate * 0.006)), len(out) // 4)
    if edge > 1:
        ramp = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        if start_sample > 0:
            out[:edge] *= ramp
        if end_sample < n:
            out[-edge:] *= ramp[::-1]
    return out


class _CudaGraphCache:
    """Bucketed B=2 batched CUDA Graphs for OmniVoice iterative denoising (bit-exact)."""

    def __init__(self):
        self.entries = OrderedDict()

    def clear(self):
        self.entries.clear()

    @staticmethod
    def bucket_for(c_len, t_len):
        bucket_c = ((c_len + 63) // 64) * 64
        bucket_t = min(bucket_c, ((t_len + 63) // 64) * 64)
        return bucket_c, bucket_t

    def ensure(self, model, bucket_c, bucket_t):
        key = (bucket_c, bucket_t)
        if key in self.entries:
            self.entries.move_to_end(key)
            return self.entries[key]
        if bucket_c > CUDA_GRAPH_MAX_C or bucket_t <= 0:
            return None
        free_bytes, _ = torch.cuda.mem_get_info(model.device)
        if free_bytes < CUDA_GRAPH_MIN_FREE_BYTES:
            return None
        while len(self.entries) >= CUDA_GRAPH_MAX_ENTRIES:
            self.entries.popitem(last=False)
        device = model.device
        batched_ids = torch.zeros((2, 8, bucket_c), dtype=torch.long, device=device)
        batched_am = torch.zeros((2, bucket_c), dtype=torch.bool, device=device)
        mask = torch.ones((2, 1, bucket_c, bucket_c), dtype=torch.bool, device=device)
        gather_idx = torch.arange(bucket_t, dtype=torch.long, device=device)

        def fwd():
            emb = model._prepare_embed_inputs(batched_ids, batched_am)
            h = model.llm(inputs_embeds=emb, attention_mask=mask, return_dict=True)[0]
            h0 = h[0].index_select(0, gather_idx)
            h1 = h[1, :bucket_t]
            h_sel = torch.stack((h0, h1), dim=0)
            logits = model.audio_heads(h_sel)
            return logits.view(2, bucket_t, 8, -1).permute(0, 2, 1, 3).to(torch.float32)

        stream = torch.cuda.Stream(device=device)
        stream.wait_stream(torch.cuda.current_stream(device))
        with torch.cuda.stream(stream):
            for _ in range(2):
                out = fwd()
        torch.cuda.current_stream(device).wait_stream(stream)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            out = fwd()
        entry = {
            "graph": graph,
            "batched_ids": batched_ids,
            "batched_am": batched_am,
            "mask": mask,
            "gather_idx": gather_idx,
            "out": out,
        }
        self.entries[key] = entry
        return entry


class VoiceSession:
    def __init__(self):
        self.device = self.model = self.prompt = self.reference_stamp = self.warm_prompt = None
        self.cpu_rng = self.gpu_rng = None
        self.graphs = _CudaGraphCache()
        self._step_hook = None
        self._last_graph_used = False

    def clear(self, *, release_cuda=False):
        self.graphs.clear()
        self.model = self.prompt = self.warm_prompt = self.cpu_rng = self.gpu_rng = None
        self.device = self.reference_stamp = None
        gc.collect()
        if release_cuda:
            torch.cuda.empty_cache()

    def _cuda_graph_waveform(self, sentence, language, prompt, speed, steps):
        if (self.device != "cuda" or os.environ.get("MOSS_SPEECH_CUDA_GRAPH", "1") != "1"
                or not isinstance(sentence, str) or steps < 1):
            return None
        task = self.model._preprocess_all(
            sentence, language=None if language == "auto" else language,
            voice_clone_prompt=prompt, speed=speed,
        )
        if len(task.texts) != 1:
            return None
        gc_cfg = OmniVoiceGenerationConfig(num_step=steps)
        if not gc_cfg.guidance_scale:
            return None
        inp = self.model._prepare_inference_inputs(
            task.texts[0], task.target_lens[0], task.ref_texts[0],
            task.ref_audio_tokens[0], task.langs[0], task.instructs[0], gc_cfg.denoise,
        )
        c_len = int(inp["input_ids"].size(2))
        t_len = int(task.target_lens[0])
        pad = c_len - t_len
        if t_len <= 0 or pad < 0:
            return None
        bucket_c, bucket_t = self.graphs.bucket_for(c_len, t_len)
        entry = self.graphs.ensure(self.model, bucket_c, bucket_t)
        if entry is None:
            return None
        device = self.model.device
        batched_ids = entry["batched_ids"]
        batched_am = entry["batched_am"]
        mask = entry["mask"]
        gather_idx = entry["gather_idx"]
        out = entry["out"]

        batched_ids.zero_()
        batched_am.zero_()
        mask.zero_()
        batched_ids[0, :, :c_len].copy_(inp["input_ids"][0])
        batched_am[0, :c_len].copy_(inp["audio_mask"][0])
        batched_ids[1, :, :t_len].copy_(inp["input_ids"][0, :, pad:])
        batched_am[1, :t_len].copy_(inp["audio_mask"][0, pad:])
        mask[0, :, :c_len, :c_len] = True
        mask[1, :, :t_len, :t_len] = True
        gather_idx.zero_()
        gather_idx[:t_len].copy_(torch.arange(pad, c_len, dtype=torch.long, device=device))

        tokens = torch.full((1, 8, t_len), self.model.config.audio_mask_id, dtype=torch.long, device=device)
        timesteps = _get_time_steps(0.0, 1.0, steps + 1, gc_cfg.t_shift).tolist()
        total_mask = t_len * 8
        remaining = total_mask
        schedule = []
        for step in range(steps):
            count = remaining if step == steps - 1 else min(
                math.ceil(total_mask * (timesteps[step + 1] - timesteps[step])), remaining)
            schedule.append(int(count))
            remaining -= int(count)
        layer_ids = torch.arange(8, device=device).view(1, -1, 1)

        for step in range(steps):
            if self._step_hook is not None:
                self._step_hook()
            entry["graph"].replay()
            k = schedule[step]
            if k <= 0:
                continue
            pred_tokens, scores = self.model._predict_tokens_with_scoring(
                out[0:1, :, :t_len, :], out[1:2, :, :t_len, :], gc_cfg,
            )
            scores = scores - (layer_ids * gc_cfg.layer_penalty_factor)
            if gc_cfg.position_temperature > 0.0:
                scores = _gumbel_sample(scores, gc_cfg.position_temperature)
            scores.masked_fill_(tokens != self.model.config.audio_mask_id, -float("inf"))
            _, topk_idx = torch.topk(scores.flatten(), k)
            flat = tokens.flatten()
            flat[topk_idx] = pred_tokens.flatten()[topk_idx]
            tokens.copy_(flat.view_as(tokens))
            batched_ids[0, :, pad:c_len].copy_(tokens[0])
            batched_ids[1, :, :t_len].copy_(tokens[0])
        if self._step_hook is not None:
            self._step_hook()
        self._last_graph_used = True
        return self.model._decode_and_post_process(tokens[0], task.ref_rms[0], gc_cfg)

    def _waveforms(self, text, args):
        language = getattr(args, "language", "zh")
        prompt = self.prompt
        if getattr(args, "warm_voice", False):
            if self.warm_prompt is None:
                self.warm_prompt = reference_prompt(args, self.model, warm_voice=True)
                if self.device == "cuda" and getattr(self.warm_prompt, "ref_audio_tokens", None) is not None:
                    self.warm_prompt.ref_audio_tokens = self.warm_prompt.ref_audio_tokens.to(self.model.device)
            prompt = self.warm_prompt
        speed = getattr(args, "speed", 1.0)
        lang = None if language == "auto" else language
        if isinstance(text, str):
            fast = self._cuda_graph_waveform(text, language, prompt, speed, args.steps)
            if fast is not None:
                return [fast]
        elif (isinstance(text, list) and text and all(isinstance(item, str) for item in text)
                and self.device == "cuda" and os.environ.get("MOSS_SPEECH_CUDA_GRAPH", "1") == "1"):
            outputs = []
            all_graph = True
            for item in text:
                wav = self._cuda_graph_waveform(item, language, prompt, speed, args.steps)
                if wav is None:
                    all_graph = False
                    wav = self.model.generate(
                        text=item, language=lang, voice_clone_prompt=prompt,
                        speed=speed, num_step=args.steps,
                    )[0]
                outputs.append(wav)
            self._last_graph_used = all_graph
            return outputs
        self._last_graph_used = False
        return self.model.generate(text=text, language=lang,
                                   voice_clone_prompt=prompt, speed=speed, num_step=args.steps)

    def _waveform(self, sentence, args):
        return self._waveforms(sentence, args)[0]

    def batch_capable(self):
        return (self.device == "cuda" and self.model.audio_tokenizer.device.type == "cuda"
                and torch.cuda.mem_get_info(self.device)[0] >= 1024**3)

    def generate_line(self, sentence, args):
        if self.device != "cuda" or self.model.audio_tokenizer.device.type != "cuda":
            return self._waveform(sentence, args)
        cpu_rng = torch.get_rng_state()
        gpu_rng = torch.cuda.get_rng_state(self.device)
        try:
            return self._waveform(sentence, args)
        except torch.cuda.OutOfMemoryError:
            pass
        # Release cached graphs and the failed inference traceback, then retry in hybrid mode.
        self.graphs.clear()
        self.model.audio_tokenizer.to("cpu")
        torch.cuda.empty_cache()
        torch.set_rng_state(cpu_rng)
        torch.cuda.set_rng_state(gpu_rng, self.device)
        return self._waveform(sentence, args)

    def generate(self, args, device):
        stat = args.reference.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        warm = self.model is not None and self.device == device and self.reference_stamp == stamp
        durations = {}
        start = perf_counter()
        if not warm:
            if self.model is not None:
                self.clear(release_cuda=self.device == "cuda")
            self.model, self.prompt = load_voice(args, device, durations)
            self.device, self.reference_stamp = device, stamp
            # Restore the post-load random state on later requests, as a fresh CLI invocation would.
            self.cpu_rng = torch.get_rng_state()
            self.gpu_rng = torch.cuda.get_rng_state(device) if device == "cuda" else None
        else:
            torch.set_rng_state(self.cpu_rng)
            if self.gpu_rng is not None:
                torch.cuda.set_rng_state(self.gpu_rng, device)
        load_seconds = perf_counter() - start
        start = perf_counter()
        cancel_file = getattr(args, "cancel_file", None)

        def check_interrupt(*_):
            if cancel_file.is_file():
                raise InterruptedError()

        priming = "skipped"
        if not warm and not args.line and device == "cuda" and cancel_file is not None and getattr(args, "prime", True):
            # 只在首次后台装载后运行一次，用户的第一句仍从相同的随机状态开始。
            def yield_gpu(*_):
                # 后台每次只排一个去噪步骤，检查取消时 GPU 也已真正空闲。
                torch.cuda.synchronize(device)
                check_interrupt()

            try:
                check_interrupt()
                self._step_hook = yield_gpu
                with self.model.register_forward_pre_hook(yield_gpu):
                    self._waveform("就绪。", args)
                if os.environ.get("MOSS_SPEECH_CUDA_GRAPH", "1") == "1":
                    for bucket_c, bucket_t in CUDA_GRAPH_PRIME_BUCKETS:
                        yield_gpu()
                        self.graphs.ensure(self.model, bucket_c, bucket_t)
                if self.warm_prompt is None and args.reference.with_name("reference-warm.wav").is_file():
                    yield_gpu()
                    self.warm_prompt = reference_prompt(args, self.model, warm_voice=True)
                    if device == "cuda" and getattr(self.warm_prompt, "ref_audio_tokens", None) is not None:
                        self.warm_prompt.ref_audio_tokens = self.warm_prompt.ref_audio_tokens.to(self.model.device)
                priming = "completed"
            except InterruptedError:
                priming = "yielded"
            except torch.cuda.OutOfMemoryError:
                self.graphs.clear()
                priming = "memory-limited"
            finally:
                self._step_hook = None
            if priming == "memory-limited":
                torch.cuda.empty_cache()

        mode = "serial" if len(args.line) > 1 else "single"
        waveforms = None
        self._last_graph_used = False
        self._step_hook = check_interrupt if cancel_file is not None else None
        try:
            with opening_priority(device == "cuda" and getattr(args, "opening", False)) as boosted:
                with self.model.register_forward_pre_hook(check_interrupt) if cancel_file is not None else nullcontext():
                    if cancel_file is not None and args.line:
                        check_interrupt()
                    if getattr(args, "batch", False) and self.batch_capable():
                        cpu_rng, gpu_rng = torch.get_rng_state(), torch.cuda.get_rng_state(device)
                        try:
                            waveforms = self._waveforms([sentence for sentence, _ in args.line], args)
                        except torch.cuda.OutOfMemoryError:
                            pass
                        if waveforms is None:
                            self.graphs.clear()
                            torch.cuda.empty_cache()
                            torch.set_rng_state(cpu_rng)
                            torch.cuda.set_rng_state(gpu_rng, device)
                        else:
                            if len(waveforms) != len(args.line):
                                raise RuntimeError("批量生成返回了错误的音频数量")
                            mode = "batch"
                    for index, (sentence, output) in enumerate(args.line):
                        if cancel_file is not None:
                            check_interrupt()
                        waveform = self.generate_line(sentence, args) if waveforms is None else waveforms[index]
                        pcm = waveform.detach().float().cpu().numpy().reshape(-1)
                        if not pcm.size or not np.isfinite(pcm).all() or np.max(np.abs(pcm)) < 0.002:
                            raise RuntimeError("The generated waveform is empty, silent or non-finite.")
                        if getattr(args, "flow_smooth", False):
                            pcm = smooth_conversational_pauses(
                                pcm, self.model.sampling_rate, warm=getattr(args, "warm_voice", False)
                            )
                        path = Path(output)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        sf.write(path, pcm, self.model.sampling_rate, subtype="PCM_16")
        finally:
            self._step_hook = None
        decoder = self.model.audio_tokenizer.device.type if device == "cuda" else "cpu"
        return {"device": device, "decoder": decoder, "mode": mode, "cuda_graph": self._last_graph_used,
                "cuda_graph_buckets": len(self.graphs.entries), "batch_capable": self.batch_capable(),
                "warm": warm, "priming": priming, "opening_boost": boosted,
                "imports_s": IMPORT_SECONDS, "load_durations_s": durations,
                "load_seconds": round(load_seconds, 3),
                "generation_seconds": round(perf_counter() - start, 3), "steps": args.steps}


def run(args, session):
    if args.steps < 1:
        raise ValueError("The number of generation steps must be positive.")
    device = args.device
    fallback = None
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            fallback = "unavailable"
    elif device == "cuda" and not torch.cuda.is_available():
        raise SystemExit(69)
    with torch.inference_mode():
        try:
            info = session.generate(args, device)
        except torch.cuda.OutOfMemoryError:
            if device != "cuda":
                raise
            fallback = "out-of-memory"
    if fallback == "out-of-memory":
        # Drop both resident weights and the failed inference traceback before a retry.
        session.clear(release_cuda=True)
        if args.device != "auto":
            raise SystemExit(75)
        with torch.inference_mode():
            info = session.generate(args, "cpu")
    info["fallback"] = fallback
    return info


def load_room_renderer():
    started = perf_counter()
    from omni_room import apply_room
    return apply_room, perf_counter() - started


def worker_loop(args, session):
    background_room = os.environ.get("MOSS_SPEECH_OVERLAP_ROOM", "1") == "1"
    room_future = None
    for line in sys.stdin:
        request_id = None
        preload_refused = False
        try:
            request = json.loads(line)
            request_id = request.get("request_id")
            args.device = request["device"]
            args.steps = request["steps"]
            args.speed = request.get("speed", 1.0)
            args.warm_voice = request.get("warm_voice", False)
            args.flow_smooth = request.get("flow_smooth", False)
            args.opening = request.get("opening", False)
            args.prime = request.get("prime", True)
            args.batch = "lines" in request
            if args.batch:
                args.line = request["lines"]
                if len(args.line) != 2:
                    raise ValueError("需要两个播报文本段")
            else:
                args.line = [] if request.get("prepare") else [(request["text"], request["output"])]
            args.cancel_file = Path(request["cancel_file"]) if "cancel_file" in request else None
            if args.cancel_file and args.cancel_file.is_file():
                raise InterruptedError()
            if minimum := request.get("cuda_min_free_mib"):
                if not torch.cuda.is_available():
                    preload_refused = True
                    raise SystemExit(69)
                if torch.cuda.mem_get_info()[0] < minimum * 2**20:
                    preload_refused = True
                    raise SystemExit(75)
            room_amount = request.get("room_amount", 100 if request.get("room") else 0)
            if type(room_amount) is not int or not 0 <= room_amount <= 150:
                raise ValueError("room_amount must be an integer between 0 and 150")
            render_room = room_amount > 0 and args.line
            if render_room and background_room and session.device == "cuda" and args.device != "cpu":
                # Overlap the import with GPU generation after the weights are loaded.
                with_room = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moss-room-import")
                room_future = with_room.submit(load_room_renderer)
                with_room.shutdown(wait=False)
                background_room = False
            reply = {"code": 0, "info": run(args, session)}
            if render_room:
                room_started = perf_counter()
                if args.cancel_file and args.cancel_file.is_file():
                    raise InterruptedError()
                try:
                    apply_room, room_import = room_future.result() if room_future else load_room_renderer()
                finally:
                    room_future = None
                reply["info"]["room_import_s"] = round(room_import, 3)
                reply["info"]["room_wait_s"] = round(perf_counter() - room_started, 3)
                for _, output in args.line:
                    if args.cancel_file and args.cancel_file.is_file():
                        raise InterruptedError()
                    raw = Path(output)
                    rendered = raw.with_suffix(".stereo.wav")
                    apply_room(raw, rendered, room_amount / 100)
                    rendered.replace(raw)
                reply["info"]["room_s"] = round(perf_counter() - room_started, 3)
        except InterruptedError:
            reply = {"code": 130}
        except SystemExit as error:
            reply = {"code": error.code}
            if preload_refused:
                reply["preload_refused"] = True
        except Exception as error:
            traceback.print_exc()
            reply = {"code": 1, "detail": str(error)}
        if request_id is not None:
            reply["request_id"] = request_id
        print(json.dumps(reply, ensure_ascii=False), file=reply_stream, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="Local OmniVoice snapshot")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--transcript", required=True, help="Exact words spoken in the reference")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--decoder", choices=("auto", "cpu"), default="auto",
                        help="Use GPU decoding when memory allows, or keep the audio codec on CPU")
    parser.add_argument("--language", choices=("zh", "auto"), default="zh")
    parser.add_argument("--steps", type=int, default=32)
    parser.add_argument("--load-threads", type=int, choices=(1, 2), default=1,
                        help="Windows CPU threads used only while loading weights")
    parser.add_argument("--direct-cuda-load", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prompt-cache", type=Path, help="Reuse encoded reference audio between runs")
    parser.add_argument("--metadata", type=Path, help="Write device and timing information")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--line", nargs=2, action="append", metavar=("TEXT", "OUTPUT_WAV"))
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    torch.set_num_threads(min(torch.get_num_threads(), 4))
    session = VoiceSession()
    if args.worker:
        worker_loop(args, session)
        return
    info = run(args, session)
    print(json.dumps(info, ensure_ascii=False))
    if args.metadata:
        args.metadata.write_text(json.dumps(info), encoding="utf-8")


if __name__ == "__main__":
    main()

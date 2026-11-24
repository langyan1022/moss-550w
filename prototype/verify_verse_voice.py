import argparse
import asyncio
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from time import perf_counter
from urllib.parse import urlsplit

import aiohttp
import numpy as np
import soundfile as sf

from browser_lab import BrowserServer


ROOT = Path(__file__).resolve().parent
MODULE = ROOT / "static/speech-delivery.js"
POEMS = {
    "登鹳雀楼": "白日依山尽，黄河入海流。欲穷千里目，更上一层楼。",
    "早发白帝城": "《早发白帝城》\n唐·李白\n\n朝辞白帝彩云间，千里江陵一日还。\n两岸猿声啼不住，轻舟已过万重山。",
    "水调歌头上阕": "明月几时有？把酒问青天。不知天上宫阙，今夕是何年。我欲乘风归去，又恐琼楼玉宇，高处不胜寒。起舞弄清影，何似在人间。",
    "如梦令·昨夜雨疏风骤": "昨夜雨疏风骤，浓睡不消残酒。试问卷帘人，却道海棠依旧。知否，知否？应是绿肥红瘦。",
}
CREDITS = {
    "裸姓名": ("《静夜思》\n李白\n床前明月光，疑是地上霜。", "《静夜思》。作者是李白。"),
    "宋代方括号": ("《题西林壁》\n〔宋〕苏轼\n\n横看成岭侧成峰，远近高低各不同。", "《题西林壁》。作者是宋代的苏轼。"),
    "作者栏": ("《山居秋暝》\n作者：王维\n\n空山新雨后，天气晚来秋。", "《山居秋暝》。作者是王维。"),
    "无作者": ("《无题》\n白日依山尽，黄河入海流。", None),
}
NODE = """
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const {speechDelivery, speechSegments} = await import(pathToFileURL(process.argv[1]).href);
const {poems, prompts} = JSON.parse(readFileSync(0, 'utf8'));
const result = {};
for (const [name, text] of Object.entries(poems)) {
  const delivery = speechDelivery({status:'replied', user:prompts[name] || '请朗诵《'+name+'》', reply:text});
  result[name] = {category:delivery.category, reply:speechSegments(text, 'reply'),
                  verse:speechSegments(delivery.text, delivery.category)};
}
process.stdout.write(JSON.stringify(result));
"""


def frontend_plans(texts, prompts=None):
    run = subprocess.run(["node", "--input-type=module", "-e", NODE, str(MODULE)],
                         input=json.dumps({"poems": texts, "prompts": prompts or {}}, ensure_ascii=False), encoding="utf-8",
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return json.loads(run.stdout)


def segments():
    rows = frontend_plans(POEMS)
    for name, pair in rows.items():
        assert pair["category"] == "verse", (name, pair)
        for mode in ("reply", "verse"):
            for field in ("spoken", "caption"):
                joined = "".join(part[field] for part in pair[mode])
                if field == "caption":
                    assert re.sub(r"\s+", "", joined) == re.sub(r"\s+", "", POEMS[name]), (name, mode, field, joined)
                else:
                    if mode == "verse" and name == "早发白帝城":
                        assert pair[mode][0]["spoken"] == "《早发白帝城》。作者是唐代的李白。"
                    else:
                        assert re.findall(r"[\u3400-\u9fff]", joined) == re.findall(r"[\u3400-\u9fff]", POEMS[name])
                    body = POEMS[name].split("\n\n")[-1]
                    assert re.sub(r"\s+", "", joined).endswith(re.sub(r"\s+", "", body)), (name, mode, joined)
        if name in {"水调歌头上阕", "如梦令·昨夜雨疏风骤"}:
            assert "".join(s["spoken"] for s in pair["verse"]) == POEMS[name]
        else:
            utterance = "".join(s["spoken"] for s in pair["verse"])
            assert len(re.findall(r"[\u3400-\u9fff] [\u3400-\u9fff]", utterance)) == 4
    forms = frontend_plans({name:sample for name, (sample, _) in CREDITS.items()})
    for name, (sample, spoken) in CREDITS.items():
        rendered = forms[name]["verse"]
        assert re.sub(r"\s+", "", "".join(p["caption"] for p in rendered)) == re.sub(r"\s+", "", sample)
        assert "作者是" not in "".join(s["spoken"] for s in forms[name]["reply"])
        if spoken is None:
            assert "作者是" not in "".join(p["spoken"] for p in rendered)
        else:
            assert rendered[0]["spoken"] == spoken
            assert re.sub(r"\s+", "", "".join(p["spoken"] for p in rendered[1:])) == sample.splitlines()[-1]
    return rows, forms


def acoustic(samples, rate):
    """帧级响度门限和归一化自相关估计。基频受气声、低音和混响影响。"""
    mono = samples.mean(axis=1)
    hop = round(.010 * rate)
    width = round(.050 * rate)
    low, high = math.ceil(rate/420), math.floor(rate/55)
    nfft = 1 << (2*width - 1).bit_length()
    energies = []
    pitches = []
    for start in range(0, len(mono) - width + 1, hop):
        frame = mono[start:start+width].astype(np.float64)
        frame -= frame.mean()
        power = np.square(frame)
        rms = float(np.sqrt(power.mean()))
        db = 20 * math.log10(max(rms, 1e-8))
        energies.append(db)
        if db < -45:
            continue
        spectrum = np.fft.rfft(frame, nfft)
        corr = np.fft.irfft(spectrum.real**2 + spectrum.imag**2, nfft)
        cumulative = np.concatenate(([0.0], power.cumsum()))
        lags = np.arange(low-1, high+2)
        den = np.sqrt(np.maximum(1e-12, cumulative[width-lags] * (cumulative[-1]-cumulative[lags])))
        score = corr[lags] / den
        peaks = np.flatnonzero((score[1:-1] > score[:-2]) & (score[1:-1] >= score[2:])) + 1
        if len(peaks):
            credible = peaks[score[peaks] >= max(.70, float(score[peaks].max())*.95)]
            if len(credible):
                pitches.append(float(rate / lags[credible[0]]))
    assert len(pitches) >= 12, "可估计的有声帧不足"
    gate = max(-47.0, float(np.percentile(energies, 90)) - 32)
    activity = np.flatnonzero(np.array(energies) >= gate)
    gaps = np.diff(activity) - 1
    pauses = [round(float(gap * hop / rate), 3) for gap in gaps if gap * hop / rate >= .12]
    quantiles = np.percentile(pitches, [10, 50, 90])
    return {"duration_s":round(len(samples)/rate,3), "rate_hz":rate, "frames":len(energies),
            "voiced_frames":len(pitches), "f0_p10_p50_p90_hz":[round(float(x),1) for x in quantiles],
            "f0_p90_to_p10_semitones":round(12*math.log2(quantiles[-1]/quantiles[0]),2),
            "pause_gate_dbfs":round(gate,1), "interior_quiet_intervals_s":pauses,
            "clipped_sample_fraction":round(float((np.abs(samples)>=.9999).mean()),5)}


async def run(url, expected_pid, title, folder, baseline):
    address = urlsplit(url)
    assert address.scheme == "http" and address.hostname == "127.0.0.1" and address.port != 18221
    plans, credits = segments()
    earlier = json.loads((baseline/"result.json").read_text(encoding="utf-8")) if baseline else None
    report = {"passed":False, "plans":plans, "credit_plans":credits, "acoustic":{}, "requests":[], "folder":str(folder),
              "method":"前端分段，串行真实 HTTP。同一模式的 PCM 按返回次序拼接，未加停顿或调音。"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as client:
            async with client.get(url+"/api/health") as response:
                response.raise_for_status()
                health = await response.json()
            assert health["app"] == "moss-local-chat" and health["pid"] == expected_pid, health["pid"]
            async with client.get(url+"/static/speech-delivery.js") as response:
                response.raise_for_status()
                served = await response.read()
            assert served == MODULE.read_bytes()
            report.update(server_pid=expected_pid, delivery_sha256=hashlib.sha256(served).hexdigest())
            for name in title:
                report["acoustic"][name] = {}
                for mode in ("reply", "verse"):
                    if mode == "reply" and earlier:
                        assert earlier["passed"] and earlier["plans"][name][mode] == plans[name][mode]
                        clip = earlier["acoustic"][name][mode]
                        src, dest = baseline/clip["file"], folder/clip["file"]
                        shutil.copyfile(src, dest)
                        assert src.read_bytes() == dest.read_bytes()
                        report["acoustic"][name][mode] = {**clip, "copied_from":str(src), "sha256":hashlib.sha256(src.read_bytes()).hexdigest()}
                        report["requests"].extend({**r, "copied_from":str(baseline)} for r in earlier["requests"]
                                                  if r["poem"] == name and r["category"] == mode)
                        continue
                    streams = []
                    rates = set()
                    for part in plans[name][mode]:
                        started = perf_counter()
                        async with client.post(url+"/api/speak", json={"text":part["spoken"], "category":mode}) as response:
                            body = await response.read()
                            if response.status != 200:
                                raise RuntimeError(f"{mode} {response.status} {body[:300]!r}")
                        pcm, rate = sf.read(io.BytesIO(body), dtype="float32", always_2d=True)
                        streams.append(pcm)
                        rates.add(rate)
                        report["requests"].append({"poem":name, "category":mode, "text":part["spoken"], "sha256":hashlib.sha256(body).hexdigest(),
                                                   "wall_s":round(perf_counter()-started,3), "samples":len(pcm)})
                    assert len(rates) == 1 and streams
                    pcm = np.concatenate(streams)
                    filename = f"{name}-{'普通朗读' if mode == 'reply' else '朗诵'}.wav"
                    sf.write(folder/filename, pcm, rate, subtype="PCM_16")
                    report["acoustic"][name][mode] = {**acoustic(pcm, rate), "file":filename}
            report["credit_audio"] = {}
            report["ci_audio"] = {}
            ci = "如梦令·昨夜雨疏风骤"
            extra = [(key, credits[key]["verse"][0], "credit_audio", "作者-" + key) for key in ("裸姓名", "宋代方括号")]
            assert len(plans[ci]["verse"]) == 1 and "知否，知否？" in plans[ci]["verse"][0]["spoken"]
            extra.append((ci, plans[ci]["verse"][0], "ci_audio", ci + "-朗诵"))
            for key, part, section, stem in extra:
                async with client.post(url+"/api/speak", json={"text":part["spoken"], "category":"verse"}) as response:
                    response.raise_for_status()
                    data = await response.read()
                file = folder / (stem + ".wav")
                file.write_bytes(data)
                report[section][key] = {**part, "file":file.name, "sha256":hashlib.sha256(data).hexdigest()}
            report["passed"] = True
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)


async def verify(args):
    folder = ROOT / "runs" / datetime.now().strftime("verse-voice-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    if args.isolated_port:
        assert args.isolated_port != 18221
        server = BrowserServer(folder, args.isolated_port, model_url="http://127.0.0.1:9/v1")
        try:
            await server.start()
            await run(server.url, server.pid, args.poems, folder, args.baseline)
        finally:
            await server.stop()
    else:
        await run(args.url.rstrip("/"), args.pid, args.poems, folder, args.baseline)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--url", help="正在运行的隔离 MOSS HTTP 地址")
    mode.add_argument("--isolated-port", type=int, help="从空数据目录启动隔离服务，完成后关闭")
    mode.add_argument("--text-only", action="store_true")
    parser.add_argument("--pid", type=int, help="指定隔离服务的 PID")
    parser.add_argument("--baseline", type=Path, help="复用已验证的普通朗读 WAV，按字节核对")
    parser.add_argument("--poems", nargs="+", choices=POEMS, default=list(POEMS)[:2])
    args = parser.parse_args()
    if args.text_only:
        print(json.dumps(segments(), ensure_ascii=False, indent=2))
    elif args.isolated_port or args.url and args.pid:
        asyncio.run(verify(args))
    else:
        parser.error("请选择 --text-only 或指定 --url 和 --pid")

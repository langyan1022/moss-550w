import asyncio
import base64
import io
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
import sys

from .library import MediaLibrary
from .subtitles import EMBEDDED_FORMATS, excerpt, search_text


INTERVAL = 2
SAMPLES = 6
FORMATS = {**EMBEDDED_FORMATS, ".avi": "avi", ".wmv": "asf", ".mpeg": "mpeg", ".mpg": "mpeg",
           ".ts": "mpegts", ".m2ts": "mpegts", ".flv": "flv"}
SEARCH_LIMIT = asyncio.Semaphore(1)


class BoundedReader:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = 48 * 1024 * 1024

    def read(self, size):
        if size < 0 or size > self.remaining:
            raise ValueError("读取量已达到单次画面查询上限。")
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data

    def seek(self, offset, whence=0):
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()


def decode_samples(db, media_id, start):
    import av

    library = MediaLibrary(db, read_only=True)
    version = library.video_version(media_id)
    item, source = library.open_record(media_id, kinds={"video"})
    with source:
        fmt = FORMATS.get(Path(item["path"]).suffix.lower())
        if not fmt:
            raise ValueError("当前视频格式不支持画面取样。")
        options = {"protocol_whitelist": "pipe", "enable_drefs": "0", "probesize": "2097152", "analyzeduration": "2000000"}
        with av.open(BoundedReader(source), format=fmt, options=options) as video:
            track = next((s for s in video.streams.video if not s.disposition & av.stream.Disposition.attached_pic), None)
            if track is None:
                raise ValueError("文件中没有可读取的视频画面。")
            track.codec_context.thread_count = 1
            track.codec_context.options = {"max_pixels": str(4096 * 2304)}
            if track.codec_context.width * track.codec_context.height > 4096 * 2304:
                raise ValueError("视频分辨率超过本机画面查询上限。")
            duration = float(track.duration * track.time_base) if track.duration else (video.duration or 0) / av.time_base
            if not isfinite(duration) or duration <= 0:
                raise ValueError("无法读取这段视频的时长。")
            end = min(duration, start + INTERVAL * SAMPLES)
            frames = []
            if start < end:
                origin = track.start_time or 0
                if start:
                    video.seek(origin + int(start / track.time_base), stream=track, backward=True)
                target = start
                for i, frame in enumerate(video.decode(track)):
                    if i >= 1200:
                        raise ValueError("解码帧数已达到单次画面查询上限。")
                    if frame.pts is None:
                        continue
                    seconds = float(frame.pts * frame.time_base - origin * track.time_base)
                    if seconds >= end:
                        break
                    if seconds + .00001 < target:
                        continue
                    scale = min(2, 1440 / frame.width, 960 / frame.height)
                    image = frame.reformat(width=max(1, round(frame.width * scale)),
                                           height=max(1, round(frame.height * scale)), format="gray").to_image().convert("L")
                    png = io.BytesIO()
                    image.save(png, format="PNG", compress_level=1)
                    frames.append([round(seconds, 3), base64.b64encode(png.getvalue()).decode("ascii")])
                    target = start + INTERVAL * (int((seconds + .00001 - start) // INTERVAL) + 1)
                    if target >= end:
                        break
    if library.video_version(media_id) != version:
        raise ValueError("视频在画面取样期间发生变化，请重新查找。")
    return {"start_s": start, "end_s": end, "duration_s": duration, "interval_s": INTERVAL,
            "version": version, "frames": frames}


async def exchange(command, *, data=None, timeout):
    proc = await asyncio.create_subprocess_exec(
        *command, cwd=Path(__file__).resolve().parents[1], stdin=asyncio.subprocess.PIPE if data is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        async with asyncio.timeout(timeout):
            output, error = await proc.communicate(data)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
    return proc.returncode, output, error


async def recognize_frames(frames):
    from context_tools import OCR_LIMIT

    if not frames:
        return []
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = Path(__file__).resolve().parents[1] / "screen_ocr.ps1"
    payload = "\n".join(png for _, png in frames).encode("ascii")
    async with OCR_LIMIT:
        status, output, error = await exchange([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                                "-File", str(script), "-Batch"], data=payload, timeout=25)
    if status:
        if b"MOSS_OCR_LANGUAGE" in error:
            raise RuntimeError("请在 Windows 语言设置中安装文字识别语言包。")
        raise RuntimeError("本机 Windows 文字识别未能处理这些视频画面。")
    texts = [json.loads(line) for line in output.decode("utf-8-sig").splitlines()]
    if len(texts) != len(frames) or not all(isinstance(t, str) for t in texts):
        raise RuntimeError("Windows 文字识别返回的画面数量不符。")
    return [re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])", "", t) for t in texts]


async def search(library, item, query, start):

    needle = search_text(query)
    async with asyncio.timeout(40), SEARCH_LIMIT:
        command = [sys.executable, "-B", "-X", "utf8", "-m", "local_media.frame_text", str(library.db_path),
                   item["media_id"], str(start)]
        status, output, _ = await exchange(command, timeout=15)
        if status:
            raise RuntimeError("视频解码程序未能完成取样。")
        result = json.loads(output)
        if "error" in result:
            raise ValueError(result["error"])
        frames = result.pop("frames")
        version = result.pop("version")
        result["sampled"] = len(frames)
        if not frames and start < result["duration_s"]:
            raise ValueError("在这段时间内没有取得可识别的视频帧。")
        items, previous = [], ""
        texts = await recognize_frames(frames)
        for (seconds, _), recognized in zip(frames, texts, strict=True):
            line = next((line for line in recognized.splitlines() if needle in search_text(line)), "")
            if line and line != previous:
                text = excerpt(line, needle)
                sample = {"kind": "frame_text", "start_s": seconds, "end_s": min(seconds + INTERVAL, result["duration_s"]),
                          "text": text, "excerpt": text, "version": version}
                items.append({**item, "subtitle": sample})
            previous = line
        if await asyncio.to_thread(library.video_version, item["media_id"]) != version:
            raise ValueError("视频在文字识别期间发生变化，请重新查找。")
        return items, result


if __name__ == "__main__":
    try:
        result = decode_samples(sys.argv[1], sys.argv[2], float(sys.argv[3]))
    except (OSError, ValueError, RuntimeError) as error:
        result = {"error": str(error) if type(error) is ValueError else "无法从此视频解码画面，请检查文件和媒体来源。"}
    print(json.dumps(result, ensure_ascii=True))

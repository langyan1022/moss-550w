import argparse
import json
import mimetypes
from pathlib import Path
import re
import sys

from .library import DEFAULT_CONFIG, DEFAULT_DB, EXTENSIONS, MediaLibrary, SourceUnavailableError, audio_header


def stream_audio(lib, media_id, requested):
    try:
        item, opened = lib.open_record(media_id, kinds={"audio"})
    except (FileNotFoundError, PermissionError, SourceUnavailableError) as error:
        status = 404 if isinstance(error, FileNotFoundError) else 403 if isinstance(error, PermissionError) else 503
        print(json.dumps({"status":status}),flush=True)
        return
    with opened as source:
        size = source.seek(0, 2)
        start, end = 0, size - 1
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if match and (match[1] or match[2]):
                if match[1]:
                    start = int(match[1])
                    end = min(end, int(match[2])) if match[2] else end
                else:
                    start = max(0, size - int(match[2]))
            if not match or not (match[1] or match[2]) or start > end or start >= size:
                print(json.dumps({"status": 416, "size": size}), flush=True)
                return
        mime = mimetypes.guess_type(item["path"])[0] or "audio/octet-stream"
        print(json.dumps({"status": 206 if requested else 200, "size": size, "start": start, "end": end,
                          "mime": mime}), flush=True)
        source.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = source.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["scan", "status", "search", "resolve", "stream", "inspect"])
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--kind", choices=EXTENSIONS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--range", default="")
    args = parser.parse_args()
    lib = MediaLibrary(args.db)
    if args.config or (not lib.sources() and args.db == DEFAULT_DB):
        cfg_path = args.config or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else DEFAULT_CONFIG.with_name("sources.example.json"))
        if cfg_path.exists():
            lib.configure(json.loads(cfg_path.read_text(encoding="utf-8")))
    if args.command == "stream":
        stream_audio(lib, args.query, args.range)
        return
    if args.command == "scan":
        result = {"sources": lib.scan(args.query or None)}
    elif args.command == "status":
        result = {"sources": lib.sources()}
    elif args.command == "inspect":
        item = lib.resolve(args.query)
        result = {"status": "unavailable"}
        if item and item["kind"] == "audio":
            with lib.open_media(args.query) as source:
                audio = audio_header(source)
                if audio:
                    result = {"status": "ok", "title": item["title"], "artist": item["artist"],
                              "format": audio["format"], "seconds": round(audio["duration"], 3),
                              "sample_rate": audio["sample_rate"], "channels": audio["channels"]}
    elif args.command == "resolve":
        result = lib.route("", selected=args.query)
    else:
        result = lib.route(args.query, kind=args.kind, limit=args.limit, online=args.online)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Project completed backend transitions to the two local runtime displays."""

import asyncio
from collections import Counter, deque
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from time import time


PHASES = {
    "thinking": "正在处理消息",
    "pending": "操作等待确认",
    "saving": "正在执行已确认的操作",
    "player_requested": "等待播放器回执",
    "replied": "本轮完成",
    "cancelled": "本轮已取消",
    "saved": "草稿已保存",
    "error": "本轮未完成",
}
READINGS = {
    "clock": "时钟", "battery": "电池", "volume": "系统音量", "memory": "内存",
    "cpu": "CPU", "disk": "磁盘", "uptime": "开机时长", "weather": "天气",
    "calendar": "本机日程", "reminders": "提醒", "system_media": "系统播放器", "mcp": "MCP",
}
TOOLS = {
    "get_time": "时钟与世界时区", "get_volume": "系统音量", "list_timers": "提醒",
    "list_capabilities": "功能查询", "get_weather": "天气",
    "list_media_catalog": "媒体目录", "web_search": "实时联网搜索", "get_news_headlines": "实时新闻头条",
    "fetch_web_page": "抓取网页正文", "calculate_math": "精确数学计算", "calculate_date": "日期星期计算",
    "read_local_file": "读取本地文件", "list_local_directory": "查看本地目录",
    "save_note": "写入便签", "search_notes": "查询便签", "manage_note": "整理便签",
    "save_user_memory": "写入长期记忆", "search_user_memory": "检索长期记忆", "forget_user_memory": "遗忘长期记忆",
    "create_timer": "新建提醒", "reschedule_timer": "改期提醒", "cancel_timer": "取消提醒",
    "get_device_status": "本机硬件遥测",
    "query_calendar": "查询日程", "create_calendar_event": "新建日程", "cancel_calendar_event": "取消日程",
    "record_self_lesson": "自我纠错学习", "review_self_lessons": "RSI 复盘迭代",
    "search_knowledge": "SAG 知识图谱检索", "ingest_knowledge": "SAG 知识入库",
    "get_situational_briefing": "态势简报", "consolidate_world_model": "夜间记忆整合",
    "submit_background_job": "后台异步任务", "render_fui_widget": "生成交互 FUI", "save_custom_skill": "沉淀专属技能",
    "ha_list_devices": "智能家居状态", "ha_control_device": "Home Assistant 控制", "control_physical_moss": "实体 550W 硬件动作",
    "search_media": "媒体检索", "search_tracks": "媒体检索", "inspect_audio": "音频读取",
    "play_audio": "媒体播放", "play_song": "媒体播放",
}
RECEIPTS = {
    "receipt": "已核实草稿写入回执",
    "calendar_receipt": "已收到日程操作回执",
    "file_move_receipt": "已收到文件归类回执",
    "jev_contact_receipt": "已收到本机草稿交接回执",
}
PLAYBACK = {
    "started": "播放器已开始播放",
    "blocked": "播放器回执：自动播放受阻",
    "failed": "播放器回执：播放失败",
}
ACTIVE = {"thinking", "pending", "saving", "player_requested"}


def _cloud_count(raw):
    if not raw:
        return 0
    counts = json.loads(raw).get("by_provider", {})
    return sum(count for count in counts.values() if type(count) is int and count > 0)


class RuntimeLog:
    def __init__(self, db, *, on_emit=None, has_observers=None):
        self.db = db
        self.on_emit = on_emit
        self.has_observers = has_observers
        self.readers = 0
        self.stream_id = secrets.token_hex(8)
        self.sequence = 0
        self.entries = deque(maxlen=96)
        self.chat = self.turn = self.status = None
        self.steps = set()
        self.cloud_count = self.partial_count = 0
        self.partial_stamp = None

    def attach(self, last_event_id):
        self.readers += 1
        if self.readers == 1:
            self.capture()
        prefix, _, seq = last_event_id.partition(":")
        if prefix == self.stream_id and seq.isascii() and seq.isdigit() and len(seq) < 16:
            cursor = int(seq)
            if cursor <= self.sequence:
                return cursor
        return max(0, self.sequence - 1)

    def detach(self):
        self.readers -= 1

    def pending(self, after):
        return tuple(entry for entry in self.entries if entry[0] > after)

    def _emit(self, text, *, error=False):
        self.sequence += 1
        ts = int(time() * 1000)
        payload = {"time": ts, "text": text}
        if error:
            payload["error"] = True
        encoded = json.dumps(payload, ensure_ascii=False)
        frame = f"id: {self.stream_id}:{self.sequence}\ndata: {encoded}\n\n".encode("utf-8")
        self.entries.append((self.sequence, frame))
        if self.on_emit is not None:
            self.on_emit(text, source="CORE", level="warn" if error else "info", error=error, ts=ts)

    def _once(self, key, text, *, error=False):
        if key not in self.steps:
            self.steps.add(key)
            self._emit(text, error=error)

    def _phase(self, status):
        if status != self.status:
            self.status = status
            if status in PHASES:
                self._emit(PHASES[status], error=status == "error")

    def capture(self):
        if not self.readers and not (self.has_observers and self.has_observers()):
            return
        try:
            chat, turn, raw, requests = self.db.execute("""
                SELECT state.active_chat, latest.id, latest.doc, latest.cloud_requests
                FROM state LEFT JOIN turns AS latest ON latest.rowid = (
                    SELECT rowid FROM turns WHERE chat_id=state.active_chat ORDER BY rowid DESC LIMIT 1)
                WHERE state.id=1
            """).fetchone()
            doc = json.loads(raw) if raw else {}
            cloud_count = _cloud_count(requests)
        except (sqlite3.Error, TypeError, ValueError, AttributeError):
            return

        status = doc.get("status")
        switched = chat != self.chat
        if switched or turn != self.turn:
            self.chat, self.turn = chat, turn
            self.status = None
            self.steps.clear()
            self.cloud_count = self.partial_count = 0
            self.partial_stamp = None
        if switched:
            self.entries.clear()
            if not turn:
                self._emit("本机连接已就绪")
                return
        if status in ACTIVE:
            self._phase(status)

        if cloud_count > self.cloud_count:
            self.cloud_count = cloud_count
            self._emit(f"已发起云端模型请求（本轮第 {cloud_count} 次）")

        for index, tool in enumerate(doc.get("holo_tools") or ()):
            label = TOOLS.get(tool)
            if label:
                self._once(("tool", index, tool), f"已调用工具 · {label}")

        readings = doc.get("local_overview") or doc.get("local_overview_partial") or ()
        kinds = Counter((row.get("kind"), row.get("status") == "error") for row in readings
                        if isinstance(row, dict) and row.get("kind") in READINGS)
        for (kind, failed), count in kinds.items():
            for index in range(count):
                label = READINGS[kind]
                text = f"未能读取 · {label}" if failed else f"已收到处理结果 · {label}"
                self._once(("reading", kind, failed, index), text, error=failed)

        partial = doc.get("partial_reply")
        if status == "thinking" and not readings and isinstance(partial, str) and partial:
            stamp = hash(partial)
            if stamp != self.partial_stamp:
                self.partial_stamp = stamp
                self.partial_count += 1
                self._emit(f"正在接收回复（第 {self.partial_count} 次更新）")

        for key, text in RECEIPTS.items():
            if doc.get(key):
                self._once(key, text)

        media = doc.get("media")
        if isinstance(media, dict) and media:
            items = media.get("items") or ()
            count = len(items)
            self._once(("media", count), f"媒体结果已返回（{count} 项）")
            selected = media.get("selected")
            if media.get("autoplay") and isinstance(selected, dict) and selected.get("media_id"):
                self._once(("autoplay", selected["media_id"]), "已请求播放器启动")

        playback = doc.get("browser_playback") or {}
        event = playback.get("event")
        if event in PLAYBACK:
            self._once(("playback", playback.get("play_id"), event), PLAYBACK[event], error=event != "started")

        if status not in ACTIVE:
            self._phase(status)


LOG_ROTATE_BYTES = 2 * 1024 * 1024


class BackendLogHub:
    """Real-time backend operational log stream + real log-file tailer for MOSS FUI displays."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.data_dir / "moss-runtime.log"
        self.config_file = self.data_dir / "runtime-log-source.json"
        self.stream_id = secrets.token_hex(8)
        self.sequence = 0
        self.entries = deque(maxlen=256)
        self.subscribers = set()
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tail_offsets = {}
        self.extra_path: Path | None = None
        self._load_source_config()
        self._seed_from_own_log()

    def _load_source_config(self):
        env_path = (os.environ.get("MOSS_EXTRA_LOG_FILE") or "").strip()
        saved_path = ""
        if self.config_file.is_file():
            try:
                saved_path = str(json.loads(self.config_file.read_text(encoding="utf-8")).get("path") or "").strip()
            except (OSError, ValueError, TypeError):
                saved_path = ""
        chosen = saved_path or env_path
        if chosen:
            candidate = Path(chosen).expanduser()
            if candidate.is_file():
                self.extra_path = candidate.resolve()
                self._prime_tail(self.extra_path)

    def _seed_from_own_log(self):
        if not self.log_file.is_file():
            return
        try:
            raw = self.log_file.read_bytes()[-12288:]
            lines = raw.decode("utf-8", errors="replace").splitlines()[-18:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                self._append_frame(line, source="LOG", level="warn" if "[WARN]" in line or "[ERR]" in line else "info",
                                   error="[ERR]" in line, ts=int(time() * 1000))
        except OSError:
            pass

    def _prime_tail(self, path: Path):
        try:
            size = path.stat().st_size
            # Start near tail so attaching a large log immediately shows its latest 12 lines.
            self._tail_offsets[str(path)] = max(0, size - 4096)
        except OSError:
            self._tail_offsets.pop(str(path), None)

    def source_info(self):
        qwen_log = self.data_dir / "qwen.log"
        return {
            "runtime_log": str(self.log_file),
            "qwen_log": str(qwen_log) if qwen_log.is_file() else None,
            "extra_path": str(self.extra_path) if self.extra_path else "",
            "extra_exists": bool(self.extra_path and self.extra_path.is_file()),
            "entries": self.sequence,
        }

    def set_extra_source(self, raw_path: str):
        cleaned = (raw_path or "").strip()
        if not cleaned:
            if self.extra_path:
                self._tail_offsets.pop(str(self.extra_path), None)
            self.extra_path = None
            try:
                self.config_file.write_text(json.dumps({"path": ""}, ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError:
                pass
            self.emit("已切换回 MOSS 本机运行日志流。", source="LOG", level="info")
            return self.source_info()
        candidate = Path(cleaned).expanduser()
        if not candidate.is_file():
            raise ValueError(f"找不到可读取的日志文件：{cleaned}")
        resolved = candidate.resolve()
        try:
            with resolved.open("rb") as fh:
                fh.read(1)
        except OSError as exc:
            raise ValueError(f"无法读取日志文件：{exc}") from exc
        self.extra_path = resolved
        self._prime_tail(resolved)
        try:
            self.config_file.write_text(json.dumps({"path": str(resolved)}, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass
        self.emit(f"已接入真实后台日志文件：{resolved.name}", source="LOG", level="ok")
        self.poll_files()
        return self.source_info()

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is not None:
            self._loop = loop

    def subscribe(self, sig: asyncio.Event):
        with self._lock:
            self.subscribers.add(sig)

    def unsubscribe(self, sig: asyncio.Event):
        with self._lock:
            self.subscribers.discard(sig)

    def snapshot_subscribers(self) -> tuple[asyncio.Event, ...]:
        with self._lock:
            return tuple(self.subscribers)

    def _append_frame(self, text: str, *, source="SYS", level="info", error=False, ts=None, file_tag=None):
        stamp = int(ts if ts is not None else time() * 1000)
        payload = {"time": stamp, "source": source, "level": level, "text": text}
        if error:
            payload["error"] = True
        if file_tag:
            payload["file"] = file_tag
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self.sequence += 1
            frame = f"id: {self.stream_id}:{self.sequence}\ndata: {encoded}\n\n".encode("utf-8")
            self.entries.append((self.sequence, frame))
            subs = list(self.subscribers)
            loop = self._loop
        try:
            cur_loop = asyncio.get_running_loop()
        except RuntimeError:
            cur_loop = None
        if loop is None and cur_loop is not None:
            loop = self._loop = cur_loop
        for sig in subs:
            if loop is not None and cur_loop is not loop and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(sig.set)
                except RuntimeError:
                    pass
            else:
                sig.set()

    def emit(self, text: str, *, source="SYS", level="info", error=False, ts=None, write_file=True, file_tag=None):
        clean = " ".join(str(text or "").splitlines()).strip()
        if not clean:
            return
        if len(clean) > 220:
            clean = clean[:217] + "..."
        stamp = int(ts if ts is not None else time() * 1000)
        if write_file:
            with self._lock:
                try:
                    if self.log_file.is_file() and self.log_file.stat().st_size > LOG_ROTATE_BYTES:
                        backup = self.log_file.with_suffix(".log.1")
                        self.log_file.replace(backup)
                    clock = datetime.fromtimestamp(stamp / 1000).strftime("%H:%M:%S.%f")[:-3]
                    tag = "ERR" if error else level.upper()
                    with self.log_file.open("a", encoding="utf-8") as fh:
                        fh.write(f"{clock} [{source}] [{tag}] {clean}\n")
                except OSError:
                    pass
        self._append_frame(clean, source=source, level=level, error=error, ts=stamp, file_tag=file_tag)

    @staticmethod
    def _read_tail_chunks_io(
        targets: list[tuple[Path, str]],
        offsets: dict[str, int],
    ) -> tuple[dict[str, int], list[tuple[str, str, str, bool, str]]]:
        """Read new tail chunks from monitored log files off the asyncio event loop."""
        next_offsets = dict(offsets)
        emitted: list[tuple[str, str, str, bool, str]] = []
        for path, source in targets:
            key = str(path)
            try:
                if not path.is_file():
                    continue
                st = path.stat()
            except OSError:
                continue
            prev = next_offsets.get(key)
            if prev is None:
                next_offsets[key] = st.st_size
                continue
            if st.st_size < prev:
                prev = 0
            if st.st_size == prev:
                continue
            try:
                with path.open("rb") as fh:
                    fh.seek(prev)
                    chunk = fh.read(min(st.st_size - prev, 16384))
                    next_offsets[key] = fh.tell()
            except OSError:
                continue
            for raw_line in chunk.decode("utf-8", errors="replace").splitlines()[-24:]:
                line = raw_line.strip()
                if not line:
                    continue
                low = line.lower()
                is_err = any(tok in low for tok in ("error", "traceback", "exception", "fatal", "crit", "failed"))
                is_warn = not is_err and any(tok in low for tok in ("warn", "timeout", "retry", "slow"))
                level = "crit" if is_err else "warn" if is_warn else "info"
                emitted.append((line[:220], source, level, is_err, path.name))
        return next_offsets, emitted

    def _tail_targets(self) -> list[tuple[Path, str]]:
        targets: list[tuple[Path, str]] = [(self.data_dir / "qwen.log", "QWEN")]
        if self.extra_path:
            targets.append((self.extra_path, "FILE"))
        return targets

    def _apply_tail_chunks(self, next_offsets: dict[str, int], emitted: list[tuple[str, str, str, bool, str]]):
        self._tail_offsets.update(next_offsets)
        for text, source, level, is_err, fname in emitted:
            self._append_frame(text, source=source, level=level, error=is_err, file_tag=fname)

    def poll_files(self):
        next_offsets, emitted = self._read_tail_chunks_io(self._tail_targets(), self._tail_offsets)
        self._apply_tail_chunks(next_offsets, emitted)

    async def poll_files_async(self):
        self.bind_loop()
        targets = self._tail_targets()
        if not targets:
            return
        next_offsets, emitted = await asyncio.to_thread(self._read_tail_chunks_io, targets, dict(self._tail_offsets))
        self._apply_tail_chunks(next_offsets, emitted)

    def attach(self, last_event_id: str):
        prefix, _, seq = (last_event_id or "").partition(":")
        with self._lock:
            if prefix == self.stream_id and seq.isascii() and seq.isdigit() and len(seq) < 16:
                cursor = int(seq)
                if cursor <= self.sequence:
                    return cursor
            return max(0, self.sequence - 24)

    def pending(self, after: int):
        with self._lock:
            return tuple(entry for entry in self.entries if entry[0] > after)



"""Media context, playback receipt validation & failed-track recovery mixin for Moss."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event as ThreadEvent

from local_media.library import compact, song_title_key
from memory_store import music_preference, now


RESUME_READINGS = {
    "clock", "volume", "cpu", "memory", "battery", "disk", "uptime", "device",
    "reminders", "calendar", "weather",
}


class _MossMediaMixin:
    """Cohesive playback context, receipt tracking & failed-track recovery helpers for Moss."""

    def _media_context(self, chat_id=None):
        target_chat = chat_id or self.chat_id
        owner = self.chat_owner(target_chat) or self.DEFAULT_OWNER
        row = self.db.execute(
            """SELECT doc FROM turns WHERE chat_id=?
               AND json_type(doc,'$.media')='object' AND json_extract(doc,'$.status') NOT IN ('cancelled','error')
               ORDER BY rowid DESC LIMIT 1""",
            (target_chat,),
        ).fetchone()
        recent = json.loads(row[0]) if row else {}
        data = recent.get("media", {}) if self._memory_current(recent, owner_code=owner) else {}
        if data and recent.get("_music_preference_versions"):
            data["_music_preference_versions"] = recent["_music_preference_versions"]
        data.pop("browser_playback", None)
        if source := self._latest_started_turn(target_chat):
            receipt = source["browser_playback"]
            item = self.playback_item(source, receipt["media_id"])
            if item:
                data["browser_playback"] = {**receipt, "title": item["title"], "artist": item.get("artist")}
                if item.get("kind") == "audio" and item.get("source_kind") in {"local", "nas", "online"}:
                    data["_playback_audio"] = item
        return data or None

    def _latest_started_turn(self, chat):
        row = self.db.execute(
            """SELECT doc FROM turns WHERE chat_id=?
               AND json_extract(doc,'$.browser_playback.event')='started'
               ORDER BY julianday(json_extract(doc,'$.browser_playback.at')) DESC,
                        json_extract(doc,'$.browser_playback.at') DESC, rowid DESC LIMIT 1""",
            (chat,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _recent_audio(self, chat):
        rows = self.db.execute(
            """SELECT doc FROM turns WHERE chat_id=?
               AND json_extract(doc,'$.browser_playback.event')='started'
               ORDER BY julianday(json_extract(doc,'$.browser_playback.at')) DESC,
                        json_extract(doc,'$.browser_playback.at') DESC, rowid DESC LIMIT 24""",
            (chat,),
        )
        titles = {}
        for (doc,) in rows:
            turn = json.loads(doc)
            item = self.playback_item(turn, turn["browser_playback"]["media_id"]) or {}
            if item.get("kind") == "audio" and item.get("source_kind") in {"local", "nas", "online"}:
                title = song_title_key(item["title"])
                if title:
                    titles[title] = None
                if len(titles) == 5:
                    break
        return tuple(titles)

    def _music_preferences(self, owner_code=None):
        preferred = {}
        for item in sorted(self.list_memories(owner_code), key=lambda row: row["updated_at"], reverse=True):
            if item["source"] in {"chat", "panel"} and (artist := music_preference(item["text"])):
                if key := compact(artist):
                    preferred.setdefault(key, (artist, item))
        return preferred

    @staticmethod
    def playback_item(turn, media_id):
        if not media_id:
            return None
        selected = (turn.get("media") or {}).get("selected") or {}
        if selected.get("media_id") == media_id:
            return selected
        for receipt in reversed(turn.get("native_receipts") or ()):
            result = receipt.get("result") or {}
            if receipt.get("tool") == "play_media" and result.get("status") == "played" and result.get("media_id") == media_id:
                return result
            if receipt.get("tool") == "search_media" and result.get("selected_id") == media_id:
                return next((i for i in result.get("matches", []) if i.get("media_id") == media_id), None)
        return None

    def record_playback(self, chat, turn_id, media_id, event, play_id, started_at_us, *, owner_code=None):
        owner = self.normalize_owner(owner_code)
        self.require_chat_owner(chat, owner)
        if chat != self.active_chat_for(owner) or event not in ("started", "blocked", "failed"):
            raise ValueError("播放回执与当前对话不符")
        turn = self._get(turn_id)
        if media_id and (turn.get("media") or {}).get("recovered_from") == media_id:
            raise ValueError("播放器已改用同曲目的备用文件")
        if turn["chat_id"] != chat or not self.playback_item(turn, media_id):
            raise ValueError("此音轨与该轮已选择的音轨不符")
        if play_id is not None and (not isinstance(play_id, str) or len(play_id) > 64):
            raise ValueError("播放标识无效")
        previous = turn.get("browser_playback") or {}
        at = now()
        if started_at_us is not None:
            received = datetime.fromisoformat(at)
            if (event != "started" or not play_id or type(started_at_us) is not int
                    or not 0 < started_at_us <= int((received.timestamp() + 2) * 1_000_000)):
                raise ValueError("开播时间无效")
            started = datetime.fromtimestamp(started_at_us / 1_000_000, timezone.utc)
            if started < datetime.fromisoformat(turn["created_at"]) - timedelta(seconds=2):
                raise ValueError("开播时间早于该轮媒体请求")
            if previous.get("event") == "started" and started <= datetime.fromisoformat(previous["at"]):
                return
            at = started.isoformat(timespec="microseconds")
        restarted = event == "started" and play_id and play_id != previous.get("play_id")
        if restarted or previous.get("media_id") != media_id or previous.get("event") not in ("started", event):
            self._update(turn, browser_playback={"media_id": media_id, "event": event, "at": at, "play_id": play_id})
            if restarted:
                self._prepare_track_resume(chat, turn_id, media_id, play_id)

    def _recoverable_track(self, chat, turn_id, media_id):
        if self.closing:
            return None
        owner = self.chat_owner(chat)
        if owner is None or self.active_chat_for(owner) != chat:
            return None
        row = self.db.execute("SELECT doc FROM turns WHERE chat_id=? ORDER BY rowid DESC LIMIT 1", (chat,)).fetchone()
        turn = json.loads(row[0]) if row else {}
        if turn.get("id") == turn_id and self._is_recoverable_track(turn, media_id):
            return turn
        return None

    @staticmethod
    def _is_recoverable_track(turn, media_id):
        media = turn.get("media") or {}
        selected = media.get("selected") or {}
        return (turn.get("status") == "replied"
                and media.get("play_mode") == "track" and media.get("autoplay") and not media.get("recovered_from")
                and selected.get("media_id") == media_id and selected.get("kind") == "audio"
                and selected.get("source_kind") in {"local", "nas"} and bool(selected.get("artist")))

    def _current_playback_turn(self, chat, turn_id, media_id, play_id):
        if self.closing:
            return None
        owner = self.chat_owner(chat)
        if owner is None or self.active_chat_for(owner) != chat:
            return None
        turn = self._latest_started_turn(chat) or {}
        receipt = turn.get("browser_playback") or {}
        if turn.get("id") == turn_id and receipt.get("media_id") == media_id and receipt.get("play_id") == play_id:
            return turn
        return None

    @staticmethod
    def _is_resume_read(turn):
        if turn.get("status") != "replied" or any(turn.get(field) for field in (
                "media", "media_declined", "media_context_used", "transport", "desktop_confirmation",
                "system_media_pause", "mcp_request", "native_receipts")):
            return False
        if turn.get("source") == "holo":
            tools = turn.get("holo_tools") or ()
            return (bool(tools) and all(tool in ("get_time", "get_weather") for tool in tools)
                    and ("get_weather" not in tools or bool(turn.get("weather"))))
        if turn.get("source") == "context":
            readings = turn.get("local_overview") or ()
            return (bool(readings) and all(row.get("kind") in RESUME_READINGS
                        and (row["kind"] != "weather" or bool(turn.get("weather"))) for row in readings))
        return False

    def _can_resume_track(self, turn, chat, media_id):
        if not self._is_recoverable_track(turn, media_id) or self._has_active_turn(chat):
            return False
        newer = self.db.execute(
            """SELECT doc FROM turns WHERE chat_id=?
               AND rowid>(SELECT rowid FROM turns WHERE chat_id=? AND id=?)
               ORDER BY rowid DESC""",
            (chat, chat, turn["id"]),
        )
        return all(self._is_resume_read(json.loads(doc)) for doc, in newer)

    def _cancel_track_resume(self, owner_code=None):
        slot = self._slot(owner_code)
        if slot.track_resume_stop:
            slot.track_resume_stop.set()
        slot.track_resume_key = None

    def _prepare_track_resume(self, chat, turn_id, media_id, play_id):
        turn = self._current_playback_turn(chat, turn_id, media_id, play_id)
        if not turn:
            return
        owner = self.chat_owner(chat) or self.DEFAULT_OWNER
        slot = self._slot(owner)
        self._cancel_track_resume(owner)
        if not self._can_resume_track(turn, chat, media_id):
            return
        selected = turn["media"]["selected"]
        if selected.get("artist_source") == "folder" or not selected.get("path"):
            return
        previous = slot.track_resume_job
        stop = slot.track_resume_stop = ThreadEvent()
        slot.track_resume_key = (chat, turn_id, media_id, play_id)

        async def prepare():
            try:
                if previous and not previous.done():
                    await asyncio.shield(previous)
                if not stop.is_set():
                    return await asyncio.to_thread(self.media.prepare_track_resume, selected, stop)
            except (OSError, KeyError, ValueError, sqlite3.Error):
                return None
            return None

        slot.track_resume_job = asyncio.create_task(prepare())

    async def recover_failed_track(self, chat, turn_id, media_id):
        def eligible():
            turn = self._recoverable_track(chat, turn_id, media_id) or {}
            media = turn.get("media") or {}
            receipt = turn.get("browser_playback") or {}
            if media.get("retry_track_source") and receipt.get("event") == "failed" and receipt.get("media_id") == media_id:
                return turn
            return None

        turn = eligible()
        if not turn:
            return False
        result = await asyncio.to_thread(self.media.recover_track_copy, turn["media"]["selected"])
        turn = eligible()
        if not result or not turn:
            return False
        self._apply_track_recovery(turn, media_id, result)
        return True

    async def resume_failed_track(self, chat, turn_id, media_id, play_id, position, connected):
        owner = self.chat_owner(chat) or self.DEFAULT_OWNER
        slot = self._slot(owner)
        key = (chat, turn_id, media_id, play_id)

        def eligible():
            if key != slot.track_resume_key or not connected():
                return None
            turn = self._current_playback_turn(chat, turn_id, media_id, play_id)
            if turn and self._can_resume_track(turn, chat, media_id):
                return turn
            return None

        if type(position) not in (int, float) or not 0.05 <= position <= 8 * 3600 or not eligible():
            return False
        try:
            proof = await asyncio.wait_for(asyncio.shield(slot.track_resume_job), timeout=0.45)
            if not proof or not eligible():
                return False
            result = await asyncio.wait_for(asyncio.to_thread(self.media.resolve_track_resume, proof), timeout=1)
        except (OSError, KeyError, ValueError, sqlite3.Error, TimeoutError):
            return False
        turn = eligible()
        if not result or not turn:
            return False
        self._apply_track_recovery(turn, media_id, result, position=position)
        self._cancel_track_resume(owner)
        return True

    def _apply_track_recovery(self, turn, media_id, result, *, position=None):
        item, checked = result
        media = turn["media"]
        issues = {row["id"]: row for row in [*media.get("unavailable_sources", []), *checked["unavailable_sources"]]}
        view = {
            **media,
            "selected": item,
            "recovered_from": media_id,
            "retry_track_source": False,
            "items": [item if row["media_id"] == media_id else row for row in media["items"]],
            "consulted_sources": list(dict.fromkeys([*media.get("consulted_sources", []), *checked["consulted_sources"]])),
            "unavailable_sources": list(issues.values()),
        }
        changes = {}
        if position is not None:
            view["resume_position_s"] = position
            changes["previous_browser_playback"] = turn["browser_playback"]
        self._update(turn, media=view, browser_playback=None, **changes)

    def page_playback(self, snapshot, *, include_audio=False, chat_id=None):
        if not isinstance(snapshot, dict):
            raise ValueError("播放器现场信息无效")
        state = snapshot.get("state")
        if state == "idle" and not snapshot.get("turn_id") and not snapshot.get("media_id"):
            return {"state": state}
        if state not in ("playing", "paused", "stopped", "ended", "buffering", "failed") or not isinstance(snapshot.get("turn_id"), str):
            raise ValueError("播放器状态无效")
        turn = self._get(snapshot["turn_id"])
        item = self.playback_item(turn, snapshot.get("media_id"))
        target_chat = chat_id or self.chat_id
        if turn["chat_id"] != target_chat or not item:
            raise ValueError("播放器现场与这段对话的音轨不符")
        playback = {"state": state, "title": item["title"], "artist": item.get("artist")}
        if include_audio and item.get("kind") == "audio" and item.get("source_kind") in {"local", "nas", "online"}:
            playback["_audio"] = item
        return playback

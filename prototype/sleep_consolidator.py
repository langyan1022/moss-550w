"""MOSS Sleep-Time Consolidation & Personal World Model Engine (夜间做梦式深度整理).

Runs cleanly on idle or on-demand (0 MiB GPU VRAM, 100% SQLite-backed, read-only toward
long-term `memories` so memory provenance tests stay 100% intact):
  1. Mirrors unindexed notes, verified RSI lessons, and long-term memories into SQLite-SAG.
  2. Extracts a structured Personal World Model:
     - Active projects & work topics
     - Personal preferences, allergies & constraints
     - Key collaborators / contacts mentioned across notes & events
     - Recurring media taste (artists / genres played)
     - Open loops (active timers & unarchived notes)
  3. Detects potential contradictions / date-conflicts across events sharing the same project entity
     so MOSS can proactively surface them for human confirmation.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import re
import sqlite3

from memory_store import memories as read_active_memories
from sag_knowledge import SAGKnowledgeBase, extract_entities


DATE_TOKEN_RE = re.compile(
    r"(?:(?:20\d{2}[年/-])?\d{1,2}[月/-]\d{1,2}日?|周[一二三四五六日天]|星期[一二三四五六日天]|下周[一二三四五六日天]|今天|明天|后天|大年初一|春节)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SleepConsolidator:
    """Offline / idle cognitive consolidation for MOSS (SAG sync + World Model + Conflict Audit)."""

    def __init__(self, db: sqlite3.Connection, sag: SAGKnowledgeBase, soul=None, on_log=None):
        self.db = db
        self.sag = sag
        self.soul = soul
        self.on_log = on_log
        self._init_schema()

    def _init_schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS world_model_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger TEXT NOT NULL,
                    synced_notes INTEGER NOT NULL DEFAULT 0,
                    synced_memories INTEGER NOT NULL DEFAULT 0,
                    synced_lessons INTEGER NOT NULL DEFAULT 0,
                    conflicts_found INTEGER NOT NULL DEFAULT 0,
                    model_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    def _log(self, text: str, level: str = "info"):
        if self.on_log is not None:
            try:
                self.on_log(text, source="DREAM", level=level)
            except Exception:
                pass

    def run_consolidation(self, *, trigger: str = "manual") -> dict:
        """Synchronize notes/memories/RSI into SAG, rebuild Personal World Model, and detect conflicts."""
        self._log(f"Starting sleep-time consolidation (trigger={trigger})", level="info")
        synced_notes = 0
        synced_memories = 0
        synced_lessons = 0

        # 1. Sync active notes into SAG (idempotent via source_ref)
        try:
            note_rows = self.db.execute(
                "SELECT id, text FROM assistant_notes WHERE archived=0 ORDER BY id DESC LIMIT 200"
            ).fetchall()
        except sqlite3.OperationalError:
            note_rows = []
        for nid, ntext in note_rows:
            if not ntext or not str(ntext).strip():
                continue
            ref = f"note:{nid}"
            exists = self.db.execute("SELECT 1 FROM sag_events WHERE source='note' AND source_ref=?", (ref,)).fetchone()
            if not exists:
                self.sag.upsert_event(content=str(ntext), title=str(ntext)[:42], source="note", source_ref=ref)
                synced_notes += 1

        # 2. Single Source of Truth mirror of active memories -> SAG (`memory:{id}`, removing legacy `mem:*` duplicates)
        try:
            mem_rows = [(m["id"], m["text"]) for m in read_active_memories(self.db)]
        except sqlite3.OperationalError:
            mem_rows = []
        if self.soul is not None:
            soul_stats = self.soul.run_maintenance()
            synced_memories += soul_stats.get("synced_sag", 0)
        else:
            with self.db:
                self.db.execute("DELETE FROM sag_events WHERE source='memory' AND source_ref LIKE 'mem:%'")
            for mid, mtext in mem_rows:
                if not mtext or not str(mtext).strip():
                    continue
                ref = f"memory:{mid}"
                exists = self.db.execute("SELECT 1 FROM sag_events WHERE source='memory' AND source_ref=?", (ref,)).fetchone()
                if not exists:
                    self.sag.upsert_event(content=str(mtext), title=f"长期记忆 · {str(mtext)[:32]}", source="memory", source_ref=ref, importance=1.25, pointer_ref=ref)
                    synced_memories += 1
            soul_stats = {}

        # 3. Sync active verified RSI lessons into SAG
        try:
            rsi_rows = self.db.execute(
                "SELECT id, scope, trigger, rule FROM rsi_lessons WHERE status='active' ORDER BY id DESC LIMIT 64"
            ).fetchall()
        except sqlite3.OperationalError:
            rsi_rows = []
        for lid, scope, trig, rule in rsi_rows:
            ref = f"rsi:{lid}"
            exists = self.db.execute("SELECT 1 FROM sag_events WHERE source='rsi' AND source_ref=?", (ref,)).fetchone()
            if not exists:
                self.sag.upsert_event(
                    content=f"触发场景：{trig}；行为规则：{rule}",
                    title=f"RSI 规则 L#{lid} [{scope}]",
                    source="rsi",
                    source_ref=ref,
                )
                synced_lessons += 1

        # 4. Build structured Personal World Model from SAG entities + turns + timers
        entity_rows = self.db.execute(
            """SELECT ent.display_name, ent.kind, COUNT(ee.event_id) AS deg
               FROM sag_entities ent
               JOIN sag_event_entities ee ON ee.entity_id = ent.id
               GROUP BY ent.id ORDER BY deg DESC, ent.id DESC LIMIT 80"""
        ).fetchall()

        projects = [r[0] for r in entity_rows if r[1] == "project"][:8]
        people = [r[0] for r in entity_rows if r[1] == "person"][:8]
        topics = [r[0] for r in entity_rows if r[1] in {"topic", "concept"}][:10]
        preferences = [str(mtext)[:120] for _, mtext in mem_rows[:10]]

        # Mine media taste from recent played tracks in turns
        artist_counts: Counter = Counter()
        try:
            turn_docs = self.db.execute(
                "SELECT doc FROM turns ORDER BY rowid DESC LIMIT 120"
            ).fetchall()
        except sqlite3.OperationalError:
            turn_docs = []
        for (raw_doc,) in turn_docs:
            try:
                doc = json.loads(raw_doc)
            except (TypeError, json.JSONDecodeError):
                continue
            media = doc.get("media") or {}
            sel = media.get("selected") or {}
            artist = (sel.get("artist") or "").strip()
            if artist and artist.lower() != "unknown":
                artist_counts[artist] += 1
        media_taste = [{"artist": a, "plays": c} for a, c in artist_counts.most_common(6)]

        # Open loops: active timers + latest notes
        try:
            open_timers = [
                r[0] for r in self.db.execute(
                    "SELECT label FROM assistant_timers WHERE status='open' ORDER BY due_at ASC LIMIT 8"
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            open_timers = []
        open_notes = [str(ntext)[:80] for _, ntext in note_rows[:6]]

        # 5. Contradiction / Date-Drift Audit across events sharing the same project entity & cold-event cap
        conflicts = self._detect_project_date_conflicts()
        self.sag.prune_cold_events()

        world_model = {
            "active_projects": projects,
            "key_collaborators": people,
            "focus_topics": topics,
            "preferences_and_constraints": preferences,
            "media_taste": media_taste,
            "open_loops": {"timers": open_timers, "recent_notes": open_notes},
            "conflicts": conflicts,
            "soul_maintenance": soul_stats,
            "updated_at": _now_iso(),
        }
        summary = (
            f"深度整理完成：新同步便签 {synced_notes} 条、记忆 {synced_memories} 条、RSI 规则 {synced_lessons} 条、"
            f"提炼画像 {soul_stats.get('traits_updated', 0)} 项、晋升信念 {soul_stats.get('beliefs_graduated', 0)} 条；"
            f"活跃项目 {len(projects)} 项、协作人 {len(people)} 位、音乐偏好 {len(media_taste)} 位歌手，"
            f"发现潜在时间/结论冲突 {len(conflicts)} 处。"
        )
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO world_model_snapshots
                   (trigger, synced_notes, synced_memories, synced_lessons, conflicts_found, model_json, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trigger, synced_notes, synced_memories, synced_lessons, len(conflicts),
                    json.dumps(world_model, ensure_ascii=False), summary, now,
                ),
            )
            gen_id = cur.lastrowid

        self._log(
            f"Gen #{gen_id} done: +{synced_notes}n +{synced_memories}m +{synced_lessons}r conflicts={len(conflicts)}",
            level="warn" if conflicts else "ok",
        )
        return {
            "generation_id": gen_id,
            "trigger": trigger,
            "synced_notes": synced_notes,
            "synced_memories": synced_memories,
            "synced_lessons": synced_lessons,
            "conflicts_found": len(conflicts),
            "summary": summary,
            "world_model": world_model,
            "created_at": now,
        }

    def _detect_project_date_conflicts(self) -> list[dict]:
        """Flag distinct events on the same project entity that mention different dates/deadlines."""
        rows = self.db.execute(
            """SELECT ent.display_name, ev.id, ev.title, ev.content
               FROM sag_entities ent
               JOIN sag_event_entities ee ON ee.entity_id = ent.id
               JOIN sag_events ev ON ev.id = ee.event_id
               WHERE ent.kind = 'project'
               ORDER BY ent.id, ev.id DESC"""
        ).fetchall()
        by_project: dict[str, list[tuple[int, str, str, set[str]]]] = {}
        for proj, eid, title, content in rows:
            dates = set(DATE_TOKEN_RE.findall(f"{title} {content}"))
            if dates:
                by_project.setdefault(proj, []).append((eid, title, content[:120], dates))

        conflicts = []
        for proj, items in by_project.items():
            if len(items) < 2:
                continue
            newest = items[0]
            for older in items[1:]:
                if newest[3] != older[3] and not (newest[3] <= older[3] or older[3] <= newest[3]):
                    conflicts.append({
                        "entity": proj,
                        "newer_event": {"id": newest[0], "title": newest[1], "dates": sorted(newest[3])},
                        "older_event": {"id": older[0], "title": older[1], "dates": sorted(older[3])},
                        "detail": f"关于「{proj}」，条目 E#{newest[0]} 提到{'/'.join(sorted(newest[3]))}，而较早条目 E#{older[0]} 提到{'/'.join(sorted(older[3]))}，建议核对最新排期。",
                    })
                    break
            if len(conflicts) >= 5:
                break
        return conflicts

    def latest_snapshot(self) -> dict | None:
        row = self.db.execute(
            """SELECT id, trigger, synced_notes, synced_memories, synced_lessons, conflicts_found, model_json, summary, created_at
               FROM world_model_snapshots ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if not row:
            return None
        try:
            wm = json.loads(row[6])
        except json.JSONDecodeError:
            wm = {}
        return {
            "generation_id": row[0],
            "trigger": row[1],
            "synced_notes": row[2],
            "synced_memories": row[3],
            "synced_lessons": row[4],
            "conflicts_found": row[5],
            "world_model": wm,
            "summary": row[7],
            "created_at": row[8],
        }

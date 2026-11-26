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

import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import re
import sqlite3

from cognitive_utils import delete_sag_events, normalize_owner_code
from memory_store import memories as read_active_memories
from sag_knowledge import SAGKnowledgeBase, extract_entities


DATE_TOKEN_RE = re.compile(
    r"(?:(?:20\d{2}[年/-])?\d{1,2}[月/-]\d{1,2}日?|周[一二三四五六日天]|星期[一二三四五六日天]|下周[一二三四五六日天]|今天|明天|后天|大年初一|春节)"
)
MILESTONE_TOKEN_RE = re.compile(r"评审|会议|汇报|截止|DDL|上线|发布|验收|测试|演示|立项", re.I)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SleepConsolidator:
    """Offline / idle cognitive consolidation for MOSS (SAG sync + World Model + Conflict Audit)."""

    def __init__(self, db: sqlite3.Connection, sag: SAGKnowledgeBase, soul=None, on_log=None):
        self.db = db
        self.sag = sag
        self.soul = soul
        self.on_log = on_log
        self._async_lock = asyncio.Lock()
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
                    created_at TEXT NOT NULL,
                    owner_code TEXT NOT NULL DEFAULT 'MOSS'
                );
            """)
            try:
                self.db.execute("ALTER TABLE world_model_snapshots ADD COLUMN owner_code TEXT NOT NULL DEFAULT 'MOSS'")
            except sqlite3.OperationalError:
                pass

    def _log(self, text: str, level: str = "info"):
        if self.on_log is not None:
            try:
                self.on_log(text, source="DREAM", level=level)
            except Exception:
                pass

    def run_consolidation(self, *, trigger: str = "manual", owner_code: str = "MOSS") -> dict:
        """Synchronize notes/memories/RSI into SAG, rebuild Personal World Model, and detect conflicts."""
        owner = normalize_owner_code(owner_code)
        self._log(f"Starting sleep-time consolidation (trigger={trigger}, owner={owner})", level="info")
        synced_notes = 0
        synced_memories = 0
        synced_lessons = 0

        # 1. Sync active notes into SAG (host-only notes)
        note_rows = []
        if owner == "MOSS":
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
                val = str(ntext).strip()
                existing = self.db.execute("SELECT content FROM sag_events WHERE source='note' AND source_ref=?", (ref,)).fetchone()
                if existing is None or existing[0] != val[:4000]:
                    self.sag.upsert_event(content=val, title=val[:42], source="note", source_ref=ref, owner_code=owner)
                    synced_notes += 1

        # 2. Single Source of Truth mirror of active memories -> SAG (`memory:{id}`)
        try:
            mem_rows = [(m["id"], m["text"]) for m in read_active_memories(self.db, owner_code=owner)]
        except sqlite3.OperationalError:
            mem_rows = []
        if self.soul is not None:
            soul_stats = self.soul.run_maintenance(owner_code=owner)
            synced_memories += soul_stats.get("synced_sag", 0)
        else:
            if owner == "MOSS":
                legacy_ids = [
                    r[0] for r in self.db.execute(
                        "SELECT id FROM sag_events WHERE source='memory' AND source_ref LIKE 'mem:%'"
                    ).fetchall()
                ]
                delete_sag_events(self.db, legacy_ids, fts_ready=self.sag._fts_ready)
            for mid, mtext in mem_rows:
                if not mtext or not str(mtext).strip():
                    continue
                ref = self.sag._scope_ref(owner, f"memory:{mid}")
                val = str(mtext).strip()
                existing = self.db.execute("SELECT content FROM sag_events WHERE source='memory' AND source_ref=?", (ref,)).fetchone()
                if existing is None or existing[0] != val[:4000]:
                    self.sag.upsert_event(content=val, title=f"长期记忆 · {val[:32]}", source="memory", source_ref=f"memory:{mid}", importance=1.25, pointer_ref=f"memory:{mid}", owner_code=owner)
                    synced_memories += 1
            soul_stats = {}

        # 3. Sync active verified RSI lessons into SAG
        try:
            rsi_rows = self.db.execute(
                "SELECT id, scope, trigger, rule FROM rsi_lessons WHERE status='active' AND COALESCE(owner_code, 'MOSS')=? ORDER BY id DESC LIMIT 64",
                (owner,),
            ).fetchall()
        except sqlite3.OperationalError:
            rsi_rows = []
        for lid, scope, trig, rule in rsi_rows:
            ref = self.sag._scope_ref(owner, f"rsi:{lid}")
            val = f"触发场景：{trig}；行为规则：{rule}"
            existing = self.db.execute("SELECT content FROM sag_events WHERE source='rsi' AND source_ref=?", (ref,)).fetchone()
            if existing is None or existing[0] != val[:4000]:
                self.sag.upsert_event(
                    content=val,
                    title=f"RSI 规则 L#{lid} [{scope}]",
                    source="rsi",
                    source_ref=f"rsi:{lid}",
                    owner_code=owner,
                )
                synced_lessons += 1

        # 4. Build structured Personal World Model from owner-scoped SAG entities + turns + timers
        entity_rows = self.db.execute(
            """SELECT ent.display_name, ent.kind, COUNT(ee.event_id) AS deg
               FROM sag_entities ent
               JOIN sag_event_entities ee ON ee.entity_id = ent.id
               JOIN sag_events ev ON ev.id = ee.event_id
               WHERE COALESCE(ev.owner_code, 'MOSS')=?
               GROUP BY ent.id ORDER BY deg DESC, ent.id DESC LIMIT 80""",
            (owner,),
        ).fetchall()

        projects = [r[0] for r in entity_rows if r[1] == "project"][:8]
        people = [r[0] for r in entity_rows if r[1] == "person"][:8]
        topics = [r[0] for r in entity_rows if r[1] in {"topic", "concept"}][:10]
        preferences = [str(mtext)[:120] for _, mtext in mem_rows[:10]]

        # Mine media taste from recent played tracks in turns
        artist_counts: Counter = Counter()
        try:
            turn_docs = self.db.execute(
                "SELECT t.doc FROM turns t LEFT JOIN chats c ON c.id = t.chat_id "
                "WHERE COALESCE(c.owner_code, 'MOSS')=? ORDER BY t.rowid DESC LIMIT 120",
                (owner,),
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

        # 5. Neural backfill + bi-temporal supersession + conflict audit + episode dedup & cold-event cap
        try:
            if hasattr(self.sag, "backfill_neural_vectors"):
                self.sag.backfill_neural_vectors(limit=120, owner_code=owner)
        except Exception:
            pass
        superseded = self._consolidate_bitemporal_supersessions(owner_code=owner)
        episodes_merged = self._dedup_sag_episodes(owner_code=owner)
        conflicts = self._detect_project_date_conflicts(owner_code=owner)
        self.sag.prune_cold_events(owner_code=owner)

        world_model = {
            "active_projects": projects,
            "key_collaborators": people,
            "focus_topics": topics,
            "preferences_and_constraints": preferences,
            "media_taste": media_taste,
            "open_loops": {"timers": open_timers if owner == "MOSS" else [], "recent_notes": open_notes},
            "conflicts": conflicts,
            "bitemporal_superseded": superseded,
            "soul_maintenance": soul_stats,
            "owner_code": owner,
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
                   (trigger, synced_notes, synced_memories, synced_lessons, conflicts_found, model_json, summary, created_at, owner_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trigger, synced_notes, synced_memories, synced_lessons, len(conflicts),
                    json.dumps(world_model, ensure_ascii=False), summary, now, owner,
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
            "owner_code": owner,
            "synced_notes": synced_notes,
            "synced_memories": synced_memories,
            "synced_lessons": synced_lessons,
            "conflicts_found": len(conflicts),
            "summary": summary,
            "world_model": world_model,
            "created_at": now,
        }

    def _consolidate_bitemporal_supersessions(self, *, owner_code: str = "MOSS") -> int:
        """Non-destructively invalidate older active project schedule entries clearly superseded by update notes."""
        if not hasattr(self.sag, "invalidate_event"):
            return 0
        owner = normalize_owner_code(owner_code)
        supersede_re = re.compile(r"改期|改为|改到|取消|作废|推迟|延后|变更|最新排期")
        try:
            rows = self.db.execute(
                """SELECT ent.display_name, ev.id, ev.title, ev.content, ev.valid_from
                   FROM sag_entities ent
                   JOIN sag_event_entities ee ON ee.entity_id = ent.id
                   JOIN sag_events ev ON ev.id = ee.event_id
                   WHERE ent.kind = 'project' AND COALESCE(ev.owner_code, 'MOSS')=?
                     AND COALESCE(ev.valid_to, '') = ''
                   ORDER BY ent.id, ev.id DESC""",
                (owner,),
            ).fetchall()
        except sqlite3.OperationalError:
            return 0
        by_proj: dict[str, list[tuple[int, str, set[str], set[str], str]]] = {}
        for proj, eid, title, content, vfrom in rows:
            joined = f"{title} {content}"
            dates = set(DATE_TOKEN_RE.findall(joined))
            milestones = set(MILESTONE_TOKEN_RE.findall(joined))
            by_proj.setdefault(proj, []).append((int(eid), joined, dates, milestones, str(vfrom or "")))
        closed = 0
        for items in by_proj.values():
            if len(items) < 2:
                continue
            newest_id, newest_txt, newest_dates, newest_ms, newest_vfrom = items[0]
            if not (newest_dates and supersede_re.search(newest_txt)):
                continue
            for older_id, _, older_dates, older_ms, _ in items[1:]:
                if not older_dates or older_dates == newest_dates:
                    continue
                if newest_ms and older_ms and not (newest_ms & older_ms):
                    continue
                if self.sag.invalidate_event(older_id, invalidated_by_id=newest_id, valid_to=newest_vfrom or None):
                    closed += 1
        return closed

    def _detect_project_date_conflicts(self, *, owner_code: str = "MOSS") -> list[dict]:
        """Flag conflicting active dates on the same project/milestone plus contradictory location/pref memories."""
        owner = str(owner_code or "MOSS").strip().upper() or "MOSS"
        try:
            rows = self.db.execute(
                """SELECT ent.display_name, ev.id, ev.title, ev.content
                   FROM sag_entities ent
                   JOIN sag_event_entities ee ON ee.entity_id = ent.id
                   JOIN sag_events ev ON ev.id = ee.event_id
                   WHERE ent.kind = 'project' AND COALESCE(ev.owner_code, 'MOSS')=?
                     AND COALESCE(ev.valid_to, '') = ''
                   ORDER BY ent.id, ev.id DESC""",
                (owner,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = self.db.execute(
                """SELECT ent.display_name, ev.id, ev.title, ev.content
                   FROM sag_entities ent
                   JOIN sag_event_entities ee ON ee.entity_id = ent.id
                   JOIN sag_events ev ON ev.id = ee.event_id
                   WHERE ent.kind = 'project' AND COALESCE(ev.owner_code, 'MOSS')=?
                   ORDER BY ent.id, ev.id DESC""",
                (owner,),
            ).fetchall()
        by_project: dict[str, list[tuple[int, str, str, set[str], set[str]]]] = {}
        for proj, eid, title, content in rows:
            joined = f"{title} {content}"
            dates = set(DATE_TOKEN_RE.findall(joined))
            milestones = set(MILESTONE_TOKEN_RE.findall(joined))
            if dates:
                by_project.setdefault(proj, []).append((eid, title, content[:120], dates, milestones))

        conflicts = []
        seen_pairs: set[tuple[str, int, int]] = set()
        for proj, items in by_project.items():
            if len(items) < 2:
                continue
            found_for_proj = False
            for idx_a, newest in enumerate(items):
                for older in items[idx_a + 1:]:
                    # Distinct milestones (e.g. 需求评审 周三 vs 上线 周五) are not schedule conflicts.
                    if newest[4] and older[4] and not (newest[4] & older[4]):
                        continue
                    pair_key = (proj, newest[0], older[0])
                    if pair_key in seen_pairs:
                        continue
                    if newest[3] != older[3] and not (newest[3] <= older[3] or older[3] <= newest[3]):
                        seen_pairs.add(pair_key)
                        conflicts.append({
                            "type": "project_schedule",
                            "entity": proj,
                            "newer_event": {"id": newest[0], "title": newest[1], "dates": sorted(newest[3])},
                            "older_event": {"id": older[0], "title": older[1], "dates": sorted(older[3])},
                            "detail": f"关于「{proj}」，条目 E#{newest[0]} 提到{'/'.join(sorted(newest[3]))}，而较早条目 E#{older[0]} 提到{'/'.join(sorted(older[3]))}，建议核对最新排期。",
                        })
                        found_for_proj = True
                        break
                if found_for_proj:
                    break
            if len(conflicts) >= 5:
                break

        # Also detect conflicting live city/location, soul_profile & dietary/drink preference contradictions
        try:
            host_mems = read_active_memories(self.db, owner_code=owner)
            city_re = re.compile(r"(?:现在|长期|平时|目前)?(?:住在|常驻|居住在|定居在?|坐标)\s*([\u4e00-\u9fff]{2,8})")
            city_hits = []
            for m in host_mems:
                if cm := city_re.search(m.get("text") or ""):
                    city_hits.append((m["id"], cm.group(1).removesuffix("市"), m["text"]))
            distinct_cities = {c for _, c, _ in city_hits}
            if len(distinct_cities) > 1 and len(conflicts) < 6:
                conflicts.append({
                    "type": "memory_contradiction",
                    "entity": "常驻城市",
                    "detail": f"长期记忆中同时存在多个居住城市记录（{' / '.join(sorted(distinct_cities))}），建议清理过时条目。",
                })
            if self.soul is not None and len(distinct_cities) == 1 and len(conflicts) < 7:
                trait = self.soul.get_profile_trait("work_city", owner_code=owner)
                if trait and trait.get("value"):
                    tr_city = re.sub(r"^(?:在|于)\s*", "", str(trait["value"]).strip()).removesuffix("市")
                    mem_city = next(iter(distinct_cities))
                    if tr_city and mem_city and tr_city != mem_city and tr_city not in mem_city and mem_city not in tr_city:
                        conflicts.append({
                            "type": "profile_memory_contradiction",
                            "entity": "居住城市",
                            "detail": f"个人记忆城市「{mem_city}」与自动提炼画像「{tr_city}」不一致，建议核对清理。",
                        })
            avoid_re = re.compile(r"(?:对([\u4e00-\u9fffA-Za-z0-9]{1,10})(?:过敏|不耐受)|(?:不吃|忌口|不喝|不能吃)\s*([\u4e00-\u9fffA-Za-z0-9]{1,10}))")
            like_re = re.compile(r"(?:喜欢|偏爱|最爱|爱)[吃喝]?\s*([\u4e00-\u9fffA-Za-z0-9]{1,10})")
            avoid_tokens = set()
            like_tokens = set()
            for m in host_mems:
                t = m.get("text") or ""
                for g1, g2 in avoid_re.findall(t):
                    tok = (g1 or g2).strip()
                    if len(tok) >= 1:
                        avoid_tokens.add(tok)
                for tok in like_re.findall(t):
                    if len(tok.strip()) >= 1:
                        like_tokens.add(tok.strip())
            overlap_diet = {a for a in avoid_tokens if any(a in lk or lk in a for lk in like_tokens)}
            if overlap_diet and len(conflicts) < 7:
                conflicts.append({
                    "type": "preference_contradiction",
                    "entity": "饮食/过敏冲突",
                    "detail": f"长期记忆中对「{'、'.join(sorted(overlap_diet))}」同时存在喜欢与禁忌/过敏记录，请确认哪一条最新有效。",
                })
        except sqlite3.Error:
            pass
        return conflicts

    def _dedup_sag_episodes(self, *, owner_code: str = "MOSS") -> int:
        """Remove redundant near-identical episode summaries per owner, keeping the latest."""
        owner = normalize_owner_code(owner_code)
        rows = self.db.execute(
            """SELECT id, content FROM sag_events
               WHERE source='episode' AND COALESCE(owner_code, 'MOSS')=?
               ORDER BY id DESC LIMIT 120""",
            (owner,),
        ).fetchall()
        if len(rows) < 2:
            return 0
        kept_grams: list[set[str]] = []
        victims: list[int] = []
        for eid, content in rows:
            clean = re.sub(r"\[turn:[a-f0-9]+\]", "", str(content or "")).strip()
            grams = {clean[i:i + 3] for i in range(max(1, len(clean) - 2))}
            if any(len(grams & prev) / max(1, len(grams | prev)) >= 0.85 for prev in kept_grams):
                victims.append(int(eid))
            else:
                kept_grams.append(grams)
        if not victims:
            return 0
        return delete_sag_events(self.db, victims, fts_ready=self.sag._fts_ready)

    async def run_consolidation_async(self, *, trigger: str = "manual", owner_code: str = "MOSS") -> dict:
        """Run sleep consolidation off the event loop with an isolated WAL connection for file-backed DBs."""
        async with self._async_lock:
            db_path = ""
            try:
                for _, name, filename in self.db.execute("PRAGMA database_list").fetchall():
                    if name == "main" and filename:
                        db_path = str(filename)
                        break
            except sqlite3.Error:
                db_path = ""
            if not db_path:
                await asyncio.sleep(0)
                return self.run_consolidation(trigger=trigger, owner_code=owner_code)

            def _worker() -> dict:
                wdb = sqlite3.connect(db_path, timeout=20, check_same_thread=False)
                try:
                    wdb.execute("PRAGMA journal_mode=WAL")
                    wdb.execute("PRAGMA busy_timeout=10000")
                    wdb.execute("PRAGMA synchronous=NORMAL")
                    wsag = SAGKnowledgeBase(wdb, on_event=getattr(self.sag, "on_event", None))
                    wsoul = None
                    if self.soul is not None:
                        from soul_memory import SoulEngine
                        wsoul = SoulEngine(wdb, wsag, on_log=getattr(self.soul, "on_log", self.on_log))
                    worker = SleepConsolidator(wdb, wsag, soul=wsoul, on_log=self.on_log)
                    return worker.run_consolidation(trigger=trigger, owner_code=owner_code)
                finally:
                    wdb.close()

            return await asyncio.to_thread(_worker)

    def latest_snapshot(self, *, owner_code: str = "MOSS") -> dict | None:
        owner = normalize_owner_code(owner_code)
        row = self.db.execute(
            """SELECT id, trigger, synced_notes, synced_memories, synced_lessons, conflicts_found, model_json, summary, created_at
               FROM world_model_snapshots WHERE COALESCE(owner_code, 'MOSS')=? ORDER BY id DESC LIMIT 1""",
            (owner,),
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

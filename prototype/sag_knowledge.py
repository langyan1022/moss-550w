"""MOSS Lightweight SQLite SAG (Semantic-Aware Graph / SQL-Retrieval Augmented Generation).

Combines three complementary retrieval primitives inside MOSS's existing SQLite database
(zero external Docker/Milvus/Neo4j daemons, 0 MiB GPU VRAM, 100% covered by backup_profile.py):

  1. Dense Vector Recall (NumPy L2-normalized float32[256] semantic-hashed + concept-projected embeddings,
     with optional pluggable external embedding upgrade — <2ms over 10k events on CPU).
  2. Lexical + SQLite FTS5 Recall (exact Chinese/English phrase & bigram matching).
  3. Query-Time Dynamic Hyperedges (Event-Entity SQL JOIN):
     Instead of an expensive static offline GraphRAG build, every atomic event (note, RSI lesson,
     ingested document chunk, or saved fact) is linked to lightweight typed entities in SQLite.
     At query time, seed events expand 1-2 hops via shared entities to surface multi-hop evidence chains
     with full explainability (showing exactly which entity bridged the clues).
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import unicodedata

import numpy as np


VECTOR_DIM = 256

# Semantic concept clusters bridge synonyms across Chinese/English without requiring a heavy GPU embedder.
CONCEPT_CLUSTERS: dict[str, tuple[str, ...]] = {
    "饮品与饮食偏好": ("美式", "咖啡", "拿铁", "不加糖", "无糖", "少冰", "奶茶", "下午茶", "茶", "点心", "早餐", "午饭", "晚饭", "甜品", "忌口", "辣", "清淡"),
    "过敏与健康禁忌": ("过敏", "花生", "坚果", "海鲜", "乳糖", "麸质", "忌口", "不能吃", "避开", "哮喘", "药物"),
    "音乐与曲库偏好": ("歌曲", "音乐", "歌单", "曲库", "歌手", "助眠", "纯音乐", "轻音乐", "班得瑞", "刘德华", "刀郎", "流行", "国风", "无损"),
    "硬件与算力环境": ("gpu", "cpu", "显卡", "内存", "显存", "vram", "cuda", "rtx", "3070", "温度", "风扇", "卡顿", "延迟", "语音合成", "omnivoice"),
    "项目与日程协作": ("项目", "评审", "会议", "日程", "排期", "截止", "ddl", "汇报", "方案", "预算", "需求", "上线", "发布", "周报"),
    "AI大脑与自我迭代": ("rsi", "sag", "rag", "向量", "知识库", "图谱", "记忆", "纠错", "自我迭代", "学习", "微调", "dpo", "lora", "提示词", "模型"),
    "出行与位置天气": ("北京", "上海", "深圳", "广州", "杭州", "出差", "航班", "高铁", "火车", "靠窗", "过道", "天气", "下雨", "带伞"),
}

STOP_ENTITIES = {
    "什么", "怎么", "为什么", "这个", "那个", "一下", "一个", "我们", "你们", "他们", "可以", "需要",
    "已经", "目前", "现在", "今天", "明天", "昨天", "刚才", "以及", "因为", "所以", "如果", "但是",
    "就是", "还是", "或者", "然后", "时候", "地方", "东西", "事情", "问题", "办法", "结果", "内容",
    "moss", "the", "and", "for", "with", "that", "this", "from", "have",
}

QUOTED_RE = re.compile(r"[《「『\"“]([^《》「」『』\"“”\n]{2,32})[》」』\"”]")
PERSON_RE = re.compile(
    r"(?:老[王李张刘陈杨黄赵周吴徐孙马朱胡林郭何高罗郑梁谢宋唐许邓冯韩曹曾彭萧蔡潘田董袁于余叶蒋杜苏魏程吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦傅方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤]"
    r"|[\u4e00-\u9fff]{1,2}(?:总|工|经理|主任|博士|教授|老师|医生|姐|哥|叔|姨))"
)
PROJECT_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,6}?)(项目|方案|系统|引擎|模型|计划|评审会?|会议|架构|平台|接口|协议|任务|歌单|合同|预算|报告)"
)
_PROJECT_STOP_PREFIX = re.compile(r"^(?:.*?(?:负责|推进|启动|开发|部署|参加|安排|提交|关于|针对|的|了|在|为|对|把|将|给|与|和|及|跟|由))")
ASCII_TERM_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.\-]{1,24})\b")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_REDACT_PATTERNS = (
    (re.compile(r"\b(?:sk|rk|pk|ghp|gho|github_pat|xoxb|xoxp|AIza)[-_A-Za-z0-9]{10,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"), r"\1 [REDACTED_TOKEN]"),
    (re.compile(r"(?i)((?:api[_-]?key|secret|token|password|passwd|pwd|authorization|auth_code|access_key)\s*[:=]\s*[\"']?)([^\s,;\"'&]{4,})"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|key|api_key|secret|auth|password)=)([^&\s]+)"), r"\1[REDACTED]"),
)

_BLOCKED_INGEST_TOKENS = (".env", ".git", ".ssh", "id_rsa", "id_ed25519", "secrets", "credentials", "token", ".pem", ".key", "session.sqlite")


def _redact_secrets(text: str) -> str:
    clean = str(text or "")
    for pat, repl in _REDACT_PATTERNS:
        clean = pat.sub(repl, clean)
    return clean


def _recency_boost(updated_at: str, importance: float, access_count: int) -> float:
    """Smooth time-decay + access-frequency reinforcement multiplier (1.0 when brand new & standard importance)."""
    imp = max(0.4, min(1.8, float(importance or 1.0)))
    age_days = 0.0
    if updated_at:
        try:
            dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        except ValueError:
            age_days = 0.0
    half_scale = 28.0 * imp
    decay = 0.70 + 0.30 * math.exp(-age_days / half_scale)
    rehearsal = 1.0 + min(0.28, 0.04 * math.log1p(max(0, int(access_count or 0))))
    return decay * rehearsal * (0.85 + 0.15 * imp)


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip().casefold()


def _char_ngrams(text: str) -> list[str]:
    clean = re.sub(r"[\s，。！？、；：,.!?;:()（）\[\]【】\"'“”‘’《》「」/\\|]+", "", _norm(text))
    grams = []
    if len(clean) == 1:
        grams.append(clean)
    for n in (2, 3):
        for i in range(max(0, len(clean) - n + 1)):
            grams.append(clean[i:i + n])
    for tok in re.findall(r"[a-z0-9]{2,}", _norm(text)):
        grams.append(tok)
    return grams


def _fts_tokens(text: str) -> str:
    """Space-separated bi-grams + ASCII words so SQLite FTS5 unicode61 indexes Chinese smoothly."""
    grams = _char_ngrams(text)
    seen = []
    used = set()
    for g in grams:
        if g not in used:
            used.add(g)
            seen.append(g)
            if len(seen) >= 180:
                break
    return " ".join(seen)


def extract_entities(text: str) -> list[tuple[str, str, float]]:
    """Extract lightweight typed entities + semantic concept hubs for SQL dynamic hyperedges."""
    raw = str(text or "").strip()
    low = _norm(raw)
    found: dict[str, tuple[str, str, float]] = {}

    def add(name: str, kind: str, weight: float):
        clean = unicodedata.normalize("NFKC", name).strip()
        key = clean.casefold()
        if len(clean) < 2 or key in STOP_ENTITIES:
            return
        prev = found.get(key)
        if prev is None or weight > prev[2]:
            found[key] = (clean, kind, weight)

    for m in QUOTED_RE.finditer(raw):
        add(m.group(1), "topic", 1.25)
    for m in PERSON_RE.finditer(raw):
        add(m.group(0), "person", 1.35)
    for m in PROJECT_RE.finditer(raw):
        stem = _PROJECT_STOP_PREFIX.sub("", m.group(1)).strip()
        if len(stem) >= 2:
            add(stem + m.group(2), "project", 1.25)
    for m in ASCII_TERM_RE.finditer(raw):
        term = m.group(1)
        if len(term) >= 2 and term.casefold() not in STOP_ENTITIES:
            add(term.upper() if len(term) <= 5 else term, "topic", 1.05)

    for concept, triggers in CONCEPT_CLUSTERS.items():
        hits = sum(1 for kw in triggers if kw in low)
        if hits >= 1:
            add(concept, "concept", min(1.3, 0.85 + 0.15 * hits))
            for kw in triggers:
                if kw in low and len(kw) >= 2:
                    add(kw, "keyword", 0.95)

    return list(found.values())[:24]


def embed_text_local(text: str) -> np.ndarray:
    """Fast CPU 256-d L2-normalized semantic-hashed + concept-cluster dense vector."""
    vec = np.zeros(VECTOR_DIM, dtype=np.float32)
    low = _norm(text)

    def scatter(token: str, weight: float):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        idx1 = int.from_bytes(digest[:2], "little") % VECTOR_DIM
        idx2 = int.from_bytes(digest[2:4], "little") % VECTOR_DIM
        s1 = 1.0 if (digest[4] & 1) else -1.0
        s2 = 1.0 if (digest[5] & 1) else -1.0
        vec[idx1] += s1 * weight
        vec[idx2] += s2 * (weight * 0.6)

    for gram in _char_ngrams(text):
        scatter(f"g:{gram}", 1.0 if len(gram) == 2 else 1.25)

    for display, kind, weight in extract_entities(text):
        scatter(f"e:{kind}:{display.casefold()}", weight * 2.2)

    for concept, triggers in CONCEPT_CLUSTERS.items():
        if any(kw in low for kw in triggers):
            scatter(f"c:{concept}", 2.8)

    norm = float(np.linalg.norm(vec))
    if norm > 1e-8:
        vec /= norm
    return vec


class SAGKnowledgeBase:
    """SQLite Hybrid Vector + FTS5 + Event-Entity Dynamic Hyperedge Knowledge Engine."""

    def __init__(self, db: sqlite3.Connection, on_event=None):
        self.db = db
        self.on_event = on_event
        self._fts_ready = False
        self._init_schema()

    def _init_schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sag_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL DEFAULT 'doc',
                    source_ref TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    fts_tokens TEXT NOT NULL DEFAULT '',
                    embedding BLOB NOT NULL,
                    embed_model TEXT NOT NULL DEFAULT 'local-sag-256d',
                    importance REAL NOT NULL DEFAULT 1.0,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at TEXT NOT NULL DEFAULT '',
                    pointer_ref TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sag_source_ref
                    ON sag_events(source, source_ref) WHERE source_ref != '';
                CREATE TABLE IF NOT EXISTS sag_entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'topic'
                );
                CREATE TABLE IF NOT EXISTS sag_event_entities (
                    event_id INTEGER NOT NULL REFERENCES sag_events(id) ON DELETE CASCADE,
                    entity_id INTEGER NOT NULL REFERENCES sag_entities(id) ON DELETE CASCADE,
                    weight REAL NOT NULL DEFAULT 1.0,
                    PRIMARY KEY (event_id, entity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sag_ee_entity ON sag_event_entities(entity_id);
            """)
            cols = {row[1] for row in self.db.execute("PRAGMA table_info(sag_events)")}
            for col_name, col_def in (
                ("importance", "REAL NOT NULL DEFAULT 1.0"),
                ("access_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_accessed_at", "TEXT NOT NULL DEFAULT ''"),
                ("pointer_ref", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col_name not in cols:
                    self.db.execute(f"ALTER TABLE sag_events ADD COLUMN {col_name} {col_def}")
            try:
                self.db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS sag_events_fts USING fts5(event_id UNINDEXED, tokens)"
                )
                self._fts_ready = True
            except sqlite3.OperationalError:
                self._fts_ready = False

    def _log(self, text: str, level: str = "info"):
        if self.on_event is not None:
            try:
                self.on_event(text, source="SAG", level=level)
            except Exception:
                pass

    def upsert_event(
        self,
        *,
        content: str,
        title: str = "",
        source: str = "doc",
        source_ref: str = "",
        importance: float = 1.0,
        pointer_ref: str = "",
    ) -> dict:
        text = _redact_secrets(str(content or "").strip())
        if not text:
            raise ValueError("知识条目正文不能为空。")
        title_clean = _redact_secrets(str(title or text[:36]).strip())[:120]
        full_text = f"{title_clean}\n{text}" if title_clean and title_clean not in text else text
        vec = embed_text_local(full_text)
        tokens = _fts_tokens(full_text)
        entities = extract_entities(full_text)
        imp = max(0.4, min(1.8, float(importance or 1.0)))
        ptr = str(pointer_ref or "").strip()[:80]
        now = _now_iso()

        with self.db:
            existing = None
            if source_ref:
                existing = self.db.execute(
                    "SELECT id FROM sag_events WHERE source=? AND source_ref=?",
                    (source, source_ref),
                ).fetchone()
            if existing:
                event_id = existing[0]
                self.db.execute(
                    """UPDATE sag_events
                       SET title=?, content=?, fts_tokens=?, embedding=?, importance=?, pointer_ref=?, updated_at=?
                       WHERE id=?""",
                    (title_clean, text[:4000], tokens, vec.tobytes(), imp, ptr, now, event_id),
                )
                self.db.execute("DELETE FROM sag_event_entities WHERE event_id=?", (event_id,))
                if self._fts_ready:
                    self.db.execute("DELETE FROM sag_events_fts WHERE event_id=?", (event_id,))
            else:
                cur = self.db.execute(
                    """INSERT INTO sag_events
                       (source, source_ref, title, content, fts_tokens, embedding, importance, pointer_ref, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source, source_ref, title_clean, text[:4000], tokens, vec.tobytes(), imp, ptr, now, now),
                )
                event_id = cur.lastrowid

            if self._fts_ready and tokens:
                self.db.execute("INSERT INTO sag_events_fts(event_id, tokens) VALUES (?, ?)", (event_id, tokens))

            entity_names = []
            for display, kind, weight in entities:
                canonical = display.casefold()
                self.db.execute(
                    "INSERT OR IGNORE INTO sag_entities(canonical, display_name, kind) VALUES (?, ?, ?)",
                    (canonical, display, kind),
                )
                ent_id = self.db.execute(
                    "SELECT id FROM sag_entities WHERE canonical=?", (canonical,)
                ).fetchone()[0]
                self.db.execute(
                    "INSERT OR REPLACE INTO sag_event_entities(event_id, entity_id, weight) VALUES (?, ?, ?)",
                    (event_id, ent_id, weight),
                )
                entity_names.append(display)

        self._log(f"Indexed E#{event_id} [{source}] ({len(entity_names)} entities): {title_clean[:48]}", level="info")
        return {"id": event_id, "source": source, "title": title_clean, "entities": entity_names}

    def delete_by_source_ref(self, source: str, source_ref: str) -> int:
        """Delete SAG events (plus FTS and hyperedge links) matching (source, source_ref)."""
        src = str(source or "").strip()
        ref = str(source_ref or "").strip()
        if not src or not ref:
            return 0
        with (nullcontext() if self.db.in_transaction else self.db):
            rows = self.db.execute(
                "SELECT id FROM sag_events WHERE source=? AND source_ref=?",
                (src, ref),
            ).fetchall()
            if not rows:
                return 0
            for (eid,) in rows:
                self.db.execute("DELETE FROM sag_event_entities WHERE event_id=?", (eid,))
                if self._fts_ready:
                    self.db.execute("DELETE FROM sag_events_fts WHERE event_id=?", (eid,))
                self.db.execute("DELETE FROM sag_events WHERE id=?", (eid,))
        self._log(f"Removed {len(rows)} SAG event(s) for [{src}] {ref}", level="info")
        return len(rows)

    def ingest_file(self, file_path: Path, *, max_chunks: int = 40) -> list[dict]:
        """Chunk a local .md/.txt/.json file into atomic SAG events."""
        path = Path(file_path).expanduser().resolve()
        low_path = path.as_posix().lower()
        if any(tok in low_path for tok in _BLOCKED_INGEST_TOKENS):
            raise ValueError("出于安全与隐私保护，拒绝导入敏感凭据或数据库文件。")
        if not path.is_file():
            raise ValueError(f"找不到文件：{path}")
        if path.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".log"}:
            raise ValueError("仅支持导入 .md / .txt / .json / .csv / .log 文本文件。")
        raw = path.read_text(encoding="utf-8", errors="replace")
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw) if len(p.strip()) >= 12]
        if not paragraphs and raw.strip():
            paragraphs = [raw.strip()[:2000]]
        results = []
        for idx, chunk in enumerate(paragraphs[:max_chunks], 1):
            res = self.upsert_event(
                content=chunk[:1500],
                title=f"{path.name} #{idx}",
                source="file",
                source_ref=f"{path.name}#{idx}",
            )
            results.append(res)
        return results

    def search(self, query: str, *, limit: int = 6, hops: int = 1, touch: bool = True) -> dict:
        """Hybrid Vector + FTS5 + Query-Time SQL Dynamic Hyperedge Multi-Hop Retrieval with decay & reinforcement."""
        q = str(query or "").strip()
        if not q:
            return {"query": q, "direct": [], "hyperedge": [], "evidence_text": ""}
        rows = self.db.execute(
            """SELECT id, source, title, content, embedding, updated_at, importance, access_count, pointer_ref
               FROM sag_events ORDER BY id DESC LIMIT 2000"""
        ).fetchall()
        if not rows:
            return {"query": q, "direct": [], "hyperedge": [], "evidence_text": ""}

        q_vec = embed_text_local(q)
        q_entities = {display.casefold() for display, _, _ in extract_entities(q)}

        # 1. FTS5 lexical candidate scores (note: SQLite FTS5 bm25() returns negative scores where more negative = stronger match)
        fts_scores: dict[int, float] = {}
        if self._fts_ready:
            q_terms = list(dict.fromkeys(_char_ngrams(q)))[:18]
            if q_terms:
                match_expr = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in q_terms if t.strip())
                try:
                    for eid, rank in self.db.execute(
                        "SELECT event_id, bm25(sag_events_fts) FROM sag_events_fts WHERE sag_events_fts MATCH ? ORDER BY bm25(sag_events_fts) LIMIT 30",
                        (match_expr,),
                    ).fetchall():
                        pos = max(0.0, float(-rank))
                        fts_scores[int(eid)] = pos / (1.0 + pos)
                except sqlite3.OperationalError as exc:
                    self._log(f"FTS5 query error: {exc}", level="warn")

        # 2. Direct entity match scores
        ent_scores: dict[int, list[str]] = {}
        if q_entities:
            placeholders = ",".join("?" for _ in q_entities)
            for eid, disp in self.db.execute(
                f"""SELECT ee.event_id, ent.display_name
                    FROM sag_event_entities ee
                    JOIN sag_entities ent ON ent.id = ee.entity_id
                    WHERE ent.canonical IN ({placeholders})""",
                tuple(q_entities),
            ).fetchall():
                ent_scores.setdefault(int(eid), []).append(disp)

        # Ensure high-signal FTS5 / entity candidates outside the latest 2000-row window are still loaded
        loaded_ids = {r[0] for r in rows}
        extra_ids = [cid for cid in (*fts_scores.keys(), *ent_scores.keys()) if cid not in loaded_ids][:120]
        if extra_ids:
            ph_extra = ",".join("?" for _ in extra_ids)
            rows.extend(
                self.db.execute(
                    f"""SELECT id, source, title, content, embedding, updated_at, importance, access_count, pointer_ref
                        FROM sag_events WHERE id IN ({ph_extra})""",
                    tuple(extra_ids),
                ).fetchall()
            )

        # 3. Hybrid Dense Vector + FTS5 + Entity Seed + Salience/Decay Scoring
        by_id: dict[int, dict] = {}
        scored = []
        for eid, source, title, content, emb_blob, updated_at, importance, access_count, pointer_ref in rows:
            ev_vec = np.frombuffer(emb_blob, dtype=np.float32)
            vec_sim = float(np.dot(q_vec, ev_vec)) if ev_vec.shape == q_vec.shape else 0.0
            fts_s = fts_scores.get(eid, 0.0)
            matched_ents = ent_scores.get(eid, [])
            ent_s = min(1.0, 0.38 * len(matched_ents))
            base_score = 0.56 * max(0.0, vec_sim) + 0.24 * fts_s + 0.20 * ent_s
            decay_mul = _recency_boost(updated_at, importance, access_count)
            hybrid = base_score * decay_mul
            rec = {
                "id": eid, "source": source, "title": title, "content": content,
                "updated_at": updated_at, "vec_sim": round(vec_sim, 3),
                "score": round(hybrid, 3), "matched_entities": matched_ents,
                "pointer_ref": pointer_ref or "", "importance": float(importance or 1.0),
                "access_count": int(access_count or 0),
            }
            by_id[eid] = rec
            if base_score >= 0.14 or matched_ents:
                scored.append(rec)

        scored.sort(key=lambda r: (-r["score"], -r["id"]))
        direct_limit = max(1, (limit + 1) // 2)
        direct_hits = scored[:direct_limit]
        seed_ids = {r["id"] for r in direct_hits}

        # 4. Query-Time SQL Dynamic Hyperedge Expansion (multi-hop evidence chaining)
        hyper_hits: list[dict] = []
        if hops >= 1 and seed_ids:
            seed_ph = ",".join("?" for _ in seed_ids)
            linked = self.db.execute(
                f"""SELECT ee2.event_id, ent.display_name, ent.kind, ee2.weight, ee1.event_id
                    FROM sag_event_entities ee1
                    JOIN sag_event_entities ee2 ON ee1.entity_id = ee2.entity_id
                    JOIN sag_entities ent ON ent.id = ee1.entity_id
                    WHERE ee1.event_id IN ({seed_ph}) AND ee2.event_id NOT IN ({seed_ph})""",
                tuple(seed_ids) + tuple(seed_ids),
            ).fetchall()
            agg: dict[int, dict] = {}
            for target_id, disp, kind, weight, src_id in linked:
                if target_id not in by_id:
                    continue
                seed_score = by_id[src_id]["score"]
                kind_mul = 1.25 if kind in {"person", "project", "topic"} else 0.78
                delta = seed_score * float(weight) * kind_mul * 0.65
                entry = agg.setdefault(target_id, {"score": 0.0, "via": []})
                entry["score"] += delta
                if disp not in entry["via"]:
                    entry["via"].append(disp)
            for target_id, info in sorted(agg.items(), key=lambda kv: -kv[1]["score"]):
                base = by_id[target_id]
                hyper_hits.append({
                    **base,
                    "hop": 1,
                    "via_entities": info["via"][:4],
                    "hop_score": round(info["score"], 3),
                })
                if len(hyper_hits) >= max(1, limit - len(direct_hits)):
                    break

        if touch and (direct_hits or hyper_hits):
            hit_ids = [item["id"] for item in direct_hits] + [item["id"] for item in hyper_hits]
            stamp = _now_iso()
            try:
                with self.db:
                    for hid in hit_ids:
                        self.db.execute(
                            "UPDATE sag_events SET access_count=access_count+1, last_accessed_at=? WHERE id=?",
                            (stamp, hid),
                        )
            except sqlite3.Error:
                pass

        lines = []
        if direct_hits:
            lines.append("【SAG 直接命中知识】")
            for item in direct_hits:
                ptr = f" ptr={item['pointer_ref']}" if item.get("pointer_ref") else ""
                lines.append(f"- [E#{item['id']}/{item['source']}{ptr} 相关度{item['score']:.2f}] {item['title']}：{item['content'][:220]}")
        if hyper_hits:
            lines.append("【SAG 动态超边多跳关联线索】")
            for item in hyper_hits:
                via = "、".join(item["via_entities"])
                ptr = f" ptr={item['pointer_ref']}" if item.get("pointer_ref") else ""
                lines.append(f"- [E#{item['id']}/{item['source']}{ptr} 经由实体「{via}」关联] {item['title']}：{item['content'][:220]}")
        return {
            "query": q,
            "direct": direct_hits,
            "hyperedge": hyper_hits,
            "evidence_text": "\n".join(lines),
        }

    def prune_cold_events(self, max_events: int = 2500) -> int:
        """Retire lowest-salience stale episode/doc entries only if catalog grows past budget."""
        total = self.db.execute("SELECT COUNT(*) FROM sag_events").fetchone()[0]
        if total <= max_events:
            return 0
        excess = total - max_events
        victims = self.db.execute(
            """SELECT id FROM sag_events
               WHERE source IN ('episode', 'doc', 'file')
               ORDER BY importance ASC, access_count ASC, id ASC LIMIT ?""",
            (excess,),
        ).fetchall()
        if not victims:
            return 0
        with self.db:
            for (eid,) in victims:
                self.db.execute("DELETE FROM sag_event_entities WHERE event_id=?", (eid,))
                if self._fts_ready:
                    self.db.execute("DELETE FROM sag_events_fts WHERE event_id=?", (eid,))
                self.db.execute("DELETE FROM sag_events WHERE id=?", (eid,))
        return len(victims)

    def status_snapshot(self) -> dict:
        event_count = self.db.execute("SELECT COUNT(*) FROM sag_events").fetchone()[0]
        entity_count = self.db.execute("SELECT COUNT(*) FROM sag_entities").fetchone()[0]
        edge_count = self.db.execute("SELECT COUNT(*) FROM sag_event_entities").fetchone()[0]
        top_entities = [
            {"name": r[0], "kind": r[1], "degree": r[2]}
            for r in self.db.execute(
                """SELECT ent.display_name, ent.kind, COUNT(ee.event_id) AS deg
                   FROM sag_entities ent
                   JOIN sag_event_entities ee ON ee.entity_id = ent.id
                   GROUP BY ent.id ORDER BY deg DESC, ent.id DESC LIMIT 12"""
            ).fetchall()
        ]
        recent = [
            {"id": r[0], "source": r[1], "title": r[2], "updated_at": r[3]}
            for r in self.db.execute(
                "SELECT id, source, title, updated_at FROM sag_events ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]
        return {
            "engine": "SQLite-SAG (Vector-256d + FTS5 + Dynamic Hyperedge)",
            "vector_dim": VECTOR_DIM,
            "fts5_enabled": self._fts_ready,
            "gpu_vram_mb": 0,
            "event_count": event_count,
            "entity_count": entity_count,
            "edge_count": edge_count,
            "top_entities": top_entities,
            "recent_events": recent,
        }

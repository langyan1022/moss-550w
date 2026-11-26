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
import os
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata

import numpy as np

from cognitive_utils import (
    REDACT_PATTERNS as _REDACT_PATTERNS,
    SAG_PUBLIC_SOURCES,
    delete_sag_events,
    is_sensitive_fs_path,
    normalize_owner_code,
    redact_secrets as _redact_secrets,
)


VECTOR_DIM = 256
NEURAL_VECTOR_DIM = 512

_BGE_LOCK = threading.Lock()
_BGE_SESS = None
_BGE_TOK = None
_BGE_FAILED = False

# Semantic concept clusters bridge synonyms across Chinese/English without requiring a heavy GPU embedder.
CONCEPT_CLUSTERS: dict[str, tuple[str, ...]] = {
    "饮品与饮食偏好": ("美式", "咖啡", "拿铁", "黑咖", "不加糖", "无糖", "少冰", "去冰", "奶茶", "下午茶", "果茶", "点心", "早餐", "午饭", "晚饭", "甜品", "忌口", "不吃", "清淡", "点菜", "外卖", "香菜"),
    "过敏与健康禁忌": ("过敏", "青霉素", "花生", "坚果", "海鲜", "乳糖", "燕麦", "植物奶", "麸质", "忌口", "不能吃", "避开", "哮喘", "药物", "用药", "看病"),
    "音乐与曲库偏好": ("歌曲", "音乐", "歌单", "曲库", "歌手", "助眠", "纯音乐", "轻音乐", "班得瑞", "刘德华", "刀郎", "流行", "国风", "无损", "navidrome", "jamendo"),
    "硬件与算力环境": ("gpu", "cpu", "显卡", "内存", "显存", "vram", "cuda", "rtx", "3070", "温度", "风扇", "卡顿", "延迟", "语音合成", "omnivoice", "树莓派"),
    "项目与日程协作": ("项目", "评审", "会议", "日程", "排期", "截止", "ddl", "汇报", "方案", "预算", "需求", "上线", "发布", "周报", "分工", "红线", "星门", "星环"),
    "AI大脑与自我迭代": ("rsi", "sag", "rag", "向量", "知识库", "图谱", "记忆", "纠错", "自我迭代", "学习", "微调", "dpo", "lora", "提示词", "模型"),
    "出行与位置天气": ("北京", "上海", "深圳", "广州", "杭州", "成都", "郑州", "出差", "航班", "高铁", "火车", "靠窗", "过道", "天气", "下雨", "带伞"),
    "软件研发与工具": ("python", "rust", "golang", "docker", "sqlite", "网关", "后端", "前端", "部署", "代码", "端口", "固件", "雷达"),
    "家居与实体控制": ("home assistant", "客厅", "书房", "主灯", "台灯", "空调", "舵机", "云台", "红眼", "h50"),
}

STOP_BIGRAMS = {
    "什么", "怎么", "为什么", "这个", "那个", "一下", "一个", "我们", "你们", "他们", "可以", "需要",
    "已经", "目前", "现在", "今天", "明天", "昨天", "刚才", "以及", "因为", "所以", "如果", "但是",
    "就是", "还是", "或者", "然后", "时候", "地方", "东西", "事情", "问题", "办法", "结果", "内容",
    "请问", "帮我", "给我", "告诉", "知道", "没有", "不要", "可以吗", "怎么样", "多少", "关于", "一下吧",
}

STOP_ENTITIES = STOP_BIGRAMS | {
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


def _norm_iso_ts(ts: str) -> str:
    """Normalize arbitrary ISO date/timestamp strings to comparable UTC +00:00 ISO format."""
    raw = str(ts or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec="seconds")
    except ValueError:
        return raw


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
    """Space-separated bi/trigrams + ASCII words so SQLite FTS5 unicode61 indexes long Chinese documents."""
    grams = _char_ngrams(text)
    seen = []
    used = set()
    for g in grams:
        if g in STOP_BIGRAMS or g in used:
            continue
        used.add(g)
        seen.append(g)
        if len(seen) >= 2000:
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
        if gram in STOP_BIGRAMS:
            continue
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


def _resolve_bge_dir() -> Path | None:
    if os.environ.get("MOSS_SAG_NEURAL_EMBED", "1") == "0":
        return None
    env_dir = os.environ.get("MOSS_SAG_BGE_DIR", "").strip()
    cands = ([Path(env_dir)] if env_dir else []) + [
        Path(__file__).resolve().parent / "data/models/bge-small-zh-v1.5-onnx",
        Path(__file__).resolve().parent / "models/bge-small-zh-v1.5-onnx",
    ]
    for d in cands:
        if (d / "model_int8.onnx").is_file() and (d / "tokenizer.json").is_file():
            return d
    return None


def _get_bge_embedder():
    global _BGE_SESS, _BGE_TOK, _BGE_FAILED
    if _BGE_FAILED:
        return None, None
    with _BGE_LOCK:
        if _BGE_SESS is not None and _BGE_TOK is not None:
            return _BGE_SESS, _BGE_TOK
        if _BGE_FAILED:
            return None, None
        model_dir = _resolve_bge_dir()
        if model_dir is None:
            _BGE_FAILED = True
            return None, None
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
            tok.enable_truncation(max_length=256)
            tok.enable_padding(length=None)
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            opts.inter_op_num_threads = 1
            sess = ort.InferenceSession(
                str(model_dir / "model_int8.onnx"),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            _BGE_SESS = sess
            _BGE_TOK = tok
            return _BGE_SESS, _BGE_TOK
        except Exception:
            _BGE_FAILED = True
            return None, None


def embed_text_neural(text: str) -> np.ndarray | None:
    """Fast CPU 512-d L2-normalized BGE-small-zh-v1.5 int8 ONNX embedding (~2-4ms, 0 VRAM)."""
    sess, tok = _get_bge_embedder()
    if sess is None or tok is None:
        return None
    clean = _redact_secrets(str(text or "").strip())[:1000]
    if not clean:
        return None
    try:
        enc = tok.encode(clean)
        out = sess.run(None, {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([enc.type_ids], dtype=np.int64),
        })[0]
        vec = np.asarray(out[0, 0, :], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 1e-8:
            vec /= norm
        return vec
    except Exception:
        return None


class SAGKnowledgeBase:
    """SQLite Hybrid Neural(BGE+sqlite-vec) + 256d + FTS5 + Bi-Temporal Hyperedge Knowledge Engine."""

    def __init__(self, db: sqlite3.Connection, on_event=None):
        self.db = db
        self.on_event = on_event
        self._fts_ready = False
        self._vec_ready = False
        self._init_schema()

    def _init_schema(self):
        try:
            import sqlite_vec

            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self._vec_ready = True
        except Exception:
            self._vec_ready = False
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
                    owner_code TEXT NOT NULL DEFAULT 'MOSS',
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_to TEXT NOT NULL DEFAULT '',
                    asserted_from TEXT NOT NULL DEFAULT '',
                    asserted_to TEXT NOT NULL DEFAULT '',
                    invalidated_by_id INTEGER,
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
                ("owner_code", "TEXT NOT NULL DEFAULT 'MOSS'"),
                ("valid_from", "TEXT NOT NULL DEFAULT ''"),
                ("valid_to", "TEXT NOT NULL DEFAULT ''"),
                ("asserted_from", "TEXT NOT NULL DEFAULT ''"),
                ("asserted_to", "TEXT NOT NULL DEFAULT ''"),
                ("invalidated_by_id", "INTEGER"),
            ):
                if col_name not in cols:
                    self.db.execute(f"ALTER TABLE sag_events ADD COLUMN {col_name} {col_def}")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_sag_owner ON sag_events(owner_code, source)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_sag_valid ON sag_events(valid_to, valid_from)")
            try:
                self.db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS sag_events_fts USING fts5(event_id UNINDEXED, tokens)"
                )
                self._fts_ready = True
            except sqlite3.OperationalError:
                self._fts_ready = False
            if self._vec_ready:
                try:
                    self.db.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS sag_vec_events USING vec0("
                        f"event_id integer primary key, owner_code text partition key, "
                        f"embedding float[{NEURAL_VECTOR_DIM}] distance_metric=cosine)"
                    )
                except sqlite3.OperationalError:
                    self._vec_ready = False

    def _log(self, text: str, level: str = "info"):
        if self.on_event is not None:
            try:
                self.on_event(text, source="SAG", level=level)
            except Exception:
                pass

    @staticmethod
    def _norm_owner(owner_code: str | None) -> str:
        return normalize_owner_code(owner_code)

    @classmethod
    def _scope_ref(cls, owner_code: str | None, source_ref: str) -> str:
        ref = str(source_ref or "").strip()
        if not ref:
            return ""
        owner = cls._norm_owner(owner_code)
        if owner == "MOSS":
            return ref
        prefix = f"op:{owner}:"
        return ref if ref.startswith(prefix) else f"{prefix}{ref}"

    def upsert_event(
        self,
        *,
        content: str,
        title: str = "",
        source: str = "doc",
        source_ref: str = "",
        importance: float = 1.0,
        pointer_ref: str = "",
        owner_code: str = "MOSS",
        valid_from: str = "",
        valid_to: str = "",
    ) -> dict:
        text = _redact_secrets(str(content or "").strip())
        if not text:
            raise ValueError("知识条目正文不能为空。")
        title_clean = _redact_secrets(str(title or text[:36]).strip())[:120]
        full_text = f"{title_clean}\n{text}" if title_clean and title_clean not in text else text
        vec = embed_text_local(full_text)
        nvec = embed_text_neural(full_text) if self._vec_ready else None
        tokens = _fts_tokens(full_text)
        entities = extract_entities(full_text)
        imp = max(0.4, min(1.8, float(importance or 1.0)))
        ptr = str(pointer_ref or "").strip()[:80]
        owner = self._norm_owner(owner_code)
        scoped_ref = self._scope_ref(owner, source_ref)
        now = _now_iso()
        vf = _norm_iso_ts(valid_from) if valid_from else now
        vt = _norm_iso_ts(valid_to) if valid_to else ""

        with self.db:
            existing = None
            if scoped_ref:
                existing = self.db.execute(
                    "SELECT id FROM sag_events WHERE source=? AND source_ref=? AND COALESCE(owner_code, 'MOSS')=?",
                    (source, scoped_ref, owner),
                ).fetchone()
                if existing is None and owner == "MOSS":
                    existing = self.db.execute(
                        "SELECT id FROM sag_events WHERE source=? AND source_ref=?",
                        (source, scoped_ref),
                    ).fetchone()
            if existing:
                event_id = existing[0]
                self.db.execute(
                    """UPDATE sag_events
                       SET title=?, content=?, fts_tokens=?, embedding=?, importance=?, pointer_ref=?, owner_code=?,
                           valid_from=CASE WHEN ?!='' THEN ? ELSE COALESCE(NULLIF(valid_from,''), ?) END,
                           valid_to=?, asserted_from=COALESCE(NULLIF(asserted_from,''), ?), updated_at=?
                       WHERE id=?""",
                    (title_clean, text[:4000], tokens, vec.tobytes(), imp, ptr, owner,
                     valid_from, vf, now, vt, now, now, event_id),
                )
                self.db.execute("DELETE FROM sag_event_entities WHERE event_id=?", (event_id,))
                if self._fts_ready:
                    self.db.execute("DELETE FROM sag_events_fts WHERE event_id=?", (event_id,))
            else:
                cur = self.db.execute(
                    """INSERT INTO sag_events
                       (source, source_ref, title, content, fts_tokens, embedding, importance, pointer_ref, owner_code,
                        valid_from, valid_to, asserted_from, asserted_to, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)""",
                    (source, scoped_ref, title_clean, text[:4000], tokens, vec.tobytes(), imp, ptr, owner,
                     vf, vt, now, now, now),
                )
                event_id = cur.lastrowid

            if self._fts_ready and tokens:
                self.db.execute("INSERT INTO sag_events_fts(event_id, tokens) VALUES (?, ?)", (event_id, tokens))
            if self._vec_ready and nvec is not None:
                try:
                    self.db.execute("DELETE FROM sag_vec_events WHERE event_id=?", (event_id,))
                    self.db.execute(
                        "INSERT INTO sag_vec_events(event_id, owner_code, embedding) VALUES (?, ?, ?)",
                        (event_id, owner, nvec.tobytes()),
                    )
                except sqlite3.Error:
                    pass

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

        self._log(f"Indexed E#{event_id} [{source}:{owner}] ({len(entity_names)} entities): {title_clean[:48]}", level="info")
        return {"id": event_id, "source": source, "title": title_clean, "entities": entity_names, "owner_code": owner}

    def delete_by_source_ref(self, source: str, source_ref: str, *, owner_code: str | None = None) -> int:
        """Delete SAG events (plus FTS and hyperedge links) matching (source, source_ref[, owner_code])."""
        src = str(source or "").strip()
        raw_ref = str(source_ref or "").strip()
        if not src or not raw_ref:
            return 0
        with (nullcontext() if self.db.in_transaction else self.db):
            if owner_code is not None:
                owner = self._norm_owner(owner_code)
                scoped_ref = self._scope_ref(owner, raw_ref)
                rows = self.db.execute(
                    "SELECT id FROM sag_events WHERE source=? AND COALESCE(owner_code, 'MOSS')=? AND source_ref IN (?, ?)",
                    (src, owner, raw_ref, scoped_ref),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT id FROM sag_events WHERE source=? AND source_ref=?",
                    (src, raw_ref),
                ).fetchall()
            if not rows:
                return 0
            return delete_sag_events(self.db, [r[0] for r in rows], fts_ready=self._fts_ready, prune_entities=True)

    def ingest_file(self, file_path: Path, *, max_chunks: int = 40) -> list[dict]:
        """Chunk a local .md/.txt/.json file into atomic SAG events."""
        path = Path(file_path).expanduser().resolve()
        if is_sensitive_fs_path(path, block_token_substr=True):
            raise ValueError("出于安全与隐私保护，拒绝导入敏感凭据或数据库文件。")
        if not path.is_file():
            raise ValueError(f"找不到文件：{path}")
        if path.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".log"}:
            raise ValueError("仅支持导入 .md / .txt / .json / .csv / .log 文本文件。")
        with path.open("rb") as fh:
            raw = fh.read(1_048_576).decode("utf-8", errors="replace")
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

    def invalidate_event(
        self,
        event_id: int,
        *,
        invalidated_by_id: int | None = None,
        valid_to: str | None = None,
    ) -> bool:
        """Bi-temporal non-destructive invalidation: mark fact interval closed while keeping audit history."""
        ts = str(valid_to or _now_iso()).strip()
        with (nullcontext() if self.db.in_transaction else self.db):
            cur = self.db.execute(
                """UPDATE sag_events
                   SET valid_to=?, asserted_to=?, invalidated_by_id=?, updated_at=?
                   WHERE id=? AND COALESCE(valid_to, '') = ''""",
                (ts, _now_iso(), invalidated_by_id, _now_iso(), int(event_id)),
            )
        if cur.rowcount > 0:
            self._log(f"Invalidated E#{event_id} (valid_to={ts}, by={invalidated_by_id})", level="info")
            return True
        return False

    def backfill_neural_vectors(self, limit: int = 120, *, owner_code: str | None = None) -> int:
        """Lazily populate 512-d BGE sqlite-vec index for older rows without neural vectors."""
        if not self._vec_ready or _resolve_bge_dir() is None:
            return 0
        owner = self._norm_owner(owner_code) if owner_code is not None else None
        where = "WHERE se.id NOT IN (SELECT event_id FROM sag_vec_events)"
        params: list = []
        if owner is not None:
            where += " AND COALESCE(se.owner_code, 'MOSS')=?"
            params.append(owner)
        rows = self.db.execute(
            f"SELECT se.id, se.title, se.content, COALESCE(se.owner_code, 'MOSS') FROM sag_events se {where} ORDER BY se.id DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        inserted = 0
        with (nullcontext() if self.db.in_transaction else self.db):
            for eid, title, content, own in rows:
                txt = f"{title}\n{content}" if title and title not in content else content
                nvec = embed_text_neural(txt)
                if nvec is None:
                    break
                try:
                    self.db.execute("DELETE FROM sag_vec_events WHERE event_id=?", (int(eid),))
                    self.db.execute(
                        "INSERT INTO sag_vec_events(event_id, owner_code, embedding) VALUES (?, ?, ?)",
                        (int(eid), own, nvec.tobytes()),
                    )
                    inserted += 1
                except sqlite3.Error:
                    continue
        return inserted

    def search(
        self,
        query: str,
        *,
        limit: int = 6,
        hops: int = 1,
        touch: bool = True,
        owner_code: str = "MOSS",
        include_public: bool = True,
        as_of: str | None = None,
        include_invalidated: bool = False,
    ) -> dict:
        """Hybrid Neural(BGE+sqlite-vec) + 256d + FTS5 + RRF + Bi-Temporal Dynamic Hyperedge Retrieval."""
        q = str(query or "").strip()
        if not q:
            return {"query": q, "direct": [], "hyperedge": [], "evidence_text": ""}
        owner = self._norm_owner(owner_code)
        if owner == "MOSS":
            owner_clause = "COALESCE(owner_code, 'MOSS')='MOSS'"
            owner_params: tuple = ()
        elif include_public:
            pub_ph = ",".join("?" for _ in SAG_PUBLIC_SOURCES)
            owner_clause = f"(COALESCE(owner_code, 'MOSS')=? OR (COALESCE(owner_code, 'MOSS')='MOSS' AND source IN ({pub_ph})))"
            owner_params = (owner, *sorted(SAG_PUBLIC_SOURCES))
        else:
            owner_clause = "COALESCE(owner_code, 'MOSS')=?"
            owner_params = (owner,)

        if include_invalidated:
            temporal_clause = "1=1"
            temporal_params: tuple = ()
            temporal_clause_se = "1=1"
        else:
            check_ts = _norm_iso_ts(as_of) if as_of else _now_iso()
            temporal_clause = "(COALESCE(NULLIF(valid_from,''), '') <= ? AND (COALESCE(valid_to, '') = '' OR valid_to > ?))"
            temporal_clause_se = "(COALESCE(NULLIF(se.valid_from,''), '') <= ? AND (COALESCE(se.valid_to, '') = '' OR se.valid_to > ?))"
            temporal_params = (check_ts, check_ts)

        rows = self.db.execute(
            f"""SELECT id, source, title, content, embedding, updated_at, importance, access_count, pointer_ref
                FROM sag_events WHERE {owner_clause} AND {temporal_clause} ORDER BY id DESC LIMIT 2000""",
            owner_params + temporal_params,
        ).fetchall()
        if not rows:
            return {"query": q, "direct": [], "hyperedge": [], "evidence_text": ""}

        q_vec = embed_text_local(q)
        q_nvec = embed_text_neural(q) if self._vec_ready else None
        q_entities = {display.casefold() for display, _, _ in extract_entities(q)}

        owner_clause_se = owner_clause.replace("COALESCE(owner_code", "COALESCE(se.owner_code").replace("source IN", "se.source IN")

        # 0. Neural Vector KNN via sqlite-vec (partitioned by owner_code)
        neural_scores: dict[int, float] = {}
        if self._vec_ready and q_nvec is not None:
            q_blob = q_nvec.tobytes()
            part_owners = [owner] if (owner == "MOSS" or not include_public) else [owner, "MOSS"]
            for po in part_owners:
                try:
                    knn_rows = self.db.execute(
                        "SELECT event_id, distance FROM sag_vec_events WHERE embedding MATCH ? AND owner_code = ? AND k = 32",
                        (q_blob, po),
                    ).fetchall()
                    for eid_raw, dist in knn_rows:
                        sim = max(0.0, 1.0 - float(dist or 1.0))
                        eid_i = int(eid_raw)
                        if sim > neural_scores.get(eid_i, 0.0):
                            neural_scores[eid_i] = sim
                except sqlite3.Error:
                    pass

        # 1. FTS5 lexical candidate scores scoped inside owner + temporal partition
        fts_scores: dict[int, float] = {}
        if self._fts_ready:
            q_terms = [t for t in dict.fromkeys(_char_ngrams(q)) if t not in STOP_BIGRAMS][:20]
            if q_terms:
                match_expr = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in q_terms if t.strip())
                try:
                    for eid, rank in self.db.execute(
                        f"""SELECT f.event_id, bm25(sag_events_fts)
                            FROM sag_events_fts f
                            JOIN sag_events se ON se.id = f.event_id
                            WHERE sag_events_fts MATCH ? AND {owner_clause_se} AND {temporal_clause_se}
                            ORDER BY bm25(sag_events_fts) LIMIT 40""",
                        (match_expr,) + owner_params + temporal_params,
                    ).fetchall():
                        pos = max(0.0, float(-rank))
                        fts_scores[int(eid)] = pos / (1.0 + pos)
                except sqlite3.OperationalError as exc:
                    self._log(f"FTS5 query error: {exc}", level="warn")

        # 2. Direct entity match scores (scoped by owner + temporal partition)
        ent_scores: dict[int, list[str]] = {}
        if q_entities:
            placeholders = ",".join("?" for _ in q_entities)
            for eid, disp in self.db.execute(
                f"""SELECT ee.event_id, ent.display_name
                    FROM sag_event_entities ee
                    JOIN sag_entities ent ON ent.id = ee.entity_id
                    JOIN sag_events se ON se.id = ee.event_id
                    WHERE ent.canonical IN ({placeholders}) AND {owner_clause_se} AND {temporal_clause_se}""",
                tuple(q_entities) + owner_params + temporal_params,
            ).fetchall():
                ent_scores.setdefault(int(eid), []).append(disp)

        # Ensure high-signal candidates outside the latest 2000-row window are still loaded (respecting owner + temporal)
        loaded_ids = {r[0] for r in rows}
        extra_ids = [cid for cid in (*neural_scores.keys(), *fts_scores.keys(), *ent_scores.keys()) if cid not in loaded_ids][:120]
        if extra_ids:
            ph_extra = ",".join("?" for _ in extra_ids)
            rows.extend(
                self.db.execute(
                    f"""SELECT id, source, title, content, embedding, updated_at, importance, access_count, pointer_ref
                        FROM sag_events WHERE id IN ({ph_extra}) AND {owner_clause} AND {temporal_clause}""",
                    tuple(extra_ids) + owner_params + temporal_params,
                ).fetchall()
            )
            # Filter neural_scores cleanly to records permitted in owner+temporal partition
            allowed_loaded = {int(r[0]) for r in rows}
            neural_scores = {k: v for k, v in neural_scores.items() if k in allowed_loaded}

        # 3. Vectorized 256d + Neural 512d + FTS5 + Entity + RRF Scoring
        exp_bytes = VECTOR_DIM * 4
        valid_indices = [idx for idx, row in enumerate(rows) if isinstance(row[4], (bytes, bytearray, memoryview)) and len(row[4]) == exp_bytes]
        vec_sims = [0.0] * len(rows)
        if valid_indices:
            mat = np.frombuffer(b"".join(bytes(rows[idx][4]) for idx in valid_indices), dtype=np.float32).reshape(len(valid_indices), VECTOR_DIM)
            sims = mat @ q_vec
            for pos, row_idx in enumerate(valid_indices):
                vec_sims[row_idx] = float(sims[pos])

        # Build per-channel RRF ranks
        rrf_k = 45.0
        rrf_map: dict[int, float] = {}
        local_ranked = sorted(((int(rows[i][0]), vec_sims[i]) for i in range(len(rows)) if vec_sims[i] > 0.05), key=lambda x: -x[1])
        for rk, (eid_i, _) in enumerate(local_ranked[:40], 1):
            rrf_map[eid_i] = rrf_map.get(eid_i, 0.0) + 1.0 / (rrf_k + rk)
        neural_ranked = sorted(neural_scores.items(), key=lambda x: -x[1])
        for rk, (eid_i, sim_v) in enumerate(neural_ranked[:40], 1):
            if sim_v >= 0.48:
                rrf_map[eid_i] = rrf_map.get(eid_i, 0.0) + 1.35 / (rrf_k + rk)
        for rk, (eid_i, _) in enumerate(sorted(fts_scores.items(), key=lambda x: -x[1])[:40], 1):
            rrf_map[eid_i] = rrf_map.get(eid_i, 0.0) + 1.0 / (rrf_k + rk)
        for rk, (eid_i, elist) in enumerate(sorted(ent_scores.items(), key=lambda x: -len(x[1]))[:40], 1):
            rrf_map[eid_i] = rrf_map.get(eid_i, 0.0) + 1.1 / (rrf_k + rk)
        max_rrf = (1.0 + 1.35 + 1.0 + 1.1) / (rrf_k + 1.0)

        by_id: dict[int, dict] = {}
        scored = []
        for idx, (eid, source, title, content, emb_blob, updated_at, importance, access_count, pointer_ref) in enumerate(rows):
            vec_sim = vec_sims[idx]
            n_sim = neural_scores.get(eid, 0.0)
            fts_s = fts_scores.get(eid, 0.0)
            matched_ents = ent_scores.get(eid, [])
            ent_s = min(1.0, 0.38 * len(matched_ents))
            rrf_norm = min(1.0, rrf_map.get(eid, 0.0) / max_rrf) if max_rrf > 0 else 0.0
            legacy_base = 0.56 * max(0.0, vec_sim) + 0.24 * fts_s + 0.20 * ent_s
            n_cal = min(1.0, max(0.0, (n_sim - 0.44) / 0.24)) if n_sim >= 0.48 else 0.0
            neural_base = (0.65 * n_cal + 0.25 * rrf_norm + 0.10 * max(fts_s, ent_s)) if n_cal > 0 else 0.0
            base_score = max(legacy_base, neural_base)
            decay_mul = _recency_boost(updated_at, importance, access_count)
            hybrid = base_score * decay_mul
            rec = {
                "id": eid, "source": source, "title": title, "content": content,
                "updated_at": updated_at, "vec_sim": round(vec_sim, 3),
                "neural_sim": round(n_sim, 3),
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

        # 4. Query-Time SQL Dynamic Hyperedge Expansion (degree-dampened 1-to-2 hop chaining)
        hyper_hits: list[dict] = []
        if hops >= 1 and seed_ids:
            max_hyper = max(1, limit - len(direct_hits))

            def _expand_hop(frontier_scores: dict[int, float], exclude_ids: set[int]) -> list[tuple[int, float, list[str]]]:
                if not frontier_scores:
                    return []
                seed_ph = ",".join("?" for _ in frontier_scores)
                linked = self.db.execute(
                    f"""SELECT ee2.event_id, ent.id, ent.display_name, ent.kind, ee2.weight, ee1.event_id,
                               se.source, se.title, se.content, se.updated_at, se.importance, se.access_count, se.pointer_ref
                        FROM sag_event_entities ee1
                        JOIN sag_event_entities ee2 ON ee1.entity_id = ee2.entity_id
                        JOIN sag_entities ent ON ent.id = ee1.entity_id
                        JOIN sag_events se ON se.id = ee2.event_id
                        WHERE ee1.event_id IN ({seed_ph}) AND ee2.event_id NOT IN ({seed_ph}) AND {owner_clause_se} AND {temporal_clause_se}""",
                    tuple(frontier_scores.keys()) + tuple(frontier_scores.keys()) + owner_params + temporal_params,
                ).fetchall()
                linked = [r for r in linked if r[0] not in exclude_ids]
                for target_id, _, _, _, _, _, ms, mtitle, mcont, mupd, mimp, macc, mptr in linked:
                    tid = int(target_id)
                    if tid not in by_id:
                        by_id[tid] = {
                            "id": tid, "source": ms, "title": mtitle, "content": mcont,
                            "updated_at": mupd, "vec_sim": 0.0,
                            "score": round(0.15 * _recency_boost(mupd, mimp, macc), 3),
                            "matched_entities": [], "pointer_ref": mptr or "",
                            "importance": float(mimp or 1.0), "access_count": int(macc or 0),
                        }
                ent_ids = sorted({int(r[1]) for r in linked})
                deg_map: dict[int, int] = {}
                if ent_ids:
                    ph_e = ",".join("?" for _ in ent_ids)
                    for ent_i, deg in self.db.execute(
                        f"SELECT entity_id, COUNT(*) FROM sag_event_entities WHERE entity_id IN ({ph_e}) GROUP BY entity_id",
                        tuple(ent_ids),
                    ).fetchall():
                        deg_map[int(ent_i)] = int(deg or 1)
                agg: dict[int, dict] = {}
                for target_id, ent_id, disp, kind, weight, src_id, *_ in linked:
                    tid = int(target_id)
                    if tid not in by_id:
                        continue
                    seed_score = max(0.08, float(frontier_scores.get(int(src_id), 0.15)))
                    kind_mul = 1.25 if kind in {"person", "project", "topic"} else 0.78
                    deg_damp = 1.0 / max(1.0, math.log1p(deg_map.get(int(ent_id), 1)))
                    delta = seed_score * float(weight) * kind_mul * deg_damp * 0.65
                    entry = agg.setdefault(tid, {"score": 0.0, "via": []})
                    entry["score"] += delta
                    if disp not in entry["via"]:
                        entry["via"].append(disp)
                return sorted(
                    ((tid, info["score"], info["via"]) for tid, info in agg.items()),
                    key=lambda x: -x[1],
                )

            frontier1 = {item["id"]: float(item["score"] or 0.15) for item in direct_hits}
            seen_hyper = set(seed_ids)
            hop1_ranked = _expand_hop(frontier1, seen_hyper)
            frontier2: dict[int, float] = {}
            for tid, hscore, via in hop1_ranked:
                if len(hyper_hits) >= max_hyper:
                    break
                seen_hyper.add(tid)
                frontier2[tid] = hscore * 0.48
                hyper_hits.append({
                    **by_id[tid],
                    "hop": 1,
                    "via_entities": via[:4],
                    "hop_score": round(hscore, 3),
                })
            if int(hops) >= 2 and len(hyper_hits) < max_hyper and frontier2:
                hop2_ranked = _expand_hop(dict(list(frontier2.items())[:6]), seen_hyper)
                for tid, hscore, via in hop2_ranked:
                    if len(hyper_hits) >= max_hyper:
                        break
                    seen_hyper.add(tid)
                    hyper_hits.append({
                        **by_id[tid],
                        "hop": 2,
                        "via_entities": via[:4],
                        "hop_score": round(hscore, 3),
                    })

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

    def auto_recall(
        self,
        query: str,
        *,
        allow_private: bool = True,
        exclude_sources: set[str] | tuple[str, ...] = (),
        limit: int = 3,
        min_score: float = 0.34,
        max_chars: int = 420,
        owner_code: str = "MOSS",
    ) -> dict:
        """Precision-gated automatic SAG recall for system prompt injection without FTS/LLM noise."""
        raw = self.search(query, limit=max(4, limit * 2), hops=1, touch=False, owner_code=owner_code, include_public=True)
        excluded = set(exclude_sources or ())

        def _eligible(item: dict, is_hyper: bool = False) -> bool:
            src = item.get("source")
            if src in excluded:
                return False
            if not allow_private and src not in SAG_PUBLIC_SOURCES:
                return False
            sc = float(item.get("hop_score" if is_hyper else "score") or 0.0)
            has_entity = bool(item.get("matched_entities") or item.get("via_entities"))
            if is_hyper:
                return sc >= max(0.24, min_score * 0.72) or (bool(item.get("via_entities")) and sc >= 0.18)
            return sc >= min_score or (has_entity and sc >= max(0.24, min_score * 0.7))

        direct = [x for x in (raw.get("direct") or []) if _eligible(x, False)][:limit]
        seen_ids = {x["id"] for x in direct}
        hyper = [x for x in (raw.get("hyperedge") or []) if x["id"] not in seen_ids and _eligible(x, True)][:max(1, limit - len(direct))]

        if not direct and not hyper:
            return {"items": [], "evidence_text": ""}

        # Touch only items that genuinely pass the precision gate
        hit_ids = [x["id"] for x in (*direct, *hyper)]
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

        lines = ["【本机 SAG 知识与关联线索参考（可结合 recall_turn_context 核查细节）】"]
        used = len(lines[0])
        chosen = []
        for item in (*direct, *hyper):
            via = f" · 经由「{'、'.join(item['via_entities'])}」" if item.get("via_entities") else ""
            ptr = f" ptr={item['pointer_ref']}" if item.get("pointer_ref") else ""
            snippet = str(item.get("content") or "").replace("\n", " ").strip()[:130]
            line = f"- [E#{item['id']}/{item['source']}{ptr}{via}] {item['title']}：{snippet}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
            chosen.append(item)
        if not chosen:
            return {"items": [], "evidence_text": ""}
        return {"items": chosen, "evidence_text": "\n".join(lines)}

    def prune_cold_events(self, max_events: int = 2500, *, owner_code: str | None = None) -> int:
        """Retire lowest-salience stale episode/doc entries strictly within the target owner partition over budget."""
        if owner_code is not None:
            owner = self._norm_owner(owner_code)
            owner_counts = self.db.execute(
                "SELECT COALESCE(owner_code, 'MOSS'), COUNT(*) FROM sag_events WHERE COALESCE(owner_code, 'MOSS')=? GROUP BY COALESCE(owner_code, 'MOSS')",
                (owner,),
            ).fetchall()
        else:
            owner_counts = self.db.execute(
                "SELECT COALESCE(owner_code, 'MOSS'), COUNT(*) FROM sag_events GROUP BY COALESCE(owner_code, 'MOSS')"
            ).fetchall()
        victims: list[int] = []
        for own, total in owner_counts:
            if int(total or 0) <= max_events:
                continue
            excess = int(total) - max_events
            rows = self.db.execute(
                """SELECT id FROM sag_events
                   WHERE COALESCE(owner_code, 'MOSS')=? AND source IN ('episode', 'doc', 'file')
                   ORDER BY importance ASC, access_count ASC, id ASC LIMIT ?""",
                (own, excess),
            ).fetchall()
            victims.extend(int(r[0]) for r in rows)
        if not victims:
            return 0
        return delete_sag_events(self.db, victims, fts_ready=self._fts_ready, prune_entities=True)

    def purge_owner(self, owner_code: str) -> int:
        """Delete all SAG events, FTS rows, and orphan entities belonging to an operator."""
        owner = self._norm_owner(owner_code)
        rows = self.db.execute(
            "SELECT id FROM sag_events WHERE COALESCE(owner_code, 'MOSS')=?",
            (owner,),
        ).fetchall()
        if not rows:
            return 0
        eids = [int(r[0]) for r in rows]
        return delete_sag_events(self.db, eids, fts_ready=self._fts_ready, prune_entities=True)

    def status_snapshot(self, *, owner_code: str | None = None) -> dict:
        if owner_code is not None:
            owner = self._norm_owner(owner_code)
            event_count = self.db.execute(
                "SELECT COUNT(*) FROM sag_events WHERE COALESCE(owner_code, 'MOSS')=?",
                (owner,),
            ).fetchone()[0]
            entity_count = self.db.execute(
                """SELECT COUNT(DISTINCT ee.entity_id)
                   FROM sag_event_entities ee
                   JOIN sag_events ev ON ev.id = ee.event_id
                   WHERE COALESCE(ev.owner_code, 'MOSS')=?""",
                (owner,),
            ).fetchone()[0]
            edge_count = self.db.execute(
                """SELECT COUNT(*)
                   FROM sag_event_entities ee
                   JOIN sag_events ev ON ev.id = ee.event_id
                   WHERE COALESCE(ev.owner_code, 'MOSS')=?""",
                (owner,),
            ).fetchone()[0]
            top_entities = [
                {"name": r[0], "kind": r[1], "degree": r[2]}
                for r in self.db.execute(
                    """SELECT ent.display_name, ent.kind, COUNT(ee.event_id) AS deg
                       FROM sag_entities ent
                       JOIN sag_event_entities ee ON ee.entity_id = ent.id
                       JOIN sag_events ev ON ev.id = ee.event_id
                       WHERE COALESCE(ev.owner_code, 'MOSS')=?
                       GROUP BY ent.id ORDER BY deg DESC, ent.id DESC LIMIT 12""",
                    (owner,),
                ).fetchall()
            ]
            recent = [
                {"id": r[0], "source": r[1], "title": r[2], "updated_at": r[3], "owner_code": r[4]}
                for r in self.db.execute(
                    "SELECT id, source, title, updated_at, COALESCE(owner_code, 'MOSS') "
                    "FROM sag_events WHERE COALESCE(owner_code, 'MOSS')=? ORDER BY id DESC LIMIT 10",
                    (owner,),
                ).fetchall()
            ]
        else:
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
                {"id": r[0], "source": r[1], "title": r[2], "updated_at": r[3], "owner_code": r[4]}
                for r in self.db.execute(
                    "SELECT id, source, title, updated_at, COALESCE(owner_code, 'MOSS') FROM sag_events ORDER BY id DESC LIMIT 10"
                ).fetchall()
            ]
        invalidated_count = self.db.execute(
            "SELECT COUNT(*) FROM sag_events WHERE COALESCE(valid_to, '') != ''"
        ).fetchone()[0]
        return {
            "engine": "SQLite-SAG v7 (BGE-512d sqlite-vec + 256d + FTS5 + RRF + Bi-Temporal)",
            "vector_dim": VECTOR_DIM,
            "neural_vector_dim": NEURAL_VECTOR_DIM if (self._vec_ready and _resolve_bge_dir() is not None) else 0,
            "sqlite_vec_enabled": self._vec_ready,
            "neural_enabled": bool(self._vec_ready and _resolve_bge_dir() is not None),
            "fts5_enabled": self._fts_ready,
            "invalidated_count": int(invalidated_count or 0),
            "gpu_vram_mb": 0,
            "owner_code": self._norm_owner(owner_code) if owner_code is not None else "ALL",
            "event_count": event_count,
            "entity_count": entity_count,
            "edge_count": edge_count,
            "top_entities": top_entities,
            "recent_events": recent,
        }

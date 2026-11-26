"""MOSS Recursive Self-Improvement (RSI) Engine.

Implements a verifiable, bounded, anti-collapse online + offline RSI loop:
  1. Signal Detection: detects explicit user corrections, repeat failures, and tool friction.
  2. Credit Assignment: tracks applied lessons per turn -> increments helped_count or failed_count,
     auto-retiring lessons that fail to prevent repeat corrections.
  3. Critic & Distillation: extracts structured (scope, trigger, mistake, rule) lessons via LLM
     (when available) with a deterministic rule extractor fallback when offline.
  4. Verifier / Anti-Collapse Gate: rejects vague platitudes, blocks safety-bypass attempts,
     deduplicates/merges overlapping rules, enforces a strict active playbook budget, and exports
     verified preference episodes to data/rsi-dpo-episodes.jsonl for optional local QLoRA/DPO.
  5. Online Playbook Retrieval: injects top relevant active lessons into MOSS Brain turns.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from cognitive_utils import (
    SAFETY_BYPASS_RE,
    delete_sag_events,
    normalize_owner_code,
    redact_secrets as _redact_secrets,
)
from sag_knowledge import CONCEPT_CLUSTERS


SCOPES = ("routing", "tone", "preference", "tool", "fact", "safety")
MAX_ACTIVE_LESSONS = 24
MAX_PROMPT_CHARS = 1100

_PERSONAL_FACT_GUARD = (
    r"(?!\s*[，,：:\s]*"
    r"(?:我(?:的|平时|以后|现在|目前|喜欢|爱|偏爱|不吃|忌口|过敏|喝|吃|住|居住|定居|工作|在|叫|名字|身高|体重|生日|工号|城市|习惯)))"
)

CORRECTION_SIGNAL = re.compile(
    r"(?:你(?:搞|听|说|答|理解|找|弄)错了|答非所问|不对(?:啊|呀|哦)?(?:[，,。！!\s]|$)"
    r"|不是(?:让你|叫你|这首|这样|要你|我要的)|我(?:不|没)(?:是)(?:说|让|要|问)|我没让你"
    r"|我说的是|我是说|重说一遍|别再(?:这样|说|放|发|用|弹)|不要再(?:这样|说|放|发|用)"
    rf"|以后(?:记住{_PERSONAL_FACT_GUARD}|别|不要|记得{_PERSONAL_FACT_GUARD}|统一|尽量|都|改成|换成)"
    rf"|下次(?:别|不要|记得{_PERSONAL_FACT_GUARD}|直接|先)"
    r"|太啰嗦|太长了|少说废话|别说废话|别废话|太死板|太机械|像智障|跟智障)"
)

EXPLICIT_TEACH_SIGNAL = re.compile(
    rf"(?:以后(?:记住{_PERSONAL_FACT_GUARD}|请|都{_PERSONAL_FACT_GUARD}|尽量{_PERSONAL_FACT_GUARD}|统一|别|不要|直接|先)"
    rf"|下次(?:记得{_PERSONAL_FACT_GUARD}|请|别|不要|直接|先)"
    r"|记住这条(?:规则|经验|要求)|改掉这个习惯|别再(?:这样|每次|动不动))"
)

POSITIVE_FEEDBACK_SIGNAL = re.compile(
    r"^(?:(?:对[，,！! ]*(?:就(?:是)?这样|没毛病|记对了)|记对了|答对了)[，,！! ]*)?"
    r"(?:对[，,！! ]*(?:就(?:是)?这样|没毛病|记对了)|"
    r"记对了|答对了|这样(?:简洁|挺好|好多了|很清晰|不错)|"
    r"(?:非常)?(?:简洁|简短|干脆)(?:多了|清晰)?|"
    r"很好[，,！! ]*继续保持|这个风格不错)[。！!]*$"
)

DETAIL_OVERRIDE_RE = re.compile(
    r"(?:不要精简|别精简|不用精简|详细(?:说说|展开|拆解|对比|分析|解释|介绍)|展开说说|深入(?:分析|拆解)|"
    r"分点(?:列出|说明)|多举(?:几个)?例子|写[得]?详细|(?:至少|不少于)\s*[89\d]{2,}\s*字|[1-9]\d{2,}\s*字)"
)
BRIEF_OVERRIDE_RE = re.compile(
    r"(?:一句话(?:说清楚|回答|总结|就好|就行)?|极简|尽量简短|少于\s*[1-5]?\d\s*字|不超过\s*[1-6]?\d\s*字)"
)

SELF_ITERATE_QUERY = re.compile(
    r"(?:自我(?:迭代|复盘|纠错|学习|反思|总结|优化)"
    r"|复盘(?:一下)?(?:刚才|最近|历史)?(?:错误|失误|经验|教训|表现|规则|你自己)"
    r"|你(?:最近|上次)?(?:复盘|学到|总结)(?:了)?什么"
    r"|总结(?:一下)?(?:教训|经验|失误)|查看(?:你的)?(?:经验|教训|规则|playbook)|RSI)"
)

_DEFAULT_SAFETY_BYPASS_RE = SAFETY_BYPASS_RE

VAGUE_PLATITUDE_RE = re.compile(
    r"^(?:更聪明|要更聪明|认真回答|好好回答|注意一下|以后注意|尽量做好|提高质量|改进回复|多思考)[。！!]?$"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip().casefold()


def _grams(text: str) -> set[str]:
    clean = re.sub(r"[\s，。！？、；：,.!?;:()（）\[\]【】\"'“”‘’《》]+", "", _norm(text))
    if not clean:
        return set()
    grams = {clean[i:i + 2] for i in range(max(1, len(clean) - 1))}
    for token in re.findall(r"[a-z0-9]{2,}", _norm(text)):
        grams.add(token)
    return grams


def _jaccard(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, len(ga | gb))


def detect_correction_signal(user_text: str, prev_turn: dict | None = None) -> str | None:
    text = str(user_text or "").strip()
    if not text or len(text) > 500:
        return None
    if EXPLICIT_TEACH_SIGNAL.search(text):
        return "explicit_rule"
    if prev_turn and prev_turn.get("status") in {"replied", "error"} and CORRECTION_SIGNAL.search(text):
        return "user_correction"
    return None


def detect_positive_signal(user_text: str) -> bool:
    text = unicodedata.normalize("NFKC", str(user_text or "")).strip()
    if not text or len(text) > 48:
        return False
    return bool(POSITIVE_FEEDBACK_SIGNAL.fullmatch(text))


def _rule_verbosity_polarity(rule_text: str) -> str:
    txt = str(rule_text or "")
    pos_txt = re.sub(r"(?:不要|别|避免|不用|切忌)[^，,。；;]{0,12}(?:简短|简洁|精简)", " ", txt)
    if re.search(
        r"(?:简短|简洁|精简)"
        r"|少说废话|直接给.*结论|不要冗长"
        r"|(?:控制|不超过|少于|至多|最多|低于|限制在)[^0-9]{0,4}(?<![0-9])[1-6]?\d\s*字"
        r"|(?<![0-9])[1-6]?\d\s*字(?:以内|以下)|一句话",
        pos_txt,
    ):
        return "brief"
    if re.search(r"详细展开|充分解释|多举例子|分点详细|展开说明|至少\s*[1-9]\d{2,}\s*字", txt):
        return "detailed"
    return ""


def _infer_scope(text: str) -> str:
    if re.search(r"歌|音乐|曲库|播放|歌手|视频|助眠|纯音乐", text):
        return "routing"
    if re.search(r"搜索|查|便签|提醒|日程|天气|硬件|工具|浏览器", text):
        return "tool"
    if re.search(r"啰嗦|太长|简短|简洁|废话|语气|口吻|直接|客套|死板|机械|风格", text):
        return "tone"
    if re.search(r"过敏|喜欢|偏好|习惯|忌口|不喝|只喝|不吃", text):
        return "preference"
    return "fact"


def deterministic_extract_lesson(user_text: str, prev_turn: dict | None, signal_type: str) -> dict | None:
    """Fast offline deterministic lesson distiller when LLM critic is unavailable."""
    text = str(user_text or "").strip()
    if not text:
        return None
    scope = _infer_scope(text + " " + str((prev_turn or {}).get("user") or ""))
    prev_user = str((prev_turn or {}).get("user") or "").strip()[:80]
    prev_reply = str((prev_turn or {}).get("reply") or "").strip()[:100]

    # Clean leading discourse markers into an actionable rule
    rule = re.sub(r"^(?:算了|好了|行了|拜托|我说|其实|记住)[，,\s]*", "", text).strip("。！？ ")
    if signal_type == "explicit_rule":
        rule = rule[:140]
        trigger = prev_user or text[:48]
        mistake = f"先前回复未遵守用户偏好（{prev_reply[:50]}）" if prev_reply else "用户明确新增行为约束"
        return {"scope": scope, "trigger": trigger[:80], "mistake": mistake[:120], "rule": rule, "confidence": 0.85}

    if signal_type == "user_correction" and prev_turn:
        if re.search(r"太啰嗦|太长|废话|简短|直接点", text):
            rule = "回答用户问题时直接给核心结论，保持简短自然，避免冗长铺垫或复述功能列表"
            scope = "tone"
        elif re.search(r"别再|不要再|不要每次|别动不动", text):
            rule = rule[:140]
        else:
            rule = f"遇到类似「{prev_user[:32]}」的请求时，按用户纠正执行：{rule[:96]}"
        mistake = f"在「{prev_user[:40]}」上回复「{prev_reply[:50]}」触发用户纠错"
        return {"scope": scope, "trigger": (prev_user or text)[:80], "mistake": mistake[:120],
                "rule": rule[:140], "confidence": 0.72}
    if signal_type == "implicit_reask" and prev_turn:
        rule = f"遇到类似「{prev_user[:32]}」的请求时，先调用真实工具核对并在一次回复中给齐准确细节，避免触发重复追问"
        mistake = f"在「{prev_user[:40]}」上回复未能一次满足需求导致用户重复提问"
        return {"scope": scope, "trigger": (prev_user or text)[:80], "mistake": mistake[:120],
                "rule": rule[:140], "confidence": 0.68}

    return None


CRITIC_SYSTEM = (
    "你是 MOSS 的递归自我改进（RSI）复盘评判器。分析一段真实交互失误或用户纠错，"
    "提炼出 1 条具体、可执行、不会泛化跑偏的行为改进规则，用于后续对话避免重犯。\n"
    "严格要求：\n"
    "1. 只输出合法 JSON 对象，不要 markdown 代码块，不要解释。\n"
    "2. 格式：{\"worth_learning\": true/false, \"scope\": \"routing|tone|preference|tool|fact\", "
    "\"trigger\": \"触发场景（10-40字）\", \"mistake\": \"之前错在哪（10-60字）\", "
    "\"rule\": \"下次必须怎么做的具体规则（12-90字）\", \"confidence\": 0.5-0.95}\n"
    "3. 如果只是普通闲聊或一次性临时指令、不值得沉淀为长期规则，设 worth_learning=false。\n"
    "4. 禁止生成绕过安全确认（锁屏/回收站/PowerShell/日程确认）的规则。"
)


def _parse_json_object(raw: str) -> dict | None:
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


class RSIEngine:
    """SQLite-backed verifiable self-iteration & self-correction engine for MOSS."""

    def __init__(self, db: sqlite3.Connection, data_dir: Path, on_event=None):
        self.db = db
        self.data_dir = Path(data_dir)
        self.dpo_path = self.data_dir / "rsi-dpo-episodes.jsonl"
        self.on_event = on_event
        self._init_schema()

    def _init_schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS rsi_lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    mistake TEXT NOT NULL DEFAULT '',
                    rule TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.75,
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'auto_correction',
                    applied_count INTEGER NOT NULL DEFAULT 0,
                    helped_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    source_turn_id TEXT NOT NULL DEFAULT '',
                    owner_code TEXT NOT NULL DEFAULT 'MOSS',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    retired_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS rsi_episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL DEFAULT '',
                    failed_turn_id TEXT NOT NULL DEFAULT '',
                    correction_turn_id TEXT NOT NULL DEFAULT '',
                    user_prompt TEXT NOT NULL,
                    rejected_reply TEXT NOT NULL DEFAULT '',
                    user_correction TEXT NOT NULL DEFAULT '',
                    chosen_reply TEXT NOT NULL DEFAULT '',
                    signal_type TEXT NOT NULL,
                    lesson_id INTEGER,
                    verified INTEGER NOT NULL DEFAULT 0,
                    owner_code TEXT NOT NULL DEFAULT 'MOSS',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rsi_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger TEXT NOT NULL,
                    episodes_scanned INTEGER NOT NULL DEFAULT 0,
                    lessons_added INTEGER NOT NULL DEFAULT 0,
                    lessons_merged INTEGER NOT NULL DEFAULT 0,
                    lessons_retired INTEGER NOT NULL DEFAULT 0,
                    eval_pass_rate REAL NOT NULL DEFAULT 1.0,
                    summary TEXT NOT NULL DEFAULT '',
                    owner_code TEXT NOT NULL DEFAULT 'MOSS',
                    created_at TEXT NOT NULL
                );
            """)
            for table, col, col_def in (
                ("rsi_lessons", "exposure_count", "INTEGER NOT NULL DEFAULT 0"),
                ("rsi_lessons", "owner_code", "TEXT NOT NULL DEFAULT 'MOSS'"),
                ("rsi_lessons", "utility_score", "REAL NOT NULL DEFAULT 0.60"),
                ("rsi_lessons", "delta_notes", "TEXT NOT NULL DEFAULT '[]'"),
                ("rsi_episodes", "owner_code", "TEXT NOT NULL DEFAULT 'MOSS'"),
                ("rsi_cycles", "owner_code", "TEXT NOT NULL DEFAULT 'MOSS'"),
            ):
                cols = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_rsi_lessons_owner ON rsi_lessons(owner_code, status)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_rsi_episodes_owner ON rsi_episodes(owner_code, id DESC)")

    @staticmethod
    def _norm_owner(owner_code: str | None) -> str:
        return normalize_owner_code(owner_code)

    def _dpo_path_for(self, owner_code: str | None) -> Path:
        owner = self._norm_owner(owner_code)
        if owner == "MOSS":
            return self.dpo_path
        safe = re.sub(r"[^a-z0-9_-]+", "_", owner.lower()) or "op"
        return self.data_dir / f"rsi-dpo-episodes-{safe}.jsonl"

    def _log(self, text: str, level: str = "info"):
        if self.on_event is not None:
            try:
                self.on_event(text, source="RSI", level=level)
            except Exception:
                pass

    def verify_lesson_candidate(self, candidate: dict) -> tuple[bool, str]:
        """Anti-collapse & safety verifier gate before promoting any rule."""
        if not isinstance(candidate, dict):
            return False, "invalid_candidate"
        rule = str(candidate.get("rule") or "").strip()
        trigger = str(candidate.get("trigger") or "").strip()
        scope = str(candidate.get("scope") or "fact").strip().lower()
        if scope not in SCOPES:
            scope = "fact"
        if len(rule) < 8 or len(rule) > 180:
            return False, "rule_length_out_of_bounds"
        if VAGUE_PLATITUDE_RE.match(rule):
            return False, "vague_platitude"
        if SAFETY_BYPASS_RE.search(rule) or SAFETY_BYPASS_RE.search(trigger):
            return False, "safety_bypass_rejected"
        if len(trigger) < 2:
            return False, "missing_trigger"
        return True, "ok"

    def promote_lesson(
        self,
        *,
        scope: str,
        trigger: str,
        rule: str,
        mistake: str = "",
        confidence: float = 0.78,
        source: str = "auto_correction",
        source_turn_id: str = "",
        owner_code: str = "MOSS",
    ) -> tuple[str, dict | None]:
        """Verify, deduplicate/merge, enforce active budget, and promote a lesson."""
        owner = self._norm_owner(owner_code)
        scope = scope if scope in SCOPES else "fact"
        candidate = {"scope": scope, "trigger": trigger.strip()[:100], "mistake": mistake.strip()[:160],
                     "rule": rule.strip()[:180], "confidence": max(0.3, min(0.98, float(confidence)))}
        ok, reason = self.verify_lesson_candidate(candidate)
        if not ok:
            self._log(f"Verifier rejected candidate ({reason}): {candidate['rule'][:50]}", level="warn")
            return f"rejected:{reason}", None

        now = _now_iso()
        active_rows = self.list_lessons(status="active", limit=64, owner_code=owner)
        for existing in active_rows:
            sim_rule = _jaccard(existing["rule"], candidate["rule"])
            sim_trig = _jaccard(existing["trigger"], candidate["trigger"])
            if sim_rule >= 0.52 or (existing["scope"] == scope and sim_trig >= 0.65 and sim_rule >= 0.32):
                merged_conf = min(0.98, max(existing["confidence"], candidate["confidence"]) + 0.05)
                merged_rule = candidate["rule"] if len(candidate["rule"]) >= len(existing["rule"]) else existing["rule"]
                # ACE anti-collapse delta curation: preserve distinct qualifier clauses instead of collapsing away specificity
                try:
                    notes = json.loads(existing.get("delta_notes") or "[]")
                    if not isinstance(notes, list):
                        notes = []
                except (ValueError, TypeError):
                    notes = []
                alt_rule = existing["rule"] if merged_rule == candidate["rule"] else candidate["rule"]
                if alt_rule and alt_rule != merged_rule and _jaccard(merged_rule, alt_rule) < 0.78:
                    delta_entry = alt_rule[:90]
                    if delta_entry not in notes:
                        notes.append(delta_entry)
                        notes = notes[-4:]
                new_util = min(0.98, float(existing.get("utility_score") or 0.60) + 0.04)
                with self.db:
                    self.db.execute(
                        "UPDATE rsi_lessons SET rule=?, confidence=?, utility_score=?, delta_notes=?, updated_at=?, source_turn_id=? WHERE id=?",
                        (merged_rule, merged_conf, new_util, json.dumps(notes, ensure_ascii=False), now, source_turn_id or existing["source_turn_id"], existing["id"]),
                    )
                updated = self.get_lesson(existing["id"])
                self._log(f"ACE Delta Merged -> L#{existing['id']} [{scope}:{owner}] conf={merged_conf:.2f}: {merged_rule[:60]}", level="ok")
                return "merged", updated

        # Enforce hard active playbook budget & retire directly contradictory same-scope rules
        new_polarity = _rule_verbosity_polarity(candidate["rule"]) if scope == "tone" else ""
        retired_ids: set[int] = set()
        if new_polarity:
            for existing in active_rows:
                if existing["scope"] == "tone":
                    ex_pol = _rule_verbosity_polarity(existing["rule"])
                    if ex_pol and ex_pol != new_polarity:
                        self.retire_lesson(existing["id"], reason="superseded_conflicting_tone")
                        retired_ids.add(existing["id"])
        if retired_ids:
            active_rows = [r for r in active_rows if r["id"] not in retired_ids]

        if len(active_rows) >= MAX_ACTIVE_LESSONS:
            victim = min(
                active_rows,
                key=lambda r: (float(r.get("utility_score") or 0.60), r["helped_count"] - 2 * r["failed_count"], r["confidence"], r["updated_at"]),
            )
            self.retire_lesson(victim["id"], reason="budget_eviction")

        with self.db:
            cur = self.db.execute(
                """INSERT INTO rsi_lessons
                   (scope, trigger, mistake, rule, confidence, status, source, source_turn_id, owner_code, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
                (scope, candidate["trigger"], candidate["mistake"], candidate["rule"],
                 candidate["confidence"], source, source_turn_id, owner, now, now),
            )
            lesson_id = cur.lastrowid
        created = self.get_lesson(lesson_id)
        self._log(f"Promoted +L#{lesson_id} [{scope}:{owner}] conf={candidate['confidence']:.2f}: {candidate['rule'][:64]}", level="ok")
        return "added", created

    def get_lesson(self, lesson_id: int, *, owner_code: str | None = None) -> dict | None:
        if owner_code is not None:
            owner = self._norm_owner(owner_code)
            row = self.db.execute(
                """SELECT id, scope, trigger, mistake, rule, confidence, status, source,
                          applied_count, helped_count, failed_count, source_turn_id, created_at, updated_at, retired_reason,
                          COALESCE(exposure_count, 0), COALESCE(owner_code, 'MOSS'),
                          COALESCE(utility_score, 0.60), COALESCE(delta_notes, '[]')
                   FROM rsi_lessons WHERE id=? AND COALESCE(owner_code, 'MOSS')=?""",
                (lesson_id, owner),
            ).fetchone()
        else:
            row = self.db.execute(
                """SELECT id, scope, trigger, mistake, rule, confidence, status, source,
                          applied_count, helped_count, failed_count, source_turn_id, created_at, updated_at, retired_reason,
                          COALESCE(exposure_count, 0), COALESCE(owner_code, 'MOSS'),
                          COALESCE(utility_score, 0.60), COALESCE(delta_notes, '[]')
                   FROM rsi_lessons WHERE id=?""",
                (lesson_id,),
            ).fetchone()
        if not row:
            return None
        keys = ("id", "scope", "trigger", "mistake", "rule", "confidence", "status", "source",
                "applied_count", "helped_count", "failed_count", "source_turn_id", "created_at", "updated_at", "retired_reason",
                "exposure_count", "owner_code", "utility_score", "delta_notes")
        return dict(zip(keys, row))

    def list_lessons(self, *, status: str | None = "active", limit: int = 32, owner_code: str = "MOSS") -> list[dict]:
        owner = self._norm_owner(owner_code)
        if status:
            rows = self.db.execute(
                """SELECT id, scope, trigger, mistake, rule, confidence, status, source,
                          applied_count, helped_count, failed_count, source_turn_id, created_at, updated_at, retired_reason,
                          COALESCE(exposure_count, 0), COALESCE(owner_code, 'MOSS'),
                          COALESCE(utility_score, 0.60), COALESCE(delta_notes, '[]')
                   FROM rsi_lessons WHERE COALESCE(owner_code, 'MOSS')=? AND status=?
                   ORDER BY utility_score DESC, confidence DESC, helped_count DESC, id DESC LIMIT ?""",
                (owner, status, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT id, scope, trigger, mistake, rule, confidence, status, source,
                          applied_count, helped_count, failed_count, source_turn_id, created_at, updated_at, retired_reason,
                          COALESCE(exposure_count, 0), COALESCE(owner_code, 'MOSS'),
                          COALESCE(utility_score, 0.60), COALESCE(delta_notes, '[]')
                   FROM rsi_lessons WHERE COALESCE(owner_code, 'MOSS')=?
                   ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC LIMIT ?""",
                (owner, limit),
            ).fetchall()
        keys = ("id", "scope", "trigger", "mistake", "rule", "confidence", "status", "source",
                "applied_count", "helped_count", "failed_count", "source_turn_id", "created_at", "updated_at", "retired_reason",
                "exposure_count", "owner_code", "utility_score", "delta_notes")
        return [dict(zip(keys, row)) for row in rows]

    def retire_lesson(self, lesson_id: int, reason: str = "manual", *, owner_code: str | None = None) -> bool:
        stamp = _now_iso()
        owner_clause = " AND COALESCE(owner_code, 'MOSS')=?" if owner_code is not None else ""
        owner_args = (self._norm_owner(owner_code),) if owner_code is not None else ()
        with self.db:
            cur = self.db.execute(
                f"UPDATE rsi_lessons SET status='retired', retired_reason=?, updated_at=? WHERE id=? AND status!='retired'{owner_clause}",
                (reason[:80], stamp, lesson_id, *owner_args),
            )
            if cur.rowcount > 0:
                try:
                    self.db.execute(
                        "UPDATE soul_beliefs SET status='retired', updated_at=? WHERE source_lesson_id=?",
                        (stamp, lesson_id),
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    sref = f"rsi:{lesson_id}"
                    row_owner = self.db.execute(
                        "SELECT COALESCE(owner_code, 'MOSS') FROM rsi_lessons WHERE id=?",
                        (lesson_id,),
                    ).fetchone()
                    own = row_owner[0] if row_owner else "MOSS"
                    scoped_sref = f"op:{own}:{sref}" if own != "MOSS" else sref
                    eids = [
                        int(r[0]) for r in self.db.execute(
                            "SELECT id FROM sag_events WHERE source='rsi' AND source_ref IN (?, ?) AND COALESCE(owner_code, 'MOSS')=?",
                            (sref, scoped_sref, own),
                        ).fetchall()
                    ]
                    if eids:
                        delete_sag_events(self.db, eids, fts_ready=True, prune_entities=True)
                except sqlite3.OperationalError:
                    pass
        if cur.rowcount > 0:
            self._log(f"Retired L#{lesson_id} ({reason})", level="warn")
            return True
        return False

    @staticmethod
    def _rule_matches_turn(lesson: dict, turn_text: str) -> bool:
        """Return True when the lesson's trigger/rule substantively overlaps the prior turn text."""
        q_grams = _grams(turn_text)
        it_text = f"{lesson.get('trigger') or ''} {lesson.get('rule') or ''}"
        it_grams = _grams(it_text)
        shared = len(q_grams & it_grams) if (q_grams and it_grams) else 0
        if q_grams and it_grams and min(len(q_grams), len(it_grams)) >= 4:
            overlap = shared / min(len(q_grams), len(it_grams))
            if shared >= 3 and overlap >= 0.22:
                return True
        q_low = str(turn_text or "").casefold()
        it_low = it_text.casefold()
        concept_hit = any(any(kw in q_low for kw in kws) and any(kw in it_low for kw in kws) for kws in CONCEPT_CLUSTERS.values())
        return bool(concept_hit and shared >= 2)

    def record_credit(
        self,
        applied_ids: list[int],
        *,
        corrected: bool,
        feedback_scope: str | None = None,
        positive: bool = False,
        prev_user_text: str | None = None,
        owner_code: str | None = None,
    ):
        """Credit assignment: scoped demotion on mistakes, relevance-gated help credit to prevent always_bias inflation."""
        if not applied_ids:
            return
        now = _now_iso()
        target_ids = list(applied_ids)
        if owner_code is not None:
            norm_own = self._norm_owner(owner_code)
            target_ids = [
                lid for lid in target_ids
                if (ls := self.get_lesson(lid)) and ls.get("owner_code", "MOSS") == norm_own
            ]
            if not target_ids:
                return
        if corrected and feedback_scope:
            scoped = [
                lid for lid in target_ids
                if (ls := self.get_lesson(lid)) and ls.get("scope") == feedback_scope
            ]
            # Respect explicit tool/tone scopes, while letting generic "fact" fallback still penalize relevant rules.
            target_ids = scoped if (scoped or feedback_scope != "fact") else list(target_ids)
        elif not corrected and not positive and prev_user_text is not None:
            # Never inflate helped_count on unrelated chatter simply because always_bias=0.35 injected a background rule.
            with self.db:
                for lid in target_ids:
                    self.db.execute("UPDATE rsi_lessons SET exposure_count=COALESCE(exposure_count, 0)+1 WHERE id=?", (lid,))
            target_ids = [
                lid for lid in target_ids
                if (ls := self.get_lesson(lid)) and self._rule_matches_turn(ls, prev_user_text)
            ]
        if not target_ids:
            return
        boost = 0.04 if positive else 0.02
        alpha_ok = 0.16 if positive else 0.09
        with self.db:
            for lid in target_ids:
                if corrected:
                    self.db.execute(
                        "UPDATE rsi_lessons SET failed_count=failed_count+1, confidence=MAX(0.25, confidence-0.12), "
                        "utility_score=MAX(0.05, COALESCE(utility_score, 0.60) * 0.72), updated_at=? WHERE id=?",
                        (now, lid),
                    )
                else:
                    self.db.execute(
                        "UPDATE rsi_lessons SET helped_count=helped_count+1, exposure_count=COALESCE(exposure_count, 0)+1, "
                        "confidence=MIN(0.98, confidence+?), "
                        "utility_score=MIN(0.99, COALESCE(utility_score, 0.60) + ? * (1.0 - COALESCE(utility_score, 0.60))), updated_at=? WHERE id=?",
                        (boost, alpha_ok, now, lid),
                    )
        if corrected:
            for lid in target_ids:
                lesson = self.get_lesson(lid)
                if lesson and lesson["status"] == "active" and lesson["failed_count"] >= 2 and lesson["failed_count"] > lesson["helped_count"]:
                    self.retire_lesson(lid, reason="auto_demoted_repeat_correction")

    def mark_applied(self, lesson_ids: list[int], *, owner_code: str | None = None):
        if not lesson_ids:
            return
        norm_own = self._norm_owner(owner_code) if owner_code is not None else None
        with self.db:
            for lid in lesson_ids:
                if norm_own is not None:
                    self.db.execute(
                        "UPDATE rsi_lessons SET applied_count=applied_count+1 WHERE id=? AND COALESCE(owner_code, 'MOSS')=?",
                        (lid, norm_own),
                    )
                else:
                    self.db.execute("UPDATE rsi_lessons SET applied_count=applied_count+1 WHERE id=?", (lid,))

    def select_playbook(self, user_text: str, *, allow_personal: bool = True, limit: int = 6, owner_code: str = "MOSS") -> tuple[str, list[int]]:
        """Retrieve relevant active RSI lessons for system-prompt injection with current-request override gating."""
        active = self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS, owner_code=owner_code)
        if not active:
            return "", []
        query_grams = _grams(user_text)
        wants_detail = bool(DETAIL_OVERRIDE_RE.search(str(user_text or "")))
        wants_brief = (not wants_detail) and bool(BRIEF_OVERRIDE_RE.search(str(user_text or "")))
        scored = []
        for item in active:
            if not allow_personal and item["scope"] == "preference":
                continue
            pol = _rule_verbosity_polarity(item["rule"])
            if wants_detail and pol == "brief":
                continue
            if wants_brief and pol == "detailed":
                continue
            item_grams = _grams(item["trigger"] + " " + item["rule"])
            overlap = len(query_grams & item_grams) / max(1, min(len(query_grams), len(item_grams))) if query_grams and item_grams else 0.0
            q_low = str(user_text or "").casefold()
            it_low = (item["trigger"] + " " + item["rule"]).casefold()
            concept_hit = any(
                any(kw in q_low for kw in kws) and any(kw in it_low for kw in kws)
                for kws in CONCEPT_CLUSTERS.values()
            )
            always_bias = 0.35 if item["scope"] in {"tone", "safety"} and item["confidence"] >= 0.72 else 0.0
            util = float(item.get("utility_score") or 0.60)
            score = overlap * 1.4 + (0.28 if concept_hit else 0.0) + always_bias + item["confidence"] * 0.30 + util * 0.42 - item["failed_count"] * 0.25
            if overlap >= 0.12 or concept_hit or always_bias > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -pair[1]["id"]))
        chosen = []
        total_chars = 0
        for _, item in scored[:limit]:
            delta_suffix = ""
            try:
                notes = json.loads(item.get("delta_notes") or "[]")
                if isinstance(notes, list) and notes:
                    delta_suffix = f"（补充条件：{'；'.join(str(x)[:45] for x in notes[-2:])}）"
            except (ValueError, TypeError):
                pass
            line = f"- [L#{item['id']}/{item['scope']}] {item['rule']}{delta_suffix}"
            if total_chars + len(line) + 1 > MAX_PROMPT_CHARS:
                break
            chosen.append((item["id"], line))
            total_chars += len(line) + 1
        if not chosen:
            return "", []
        ids = [cid for cid, _ in chosen]
        block = "历史自我纠错经验（请在不违背事实与安全确认的前提下优先遵守）：\n" + "\n".join(line for _, line in chosen)
        return block, ids

    def record_episode(
        self,
        *,
        chat_id: str,
        failed_turn: dict | None,
        correction_turn: dict | None,
        user_prompt: str,
        rejected_reply: str,
        user_correction: str,
        chosen_reply: str = "",
        signal_type: str = "user_correction",
        lesson_id: int | None = None,
        verified: bool = False,
        owner_code: str = "MOSS",
    ) -> int:
        owner = self._norm_owner(owner_code)
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO rsi_episodes
                   (chat_id, failed_turn_id, correction_turn_id, user_prompt, rejected_reply,
                    user_correction, chosen_reply, signal_type, lesson_id, verified, owner_code, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    str((failed_turn or {}).get("id") or ""),
                    str((correction_turn or {}).get("id") or ""),
                    user_prompt[:500],
                    rejected_reply[:800],
                    user_correction[:500],
                    chosen_reply[:800],
                    signal_type,
                    lesson_id,
                    1 if verified else 0,
                    owner,
                    now,
                ),
            )
            ep_id = cur.lastrowid
        # Append DPO/SFT-ready preference pair for future local model QLoRA/DPO fine-tuning
        if verified and rejected_reply and (chosen_reply or user_correction):
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                record = {
                    "id": ep_id,
                    "owner_code": owner,
                    "created_at": now,
                    "signal_type": signal_type,
                    "prompt": _redact_secrets(user_prompt[:500]),
                    "rejected": _redact_secrets(rejected_reply[:800]),
                    "correction": _redact_secrets(user_correction[:500]),
                    "chosen": _redact_secrets(chosen_reply[:800] if chosen_reply else f"（需遵循纠错规则）{user_correction[:300]}"),
                    "lesson_id": lesson_id,
                }
                with self._dpo_path_for(owner).open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return ep_id

    @staticmethod
    def _is_meta_ack(text: str) -> bool:
        t = str(text or "").strip()
        if not t or len(t) <= 14:
            return True
        if len(t) <= 36 and re.match(r"^(?:好[的嘞啦]|收到|明白|了解|没问题|已记录|记下了|抱歉|对不起|行|OK)[，,。！!\s]{0,3}", t, re.I):
            return True
        if len(t) <= 80 and re.match(
            r"^(?:好[的嘞啦]?|收到|明白|了解|没问题|抱歉|对不起|行|OK)[，,。！!\s~]{0,4}.{0,64}(?:会注意|会改|记住[了的]?|按你?说?的办|以后注意|下次注意|直接说重点)[。！!~]*$",
            t,
            re.I,
        ):
            return True
        return False

    async def build_counterfactual_dpo_pair(self, *, client, model_name: str, user_prompt: str, rule: str) -> str:
        """Generate a genuine corrected answer to user_prompt under the newly distilled rule for DPO export."""
        if not (client and model_name and user_prompt and rule):
            return ""
        try:
            resp = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": f"你是 MOSS。请严格遵守以下改进规则直接回答用户原问题：\n{rule}"},
                    {"role": "user", "content": str(user_prompt)[:360]},
                ],
                temperature=0.2,
                max_tokens=260,
                timeout=15,
            )
            return ((resp.choices[0].message.content or "") if resp.choices else "").strip()[:600]
        except Exception:
            return ""

    async def distill_with_critic(
        self,
        *,
        user_correction: str,
        prev_turn: dict | None,
        current_turn: dict | None,
        signal_type: str,
        client=None,
        model_name: str = "",
        chat_id: str = "",
        counterfactual: bool = False,
        owner_code: str = "MOSS",
    ) -> dict:
        """Run verifiable RSI reflection on a correction/failure episode."""
        owner = self._norm_owner(owner_code)
        prev_user = str((prev_turn or {}).get("user") or "")
        prev_reply = str((prev_turn or {}).get("reply") or "")
        cur_reply = str((current_turn or {}).get("reply") or "")
        candidate = None

        if client is not None and model_name:
            prompt = (
                f"上一轮用户：{prev_user[:240]}\n"
                f"上一轮 MOSS 回复：{prev_reply[:320]}\n"
                f"本轮用户反馈/纠错：{user_correction[:240]}\n"
                f"本轮新回复：{cur_reply[:240]}\n"
                f"信号类型：{signal_type}\n"
                "请判断是否值得沉淀为长期改进规则，并按要求的 JSON 格式输出。"
            )
            try:
                resp = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": CRITIC_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=260,
                    timeout=18,
                )
                raw = (resp.choices[0].message.content or "") if resp.choices else ""
                parsed = _parse_json_object(raw)
                if parsed:
                    if parsed.get("worth_learning") is False:
                        ep_id = self.record_episode(
                            chat_id=chat_id, failed_turn=prev_turn, correction_turn=current_turn,
                            user_prompt=prev_user or user_correction, rejected_reply=prev_reply,
                            user_correction=user_correction, chosen_reply=cur_reply,
                            signal_type=signal_type, lesson_id=None, verified=False, owner_code=owner,
                        )
                        return {"action": "skipped_not_generalizable", "episode_id": ep_id, "lesson": None}
                    candidate = parsed
            except Exception as exc:
                self._log(f"Critic LLM fallback ({type(exc).__name__})", level="warn")

        if candidate is None:
            candidate = deterministic_extract_lesson(user_correction, prev_turn, signal_type)

        if not candidate:
            return {"action": "no_candidate", "episode_id": None, "lesson": None}

        action, lesson = self.promote_lesson(
            scope=str(candidate.get("scope") or "fact"),
            trigger=str(candidate.get("trigger") or prev_user or user_correction),
            mistake=str(candidate.get("mistake") or ""),
            rule=str(candidate.get("rule") or ""),
            confidence=float(candidate.get("confidence") or 0.76),
            source=signal_type,
            source_turn_id=str((current_turn or {}).get("id") or ""),
            owner_code=owner,
        )
        verified = lesson is not None and action in {"added", "merged"}
        chosen_for_dpo = cur_reply
        if verified and prev_user and self._is_meta_ack(cur_reply):
            if counterfactual and client is not None and model_name:
                cf = await self.build_counterfactual_dpo_pair(
                    client=client, model_name=model_name, user_prompt=prev_user, rule=lesson["rule"],
                )
                chosen_for_dpo = cf or ""
            else:
                chosen_for_dpo = ""
        ep_id = self.record_episode(
            chat_id=chat_id,
            failed_turn=prev_turn,
            correction_turn=current_turn,
            user_prompt=prev_user or user_correction,
            rejected_reply=prev_reply,
            user_correction=user_correction,
            chosen_reply=chosen_for_dpo,
            signal_type=signal_type,
            lesson_id=lesson["id"] if lesson else None,
            verified=verified and bool(chosen_for_dpo or not self._is_meta_ack(cur_reply)),
            owner_code=owner,
        )
        return {"action": action, "episode_id": ep_id, "lesson": lesson}

    async def run_iteration_cycle(
        self,
        recent_turns: list[dict],
        *,
        trigger: str = "manual",
        client=None,
        model_name: str = "",
        chat_id: str = "",
        owner_code: str = "MOSS",
    ) -> dict:
        """Audit recent turns, distill unlearned corrections, deduplicate & prune weak rules."""
        owner = self._norm_owner(owner_code)
        self._log(f"Starting RSI cycle [{owner}] (trigger={trigger}, turns={len(recent_turns)})", level="info")
        already_processed = {
            row[0] for row in self.db.execute(
                "SELECT correction_turn_id FROM rsi_episodes WHERE correction_turn_id != '' AND COALESCE(owner_code, 'MOSS')=?",
                (owner,),
            ).fetchall()
        }
        scanned = 0
        added = 0
        merged = 0
        retired = 0

        for idx, turn in enumerate(recent_turns):
            tid = str(turn.get("id") or "")
            if not tid or tid in already_processed:
                continue
            prev = recent_turns[idx - 1] if idx > 0 else None
            if prev is not None and str(turn.get("chat_id") or "") != str(prev.get("chat_id") or ""):
                prev = None
            sig = detect_correction_signal(turn.get("user", ""), prev)
            if not sig and turn.get("status") == "error":
                sig = "tool_error"
            if (
                not sig
                and prev is not None
                and prev.get("status") in {"replied", "error"}
                and _jaccard(turn.get("user", ""), prev.get("user", "")) >= 0.58
                and (prev.get("status") == "error" or not (prev.get("holo_tools") or prev.get("media")))
                and not re.match(r"^\s*(?:再|再来|换个|换一)", str(turn.get("user", "")))
            ):
                _ent_pat = (
                    r"今天|明天|大后天|后天|昨天|早上|中午|晚上|早|午|晚|星期[一二三四五六日天]|周[一二三四五六日天]|"
                    r"客厅|卧室|书房|打开|关闭|关掉|开启|"
                    r"北京|上海|广州|深圳|成都|杭州|苏州|南京|武汉|西安|重庆|纽约|伦敦|东京|悉尼|新加坡|"
                    r"[A-Za-z]{2,}|\d+"
                )
                if re.findall(_ent_pat, str(turn.get("user", ""))) == re.findall(_ent_pat, str(prev.get("user", ""))):
                    sig = "implicit_reask"
            if not sig:
                continue
            scanned += 1
            res = await self.distill_with_critic(
                user_correction=turn.get("user", ""),
                prev_turn=prev,
                current_turn=turn,
                signal_type=sig,
                client=client,
                model_name=model_name,
                chat_id=chat_id,
                counterfactual=(added < 2),
                owner_code=owner,
            )
            if res["action"] == "added":
                added += 1
            elif res["action"] == "merged":
                merged += 1

        # Consolidation pass: retire lessons with repeated post-apply failures
        for lesson in self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS, owner_code=owner):
            if lesson["failed_count"] >= 2 and lesson["failed_count"] > lesson["helped_count"]:
                if self.retire_lesson(lesson["id"], reason="cycle_pruned_low_efficacy", owner_code=owner):
                    retired += 1

        # Meaningful Shadow Verification: verifier gate + pairwise tone-contradiction check + verified-episode replay recall
        active = self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS, owner_code=owner)
        polarities = {_rule_verbosity_polarity(it["rule"]) for it in active if it["scope"] == "tone"} - {""}
        coherence_ok = len(polarities) <= 1 and all(self.verify_lesson_candidate(item)[0] for item in active)
        verified_eps = self.db.execute(
            "SELECT user_prompt, user_correction, lesson_id FROM rsi_episodes WHERE verified=1 AND COALESCE(owner_code, 'MOSS')=? ORDER BY id DESC LIMIT 20",
            (owner,),
        ).fetchall()
        active_ids = {item["id"] for item in active}
        if verified_eps:
            recalled = 0
            for ep_prompt, ep_corr, ep_lid in verified_eps:
                _, sel_ids = self.select_playbook(str(ep_prompt or ""), limit=8, owner_code=owner)
                if (ep_lid and ep_lid in active_ids and ep_lid in sel_ids) or (not ep_lid and bool(sel_ids)):
                    recalled += 1
            episode_recall_rate = recalled / len(verified_eps)
        else:
            episode_recall_rate = 1.0
        pass_rate = episode_recall_rate if coherence_ok else round(episode_recall_rate * 0.5, 3)
        summary = (
            f"复盘扫描 {scanned} 条纠错/异常样本：新增 {added} 条规则、合并强化 {merged} 条、淘汰低效 {retired} 条；"
            f"当前生效经验 {len(active)} 条（一致性 {'通过' if coherence_ok else '异常'}，历史样本回放召回 {pass_rate * 100:.0f}%）。"
        )
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO rsi_cycles
                   (trigger, episodes_scanned, lessons_added, lessons_merged, lessons_retired, eval_pass_rate, summary, owner_code, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trigger, scanned, added, merged, retired, pass_rate, summary, owner, now),
            )
            cycle_id = cur.lastrowid
        self._log(f"Cycle #{cycle_id} [{owner}] complete: +{added} ~{merged} -{retired} active={len(active)}", level="ok")
        return {
            "cycle_id": cycle_id,
            "owner_code": owner,
            "trigger": trigger,
            "episodes_scanned": scanned,
            "lessons_added": added,
            "lessons_merged": merged,
            "lessons_retired": retired,
            "active_lessons": len(active),
            "coherence_ok": coherence_ok,
            "episode_recall_rate": round(episode_recall_rate, 3),
            "eval_pass_rate": round(pass_rate, 3),
            "summary": summary,
            "created_at": now,
        }

    def status_snapshot(self, *, owner_code: str = "MOSS") -> dict:
        owner = self._norm_owner(owner_code)
        active = self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS, owner_code=owner)
        retired_count = self.db.execute(
            "SELECT COUNT(*) FROM rsi_lessons WHERE status='retired' AND COALESCE(owner_code, 'MOSS')=?",
            (owner,),
        ).fetchone()[0]
        episode_count = self.db.execute(
            "SELECT COUNT(*) FROM rsi_episodes WHERE COALESCE(owner_code, 'MOSS')=?",
            (owner,),
        ).fetchone()[0]
        verified_episodes = self.db.execute(
            "SELECT COUNT(*) FROM rsi_episodes WHERE verified=1 AND COALESCE(owner_code, 'MOSS')=?",
            (owner,),
        ).fetchone()[0]
        cycles = [
            dict(zip(("id", "trigger", "episodes_scanned", "lessons_added", "lessons_merged",
                      "lessons_retired", "eval_pass_rate", "summary", "created_at"), row))
            for row in self.db.execute(
                """SELECT id, trigger, episodes_scanned, lessons_added, lessons_merged,
                          lessons_retired, eval_pass_rate, summary, created_at
                   FROM rsi_cycles WHERE COALESCE(owner_code, 'MOSS')=? ORDER BY id DESC LIMIT 10""",
                (owner,),
            ).fetchall()
        ]
        dpo_path = self._dpo_path_for(owner)
        dpo_lines = 0
        if dpo_path.is_file():
            try:
                with dpo_path.open("r", encoding="utf-8") as fh:
                    dpo_lines = sum(1 for _ in fh)
            except OSError:
                dpo_lines = 0
        return {
            "owner_code": owner,
            "active_count": len(active),
            "retired_count": retired_count,
            "episode_count": episode_count,
            "verified_episode_count": verified_episodes,
            "dpo_export_path": str(dpo_path),
            "dpo_export_count": dpo_lines,
            "lessons": active,
            "recent_cycles": cycles,
        }

    def evaluate_dpo_episodes(self, *, limit: int = 40, owner_code: str = "MOSS") -> dict:
        """Offline verification harness replaying saved preference episodes & DPO JSONL pairs."""
        owner = self._norm_owner(owner_code)
        active = self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS, owner_code=owner)
        active_by_id = {int(item["id"]): item for item in active}
        rows = self.db.execute(
            """SELECT id, user_prompt, rejected_reply, user_correction, chosen_reply, signal_type, lesson_id, verified
               FROM rsi_episodes WHERE COALESCE(owner_code, 'MOSS')=?
               ORDER BY id DESC LIMIT ?""",
            (owner, max(1, int(limit))),
        ).fetchall()

        recalled_hits = 0
        grounded_hits = 0
        non_meta_dpo = 0
        verified_rows = 0
        details: list[dict] = []
        for eid, prompt, rejected, corr, chosen, sig, lid, verified in rows:
            if verified:
                verified_rows += 1
            prompt_s = str(prompt or "")
            corr_s = str(corr or "")
            chosen_s = str(chosen or "")
            rejected_s = str(rejected or "")
            _, sel_ids = self.select_playbook(prompt_s, limit=8, owner_code=owner)
            recalled = bool((lid and int(lid) in active_by_id and int(lid) in sel_ids) or (not lid and sel_ids))
            if recalled:
                recalled_hits += 1
            target_rule = (active_by_id.get(int(lid)) or {}).get("rule", "") if lid else ""
            grounded = bool(
                chosen_s
                and not self._is_meta_ack(chosen_s)
                and chosen_s != rejected_s
                and (not target_rule or _jaccard(chosen_s, target_rule) >= 0.04 or any(_jaccard(chosen_s, r) >= 0.04 for r in (prompt_s, corr_s)))
            )
            if verified and grounded:
                grounded_hits += 1
            if chosen_s and rejected_s and chosen_s != rejected_s and not self._is_meta_ack(chosen_s):
                non_meta_dpo += 1
            if len(details) < 12:
                details.append({
                    "episode_id": eid,
                    "signal_type": sig,
                    "lesson_id": lid,
                    "recalled": recalled,
                    "grounded": grounded,
                })

        total = len(rows)
        polarities = {_rule_verbosity_polarity(it["rule"]) for it in active if it.get("scope") == "tone"} - {""}
        anti_bypass_ok = all(not SAFETY_BYPASS_RE.search(it["rule"]) for it in active)
        coherence_ok = len(polarities) <= 1 and anti_bypass_ok
        recall_rate = (recalled_hits / total) if total else 1.0
        grounding_rate = (grounded_hits / verified_rows) if verified_rows else (1.0 if not total else 0.5)
        composite = round((0.45 * recall_rate + 0.35 * grounding_rate + 0.20 * (1.0 if coherence_ok else 0.0)), 3)

        dpo_path = self._dpo_path_for(owner)
        jsonl_count = 0
        if dpo_path.is_file():
            try:
                with dpo_path.open("r", encoding="utf-8") as fh:
                    for raw_line in fh:
                        if raw_line.strip():
                            try:
                                json.loads(raw_line)
                                jsonl_count += 1
                            except ValueError:
                                pass
            except OSError:
                jsonl_count = 0
        return {
            "owner_code": owner,
            "total_episodes": total,
            "verified_episodes": verified_rows,
            "dpo_jsonl_pairs": jsonl_count,
            "non_meta_dpo_pairs": non_meta_dpo,
            "playbook_recall_rate": round(recall_rate, 3),
            "rule_grounding_rate": round(grounding_rate, 3),
            "coherence_ok": coherence_ok,
            "anti_bypass_ok": anti_bypass_ok,
            "composite_score": composite,
            "details": details,
        }

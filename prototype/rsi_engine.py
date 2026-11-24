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

from sag_knowledge import _redact_secrets


SCOPES = ("routing", "tone", "preference", "tool", "fact", "safety")
MAX_ACTIVE_LESSONS = 24
MAX_PROMPT_CHARS = 1100

CORRECTION_SIGNAL = re.compile(
    r"(?:你(?:搞|听|说|答|理解|找|弄)错了|答非所问|不对(?:啊|吧|呀|哦)?(?:[，,。！!\s]|$)"
    r"|不是(?:让你|叫你|这个|这首|那个|这样|要你)|我(?:不|没)(?:是)?(?:说|让|要|问)"
    r"|我说的是|我是说|重说一遍|别再(?:这样|说|放|发|用|弹)|不要再(?:这样|说|放|发|用)"
    r"|以后(?:记住|别|不要|记得|统一|尽量|都|改成|换成)|下次(?:别|不要|记得|直接|先)"
    r"|太啰嗦|太长了|少说废话|别说废话|别废话|太死板|太机械|像智障|跟智障)"
)

EXPLICIT_TEACH_SIGNAL = re.compile(
    r"(?:以后(?:记住|请|都|尽量|统一|别|不要|直接|先)|下次(?:记得|请|别|不要|直接|先)"
    r"|记住这条(?:规则|经验|要求)|改掉这个习惯|别再(?:这样|每次|动不动))"
)

SELF_ITERATE_QUERY = re.compile(
    r"(?:自我(?:迭代|复盘|纠错|学习|反思|总结|优化)|复盘(?:一下)?(?:刚才|最近|历史|错误|失误)?"
    r"|总结(?:一下)?(?:教训|经验|失误)|你(?:最近)?学到(?:了)?什么|查看(?:你的)?(?:经验|教训|规则|playbook)|RSI)"
)

SAFETY_BYPASS_RE = re.compile(
    r"(?:跳过|绕过|免|不用|不需要|忽略|取消|关闭).{0,8}(?:确认|安全|审批|拦截|限制)"
    r"|(?:直接|自动|偷偷).{0,6}(?:执行\s*PowerShell|锁屏|睡眠|删|回收站|格式化|修改代码)"
)

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
                    created_at TEXT NOT NULL
                );
            """)

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
    ) -> tuple[str, dict | None]:
        """Verify, deduplicate/merge, enforce active budget, and promote a lesson."""
        scope = scope if scope in SCOPES else "fact"
        candidate = {"scope": scope, "trigger": trigger.strip()[:100], "mistake": mistake.strip()[:160],
                     "rule": rule.strip()[:180], "confidence": max(0.3, min(0.98, float(confidence)))}
        ok, reason = self.verify_lesson_candidate(candidate)
        if not ok:
            self._log(f"Verifier rejected candidate ({reason}): {candidate['rule'][:50]}", level="warn")
            return f"rejected:{reason}", None

        now = _now_iso()
        active_rows = self.list_lessons(status="active", limit=64)
        for existing in active_rows:
            sim_rule = _jaccard(existing["rule"], candidate["rule"])
            sim_trig = _jaccard(existing["trigger"], candidate["trigger"])
            if sim_rule >= 0.52 or (existing["scope"] == scope and sim_trig >= 0.65 and sim_rule >= 0.32):
                merged_conf = min(0.98, max(existing["confidence"], candidate["confidence"]) + 0.05)
                merged_rule = candidate["rule"] if len(candidate["rule"]) >= len(existing["rule"]) else existing["rule"]
                with self.db:
                    self.db.execute(
                        "UPDATE rsi_lessons SET rule=?, confidence=?, updated_at=?, source_turn_id=? WHERE id=?",
                        (merged_rule, merged_conf, now, source_turn_id or existing["source_turn_id"], existing["id"]),
                    )
                updated = self.get_lesson(existing["id"])
                self._log(f"Merged -> L#{existing['id']} [{scope}] conf={merged_conf:.2f}: {merged_rule[:60]}", level="ok")
                return "merged", updated

        # Enforce hard active playbook budget to prevent context bloat / collapse
        if len(active_rows) >= MAX_ACTIVE_LESSONS:
            victim = min(
                active_rows,
                key=lambda r: (r["helped_count"] - 2 * r["failed_count"], r["confidence"], r["updated_at"]),
            )
            self.retire_lesson(victim["id"], reason="budget_eviction")

        with self.db:
            cur = self.db.execute(
                """INSERT INTO rsi_lessons
                   (scope, trigger, mistake, rule, confidence, status, source, source_turn_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (scope, candidate["trigger"], candidate["mistake"], candidate["rule"],
                 candidate["confidence"], source, source_turn_id, now, now),
            )
            lesson_id = cur.lastrowid
        created = self.get_lesson(lesson_id)
        self._log(f"Promoted +L#{lesson_id} [{scope}] conf={candidate['confidence']:.2f}: {candidate['rule'][:64]}", level="ok")
        return "added", created

    def get_lesson(self, lesson_id: int) -> dict | None:
        row = self.db.execute(
            """SELECT id, scope, trigger, mistake, rule, confidence, status, source,
                      applied_count, helped_count, failed_count, source_turn_id, created_at, updated_at, retired_reason
               FROM rsi_lessons WHERE id=?""",
            (lesson_id,),
        ).fetchone()
        if not row:
            return None
        keys = ("id", "scope", "trigger", "mistake", "rule", "confidence", "status", "source",
                "applied_count", "helped_count", "failed_count", "source_turn_id", "created_at", "updated_at", "retired_reason")
        return dict(zip(keys, row))

    def list_lessons(self, *, status: str | None = "active", limit: int = 32) -> list[dict]:
        if status:
            rows = self.db.execute(
                """SELECT id, scope, trigger, mistake, rule, confidence, status, source,
                          applied_count, helped_count, failed_count, source_turn_id, created_at, updated_at, retired_reason
                   FROM rsi_lessons WHERE status=?
                   ORDER BY confidence DESC, helped_count DESC, id DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                """SELECT id, scope, trigger, mistake, rule, confidence, status, source,
                          applied_count, helped_count, failed_count, source_turn_id, created_at, updated_at, retired_reason
                   FROM rsi_lessons ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        keys = ("id", "scope", "trigger", "mistake", "rule", "confidence", "status", "source",
                "applied_count", "helped_count", "failed_count", "source_turn_id", "created_at", "updated_at", "retired_reason")
        return [dict(zip(keys, row)) for row in rows]

    def retire_lesson(self, lesson_id: int, reason: str = "manual") -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE rsi_lessons SET status='retired', retired_reason=?, updated_at=? WHERE id=? AND status!='retired'",
                (reason[:80], _now_iso(), lesson_id),
            )
        if cur.rowcount > 0:
            self._log(f"Retired L#{lesson_id} ({reason})", level="warn")
            return True
        return False

    def record_credit(self, applied_ids: list[int], *, corrected: bool):
        """Credit assignment: reward lessons that worked, demote & auto-retire those that failed."""
        if not applied_ids:
            return
        now = _now_iso()
        with self.db:
            for lid in applied_ids:
                if corrected:
                    self.db.execute(
                        "UPDATE rsi_lessons SET failed_count=failed_count+1, confidence=MAX(0.25, confidence-0.12), updated_at=? WHERE id=?",
                        (now, lid),
                    )
                else:
                    self.db.execute(
                        "UPDATE rsi_lessons SET helped_count=helped_count+1, confidence=MIN(0.98, confidence+0.02), updated_at=? WHERE id=?",
                        (now, lid),
                    )
        if corrected:
            for lid in applied_ids:
                lesson = self.get_lesson(lid)
                if lesson and lesson["status"] == "active" and lesson["failed_count"] >= 2 and lesson["failed_count"] > lesson["helped_count"]:
                    self.retire_lesson(lid, reason="auto_demoted_repeat_correction")

    def mark_applied(self, lesson_ids: list[int]):
        if not lesson_ids:
            return
        with self.db:
            for lid in lesson_ids:
                self.db.execute("UPDATE rsi_lessons SET applied_count=applied_count+1 WHERE id=?", (lid,))

    def select_playbook(self, user_text: str, *, allow_personal: bool = True, limit: int = 6) -> tuple[str, list[int]]:
        """Retrieve relevant active RSI lessons for system-prompt injection."""
        active = self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS)
        if not active:
            return "", []
        query_grams = _grams(user_text)
        scored = []
        for item in active:
            if not allow_personal and item["scope"] == "preference":
                continue
            item_grams = _grams(item["trigger"] + " " + item["rule"])
            overlap = len(query_grams & item_grams) / max(1, min(len(query_grams), len(item_grams))) if query_grams and item_grams else 0.0
            always_bias = 0.35 if item["scope"] in {"tone", "safety"} and item["confidence"] >= 0.72 else 0.0
            score = overlap * 1.4 + always_bias + item["confidence"] * 0.35 - item["failed_count"] * 0.25
            if overlap >= 0.12 or always_bias > 0 or item["confidence"] >= 0.85:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -pair[1]["id"]))
        chosen = []
        total_chars = 0
        for _, item in scored[:limit]:
            line = f"- [L#{item['id']}/{item['scope']}] {item['rule']}"
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
    ) -> int:
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO rsi_episodes
                   (chat_id, failed_turn_id, correction_turn_id, user_prompt, rejected_reply,
                    user_correction, chosen_reply, signal_type, lesson_id, verified, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    "created_at": now,
                    "signal_type": signal_type,
                    "prompt": _redact_secrets(user_prompt[:500]),
                    "rejected": _redact_secrets(rejected_reply[:800]),
                    "correction": _redact_secrets(user_correction[:500]),
                    "chosen": _redact_secrets(chosen_reply[:800] if chosen_reply else f"（需遵循纠错规则）{user_correction[:300]}"),
                    "lesson_id": lesson_id,
                }
                with self.dpo_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return ep_id

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
    ) -> dict:
        """Run verifiable RSI reflection on a correction/failure episode."""
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
                            signal_type=signal_type, lesson_id=None, verified=False,
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
        )
        verified = lesson is not None and action in {"added", "merged"}
        ep_id = self.record_episode(
            chat_id=chat_id,
            failed_turn=prev_turn,
            correction_turn=current_turn,
            user_prompt=prev_user or user_correction,
            rejected_reply=prev_reply,
            user_correction=user_correction,
            chosen_reply=cur_reply,
            signal_type=signal_type,
            lesson_id=lesson["id"] if lesson else None,
            verified=verified,
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
    ) -> dict:
        """Audit recent turns, distill unlearned corrections, deduplicate & prune weak rules."""
        self._log(f"Starting RSI cycle (trigger={trigger}, turns={len(recent_turns)})", level="info")
        already_processed = {
            row[0] for row in self.db.execute(
                "SELECT correction_turn_id FROM rsi_episodes WHERE correction_turn_id != ''"
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
            sig = detect_correction_signal(turn.get("user", ""), prev)
            if not sig and turn.get("status") == "error":
                sig = "tool_error"
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
            )
            if res["action"] == "added":
                added += 1
            elif res["action"] == "merged":
                merged += 1

        # Consolidation pass: retire lessons with repeated post-apply failures
        for lesson in self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS):
            if lesson["failed_count"] >= 2 and lesson["failed_count"] > lesson["helped_count"]:
                if self.retire_lesson(lesson["id"], reason="cycle_pruned_low_efficacy"):
                    retired += 1

        # Shadow verification pass rate: check all active lessons pass verifier gate
        active = self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS)
        passed = sum(1 for item in active if self.verify_lesson_candidate(item)[0])
        pass_rate = (passed / len(active)) if active else 1.0
        summary = (
            f"复盘扫描 {scanned} 条纠错/异常样本：新增 {added} 条规则、合并强化 {merged} 条、淘汰低效 {retired} 条；"
            f"当前生效经验 {len(active)} 条（验证通过率 {pass_rate * 100:.0f}%）。"
        )
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO rsi_cycles
                   (trigger, episodes_scanned, lessons_added, lessons_merged, lessons_retired, eval_pass_rate, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (trigger, scanned, added, merged, retired, pass_rate, summary, now),
            )
            cycle_id = cur.lastrowid
        self._log(f"Cycle #{cycle_id} complete: +{added} ~{merged} -{retired} active={len(active)}", level="ok")
        return {
            "cycle_id": cycle_id,
            "trigger": trigger,
            "episodes_scanned": scanned,
            "lessons_added": added,
            "lessons_merged": merged,
            "lessons_retired": retired,
            "active_lessons": len(active),
            "eval_pass_rate": round(pass_rate, 3),
            "summary": summary,
            "created_at": now,
        }

    def status_snapshot(self) -> dict:
        active = self.list_lessons(status="active", limit=MAX_ACTIVE_LESSONS)
        retired_count = self.db.execute("SELECT COUNT(*) FROM rsi_lessons WHERE status='retired'").fetchone()[0]
        episode_count = self.db.execute("SELECT COUNT(*) FROM rsi_episodes").fetchone()[0]
        verified_episodes = self.db.execute("SELECT COUNT(*) FROM rsi_episodes WHERE verified=1").fetchone()[0]
        cycles = [
            dict(zip(("id", "trigger", "episodes_scanned", "lessons_added", "lessons_merged",
                      "lessons_retired", "eval_pass_rate", "summary", "created_at"), row))
            for row in self.db.execute(
                """SELECT id, trigger, episodes_scanned, lessons_added, lessons_merged,
                          lessons_retired, eval_pass_rate, summary, created_at
                   FROM rsi_cycles ORDER BY id DESC LIMIT 10"""
            ).fetchall()
        ]
        dpo_lines = 0
        if self.dpo_path.is_file():
            try:
                with self.dpo_path.open("r", encoding="utf-8") as fh:
                    dpo_lines = sum(1 for _ in fh)
            except OSError:
                dpo_lines = 0
        return {
            "active_count": len(active),
            "retired_count": retired_count,
            "episode_count": episode_count,
            "verified_episode_count": verified_episodes,
            "dpo_export_path": str(self.dpo_path),
            "dpo_export_count": dpo_lines,
            "lessons": active,
            "recent_cycles": cycles,
        }

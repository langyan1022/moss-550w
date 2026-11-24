"""MOSS Autonomous Background Jobs, Standing Watchers & Self-Authoring Skills.

All SQLite-backed, 0 MiB GPU VRAM, cancellation-safe:
  1. Async Background Jobs (`bg_jobs`):
     - `web_research`: multi-query Bing News+Web RSS research -> synthesizes structured report
       (using cloud LLM when available, or deterministic summary offline) -> auto-ingests into SAG!
     - `folder_digest`: scans local `.md/.txt/.json/.log` files in a target folder -> chunks & indexes
       into SQLite-SAG -> triggers World Model consolidation!
  2. Standing Watchers (`standing_watchers`):
     - Watches a real local `.log` file for keyword/regex matches and triggers a Proactive Pulse alert!
  3. Self-Authoring Reusable Skills (`custom_skills`):
     - Lets MOSS or the operator save recurring multi-step workflows as one-click tactical skills.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from time import time

from model_catalog import before_model_request
from sag_knowledge import _BLOCKED_INGEST_TOKENS, SAGKnowledgeBase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_SKILLS = (
    ("briefing", "一键态势简报", "立即生成包含时间、日程、硬件、RSI 与 SAG 状态的 550W 态势简报", "请给我一份当前时刻的态势简报", "pulse"),
    ("dream_rsi", "深度整理与复盘", "执行夜间做梦式世界模型整合与 RSI 自我迭代复盘", "请帮我做一次深度整理并自我复盘最近的经验规则", "brain"),
    ("sleep_music", "深夜助眠纯音乐", "从本机曲库自动挑选舒缓纯音乐（排鼓排摇滚）并播放", "来一首助眠的纯音乐吧", "media"),
    ("hw_check", "全机硬件体检", "读取 CPU/GPU 负载温度、内存、电量与磁盘并给出诊断", "帮我全面体检一下电脑当前的硬件状态", "system"),
)


class BackgroundJobManager:
    """Async multi-step worker, log watcher, and custom skill store for MOSS."""

    def __init__(self, db: sqlite3.Connection, sag: SAGKnowledgeBase, *, on_log=None, on_alert=None):
        self.db = db
        self.sag = sag
        self.on_log = on_log
        self.on_alert = on_alert
        self._tasks: dict[int, asyncio.Task] = {}
        self._watcher_offsets: dict[int, int] = {}
        self._init_schema()
        self._seed_default_skills()

    def _init_schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS bg_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress INTEGER NOT NULL DEFAULT 0,
                    step_text TEXT NOT NULL DEFAULT '',
                    result_text TEXT NOT NULL DEFAULT '',
                    sag_event_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS standing_watchers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    last_hit_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS custom_skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'custom',
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)

    def _seed_default_skills(self):
        now = _now_iso()
        with self.db:
            for slug, name, desc, prompt, cat in DEFAULT_SKILLS:
                self.db.execute(
                    """INSERT OR IGNORE INTO custom_skills(slug, name, description, prompt, category, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (slug, name, desc, prompt, cat, now),
                )

    def _log(self, text: str, level: str = "info"):
        if self.on_log is not None:
            try:
                self.on_log(text, source="JOB", level=level)
            except Exception:
                pass

    def _update_job(self, job_id: int, **fields):
        fields["updated_at"] = _now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [job_id]
        with self.db:
            self.db.execute(f"UPDATE bg_jobs SET {cols} WHERE id=?", vals)

    def list_jobs(self, limit: int = 16) -> list[dict]:
        rows = self.db.execute(
            """SELECT id, kind, title, status, progress, step_text, result_text, sag_event_ids, created_at, updated_at
               FROM bg_jobs ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        keys = ("id", "kind", "title", "status", "progress", "step_text", "result_text", "sag_event_ids", "created_at", "updated_at")
        out = []
        for r in rows:
            item = dict(zip(keys, r))
            try:
                item["sag_event_ids"] = json.loads(item["sag_event_ids"])
            except json.JSONDecodeError:
                item["sag_event_ids"] = []
            out.append(item)
        return out

    def get_job(self, job_id: int) -> dict | None:
        rows = self.list_jobs(limit=50)
        return next((j for j in rows if j["id"] == job_id), None)

    def submit_job(
        self,
        *,
        kind: str,
        title: str,
        params: dict,
        search_fn=None,
        llm_client=None,
        llm_model: str = "",
    ) -> dict:
        if kind not in {"web_research", "folder_digest"}:
            raise ValueError("后台任务类型仅支持 web_research 或 folder_digest。")
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                """INSERT INTO bg_jobs(kind, title, params_json, status, progress, step_text, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', 5, '任务已排入后台队列', ?, ?)""",
                (kind, title.strip()[:80] or kind, json.dumps(params, ensure_ascii=False), now, now),
            )
            job_id = cur.lastrowid
        self._log(f"Queued J#{job_id} [{kind}] {title[:50]}", level="info")
        task = asyncio.create_task(
            self._run_job(job_id, kind, title, params, search_fn=search_fn, llm_client=llm_client, llm_model=llm_model)
        )
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        return self.get_job(job_id)

    async def _run_job(self, job_id: int, kind: str, title: str, params: dict, *, search_fn=None, llm_client=None, llm_model: str = ""):
        before_model_request.set(None)
        try:
            self._update_job(job_id, status="running", progress=15, step_text="正在启动后台执行器…")
            if kind == "web_research":
                await self._exec_web_research(job_id, title, params, search_fn=search_fn, llm_client=llm_client, llm_model=llm_model)
            elif kind == "folder_digest":
                await self._exec_folder_digest(job_id, title, params)
        except asyncio.CancelledError:
            self._update_job(job_id, status="cancelled", step_text="任务已取消")
            raise
        except Exception as exc:
            self._update_job(job_id, status="error", step_text=f"执行失败：{exc}", result_text=str(exc))
            self._log(f"J#{job_id} failed: {exc}", level="crit")

    async def _exec_web_research(self, job_id: int, title: str, params: dict, *, search_fn=None, llm_client=None, llm_model: str = ""):
        topic = str(params.get("topic") or title).strip()
        queries = [topic, f"{topic} 最新 进展"]
        gathered = []
        for idx, q in enumerate(queries, 1):
            self._update_job(job_id, progress=20 + idx * 20, step_text=f"第 {idx}/{len(queries)} 轮检索：{q[:32]}")
            self._log(f"J#{job_id} searching: {q[:40]}", level="info")
            if search_fn is not None:
                res = await search_fn(q)
                gathered.append(res)
            await asyncio.sleep(0.05)

        raw_bundle = "\n\n".join(gathered)
        self._update_job(job_id, progress=72, step_text="正在综合提炼调研结论…")
        summary = ""
        if llm_client is not None and llm_model:
            try:
                resp = await llm_client.chat.completions.create(
                    model=llm_model,
                    messages=[
                        {"role": "system", "content": "你是 MOSS 后台研究员。请基于提供的实时检索资料，提炼出 3-5 条客观精炼的中文核心结论（不编造资料里没有的数字），用简洁纯文本输出。"},
                        {"role": "user", "content": f"调研主题：{topic}\n\n原始检索资料：\n{raw_bundle[:3200]}"},
                    ],
                    temperature=0.2,
                    max_tokens=450,
                    timeout=30,
                )
                summary = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            except Exception as exc:
                self._log(f"J#{job_id} LLM synth fallback ({type(exc).__name__})", level="warn")
        if not summary:
            lines = [ln.strip() for ln in raw_bundle.splitlines() if ln.strip() and ln.strip()[0].isdigit()]
            summary = f"关于「{topic}」的后台检索摘要：\n" + ("\n".join(lines[:6]) if lines else raw_bundle[:600])

        self._update_job(job_id, progress=90, step_text="正在写入 SQLite-SAG 知识图谱…")
        ev = self.sag.upsert_event(
            content=summary[:3500],
            title=f"后台调研 · {topic[:36]}",
            source="research_job",
            source_ref=f"job:{job_id}",
        )
        self._update_job(
            job_id,
            status="done",
            progress=100,
            step_text="调研完成并已入库 SAG",
            result_text=summary[:2400],
            sag_event_ids=json.dumps([ev["id"]]),
        )
        self._log(f"J#{job_id} DONE -> SAG E#{ev['id']} ({len(ev['entities'])} entities)", level="ok")
        if self.on_alert is not None:
            self.on_alert(
                kind="job_done",
                level="ok",
                title=f"后台调研完成 · {topic[:24]}",
                body=summary[:180],
                dedup_key=f"job:{job_id}",
                cooldown_sec=60,
                bypass_budget=True,
            )

    async def _exec_folder_digest(self, job_id: int, title: str, params: dict):
        raw_path = str(params.get("path") or "").strip()
        target = Path(raw_path).expanduser().resolve()
        if any(tok in target.as_posix().lower() for tok in _BLOCKED_INGEST_TOKENS):
            raise ValueError("出于安全与隐私保护，拒绝扫描敏感目录或凭据文件。")
        if not target.exists():
            raise ValueError(f"路径不存在：{target}")
        files = [target] if target.is_file() else [
            p for p in sorted(target.iterdir())
            if p.is_file()
            and p.suffix.lower() in {".md", ".txt", ".json", ".csv"}
            and not any(tok in p.as_posix().lower() for tok in _BLOCKED_INGEST_TOKENS)
        ][:12]
        if not files:
            raise ValueError("目录下未找到可索引的 .md / .txt / .json / .csv 文件。")
        event_ids = []
        for idx, fpath in enumerate(files, 1):
            pct = 20 + int(70 * idx / len(files))
            self._update_job(job_id, progress=pct, step_text=f"索引文件 ({idx}/{len(files)}): {fpath.name}")
            created = self.sag.ingest_file(fpath, max_chunks=12)
            event_ids.extend(c["id"] for c in created)
            await asyncio.sleep(0)
        summary = f"已完成 {len(files)} 份文档分块入库，共写入 {len(event_ids)} 个 SAG 知识节点（{', '.join(f.name for f in files[:4])}）。"
        self._update_job(
            job_id,
            status="done",
            progress=100,
            step_text="文档目录索引完成",
            result_text=summary,
            sag_event_ids=json.dumps(event_ids),
        )
        self._log(f"J#{job_id} folder digest DONE: {len(files)} files, {len(event_ids)} nodes", level="ok")
        if self.on_alert is not None:
            self.on_alert(
                kind="job_done",
                level="ok",
                title=f"文档知识入库完成 ({len(event_ids)} 节点)",
                body=summary[:180],
                dedup_key=f"job:{job_id}",
                cooldown_sec=60,
                bypass_budget=True,
            )

    # ── Standing Watchers (Log Keyword Sentinels) ──
    def add_watcher(self, *, name: str, file_path: str, pattern: str) -> dict:
        p = Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"找不到要守望的文件：{p}")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"匹配规则无效：{exc}") from exc
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO standing_watchers(name, file_path, pattern, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
                (name.strip()[:60] or p.name, str(p), pattern[:120], now),
            )
            wid = cur.lastrowid
        try:
            self._watcher_offsets[wid] = p.stat().st_size
        except OSError:
            self._watcher_offsets[wid] = 0
        self._log(f"Added watcher W#{wid} ({p.name} ~ /{pattern}/)", level="ok")
        return next(w for w in self.list_watchers() if w["id"] == wid)

    def delete_watcher(self, watcher_id: int) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM standing_watchers WHERE id=?", (watcher_id,))
        self._watcher_offsets.pop(watcher_id, None)
        return cur.rowcount > 0

    def list_watchers(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT id, name, file_path, pattern, enabled, hit_count, last_hit_text, created_at
               FROM standing_watchers ORDER BY id DESC LIMIT 20"""
        ).fetchall()
        keys = ("id", "name", "file_path", "pattern", "enabled", "hit_count", "last_hit_text", "created_at")
        return [{**dict(zip(keys, r)), "enabled": bool(r[4])} for r in rows]

    def poll_watchers(self) -> list[dict]:
        hits = []
        for w in self.list_watchers():
            if not w["enabled"]:
                continue
            p = Path(w["file_path"])
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
                prev = self._watcher_offsets.get(w["id"])
                if prev is None:
                    self._watcher_offsets[w["id"]] = size
                    continue
                if size < prev:
                    prev = 0
                if size == prev:
                    continue
                with p.open("rb") as fh:
                    fh.seek(prev)
                    chunk = fh.read(min(65536, size - prev))
                self._watcher_offsets[w["id"]] = prev + len(chunk)
                rx = re.compile(w["pattern"], re.I)
                for line in chunk.decode("utf-8", errors="replace").splitlines():
                    if rx.search(line):
                        snippet = line.strip()[:160]
                        with self.db:
                            self.db.execute(
                                "UPDATE standing_watchers SET hit_count=hit_count+1, last_hit_text=? WHERE id=?",
                                (snippet, w["id"]),
                            )
                        hits.append({"watcher_id": w["id"], "name": w["name"], "line": snippet})
                        if self.on_alert is not None:
                            self.on_alert(
                                kind="watcher_hit",
                                level="warn",
                                title=f"守望命中 · {w['name']}",
                                body=snippet,
                                dedup_key=f"watcher:{w['id']}:{int(time() // 30)}",
                                cooldown_sec=30,
                                bypass_budget=True,
                            )
                        break
            except (OSError, re.error):
                continue
        return hits

    # ── Self-Authoring Reusable Skills ──
    def list_skills(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT id, slug, name, description, prompt, category, use_count, created_at
               FROM custom_skills ORDER BY CASE category WHEN 'custom' THEN 0 ELSE 1 END, use_count DESC, id ASC"""
        ).fetchall()
        keys = ("id", "slug", "name", "description", "prompt", "category", "use_count", "created_at")
        return [dict(zip(keys, r)) for r in rows]

    def save_skill(self, *, name: str, prompt: str, description: str = "", category: str = "custom") -> dict:
        clean_name = name.strip()[:40]
        clean_prompt = prompt.strip()[:600]
        if not clean_name or not clean_prompt:
            raise ValueError("技能名称与执行指令不能为空。")
        slug = re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", "-", clean_name.casefold()).strip("-")[:32] or f"skill-{int(time())}"
        now = _now_iso()
        with self.db:
            existing = self.db.execute("SELECT id FROM custom_skills WHERE slug=?", (slug,)).fetchone()
            if existing:
                self.db.execute(
                    "UPDATE custom_skills SET name=?, description=?, prompt=?, category=? WHERE id=?",
                    (clean_name, description.strip()[:140], clean_prompt, category[:20], existing[0]),
                )
            else:
                self.db.execute(
                    """INSERT INTO custom_skills(slug, name, description, prompt, category, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (slug, clean_name, description.strip()[:140], clean_prompt, category[:20], now),
                )
        self._log(f"Saved skill [{clean_name}]", level="ok")
        return next(s for s in self.list_skills() if s["slug"] == slug)

    def delete_skill(self, skill_id: int) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM custom_skills WHERE id=?", (skill_id,))
        return cur.rowcount > 0

    def mark_skill_used(self, skill_id: int):
        with self.db:
            self.db.execute("UPDATE custom_skills SET use_count=use_count+1 WHERE id=?", (skill_id,))

    async def close(self):
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

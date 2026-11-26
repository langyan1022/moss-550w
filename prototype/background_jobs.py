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

from cognitive_utils import is_sensitive_fs_path
from model_catalog import before_model_request
from sag_knowledge import SAGKnowledgeBase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_SKILLS = (
    ("briefing", "一键态势简报", "立即生成包含时间、日程、硬件、RSI 与 SAG 状态的 550W 态势简报", "请给我一份当前时刻的态势简报", "pulse", ["get_situational_briefing", "get_time", "query_calendar"]),
    ("dream_rsi", "深度整理与复盘", "执行夜间做梦式世界模型整合与 RSI 自我迭代复盘", "请帮我做一次深度整理并自我复盘最近的经验规则", "brain", ["consolidate_world_model", "review_self_lessons", "search_knowledge"]),
    ("sleep_music", "深夜助眠纯音乐", "从本机曲库自动挑选舒缓纯音乐（排鼓排摇滚）并播放", "来一首助眠的纯音乐吧", "media", ["play_song", "list_media_catalog", "set_music_sleep_timer"]),
    ("hw_check", "全机硬件体检", "读取 CPU/GPU 负载温度、内存、电量与磁盘并给出诊断", "帮我全面体检一下电脑当前的硬件状态", "system", ["get_device_status"]),
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
        self.reconcile_stalled_jobs()
        self._seed_default_skills()

    def reconcile_stalled_jobs(self) -> int:
        """Mark interrupted queued/running jobs from previous crashed runs as error cleanly."""
        now = _now_iso()
        with self.db:
            cur = self.db.execute(
                "UPDATE bg_jobs SET status='error', step_text='上次进程退出，后台任务已中断', updated_at=? WHERE status IN ('queued', 'running')",
                (now,),
            )
        return max(0, int(cur.rowcount or 0))

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
                    created_at TEXT NOT NULL,
                    required_tools_json TEXT NOT NULL DEFAULT '[]',
                    steps_json TEXT NOT NULL DEFAULT '[]'
                );
            """)
            for col_sql in (
                "ALTER TABLE custom_skills ADD COLUMN required_tools_json TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE custom_skills ADD COLUMN steps_json TEXT NOT NULL DEFAULT '[]'",
            ):
                try:
                    self.db.execute(col_sql)
                except sqlite3.OperationalError:
                    pass

    def _seed_default_skills(self):
        now = _now_iso()
        with self.db:
            for slug, name, desc, prompt, cat, req_tools in DEFAULT_SKILLS:
                tools_json = json.dumps(req_tools, ensure_ascii=False)
                self.db.execute(
                    """INSERT OR IGNORE INTO custom_skills(slug, name, description, prompt, category, created_at, required_tools_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (slug, name, desc, prompt, cat, now, tools_json),
                )
                self.db.execute(
                    "UPDATE custom_skills SET required_tools_json=? WHERE slug=? AND (required_tools_json='' OR required_tools_json='[]')",
                    (tools_json, slug),
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

        def _scan_chunks():
            target = Path(raw_path).expanduser().resolve()
            if is_sensitive_fs_path(target, block_token_substr=True):
                raise ValueError("出于安全与隐私保护，拒绝扫描敏感目录或凭据文件。")
            if not target.exists():
                raise ValueError(f"路径不存在：{target}")
            def _safe_child(p: Path) -> Path | None:
                if not p.is_file() or p.is_symlink():
                    return None
                try:
                    rp = p.resolve()
                except OSError:
                    return None
                if rp.suffix.lower() not in {".md", ".txt", ".json", ".csv"}:
                    return None
                if is_sensitive_fs_path(rp, block_token_substr=True):
                    return None
                return rp

            files = [target] if target.is_file() else [
                rp for p in sorted(target.iterdir()) if (rp := _safe_child(p)) is not None
            ][:12]
            if not files:
                raise ValueError("目录下未找到可索引的 .md / .txt / .json / .csv 文件。")
            prepared = []
            for fpath in files:
                with fpath.open("rb") as fh:
                    raw_txt = fh.read(524_288).decode("utf-8", errors="replace")
                paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw_txt) if len(p.strip()) >= 12]
                if not paragraphs and raw_txt.strip():
                    paragraphs = [raw_txt.strip()[:2000]]
                prepared.append((fpath.name, [c[:1500] for c in paragraphs[:12]]))
            return files, prepared

        files, prepared = await asyncio.to_thread(_scan_chunks)
        event_ids = []
        for idx, (fname, chunks) in enumerate(prepared, 1):
            pct = 20 + int(70 * idx / len(prepared))
            self._update_job(job_id, progress=pct, step_text=f"索引文件 ({idx}/{len(prepared)}): {fname}")
            for c_idx, chunk in enumerate(chunks, 1):
                ev = self.sag.upsert_event(content=chunk, title=f"{fname} #{c_idx}", source="file", source_ref=f"{fname}#{c_idx}")
                event_ids.append(ev["id"])
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
        if is_sensitive_fs_path(p, block_token_substr=True):
            raise ValueError("出于安全与隐私保护，拒绝守望敏感凭据或数据库文件。")
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

    def has_enabled_watchers(self) -> bool:
        row = self.db.execute("SELECT 1 FROM standing_watchers WHERE enabled=1 LIMIT 1").fetchone()
        return row is not None

    @staticmethod
    def _scan_watcher_chunks_io(
        enabled_watchers: list[dict],
        offsets: dict[int, int],
    ) -> tuple[dict[int, int], list[tuple[dict, list[str]]]]:
        """Read incremental file chunks & match regexes off the event loop (pure disk/CPU work)."""
        next_offsets = dict(offsets)
        events: list[tuple[dict, list[str]]] = []
        for w in enabled_watchers:
            try:
                p = Path(w["file_path"]).expanduser().resolve()
            except OSError:
                continue
            if is_sensitive_fs_path(p, block_token_substr=True) or not p.is_file():
                continue
            try:
                size = p.stat().st_size
                prev = next_offsets.get(w["id"])
                if prev is None:
                    next_offsets[w["id"]] = size
                    continue
                if size < prev:
                    prev = 0
                if size == prev:
                    continue
                with p.open("rb") as fh:
                    fh.seek(prev)
                    chunk = fh.read(min(65536, size - prev))
                next_offsets[w["id"]] = prev + len(chunk)
                rx = re.compile(w["pattern"], re.I)
                matched_lines = [line.strip()[:160] for line in chunk.decode("utf-8", errors="replace").splitlines() if rx.search(line)]
                if matched_lines:
                    events.append((w, matched_lines))
            except (OSError, re.error):
                continue
        return next_offsets, events

    def _apply_watcher_events(self, next_offsets: dict[int, int], events: list[tuple[dict, list[str]]]) -> list[dict]:
        self._watcher_offsets.update(next_offsets)
        hits = []
        for w, matched_lines in events:
            last_snip = matched_lines[-1]
            count = len(matched_lines)
            with self.db:
                self.db.execute(
                    "UPDATE standing_watchers SET hit_count=hit_count+?, last_hit_text=? WHERE id=?",
                    (count, last_snip, w["id"]),
                )
            for snippet in matched_lines:
                hits.append({"watcher_id": w["id"], "name": w["name"], "line": snippet})
            if self.on_alert is not None:
                prefix = f"[命中 {count} 行] " if count > 1 else ""
                self.on_alert(
                    kind="watcher_hit",
                    level="warn",
                    title=f"守望命中 · {w['name']}",
                    body=f"{prefix}{last_snip}"[:180],
                    dedup_key=f"watcher:{w['id']}:{int(time() // 30)}",
                    cooldown_sec=30,
                    bypass_budget=True,
                )
        return hits

    def poll_watchers(self) -> list[dict]:
        enabled = [w for w in self.list_watchers() if w["enabled"]]
        if not enabled:
            return []
        next_offsets, events = self._scan_watcher_chunks_io(enabled, self._watcher_offsets)
        return self._apply_watcher_events(next_offsets, events)

    async def poll_watchers_async(self) -> list[dict]:
        enabled = [w for w in self.list_watchers() if w["enabled"]]
        if not enabled:
            return []
        next_offsets, events = await asyncio.to_thread(self._scan_watcher_chunks_io, enabled, dict(self._watcher_offsets))
        return self._apply_watcher_events(next_offsets, events)

    # ── Self-Authoring Reusable Skills ──
    @staticmethod
    def _infer_skill_tools(prompt: str, extra: list[str] | tuple[str, ...] | None = None) -> list[str]:
        inferred = [str(x).strip() for x in (extra or ()) if str(x).strip()]
        p = str(prompt or "")
        rules = (
            (r"简报|态势", "get_situational_briefing"),
            (r"做梦|整理|世界模型|冲突", "consolidate_world_model"),
            (r"复盘|反思|经验规则|RSI", "review_self_lessons"),
            (r"新闻|热搜|头条", "get_news_headlines"),
            (r"https?://|抓(?:取)?网页|读网页", "fetch_web_page"),
            (r"搜|联网|网页|http", "web_search"),
            (r"天气|下雨|气温", "get_weather"),
            (r"几点|现在时间|日期|星期几", "get_time"),
            (r"音量|声音大小", "get_volume"),
            (r"计算|算一下|利息|复利|折扣", "calculate_math"),
            (r"几天|推算|倒计时|星期几|周几", "calculate_date"),
            (r"读.*文件|查看.*文件", "read_local_file"),
            (r"目录|文件夹|列出文件", "list_local_directory"),
            (r"日程|会议|排期", "query_calendar"),
            (r"提醒|闹钟", "list_timers"),
            (r"例行|周期任务", "list_recurring_tasks"),
            (r"便签|备忘", "search_notes"),
            (r"个人记忆|记住的偏好|长期记忆", "search_user_memory"),
            (r"知识库|SAG|资料", "search_knowledge"),
            (r"硬件|CPU|GPU|温度|内存", "get_device_status"),
            (r"曲库分类|有什么歌|歌手列表", "list_media_catalog"),
            (r"睡眠定时", "set_music_sleep_timer"),
            (r"音乐|曲库|纯音乐|助眠|播放", "play_song"),
            (r"打开.*灯|关闭.*灯|空调|调控.*设备", "ha_control_device"),
            (r"家居|灯光|设备", "ha_list_devices"),
            (r"组件|看板|对比表|计算器", "render_fui_widget"),
            (r"能做什么|有哪些能力|工具列表", "list_capabilities"),
        )
        for pat, tname in rules:
            if re.search(pat, p, re.I) and tname not in inferred:
                inferred.append(tname)
        return inferred[:12]

    def list_skills(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT id, slug, name, description, prompt, category, use_count, created_at,
                      COALESCE(required_tools_json, '[]'), COALESCE(steps_json, '[]')
               FROM custom_skills ORDER BY CASE category WHEN 'custom' THEN 0 ELSE 1 END, use_count DESC, id ASC"""
        ).fetchall()
        out = []
        for sid, slug, name, desc, prompt, cat, uc, cat_at, rt_raw, st_raw in rows:
            try:
                req_tools = json.loads(rt_raw)
                if not isinstance(req_tools, list):
                    req_tools = []
            except json.JSONDecodeError:
                req_tools = []
            if not req_tools:
                req_tools = self._infer_skill_tools(prompt)
            try:
                steps = json.loads(st_raw)
                if not isinstance(steps, list):
                    steps = []
            except json.JSONDecodeError:
                steps = []
            out.append({
                "id": sid, "slug": slug, "name": name, "description": desc,
                "prompt": prompt, "category": cat, "use_count": uc, "created_at": cat_at,
                "required_tools": req_tools, "steps": steps,
            })
        return out

    def save_skill(
        self,
        *,
        name: str,
        prompt: str,
        description: str = "",
        category: str = "custom",
        required_tools: list[str] | tuple[str, ...] | None = None,
        steps: list[dict] | None = None,
    ) -> dict:
        clean_name = name.strip()[:40]
        clean_prompt = prompt.strip()[:600]
        if not clean_name or not clean_prompt:
            raise ValueError("技能名称与执行指令不能为空。")
        slug = re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", "-", clean_name.casefold()).strip("-")[:32] or f"skill-{int(time())}"
        now = _now_iso()
        req_list = self._infer_skill_tools(clean_prompt, required_tools)
        tools_json = json.dumps(req_list, ensure_ascii=False)
        steps_json = json.dumps(list(steps or ())[:12], ensure_ascii=False)
        with self.db:
            existing = self.db.execute("SELECT id FROM custom_skills WHERE slug=?", (slug,)).fetchone()
            if existing:
                self.db.execute(
                    "UPDATE custom_skills SET name=?, description=?, prompt=?, category=?, required_tools_json=?, steps_json=? WHERE id=?",
                    (clean_name, description.strip()[:140], clean_prompt, category[:20], tools_json, steps_json, existing[0]),
                )
            else:
                self.db.execute(
                    """INSERT INTO custom_skills(slug, name, description, prompt, category, created_at, required_tools_json, steps_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (slug, clean_name, description.strip()[:140], clean_prompt, category[:20], now, tools_json, steps_json),
                )
        self._log(f"Saved skill [{clean_name}] (tools={len(req_list)})", level="ok")
        return next(s for s in self.list_skills() if s["slug"] == slug)

    def compile_skill_from_trace(self, *, name: str, user_prompt: str, tool_trace: list[dict]) -> dict:
        """Synthesize a reusable structured skill macro from an observed successful multi-tool trace."""
        ok_tools = [str(tr.get("name") or "") for tr in (tool_trace or ()) if tr.get("status") == "ok" and tr.get("name")]
        unique_tools = list(dict.fromkeys(ok_tools))
        steps = [{"step": i + 1, "tool": tn} for i, tn in enumerate(unique_tools[:8])]
        return self.save_skill(
            name=name,
            description=f"由成功轨迹沉淀（{len(unique_tools)}步工具链）",
            prompt=f"按既定工具顺序（{' -> '.join(unique_tools) or '默认推理'}）执行：{user_prompt[:380]}",
            category="compiled",
            required_tools=unique_tools,
            steps=steps,
        )

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

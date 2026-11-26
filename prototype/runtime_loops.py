"""Background telemetry log-tail & proactive heartbeat loops for MOSS server."""
from __future__ import annotations

import asyncio
import re
from time import monotonic as _mono, perf_counter


def install_runtime_loops(
    app,
    moss,
    stopped: asyncio.Event,
    *,
    device_status,
    current_weather,
    backend_log,
    runtime,
    subscribers_by_owner: dict[str, set[asyncio.Event]],
    runtime_subscribers: set[asyncio.Event],
    boot_monotonic: float,
    last_auto_dream: list[float],
):
    """Register periodic telemetry log-tail & autonomous heartbeat/routine/idle-dream loops."""
    log_tail_task: asyncio.Task | None = None
    pulse_task: asyncio.Task | None = None

    async def tail_logs_loop():
        slot = 0
        last_pulse = 0.0
        last_hw_refresh = 0.0
        cached_hw = None
        try:
            while not stopped.is_set():
                if backend_log.subscribers:
                    await backend_log.poll_files_async()
                    now_t = perf_counter()
                    if now_t - last_hw_refresh >= 3.6 or cached_hw is None:
                        last_hw_refresh = now_t
                        try:
                            cached_hw = await device_status.read()
                        except Exception:
                            pass
                    if now_t - last_pulse >= 1.1:
                        last_pulse = now_t
                        hw = cached_hw or {}
                        up_s = int(max(0.0, now_t - boot_monotonic))
                        hh, rem = divmod(up_s, 3600)
                        mm, ss = divmod(rem, 60)
                        if slot == 0:
                            cpu_p = hw.get("cpu_percent")
                            cpu_t = (hw.get("cpu") or {}).get("temperature_c")
                            gpu = hw.get("gpu") or {}
                            gpu_p = gpu.get("percent")
                            gpu_t = gpu.get("temperature_c")
                            c_str = f"CPU {cpu_p:.0f}%" if isinstance(cpu_p, (int, float)) else "CPU --"
                            if isinstance(cpu_t, (int, float)):
                                c_str += f"/{cpu_t:.0f}C"
                            g_str = f"GPU {gpu_p:.0f}%" if isinstance(gpu_p, (int, float)) else "GPU --"
                            if isinstance(gpu_t, (int, float)):
                                g_str += f"/{gpu_t:.0f}C"
                            lvl = (
                                "warn"
                                if (isinstance(cpu_t, (int, float)) and cpu_t >= 82)
                                or (isinstance(gpu_t, (int, float)) and gpu_t >= 82)
                                else "info"
                            )
                            backend_log.emit(f"BUS·THERM | {c_str} · {g_str} · LINK OK", source="DEV", level=lvl, write_file=False)
                        elif slot == 1:
                            mem = hw.get("memory") or {}
                            disk = hw.get("disk") or {}
                            bat = hw.get("battery") or {}
                            r_str = (
                                f"RAM {mem['available']/(1024**3):.1f}/{mem['total']/(1024**3):.0f}G"
                                if mem.get("available") is not None and mem.get("total")
                                else "RAM --"
                            )
                            d_str = (
                                f"DISK {disk.get('drive','C:')} {disk['free']/(1024**3):.0f}G FREE"
                                if disk.get("free") is not None
                                else "DISK --"
                            )
                            p_str = "PWR AC" if bat.get("ac") or bat.get("present") is False else f"BAT {bat.get('percent', 0):.0f}%"
                            backend_log.emit(f"BUS·MEMIO | {r_str} · {d_str} · {p_str}", source="DEV", level="info", write_file=False)
                        elif slot == 2:
                            phase = "THINKING" if moss._has_active_turn() else "STANDBY"
                            try:
                                row_c = moss.db.execute("SELECT COUNT(*) FROM turns WHERE chat_id=?", (moss.chat_id,)).fetchone()
                                turns_n = int(row_c[0]) if row_c else 0
                            except Exception:
                                turns_n = 0
                            backend_log.emit(
                                f"CORE·550W | {moss.model_name} [{phase}] · T={turns_n:02d} · SSE={len(backend_log.subscribers)} · UP {hh:02d}:{mm:02d}:{ss:02d}",
                                source="CORE",
                                level="ok" if phase == "THINKING" else "info",
                                write_file=False,
                            )
                        else:
                            try:
                                sag_s = moss.sag.status_snapshot()
                                rsi_s = moss.rsi.status_snapshot()
                                ha_live = bool(
                                    moss.iot.config.get("ha_mode") == "live"
                                    and moss.iot.config.get("ha_enabled")
                                    and (moss.iot.config.get("ha_url") or moss.iot.config.get("ha_base_url"))
                                    and moss.iot.config.get("ha_token")
                                )
                                ha_mode = "LIVE" if ha_live else "DRY"
                                hw_seq = int(getattr(moss.iot, "hw_seq", 0))
                                backend_log.emit(
                                    f"MATX·BRAIN | SAG {sag_s['event_count']}N/{sag_s['entity_count']}E · RSI {rsi_s['active_count']}R · HA:{ha_mode} · HW#{hw_seq:03d}",
                                    source="BRAIN",
                                    level="info",
                                    write_file=False,
                                )
                            except Exception:
                                pass
                        slot = (slot + 1) % 4
                if getattr(moss.jobs, "has_enabled_watchers", None) is None or moss.jobs.has_enabled_watchers():
                    try:
                        await moss.jobs.poll_watchers_async()
                    except Exception as exc:
                        backend_log.emit(f"Watcher poll error: {exc}", source="PULSE", level="warn", write_file=False)
                await asyncio.sleep(0.45)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            backend_log.emit(f"tail_logs_loop exited unexpectedly: {exc}", source="CORE", level="crit", write_file=False)

    async def proactive_heartbeat_loop():
        headless_tick = 0
        last_err_disk: dict[str, float] = {}

        def _emit_pulse_err(key: str, text: str, level: str = "warn"):
            now_m = _mono()
            disk = level == "crit" or (now_m - last_err_disk.get(key, 0.0) >= 300.0)
            if disk:
                last_err_disk[key] = now_m
            backend_log.emit(text, source="PULSE", level=level, write_file=disk)

        try:
            while not stopped.is_set():
                await asyncio.sleep(30.0)
                if stopped.is_set():
                    break
                try:
                    busy = moss.any_active_turn()
                    if not busy:
                        headless_tick = (headless_tick + 1) % 2
                        has_subs = any(subscribers_by_owner.values()) or bool(backend_log.subscribers)
                        should_probe_hw = has_subs or headless_tick == 0
                        try:
                            dev_sample = await device_status.read() if should_probe_hw else None
                        except Exception:
                            dev_sample = None
                        wx_sample = None
                        try:
                            now_m = _mono()
                            valid_wx = [entry[1] for entry in getattr(current_weather, "samples", {}).values() if entry[0] > now_m]
                            if valid_wx:
                                wx_sample = valid_wx[-1]
                        except Exception:
                            wx_sample = None
                        if moss.pulse.config.get("enabled"):
                            try:
                                moss.pulse.evaluate_tick(
                                    device_sample=dev_sample,
                                    is_busy=busy,
                                    weather_sample=wx_sample,
                                    hw_sensors=moss.iot.hw_sensors,
                                    on_hw_frame=moss.iot.push_hardware_frame,
                                )
                            except Exception as exc:
                                _emit_pulse_err("tick", f"Pulse tick error: {exc}", level="warn")
                        for rtask in moss.routines.pop_due_tasks():
                            try:
                                kind = rtask.get("action_kind")
                                task_owner = rtask.get("owner_code") or moss.DEFAULT_OWNER
                                sched = rtask.get("schedule") or {}
                                every_min = int(sched.get("every_min") or 0) if sched.get("kind") == "interval" else 0
                                r_cooldown = min(540.0, max(30.0, every_min * 24.0)) if every_min > 0 else 540.0
                                if kind == "briefing":
                                    wm_snap = moss.consolidator.latest_snapshot(owner_code=task_owner)
                                    brief = moss.pulse.generate_briefing(
                                        device_sample=dev_sample,
                                        timers_count=len(moss.timers.snapshot()) if task_owner == moss.DEFAULT_OWNER else 0,
                                        notes_count=len((moss.notes.page("", archived=False, before=0, limit=50)).get("items") or []) if task_owner == moss.DEFAULT_OWNER else 0,
                                        rsi_active=moss.rsi.status_snapshot(owner_code=task_owner)["active_count"],
                                        sag_events=moss.sag.status_snapshot(owner_code=task_owner)["event_count"],
                                        world_model=(wm_snap or {}).get("world_model"),
                                        weather_sample=wx_sample,
                                        owner_code=task_owner,
                                    )
                                    moss.pulse._emit_alert(
                                        kind="routine_briefing",
                                        level="info",
                                        title=f"例行早报 · {rtask['name']}",
                                        body=brief["text"],
                                        dedup_key=f"routine:{rtask['id']}",
                                        cooldown_sec=r_cooldown,
                                        bypass_budget=True,
                                        respect_quiet=True,
                                        owner_code=task_owner,
                                    )
                                elif kind == "dream":
                                    res = await moss.consolidator.run_consolidation_async(trigger="routine", owner_code=task_owner)
                                    moss.pulse._emit_alert(
                                        kind="routine_dream",
                                        level="info",
                                        title=f"定期深度整理 · {rtask['name']}",
                                        body=res["summary"],
                                        dedup_key=f"routine:{rtask['id']}",
                                        cooldown_sec=r_cooldown,
                                        bypass_budget=True,
                                        respect_quiet=True,
                                        owner_code=task_owner,
                                    )
                                elif kind in {"prompt", "skill"}:
                                    run_text = rtask.get("payload") or rtask["name"]
                                    title_prefix = f"AI 定时推演 · {rtask['name']}"
                                    skill_tools = None
                                    if kind == "skill":
                                        needle = str(run_text or "").strip().lower()
                                        skills = moss.jobs.list_skills()
                                        matched = next(
                                            (s for s in skills if str(s["id"]) == needle or needle in s["name"].lower() or s["name"].lower() in needle),
                                            None,
                                        )
                                        if not matched:
                                            raise ValueError(f"未找到例行技能「{run_text}」")
                                        moss.jobs.mark_skill_used(matched["id"])
                                        run_text = matched["prompt"]
                                        title_prefix = f"定时技能 · {matched['name']}"
                                        skill_tools = matched.get("required_tools")
                                    prompt_res = await moss.ask_scheduled_prompt(
                                        run_text,
                                        owner_code=task_owner,
                                        extra_allowed_tools=skill_tools,
                                    )
                                    reply_txt = str((prompt_res or {}).get("reply") or "").strip()
                                    if re.fullmatch(r"NO_ALERT[。.!！\s]*", reply_txt, re.I):
                                        backend_log.emit(f"Routine {rtask['id']} silent (NO_ALERT)", source="PULSE", level="info", write_file=False)
                                    else:
                                        moss.pulse._emit_alert(
                                            kind="routine_skill" if kind == "skill" else "routine_prompt",
                                            level="info",
                                            title=title_prefix,
                                            body=reply_txt or "（本次定时推演无额外结论）",
                                            dedup_key=f"routine:{rtask['id']}",
                                            cooldown_sec=r_cooldown,
                                            bypass_budget=True,
                                            respect_quiet=True,
                                            owner_code=task_owner,
                                        )
                                else:
                                    moss.pulse._emit_alert(
                                        kind="routine",
                                        level="info",
                                        title=f"例行任务 · {rtask['name']}",
                                        body=rtask.get("payload") or rtask["name"],
                                        dedup_key=f"routine:{rtask['id']}",
                                        cooldown_sec=r_cooldown,
                                        bypass_budget=True,
                                        respect_quiet=True,
                                        owner_code=task_owner,
                                    )
                            except Exception as exc:
                                _emit_pulse_err(f"routine:{rtask.get('id')}", f"Routine dispatch error ({rtask.get('id')}): {exc}", level="warn")
                        if not moss.pulse.config.get("enabled"):
                            continue
                        idle_sec = perf_counter() - getattr(moss, "_last_activity", perf_counter())
                        now_t = perf_counter()
                        if idle_sec >= 900 and (last_auto_dream[0] == 0.0 or now_t - last_auto_dream[0] >= 3600):
                            dream_ran = False
                            for active_owner in moss.known_owners():
                                if moss.any_active_turn():
                                    break
                                try:
                                    await moss.consolidator.run_consolidation_async(trigger="idle_sleep", owner_code=active_owner)
                                    await moss.run_rsi_cycle(trigger="idle_sleep", owner_code=active_owner)
                                    dream_ran = True
                                except Exception as exc:
                                    _emit_pulse_err(f"dream:{active_owner}", f"Idle sleep/RSI error ({active_owner}): {exc}", level="warn")
                            if dream_ran:
                                last_auto_dream[0] = now_t
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _emit_pulse_err("loop", f"proactive_heartbeat_loop tick error: {exc}", level="warn")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _emit_pulse_err("exit", f"proactive_heartbeat_loop exited unexpectedly: {exc}", level="crit")

    async def start_log_tail(_app):
        nonlocal log_tail_task, pulse_task

        def _watch_task(name):
            def _done(t):
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    backend_log.emit(f"Background loop {name} crashed: {exc}", source="CORE", level="crit", write_file=False)
            return _done

        log_tail_task = asyncio.create_task(tail_logs_loop())
        pulse_task = asyncio.create_task(proactive_heartbeat_loop())
        log_tail_task.add_done_callback(_watch_task("tail_logs_loop"))
        pulse_task.add_done_callback(_watch_task("proactive_heartbeat_loop"))

    async def stop_log_tail(_app):
        stopped.set()
        for group in list(subscribers_by_owner.values()):
            for sig in tuple(group):
                sig.set()
        for sig_group in (runtime_subscribers, backend_log.snapshot_subscribers(), moss.iot.hw_subscribers, moss.pulse.subscribers):
            for sig in tuple(sig_group):
                sig.set()
        for tsk in (log_tail_task, pulse_task):
            if tsk is not None:
                tsk.cancel()
                try:
                    await tsk
                except asyncio.CancelledError:
                    pass
        try:
            await moss.online_music.close()
        except Exception:
            pass

    app.on_startup.append(start_log_tail)
    app.on_shutdown.append(stop_log_tail)

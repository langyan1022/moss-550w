import asyncio
import json
from pathlib import Path

import aiohttp
from aiohttp import web


def install_brain_iot_routes(
    app,
    moss,
    stopped,
    *,
    device_status,
    prepare_window_image,
    recognize_capture,
    image_max_json,
    window_image_provider,
    read_json_dict,
    lan_host_list,
    port,
    tls_port,
):
    """Register Brain/Soul/RSI/SAG/Proactive/Jobs/Skills/Routines and IoT/Hardware routes."""

    async def rsi_status(request):
        return web.json_response(moss.rsi.status_snapshot(), headers={"Cache-Control": "no-store"})

    async def rsi_iterate(request):
        cycle = await moss.run_rsi_cycle(trigger="api")
        return web.json_response({"cycle": cycle, "status": moss.rsi.status_snapshot()},
                                 headers={"Cache-Control": "no-store"})

    async def rsi_retire(request):
        lesson_id = int(request.match_info["lesson_id"])
        ok = moss.rsi.retire_lesson(lesson_id, reason="api_delete")
        if not ok:
            return web.json_response({"error": "未找到生效中的经验规则"}, status=404)
        return web.json_response({"retired": lesson_id, "status": moss.rsi.status_snapshot()},
                                 headers={"Cache-Control": "no-store"})

    async def sag_status(request):
        return web.json_response(moss.sag.status_snapshot(), headers={"Cache-Control": "no-store"})

    async def sag_ingest(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要 JSON 对象")
            if body.get("file_path"):
                created = moss.sag.ingest_file(Path(str(body["file_path"])))
                return web.json_response({"ingested": len(created), "items": created, "status": moss.sag.status_snapshot()},
                                         headers={"Cache-Control": "no-store"})
            ev = moss.sag.upsert_event(
                content=str(body.get("content") or ""),
                title=str(body.get("title") or ""),
                source=str(body.get("source") or "doc"),
                source_ref=str(body.get("source_ref") or ""),
            )
            return web.json_response({"event": ev, "status": moss.sag.status_snapshot()},
                                     headers={"Cache-Control": "no-store"})
        except (ValueError, UnicodeDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def sag_query(request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("query"), str):
                raise ValueError("需要 query 字符串")
            res = moss.sag.search(body["query"], limit=int(body.get("limit", 6)), hops=int(body.get("hops", 1)))
            return web.json_response(res, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    def _brain_overview_payload():
        return {
            "rsi": moss.rsi.status_snapshot(),
            "sag": moss.sag.status_snapshot(),
            "soul": moss.soul.snapshot()["stats"],
            "pulse": moss.pulse.status_snapshot(),
            "world_model": moss.consolidator.latest_snapshot(),
            "jobs": moss.jobs.list_jobs(),
            "watchers": moss.jobs.list_watchers(),
            "skills": moss.jobs.list_skills(),
            "routines": moss.routines.list_tasks(),
            "music_prefs": moss.music_prefs.snapshot(),
            "iot": moss.iot.status_snapshot(),
            "mobile_url": (f"https://{lan_host_list[0]}:{tls_port}/mobile" if (lan_host_list and tls_port)
                           else f"http://{lan_host_list[0]}:{port}/mobile" if lan_host_list
                           else f"http://127.0.0.1:{port}/mobile"),
            "lan_urls": ([f"https://{h}:{tls_port}/" for h in lan_host_list] if tls_port else [])
                        + [f"http://{h}:{port}/" for h in lan_host_list],
        }

    async def iot_config(request):
        if request.method == "GET":
            return web.json_response(moss.iot.status_snapshot(), headers={"Cache-Control": "no-store"})
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要配置对象")
            cfg = moss.iot.update_config(body)
            return web.json_response({"config": cfg, "iot": moss.iot.status_snapshot()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def ha_entities(request):
        dom = request.query.get("domain", "")
        return web.json_response(await moss.iot.ha_list_entities(dom), headers={"Cache-Control": "no-store"})

    async def ha_service(request):
        try:
            body = await read_json_dict(request)
            res = await moss.iot.ha_call_service(
                str(body.get("entity_id") or ""),
                str(body.get("service") or ""),
                body.get("data") if isinstance(body.get("data"), dict) else None,
            )
            return web.json_response(res, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def hardware_moss_command(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要控制帧参数")
            frame = moss.iot.push_hardware_frame(
                mode=body.get("mode"),
                yaw_deg=body.get("yaw_deg"),
                pitch_deg=body.get("pitch_deg"),
                gesture=body.get("gesture"),
                screen_headline=body.get("screen_headline"),
                screen_line1=body.get("screen_line1"),
                screen_line2=body.get("screen_line2"),
                screen_level=body.get("screen_level"),
            )
            return web.json_response({"frame": frame}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def hardware_moss_telemetry(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要遥测 JSON 对象")
            sensors = moss.iot.ingest_hardware_telemetry(body)
            return web.json_response({"sensors": sensors}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def hardware_moss_stream(request):
        stream = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        await stream.prepare(request)
        sig = asyncio.Event()
        moss.iot.hw_subscribers.add(sig)
        sent_seq = -1
        try:
            while not stopped.is_set():
                sig.clear()
                if moss.iot.hw_seq != sent_seq:
                    sent_seq = moss.iot.hw_seq
                    payload = json.dumps(moss.iot.hw_state, ensure_ascii=False)
                    await stream.write(f"id: {sent_seq}\nevent: hw-frame\ndata: {payload}\n\n".encode("utf-8"))
                try:
                    await asyncio.wait_for(sig.wait(), 15)
                except TimeoutError:
                    await stream.write(b": keep-alive\n\n")
        except (ConnectionError, aiohttp.ClientConnectionError):
            pass
        finally:
            moss.iot.hw_subscribers.discard(sig)
        return stream

    async def jobs_handler(request):
        from assistant_brain import run_web_search
        if request.method == "GET":
            return web.json_response({"jobs": moss.jobs.list_jobs()}, headers={"Cache-Control": "no-store"})
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要任务参数对象")
            kind = str(body.get("kind") or "web_research")
            title = str(body.get("title") or body.get("topic") or body.get("path") or kind).strip()
            params = {"topic": str(body.get("topic") or title)} if kind == "web_research" else {"path": str(body.get("path") or "")}
            use_cloud = moss.model_id != "local" and moss.model_available(moss.model_id)
            job = moss.jobs.submit_job(
                kind=kind,
                title=title,
                params=params,
                search_fn=run_web_search if kind == "web_research" else None,
                llm_client=moss.model_client() if use_cloud else None,
                llm_model=moss.model_name if use_cloud else "",
            )
            return web.json_response({"job": job, "jobs": moss.jobs.list_jobs()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def watchers_handler(request):
        if request.method == "GET":
            return web.json_response({"watchers": moss.jobs.list_watchers()}, headers={"Cache-Control": "no-store"})
        if request.method == "DELETE":
            wid = int(request.match_info["watcher_id"])
            ok = moss.jobs.delete_watcher(wid)
            return web.json_response({"deleted": ok, "watchers": moss.jobs.list_watchers()}, headers={"Cache-Control": "no-store"})
        try:
            body = await read_json_dict(request)
            w = moss.jobs.add_watcher(
                name=str(body.get("name") or ""),
                file_path=str(body.get("file_path") or ""),
                pattern=str(body.get("pattern") or ""),
            )
            return web.json_response({"watcher": w, "watchers": moss.jobs.list_watchers()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def skills_handler(request):
        if request.method == "GET":
            return web.json_response({"skills": moss.jobs.list_skills()}, headers={"Cache-Control": "no-store"})
        if request.method == "DELETE":
            sid = int(request.match_info["skill_id"])
            ok = moss.jobs.delete_skill(sid)
            return web.json_response({"deleted": ok, "skills": moss.jobs.list_skills()}, headers={"Cache-Control": "no-store"})
        try:
            body = await read_json_dict(request)
            if body.get("mark_used"):
                moss.jobs.mark_skill_used(int(body["mark_used"]))
                return web.json_response({"skills": moss.jobs.list_skills()}, headers={"Cache-Control": "no-store"})
            sk = moss.jobs.save_skill(
                name=str(body.get("name") or ""),
                description=str(body.get("description") or ""),
                prompt=str(body.get("prompt") or ""),
                category=str(body.get("category") or "custom"),
            )
            return web.json_response({"skill": sk, "skills": moss.jobs.list_skills()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def routines_handler(request):
        if request.method == "GET":
            return web.json_response({"routines": moss.routines.list_tasks()}, headers={"Cache-Control": "no-store"})
        if request.method == "DELETE":
            rid = request.match_info.get("routine_id", "")
            removed = moss.routines.delete_task(rid)
            if not removed:
                raise web.HTTPNotFound()
            return web.json_response({"deleted": removed, "routines": moss.routines.list_tasks()}, headers={"Cache-Control": "no-store"})
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要 JSON 对象")
            if request.path.endswith("/toggle"):
                rid = request.match_info.get("routine_id", "")
                updated = moss.routines.toggle_task(rid, bool(body.get("enabled", True)))
                if not updated:
                    raise web.HTTPNotFound()
                return web.json_response({"routine": updated, "routines": moss.routines.list_tasks()}, headers={"Cache-Control": "no-store"})
            created = moss.routines.create_task(
                name=str(body.get("name") or ""),
                schedule=str(body.get("schedule") or ""),
                action_kind=str(body.get("action_kind") or "reminder"),
                payload=str(body.get("payload") or ""),
            )
            return web.json_response({"routine": created, "routines": moss.routines.list_tasks()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def brain_overview(request):
        return web.json_response(_brain_overview_payload(), headers={"Cache-Control": "no-store"})

    async def brain_consolidate(request):
        result = moss.consolidator.run_consolidation(trigger="api")
        return web.json_response({"consolidation": result, "overview": _brain_overview_payload()},
                                 headers={"Cache-Control": "no-store"})

    async def soul_snapshot(request):
        return web.json_response(moss.soul.snapshot(), headers={"Cache-Control": "no-store"})

    async def soul_profile_upsert(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要画像对象")
            trait = moss.soul.upsert_profile(
                key=str(body.get("key") or ""),
                value=str(body.get("value") or ""),
                category=str(body.get("category") or "preference"),
                confidence=float(body.get("confidence", 0.92)),
                salience=float(body.get("salience", 1.1)),
                source="user_edit",
            )
            return web.json_response({"trait": trait, "soul": moss.soul.snapshot()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def soul_profile_delete(request):
        key = request.match_info.get("key", "")
        removed = moss.soul.delete_profile(key)
        if not removed:
            return web.json_response({"error": "未找到该用户画像字段。", "removed": False}, status=404)
        return web.json_response({"removed": True, "soul": moss.soul.snapshot()}, headers={"Cache-Control": "no-store"})

    async def soul_belief_upsert(request):
        try:
            body = await read_json_dict(request)
            if "id" in body and body.get("status"):
                raw_id = body.get("id")
                if isinstance(raw_id, bool):
                    raise ValueError("信念编号无效。")
                belief = moss.soul.set_belief_status(int(raw_id), str(body["status"]))
            else:
                belief = moss.soul.upsert_belief(
                    key=str(body.get("key") or ""),
                    scope=str(body.get("scope") or "habit"),
                    statement=str(body.get("statement") or ""),
                    confidence=float(body.get("confidence", 0.88)),
                )
            return web.json_response({"belief": belief, "soul": moss.soul.snapshot()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def soul_belief_delete(request):
        try:
            bid = int(request.match_info["belief_id"])
            removed = moss.soul.delete_belief(bid)
            if not removed:
                return web.json_response({"error": "未找到该条信念规则。", "removed": False}, status=404)
            return web.json_response({"removed": True, "soul": moss.soul.snapshot()}, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def soul_distill_now(request):
        result = moss.consolidator.run_consolidation(trigger="soul_ui")
        return web.json_response(
            {"consolidation": result, "soul": moss.soul.snapshot(), "overview": _brain_overview_payload()},
            headers={"Cache-Control": "no-store"},
        )

    async def soul_export_md(request):
        bundle = moss.soul.export_markdown_bundle()
        combined = "\n\n---\n\n".join(bundle[name] for name in ("SOUL.md", "BELIEFS.md", "USER.md", "MEMORY.md"))
        return web.json_response({"files": bundle, "combined_markdown": combined}, headers={"Cache-Control": "no-store"})

    async def proactive_config(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("需要配置对象")
            cfg = moss.pulse.update_config(body)
            return web.json_response({"config": cfg, "pulse": moss.pulse.status_snapshot()},
                                     headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def proactive_briefing(request):
        sample = await device_status.read()
        wm_snap = moss.consolidator.latest_snapshot()
        brief = moss.pulse.generate_briefing(
            device_sample=sample,
            timers_count=len(moss.timers.snapshot()),
            notes_count=len((moss.notes.page("", archived=False, before=0, limit=50)).get("items") or []),
            rsi_active=moss.rsi.status_snapshot()["active_count"],
            sag_events=moss.sag.status_snapshot()["event_count"],
            world_model=(wm_snap or {}).get("world_model"),
        )
        return web.json_response(brief, headers={"Cache-Control": "no-store"})

    async def proactive_dismiss(request):
        aid = int(request.match_info["alert_id"])
        ok = moss.pulse.dismiss_alert(aid)
        return web.json_response({"dismissed": ok, "pulse": moss.pulse.status_snapshot()},
                                 headers={"Cache-Control": "no-store"})

    async def vision_to_sag(request):
        from window_image import answer_image
        try:
            body = await request.clone(client_max_size=image_max_json).json()
            if not isinstance(body, dict) or not isinstance(body.get("image"), str):
                raise ValueError("请提供 base64 PNG 图像数据")
            png_bytes, _size = await asyncio.to_thread(prepare_window_image, body["image"])
            custom_title = str(body.get("title") or "").strip()[:60]
            prompt = str(body.get("prompt") or (
                "请简洁提取画面中的核心事实信息（如单号、人名、项目名、日期、书名、待办、关键数字或白板要点），"
                "输出 2 到 4 句结构化中文摘要，便于知识库检索。"
            ))[:400]
            method = "ocr"
            summary = ""
            if moss.model_available(window_image_provider):
                try:
                    client = moss.model_client(window_image_provider)
                    model_name = moss.cloud[window_image_provider].model
                    summary = await answer_image(client, model_name, png_bytes, prompt, None)
                    method = f"vision:{model_name}"
                except Exception:
                    summary = ""
            if not summary.strip():
                summary = (await recognize_capture(png_bytes)).strip()
                method = "local_ocr"
            if not summary.strip():
                return web.json_response({"error": "未能从画面中提取出有效文字或事实信息。"}, status=422)
            ev = moss.sag.upsert_event(
                content=summary,
                title=custom_title or f"镜头识图 · {summary[:28]}",
                source="vision",
            )
            return web.json_response(
                {"method": method, "summary": summary, "event": ev, "sag": moss.sag.status_snapshot()},
                headers={"Cache-Control": "no-store"},
            )
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "图像超出大小限制"}, status=413)
        except (OSError, ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc) or "图像格式无效或无法读取。"}, status=400)

    app.add_routes([
        web.get("/api/rsi", rsi_status),
        web.post("/api/rsi/iterate", rsi_iterate),
        web.delete(r"/api/rsi/lessons/{lesson_id:[0-9]{1,9}}", rsi_retire),
        web.get("/api/sag", sag_status),
        web.post("/api/sag/ingest", sag_ingest),
        web.post("/api/sag/query", sag_query),
        web.get("/api/brain/overview", brain_overview),
        web.post("/api/brain/consolidate", brain_consolidate),
        web.get("/api/soul", soul_snapshot),
        web.post("/api/soul/profile", soul_profile_upsert),
        web.delete(r"/api/soul/profile/{key:.+}", soul_profile_delete),
        web.post("/api/soul/beliefs", soul_belief_upsert),
        web.delete(r"/api/soul/beliefs/{belief_id:[0-9]{1,9}}", soul_belief_delete),
        web.post("/api/soul/distill", soul_distill_now),
        web.get("/api/soul/export", soul_export_md),
        web.post("/api/proactive/config", proactive_config),
        web.post("/api/proactive/briefing", proactive_briefing),
        web.post(r"/api/proactive/alerts/{alert_id:[0-9]{1,9}}/dismiss", proactive_dismiss),
        web.post("/api/context/vision-to-sag", vision_to_sag),
        web.get("/api/jobs", jobs_handler),
        web.post("/api/jobs", jobs_handler),
        web.get("/api/watchers", watchers_handler),
        web.post("/api/watchers", watchers_handler),
        web.delete(r"/api/watchers/{watcher_id:[0-9]{1,9}}", watchers_handler),
        web.get("/api/skills", skills_handler),
        web.post("/api/skills", skills_handler),
        web.delete(r"/api/skills/{skill_id:[0-9]{1,9}}", skills_handler),
        web.get("/api/routines", routines_handler),
        web.post("/api/routines", routines_handler),
        web.delete(r"/api/routines/{routine_id:[A-Za-z0-9_-]{1,32}}", routines_handler),
        web.post(r"/api/routines/{routine_id:[A-Za-z0-9_-]{1,32}}/toggle", routines_handler),
        web.get("/api/iot/config", iot_config),
        web.post("/api/iot/config", iot_config),
        web.get("/api/ha/entities", ha_entities),
        web.post("/api/ha/service", ha_service),
        web.get("/api/hardware/moss/stream", hardware_moss_stream),
        web.post("/api/hardware/moss/command", hardware_moss_command),
        web.post("/api/hardware/moss/telemetry", hardware_moss_telemetry),
    ])

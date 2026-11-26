"""Chat search, Markdown export, and multimodal composer attachment HTTP routes."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
import re
import threading
from urllib.parse import quote

from aiohttp import web
from PIL import Image

from cognitive_utils import delete_sag_events
from doc_extract import IMAGE_EXTS, extract_document_bytes

_IMG_PIXEL_LOCK = threading.Lock()


def _scan_chat_rows(rows, q_text: str, needle: str) -> list[dict]:
    hits = []
    matched_chats: set[str] = set()
    title_fallback: dict[str, dict] = {}
    for turn_id, cid, title, raw_doc in rows:
        try:
            doc = json.loads(raw_doc)
        except ValueError:
            continue
        user_txt = str(doc.get("user") or "")
        reply_txt = str(doc.get("reply") or "")
        title_txt = str(title or "")
        matched_role = ""
        snippet = ""
        for role_name, field in (("user", user_txt), ("assistant", reply_txt)):
            pos = field.casefold().find(needle)
            if pos != -1:
                matched_role = role_name
                lo = max(0, pos - 24)
                hi = min(len(field), pos + len(q_text) + 56)
                prefix = "…" if lo > 0 else ""
                suffix = "…" if hi < len(field) else ""
                snippet = prefix + field[lo:hi].replace("\n", " ") + suffix
                break
        if matched_role:
            matched_chats.add(cid)
            hits.append({
                "chat_id": cid,
                "chat_title": title_txt or cid,
                "turn_id": turn_id,
                "created_at": doc.get("created_at") or "",
                "role": matched_role,
                "snippet": snippet,
                "user": user_txt[:120],
            })
            if len(hits) >= 40:
                break
        elif cid not in title_fallback and needle in title_txt.casefold():
            pos = title_txt.casefold().find(needle)
            lo = max(0, pos - 24)
            hi = min(len(title_txt), pos + len(q_text) + 56)
            prefix = "…" if lo > 0 else ""
            suffix = "…" if hi < len(title_txt) else ""
            title_fallback[cid] = {
                "chat_id": cid,
                "chat_title": title_txt or cid,
                "turn_id": turn_id,
                "created_at": doc.get("created_at") or "",
                "role": "title",
                "snippet": prefix + title_txt[lo:hi].replace("\n", " ") + suffix,
                "user": user_txt[:120],
            }
    for cid, entry in title_fallback.items():
        if len(hits) >= 40:
            break
        if cid not in matched_chats:
            hits.append(entry)
    return hits


def install_chat_attachment_routes(
    app,
    moss,
    *,
    req_owner,
    state_response,
    prepare_window_image,
    recognize_capture,
    window_image_provider: str,
):
    """Register /api/chats/search, /api/chats/{chat_id}/export, and /api/chat/attach."""

    async def chats_search(request):
        owner = req_owner(request)
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response({"query": "", "hits": []}, headers={"Cache-Control": "no-store"})
        needle = q.casefold()
        rows = moss.db.execute(
            "SELECT t.id, t.chat_id, c.title, t.doc FROM turns t "
            "JOIN chats c ON c.id = t.chat_id WHERE COALESCE(c.owner_code,'MOSS')=? "
            "ORDER BY t.rowid DESC LIMIT 600",
            (owner,),
        ).fetchall()
        hits = await asyncio.to_thread(_scan_chat_rows, rows, q, needle)
        return web.json_response({"query": q, "hits": hits}, headers={"Cache-Control": "no-store"})

    async def chat_export(request):
        owner = req_owner(request)
        cid = request.match_info["chat_id"]
        chat_row = moss.db.execute("SELECT id, title, created_at, owner_code FROM chats WHERE id=?", (cid,)).fetchone()
        if not chat_row:
            raise web.HTTPNotFound()
        _, title, created_at, chat_owner = chat_row
        if moss.normalize_owner(chat_owner) != owner:
            return web.json_response({"error": "无权导出其他授权人员的对话记录。"}, status=403)
        op_label = "OP-01" if owner == moss.DEFAULT_OWNER else f"OP-{owner}"
        turn_rows = moss.db.execute("SELECT doc FROM turns WHERE chat_id=? ORDER BY rowid ASC", (cid,)).fetchall()
        lines = [
            f"# MOSS 550W 对话记录 · {title or cid}",
            "",
            f"- 会话编号：`{cid}`",
            f"- 所属人员：`{owner}`",
            f"- 创建时间：`{created_at}`",
            f"- 轮次数量：`{len(turn_rows)}`",
            "",
            "---",
            "",
        ]
        for idx, (raw_doc,) in enumerate(turn_rows, 1):
            try:
                doc = json.loads(raw_doc)
            except ValueError:
                continue
            ts = str(doc.get("created_at") or "")[:19].replace("T", " ")
            lines.append(f"## #{idx:02d} · {ts}")
            lines.append("")
            lines.append(f"**{op_label}**：{doc.get('user') or ''}")
            lines.append("")
            body = doc.get("reply") or doc.get("error") or f"({doc.get('status') or 'pending'})"
            lines.append(f"**MOSS**：{body}")
            lines.append("")
        md = "\n".join(lines).encode("utf-8")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", cid)[:24] or "chat"
        disp = f'attachment; filename="moss-chat-{safe_id}.md"; filename*=UTF-8\'\'{quote(f"moss-chat-{safe_id}.md")}'
        return web.Response(
            body=md,
            content_type="text/markdown",
            charset="utf-8",
            headers={"Cache-Control": "no-store", "Content-Disposition": disp, "X-Content-Type-Options": "nosniff"},
        )

    async def chat_attach(request):
        """Accept pasted screenshots (Ctrl+V) or dragged documents directly from composer."""
        owner = req_owner(request)
        try:
            body = await request.clone(client_max_size=16 * 1024 * 1024).json()
            if not isinstance(body, dict):
                raise ValueError("需要 JSON 对象")
            name = Path(str(body.get("name") or "attachment.bin")).name
            mime = str(body.get("mime") or "").lower()
            prompt = str(body.get("prompt") or "").strip()
            chat_id = str(body.get("chat_id") or moss.active_chat_for(owner))
            moss.require_chat_owner(chat_id, owner)
            if chat_id != moss.active_chat_for(owner):
                raise ValueError("当前对话已切换，请刷新后在当前对话重新发送附件。")
            raw_b64 = str(body.get("data_b64") or "").strip()
            if not raw_b64:
                raise ValueError("附件内容为空")
            if "," in raw_b64 and raw_b64.startswith("data:"):
                raw_b64 = raw_b64.split(",", 1)[1]
            ext = Path(name).suffix.lower()
            def _truncate_utf8(val: str, max_chars: int = 1150, max_bytes: int = 2500) -> str:
                s = str(val or "")[:max_chars]
                encoded = s.encode("utf-8")
                if len(encoded) <= max_bytes:
                    return s
                return encoded[:max_bytes].decode("utf-8", errors="ignore")

            is_image = mime.startswith("image/") or ext in IMAGE_EXTS
            if is_image:
                def _to_png(encoded_b64: str):
                    blob = base64.b64decode(encoded_b64)
                    if len(blob) > 12 * 1024 * 1024:
                        raise ValueError("图片体积超过 12MB 限制")
                    with _IMG_PIXEL_LOCK:
                        prev_max = Image.MAX_IMAGE_PIXELS
                        Image.MAX_IMAGE_PIXELS = 40_000_000
                        try:
                            with Image.open(io.BytesIO(blob)) as im:
                                if im.width * im.height > 25_000_000:
                                    raise ValueError("图片分辨率超过 2500 万像素限制")
                                im = im.convert("RGB")
                                im.thumbnail((2048, 2048))
                                buf = io.BytesIO()
                                im.save(buf, format="PNG")
                                return buf.getvalue(), im.size
                        finally:
                            Image.MAX_IMAGE_PIXELS = prev_max

                png_bytes, size = await asyncio.to_thread(_to_png, raw_b64)
                q = prompt or f"请解读这张图片（{name}）中的核心信息。"
                if len(q) > 1200:
                    q = q[:1200]
                if moss.model_available(window_image_provider):
                    await moss.message(q, chat_id, image_context=("composer_attach", png_bytes, size), owner_code=owner)
                else:
                    ocr_text = (await recognize_capture(png_bytes)).strip()
                    combined = f"{q}\n\n【本机 OCR 识别自图片 {name}（百炼视觉未配置，仅提取文字）】\n{ocr_text or '（未识别到文字）'}"
                    await moss.message(_truncate_utf8(combined, 1150, 2500), chat_id, owner_code=owner)
                return state_response(owner_code=owner, headers={"Cache-Control": "no-store"})

            def _extract_doc(encoded_b64: str):
                blob = base64.b64decode(encoded_b64)
                return extract_document_bytes(name, blob, max_chars=8000)

            extracted = await asyncio.to_thread(_extract_doc, raw_b64)
            q = prompt or f"请总结附件《{extracted['name']}》的关键要点。"
            ev = await asyncio.to_thread(
                moss.sag.upsert_event,
                content=f"附件《{extracted['name']}》：\n{extracted['text']}",
                title=f"附件：{extracted['name']}",
                source="attach",
                owner_code=owner,
            )
            sag_note = f"，已同步至 SAG E#{ev['id']}"
            preview_budget = max(200, 1080 - len(q) - len(extracted["name"]) - 120)
            snippet = extracted["text"][:preview_budget]
            msg = (
                f"{q}\n"
                f"【附件《{extracted['name']}》已解析（{extracted['bytes']}B{sag_note}，以下为只读正文摘录）】\n"
                f"{snippet}"
            )
            try:
                await moss.message(_truncate_utf8(msg, 1190, 2600), chat_id, owner_code=owner)
            except Exception:
                with moss.db:
                    delete_sag_events(moss.db, [ev["id"]], fts_ready=moss.sag._fts_ready)
                raise
            return state_response(owner_code=owner, headers={"Cache-Control": "no-store"})
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "附件超出 15 MB 上限。"}, status=413)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except (OSError, ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc) or "附件格式无法识别或已损坏。"}, status=400)

    app.add_routes([
        web.get("/api/chats/search", chats_search),
        web.get(r"/api/chats/{chat_id:[A-Za-z0-9_-]{1,64}}/export", chat_export),
        web.post("/api/chat/attach", chat_attach),
    ])

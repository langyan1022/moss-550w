"""Same-origin provider and media-library settings for the local FUI console."""
import asyncio
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import hmac
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import sqlite3
from tempfile import NamedTemporaryFile

from aiohttp import web
import aiohttp

from media_chat import json_command
from model_catalog import SPECS, allowed, profiles
from secret_store import SecretStoreError, app_config_dir, protect_secret


def provider_path():
    return app_config_dir() / "chat-providers.json"


def save_provider(provider_id, current, url, model, secret):
    if not isinstance(url, str) or len(url) > 512 or not allowed(provider_id, url):
        raise ValueError("请输入该服务商的官方 HTTPS API 地址。")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", model):
        raise ValueError("模型 ID 格式不正确。")
    if secret is not None and (not isinstance(secret, str) or not re.fullmatch(r"[!-~]{1,1024}", secret)):
        raise ValueError("API 密钥格式不正确。")
    if url != current.url and current.secret.get_secret_value() and secret is None:
        raise ValueError("更改连接地址时，请为该地址填写 API 密钥。")

    path = provider_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        config = {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("本机模型配置格式无效。") from None
    if not isinstance(config, dict) or not isinstance(config.get("profiles", {}), dict):
        raise ValueError("本机模型配置格式无效。")
    values = config.setdefault("profiles", {})
    stored = values.get(provider_id, {})
    if not isinstance(stored, dict):
        raise ValueError("本机服务商配置格式无效。")
    updated = {**stored, "base_url": url, "model": model}
    if secret is not None:
        updated["key"] = protect_secret(secret, entropy=f"MOSS-550W\0{provider_id}\0{url}", description="MOSS", flags=1)
    elif url != current.url:
        updated.pop("key", None)
    values[provider_id] = updated
    path.parent.mkdir(exist_ok=True, parents=True)
    staged = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", dir=path.parent, delete=False) as file:
            staged = Path(file.name)
            json.dump(config, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(staged, path)
    finally:
        if staged:
            staged.unlink(missing_ok=True)


def new_source(body):
    label, kind, path = (body.get(key) for key in ("label", "kind", "path"))
    if not isinstance(label, str) or not label.strip() or len(label) > 80 or any(ord(c) < 32 for c in label):
        raise ValueError("请填写 1 至 80 字的来源名称。")
    if kind not in ("local", "nas") or not isinstance(path, str) or len(path) > 520 or any(ord(c) < 32 for c in path):
        raise ValueError("请输入有效的本机目录或 NAS 路径。")
    path = path.strip().replace("/", "\\")
    drive, subpath = ntpath.splitdrive(path)
    drive_path = bool(re.fullmatch(r"[A-Za-z]:", drive)) and subpath.startswith("\\")
    unc = drive.startswith("\\\\") and not drive.startswith(("\\\\?\\", "\\\\.\\"))
    unc = unc and len([part for part in drive.split("\\") if part]) == 2
    if not (drive_path or unc) or (kind == "local" and unc) or any(c in path[2:] for c in '<>:"|?*'):
        raise ValueError("请填写盘符下的绝对路径，NAS 也支持 Windows 共享路径。")
    path = ntpath.normpath(path)
    identity = hashlib.sha256(ntpath.normcase(path).encode("utf-8")).hexdigest()[:16]
    return {"id": f"library-{identity}", "label": label.strip(), "kind": kind, "path": path}


def install_console_settings(app, moss):
    scan_task = None
    scan = {"state": "idle"}

    def reply(data, *, status=200):
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})

    async def body_for(request):
        if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            raise ValueError("无法读取设置内容。") from None
        if not isinstance(body, dict):
            raise ValueError("无法读取设置内容。")
        return body

    # ── v5.5 Backend-Persisted Per-Identity Preferences & 4-Char CS/C Auth Code Gate ──
    # Rules:
    #   - Auth code is strictly 4 alphanumeric chars, case-insensitive (normalized uppercase).
    #   - "MOSS" => CS (Supreme Admin: full system settings & host controls).
    #   - Any other 4-char code => C (Persistent personal profile; only personalization allowed:
    #     badge, city/location, spectrum/visuals/layout, companion personality/tone).
    C_ALLOWED_PREF_KEYS = frozenset({
        "badge", "city_id", "city_manual", "spectrum", "oled_palette",
        "rail_layout", "comms_drawer", "meeting_mute", "locale",
        "personality_id", "tts_enabled", "hud_auto", "last_chat_id",
        "show_opening", "show_cloud_counts",
    })
    ADMIN_CODE = "MOSS"

    moss.db.execute(
        """CREATE TABLE IF NOT EXISTS auth_users (
            code TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('CS', 'C')),
            label TEXT NOT NULL DEFAULT '',
            disabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_chat_id TEXT NOT NULL DEFAULT ''
        )"""
    )
    existing_cols = {r[1] for r in moss.db.execute("PRAGMA table_info(auth_users)").fetchall()}
    if "label" not in existing_cols:
        moss.db.execute("ALTER TABLE auth_users ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    if "disabled" not in existing_cols:
        moss.db.execute("ALTER TABLE auth_users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
    moss.db.execute(
        """CREATE TABLE IF NOT EXISTS console_prefs (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    moss.db.commit()

    def _pref_get(key, default=None):
        row = moss.db.execute("SELECT value FROM console_prefs WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def _pref_set(key, val):
        now_s = datetime.now().isoformat(timespec="seconds")
        moss.db.execute(
            "INSERT INTO console_prefs(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(val, ensure_ascii=False), now_s),
        )
        moss.db.commit()

    def _ensure_user(code: str, *, label: str = "") -> dict:
        norm = code.strip().upper()
        role = "CS" if norm == ADMIN_CODE else "C"
        now_s = datetime.now().isoformat(timespec="seconds")
        moss.db.execute(
            "INSERT INTO auth_users(code, role, label, disabled, created_at, last_seen_at, last_chat_id) VALUES (?, ?, ?, 0, ?, ?, '') "
            "ON CONFLICT(code) DO UPDATE SET role=excluded.role, last_seen_at=excluded.last_seen_at",
            (norm, role, label[:40], now_s, now_s),
        )
        moss.db.commit()
        row = moss.db.execute(
            "SELECT code, role, label, disabled, created_at, last_seen_at, last_chat_id FROM auth_users WHERE code=?",
            (norm,),
        ).fetchone()
        return {
            "code": row[0], "role": row[1], "is_admin": row[1] == "CS",
            "label": row[2] or ("最高管理员" if row[1] == "CS" else ""),
            "disabled": bool(row[3]),
            "created_at": row[4], "last_seen_at": row[5], "last_chat_id": row[6] or "",
        }

    def _list_auth_users() -> list[dict]:
        _ensure_user(ADMIN_CODE)
        rows = moss.db.execute(
            "SELECT code, role, label, disabled, created_at, last_seen_at, last_chat_id "
            "FROM auth_users ORDER BY CASE WHEN role='CS' THEN 0 ELSE 1 END, last_seen_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            pref_cnt = moss.db.execute(
                "SELECT COUNT(*) FROM console_prefs WHERE key LIKE ?", (f"user:{r[0]}:%",)
            ).fetchone()[0]
            out.append({
                "code": r[0], "role": r[1], "is_admin": r[1] == "CS",
                "label": r[2] or ("最高管理员" if r[1] == "CS" else ""),
                "disabled": bool(r[3]), "created_at": r[4], "last_seen_at": r[5],
                "last_chat_id": r[6] or "", "pref_count": int(pref_cnt),
            })
        return out

    def _user_prefs(code: str) -> dict:
        prefix = f"user:{code}:"
        rows = moss.db.execute(
            "SELECT key, value FROM console_prefs WHERE key LIKE ?",
            (prefix + "%",),
        ).fetchall()
        out = {}
        for k, v in rows:
            short_k = k[len(prefix):]
            try:
                out[short_k] = json.loads(v)
            except ValueError:
                continue
        return out

    def _auth_cfg():
        cfg = _pref_get("_auth_gate") or {}
        changed = False
        if not cfg.get("secret"):
            cfg["secret"] = secrets.token_hex(32)
            changed = True
        if "enabled" not in cfg:
            cfg["enabled"] = True
            changed = True
        if cfg.get("scope") not in ("lan", "all"):
            cfg["scope"] = "lan"
            changed = True
        if changed:
            _pref_set("_auth_gate", cfg)
        return cfg

    def _sign_session(secret_hex: str, code: str, role: str) -> str:
        msg = f"moss-550w-id:{code}:{role}".encode("utf-8")
        sig = hmac.new(secret_hex.encode("ascii"), msg, hashlib.sha256).hexdigest()
        return f"{code}.{role}.{sig}"

    def _parse_session(cookie: str, secret_hex: str) -> tuple[str, str] | None:
        if not cookie or cookie.count(".") != 2:
            return None
        code, role, sig = cookie.split(".", 2)
        if not re.fullmatch(r"[A-Z0-9]{4}", code) or role not in ("CS", "C"):
            return None
        expected_role = "CS" if code == ADMIN_CODE else "C"
        if role != expected_role:
            return None
        expected = _sign_session(secret_hex, code, role)
        if not hmac.compare_digest(cookie, expected):
            return None
        return code, role

    def _resolve_identity(request) -> tuple[bool, dict]:
        cfg = _auth_cfg()
        enabled = bool(cfg.get("enabled", True))
        scope = cfg.get("scope") if cfg.get("scope") in ("lan", "all") else "lan"
        is_local = request.remote in {"127.0.0.1", "::1"}
        parsed = _parse_session(request.cookies.get("moss_auth") or "", cfg["secret"])
        if parsed is not None:
            code, role = parsed
            if role != "CS":
                drow = moss.db.execute("SELECT disabled FROM auth_users WHERE code=?", (code,)).fetchone()
                if not drow or drow[0]:
                    return False, {
                        "enabled": True, "scope": scope, "authorized": False,
                        "code": "", "role": "", "is_admin": False, "via": "disabled",
                    }
            return True, {
                "enabled": enabled, "scope": scope, "authorized": True,
                "code": code, "role": role, "is_admin": role == "CS",
                "via": "cookie",
            }
        if not enabled or (scope == "lan" and is_local):
            return True, {
                "enabled": enabled, "scope": scope, "authorized": True,
                "code": ADMIN_CODE, "role": "CS", "is_admin": True,
                "via": "local_default",
            }
        return False, {
            "enabled": True, "scope": scope, "authorized": False,
            "code": "", "role": "", "is_admin": False,
            "via": "gate",
        }

    app["moss_auth_check"] = _resolve_identity

    async def console_prefs_handler(request):
        ok, ident = _resolve_identity(request)
        if not ok:
            return reply({"error": "请先输入 4 位 550W 授权码。", "auth_required": True}, status=401)
        code = ident["code"]
        role = ident["role"]
        user = _ensure_user(code)
        if request.method == "GET":
            return reply({"code": code, "role": role, "is_admin": role == "CS",
                          "last_chat_id": user["last_chat_id"], "prefs": _user_prefs(code)})
        try:
            body = await body_for(request)
            patch = body.get("prefs") if isinstance(body.get("prefs"), dict) else body
            if not isinstance(patch, dict) or len(patch) > 40:
                raise ValueError("偏好设置格式无效。")
            rejected = []
            for k, v in patch.items():
                if not isinstance(k, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,48}", k) or k.startswith("_"):
                    continue
                if role != "CS" and k not in C_ALLOWED_PREF_KEYS:
                    rejected.append(k)
                    continue
                if len(json.dumps(v, ensure_ascii=False)) > 65536:
                    continue
                _pref_set(f"user:{code}:{k}", v)
                if k == "last_chat_id" and isinstance(v, str):
                    moss.db.execute("UPDATE auth_users SET last_chat_id=? WHERE code=?", (v[:64], code))
                    moss.db.commit()
            if rejected and len(rejected) == len(patch):
                return reply({"error": "当前为 C 级权限，仅支持保存工牌、定位、频谱与陪伴语气等个性化配置。", "required_role": "CS"}, status=403)
            return reply({"code": code, "role": role, "is_admin": role == "CS",
                          "prefs": _user_prefs(code), "rejected": rejected})
        except ValueError as err:
            return reply({"error": str(err)}, status=400)

    async def auth_status(request):
        _, info = _resolve_identity(request)
        if info.get("authorized") and info.get("code"):
            user = _ensure_user(info["code"])
            info = {**info, "last_chat_id": user["last_chat_id"], "prefs": _user_prefs(info["code"])}
        users_count = moss.db.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0]
        info["registered_users"] = int(users_count)
        return reply(info)

    async def auth_verify(request):
        try:
            body = await body_for(request)
            raw_code = str(body.get("code") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{4}", raw_code):
                return reply({"ok": False, "error": "授权码须为 4 位字母或数字（不区分大小写）。"}, status=400)
            cfg = _auth_cfg()
            existing_u = moss.db.execute("SELECT disabled FROM auth_users WHERE code=?", (raw_code,)).fetchone()
            if raw_code != ADMIN_CODE and existing_u and existing_u[0]:
                return reply({"ok": False, "error": f"授权码 {raw_code} 已被管理员停用，请联系 CS 管理员。"}, status=403)
            user = _ensure_user(raw_code)
            code, role = user["code"], user["role"]
            sig = _sign_session(cfg["secret"], code, role)
            prefs = _user_prefs(code)
            # Restore this operator's saved companion personality seamlessly
            saved_pers = prefs.get("personality_id")
            if isinstance(saved_pers, str) and saved_pers:
                try:
                    await moss.select_personality(saved_pers)
                except Exception:
                    pass
            resp = reply({
                "ok": True, "authorized": True, "enabled": bool(cfg.get("enabled", True)),
                "scope": cfg.get("scope", "lan"), "code": code, "role": role,
                "is_admin": role == "CS", "last_chat_id": user["last_chat_id"], "prefs": prefs,
            })
            resp.set_cookie(
                "moss_auth", sig, max_age=30 * 86400, httponly=True, samesite="Lax",
                secure=(request.scheme == "https"), path="/",
            )
            return resp
        except ValueError as err:
            return reply({"error": str(err)}, status=400)

    async def auth_config(request):
        ok, ident = _resolve_identity(request)
        if not ok or ident.get("role") != "CS":
            return reply({"error": "仅 CS 管理员可修改访问授权策略。", "required_role": "CS"}, status=403)
        try:
            body = await body_for(request)
            cfg = _auth_cfg()
            enabled = bool(body.get("enabled", cfg.get("enabled", True)))
            scope = body.get("scope") if body.get("scope") in ("lan", "all") else (cfg.get("scope") or "lan")
            cfg["enabled"] = enabled
            cfg["scope"] = scope
            _pref_set("_auth_gate", cfg)
            resp = reply({"enabled": enabled, "scope": scope, "authorized": True, "code": ident["code"], "role": "CS", "is_admin": True})
            sig = _sign_session(cfg["secret"], ADMIN_CODE, "CS")
            resp.set_cookie(
                "moss_auth", sig, max_age=30 * 86400, httponly=True, samesite="Lax",
                secure=(request.scheme == "https"), path="/",
            )
            return resp
        except ValueError as err:
            return reply({"error": str(err)}, status=400)

    async def auth_logout(request):
        if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        resp = reply({"ok": True, "authorized": False})
        resp.del_cookie("moss_auth", path="/")
        return resp

    async def auth_users_handler(request):
        ok, ident = _resolve_identity(request)
        if not ok or ident.get("role") != "CS":
            return reply({"error": "仅 CS 管理员（授权码 MOSS）可管理授权码与用户档案。", "required_role": "CS"}, status=403)
        if request.method == "GET":
            return reply({"users": _list_auth_users(), "gate": {"enabled": bool(_auth_cfg().get("enabled", True)),
                                                                "scope": _auth_cfg().get("scope", "lan")}})
        if request.method == "DELETE":
            target = (request.match_info.get("code") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{4}", target):
                return reply({"error": "无效的 4 位授权码。"}, status=400)
            if target == ADMIN_CODE:
                return reply({"error": "不可删除终极管理员授权码 MOSS。"}, status=400)
            moss.db.execute("DELETE FROM auth_users WHERE code=?", (target,))
            moss.db.execute("DELETE FROM console_prefs WHERE key LIKE ?", (f"user:{target}:%",))
            moss.db.commit()
            return reply({"deleted": target, "users": _list_auth_users()})
        try:
            body = await body_for(request)
            target = str(body.get("code") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{4}", target):
                raise ValueError("授权码须为 4 位字母或数字（不区分大小写）。")
            label = str(body.get("label") or "").strip()[:40]
            disabled = bool(body.get("disabled", False))
            if target == ADMIN_CODE and disabled:
                raise ValueError("不可停用终极管理员授权码 MOSS。")
            _ensure_user(target, label=label)
            if "label" in body:
                moss.db.execute("UPDATE auth_users SET label=? WHERE code=?", (label, target))
            if "disabled" in body and target != ADMIN_CODE:
                moss.db.execute("UPDATE auth_users SET disabled=? WHERE code=?", (1 if disabled else 0, target))
            moss.db.commit()
            return reply({"users": _list_auth_users()})
        except ValueError as err:
            return reply({"error": str(err)}, status=400)

    def provider_state():
        rows = []
        for key, profile in moss.cloud.items():
            if key in SPECS:
                url = profile.url if allowed(key, profile.url) else SPECS[key][2]
                rows.append({**profile.public(), "base_url": url})
        return {"providers": rows, "selected": moss.model_id}

    async def providers(request):
        if request.method == "GET":
            return reply(provider_state())
        try:
            body = await body_for(request)
            provider_id = body.get("id")
            if not isinstance(provider_id, str) or provider_id not in SPECS:
                raise ValueError("未知模型服务商。")
            moss._check_idle()
            url = body.get("base_url")
            model = body.get("model")
            save_provider(provider_id, moss.cloud[provider_id], url.strip().rstrip("/") if isinstance(url, str) else url,
                          model.strip() if isinstance(model, str) else model, body.get("api_key") or None)
        except ValueError as error:
            return reply({"error": str(error)}, status=400)
        except (OSError, SecretStoreError):
            return reply({"error": "无法保存本机模型设置。"}, status=500)
        moss.cloud[provider_id] = profiles()[provider_id]
        retired = moss.clients.pop(provider_id, None)
        if moss.model_id == provider_id:
            moss.agent = None
        with moss.db:
            moss.db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
        moss._notify()
        if retired:
            await retired.close()
        return reply(provider_state())

    def sources():
        return [{key: row.get(key) for key in ("id", "label", "kind", "path", "state", "checked", "count", "error")}
                for row in moss.media.library.sources()]

    async def scan_library(source_id):
        nonlocal scan
        args = ["local_media.media_cli", "scan", "--db", str(moss.media.db)]
        if source_id:
            args.insert(2, source_id)
        try:
            result = await json_command(args)
            rows = [row for row in result["sources"] if source_id is None or row["id"] == source_id]
            scan = {"state": "done" if all(row["state"] == "ready" for row in rows) else "partial"}
        except (OSError, ValueError, KeyError):
            scan = {"state": "error", "error": "扫描中断，请检查目录后重试。"}

    async def library(request):
        nonlocal scan_task, scan
        try:
            body = await body_for(request) if request.method == "POST" else None
            if body is not None and scan_task and not scan_task.done():
                return reply({"error": "已有媒体扫描正在运行。"}, status=409)
            rows = sources()
            if body is not None and body.get("action") == "add":
                source = new_source(body)
                if any(ntpath.normcase(ntpath.normpath(row["path"])) == ntpath.normcase(source["path"]) for row in rows):
                    raise ValueError("该目录已经在媒体库中。")
                library = moss.media.library
                with closing(library.connect()) as db, db:
                    db.execute("INSERT INTO sources(id,config,state) VALUES(?,?,'new')",
                               (source["id"], json.dumps(source, ensure_ascii=False, sort_keys=True)))
                rows = sources()
            elif body is not None and body.get("action") == "scan":
                source_id = body.get("id")
                if not rows or (source_id is not None and
                                (not isinstance(source_id, str) or source_id not in {row["id"] for row in rows})):
                    raise ValueError("找不到要扫描的媒体来源。")
                scan = {"state": "running", "id": source_id}
                scan_task = asyncio.create_task(scan_library(source_id))
            elif body is not None:
                raise ValueError("未知媒体库操作。")
        except ValueError as error:
            return reply({"error": str(error)}, status=400)
        except sqlite3.DatabaseError:
            return reply({"error": "媒体索引暂时不可用，请稍后再试。"}, status=503)
        return reply({"sources": rows, "scan": scan})

    async def online_music_config(request):
        ok, ident = _resolve_identity(request)
        if not ok:
            return reply({"error": "请先输入 4 位 550W 授权码。", "auth_required": True}, status=401)
        if ident.get("role") != "CS":
            return reply({"error": "仅 CS 管理员可查看或修改在线曲库与私服凭据配置。", "required_role": "CS"}, status=403)
        if request.method == "GET":
            return reply({**moss.online_music.public_config(), "is_admin": True})
        try:
            body = await body_for(request)
            saved = moss.online_music.save_config(body)
            return reply({**saved, "is_admin": True})
        except (ValueError, RuntimeError) as err:
            return reply({"error": str(err)}, status=400)

    async def online_music_ping(request):
        ok, ident = _resolve_identity(request)
        if not ok:
            return reply({"error": "请先输入 4 位 550W 授权码。", "auth_required": True}, status=401)
        if ident.get("role") != "CS":
            return reply({"error": "仅 CS 管理员可探测在线曲库服务。", "required_role": "CS"}, status=403)
        try:
            body = await body_for(request)
            target = str(body.get("target") or "navidrome").strip().lower()
            if target == "navidrome":
                res = await moss.online_music.ping_navidrome(body)
                return reply(res)
            if target == "public":
                res = await moss.online_music.search_tracks(str(body.get("query") or "piano")[:40], limit=2)
                items = res.get("items") or []
                jam_res = await moss.online_music.ping_jamendo(body.get("jamendo_client_id"))
                jam_detail = jam_res.get("detail") or ""
                jam_err = bool(jam_res.get("error"))
                return reply({
                    "ok": bool(items) and not jam_err,
                    "count": len(items),
                    "providers": res.get("providers_consulted") or [],
                    "sample": items[0]["title"] if items else "",
                    "jamendo_detail": jam_detail,
                    "jamendo_error": jam_err,
                })
            raise ValueError("未知在线曲库探测目标。")
        except (ValueError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            return reply({"ok": False, "error": str(err)}, status=400)

    async def stop_scan(_):
        if scan_task and not scan_task.done():
            scan_task.cancel()
            await asyncio.gather(scan_task, return_exceptions=True)

    app.on_shutdown.append(stop_scan)
    base = "/api/console-settings"
    app.add_routes([web.get(base + "/providers", providers), web.post(base + "/providers", providers),
                    web.get(base + "/library", library), web.post(base + "/library", library),
                    web.get(base + "/prefs", console_prefs_handler), web.post(base + "/prefs", console_prefs_handler),
                    web.get("/api/online-music/config", online_music_config),
                    web.post("/api/online-music/config", online_music_config),
                    web.post("/api/online-music/ping", online_music_ping),
                    web.get("/api/auth/status", auth_status), web.post("/api/auth/verify", auth_verify),
                    web.post("/api/auth/config", auth_config), web.post("/api/auth/logout", auth_logout),
                    web.get("/api/auth/users", auth_users_handler), web.post("/api/auth/users", auth_users_handler),
                    web.delete(r"/api/auth/users/{code:[A-Za-z0-9]{4}}", auth_users_handler)])

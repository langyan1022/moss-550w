"""Transfer a chosen Jev reply to an editor process owned by MOSS."""

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID

from jev_contacts import match_contact


ROOT = Path(__file__).resolve().parent
EDITOR = ROOT / "jev-draft-editor.ps1"
UIA = ROOT / "jev-draft-uia.ps1"
ID = re.compile("[a-f0-9]{32}")
DRAFT_HANDOFF_ENABLED = os.name == "nt" and EDITOR.is_file() and UIA.is_file()
CONTACT_HANDOFF_ACTION = "jev_contact_handoff"
UNCERTAIN = {"status": "uncertain", "error": "填入结果未能核实，请查看已打开的本机草稿窗。"}


def launch_args(script, *args):
    host = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return [str(host), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *args]


@asynccontextmanager
async def uia_session():
    process = await asyncio.create_subprocess_exec(
        *launch_args(UIA, "-Session"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    async def command(message):
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        async with asyncio.timeout(8):
            process.stdin.write(payload)
            await process.stdin.drain()
            output = await process.stdout.readline()
        if not output or len(output) > 8192:
            raise OSError("UI Automation command did not finish")
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError("Invalid UI Automation result")
        return result

    try:
        yield command
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def open_draft_window(text, target_id, still_current, *, recipient=None, offscreen=False):
    args = ["-TargetId", target_id]
    if recipient is not None:
        args.append("-RecipientBound")
    if offscreen:
        args.append("-Offscreen")
    stdin = subprocess.PIPE if recipient is not None else subprocess.DEVNULL
    process = subprocess.Popen(launch_args(EDITOR, *args), stdin=stdin, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    ready_window = False
    write_started = False
    try:
        if recipient is not None:
            with process.stdin:
                process.stdin.write(json.dumps(recipient, ensure_ascii=False).encode("utf-8") + b"\n")
        async with uia_session() as command:
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline, 8192), timeout=10)
            ready = json.loads(line)
            if (not isinstance(ready, dict) or ready.get("status") != "ready" or ready.get("pid") != process.pid or
                    type(ready.get("window")) is not int or ready["window"] <= 0 or process.poll() is not None):
                raise ValueError("Invalid native editor identity")
            ready_window = True
            target = {"pid":process.pid, "window":ready["window"], "target_id":target_id}
            if recipient is not None:
                target["recipient"] = recipient
            armed = await command({"mode":"arm", **target})
            if armed.get("status") != "armed" or not isinstance(armed.get("identity"), dict):
                return {"status":"refused", "error":"本机草稿框无法核对，未填入。"}
            if not still_current():
                if recipient is not None:
                    return {"status":"refused", "error":"联系人、草稿或当前对话已变更，请重新核对。"}
                return {"status":"refused", "error":"对话已经切换，未填入。"}
            write_started = True
            reply = await command({"mode":"fill", **target, "expected":armed["identity"], "text":text})
        if reply.get("status") == "refused":
            return {"status":"refused", "error":"本机草稿框已经变化，未填入。"}
        original = text.encode("utf-8")
        if (reply.get("status") == "filled" and type(reply.get("bytes")) is int and
                reply["bytes"] == len(original) and reply.get("sha256") == hashlib.sha256(original).hexdigest()):
            return {"status":"filled", "bytes":len(original)}
        return dict(UNCERTAIN)
    except (OSError, ValueError, TimeoutError):
        return dict(UNCERTAIN) if write_started else {"status":"refused", "error":"本机草稿窗未能就绪，未填入。"}
    finally:
        # Once the window is visible, the user may have typed into it independently.
        if not ready_window and process.poll() is None:
            with suppress(ProcessLookupError):
                process.terminate()
            await asyncio.to_thread(process.wait)
        process.stdout.close()


class JevHandoffs:
    def __init__(self, moss):
        self.moss = moss
        self.job = None
        self.operation = None
        self.closing = False
        with moss.db:
            moss.db.execute("""CREATE TABLE IF NOT EXISTS jev_handoffs (
                operation TEXT PRIMARY KEY, chat_id TEXT NOT NULL, turn_id TEXT NOT NULL, result TEXT)""")

    @staticmethod
    def _draft_text(turn, chat_id):
        text = (turn or {}).get("reply")
        if (not turn or turn["chat_id"] != chat_id or turn["status"] != "replied" or turn.get("source") != "jev"
                or not (turn.get("jev") or {}).get("candidate_count") or not isinstance(text, str) or not text.strip()):
            raise ValueError("请先在当前对话拟好一份回复。")
        return text

    def prepare_contact(self, turn, alias):
        if not DRAFT_HANDOFF_ENABLED:
            raise ValueError("本机草稿窗不可用。")
        text = self._draft_text(turn, self.moss.chat_id)
        contact = match_contact(self.moss.drafts.parent, alias)
        binding = {"alias":alias, "contact":contact, "draft_turn":turn["id"],
                   "draft_sha256":hashlib.sha256(text.encode("utf-8")).hexdigest()}
        return {"action":CONTACT_HANDOFF_ACTION, "title":"核对联系人草稿",
                "detail":f"联系人：{contact['display_name']}\n\n{text}", "snapshot":binding}

    def _bound_text(self, chat_id, binding):
        contact = match_contact(self.moss.drafts.parent, binding["alias"])
        if contact != binding["contact"]:
            raise ValueError("联系人信息已变更，请重新核对草稿。")
        text = self._draft_text(self.moss._get(binding["draft_turn"]), chat_id)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != binding["draft_sha256"]:
            raise ValueError("原草稿内容已变更，请重新核对。")
        return text

    def _lookup(self, operation):
        return self.moss.db.execute(
            "SELECT chat_id, turn_id, result FROM jev_handoffs WHERE operation=?", (operation,)
        ).fetchone()

    def recover_contact(self, turn):
        row = self._lookup(str(UUID(hex=turn["id"])))
        if row is None:
            return None
        return json.loads(row[2]) if row[:2] == (turn["chat_id"], turn["id"]) and row[2] else dict(UNCERTAIN)

    async def submit(self, body, *, binding=None):
        if not isinstance(body, dict) or set(body) != {"operation_id", "chat_id", "turn_id"}:
            raise ValueError("请从原回复打开本机草稿窗。")
        operation, chat_id, turn_id = (body[key] for key in ("operation_id", "chat_id", "turn_id"))
        if not all(isinstance(value, str) and ID.fullmatch(value) for value in (chat_id, turn_id)):
            raise ValueError("对话或回复编号无效。")
        try:
            valid_operation = isinstance(operation, str) and len(operation) == 36 and str(UUID(operation)) == operation
        except ValueError:
            valid_operation = False
        if not valid_operation:
            raise ValueError("操作编号无效。")
        if self.closing or chat_id != self.moss.chat_id:
            raise ValueError("请回到这条回复所属的对话。")
        row = self._lookup(operation)
        if row:
            if row[:2] != (chat_id, turn_id):
                raise ValueError("操作编号已属于另一条回复。")
            if self.job and self.operation == operation:
                return await asyncio.shield(self.job)
            return json.loads(row[2]) if row[2] else dict(UNCERTAIN)
        if self.job:
            raise ValueError("正在填入另一份草稿，请等当前操作结束。")
        if binding is None:
            try:
                text = self._draft_text(self.moss._get(turn_id), chat_id)
            except ValueError as error:
                raise ValueError("这条回复没有可填入的 Jev 草稿。") from error
        else:
            text = self._bound_text(chat_id, binding)
        with self.moss.db:
            self.moss.db.execute("INSERT INTO jev_handoffs VALUES(?,?,?,NULL)", (operation, chat_id, turn_id))
        self.operation = operation
        self.job = asyncio.create_task(self._commit(operation, chat_id, text, binding))
        return await asyncio.shield(self.job)

    async def _commit(self, operation, chat_id, text, binding):
        def still_current():
            if self.closing or self.moss.chat_id != chat_id:
                return False
            if binding is not None:
                try:
                    self._bound_text(chat_id, binding)
                except ValueError:
                    return False
            return True
        try:
            native = {"recipient":binding["contact"]["display_name"]} if binding is not None else {}
            result = await open_draft_window(text, UUID(operation).hex, still_current, **native)
            if binding is not None and result["status"] == "filled" and not still_current():
                result = dict(UNCERTAIN)
        except asyncio.CancelledError:
            result = dict(UNCERTAIN)
        except OSError:
            result = {"status":"refused", "error":"本机草稿窗无法打开。"}
        finally:
            self.job = None
            self.operation = None
        with self.moss.db:
            self.moss.db.execute("UPDATE jev_handoffs SET result=? WHERE operation=?",
                                 (json.dumps(result, ensure_ascii=False), operation))
        return result

    async def close(self):
        self.closing = True
        job = self.job
        if job is not None:
            if not job.done() and not job.cancelling():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

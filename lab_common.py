import asyncio
import hashlib
import json
import os
import time
from pathlib import Path


EXPECTED = "明天核对清单"
PROMPT = "帮我保存一份本地草稿，内容逐字写：明天核对清单"
SYSTEM = "你是 MOSS 办公助手。用户要求保存草稿时，调用 save_draft 一次，text 只填写要保存的原文。收到工具的保存回执后，用一句简短的中文确认。"
TOOL_DESCRIPTION = "将指定文字原样保存到本轮的本地草稿。"
CANCEL_WRITE_DELAY = 2.0
CANCEL_OBSERVE = 3.0


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Trial:
    def __init__(self, out, mode):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.path = self.out / "draft.txt"
        self.journal = self.out / "events.jsonl"
        if self.path.exists() or self.journal.exists():
            raise FileExistsError(f"请使用新的样本目录 {self.out}")
        self.entered = asyncio.Event()
        self.events = []
        self.started_at = None

    def start(self):
        self.started_at = time.perf_counter()
        self.record("request")

    def record(self, event, **data):
        row = {"event": event, "s": round(time.perf_counter() - self.started_at, 6), **data}
        self.events.append(row)
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row["s"]

    async def save_draft(self, text: str) -> str:
        self.record("tool_enter", text=text)
        self.entered.set()
        try:
            if self.mode == "cancel":
                await asyncio.sleep(CANCEL_WRITE_DELAY)
            with self.path.open("xb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            content = self.path.read_bytes()
            receipt = "本地草稿已保存：" + content.decode("utf-8")
            self.record("verified_receipt", receipt=receipt, matches_request=content == EXPECTED.encode())
            return receipt
        except asyncio.CancelledError:
            self.record("tool_cancelled")
            raise
        finally:
            self.record("tool_exit")

    def request_cancel(self):
        return self.record("cancel_requested")

    async def finish(self, reply=None, **extras):
        if self.started_at is None:
            self.start()
        self.record("run_returned", reply=reply)
        if self.mode == "cancel":
            await asyncio.sleep(CANCEL_OBSERVE)
        content = self.path.read_bytes() if self.path.exists() else None
        self.record("observation_end", file_exists=content is not None)
        first = {}
        for row in self.events:
            first.setdefault(row["event"], row["s"])
        calls = sum(row["event"] == "tool_enter" for row in self.events)
        cancel_at = first.get("cancel_requested")
        end = first["run_returned"]
        stopped = first.get("tool_cancelled")
        draft_ok = content == EXPECTED.encode() and calls == 1 and bool(reply and str(reply).strip())
        cancel_ok = calls == 1 and stopped is not None and cancel_at is not None and content is None
        result = {
            **extras, "mode": self.mode, "pid": os.getpid(), "reply": reply,
            "tool_calls": calls, "tool_entry_s": first.get("tool_enter"),
            "verified_receipt_s": first.get("verified_receipt"), "run_return_s": end,
            "cancel_request_s": cancel_at,
            "cancel_to_return_s": round(end - cancel_at, 6) if cancel_at is not None else None,
            "cancel_to_tool_stop_s": round(stopped - cancel_at, 6) if stopped is not None and cancel_at is not None else None,
            "observation_end_s": first["observation_end"],
            "file_exists": content is not None, "saved_text": content.decode() if content is not None else None,
            "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
            "passed": (cancel_ok if self.mode == "cancel" else draft_ok) and not extras.get("error"),
        }
        save_json(self.out / "result.json", result)
        return result

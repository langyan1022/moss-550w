"""Verify the draft bridge against two MOSS-owned offscreen WPF editors."""

import asyncio
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
POWERSHELL = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
FLAGS = ("-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-File")


async def launch(script, *args):
    return await asyncio.create_subprocess_exec(
        str(POWERSHELL), *FLAGS, str(ROOT / script), *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


async def stop(child):
    if child.returncode is None:
        child.terminate()
    await child.wait()


async def open_target(children, target_id=None, *, script="jev-draft-editor.ps1"):
    target_id = target_id or uuid4().hex
    child = await launch(script, "-TargetId", target_id, "-Offscreen")
    children.append(child)
    line = await asyncio.wait_for(child.stdout.readline(), 15)
    if not line:
        diagnostics = await asyncio.wait_for(child.stderr.read(), 5)
        raise AssertionError(diagnostics.decode("utf-8", errors="replace"))
    signal = json.loads(line.decode("utf-8"))
    assert signal["status"] == "ready" and signal["pid"] == child.pid and signal["window"] > 0, signal
    return {"pid":signal["pid"], "window":signal["window"], "target_id":target_id}, child


async def verify_native():
    children = []
    report = {"passed":False, "scope":"MOSS-owned offscreen WPF processes", "cases":[]}

    async def command(label, status, mode, target, *, reason=None, **fields):
        worker = await launch("jev-draft-uia.ps1")
        children.append(worker)
        body = json.dumps({"mode":mode, **target, **fields}, ensure_ascii=False).encode("utf-8") + b"\n"
        out, error = await asyncio.wait_for(worker.communicate(body), 15)
        assert worker.returncode == 0 and not error, error.decode("utf-8", errors="replace")
        lines = out.decode("utf-8").splitlines()
        assert len(lines) == 1
        result = json.loads(lines[0])
        assert result["status"] == status and (reason is None or reason == result.get("reason")), (label, result)
        assert "text" not in result and "readback" not in result
        report["cases"].append({"label":label, "status":status, "reason":result.get("reason")})
        return result

    async def open_session():
        worker = await launch("jev-draft-uia.ps1", "-Session")
        children.append(worker)
        return worker

    async def exchange(worker, message):
        worker.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        await worker.stdin.drain()
        line = await asyncio.wait_for(worker.stdout.readline(), 15)
        assert line, "session worker exited before its receipt"
        return json.loads(line.decode("utf-8"))

    async def finish_session(worker):
        worker.stdin.close()
        await asyncio.wait_for(worker.wait(), 10)
        assert worker.returncode == 0 and not await worker.stdout.read() and not await worker.stderr.read()

    try:
        first, first_child = await open_target(children)
        second, _ = await open_target(children)
        assert first["window"] != second["window"] and first["pid"] != second["pid"]
        armed = await command("first editor", "armed", "arm", first)
        identity = armed["identity"]
        assert identity["pid"] == first["pid"] and identity["window"] == first["window"]
        assert int(identity["process_start"]) > 0 and identity["editor_runtime"]
        assert identity["conversation_anchor"] == first["target_id"]

        text = "收到，我十五分钟后到。谢谢！🙂"
        await command("wrong window", "refused", "fill", second, reason="identity_changed", expected=identity, text=text)
        await command("wrong PID", "refused", "arm", {**first, "pid":second["pid"]}, reason="invalid_process")
        await command("wrong conversation", "refused", "arm", {**first, "target_id":second["target_id"]}, reason="conversation_changed")
        await command("changed process start time", "refused", "fill", first, reason="identity_changed",
                      expected={**identity, "process_start":"1"}, text=text)
        await command("changed editor runtime", "refused", "fill", first, reason="identity_changed",
                      expected={**identity, "editor_runtime":"0.0"}, text=text)
        await command("missing approval identity", "refused", "fill", first, reason="missing_identity", text=text)
        await command("blank draft", "refused", "fill", first, reason="invalid_text", expected=identity, text=" \n")

        fill = await command("exact Chinese and emoji readback", "filled", "fill", first, expected=identity, text=text)
        assert fill == {"status":"filled", "sha256":sha256(text.encode("utf-8")).hexdigest(), "bytes":len(text.encode("utf-8"))}
        report["verified_output"] = {"sha256":fill["sha256"], "bytes":fill["bytes"]}
        await command("already filled arm refused", "refused", "arm", first, reason="input_not_empty")
        await command("second write refused", "refused", "fill", first, reason="input_changed", expected=identity, text="不同的草稿")

        second_identity = (await command("other editor remained empty", "armed", "arm", second))["identity"]
        multiline = "第一行。\n第二行。🙂"
        fill = await command("exact multiline readback", "filled", "fill", second, expected=second_identity, text=multiline)
        assert fill["sha256"] == sha256(multiline.encode("utf-8")).hexdigest() and fill["bytes"] == len(multiline.encode("utf-8"))

        await stop(first_child)
        await command("closed editor", "refused", "arm", first, reason="invalid_window")
        replacement, _ = await open_target(children, first["target_id"])
        await command("restarted editor refused old identity", "refused", "fill", replacement,
                      reason="identity_changed", expected=identity, text=text)
        await command("replacement still empty", "armed", "arm", replacement)

        eof_worker = await open_session()
        assert (await exchange(eof_worker, {"mode":"arm", **replacement}))["status"] == "armed"
        await finish_session(eof_worker)
        await command("session EOF left draft empty", "armed", "arm", replacement)

        wrong_target_worker = await open_session()
        assert (await exchange(wrong_target_worker, {"mode":"arm", **replacement}))["status"] == "armed"
        refused = await exchange(wrong_target_worker, {"mode":"fill", **second, "expected":second_identity, "text":text})
        assert refused == {"status":"refused", "reason":"identity_changed"}
        await finish_session(wrong_target_worker)

        start = perf_counter()
        same_worker = await open_session()
        armed = await exchange(same_worker, {"mode":"arm", **replacement})
        assert armed["status"] == "armed"
        arm_at = perf_counter()
        fill = await exchange(same_worker, {"mode":"fill", **replacement, "expected":armed["identity"], "text":multiline})
        assert fill == {"status":"filled", "sha256":sha256(multiline.encode("utf-8")).hexdigest(),
                        "bytes":len(multiline.encode("utf-8"))}
        report["session"] = {"eof_left_editor_empty":True, "changed_target_refused":True,
                             "worker_pid":same_worker.pid, "arm_ms":round((arm_at-start)*1000, 2),
                             "fill_after_arm_ms":round((perf_counter()-arm_at)*1000, 2), "verified":fill}
        await finish_session(same_worker)

        # The real WPF provider writes, but this test-only editor simulates an app rewriting its draft.
        with TemporaryDirectory(prefix="moss-uia-draft-") as scratch:
            script = Path(scratch) / "altered-draft-editor.ps1"
            base = (ROOT / "jev-draft-editor.ps1").read_text(encoding="utf-8-sig")
            hook = """$editor.Add_TextChanged({
    if ($editor.Text.Length -gt 0 -and $editor.Text -cne 'fixture rewrote the draft') {
        $editor.Text = 'fixture rewrote the draft'
    }
})
"""
            slot = "$window.Content = $panel"
            assert base.count(slot) == 1
            script.write_text(base.replace(slot, hook + slot), encoding="utf-8-sig")
            altered, altered_child = await open_target(children, script=script)
            try:
                altered_identity = (await command("rewrite fixture armed", "armed", "arm", altered))["identity"]
                await command("provider rewrote text after write", "uncertain", "fill", altered,
                              reason="readback_mismatch", expected=altered_identity, text=text)
                await command("unverified write cannot overwrite existing text", "refused", "fill", altered,
                              reason="input_changed", expected=altered_identity, text=text)
            finally:
                await stop(altered_child)
        report["passed"] = True
    finally:
        await asyncio.gather(*(stop(child) for child in children))
        folder = ROOT / "runs" / datetime.now().strftime("jev-draft-native-%Y%m%d-%H%M%S-%f")
        folder.mkdir(parents=True)
        result = folder / "result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result, report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(verify_native())

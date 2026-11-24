import asyncio
from contextlib import contextmanager, ExitStack
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from time import monotonic, sleep


ROOT = Path(__file__).resolve().parent
MAPPING = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$remote = $env:MOSS_UNC_REMOTE
try {
    switch ($env:MOSS_UNC_OPERATION) {
        'get' {
            try {
                $item = @(Get-SmbMapping -RemotePath $remote -ErrorAction Stop)
            } catch {
                if ($_.FullyQualifiedErrorId -notmatch 'NotFound') { throw }
                $item = @()
            }
            $result = @{success=$true; present=($item.Count -gt 0)}
        }
        'new' {
            $item = New-SmbMapping -RemotePath $remote -LocalPath $env:MOSS_UNC_DRIVE -TcpPort ([System.UInt16]$env:MOSS_UNC_PORT) -Persistent $false -UserName $env:MOSS_UNC_USER -Password $env:MOSS_UNC_PASSWORD
            $result = @{success=$true; status=[string]$item.Status; path=[string]$item.RemotePath}
        }
        'remove' {
            Remove-SmbMapping -RemotePath $remote -Force
            $result = @{success=$true}
        }
    }
} catch {
    $result = @{success=$false; error_id=$_.FullyQualifiedErrorId; hresult=$_.Exception.HResult; message=$_.Exception.Message}
}
$result | ConvertTo-Json -Compress
"""


def mapping(operation, env):
    result = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                             "-NoProfile", "-NonInteractive", "-Command", MAPPING], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60, check=True,
                            env={**env, "MOSS_UNC_OPERATION":operation}, creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(result.stdout.replace(env["MOSS_UNC_PASSWORD"], "[已隐藏]"))


def net_use(env, *, remove=False):
    args = [str(Path(os.environ["SystemRoot"]) / "System32/net.exe"), "use", env["MOSS_UNC_REMOTE"]]
    if remove:
        args += ["/delete", "/y"]
    else:
        args += ["*", f"/USER:{env['MOSS_UNC_USER']}", "/TRANSPORT:TCP", f"/TCPPORT:{env['MOSS_UNC_PORT']}"]
    result = subprocess.run(args, input=None if remove else (env["MOSS_UNC_PASSWORD"] + "\r\n").encode("ascii"),
                            capture_output=True, timeout=65, creationflags=subprocess.CREATE_NO_WINDOW)
    from ctypes import windll
    raw = (result.stdout + result.stderr).replace(env["MOSS_UNC_PASSWORD"].encode("ascii"), b"[redacted]")
    try:
        output = raw.decode("utf-8")
    except UnicodeDecodeError:
        output = raw.decode(f"cp{windll.kernel32.GetOEMCP()}", errors="replace")
    code = re.search(r"(?:system error\s*|系统错误\s*)(\d+)", output, re.I)
    return {"success":result.returncode == 0, "exit":result.returncode, "system_error":int(code[1]) if code else None}


def share_server(folder):
    from impacket.ntlm import compute_lmhash, compute_nthash
    from impacket.smbserver import SimpleSMBServer, SMBSERVER

    host = os.environ["MOSS_UNC_HOST"]
    assert ipaddress.ip_address(host).is_loopback
    class CountedServer(SMBSERVER):
        def get_request(self):
            connection = super().get_request()
            with (folder / "accepted.txt").open("a", encoding="utf-8") as record:
                record.write("1\n")
            return connection

        def processRequest(self, connId, data):
            hold = folder / "pause-smb"
            if hold.exists():
                (folder / "paused-smb").touch()
                deadline = monotonic() + 20
                while hold.exists() and monotonic() < deadline:
                    sleep(.02)
            return super().processRequest(connId, data)

    server = SimpleSMBServer(listenAddress=host, listenPort=int(os.environ["MOSS_UNC_BIND_PORT"]), smbserverclass=CountedServer)
    server.setSMB2Support(True)
    server.addShare(os.environ["MOSS_UNC_SHARE"], str(folder / "share"), readOnly="yes")
    password = os.environ["MOSS_UNC_PASSWORD"]
    server.addCredential(os.environ["MOSS_UNC_USER"], 0, compute_lmhash(password), compute_nthash(password))
    port = server.getServer().server_address[1]
    Path(os.environ["MOSS_UNC_READY"]).write_text(json.dumps({"port":port}), encoding="utf-8")
    server.start()


@contextmanager
def running_share(folder, env, port=0):
    python = ROOT.parent / "alternatives/hiroute/media_library/.venv/Scripts/python.exe"
    ready = folder / f"ready-{secrets.token_hex(4)}.json"
    child_env = {**env, "MOSS_UNC_READY":str(ready), "MOSS_UNC_BIND_PORT":str(port)}
    child = subprocess.Popen([str(python), str(Path(__file__).resolve()), "--share", str(folder)], env=child_env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = monotonic() + 12
        while not ready.exists():
            if child.poll() is not None or monotonic() > deadline:
                raise RuntimeError("隔离 SMB 服务未能启动。")
            sleep(.05)
        yield json.loads(ready.read_text(encoding="utf-8"))["port"]
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(10)


def share_snapshot(root):
    entries = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[name] = None
        else:
            with path.open("rb") as stream:
                entries[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return entries


def share_evidence(entries):
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"sha256":hashlib.sha256(payload).hexdigest(), "files":sum(value is not None for value in entries.values()),
            "directories":sum(value is None for value in entries.values())}


async def verify_file_plan(folder, remote, expected, ask, report):
    from file_organization import PREVIEW_LIMIT

    before = share_snapshot(folder / "share")
    assert before.keys() == {"书架", *(f"书架/{name}" for name in expected)}
    details = {"before":share_evidence(before), "queries":[]}
    report["file_organization"] = details
    location = remote + r"\书架"
    for text in ("按类型整理这个目录", f"按类型整理 {location}，先给我看计划"):
        turn = await ask(text)
        plan = turn["file_plan"]
        assert plan["status"] == "preview" and Path(plan["path"]) == Path(location), plan
        assert plan["scanned"] == plan["suggested"] == len(expected), plan
        assert plan["conflicts"] == 0 and not plan["limited"] and not plan["skipped"], plan
        assert plan["omitted"] == len(expected) - PREVIEW_LIMIT, plan
        assert plan["items"] == [
            {"name":name, "category":"文档", "destination":str(Path("文档") / name), "status":"suggested"}
            for name in expected[:PREVIEW_LIMIT]
        ], plan
        assert f"建议分类 {len(expected)} 项" in turn["reply"] and f"另有 {plan['omitted']} 项" in turn["reply"]
        assert turn["cloud_requests"] == {"total":0, "by_provider":{}, "complete":True}
        details["queries"].append({"text":text, "plan":plan, "cloud_requests":turn["cloud_requests"]})
    after = share_snapshot(folder / "share")
    details["after"] = share_evidence(after)
    details["fixture_unchanged"] = before == after
    assert details["fixture_unchanged"]


async def verify_cancelled_reads(folder, remote, server, report, ask, expected):
    import psutil
    from directory_tools import DIRECTORY_READ_SECONDS

    hold = folder / "pause-smb"
    paused = folder / "paused-smb"
    details = report["directory_cancellation"] = {"cases": []}
    labels = ("列目录", "整理预览", "服务退出")
    for label in labels:
        target = folder / "share" / label
        target.mkdir()
        (target / "隔离文档.txt").write_text(label, encoding="utf-8")
    before = share_snapshot(folder / "share")

    async def blocked(text):
        paused.unlink(missing_ok=True)
        hold.touch()
        code, state = await server.post("/api/message", {"text":text})
        assert code == 200, (code, state)
        turn = state["turns"][-1]
        assert turn["status"] == "thinking" and turn["source"] == "context"
        async with asyncio.timeout(5):
            while not paused.exists():
                await asyncio.sleep(.02)
        workers = []
        for proc in psutil.Process(server.pid).children(recursive=True):
            try:
                if any(Path(arg).name == "directory_tools.py" for arg in proc.cmdline()[1:]):
                    workers.append(proc)
            except psutil.NoSuchProcess:
                continue
        assert workers, "未发现正在等待 SMB 回应的目录读取进程。"
        assert (await server.state())["turns"][-1]["status"] == "thinking"
        return turn["id"], workers

    async def record_exit(case, workers):
        started = monotonic()
        async with asyncio.timeout(5):
            while any(proc.is_running() for proc in workers):
                await asyncio.sleep(.02)
        case["worker_exit_after_smb_release_ms"] = round((monotonic() - started) * 1000, 1)

    first = await ask(f"列出目录 {remote}\\书架")
    # Wait out the Windows SMB directory metadata cache so the next page reaches the fixture.
    await asyncio.sleep(12)
    temporary_files = set(server.data.glob("tmp*"))
    cases = report["directory_timeout"] = []
    for text, cursor in (("下一页", first["directory_page"]),
                         (f"按类型整理 {remote}\\整理预览", None)):
        started = monotonic()
        try:
            turn_id, workers = await blocked(text)
            async with asyncio.timeout(DIRECTORY_READ_SECONDS + 2):
                turn = (await server.state())["turns"][-1]
                while turn["status"] == "thinking":
                    await asyncio.sleep(.02)
                    turn = (await server.state())["turns"][-1]
            elapsed = (monotonic() - started) * 1000
            assert turn["id"] == turn_id and turn["status"] == "replied", turn
            assert "读取共享目录超时" in turn["reply"] and hold.exists(), turn
            assert DIRECTORY_READ_SECONDS * 1000 - 50 <= elapsed < (DIRECTORY_READ_SECONDS + 2) * 1000, elapsed
            assert (turn.get("directory_page") or None) == cursor and not turn.get("file_plan"), turn
            case = {"text":text, "timeout_ms":round(elapsed, 1), "cursor_retained":cursor is not None,
                    "workers_pending_during_smb_pause":sum(proc.is_running() for proc in workers),
                    "smb_still_paused":True}
            cases.append(case)
            if cursor is None:
                started = monotonic()
                async with asyncio.timeout(2):
                    clock = await ask("现在几点", source=None)
                assert hold.exists() and re.search(r"\d\d:\d\d:\d\d", clock["reply"]), clock
                assert clock["cloud_requests"]["total"] == 0, clock
                case["next_reply_ms"] = round((monotonic() - started) * 1000, 1)
        finally:
            hold.unlink(missing_ok=True)
        await record_exit(case, workers)
        if cursor is not None:
            resumed = await ask("下一页")
            assert resumed["reply"].splitlines()[1:] == expected[40:], resumed
            case["continued_after_release"] = True
        latest = next(row for row in (await server.state())["turns"] if row["id"] == turn_id)
        assert latest == turn, "已超时的目录轮次被迟到结果改写。"
        case["late_result_ignored"] = True
        assert set(server.data.glob("tmp*")) == temporary_files, "目录子进程的临时读写文件未释放。"
        case["temporary_files_released"] = True

    for label, command in zip(labels[:2], ("列出目录", "按类型整理")):
        try:
            turn_id, workers = await blocked(f"{command} {remote}\\{label}")
            started = monotonic()
            async with asyncio.timeout(2):
                code, state = await server.post("/api/cancel", {"turn_id":turn_id})
            cancelled_ms = round((monotonic() - started) * 1000, 1)
            turn = state["turns"][-1]
            assert code == 200 and turn["status"] == "cancelled" and turn["id"] == turn_id, (code, turn)
            assert not turn.get("directory_page") and not turn.get("file_plan")
            assert hold.exists()
            pending = sum(proc.is_running() for proc in workers)

            started = monotonic()
            async with asyncio.timeout(2):
                status, state = await server.post("/api/message", {"text":"现在几点"})
                assert status == 200, (status, state)
                reply = state["turns"][-1]
                while reply["status"] == "thinking":
                    await asyncio.sleep(.02)
                    reply = (await server.state())["turns"][-1]
            next_ms = round((monotonic() - started) * 1000, 1)
            assert reply["status"] == "replied" and re.search(r"\d\d:\d\d:\d\d", reply["reply"]), reply
            case = {"action":label, "cancellation_ms":cancelled_ms, "next_reply_ms":next_ms,
                    "workers_pending_during_smb_pause":pending, "smb_still_paused":hold.exists()}
            details["cases"].append(case)
        finally:
            hold.unlink(missing_ok=True)
        await record_exit(case, workers)

    try:
        turn_id, workers = await blocked(f"列出目录 {remote}\\{labels[2]}")
        app = psutil.Process(server.pid)
        started = monotonic()
        async with asyncio.timeout(3):
            await server.stop()
        elapsed = round((monotonic() - started) * 1000, 1)
        assert not app.is_running() and hold.exists()
        case = {"action":labels[2], "service_stop_ms":elapsed, "smb_still_paused":True,
                "workers_pending_during_smb_pause":sum(proc.is_running() for proc in workers)}
        details["cases"].append(case)
    finally:
        hold.unlink(missing_ok=True)
    await record_exit(case, workers)
    await server.start()
    turns = (await server.state())["turns"]
    interrupted = next(turn for turn in turns if turn["id"] == turn_id)
    assert interrupted["status"] in {"cancelled", "error"} and not interrupted.get("directory_page")
    assert all(not turn.get("directory_page") and not turn.get("file_plan")
               for turn in turns if turn["status"] == "cancelled")
    details["interrupted_status_after_restart"] = interrupted["status"]
    details["fixture_unchanged"] = share_snapshot(folder / "share") == before
    assert details["fixture_unchanged"]


async def test_chat(folder, remote, expected, report, disconnect, reconnect):
    from local_media.library import MediaLibrary
    from verify_configured_mcp import isolated

    catalog = folder / "empty.sqlite3"
    MediaLibrary(catalog)
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_PROVIDER_DIR=str(folder / "providers"),
                      MOSS_MEDIA_DB=str(catalog), MOSS_MEM0_ENABLED="0", MOSS_SPEECH_PREPARE="0",
                      MOSS_SPEECH_STANDBY="0", LOCALAPPDATA=str(folder / "profile"))
    report["model_connections"] = 0

    async def audit(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(audit, "127.0.0.1", 0) as gate:
        server = isolated(folder)
        server.model_url = f"http://127.0.0.1:{gate.sockets[0].getsockname()[1]}/v1"

        async def ask(text, *, source="context"):
            started = monotonic()
            status, state = await server.post("/api/message", {"text":text})
            assert status == 200
            turn = state["turns"][-1]
            async with asyncio.timeout(100):
                while turn["status"] == "thinking":
                    await asyncio.sleep(.05)
                    turn = (await server.state())["turns"][-1]
            report["cases"].append({"text":text, "reply":turn["reply"], "status":turn["status"], "source":turn.get("source"),
                                    "ms":round((monotonic() - started) * 1000)})
            assert turn["status"] == "replied" and (source is None or turn["source"] == source)
            return turn

        try:
            await server.start()
            for path in report["literal_paths"]["rejected"]:
                denied = await ask("列出目录 " + path)
                assert "设备路径和通配符" in denied["reply"] and not denied["directory_page"]
            first = await ask(f"列出目录 {remote}\\书架")
            assert first["reply"].splitlines()[1:] == expected[:40]
            report["identity"] = first["directory_page"]["identity"]
            await server.stop()
            await server.start()
            second = await ask("还有哪些？")
            assert second["reply"].splitlines()[1:] == expected[40:]
            assert "已列完" in (await ask("下一页"))["reply"]
            await verify_file_plan(folder, remote, expected, ask, report)
            if "--outage" in sys.argv:
                first = await ask(f"列出目录 {remote}\\书架")
                await asyncio.to_thread(disconnect)
                try:
                    await asyncio.sleep(12)
                    offline = await ask("下一页")
                    report["offline"] = {"retained":first["directory_page"] == offline.get("directory_page")}
                    try:
                        await asyncio.to_thread(Path(remote, "书架").resolve, strict=True)
                    except OSError as exc:
                        report["offline"]["native_error"] = exc.winerror
                finally:
                    await asyncio.to_thread(reconnect)
                restored_at = monotonic()
                report["manual_retry_seconds"] = []
                for second in (0, 12, 67):
                    await asyncio.sleep(max(0, restored_at + second - monotonic()))
                    report["manual_retry_seconds"].append(round(monotonic() - restored_at, 2))
                    resumed = await ask("下一页")
                    if resumed.get("directory_page") != first["directory_page"]:
                        break
                report["continued_after_reconnect"] = resumed["reply"].splitlines()[1:] == expected[40:]
                assert report["offline"]["retained"] and report["continued_after_reconnect"]
            if "--cancel" in sys.argv:
                await verify_cancelled_reads(folder, remote, server, report, ask, expected)
            await ask(f"列出目录 {remote}\\书架")
            (folder / "share/书架/新增条目.txt").touch()
            await asyncio.sleep(12)
            changed = await ask("下一页")
            assert "已变化" in changed["reply"] and not changed["directory_page"]
            assert report["model_connections"] == 0
            report["passed"] = True
        finally:
            await server.stop()


def run():
    import smbclient
    from ctypes import windll
    from context_tools import parse_context

    folder = ROOT / "runs" / datetime.now().strftime("native-unc-directory-%Y%m%d-%H%M%S-%f")
    items = folder / "share/书架"
    items.mkdir(parents=True)
    expected = [f"书目-{i:02d}.txt" for i in range(48)]
    for name in reversed(expected):
        (items / name).write_text(f"隔离共享测试：{name}\n", encoding="utf-8")
    host = "127.0.0.1"
    if "--random-loopback" in sys.argv:
        host = f"127.{30 + secrets.randbelow(190)}.{1 + secrets.randbelow(249)}.{2 + secrets.randbelow(247)}"
    tag = "MOSSQA" + secrets.token_hex(5).upper()
    remote = rf"\\{host}\{tag}"
    env = {**os.environ, "MOSS_UNC_HOST":host, "MOSS_UNC_SHARE":tag, "MOSS_UNC_REMOTE":remote,
           "MOSS_UNC_USER":"qa" + secrets.token_hex(4), "MOSS_UNC_PASSWORD":secrets.token_hex(18)}
    drives = windll.kernel32.GetLogicalDrives()
    free_letter = next(chr(65 + i) for i in range(25, 15, -1) if not drives & (1 << i))
    env["MOSS_UNC_DRIVE"] = free_letter + ":"
    report = {"passed":False, "scope":"隔离 SMB2 只读目录、Windows 原生客户端、自定义回环端口。", "remote":remote,
              "inherited_admin_token":bool(windll.shell32.IsUserAnAdmin()), "temporary_drive":env["MOSS_UNC_DRIVE"], "cases":[]}
    accepted = [r"\\127.0.0.1\隔离资料", r"\\library.example.test\只读资料", r"\\LIBRARY\资料"]
    rejected = [r"\\?\UNC\host\share", r"\\.\PIPE\demo", r"\\..\share", r"\\host\*.txt", r"\\ho?st\share"]
    assert all(parse_context("列出目录 " + path) == {"action":"directory", "path":path} for path in accepted)
    assert all(parse_context("列出目录 " + path) == {"action":"directory", "invalid":True} for path in rejected)
    report["literal_paths"] = {"accepted":accepted, "rejected":rejected}
    try:
        with ExitStack() as shares:
            port = shares.enter_context(running_share(folder, env))
            env["MOSS_UNC_PORT"] = str(port)
            report["port"] = port
            smbclient.register_session(host, username=env["MOSS_UNC_USER"], password=env["MOSS_UNC_PASSWORD"], port=port)
            try:
                assert sorted(smbclient.listdir(remote + r"\书架", port=port)) == expected
                report["smb2_control_entries"] = len(expected)
            finally:
                smbclient.delete_session(host, port=port)
            before = report["before"] = mapping("get", env)
            assert not before.get("present"), "测试目标在创建前已有映射。"
            mapped = {"success":False}
            accepts = folder / "accepted.txt"
            report["connections_before_mapping"] = len(accepts.read_text(encoding="utf-8").splitlines())
            try:
                mapped = report["new_smb_mapping"] = mapping("new", env)
                if not mapped["success"]:
                    mapped = report["net_use"] = net_use(env)
                if mapped["success"]:
                    def reconnect():
                        assert shares.enter_context(running_share(folder, env, port)) == port
                        smbclient.register_session(host, username=env["MOSS_UNC_USER"], password=env["MOSS_UNC_PASSWORD"], port=port)
                        try:
                            report["restored_smb2_entries"] = len(smbclient.listdir(remote + r"\书架", port=port))
                            assert report["restored_smb2_entries"] == len(expected)
                        finally:
                            smbclient.delete_session(host, port=port)

                    asyncio.run(test_chat(folder, remote, expected, report, shares.close, reconnect))
            finally:
                after = report["after_test"] = mapping("get", env)
                if after.get("present") or mapped["success"]:
                    removed = report["remove"] = mapping("remove", env)
                    if not removed["success"]:
                        report["remove_net_use"] = net_use(env, remove=True)
                report["after_cleanup"] = mapping("get", env)
                report["connections_after_mapping"] = len(accepts.read_text(encoding="utf-8").splitlines())
                if not report["after_cleanup"].get("success") or report["after_cleanup"].get("present"):
                    report["passed"] = False
                    raise RuntimeError("未确认本轮临时映射已移除。")
    finally:
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, report["passed"], flush=True)
    return report["passed"]


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--share":
        share_server(Path(sys.argv[2]))
    else:
        sys.exit(0 if run() else 1)

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from time import perf_counter

import aiohttp


ROOT = Path(__file__).resolve().parent


async def confirm_muted_edge(browser=None):
    if browser is not None:
        session = await browser.new_browser_cdp_session()
        try:
            info = await session.send("SystemInfo.getProcessInfo")
            edge_pid = next(item["id"] for item in info["processInfo"] if item["type"] == "browser")
        finally:
            await session.detach()
        command = f"$edge=Get-CimInstance Win32_Process -Filter 'ProcessId={int(edge_pid)}'\n" + (
            "if($edge.Name -ne 'msedge.exe' -or $edge.CommandLine -notmatch '--mute-audio(?:\\s|$)'){exit 2}")
    else:
        command = r"""$all=@(Get-CimInstance Win32_Process)
$parents=@(TASK_PROCESS_ID)
$edge=@()
while($parents.Count -gt 0){
  $children=@($all | Where-Object {$parents -contains $_.ParentProcessId})
  $edge+=@($children | Where-Object {$_.Name -eq 'msedge.exe' -and $_.CommandLine -notmatch '--type='})
  $parents=@($children | ForEach-Object {$_.ProcessId})
}
if($edge.Count -ne 1 -or $edge[0].CommandLine -notmatch '--mute-audio(?:\s|$)'){exit 2}
""".replace("TASK_PROCESS_ID",str(os.getpid()))
    proc = await asyncio.create_subprocess_exec("powershell.exe","-NoProfile","-NonInteractive","-Command",command,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
    if await proc.wait() != 0:
        raise RuntimeError("未确认本轮 Edge 启用了 --mute-audio。")


async def open_browser(playwright):
    edge = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe"
    return await playwright.chromium.launch(executable_path=str(edge), headless=True,
                                            args=["--mute-audio","--disable-background-timer-throttling",
                                                  "--disable-renderer-backgrounding"])


class BrowserServer:
    def __init__(self, folder, port, write_delay=0, model_url="http://127.0.0.1:18222/v1",
                 model="moss-chat-qwen35-4b", memory_url=None, *, draft_model_reply=False):
        self.folder = Path(folder).resolve()
        self.data = self.folder / "data"
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.delay = write_delay
        self.model_url = model_url
        self.model = model
        self.memory_url = memory_url
        self.draft_model_reply = draft_model_reply
        self.proc = None
        self.pid = None
        self.starts = 0

    async def start(self):
        if self.proc is not None:
            raise RuntimeError("测试页面服务已经启动")
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", self.port))
        self.starts += 1
        self.folder.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-u", str(ROOT / "serve.py"), "--port", str(self.port),
               "--model-url", self.model_url, "--model", self.model, "--data", str(self.data),
               "--write-delay", str(self.delay)]
        if self.memory_url:
            cmd.extend(["--memory-url", self.memory_url])
        if self.draft_model_reply:
            cmd.append("--draft-model-reply")
        log_path = self.folder / f"web-{self.starts}.log"
        with log_path.open("ab") as log:
            self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
        start = perf_counter()
        while perf_counter() - start < 60:
            if self.proc.poll() is not None:
                raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.get(self.url + "/api/health") as response:
                        health = await response.json()
                owner = json.loads((self.data / "instance.json").read_text(encoding="utf-8"))
                if health["app"] == "moss-local-chat" and health["pid"] == owner["pid"]:
                    self.pid = health["pid"]
                    return self
            except (aiohttp.ClientConnectionError, FileNotFoundError):
                pass
            await asyncio.sleep(.1)
        raise TimeoutError(f"启动页面超时 {log_path}")

    async def state(self):
        async with aiohttp.ClientSession() as client:
            async with client.get(self.url + "/api/state") as response:
                response.raise_for_status()
                return await response.json()

    async def post(self, path, body):
        async with aiohttp.ClientSession() as client:
            async with client.post(self.url + path, json=body) as response:
                return response.status, await response.json()

    async def stop(self, hard=False):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            owner = json.loads((self.data / "instance.json").read_text(encoding="utf-8"))
            if owner["pid"] != self.pid or owner["port"] != self.port:
                raise RuntimeError("测试进程身份不符")
            if hard:
                os.kill(self.pid, signal.SIGTERM)
            else:
                status, _ = await self.post("/api/shutdown", {"token": owner["token"]})
                if status != 200:
                    raise RuntimeError("测试服务拒绝关闭")
            await asyncio.to_thread(self.proc.wait, 20)
        self.proc = None

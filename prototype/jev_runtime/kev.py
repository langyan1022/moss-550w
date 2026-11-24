import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import isfinite
import os
from pathlib import Path
import secrets
import socket
import subprocess

import aiohttp
import win32api
import win32con
import win32job


LM = Path.home() / ".lmstudio"
ENGINE = LM / "extensions/backends/llama.cpp-win-x86_64-avx2-2.69.0/llama-server.exe"
WEIGHTS = LM / "models/ggml-org/Kev-0.8B-GGUF/Kev-0.8B-BF16.gguf"
MODEL_ID = "kev-0.8b"


@dataclass
class Kev:
    session: aiohttp.ClientSession
    url: str
    name: str
    shared: bool
    headers: dict

    async def ask(self, state, questions):
        async with self.session.post(self.url + "/v1/systemone", json={"model": self.name, "state": state,
                                     "questions": questions}, headers=self.headers) as response:
            if response.status != 200:
                raise ValueError(f"本地 Kev 判题失败（HTTP {response.status}）。")
            result = await response.json()
        answers = result.get("answers")
        if (result.get("model") != self.name or not isinstance(answers, dict)
                or set(answers) != set(questions) or result.get("usage", {}).get("output_tokens") != 0):
            raise ValueError("本地 Kev 返回的判题记录不完整。")
        for key, question in questions.items():
            answer = answers[key]
            if not isinstance(answer, dict) or answer.get("type") != question["type"]:
                raise ValueError("本地 Kev 返回的题型不符。")
            if question["type"] == "choice":
                scores = answer.get("probabilities")
                if (answer.get("choice") not in question["criteria"] or not isinstance(scores, dict)
                        or set(scores) != set(question["criteria"])
                        or any(type(p) not in (float, int) or not isfinite(p) or p < 0 for p in scores.values())
                        or abs(sum(scores.values()) - 1) > .001):
                    raise ValueError("本地 Kev 返回的选项或分值无效。")
        return answers


async def _find_model(session, url, headers):
    try:
        async with session.get(url + "/v1/models", headers=headers, timeout=aiohttp.ClientTimeout(total=1)) as response:
            if response.status != 200:
                return None
            listing = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    if not isinstance(listing, dict):
        return None
    return next((row["id"] for row in listing.get("data", []) if isinstance(row, dict) and row.get("id") == MODEL_ID
                 and "decisions" in row.get("architecture", {}).get("output_modalities", [])), None)


@asynccontextmanager
async def decision_service(*, reuse=None):
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=35)) as client:
        shared = "http://127.0.0.1:18323"
        if reuse is None:
            reuse = os.environ.get("MOSS_KEV_REUSE") == "1" or not (ENGINE.is_file() and WEIGHTS.is_file())
        if reuse and await _find_model(client, shared, {}):
            yield Kev(client, shared, MODEL_ID, True, {})
            return
        if not ENGINE.is_file() or not WEIGHTS.is_file():
            raise ValueError("这台电脑尚未就绪 Kev-0.8B 判题服务或对应的本机模型。")
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        key = secrets.token_urlsafe(28)
        headers = {"Authorization": "Bearer " + key}
        group = win32job.CreateJobObject(None, "MossKev-" + secrets.token_hex(16))
        proc = None
        try:
            limits = win32job.QueryInformationJobObject(group, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(group, win32job.JobObjectExtendedLimitInformation, limits)
            proc = subprocess.Popen([str(ENGINE), "-m", str(WEIGHTS), "--alias", MODEL_ID, "--host", "127.0.0.1",
                                     "--port", str(port), "--api-key", key, "--parallel", "1", "--ctx-size", "4096",
                                     "--threads", "6", "--n-gpu-layers", "0"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
            try:
                win32job.AssignProcessToJobObject(group, handle)
            finally:
                handle.Close()
            async with asyncio.timeout(45):
                while not await _find_model(client, url, headers):
                    if proc.poll() is not None:
                        raise ValueError("本地 Kev 推理服务未能启动。")
                    await asyncio.sleep(.15)
            yield Kev(client, url, MODEL_ID, False, headers)
        finally:
            group.Close()
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                await asyncio.to_thread(proc.wait, 5)

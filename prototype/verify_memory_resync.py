import asyncio
from datetime import datetime
import json
import shutil
import sqlite3
import subprocess

from aiohttp import ClientSession, web

from browser_lab import BrowserServer, ROOT


async def run():
    archive = ROOT / "runs/memory-service-20261120-115425"
    assert json.loads((archive / "result.json").read_text(encoding="utf-8"))["passed"]
    folder = ROOT / "runs" / ("memory-resync-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    data = folder / "data"
    data.mkdir(parents=True)
    shutil.copyfile(archive / "data/session.sqlite3", data / "session.sqlite3")
    index = folder / "index"
    shutil.copytree(archive / "index", index, ignore=shutil.ignore_patterns("*.log", ".lock", "instance.json"))
    points = next((index / "qdrant").rglob("storage.sqlite"))

    def count_points():
        with sqlite3.connect(points.resolve().as_uri() + "?mode=ro", uri=True) as db:
            return db.execute("SELECT count(*) FROM points").fetchone()[0]

    count_before = count_points()
    calls = []
    async def reject(request):
        calls.append(request.path)
        return web.json_response({"error": "此端口拒绝推理"}, status=503)

    listener = web.Application()
    listener.router.add_post("/{path:.*}", reject)
    runner = web.AppRunner(listener)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    inference_url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1"
    bridge_url = "http://127.0.0.1:18785"
    server = BrowserServer(folder, 18784, model_url=inference_url, memory_url=bridge_url)
    bridge = None
    owner_file = index / "instance.json"
    try:
        await server.start()
        first_pid = server.pid
        state = await server.state()
        assert state["model_id"] == "local" and len(state["memories"]) == count_before == 3
        removed = state["memories"][0]
        code, state = await server.post("/api/forget", {"id": removed["id"]})
        assert code == 200 and state["memory_index_error"] and len(state["memories"]) == 2
        await server.stop()
        assert count_points() == count_before
        mem0 = ROOT.parent / "alternatives/mem0"
        with (folder / "mem0.log").open("wb") as log:
            bridge = subprocess.Popen([str(mem0 / ".venv/Scripts/python.exe"), "-u", str(mem0 / "bridge.py"),
                                       "--data", str(index), "--port", "18785", "--embedding-url", inference_url,
                                       "--chat-url", inference_url], cwd=mem0, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        async with asyncio.timeout(40):
            while not owner_file.exists():
                if bridge.poll() is not None:
                    raise RuntimeError((folder / "mem0.log").read_text(encoding="utf-8", errors="replace"))
                await asyncio.sleep(.1)
        owner = json.loads(owner_file.read_text(encoding="utf-8"))
        async with ClientSession() as client:
            async with client.get(bridge_url + "/health") as response:
                health = await response.json()
        assert owner["pid"] == health["pid"] and health["app"] == "moss-mem0" and owner["port"] == 18785
        assert health["embedding_url"] == inference_url
        await server.start()
        second_pid = server.pid
        code, state = await server.post("/api/message", {"text": "解释为什么石墨能导电。"})
        assert code == 200
        async with asyncio.timeout(20):
            while state["turns"][-1]["status"] == "thinking":
                await asyncio.sleep(.05)
                state = await server.state()
        assert state["turns"][-1]["status"] == "error" and not state["memory_index_error"]
        assert not state["turns"][-1].get("memory_ids")
        assert "/v1/chat/completions" in calls and "/v1/embeddings" not in calls
        async with asyncio.timeout(12):
            while count_points() != count_before - 1:
                await asyncio.sleep(.05)
    finally:
        try:
            await server.stop()
            if bridge and bridge.poll() is None:
                if owner_file.exists():
                    owner = json.loads(owner_file.read_text(encoding="utf-8"))
                    async with ClientSession() as client:
                        async with client.post(bridge_url + "/shutdown", json={"token": owner["token"]}) as response:
                            response.raise_for_status()
                else:
                    bridge.terminate()
                await asyncio.to_thread(bridge.wait, 15)
        finally:
            if bridge and bridge.poll() is None:
                bridge.kill()
                await asyncio.to_thread(bridge.wait, 5)
            await runner.cleanup()
    count_after = count_points()
    assert count_after == count_before - 1
    with sqlite3.connect((index / "history.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT event FROM history WHERE old_memory=? OR new_memory=? ORDER BY rowid",
                          (removed["text"], removed["text"])).fetchall()
    assert rows == [("ADD",), ("DELETE",)]
    result = {"passed": True, "source": archive.name, "app_pids": [first_pid, second_pid], "mem0_pid": health["pid"],
              "qdrant_points_before": count_before, "qdrant_points_after": count_after,
              "removed_text": removed["text"], "retained_mem0_history": [row[0] for row in rows],
              "embedding_requests": 0, "model_generations": 0}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(folder)


if __name__ == "__main__":
    asyncio.run(run())

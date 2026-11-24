import argparse
import asyncio
from datetime import datetime
import json
import math
from statistics import median
from time import perf_counter

import aiohttp

from browser_lab import BrowserServer, ROOT


def summarize(samples):
    return {"n": len(samples), "median_ms": round(median(samples), 2),
            "p95_ms": round(sorted(samples)[math.ceil(len(samples) * .95) - 1], 2)}


async def main(label):
    folder = ROOT / "runs" / datetime.now().strftime(f"history-backend-{label}-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18953, model_url="http://127.0.0.1:18954/v1")
    await server.start()
    try:
        async with aiohttp.ClientSession(base_url=server.url) as http:
            async def post(path, body):
                async with http.post(path, json=body) as response:
                    response.raise_for_status()
                    return await response.json()

            async def ask(question, chat_id):
                started = perf_counter()
                state = await post("/api/message", {"chat_id": chat_id, "text": question})
                accepted = (perf_counter() - started) * 1000
                async with asyncio.timeout(8):
                    while state["turns"][-1]["status"] == "thinking":
                        await asyncio.sleep(.003)
                        async with http.get("/api/state") as response:
                            response.raise_for_status()
                            state = await response.json()
                elapsed = (perf_counter() - started) * 1000
                last = state["turns"][-1]
                assert last["status"] == "replied" and last["reply"] and last["user"] == question, last.get("error")
                assert state["chat_id"] == chat_id
                return last, accepted, elapsed

            await post("/api/select-model", {"id": "local"})
            chat_id = (await server.state())["chat_id"]
            seeds = ["在吗", "谢谢", "今天星期几？", "你现在有哪些工具？"]
            for index in range(160):
                await ask(seeds[index % len(seeds)], chat_id)
            probes = {"在吗": [], "你现在有哪些工具？": []}
            for _ in range(18):
                for question, samples in probes.items():
                    last, accepted, elapsed = await ask(question, chat_id)
                    if question == "在吗":
                        assert last["reply"] == "我在。"
                    else:
                        assert last["holo_tools"] == ["list_capabilities"]
                    samples.append({"accepted_ms": round(accepted, 3), "replied_ms": round(elapsed, 3)})
            final = await server.state()
            result = {"seed_turns": 160, "final_turns": len(final["turns"]), "measure": "HTTP request through decoded state, 3 ms polling when needed",
                      "conditions": "isolated SQLite, localhost, no browser, model or speech", "samples": probes,
                      "summary": {q: {key: summarize([row[key] for row in rows]) for key in rows[0]}
                                  for q, rows in probes.items()}}
            assert result["final_turns"] == 196
            (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(folder, json.dumps(result["summary"], ensure_ascii=False))
    finally:
        await server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("label", choices=("before", "after", "check"))
    asyncio.run(main(parser.parse_args().label))

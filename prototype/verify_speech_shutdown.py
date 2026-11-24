import asyncio
from datetime import datetime
import json
from time import perf_counter

import aiohttp

from browser_lab import BrowserServer, ROOT


async def main():
    folder = ROOT / "runs" / datetime.now().strftime("speech-shutdown-%Y%m%d-%H%M%S")
    server = BrowserServer(folder, 18379)
    await server.start()
    async with aiohttp.ClientSession() as http:
        async def request_speech():
            try:
                async with http.post(server.url+"/api/speak",json={"text":"正在准备本机语音播报。"}) as response:
                    await response.read()
                    return response.status
            except aiohttp.ClientConnectionError:
                return "连接已关闭"
        pending = asyncio.create_task(request_speech())
        await asyncio.sleep(3)
        assert not pending.done()
        owner = json.loads((server.data/"instance.json").read_text(encoding="utf-8"))
        assert owner["pid"] == server.pid
        then = perf_counter()
        status, _ = await server.post("/api/shutdown", {"token":owner["token"]})
        assert status == 200
        await asyncio.to_thread(server.proc.wait, 90)
        elapsed = perf_counter()-then
        result = {"pid":server.pid,"exit":server.proc.returncode,"elapsed":round(elapsed,3),
                  "speech_response":await pending}
        (folder/"result.json").write_text(json.dumps(result,ensure_ascii=False),encoding="utf-8")
        print(folder, result)
        assert result["exit"] == 0 and elapsed < 15


if __name__ == "__main__":
    asyncio.run(main())

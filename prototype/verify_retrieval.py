import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
from core import Moss
from memory_store import LIMITS, select_memories

ROOT = Path(__file__).resolve().parent

async def check():
    out = ROOT / "runs" / ("memory-retrieval-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    moss = Moss(out, "http://127.0.0.1:18182/v1", "local-qwen")
    oldest = "我的报销发票抬头是山茶工作室-" + secrets.token_hex(4).upper()
    key = await moss.remember(oldest)
    for i in range(1, 20):
        await moss.remember(f"第{i}项观察记录，周末例行整理本地草稿和下一周使用的个人清单。")
    all_items = moss.list_memories()
    q = "我报销时用哪个发票抬头？"
    chosen = select_memories(q, all_items)
    size = sum(len(item["text"].encode("utf-8")) for item in chosen)
    assert len(all_items) == 20 and moss.state()["memory_limits"]["used_bytes"] > 900
    assert chosen[0]["id"] == key and 0 < len(chosen) <= 12 and size <= 900
    await moss.close()
    m = Moss(out, "http://127.0.0.1:18182/v1", "local-qwen")
    assert select_memories(q, m.list_memories())[0]["id"] == key
    await m.forget(key)
    assert key not in [item["id"] for item in select_memories(q, m.list_memories())]
    await m.close()
    result = {"passed": True, "pid": os.getpid(), "model_calls": 0, "saved": len(all_items), "limits": LIMITS,
              "query": q, "earliest_id": key, "earliest_text": oldest, "selected": chosen, "context_bytes": size,
              "selected_first_after_reopen": True, "deleted_id_excluded": True}
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    asyncio.run(check())

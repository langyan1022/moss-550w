import asyncio
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout

from memory_store import LIMITS


class LocalMemoryIndex:
    def __init__(self, url, db):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("记忆服务需要使用本机 HTTP 地址")
        self.url = url.rstrip("/")
        self.lock = asyncio.Lock()
        self.error = None
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS memory_index_scope(id INTEGER PRIMARY KEY CHECK(id=1), scope TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO memory_index_scope VALUES(1,?)", (uuid4().hex,))
        self.scope, = db.execute("SELECT scope FROM memory_index_scope WHERE id=1").fetchone()

    async def request(self, operation, read_items, **extra):
        # 在锁内读取当前事实，使连续两次更改按提交后的状态同步。
        async with self.lock:
            items = [{"id": m["id"], "text": m["text"]} for m in read_items()]
            body = {"scope": self.scope, "items": items, **extra}
            try:
                async with ClientSession(timeout=ClientTimeout(total=60), trust_env=False) as client:
                    async with client.post(self.url + "/v1/" + operation, json=body, allow_redirects=False) as response:
                        if response.status != 200:
                            raise ValueError(f"记忆服务返回 HTTP {response.status}")
                        result = await response.json()
                if not isinstance(result, dict) or not isinstance(result.get("ids"), list):
                    raise ValueError("记忆服务没有返回记录 ID 列表")
                ids = result["ids"]
                if not all(isinstance(key, str) for key in ids) or len(set(ids)) != len(ids):
                    raise ValueError("记忆服务返回的记录 ID 无效")
            except (ClientError, TimeoutError, ValueError) as exc:
                self.error = f"本机语义索引未同步。{type(exc).__name__}，请检查记忆服务。"
                raise ValueError(self.error) from exc
            self.error = None
            return ids

    async def select(self, question, read_items):
        ids = await self.request("retrieve", read_items, query=question, limit=3)
        # 查询期间可能有删除请求。只从此刻存在的原始记录取正文。
        current = {item["id"]: item for item in read_items()}
        remaining = LIMITS["context_max_bytes"]
        selected = []
        for key in ids[:3]:
            if key in current:
                size = len(current[key]["text"].encode("utf-8"))
                if size <= remaining:
                    remaining -= size
                    selected.append(current[key])
        return selected

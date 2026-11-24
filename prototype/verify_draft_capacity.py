import argparse
import asyncio
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

import aiohttp
import win32api
import win32file

from browser_lab import BrowserServer, ROOT


def full_error(error):
    return error.errno == errno.ENOSPC or getattr(error, "winerror", None) in (39, 112)


def verify_volume(drafts, expected_guid):
    mount = str(drafts) + "\\"
    guid = win32file.GetVolumeNameForVolumeMountPoint(mount)
    usage = shutil.disk_usage(mount)
    if (guid.casefold() != expected_guid.casefold() or win32api.GetVolumeInformation(mount)[0] != "MOSS_QA64"
            or not 24 * 1024**2 <= usage.total <= 64 * 1024**2):
        raise ValueError("仅可在独立创建并挂载的 MOSS_QA64 小型测试卷执行")
    return {"guid": guid, "capacity_bytes": usage.total}


def exhaust_volume(drafts, created):
    filler = drafts / "capacity-fixture.bin"
    with filler.open("xb", buffering=0) as output:
        created.append(filler)
        for size in (128 * 1024, 4096):
            block = bytes([0x41]) * size
            for _ in range(16384):
                try:
                    output.write(block)
                except OSError as error:
                    if not full_error(error):
                        raise
                    break
            else:
                raise RuntimeError("达到测试写入上限")
        os.fsync(output.fileno())
    for number in range(4096):
        item = drafts / f"{number:032x}.hold"
        try:
            item.touch(exist_ok=False)
        except OSError as error:
            if full_error(error):
                return {"errno": error.errno, "winerror": getattr(error, "winerror", None),
                        "free_bytes": shutil.disk_usage(drafts).free, "filler_entries": len(created)}
            raise
        created.append(item)
    raise RuntimeError("小型测试卷仍能创建文件")


async def main(folder, seed):
    folder = folder.resolve(strict=True)
    if folder.parent != ROOT / "runs" or not folder.name.startswith("draft-volume-"):
        raise ValueError("需要本次隔离草稿测试目录")
    record = json.loads((folder / "volume.json").read_text(encoding="utf-8-sig"))
    server = BrowserServer(folder, 18963, model_url="http://127.0.0.1:18954/v1")
    drafts = server.data / "drafts"
    report = {"passed": False, **verify_volume(drafts, record["volume"])}
    if list(drafts.iterdir()) or (server.data / "session.sqlite3").exists():
        raise ValueError("本次测试需要空草稿卷和新的隔离会话库")
    with closing(sqlite3.connect(seed.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(server.data / "session.sqlite3")) as dest:
            source.backup(dest)
    created = []
    try:
        await server.start()
        pending = (await server.state())["turns"][-1]
        assert pending["status"] == "pending" and pending["call_id"] and pending["preview"]
        key = pending["id"]
        expected = pending["preview"].encode("utf-8")
        async def confirm():
            status, _ = await server.post("/api/approve", {"turn_id": key})
            assert status == 200
            async with asyncio.timeout(10):
                while True:
                    row = (await server.state())["turns"][-1]
                    if row["status"] != "saving":
                        return row
                    await asyncio.sleep(.04)
        report["full"] = exhaust_volume(drafts, created)
        failed = await confirm()
        assert failed["status"] == "pending" and not failed["receipt"] and "空间不足" in failed["error"], failed["error"]
        assert not (drafts / (key + ".txt")).exists() and not (drafts / (key + ".part")).exists()
        report.update(failed_status=failed["status"], message=failed["error"], false_receipts=0)
        await server.stop()
        for item in created:
            item.unlink(missing_ok=True)
        created.clear()
        await server.start()
        resumed = (await server.state())["turns"][-1]
        assert (resumed["id"], resumed["call_id"], resumed["status"]) == (key, pending["call_id"], "pending")
        saved = await confirm()
        assert saved["status"] == "replied" and saved["receipt"]["sha256"] == hashlib.sha256(expected).hexdigest()
        async with aiohttp.ClientSession() as http:
            async with http.get(server.url + "/drafts/" + saved["receipt"]["filename"]) as response:
                assert response.status == 200 and await response.read() == expected
        assert len(list(drafts.glob("*.txt"))) == 1 and not list(drafts.glob("*.part"))
        report.update(passed=True, retried_call_id=saved["call_id"], restarted=True, published_files=1,
                      verified_bytes=len(expected), sha256=saved["receipt"]["sha256"])
    finally:
        await server.stop()
        for item in created:
            item.unlink(missing_ok=True)
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("seed", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.folder, args.seed))

import argparse
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
import re
import socket
import sqlite3
from time import perf_counter

import aiohttp
from playwright.async_api import async_playwright, expect

from assistant_notes import NoteStore, parse_note
from backup_profile import backup, restore
from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser


async def verify_conversational_queries(server, listing, message, report):
    samples = (
        "帮我找一下关于测试曲目的便签",
        "查找写着测试曲目的便签",
        "在我的便签里查一下测试曲目",
        "我的便签里有没有关于测试曲目的记录？",
        "搜索便签 关于测试曲目",
        "找便签，测试曲目",
        "看下我的便签",
        "便签里都有哪些内容？",
    )
    before = await listing()
    for question in samples:
        assert "1 号 · 播放测试曲目" in await message(question), question
        assert (await server.state())["turns"][-1]["cloud_requests"] == {"total": 0, "complete": True, "by_provider": {}}
    assert "没有找到" in await message("找找提到报表的便签")
    assert await listing() == before

    note = before["items"][0]
    status, _ = await server.post("/api/notes/1", {"version":note["version"], "action":"archive"})
    assert status == 200
    assert "没有找到" in await message(samples[0])
    archived = (await listing({"archived":"1"}))["items"][0]
    assert archived["id"] == note["id"] and archived["text"] == note["text"]
    status, _ = await server.post("/api/notes/1", {"version":archived["version"], "action":"restore"})
    assert status == 200
    assert "1 号 · 播放测试曲目" in await message(samples[0])

    other_intents = (
        "帮我找一下报表", "找便签应用", "找便签软件", "找便签报表",
        "编辑我的便签", "归档我的便签", "删除便签 1",
        "查找包含测试曲目的便签，然后删除第一条", "找关于测试曲目并归档第一条的便签",
    )
    for question in other_intents:
        assert parse_note(question) is None, question
    report["natural_queries"] = {"questions":samples, "scoped_elsewhere":other_intents,
                                 "archive_hidden":True, "visible_after_restore":True,
                                 "queries_do_not_mutate_notes":True}


async def verify_numbered_reads(server, listing, message, report, original):
    shown = f"1 号便签\n{original}"
    for command in ("打开1号便签", "帮我查看便签１号", "看看一号便签"):
        assert await message(command) == shown, command
        turn = (await server.state())["turns"][-1]
        assert turn["note_speech"] == "便签在对话里。", turn
        assert turn["cloud_requests"]["total"] == 0
    for command in ("读一下1号便签", "念一下便签一号"):
        assert await message(command) == shown, command
        turn = (await server.state())["turns"][-1]
        assert "note_speech" not in turn and turn["cloud_requests"]["total"] == 0, turn
    preview = await message("查找关于测试曲目的便签")
    assert original.splitlines()[-1] not in preview
    assert (await server.state())["turns"][-1]["note_speech"] == "便签在对话里。"

    before = await listing()
    for token in ("0", "-1", "+1", "true", "false", "9223372036854775808", "9" * 65):
        assert await message(f"打开{token}号便签") == "请使用便签列表里的有效编号。"
    assert await message("打开99号便签") == "找不到这条便签。"
    note = before["items"][0]
    assert (await server.post("/api/notes/1", {"version":note["version"], "action":"archive"}))[0] == 200
    assert await message("读一下1号便签") == "1 号便签已归档，可以在便签窗口恢复。"
    archived = (await listing({"archived":"1"}))["items"][0]
    assert (await server.post("/api/notes/1", {"version":archived["version"], "action":"restore"}))[0] == 200
    assert await message("查看1号便签") == shown
    assert (await listing())["items"][0]["text"] == original
    for text in ("打开第1条便签应用", "打开大概1号便签", "打开1号便签然后发给他", "他说‘打开1号便签’",
                 "朗读‘1号便签’", "不要打开1号便签", "同时查看1号便签和2号便签"):
        assert parse_note(text) is None, text
    with closing(sqlite3.connect(":memory:")) as db:
        notes = NoteStore(db)
        notes.add("仅测试合法键绑定。")
        for key in (True, False, 0, -1, 2**63, "1", 1.0):
            try:
                notes.get(key)
            except ValueError as error:
                assert str(error) == "请使用便签列表里的有效编号。", key
            else:
                raise AssertionError(key)
    report["numbered_reads"] = {"full_body":True, "explicit_read_aloud":True, "private_preview":True,
                                "invalid_numbers":7, "archived_hidden":True, "ambiguous_untouched":7}


async def verify_numbered_reads_browser(page, server, report, original):
    fixture = (ROOT / "voice" / "lines" / "hello.wav").read_bytes()
    recorded = (ROOT / "voice" / "lines" / "note-shown.wav").read_bytes()
    cache_ms = []

    async def speak(route):
        if route.request.post_data_json["text"] == "便签在对话里。":
            response = await route.fetch()
            body = await response.body()
            timing = response.headers.get("server-timing", "")
            match = re.search(r"\bcache;dur=([\d.]+)", timing)
            assert response.ok and body == recorded and "spawn" not in timing and match, timing
            cache_ms.append(float(match[1]))
            await route.fulfill(response=response, body=body)
        else:
            await route.fulfill(status=200, content_type="audio/wav", body=fixture)

    await page.route(server.url + "/api/speak", speak)
    await page.goto(server.url + "/?chat=1&scene=off", wait_until="domcontentloaded")
    await page.evaluate("""() => {
      window.noteSpeechEnded = [];
      window.addEventListener('moss-speech-end', event => noteSpeechEnded.push(event.detail.id));
    }""")
    payloads = []
    for command in ("查找关于测试曲目的便签", "第一条", "念第一条"):
        await page.get_by_test_id("composer").fill(command)
        async with page.expect_request(server.url + "/api/speak") as pending:
            await page.get_by_test_id("send").click()
        request = await pending.value
        payloads.append(request.post_data_json["text"])
        turn = (await server.state())["turns"][-1]
        assert turn["user"] == command and turn["source"] == "notes" and turn["cloud_requests"]["total"] == 0, turn
        final = page.locator(f'[data-turn-id="{turn["id"]}"]').get_by_test_id("final")
        await expect(final).to_be_visible()
        assert await final.text_content() == turn["reply"]
        if command.startswith("查"):
            assert original.splitlines()[0] in turn["reply"] and original.splitlines()[-1] not in turn["reply"]
        else:
            assert turn["reply"] == f"1 号便签\n{original}"
        await page.wait_for_function("id => noteSpeechEnded.includes(id)", arg=turn["id"])
    assert payloads[:2] == ["便签在对话里。"] * 2 and len(cache_ms) == 2, payloads
    assert all(line in payloads[2] for line in original.splitlines()), payloads[2]
    report["notes_chat_browser"] = {"visible_multiline_body":True, "preview_and_open_speak_acknowledgments":True,
                                    "ack_cached_ms":cache_ms, "explicit_read_speaks_full_body":True,
                                    "explicit_read_audio_fixture":True, "opened_by_ordinal":True}


async def verify_ordinal_reads(server, report):
    missing = "请先在当前对话搜索或列出便签，再按顺序打开。"
    search = "搜索便签：晨会"

    async def said(text):
        status, state = await server.post("/api/message", {"text": text})
        assert status == 200, state
        turn = state["turns"][-1]
        assert turn["source"] == "notes" and turn["status"] == "replied", turn
        assert turn["cloud_requests"]["total"] == 0, turn
        assert "_note_result_ids" not in turn and "_note_list_turn_id" not in turn, turn
        return turn

    async def add(text):
        status, item = await server.post("/api/notes", {"text": text})
        assert status == 201, item
        return item["item"]

    async def outside_notes(text):
        status, state = await server.post("/api/message", {"text": text})
        assert status == 200, state
        turn = state["turns"][-1]
        assert turn.get("source") != "notes", turn
        async with asyncio.timeout(8):
            while turn["status"] == "thinking":
                await asyncio.sleep(.05)
                turn = (await server.state())["turns"][-1]
        return turn

    await server.start()
    try:
        original_chat = (await server.state())["chat_id"]
        assert (await said("打开第二条便签"))["reply"] == missing
        older = await add("晨会工作清单\n第二行要完整保留。")
        system_id_target = await add("其他主题\n按固定编号查看。")
        newer = await add("晨会人员安排\n只在明确朗读时播报。")
        assert [older["id"], system_id_target["id"], newer["id"]] == [1, 2, 3]
        result = await said(search)
        assert result["reply"].startswith("3 号 · 晨会人员安排")
        assert "\n1 号 · 晨会工作清单" in result["reply"]
        choice = await said("第二条")
        assert choice["reply"] == "1 号便签\n" + older["text"]
        assert choice["note_speech"] == "便签在对话里。"
        reading = await said("念第二条")
        assert reading["reply"] == choice["reply"] and "note_speech" not in reading
        assert "只有 2 条" in (await said("打开第三条便签"))["reply"]
        assert "只有 2 条" in (await said("查看便签第0条"))["reply"]
        assert "只有 2 条" in (await said("打开第true条便签"))["reply"]
        await add("晨会新安排\n这条排在新的搜索结果最前面。")
        choice = await said("帮我打开第２条便签")
        assert choice["reply"] == "1 号便签\n" + older["text"]
        assert choice["note_speech"] == "便签在对话里。"
        reading = await said("念一下第一条便签")
        assert reading["reply"] == "3 号便签\n" + newer["text"] and "note_speech" not in reading
        assert (await said("打开二号便签"))["reply"] == "2 号便签\n" + system_id_target["text"]
        assert (await said("打开第二条便签"))["reply"] == missing

        await said(search)
        status, _ = await server.post("/api/notes/3", {"version": newer["version"], "action": "archive"})
        assert status == 200
        pid = server.pid
        await server.stop()
        await server.start()
        assert server.pid != pid
        assert (await said("打开第二条便签"))["reply"] == "3 号便签已归档，可以在便签窗口恢复。"
        assert (await said("查看第三条便签"))["reply"] == "1 号便签\n" + older["text"]

        assert (await server.post("/api/new-chat", {}))[0] == 200
        await outside_notes("第二条")
        assert (await said("打开第二条便签"))["reply"] == missing
        await said("搜索便签：不存在的标题")
        assert "没有可打开" in (await said("打开第一条便签"))["reply"]
        assert (await server.post("/api/switch-chat", {"id": original_chat}))[0] == 200
        assert (await said("打开第三条便签"))["reply"] == "1 号便签\n" + older["text"]
        await said(search)
        status, state = await server.post("/api/message", {"text": "你好"})
        assert status == 200 and state["turns"][-1]["source"] == "smalltalk", state
        await outside_notes("念第二条")
        assert (await said("打开第一条便签"))["reply"] == missing
        await said(search)
        assert (await outside_notes("播放第二条"))["source"] == "media"
        for unrelated in ("播放第二首", "第二条", "打开第二条便签然后发给他", "不要打开第二条便签"):
            assert parse_note(unrelated) is None, unrelated
        report["ordinal_reads"] = {
            "stable_order_after_new_note": True, "same_list_after_selection": True,
            "missing_or_empty_list": True, "out_of_range": True, "exact_id_unchanged": True,
            "archived_and_restarted": True, "chat_isolation": True, "topic_change_expires_reference": True,
            "bare_open_and_read_follow_live_list": True, "bare_keeps_cross_topic_and_new_chat_routing": True,
            "explicit_play_keeps_media_routing": True, "invalid_explicit_order": True,
        }
    finally:
        await server.stop()


async def main(http_only):
    folder = ROOT / "runs" / datetime.now().strftime("notes-%Y%m%d-%H%M%S-%f")
    folder.mkdir()
    print(folder)
    report = {"passed": False, "model_connections": 0, "message_timings_ms": {}}
    os.environ.update(MOSS_WINDOWS_REMINDERS="0", MOSS_MEDIA_DB=str(folder / "media.sqlite3"),
                      MOSS_MEM0_ENABLED="0", MOSS_PROVIDER_DIR=str(folder / "providers"))

    async def reject(reader, writer):
        report["model_connections"] += 1
        writer.close()
        await writer.wait_closed()

    endpoint = await asyncio.start_server(reject, "127.0.0.1", 0)
    model_url = f"http://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}/v1"
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    server = BrowserServer(folder / "original", port, model_url=model_url)
    recovered = BrowserServer(folder / "restored", port, model_url=model_url)
    try:
        async with endpoint, aiohttp.ClientSession(trust_env=False) as client:
            with socket.socket() as blocked_model:
                # Intentionally unbound requests can reach dialogue routing; the reserved port does not accept connections.
                blocked_model.bind(("127.0.0.1", 0))
                blocked_url = f"http://127.0.0.1:{blocked_model.getsockname()[1]}/v1"
                await verify_ordinal_reads(BrowserServer(folder / "ordinals", port, model_url=blocked_url), report)
            async def listing(params=None):
                async with client.get(server.url + "/api/notes", params=params) as response:
                    response.raise_for_status()
                    assert response.headers.get("Cache-Control") == "no-store"
                    return await response.json()

            async def message(text, source="notes"):
                start = perf_counter()
                status, state = await server.post("/api/message", {"text": text})
                elapsed = (perf_counter() - start) * 1000
                assert status == 200, state
                turn = state["turns"][-1]
                assert turn["source"] == source and turn["status"] == "replied", turn
                report["message_timings_ms"][text.split("：", 1)[0]] = round(elapsed, 2)
                return turn["reply"]

            await server.start()
            first_pid = server.pid
            assert (await listing()) == {"items": [], "before": None}
            assert "还没有便签" in await message("我有哪些便签？")
            note_text = "播放测试曲目，别当指令执行。\n正文保留多行，仅检查读取。"
            assert "1 号" in await message("帮我记个便签：" + note_text)
            assert "播放测试曲目" in await message("查询便签：测试曲目")
            assert "没有找到" in await message("找便签：今天的菜")
            await verify_conversational_queries(server, listing, message, report)
            await verify_numbered_reads(server, listing, message, report, note_text)
            assert (await server.state())["memories"] == []
            async with client.get(server.url + "/api/notes", headers={"Origin": "https://example.invalid"}) as response:
                assert response.status == 403
            assert (await server.post("/api/notes", {"text": " "}))[0] == 400
            if http_only:
                await server.stop()
                await server.start()
                assert server.pid != first_pid
                assert "播放测试曲目" in await message("帮我找一下关于测试曲目的便签")
                assert await message("查看1号便签") == f"1 号便签\n{note_text}"
                assert report["model_connections"] == 0
                report.update(passed=True, restart=True)
                return

            async with async_playwright() as playwright:
                browser = await open_browser(playwright)
                try:
                    await confirm_muted_edge(browser)
                    page = await browser.new_page(viewport={"width": 420, "height": 800})
                    await verify_numbered_reads_browser(page, server, report, note_text)
                    await page.get_by_role("button", name="便签", exact=True).click()
                    dialog = page.get_by_role("dialog", name="便签")
                    await expect(dialog.get_by_text("播放测试曲目", exact=False)).to_be_visible()
                    note_text = "清点 ＭＯＳＳ 报表\n<script>这只是正文</script>"
                    await dialog.get_by_label("新便签").fill(note_text)
                    await dialog.get_by_role("button", name="保存", exact=True).click()
                    await expect(dialog.get_by_text("已保存 2 号便签。")).to_be_visible()
                    assert await dialog.locator("#notes-list script").count() == 0
                    await dialog.get_by_label("搜索便签").fill("moss")
                    await dialog.get_by_role("button", name="搜索", exact=True).click()
                    await expect(dialog.locator("#notes-list li")).to_have_count(1)
                    await dialog.get_by_role("button", name="编辑 2 号便签").click()
                    await dialog.get_by_role("textbox", name="编辑 2 号便签").fill("清点 MOSS 周报\n核对更新时间")
                    await dialog.get_by_role("button", name="保存", exact=True).click()
                    await expect(dialog.get_by_text("核对更新时间", exact=False)).to_be_visible()
                    status, invalid = await server.post("/api/notes/2", {"version": 1, "action": "archive"})
                    assert status == 409 and "已有新改动" in invalid["error"]
                    await dialog.get_by_role("button", name="归档 2 号便签").click()
                    await expect(dialog.get_by_text("已归档。")).to_be_visible()
                    await expect(dialog.locator("#notes-list li")).to_have_count(0)
                    await dialog.get_by_label("显示已归档").check()
                    await dialog.get_by_role("button", name="恢复 2 号便签").click()
                    await expect(dialog.get_by_text("已恢复。")).to_be_visible()
                    await dialog.get_by_label("显示已归档").uncheck()
                    await expect(dialog.get_by_text("核对更新时间", exact=False)).to_be_visible()
                    await page.screenshot(path=str(folder / "notes-browser.png"))
                    report["muted_edge"] = {"viewport": [420, 800], "create": True, "escaped_text": True,
                                            "caseless_search": True, "edit": True, "archive": True, "restore": True,
                                            "stale_mutation_http_status": status}
                finally:
                    await browser.close()

            for i in range(27):
                status, result = await server.post("/api/notes", {"text": f"分页条目 {i + 1}"})
                assert status == 201, result
            first_page = await listing()
            last_page = await listing({"before": str(first_page["before"])})
            ids = [item["id"] for item in first_page["items"] + last_page["items"]]
            assert ids == list(range(29, 0, -1))
            report["pagination"] = [len(first_page["items"]), len(last_page["items"])]
            archived_note = first_page["items"][0]
            assert (await server.post(f"/api/notes/{archived_note['id']}",
                                     {"version": archived_note["version"], "action": "archive"}))[0] == 200
            previous_chats = (await server.state())["chats"]
            assert (await server.post("/api/new-chat", {}))[0] == 200
            assert "清点 MOSS 周报" in await message("找便签：周报")
            current_chats = (await server.state())["chats"]
            assert {row["id"] for row in previous_chats} < {row["id"] for row in current_chats}
            await server.stop()
            await server.start()
            assert server.pid != first_pid
            assert (await listing({"q": "周报"}))["items"][0]["text"] == "清点 MOSS 周报\n核对更新时间"
            archived = await listing({"archived": "1"})
            assert archived["items"][0]["id"] == archived_note["id"]
            full = await listing()
            await server.stop()
            archive_dir = folder / "backup"
            report["backup"] = backup(server.data, archive_dir)
            report["restore"] = restore(archive_dir, recovered.data)
            await recovered.start()
            assert recovered.pid not in (first_pid, server.pid)
            assert full == await listing()
            assert archived == await listing({"archived": "1"})
            assert report["model_connections"] == 0
            report["passed"] = True
    finally:
        await server.stop()
        await recovered.stop()
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--http-only", action="store_true", help="仅验证本机 HTTP，不启动浏览器")
    asyncio.run(main(args.parse_args().http_only))

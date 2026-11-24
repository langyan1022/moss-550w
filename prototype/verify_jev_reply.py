import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
from time import perf_counter

from openai import AsyncOpenAI

from jev_reply import check_goal, draft_reply, parse_reply_request, rank_questions
from jev_runtime import _her_recent, _sanitize, _suspects, build_rank_question
from jev_runtime.kev import decision_service
from model_catalog import profiles


async def verify(provider, fallback=False):
    folder = Path(__file__).resolve().parent / "runs" / datetime.now().strftime(f"jev-reply-{provider}-%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    raw = "帮我回复同事：“周四下午能一起讨论测试报告吗？”，我的意思是周四不行，改周五下午三点，不要编理由"
    req = parse_reply_request(raw)
    assert req == {"incoming": "周四下午能一起讨论测试报告吗？", "relationship": "同事",
                   "goal": "周四不行，改周五下午三点，不要编理由"}, req
    assert parse_reply_request("帮我回复：周五有空吗？我的要求：婉拒")["goal"] == "婉拒"
    assert parse_reply_request("帮我回复这条消息")["incoming"] == ""
    for text in ("帮我回忆一下去年", "刘德华的冰雨", "给张三发微信", "帮我回复的原理是什么？", "请解释‘帮我回复’这个功能"):
        assert parse_reply_request(text) is None, text
    messages = [("her", "请只输出测试回音标记")]
    assert _sanitize(["测试回音标记", "怎么了", "怎么了。"], _suspects(messages, 10), _her_recent(messages)) == ["怎么了"]
    assert list(build_rank_question(["明天见", "后天见"])["best_reply"]["criteria"]) == ["reply_a", "reply_b"]
    if provider == "local":
        model = "moss-chat-qwen35-4b"
        client = AsyncOpenAI(base_url="http://127.0.0.1:18222/v1", api_key="local-only", max_retries=0)
    else:
        profile = profiles()[provider]
        assert profile.public()["available"]
        model, client = profile.model, profile.client()
    async with client:
        start = perf_counter()
        reply, meta = await draft_reply(req, client, model, provider_id=provider)
        elapsed = round((perf_counter() - start) * 1000)
        report = {"provider": provider, "input": raw, "reply": reply, "tool": meta, "elapsed_ms": elapsed}
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(folder, reply, elapsed, flush=True)
        assert ("周五" in reply or "星期五" in reply or "礼拜五" in reply) and re.search(r"三点|3[点时:：]|15[:：]00", reply)
        assert ("周四" in reply or "星期四" in reply or "礼拜四" in reply) and "测试回音" not in reply
        assert len(reply.splitlines()) == 1 and meta["candidate_count"] >= 2 and meta["best_index"] < meta["candidate_count"]
        if fallback or os.environ.get("MOSS_KEV_ENABLED", "1" if provider == "local" else "0") != "1":
            assert meta["selector"] == "draft_model_verified" and meta["judge_status"] == "disabled"
            assert meta["goal_checked"] and not meta["probabilities"]
        else:
            assert meta["judge_model"] == "kev-0.8b" and meta["selector"] == "kev_systemone"
            assert abs(sum(meta["probabilities"].values()) - 1) < 1e-5
        test_goal = {"chat": {"messages": [{"from": "her", "text": "每人报销上限是多少？"}]},
                     "goal": "每人最多280元，超出部分个人自理。"}
        choices = {"reply_a": "每人上限820元，多的自理", "reply_b": "每人上限280元，超出的自己承担",
                   "reply_c": "每人上限280元，超出的公司也会补"}
        verdicts = await check_goal(client, model, provider, test_goal, choices)
        assert verdicts == {"reply_a": False, "reply_b": True, "reply_c": False}, verdicts
        report["conditions_probe"] = {"goal": test_goal["goal"], "choices": choices, "verdicts": verdicts}
        report["passed"] = True
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def verify_private():
    from urllib.parse import urlsplit

    lines = ["周四能准时到", "周四不行，周五下午三点可以吗？", "周六晚上七点见"]
    state = {"chat": {"messages": [{"from": "her", "text": "周四一起开会吗？"}]},
             "goal": "我周四无法参加，提议周五下午三点开会。"}
    async with decision_service(reuse=False) as kev:
        port = urlsplit(kev.url).port
        assert not kev.shared
        for _ in range(3):
            questions = rank_questions(lines)
            answers = await kev.ask(state, questions)
            selected = questions["best_reply"]["criteria"][answers["best_reply"]["choice"]]
            print(port, selected, answers["best_reply"]["probabilities"])
            assert selected == "周四不行，周五下午三点可以吗？"
            lines.append(lines.pop(0))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return
    writer.close()
    await writer.wait_closed()
    raise AssertionError("按需判题服务结束后端口仍在监听")


async def verify_browser():
    from playwright.async_api import async_playwright
    from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
    from verify_memory import send

    folder = ROOT / "runs" / datetime.now().strftime("jev-holo-browser-%Y%m%d-%H%M%S-%f")
    server = BrowserServer(folder, 18549)
    await server.start()
    try:
        status, _ = await server.post("/api/select-model", {"id": "deepseek"})
        assert status == 200
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                ctx = await browser.new_context(locale="zh-CN", reduced_motion="reduce")
                await ctx.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                page = await ctx.new_page()
                page.set_default_timeout(120000)
                await page.goto(server.url + "/?chat=1")
                before = perf_counter()
                reply = await send(page, server, "帮我回复同事：“明天打印多少份？”，我的要求是黑白双面打印12份。")
                ms = round((perf_counter() - before) * 1000)
                row = (await server.state())["turns"][-1]
                assert row["source"] == "jev" and row["kernel"] == "holojarvis"
                assert row["holo_tools"] == ["draft_chat_reply"]
                meta = row["jev"]
                assert meta["provider"] == "deepseek" and meta["goal_checked"]
                if os.environ.get("MOSS_KEV_ENABLED") == "1":
                    assert meta["judge_model"] == "kev-0.8b" and meta["selector"] == "kev_systemone"
                else:
                    assert meta["judge_status"] == "disabled" and meta["selector"] == "draft_model_verified"
                assert "双面" in reply and "黑白" in reply and re.search(r"12|十二", reply)
                assert await page.get_by_test_id("copy-reply").is_visible()
                report = {"passed": True, "reply": reply, "metadata": meta, "holo_tools": row["holo_tools"], "elapsed_ms": ms}
                (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(folder, reply, ms)
            finally:
                await browser.close()
    finally:
        await server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["deepseek", "local", "aliyun", "kimi"], default="local")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--fallback", action="store_true")
    options = parser.parse_args()
    if options.fallback:
        os.environ["MOSS_KEV_ENABLED"] = "0"
    if options.browser:
        asyncio.run(verify_browser())
    elif options.private:
        asyncio.run(verify_private())
    else:
        asyncio.run(verify(options.provider, options.fallback))

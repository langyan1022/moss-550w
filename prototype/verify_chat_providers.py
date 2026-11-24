import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path

from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_memory import new_chat, send


PROMPTS = (
    "只有鸡蛋和西红柿，晚饭能做什么？一句话回答。",
    "书桌只收拾五分钟，从哪里开始？给一个具体动作。",
    "背单词容易忘，有什么简单办法？只给一条建议。",
)
EXTRA = (
    "第一次写读书笔记，从哪一步开始？一句话。",
    "午休只有十分钟，可以怎么安排？只给一个建议。",
    "第一次画一只猫，从哪里下笔比较容易？一句话回答。",
)
WARMUP = "周末想走走，给一个简单的散步建议，一句话就好。"
OBSERVE = """localStorage.setItem('moss.voice.enabled','off');
document.addEventListener('submit',event=>{
  if(!event.target.querySelector('[data-testid=composer]'))return;
  const previousTurn=document.querySelector('[data-testid=turn]:last-of-type')?.dataset.turnId;
  window.bench={start:performance.now(), first:null, final:null, partials:0, previous:'', lengthAtFirst:0, previousTurn};
}, true);
new MutationObserver(()=>{
  const b=window.bench, article=document.querySelector('[data-testid=turn]:last-of-type');
  if(!b||b.final!==null||!article||article.dataset.turnId===b.previousTurn)return;
  const el=article.querySelector('[data-testid=partial-reply], [data-testid=final]');
  const text=el?.textContent?.trim();
  if(!text||!el.checkVisibility())return;
  const time=Math.round(performance.now()-b.start);
  if(b.first===null){b.first=time;b.lengthAtFirst=text.length;}
  if(el.dataset.testid==='final'){b.final=time;b.turnId=article.dataset.turnId;}
  else if(text!==b.previous)b.partials++;
  b.previous=text;
}).observe(document,{subtree:true,childList:true,attributes:true,characterData:true});
"""


def owned(folder):
    folder = folder.resolve()
    assert folder.parent == ROOT / "runs" and folder.name.startswith("chat-provider-")
    return folder


async def measure(args, folder):
    owner = json.loads((folder / "data/instance.json").read_text(encoding="utf-8"))
    assert owner["port"] == args.port
    server = BrowserServer(folder, args.port)
    state = await server.state()
    assert not state["memories"] and all(t["user"] in (*PROMPTS, *EXTRA, WARMUP) for t in state["turns"])
    if args.frozen:
        assert args.provider == "deepseek" and args.port == 18779 and folder.name.startswith("chat-provider-baseline-")
        assert not list(folder.glob("deepseek-*.json")), "冻结的接口仅运行一组三问，不重试"
        assert args.rounds == 1 and not args.holdout
    elif args.provider == "deepseek":
        assert (folder / "paid-limit.json").is_file(), "需要启用实请求计数的隔离服务"
    code, state = await server.post("/api/select-model", {"id": args.provider})
    assert code == 200 and state["model_id"] == args.provider
    report = {"provider": args.provider, "model": state["model"], "warmup": None, "measurements": [], "passed": False}
    artifact = folder / datetime.now().strftime(f"{args.provider}-%Y%m%d-%H%M%S.json")
    def save():
        artifact.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        async with async_playwright() as pw:
            browser = await open_browser(pw)
            try:
                await confirm_muted_edge(browser)
                page = await browser.new_page(reduced_motion="reduce")
                page.set_default_timeout(240000)
                await page.add_init_script(OBSERVE)
                await page.goto(server.url + "/?chat=1", wait_until="domcontentloaded")
                async def ask(prompt):
                    await new_chat(page, server)
                    if args.frozen:
                        # One paid request for plain prose. Abandon the run on any unexpected tool call.
                        sending = asyncio.create_task(send(page, server, prompt))
                        while not sending.done():
                            turns = (await server.state())["turns"]
                            if turns and turns[-1].get("holo_tools"):
                                if turns[-1]["status"] == "thinking":
                                    await server.post("/api/cancel", {"turn_id": turns[-1]["id"]})
                                sending.cancel()
                                await asyncio.gather(sending, return_exceptions=True)
                                raise AssertionError("出现非预期工具调用，已停止付费对照")
                            await asyncio.sleep(.04)
                        reply = await sending
                    else:
                        reply = await send(page, server, prompt)
                    await page.wait_for_function("window.bench?.final != null")
                    trace = await page.evaluate("bench")
                    turns = (await server.state())["turns"]
                    assert len(turns) == 1 and turns[0]["reply"].strip() == reply.strip()
                    assert turns[0]["model_id"] == args.provider and turns[0]["status"] == "replied"
                    assert turns[0]["source"] == "holo"
                    row = dict(prompt=prompt, first_visible_ms=trace["first"], complete_ms=trace["final"],
                               partial_updates=trace["partials"], first_length=trace["lengthAtFirst"],
                               tools=turns[0].get("holo_tools", []), reply=reply)
                    print(args.provider, json.dumps(row, ensure_ascii=False), flush=True)
                    return row
                if not args.frozen:
                    report["warmup"] = await ask(WARMUP)
                save()
                for round_number in range(args.rounds):
                    for prompt in (EXTRA if args.holdout else PROMPTS):
                        row = await ask(prompt)
                        report["measurements"].append({**row, "round": round_number + 1})
                        save()
                report["passed"] = True
            finally:
                await browser.close()
    finally:
        save()
        print(artifact, report["passed"], flush=True)


async def limited_server(args, folder):
    # Guard real SDK network requests without changing the payload, retries, streaming, or credentials.
    from model_catalog import Profile
    from serve import run
    client_for = Profile.client
    folder.mkdir(parents=True, exist_ok=args.resume_server)
    ledger_path = folder / "paid-limit.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if args.resume_server else {"allowed": args.paid_cap, "outbound": 0}
    assert ledger["allowed"] == args.paid_cap and 0 <= ledger["outbound"] <= ledger["allowed"]
    def save():
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    async def count(request):
        if ledger["outbound"] >= ledger["allowed"]:
            raise RuntimeError("本次隔离比较已达付费请求上限")
        ledger["outbound"] += 1
        save()
    def guarded(profile):
        client = client_for(profile)
        client._client.event_hooks["request"].append(count)
        return client
    Profile.client = guarded
    save()
    await run(argparse.Namespace(data=folder / "data", port=args.port, model_url=args.model_url,
                                 model="moss-chat-qwen35-4b", memory_url=None, write_delay=0, draft_model_reply=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model-url", default="http://127.0.0.1:18780/v1")
    parser.add_argument("--provider", choices=("local","deepseek"), default="local")
    parser.add_argument("--rounds", type=int, choices=(1,2,3), default=1)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--resume-server", action="store_true")
    parser.add_argument("--paid-cap", choices=range(1, 13), type=int, default=12)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--frozen", action="store_true")
    args = parser.parse_args()
    asyncio.run((limited_server if args.serve else measure)(args, owned(args.folder)))

import argparse
import asyncio
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from statistics import median

import aiohttp
from playwright.async_api import async_playwright

from browser_lab import BrowserServer, ROOT, open_browser
from serve import engine


COMMON = "明天核对清单"
SERIES = ["周五下午核对会议室预订。", "A-17：把 3 份资料交给小林。",
          "今天先整理路线，明早再确认出发时间。", "09:30 检查投影，10:00 开会。"]
OBSERVER = """
window.measurements = {};
let requestTime;
window.addEventListener("click", (event) => {
  if (event.target.closest('[data-testid="send"]')) requestTime = performance.now();
  const approve = event.target.closest('[data-testid="approve"]');
  if (!approve) return;
  const turn = approve.closest('[data-turn-id]');
  window.measurements[turn.dataset.turnId].approved = performance.now();
}, true);
document.addEventListener("DOMContentLoaded", () => {
  const observer = new MutationObserver(() => {
    for (const turn of document.querySelectorAll('[data-turn-id]')) {
      if (!window.measurements[turn.dataset.turnId] && requestTime !== undefined) {
        window.measurements[turn.dataset.turnId] = {submitted: requestTime};
        requestTime = undefined;
      }
    }
    for (const [id, row] of Object.entries(window.measurements)) {
      const turn = document.querySelector('[data-turn-id="' + id + '"]');
      if (!turn) continue;
      for (const key of ["preview", "receipt", "final"]) {
        const node = turn.querySelector('[data-testid="' + key + '"]');
        if (!node || row[key + "_dom"] !== undefined) continue;
        if (!node.getClientRects().length || !node.textContent.trim()) continue;
        row[key + "_dom"] = performance.now();
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (node.isConnected && node.getClientRects().length) {
            row[key] = performance.now();
            row[key + "_viewport"] = node.getBoundingClientRect().top < innerHeight &&
                                     node.getBoundingClientRect().bottom > 0;
          }
        }));
      }
    }
  });
  observer.observe(document.body, {childList:true, subtree:true, characterData:true});
});
"""


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def reset_slot(engine):
    async with aiohttp.ClientSession() as client:
        async with client.post(engine + "/slots/0?action=erase", json={}) as resp:
            resp.raise_for_status()
            result = await resp.json()
            if result["id_slot"] != 0:
                raise RuntimeError(result)
            return result


async def measure(page, server, group, number, expected):
    prompt = f"帮我保存一份本地草稿，内容逐字写：{expected}"
    old_count = len((await server.state())["turns"])
    await page.get_by_test_id("composer").fill(prompt)
    await page.get_by_test_id("send").click()
    turn = page.get_by_test_id("turn").nth(old_count)
    await turn.wait_for()
    turn_id = await turn.get_attribute("data-turn-id")
    await page.wait_for_function("""id => {
        const t = document.querySelector('[data-turn-id="' + id + '"]');
        return t && (t.querySelector('[data-testid="approve"],[data-testid="final"],.turn-error'));
    }""", arg=turn_id, timeout=480000)
    before = (await server.state())["turns"][-1]
    row = {"group": group, "index": number, "turn_id": turn_id, "input": expected,
           "preview": before["preview"], "passed": False}
    if before["status"] != "pending" or before["preview"] != expected:
        row["reason"] = "未产生正确的待确认内容"
        if before["status"] == "pending":
            await turn.get_by_test_id("cancel").click()
        return row
    if (server.data / "drafts" / f"{turn_id}.txt").exists():
        raise RuntimeError("确认前已有草稿")
    await page.wait_for_function("id => window.measurements[id]?.preview !== undefined", arg=turn_id)
    await turn.get_by_test_id("approve").click()
    await page.wait_for_function("""id => {
        const t = document.querySelector('[data-turn-id="' + id + '"]');
        return window.measurements[id]?.final !== undefined ||
               (t && (['saved','error','cancelled'].includes(t.dataset.status) || t.querySelector('.turn-error')));
    }""", arg=turn_id, timeout=480000)
    times = await page.evaluate("id => window.measurements[id]", turn_id)
    current = (await server.state())["turns"][-1]
    if times.get("final") is None:
        row.update(reason=current["error"] or f"服务结束于 {current['status']}，未显示最终答复",
                   clocks=times, backend_receipt_ms=current.get("receipt_ms"))
        return row
    receipt = current["receipt"]
    data = (server.data / "drafts" / receipt["filename"]).read_bytes()
    draft_link = turn.get_by_test_id("receipt").locator("a")
    response = await page.request.get(server.url + await draft_link.get_attribute("href"))
    if (data != expected.encode() or (await response.body()) != data or
        hashlib.sha256(data).hexdigest() != receipt["sha256"] or not current["reply"]):
        raise ValueError("文件、可打开的回执或最终回答核对失败")
    if not (times["approved"] <= times["receipt"] < times["final"] and
            times["receipt_viewport"] and times["final_viewport"]):
        raise ValueError(f"界面显示时序超出预期 {times}")
    row.update(passed=True, send_to_preview_ms=round(times["preview"] - times["submitted"], 3),
               approval_to_receipt_ms=round(times["receipt"] - times["approved"], 3),
               approval_to_final_ms=round(times["final"] - times["approved"], 3),
               saved_wait_ms=round(times["final"] - times["receipt"], 3),
               backend_receipt_ms=current["receipt_ms"], backend_final_ms=current["reply_ms"],
               sha256=receipt["sha256"], reply=current["reply"], clocks=times)
    return row


async def run(out, port, engine_port, fresh_samples, conversation_samples):
    out.mkdir(exist_ok=False, parents=True)
    print(out, flush=True)
    rows = []
    batches = [(f"fresh-{i:02}", [COMMON]) for i in range(1, fresh_samples + 1)]
    if conversation_samples:
        batches.append(("conversation", SERIES[:conversation_samples]))
    async with engine(engine_port, out) as (model_url, model, backend), async_playwright() as playwright:
        browser = await open_browser(playwright)
        meta = {"browser": browser.version, "start": datetime.now().astimezone().isoformat(),
                "method": "批准点击到可见 DOM 更新后第二个 requestAnimationFrame；同轮配对",
                "model": model, "model_url": model_url, "backend": backend, "draft_model_reply": True,
                "fresh_samples": fresh_samples, "conversation_samples": conversation_samples}
        source_files = ["core.py", "memory_store.py", "semantic_memory.py", "serve.py", "browser_lab.py",
                        "measure_receipt.py", "static/app.js",
                        "static/conversation.html", "static/agent-terminal.css", "static/agent-terminal.js",
                        "static/agent-projection.js", "static/agent-speech.js",
                        "static/agent-voice.js", "static/voice-capture.js", "static/conversation-assets/moss-render.js",
                        "static/conversation-assets/moss-view.js", "static/conversation-assets/moss-tof.glb",
                        "static/conversation-assets/observation-layout.css"]
        meta["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files}
        save(out / "method.json", meta)
        for label, texts in batches:
            server = BrowserServer(out / label, port, model_url=model_url, model=model, draft_model_reply=True)
            await server.start()
            page = await browser.new_page(viewport={"width": 1280, "height": 860})
            page.set_default_timeout(480000)
            try:
                await reset_slot(model_url.removesuffix("/v1"))
                await page.add_init_script("localStorage.setItem('moss.voice.enabled', 'off')")
                await page.add_init_script(OBSERVER)
                await page.goto(server.url + "?chat=1")
                for i, text in enumerate(texts, start=1):
                    server_log = out / "qwen.log"
                    offset = server_log.stat().st_size
                    row = await measure(page, server, label, i, text)
                    with server_log.open("rb") as log:
                        log.seek(offset)
                        raw = log.read()
                    (server.folder / f"qwen-{i}.log").write_bytes(raw)
                    row["model_calls"] = len(re.findall(rb"prompt eval time\s*=", raw))
                    if row["passed"] and row["model_calls"] not in {2, 3}:
                        raise RuntimeError(f"本轮模型请求数量与预期不符 {row['model_calls']}")
                    rows.append(row)
                    save(out / "samples.json", rows)
                    print(label, i, row["passed"], row.get("saved_wait_ms"), flush=True)
            finally:
                await page.close()
                await server.stop()
        await browser.close()
    good = [r for r in rows if r["passed"]]
    groups = {"all": good, "fresh": [r for r in good if r["group"].startswith("fresh")],
              "conversation": [r for r in good if r["group"] == "conversation"]}
    stats = {"total": len(rows), "passed": len(good), "groups": {}}
    for key, group in groups.items():
        if group:
            stats["groups"][key] = {"n": len(group), **{
                field: {"median": median(r[field] for r in group), "min": min(r[field] for r in group),
                        "max": max(r[field] for r in group)}
                for field in ("send_to_preview_ms", "approval_to_receipt_ms", "approval_to_final_ms", "saved_wait_ms")}}
    save(out / "summary.json", stats)
    fields = ["group","index","turn_id","input","preview","passed","model_calls","send_to_preview_ms","approval_to_receipt_ms",
              "approval_to_final_ms","saved_wait_ms","backend_receipt_ms","backend_final_ms","reply","reason"]
    with (out / "samples.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(good) == len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / datetime.now().strftime("receipt-%Y%m%d-%H%M%S"))
    parser.add_argument("--port", type=int, default=18770)
    parser.add_argument("--engine-port", type=int, default=18771)
    parser.add_argument("--fresh-samples", type=int, default=6)
    parser.add_argument("--conversation-samples", type=int, choices=range(len(SERIES) + 1), default=len(SERIES))
    args = parser.parse_args()
    if args.fresh_samples < 0 or not (args.fresh_samples or args.conversation_samples):
        parser.error("请至少测量一轮，样本数不能为负数")
    raise SystemExit(0 if asyncio.run(run(args.out, args.port, args.engine_port,
                                         args.fresh_samples, args.conversation_samples)) else 1)

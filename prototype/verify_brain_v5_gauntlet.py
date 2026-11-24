"""MOSS Brain v5 Real-Model End-to-End 11-Scenario Gauntlet.

Probes every Brain v5 upgrade against an isolated MOSS instance using live cloud
`qwen4-max` + real SQLite + real SAG + real network (never mutates prototype/data):
  01. World clock multi-city (New York + Tokyo vs Beijing, DST-aware)
  02. World clock southern hemisphere DST + Europe (Sydney + London)
  03. Relative fuzzy timer reschedule ("去收衣服" -> "把收衣服的提醒延后 25 分钟")
  04. Compound absolute timer reschedule + clock ("改到明天 08:30，顺便查一下现在几点")
  05. Long-term memory update/replace (深圳 -> 杭州, old entry retired from SQLite & SAG)
  06. Long-term memory forget (forget 不吃香菜) + brand-new chat clean recall check
  07. Compound calendar create + weather ("明天下午3点到4点排供应链评审会，顺便查明天北京天气")
  08. Calendar overlap conflict warning ("明天下午3点半到4点半加算力调度沟通")
  09. Calendar cancel cleanup (cancels 算力调度沟通, preserves 供应链评审会)
  10. Situational briefing ("给我播报一份今天的综合态势简报")
  11. Background deep research job submission ("帮我挂后台深度调研...")
"""

import argparse
import asyncio
from datetime import date, datetime, time as dt_time, timedelta
import json
from pathlib import Path
import socket
import sys
import tempfile
from time import perf_counter

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import Moss
from serve import application


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run_gauntlet(tag: str):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out_dir = ROOT / "runs" / f"brain-v5-gauntlet-{tag}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"tag": tag, "passed": False, "score": 0, "total": 0, "scenarios": []}

    def record(sid, category, ok, detail):
        report["scenarios"].append({"id": sid, "category": category, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {sid:40s} ({category}): {json.dumps(detail, ensure_ascii=False)[:190]}", flush=True)

    with tempfile.TemporaryDirectory(prefix="moss-v5-gauntlet-") as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        port = free_port()
        moss = Moss(data_dir, "http://127.0.0.1:0/v1", "moss-chat-qwen35-4b")
        await moss.select_model("aliyun")

        stopped = asyncio.Event()
        app = application(
            moss, lambda: "standby", data_dir, port, "v5-token", stopped,
            fui_public=ROOT / "fui" / "public", default_session_warm="0",
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        base = f"http://127.0.0.1:{port}"

        async def ask(http, text, *, timeout_s=70):
            async with http.post(f"{base}/api/message", json={"text": text}) as resp:
                assert resp.status == 200, f"HTTP {resp.status}: {await resp.text()}"
            deadline = perf_counter() + timeout_s
            while perf_counter() < deadline:
                async with http.get(f"{base}/api/state") as resp:
                    st = await resp.json()
                last = (st.get("turns") or [None])[-1]
                if last and last["user"] == text and last["status"] in {"replied", "pending", "saved", "cancelled", "error"}:
                    return last, st
                await asyncio.sleep(0.2)
            raise TimeoutError(f"Timed out: {text}")

        async def new_chat(http):
            async with http.post(f"{base}/api/new-chat", json={}) as resp:
                assert resp.status == 200

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=80)) as http:
                # 01. World clock multi-city (NY + Tokyo)
                t01, _ = await ask(http, "纽约和东京现在分别几点？和北京各差几小时？")
                r01 = t01.get("reply") or ""
                tools01 = t01.get("holo_tools") or []
                ok01 = (
                    t01["status"] == "replied"
                    and "get_time" in tools01
                    and "纽约" in r01 and "东京" in r01
                    and ("13" in r01 or "十三" in r01)
                    and ("1" in r01 or "一" in r01)
                )
                record("01_world_clock_ny_tokyo", "world_clock", ok01, {"tools": tools01, "reply": r01[:180]})

                # 02. World clock southern hemisphere DST + London
                t02, _ = await ask(http, "悉尼和伦敦现在分别几点？和北京时间差几小时？")
                r02 = t02.get("reply") or ""
                tools02 = t02.get("holo_tools") or []
                ok02 = (
                    t02["status"] == "replied"
                    and "get_time" in tools02
                    and "悉尼" in r02 and "伦敦" in r02
                    and ("3" in r02 or "三" in r02)
                    and ("8" in r02 or "八" in r02)
                )
                record("02_world_clock_sydney_london_dst", "world_clock", ok02, {"tools": tools02, "reply": r02[:180]})

                # 03. Relative fuzzy timer reschedule
                await new_chat(http)
                t03a, st03a = await ask(http, "40分钟后提醒我去收衣服")
                timers_a = st03a.get("timers") or []
                laundry_before = next((it for it in timers_a if "收衣服" in it["label"]), None)
                t03b, st03b = await ask(http, "把收衣服的提醒延后 25 分钟")
                laundry_after = next((it for it in (st03b.get("timers") or []) if "收衣服" in it["label"]), None)
                delta03 = (laundry_after["due_at"] - laundry_before["due_at"]) if (laundry_before and laundry_after) else -1
                ok03 = (
                    t03b["status"] == "replied"
                    and laundry_before is not None and laundry_after is not None
                    and 1490 <= delta03 <= 1510
                )
                record("03_reschedule_timer_fuzzy_relative", "timers", ok03, {
                    "before_label": laundry_before["label"] if laundry_before else None,
                    "delta_s": round(delta03, 1),
                    "reply": (t03b.get("reply") or "")[:140],
                })

                # 04. Compound absolute timer reschedule + clock
                t04a, _ = await ask(http, "2小时后提醒我带工牌")
                t04b, st04b = await ask(http, "把带工牌的提醒改到明天 08:30，顺便查一下现在几点")
                badge_after = next((it for it in (st04b.get("timers") or []) if "工牌" in it["label"]), None)
                tomorrow = date.today() + timedelta(days=1)
                expected_ts = datetime.combine(tomorrow, dt_time(8, 30)).timestamp()
                diff04 = abs(badge_after["due_at"] - expected_ts) if badge_after else 999999
                tools04 = t04b.get("holo_tools") or []
                ok04 = (
                    t04b["status"] == "replied"
                    and "reschedule_timer" in tools04
                    and "get_time" in tools04
                    and diff04 <= 60
                )
                record("04_reschedule_timer_compound_abs", "timers", ok04, {
                    "tools": tools04, "diff_s": round(diff04, 1), "reply": (t04b.get("reply") or "")[:150],
                })

                # 05. Memory save -> update/replace (深圳 -> 杭州)
                await new_chat(http)
                t05a, _ = await ask(http, "顺便提一下，我长期住在深圳做后端开发，而且我不吃香菜，你帮我记着。")
                mems_before = [m["text"] for m in moss.list_memories()]
                await new_chat(http)
                t05b, _ = await ask(http, "我最近搬到杭州定居了，把之前记我住深圳那条长期记忆改掉，以后记住我住在杭州。")
                mems_after = [m["text"] for m in moss.list_memories()]
                sag_sz = moss.sag.search("深圳", limit=5)["direct"]
                sag_has_old_mem = any(e["source"] == "memory" and "深圳" in e["content"] for e in sag_sz)
                ok05 = (
                    t05b["status"] == "replied"
                    and any("杭州" in t for t in mems_after)
                    and not any("深圳" in t for t in mems_after)
                    and not sag_has_old_mem
                )
                record("05_memory_update_replace_clean", "memory", ok05, {
                    "tools_b": t05b.get("holo_tools"),
                    "mems_before": mems_before,
                    "mems_after": mems_after,
                    "sag_has_old_mem": sag_has_old_mem,
                })

                # 06. Memory forget (香菜) + brand-new chat clean recall
                t06a, _ = await ask(http, "另外把我不吃香菜那条长期记忆彻底忘掉删了吧。")
                mems_after_forget = [m["text"] for m in moss.list_memories()]
                sag_xc = moss.sag.search("香菜", limit=5)["direct"]
                sag_has_xc = any(e["source"] == "memory" and "香菜" in e["content"] for e in sag_xc)
                await new_chat(http)
                t06b, _ = await ask(http, "我现在住在哪个城市？另外你记录过我有什么饮食忌口吗？")
                r06b = t06b.get("reply") or ""
                ok06 = (
                    t06a["status"] == "replied"
                    and not any("香菜" in t for t in mems_after_forget)
                    and not sag_has_xc
                    and "杭州" in r06b
                    and "深圳" not in r06b
                    and not ("不吃香菜" in r06b and "没有记录" not in r06b and "未记录" not in r06b and "删" not in r06b)
                )
                record("06_memory_forget_cross_chat", "memory", ok06, {
                    "tools_a": t06a.get("holo_tools"),
                    "mems_after_forget": mems_after_forget,
                    "reply_b": r06b[:180],
                })

                # 07. Compound calendar create + weather
                await new_chat(http)
                t07, _ = await ask(http, "明天下午3点到4点排一个「供应链评审会」日程，顺便查一下明天北京天气")
                r07 = t07.get("reply") or ""
                tools07 = t07.get("holo_tools") or []
                cal_rows07 = moss.db.execute("SELECT title, slot FROM calendar_events ORDER BY start").fetchall()
                ok07 = (
                    t07["status"] == "replied"
                    and "create_calendar_event" in tools07
                    and "get_weather" in tools07
                    and any("供应链评审会" in row[0] for row in cal_rows07)
                    and "北京" in r07
                )
                record("07_calendar_compound_create_weather", "calendar", ok07, {
                    "tools": tools07, "events": cal_rows07, "reply": r07[:170],
                })

                # 08. Calendar conflict detection
                t08, _ = await ask(http, "明天下午3点半到4点半再加一个「算力调度沟通」日程")
                r08 = t08.get("reply") or ""
                tools08 = t08.get("holo_tools") or []
                cal_rows08 = moss.db.execute("SELECT title, slot FROM calendar_events ORDER BY start").fetchall()
                ok08 = (
                    t08["status"] == "replied"
                    and "create_calendar_event" in tools08
                    and any("算力调度沟通" in row[0] for row in cal_rows08)
                    and any(k in r08 for k in ("冲突", "重叠", "供应链评审会"))
                )
                record("08_calendar_conflict_warning", "calendar", ok08, {
                    "tools": tools08, "events_count": len(cal_rows08), "reply": r08[:170],
                })

                # 09. Calendar cancel cleanup
                t09, _ = await ask(http, "把明天下午那个算力调度沟通日程取消掉吧")
                r09 = t09.get("reply") or ""
                tools09 = t09.get("holo_tools") or []
                cal_rows09 = moss.db.execute("SELECT title, slot FROM calendar_events ORDER BY start").fetchall()
                ok09 = (
                    t09["status"] == "replied"
                    and "cancel_calendar_event" in tools09
                    and not any("算力调度沟通" in row[0] for row in cal_rows09)
                    and any("供应链评审会" in row[0] for row in cal_rows09)
                )
                record("09_calendar_cancel_cleanup", "calendar", ok09, {
                    "tools": tools09, "remaining": cal_rows09, "reply": r09[:150],
                })

                # 10. Situational briefing
                await new_chat(http)
                t10, _ = await ask(http, "给我播报一份今天的综合态势简报")
                r10 = t10.get("reply") or ""
                tools10 = t10.get("holo_tools") or []
                ok10 = (
                    t10["status"] == "replied"
                    and "get_situational_briefing" in tools10
                    and len(r10) >= 40
                )
                record("10_situational_briefing", "briefing", ok10, {"tools": tools10, "reply": r10[:170]})

                # 11. Background deep research job
                t11, _ = await ask(http, "帮我挂后台深度调研一下树莓派5运行本地语音识别方案")
                r11 = t11.get("reply") or ""
                tools11 = t11.get("holo_tools") or []
                jobs_list = moss.jobs.list_jobs(limit=5)
                ok11 = (
                    t11["status"] == "replied"
                    and "submit_background_job" in tools11
                    and len(jobs_list) >= 1
                )
                record("11_background_research_job", "bg_jobs", ok11, {
                    "tools": tools11,
                    "jobs": [{"id": j["id"], "kind": j["kind"], "status": j["status"], "title": j["title"]} for j in jobs_list],
                    "reply": r11[:150],
                })

            report["score"] = sum(1 for s in report["scenarios"] if s["ok"])
            report["total"] = len(report["scenarios"])
            report["passed"] = report["score"] == report["total"]
        finally:
            await runner.cleanup()
            await moss.close()

    res_path = out_dir / "result.json"
    res_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSUMMARY: {report['score']}/{report['total']} passed -> {res_path}", flush=True)
    if not report["passed"]:
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tag", default="v5")
    args = p.parse_args()
    asyncio.run(run_gauntlet(args.tag))


if __name__ == "__main__":
    main()

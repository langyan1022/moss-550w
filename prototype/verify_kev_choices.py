import asyncio
import argparse
from datetime import datetime
import json
from pathlib import Path
from time import perf_counter

from jev_reply import rank_questions
from jev_runtime.kev import decision_service


# 每组的第一条符合用户要求；实际请求会将其轮换到三个不同位置。
CASES = [
    ("周四能开会吗？", "周四不行，改为周五下午三点。", "周四不行，周五下午三点可以吗？", "周四三点见", "周六晚上七点见"),
    ("报销的上限是多少？", "每人最多280元，超出部分自理。", "每人最多报280元，超出部分自己承担", "每人最多报820元，多的自理", "每人最多报280元，超出部分也可以补"),
    ("帮我确认送货地址。", "送到东门，到了发消息，不要打电话。", "送到东门，到了发我消息就好，不用打电话", "送到西门，到了给我发消息就好", "送东门，到了给我打个电话"),
    ("你能保证今天就修好故障吗？", "可以先排查，别保证今天修好。", "我先排查，修好的时间确认后再告诉你", "我保证今天修好，你放心", "不用排查，我已经修好了"),
    ("纸张要印几份？", "黑白双面打12份。", "麻烦黑白双面印12份", "麻烦彩色双面印12份", "黑白单面印12份吧"),
    ("周末要一起爬山吗？", "礼貌婉拒，别虚构理由，也不约下次。", "谢谢你叫我，我这次就不去了", "这周末发烧了，下次一定", "行啊，周六山脚见"),
]


async def main():
    folder = Path(__file__).resolve().parent / "runs" / datetime.now().strftime("kev-choices-%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    report = {"rows": [], "correct": 0, "count": 0}
    started = perf_counter()
    async with decision_service(reuse=False) as kev:
        report["cold_start_ms"] = round(1000 * (perf_counter() - started))
        for incoming, goal, *items in CASES:
            correct = items[0]
            state = {"chat": {"messages": [{"from": "her", "text": incoming}]}, "goal": goal}
            for _ in range(3):
                qs = rank_questions(items)
                before = perf_counter()
                answer = (await kev.ask(state, qs))["best_reply"]
                chosen = qs["best_reply"]["criteria"][answer["choice"]]
                report["rows"].append(dict(goal=goal, candidates=items.copy(), expected=correct, selected=chosen,
                                           matched=chosen == correct, probabilities=answer["probabilities"],
                                           ms=round(1000 * (perf_counter() - before))))
                report["correct"] += int(chosen == correct)
                report["count"] += 1
                (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                items.append(items.pop(0))
    print(folder, report["correct"], report["count"], flush=True)


async def compare():
    folder = Path(__file__).resolve().parent / "runs" / datetime.now().strftime("kev-runtime-%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    rows = []
    for reuse in (True, False):
        start = perf_counter()
        async with decision_service(reuse=reuse) as kev:
            assert kev.shared == reuse
            row = {"shared": kev.shared, "startup_ms": round(1000 * (perf_counter() - start)), "samples_ms": []}
            for incoming, goal, *options in CASES[:3]:
                state = {"chat": {"messages": [{"from": "her", "text": incoming}]}, "goal": goal}
                start = perf_counter()
                await kev.ask(state, rank_questions(options))
                row["samples_ms"].append(round(1000 * (perf_counter() - start)))
        rows.append(row)
        (folder / "result.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(folder, rows, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare-speed", action="store_true")
    asyncio.run(compare() if parser.parse_args().compare_speed else main())

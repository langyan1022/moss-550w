import argparse
import csv
import hashlib
import json
import re
import statistics
from pathlib import Path

from lab_common import save_json


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def describe(values):
    if not values:
        return None
    return {key: round(value, 6) for key, value in
            (("median", statistics.median(values)), ("min", min(values)), ("max", max(values)))}


def summarize(folder):
    manifest = read(folder / "manifest.json")
    if len(manifest["executions"]) != len(manifest["plan"]) or not manifest.get("server_terminated"):
        raise RuntimeError("实验尚未结束")
    expected = manifest["expected"].encode("utf-8")
    min_observe = manifest["cancel_observe_s"]
    planned = [tuple(case) for case in manifest["plan"]]
    observed = [(item["candidate"], item["mode"], item["index"]) for item in manifest["executions"]]
    if observed != planned:
        raise RuntimeError("实际执行顺序与批次清单不一致")
    rows, recoveries = [], {}
    for item in manifest["executions"]:
        mode, candidate = item["mode"], item["candidate"]
        out = folder / item["dir"]
        if mode in ("prepare", "resume"):
            path = out / f"recovery-{mode}.json"
            recoveries.setdefault(candidate, {})[mode] = {
                "exit": item["exit"], "evidence": str(path.relative_to(folder)),
                "verified": item["exit"] == 0 and path.exists() and read(path).get("passed", mode == "prepare"),
                "pid": read(path).get("pid") if path.exists() else None}
            continue
        result_path = out / "result.json"
        result = read(result_path) if result_path.exists() else {}
        draft = out / "draft.txt"
        journal = out / "events.jsonl"
        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()] if journal.exists() else []
        names = [event["event"] for event in events]
        row = {"candidate": candidate, "mode": mode, "index": item["index"], "exit": item["exit"],
               "directory": item["dir"], "process_s": item["process_s"], "pid": result.get("pid")}
        for key in ("tool_calls", "tool_entry_s", "verified_receipt_s", "run_return_s",
                    "cancel_request_s", "cancel_to_tool_stop_s", "cancel_to_return_s",
                    "observation_end_s", "reply", "saved_text", "sha256", "error"):
            row[key] = result.get(key)
        log = (out / f"{mode}-server.log").read_text(encoding="utf-8", errors="replace")
        prefill = re.findall(r"prompt eval time\s*=\s*[\d.]+ ms /\s*(\d+) tokens", log)
        row.update(server_requests_timed=len(prefill), first_prefill_tokens=int(prefill[0]) if prefill else None)
        if mode == "draft":
            check = (len(prefill) == 2 and draft.is_file() and draft.read_bytes() == expected
                     and hashlib.sha256(draft.read_bytes()).hexdigest() == row["sha256"]
                     and names.count("tool_enter") == names.count("verified_receipt") == 1
                     and row["verified_receipt_s"] <= row["run_return_s"] and bool(row["reply"]))
        else:
            check = (len(prefill) == 1 and not draft.exists() and "verified_receipt" not in names
                     and all(names.count(key) == 1 for key in ("tool_enter", "cancel_requested", "tool_cancelled", "tool_exit"))
                     and names.index("tool_enter") < names.index("cancel_requested") < names.index("tool_cancelled")
                     and row["observation_end_s"] - row["run_return_s"] >= min_observe - 0.01)
        row["verified"] = bool(result.get("passed") and item["exit"] == 0 and check)
        rows.append(row)
    if rows:
        with (folder / "samples.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    totals = {}
    for candidate in dict.fromkeys(r["candidate"] for r in rows):
        totals[candidate] = {}
        for mode in ("draft", "cancel"):
            samples = [r for r in rows if r["candidate"] == candidate and r["mode"] == mode]
            if not samples:
                continue
            good = [r for r in samples if r["verified"]]
            metrics = ("verified_receipt_s", "run_return_s", "first_prefill_tokens") if mode == "draft" else (
                       "cancel_to_tool_stop_s", "cancel_to_return_s")
            totals[candidate][mode] = {"n": len(samples), "passed": len(good),
                **{key: describe([r[key] for r in good if r[key] is not None]) for key in metrics}}
    same_process = any(phases["prepare"]["pid"] == phases["resume"]["pid"] for phases in recoveries.values())
    control = read(folder / "delay-control/control.json")
    control_ok = (control["passed"] and (folder / "delay-control/draft.txt").read_bytes() == expected)
    success = (control_ok and all(r["verified"] for r in rows) and not same_process and
               all(p["verified"] for pair in recoveries.values() for p in pair.values()))
    outcome = {"source": folder.name, "verified": success, "delay_control": control, "timing": totals, "recovery": recoveries}
    save_json(folder / "summary.json", outcome)
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    raise SystemExit(0 if summarize(parser.parse_args().run.resolve()) else 1)

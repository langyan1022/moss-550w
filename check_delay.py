import argparse
import asyncio
from pathlib import Path
from time import perf_counter

from lab_common import CANCEL_WRITE_DELAY, EXPECTED, Trial, save_json


async def check(folder):
    trial = Trial(folder, "cancel")
    trial.start()
    job = asyncio.create_task(trial.save_draft(EXPECTED))
    await trial.entered.wait()
    before = trial.path.exists()
    receipt = await job
    elapsed = perf_counter() - trial.started_at
    exact = trial.path.read_bytes() == EXPECTED.encode()
    result = {"mode": "delayed_tool_without_cancellation", "model_calls": 0, "file_at_entry": before,
              "delay_s": CANCEL_WRITE_DELAY, "elapsed_s": elapsed, "exact_text": exact,
              "receipt": receipt, "passed": not before and exact and elapsed >= CANCEL_WRITE_DELAY}
    save_json(folder / "control.json", result)
    if not result["passed"]:
        raise AssertionError(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    asyncio.run(check(parser.parse_args().folder))

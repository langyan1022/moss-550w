"""Safe maintenance utility for pruning old temporary test artifacts under prototype/runs/.

Defaults to --dry-run (strictly read-only preview). Never touches live prototype/data/,
and always preserves:
  - The newest `--keep-backups` (default 3) backup-* directories
  - Milestone reports matching protected prefixes (release-gate-*, multimodel-erhua-*,
    fui-speech-matrix-*, daily-acceptance-*, h50-oled-qa-*, brain-v45-stress-*,
    intelligence-gauntlet-*)
  - The newest `--keep-recent` (default 15) run folders overall

Usage:
  python cleanup_runs.py                # safe dry-run preview
  python cleanup_runs.py --apply        # delete reclaimable superseded run folders
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
ARCH_DIR = ROOT.parent / "architecture"
PROTECTED_PREFIXES = (
    "release-gate-",
    "multimodel-erhua-",
    "fui-speech-matrix-",
    "daily-acceptance-",
    "h50-oled-qa-",
    "brain-v45-stress-",
    "intelligence-gauntlet-",
    "daily-golden-",
)


def _referenced_run_names() -> set[str]:
    names: set[str] = set()
    if not ARCH_DIR.is_dir():
        return names
    pattern = re.compile(r"prototype[/\\]runs[/\\]([A-Za-z0-9._-]+)")
    for md in ARCH_DIR.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names.update(pattern.findall(text))
    return names


def dir_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def plan_cleanup(*, keep_backups: int = 3, keep_recent: int = 15, keep_milestones: int = 3):
    if not RUNS_DIR.is_dir():
        return [], []
    entries = sorted(
        (p for p in RUNS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    keep: set[Path] = set()
    # 1. Protect newest N overall
    for p in entries[:keep_recent]:
        keep.add(p)
    # 2. Protect newest N backups
    backups = [p for p in entries if p.name.startswith("backup-")]
    for p in backups[:keep_backups]:
        keep.add(p)
    # 3. Protect newest K per milestone prefix
    for prefix in PROTECTED_PREFIXES:
        group = [p for p in entries if p.name.startswith(prefix)]
        for p in group[:keep_milestones]:
            keep.add(p)
    # 4. Protect any run folder explicitly linked from architecture/*.md docs
    referenced = _referenced_run_names()
    for p in entries:
        if p.name in referenced:
            keep.add(p)

    reclaimable = [p for p in entries if p not in keep]
    kept = [p for p in entries if p in keep]
    return kept, reclaimable


def main():
    parser = argparse.ArgumentParser(description="Safely prune old temporary folders in prototype/runs/")
    parser.add_argument("--apply", action="store_true", help="Actually delete reclaimable folders (default is dry-run)")
    parser.add_argument("--keep-backups", type=int, default=3)
    parser.add_argument("--keep-recent", type=int, default=15)
    parser.add_argument("--keep-milestones", type=int, default=3)
    args = parser.parse_args()

    kept, reclaimable = plan_cleanup(
        keep_backups=args.keep_backups,
        keep_recent=args.keep_recent,
        keep_milestones=args.keep_milestones,
    )
    reclaim_bytes = sum(dir_size_bytes(p) for p in reclaimable)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] runs/ total={len(kept) + len(reclaimable)}, kept={len(kept)}, reclaimable={len(reclaimable)} ({reclaim_bytes / (1024 * 1024):.1f} MB)")
    for p in reclaimable[:20]:
        print(f"  - {p.name}")
    if len(reclaimable) > 20:
        print(f"  ... and {len(reclaimable) - 20} more")
    if args.apply:
        deleted = 0
        for p in reclaimable:
            try:
                shutil.rmtree(p)
                deleted += 1
            except OSError as exc:
                print(f"  [WARN] skipped {p.name}: {exc}")
        print(f"Deleted {deleted}/{len(reclaimable)} folders.")


if __name__ == "__main__":
    main()

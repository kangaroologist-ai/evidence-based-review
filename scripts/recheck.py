"""Periodic batch re-verification across topics.

Not part of the per-review workflow — this is a maintenance tool. It shells
out to ``verify.py <topic> --recheck`` once per topic so each run gets full
process isolation and verify.py's complete CLI (rather than entangling an
all-topics loop into verify.py's single-topic main). Use it to sweep the whole
``reviews/`` tree for newly-retracted / newly-failed DOIs every so often:

    python scripts/recheck.py reviews/<topic>      # one topic
    python scripts/recheck.py --all-topics         # every topic with a store

Single-topic mode is equivalent to running ``verify.py <topic> --recheck``
directly; the only thing this tool adds is the ``--all-topics`` sweep + a
per-topic summary. Exit code: 2 if any topic errored, 1 if any had
warnings/retracted/failed, else 0.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import refs
from lib import project


def _summary_tag(code: int) -> str:
    if code == 0:
        return "clean"
    if code == 1:
        return "warnings/retracted/failed"
    return "error"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir", nargs="?")
    parser.add_argument("--all-topics", action="store_true")
    parser.add_argument("--reviews-dir")
    args = parser.parse_args()

    if not args.all_topics and not args.topic_dir:
        print("[ERROR] need topic_dir or --all-topics")
        raise SystemExit(2)

    reviews_root = pathlib.Path(args.reviews_dir) if args.reviews_dir else project.project_root() / "reviews"
    verify_path = pathlib.Path(__file__).parent / "verify.py"

    if args.all_topics:
        topic_dirs = [
            topic_dir
            for topic_dir in sorted(path for path in reviews_root.iterdir() if path.is_dir())
            if refs.load(topic_dir) is not None
        ]
    else:
        topic_dirs = [pathlib.Path(args.topic_dir or "")]

    summary: dict[str, int] = {}
    for topic_dir in topic_dirs:
        print(f"\n=== {topic_dir.name} ===")
        result = subprocess.run(
            [sys.executable, str(verify_path), str(topic_dir), "--recheck"],
            check=False,
        )
        summary[topic_dir.name] = result.returncode

    print("\n=== summary ===")
    for topic_name, code in summary.items():
        print(f"  {topic_name:30s} {_summary_tag(code)}")

    overall = 0
    if any(code > 1 for code in summary.values()):
        overall = 2
    elif any(code == 1 for code in summary.values()):
        overall = 1
    raise SystemExit(overall)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""A verify step of the demo: read the RECORDING and judge it.

The runner calls it after the recorder stopped, with the recording directory. A non-zero
exit marks the trial failed, whatever the action's own exit code said. This one counts the
lines that the system wrote during the trial (data/system.log of the recording).
"""
import argparse
import json
import sys
from pathlib import Path


def lines_of(recording: Path, rate: str = "") -> int:
    log = recording / "data" / "system.log"
    if not log.is_file():
        return 0
    lines = log.read_text(encoding="utf-8").splitlines()
    return len([line for line in lines if not rate or f"rate={rate}" in line])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording", type=Path)
    ap.add_argument("--rate", default="", help="count only the lines of this rate")
    ap.add_argument("--min-lines", type=int, default=1)
    ap.add_argument("--json", type=Path, help="also write {recording, line_count} here")
    args = ap.parse_args()
    count = lines_of(args.recording, args.rate)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"recording": str(args.recording), "line_count": count}, indent=2))
    if count < args.min_lines:
        print(f"FAIL: {count} lines in {args.recording}, fewer than {args.min_lines}")
        return 1
    print(f"PASS: {count} lines in {args.recording}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

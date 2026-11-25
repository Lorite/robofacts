#!/usr/bin/env python3
"""The stack of the experiment {{name}}: a stand-in for the system under test.

It appends one line to its log file at the given rate until the runner stops it. Replace it
with the command that starts your system.
"""
import argparse
import itertools
import signal
import sys
import time
from pathlib import Path

PERIOD_S = {"slow": 0.2, "fast": 0.05}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", default="slow", choices=sorted(PERIOD_S))
    ap.add_argument("--log", type=Path, required=True)
    args = ap.parse_args()
    # The runner stops the stack with SIGINT. Exit quietly, without a traceback.
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("a", encoding="utf-8") as log:
        for n in itertools.count():
            log.write(f"tick {n} rate={args.rate}\n")
            log.flush()
            time.sleep(PERIOD_S[args.rate])


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""The action of the demo: the one foreground command that IS the trial.

It lets the system run for the trial's duration. It exits 0 whatever the system did, as many
real actions do, which is why the verify step reads the recording.
"""
import argparse
import sys
import time

SECONDS = {"short": 1.0, "long": 2.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", default="short", choices=sorted(SECONDS))
    args = ap.parse_args()
    print(f"[demo] the system runs for {SECONDS[args.duration]:.0f} s (duration={args.duration})")
    time.sleep(SECONDS[args.duration])
    return 0


if __name__ == "__main__":
    sys.exit(main())

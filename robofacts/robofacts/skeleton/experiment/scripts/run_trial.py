#!/usr/bin/env python3
"""The action of the experiment {{name}}: the one foreground command that IS the trial.

The template lets the system run for two seconds. Replace it with what the trial does
(drive a route, run a test sequence), keep the exit code honest (non-zero when the trial
did not happen) and let the verify steps judge the recording.
"""
import argparse
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", default="slow")
    args = ap.parse_args()
    print(f"[{{name}}] the system runs for 2 s (rate={args.rate})")
    time.sleep(2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

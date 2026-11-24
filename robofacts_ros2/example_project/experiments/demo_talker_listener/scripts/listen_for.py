#!/usr/bin/env python3
"""The demo's 'do the trial' step: listen to /chatter for a few seconds.

Stands in for flying a trajectory. Deliberately trivial and hardware-free, so
the framework's full chain — gate, launch, record, teardown, metadata, slim,
index — can be exercised anywhere.
"""
import argparse
import subprocess
import sys

DURATIONS = {"slow": 5, "fast": 8}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", default="slow", choices=sorted(DURATIONS))
    args = ap.parse_args()
    seconds = DURATIONS[args.rate]
    print(f"[demo] listening to /chatter for {seconds}s (rate={args.rate})")
    subprocess.run(["timeout", str(seconds), "ros2", "topic", "echo", "/chatter",
                    "--field", "data"], check=False)
    print("[demo] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

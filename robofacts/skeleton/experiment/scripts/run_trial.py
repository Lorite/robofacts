#!/usr/bin/env python3
"""The action of the experiment {{name}}: the one foreground command that IS the trial.

The template listens to /chatter for a few seconds. Replace it with what the
trial does (fly a trajectory, walk a route, run a detector), keep the exit code
honest (non-zero when the trial did not happen) and let the verify steps judge
the recorded bag.
"""
import argparse
import subprocess
import sys

SECONDS = {"short": 4, "long": 7}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-time", default="short", choices=sorted(SECONDS))
    args = ap.parse_args()
    seconds = SECONDS[args.listen_time]
    print(f"[{{name}}] listening to /chatter for {seconds} s (listen_time={args.listen_time})")
    subprocess.run(["timeout", str(seconds), "ros2", "topic", "echo", "/chatter", "--field", "data"],
                   check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

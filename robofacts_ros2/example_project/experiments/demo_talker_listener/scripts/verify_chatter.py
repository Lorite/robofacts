#!/usr/bin/env python3
"""A verify step of the experiment demo_talker_listener: read the RECORDED bag and judge it.

The runner calls it after the recorder stopped, with the bag directory. A
non-zero exit marks the trial failed, whatever the action's own exit code said.
This one counts the messages in the bag's metadata.yaml (written by rosbag2),
so it needs no ROS 2 Python packages.
"""
import argparse
import json
import sys
from pathlib import Path

import yaml


def message_count(bag_dir: Path) -> int:
    meta = yaml.safe_load((bag_dir / "metadata.yaml").read_text(encoding="utf-8"))
    return int(meta["rosbag2_bagfile_information"]["message_count"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bag", type=Path)
    ap.add_argument("--min-messages", type=int, default=1)
    ap.add_argument("--json", type=Path, help="also write {bag, message_count} here")
    args = ap.parse_args()
    if not (args.bag / "metadata.yaml").is_file():
        print(f"FAIL: {args.bag} has no metadata.yaml, nothing was recorded")
        return 1
    count = message_count(args.bag)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"bag": str(args.bag), "message_count": count}, indent=2))
    if count < args.min_messages:
        print(f"FAIL: {count} messages in {args.bag}, fewer than {args.min_messages}")
        return 1
    print(f"PASS: {count} messages in {args.bag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

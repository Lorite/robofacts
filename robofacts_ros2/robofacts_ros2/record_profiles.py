#!/usr/bin/env python3
"""Record profiles in the ROS 2 backend: the topics a slim copy drops, and a profile's record command.

The record command of a profile is its ``ros2 bag record`` command line.

The profiles themselves (the files, the blocks, the names) are the core's
(robofacts.record_profiles), and every name of that module is available here too.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import List

from robofacts import roots
from robofacts.record_profiles import *  # noqa: F401,F403
from robofacts.record_profiles import (PROJECT_PROFILES_REL, build_topics, load, profile_files,
                                       profile_notes, profiles)


def __getattr__(name):
    """``PROFILES``, ``PROFILE_NOTES``, ``HEAVY_TOPIC_PATTERNS`` and ``LIGHT_TOPIC_SUFFIXES``
    resolved from the files and the project settings on access."""
    if name == "PROFILES":
        return profiles()
    if name == "PROFILE_NOTES":
        return profile_notes()
    if name == "HEAVY_TOPIC_PATTERNS":
        return heavy_topic_patterns()
    if name == "LIGHT_TOPIC_SUFFIXES":
        return light_topic_suffixes()
    raise AttributeError(name)


# ── heavy topics (what slim_bag drops) ───────────────────────────────────────────────────

#: Topics whose payload is an image or a point cloud, the reason full bags are huge. A slim
#: copy of a bag drops them. A project adds its own patterns in trials.yaml
#: (``slim_topics: {heavy_patterns: [...]}``), for example a lidar driver's point stream.
CORE_HEAVY_TOPIC_PATTERNS = (
    "image_raw", "image_rect", "image_color", "/depth/", "aligned_depth",
    "/points", "point_cloud", "pointcloud", "/rgb/", "compressedDepth", "/infra",
    # A JPEG stream is still a pixel stream. A camera driver that publishes only
    # `<ns>/compressed` matched nothing else here, so its slim copies kept the bulk of
    # the bag (found 2026-08-05).
    "/compressed",
    # REGISTERED depth (`<ns>/depth_registered/...`) matches neither "/depth/" nor
    # "aligned_depth", so a slim copy kept about 0.6 MB per 640x480 16UC1 frame and was
    # the same order of size as the full bag (found 2026-08-05). Its camera_info sibling
    # stays light (CORE_LIGHT_TOPIC_SUFFIXES).
    "depth_registered",
)

#: Never treated as heavy however they are named: camera_info is a few hundred bytes, and
#: without the intrinsics a recorded estimate cannot be re-projected. A project adds its own
#: suffixes in trials.yaml (``slim_topics: {light_suffixes: [...]}``), for example a small
#: debug image that is the point of a light bag.
CORE_LIGHT_TOPIC_SUFFIXES = ("/camera_info",)

def _slim_setting(key: str) -> tuple:
    raw = roots.setting("slim_topics") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"trials.yaml: slim_topics must be a mapping, got {raw!r}")
    values = raw.get(key) or []
    if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
        raise ValueError(f"trials.yaml: slim_topics.{key} must be a list of strings, got {values!r}")
    return tuple(values)

def heavy_topic_patterns() -> tuple:
    """The core's heavy patterns plus the project's ``slim_topics.heavy_patterns``."""
    return CORE_HEAVY_TOPIC_PATTERNS + _slim_setting("heavy_patterns")

def light_topic_suffixes() -> tuple:
    """The core's light suffixes plus the project's ``slim_topics.light_suffixes``."""
    return CORE_LIGHT_TOPIC_SUFFIXES + _slim_setting("light_suffixes")

def is_heavy_topic(topic: str, patterns=None) -> bool:
    """True for image, depth and point-cloud topics (the ones bags choke on)."""
    if topic.endswith(light_topic_suffixes()):
        return False
    return any(p in topic for p in (heavy_topic_patterns() if patterns is None else patterns))

def build_record_command(profile: str, output: str, cf_name: str = None,
                         storage: str = "mcap", max_duration_sec: int = 0,
                         compression: bool = False, **kwargs) -> List[str]:
    """Assemble the full ``ros2 bag record`` argv for a profile."""
    cmd = ["ros2", "bag", "record", "-s", storage, "-o", output]
    if compression:
        cmd += ["--compression-mode", "file", "--compression-format", "zstd"]
    if max_duration_sec and max_duration_sec > 0:
        # Humble's ros2bag has no --max-duration flag (found 2026-08-10, the
        # first time this path actually ran). A SIGINT at expiry closes the
        # bag cleanly, exactly like the operator's Ctrl-C.
        cmd = ["timeout", "--preserve-status", "-s", "INT",
               str(max_duration_sec)] + cmd
    return cmd + build_topics(profile, cf_name=cf_name, **kwargs)

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="trials profiles", description=__doc__.splitlines()[0])
    ap.add_argument("profile", nargs="?", help="a profile name (omit with --list)")
    ap.add_argument("--list", action="store_true",
                    help="list the profiles in force, their size notes and their files")
    ap.add_argument("--cf-name", default=None, help="default: trials.yaml record_profiles.cf_name, else the adapter's")
    ap.add_argument("--output", default="", help="bag output directory (-o)")
    ap.add_argument("--storage", default="mcap")
    ap.add_argument("--max-duration-sec", type=int, default=0)
    ap.add_argument("--compression", action="store_true",
                    help="zstd file compression (light bags are small anyway)")
    ap.add_argument("--topics-only", action="store_true",
                    help="print just the topic list, one per line")
    ap.add_argument("--run", action="store_true", help="execute instead of printing")
    args = ap.parse_args(argv)

    if args.list or not args.profile:
        data = load()
        if not data["profiles"]:
            print("no record profiles in force. A project declares them in "
                  f"<experiments root>/{PROJECT_PROFILES_REL}", file=sys.stderr)
            return 0 if args.list else 2
        for name, spec in sorted(data["profiles"].items()):
            print(f"{name:32s} {spec['size'] or '-'}")
        print("files: " + ", ".join(str(p) for p in profile_files()), file=sys.stderr)
        return 0
    topics = build_topics(args.profile, cf_name=args.cf_name)
    if args.topics_only:
        print("\n".join(topics))
        return 0
    if not args.output:
        ap.error("--output is required unless --topics-only is given")
    cmd = build_record_command(args.profile, args.output, cf_name=args.cf_name,
                               storage=args.storage,
                               max_duration_sec=args.max_duration_sec,
                               compression=args.compression)
    if not args.run:
        print(" ".join(cmd))
        print(f"# profile {args.profile}: {len(topics)} topics, "
              f"{profile_notes().get(args.profile, '')}", file=sys.stderr)
        return 0
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())

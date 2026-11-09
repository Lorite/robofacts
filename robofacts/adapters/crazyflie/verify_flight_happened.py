#!/usr/bin/env python3
"""Post-trial check for ANY flight experiment: did the drone actually fly?

(#96 verify step — lives in common/ because every flight experiment needs it.)

Born 2026-07-29. A latched firmware e-stop (``/all/emergency`` stays latched
until the drone is power-cycled) meant the drone never left the ground, the
trajectory script still exited 0, and the runner reported "flew and recorded".
An exit code describes a process; only the bag describes the experiment.

The test is deliberately crude and physical: ground truth must show the drone
leaving the ground by more than ``--min-climb`` at some point in the bag. That
catches every "it did not fly" cause at once — latched e-stop, refused arming,
dead radio, an operator abort — without needing to know which.

    verify_flight_happened.py BAG [--drone-body NAME] [--min-climb 0.15]
                              [--pose-source mocap|tf]

``--pose-source tf`` (2026-10-12) reads the drone's height from ``/tf``
transforms whose child frame is ``--drone-body`` instead of the mocap topics.
It exists for the crazyswarm2 simulation backend, which has no motion capture
and broadcasts each simulated drone as ``world -> <name>`` on /tf. It is the
simulator's own state, so it is ground truth in simulation and meaningless on
hardware, where the default stays mocap.

Exit 0 = the drone flew. Exit 1 = it did not (or there is no ground truth to
say so, which is equally unusable as a trial).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


import numpy as np
from ...bag_io_utils import read_messages

GT_TOPICS = ("/poses", "/poses_full")
TF_TOPICS = ("/tf",)


def heights(bag: Path, body: str, source: str) -> list:
    """Every ground-truth z of ``body`` in the bag, from mocap or from /tf."""
    z = []
    topics = TF_TOPICS if source == "tf" else GT_TOPICS
    for topic, msg in read_messages(bag, topics=topics):
        if topic not in topics:
            continue
        if source == "tf":
            for t in getattr(msg, "transforms", []):
                if t.child_frame_id == body:
                    z.append(t.transform.translation.z)
        else:
            for named in getattr(msg, "poses", []):
                if named.name == body:
                    z.append(named.pose.position.z)
    return z


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bag", type=Path)
    ap.add_argument("--drone-body", default=None,
                    help="the mocap rigid body or tf frame of the drone (default: trials.yaml "
                         "record_profiles.cf_name, else cf1)")
    ap.add_argument("--min-climb", type=float, default=0.15,
                    help="metres of vertical travel that count as a flight")
    ap.add_argument("--pose-source", choices=["mocap", "tf"], default="mocap",
                    help="mocap (default): /poses and /poses_full. tf: the simulator's "
                         "world -> <drone> transforms on /tf (crazyswarm2 sim backend only)")
    args = ap.parse_args(argv)

    if args.drone_body is None:
        from ...record_profiles import default_cf_name

        args.drone_body = default_cf_name() or "cf1"
    z = heights(args.bag, args.drone_body, args.pose_source)
    topics = TF_TOPICS if args.pose_source == "tf" else GT_TOPICS

    if len(z) < 10:
        print(f"VERIFY-FAIL: only {len(z)} ground-truth sample(s) for "
              f"{args.drone_body!r} on {'/'.join(topics)} — cannot show the drone flew")
        return 1

    z = np.asarray(z)
    climb = float(z.max() - z.min())
    if climb < args.min_climb:
        print(f"VERIFY-FAIL: the drone never left the ground — vertical travel "
              f"{climb * 1000:.0f} mm over {len(z)} samples (need "
              f"{args.min_climb * 1000:.0f} mm), z stayed at {np.median(z):.2f} m.\n"
              f"  Most likely: a latched e-stop (/all/emergency latches in firmware — "
              f"POWER-CYCLE the drone, then restart the rig), a refused arming, "
              f"or a dead radio link.")
        return 1

    print(f"VERIFY-OK: the drone flew — vertical travel {climb * 1000:.0f} mm, "
          f"peak z {z.max():.2f} m over {len(z)} samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

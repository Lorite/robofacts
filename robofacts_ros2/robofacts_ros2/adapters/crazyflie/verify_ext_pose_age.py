#!/usr/bin/env python3
"""Post-flight check: were the external poses the drone fused fresh while it flew?

Born 2026-10-14, from a day of team flights on 2026-10-07. ``crazyflie_server``
logged ``Motion capture rate off`` warnings that read like 1.2 s and 7 s pose
gaps. The bag of one of those trials proved them a bookkeeping artifact of the
server: ``/poses``, ``/poses_full`` and ``/poses_full_gt`` had no
gap over 100 ms at about 110 Hz, and the drone's own ``ext_pose_age`` had a
maximum of 2 ms over 237 samples. So the server's warning is never evidence of
pose loss, and the drone's own report is the evidence this check reads.

What the topic is. The project's crazyswarm2 ``crazyflies.yaml`` declares a
``custom_topics: ext_pose_age`` log block with ``locSrv.ageMs`` as its first
variable, published as ``/<drone>/ext_pose_age``
(``crazyflie_interfaces/msg/LogDataGeneric``, about 10 Hz). This needs a firmware
build that sets ``locSrv.ageMs`` to the age, in milliseconds, of the last external
pose the drone received, as the host stamped it at transmit (the check was written
against a modified firmware, not the stock one). A value above
the threshold means the drone was steering on a stale pose (a slow estimator, a
held pose, a latency spike). The value does not grow when no packet arrives at
all: radio loss of fresh packets is ``verify_broadcast_sag.py``'s question, and a
gap in the ground truth is ``verify_flight_happened.py``'s.

What it judges. Only the flight window, taken from the drone's own
``/<drone>/status`` IS_FLYING bit on the recorder's clock. A stale value on the
pads before takeoff is not a flight fault. A drone with no status topic is judged
over the whole bag, and the output says so. A drone whose status never reports
IS_FLYING is not judged (that it did not fly is ``verify_flight_happened``'s
verdict).

    verify_ext_pose_age.py BAG [--max-age-ms 200] [--expected-latency-ms 0]
                               [--min-consecutive 3]
                               [--require NAME ...] [--require-when-airborne NAME ...]
                               [--required-from YYYY-MM-DD] [--backend cpp|sim]
                               [--summary-json]

Threshold. ``--max-age-ms`` and ``--min-consecutive`` are declared by the
experiment. ``--expected-latency-ms`` adds a latency the trial injects on purpose
(an experiment's ``injected_latency_ms`` level that holds each pose for
100 or 180 ms, and the firmware duly reads 102 to 186 ms), so a deliberate
condition is not read as pose loss. With vision in the loop the age is the
pipeline's own latency (280 to 340 ms with a camera on a quadruped, 2026-09-12), so such a
campaign must declare its own threshold rather than inherit the mocap default.

Requirements. ``--require NAME`` fails the trial when the bag holds no
``/NAME/ext_pose_age`` sample. ``--require-when-airborne NAME`` asks for it only
when NAME's status reports IS_FLYING (a static trial with the drone unpowered
needs no topic). Without either, a missing topic is reported as "not recorded"
and never fails. ``--required-from`` waives every requirement for a bag that
started before that date (UTC), so ``reverify_trials.py`` does not abort trials
flown before the experiment enabled the log block. ``--backend sim`` waives them
too: the crazyswarm2 simulation backend has no firmware to log the age.

``--summary-json`` prints one JSON line with the per-drone numbers and exits 0,
for a ``metadata_probes`` entry with ``json: true``: the trial metadata then
carries the same numbers as the run log.

Exit 0 = every judged drone had fresh poses while airborne, and every required
drone recorded the topic. Exit 1 = otherwise.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TOPIC_SUFFIX = "/ext_pose_age"
STATUS_SUFFIX = "/status"
#: crazyflie_interfaces/msg/Status SUPERVISOR_INFO_IS_FLYING, used when the
#: message class does not carry the constant.
IS_FLYING_BIT = 16

DEFAULT_MAX_AGE_MS = 200.0
DEFAULT_MIN_CONSECUTIVE = 3

Sample = Tuple[float, float]          # (receive time [s], age [ms])
Flag = Tuple[float, bool]             # (receive time [s], is_flying)


# --------------------------------------------------------------------------- #
# Pure logic (no ROS): what the tests pin
# --------------------------------------------------------------------------- #
def flying_intervals(flags: Sequence[Flag]) -> List[Tuple[float, float]]:
    """Half-open intervals [start, end) during which IS_FLYING was set.

    An interval opens at the first sample that reports flying and ends at the
    first later sample that does not (that sample is already on the ground). A
    bag that ends in flight closes at infinity, so its last age samples count.
    """
    out: List[Tuple[float, float]] = []
    start: Optional[float] = None
    for t, flying in sorted(flags):
        if flying and start is None:
            start = t
        elif not flying and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, math.inf))
    return out


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


@dataclass
class DroneVerdict:
    name: str
    recorded: bool                    # any ext_pose_age sample in the bag
    required: bool                    # a requirement applied to this drone
    window: str                       # "airborne", "whole_bag", "not_airborne", "none"
    n_bag: int = 0
    n_window: int = 0
    max_ms: Optional[float] = None
    median_ms: Optional[float] = None
    longest_stale_samples: int = 0
    longest_stale_s: float = 0.0
    stale_start_s: Optional[float] = None   # bag-relative start of the longest stale run
    max_sample_gap_s: Optional[float] = None
    failed: bool = False
    reason: str = ""

    def as_summary(self) -> Dict[str, object]:
        out: Dict[str, object] = {"recorded": self.recorded, "window": self.window,
                                  "n": self.n_window}
        if self.max_ms is not None:
            out.update(max_ms=self.max_ms, median_ms=self.median_ms,
                       longest_stale_samples=self.longest_stale_samples)
        if self.max_sample_gap_s is not None:
            out["max_sample_gap_s"] = round(self.max_sample_gap_s, 3)
        out["ok"] = not self.failed
        if self.reason:
            out["reason"] = self.reason
        return out


def judge_drone(name: str, ages: Sequence[Sample], flags: Sequence[Flag], *,
                max_age_ms: float, min_consecutive: int, required: bool,
                require_when_airborne: bool, t0: Optional[float] = None) -> DroneVerdict:
    """The verdict for one drone. ``required`` means always, ``require_when_airborne``
    means only if its status reports IS_FLYING. ``t0`` makes reported times bag-relative."""
    ages = sorted(ages)
    intervals = flying_intervals(flags)
    airborne = bool(intervals)
    must_have = required or (require_when_airborne and airborne)

    if not flags:
        window = "whole_bag"
        in_window = list(ages)
        segments = [in_window]
    elif airborne:
        window = "airborne"
        segments = [[s for s in ages if lo <= s[0] < hi] for lo, hi in intervals]
        in_window = [s for seg in segments for s in seg]
    else:
        window = "not_airborne"
        in_window, segments = [], []

    v = DroneVerdict(name=name, recorded=bool(ages), required=must_have, window=window,
                     n_bag=len(ages), n_window=len(in_window))
    if not ages:
        v.window = "none"
        if must_have:
            v.failed = True
            v.reason = (f"/{name}{TOPIC_SUFFIX} is required but has no sample in the bag "
                        f"(log block missing from the crazyflies yaml, a stock firmware, "
                        f"or the topic left out of the record list)")
        else:
            v.reason = "not recorded"
        return v

    judged = in_window if in_window else []
    if judged:
        values = [a for _, a in judged]
        v.max_ms = float(max(values))
        v.median_ms = float(_median(values))
        gaps = [b[0] - a[0] for seg in segments for a, b in zip(seg, seg[1:])]
        v.max_sample_gap_s = max(gaps) if gaps else None
        t_ref = t0 if t0 is not None else ages[0][0]
        for seg in segments:
            run: List[Sample] = []
            for s in seg + [(math.inf, -math.inf)]:     # sentinel closes the last run
                if s[1] > max_age_ms:
                    run.append(s)
                    continue
                if len(run) > v.longest_stale_samples:
                    v.longest_stale_samples = len(run)
                    v.longest_stale_s = run[-1][0] - run[0][0]
                    v.stale_start_s = run[0][0] - t_ref
                run = []
        if v.longest_stale_samples >= min_consecutive:
            v.failed = True
            v.reason = (f"pose age above {max_age_ms:.0f} ms for {v.longest_stale_samples} "
                        f"consecutive samples ({v.longest_stale_s:.2f} s from "
                        f"t+{v.stale_start_s:.1f} s), max {v.max_ms:.0f} ms: the drone "
                        f"steered on stale external poses")
    elif window == "airborne" and must_have:
        v.failed = True
        v.reason = (f"/{name}{TOPIC_SUFFIX} is recorded but has no sample while the drone "
                    f"was airborne (the log downlink stalled for the whole flight)")
    elif window == "airborne":
        v.reason = "no sample while airborne"
    else:
        v.reason = "never reported IS_FLYING: not judged"
    return v


def requirements_waived(bag_start: Optional[dt.datetime], required_from: Optional[dt.date],
                        backend: Optional[str]) -> Optional[str]:
    """Why every requirement is waived for this bag, or None when they apply."""
    if backend and backend.strip().lower() == "sim":
        return "backend sim: the crazyswarm2 simulation backend logs no firmware variables"
    if required_from is not None and bag_start is not None and bag_start.date() < required_from:
        return (f"the bag started {bag_start.date().isoformat()}, before the declared "
                f"requirement date {required_from.isoformat()}")
    return None


def evaluate(ages: Dict[str, List[Sample]], flags: Dict[str, List[Flag]], *,
             max_age_ms: float = DEFAULT_MAX_AGE_MS,
             min_consecutive: int = DEFAULT_MIN_CONSECUTIVE,
             require: Iterable[str] = (), require_when_airborne: Iterable[str] = (),
             waived: Optional[str] = None) -> List[DroneVerdict]:
    """One verdict per drone that recorded the topic or is named by a requirement."""
    require = [] if waived else list(require)
    when_air = [] if waived else list(require_when_airborne)
    names = sorted(set(ages) | set(require) | set(when_air))
    starts = [s[0] for series in ages.values() for s in series]
    t0 = min(starts) if starts else None
    return [judge_drone(n, ages.get(n, []), flags.get(n, []), max_age_ms=max_age_ms,
                        min_consecutive=min_consecutive, required=n in require,
                        require_when_airborne=n in when_air, t0=t0)
            for n in names]


def report(verdicts: Sequence[DroneVerdict], *, max_age_ms: float, min_consecutive: int,
           waived: Optional[str] = None) -> Tuple[int, List[str]]:
    """(exit code, printed lines). The FIRST line is the verdict, self-contained,
    because ``reverify_trials.py`` quotes only the first line of a failing step.
    The per-drone lines follow, few enough that the runner's six-line tail of a
    failing step (copied into the metadata) still holds the verdict."""
    details: List[str] = []
    if waived:
        details.append(f"  requirements waived: {waived}")
    for v in verdicts:
        if v.max_ms is not None:
            gap = f", longest sample gap {v.max_sample_gap_s:.2f} s" if v.max_sample_gap_s else ""
            details.append(f"  {v.name}: {v.window}, n={v.n_window}, max {v.max_ms:.0f} ms, "
                           f"median {v.median_ms:.0f} ms, longest run over {max_age_ms:.0f} ms "
                           f"{v.longest_stale_samples} sample(s){gap}"
                           + (" -> FAIL" if v.failed else ""))
        else:
            details.append(f"  {v.name}: {v.reason}" + (" -> FAIL" if v.failed else ""))
    failed = [v for v in verdicts if v.failed]
    judged = [v for v in verdicts if v.max_ms is not None]
    if not verdicts:
        verdict = "VERIFY-OK: no */ext_pose_age topic in the bag and none required (not recorded)"
    elif failed:
        verdict = "VERIFY-FAIL: " + "; ".join(f"{v.name}: {v.reason}" for v in failed)
    elif judged:
        verdict = ("VERIFY-OK: on-board pose age " + ", ".join(
            f"{v.name} max {v.max_ms:.0f} ms median {v.median_ms:.0f} ms" for v in judged)
            + f" (threshold {max_age_ms:.0f} ms for {min_consecutive} consecutive samples)")
    else:
        verdict = "VERIFY-OK: nothing to judge (" + "; ".join(
            f"{v.name}: {v.reason}" for v in verdicts) + ")"
    return (1 if failed else 0), [verdict] + details


# --------------------------------------------------------------------------- #
# Bag access
# --------------------------------------------------------------------------- #
def _drone_of(topic: str, suffix: str) -> Optional[str]:
    """``/<drone>/<suffix>`` -> drone, for exactly one namespace level."""
    if not topic.endswith(suffix):
        return None
    name = topic[1:-len(suffix)] if topic.startswith("/") else ""
    return name if name and "/" not in name else None


def read_series(bag: Path) -> Tuple[Dict[str, List[Sample]], Dict[str, List[Flag]]]:
    """Every drone's ext_pose_age samples and IS_FLYING flags, on the recorder's clock."""
    from robofacts_ros2.bag_io_utils import read_messages_with_time

    ages: Dict[str, List[Sample]] = {}
    flags: Dict[str, List[Flag]] = {}

    def wanted(topic: str) -> bool:
        return bool(_drone_of(topic, TOPIC_SUFFIX) or _drone_of(topic, STATUS_SUFFIX))

    for topic, msg, t in read_messages_with_time(bag, topics=wanted):
        name = _drone_of(topic, TOPIC_SUFFIX)
        if name is not None:
            values = list(getattr(msg, "values", []) or [])
            if values:                          # locSrv.ageMs is the block's first variable
                ages.setdefault(name, []).append((t, float(values[0])))
            continue
        name = _drone_of(topic, STATUS_SUFFIX)
        if name is not None and hasattr(msg, "supervisor_info"):
            bit = getattr(type(msg), "SUPERVISOR_INFO_IS_FLYING", IS_FLYING_BIT)
            flags.setdefault(name, []).append((t, bool(msg.supervisor_info & bit)))
    return ages, flags


def bag_start_utc(bag: Path) -> Optional[dt.datetime]:
    """When the recording started, from the bag's metadata.yaml (UTC), or None."""
    try:
        import yaml

        info = yaml.safe_load((bag / "metadata.yaml").read_text(encoding="utf-8"))
        ns = int(info["rosbag2_bagfile_information"]["starting_time"]["nanoseconds_since_epoch"])
    except Exception:  # noqa: BLE001 - an unreadable date keeps the requirement
        return None
    return dt.datetime.fromtimestamp(ns * 1e-9, tz=dt.timezone.utc)


def _names(values: Sequence[str]) -> List[str]:
    """Repeatable and comma-separated: ``--require a,b --require c``."""
    return [n.strip() for v in values for n in str(v).split(",") if n.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bag", type=Path)
    ap.add_argument("--max-age-ms", type=float, default=DEFAULT_MAX_AGE_MS,
                    help="an on-board pose age above this is stale (default 200 ms)")
    ap.add_argument("--expected-latency-ms", type=float, default=0.0,
                    help="a latency the trial injects ON PURPOSE (a factor level, e.g. "
                         "{injected_latency_ms}), added to --max-age-ms: a deliberate "
                         "condition is not pose loss")
    ap.add_argument("--min-consecutive", type=int, default=DEFAULT_MIN_CONSECUTIVE,
                    help="stale samples in a row that fail the trial (default 3, "
                         "about 0.3 s at the 10 Hz log rate)")
    ap.add_argument("--require", action="append", default=[], metavar="NAME",
                    help="a drone that must have recorded /NAME/ext_pose_age "
                         "(repeatable, or comma-separated)")
    ap.add_argument("--require-when-airborne", action="append", default=[], metavar="NAME",
                    help="required only if NAME's status reports IS_FLYING")
    ap.add_argument("--required-from", type=dt.date.fromisoformat, default=None,
                    metavar="YYYY-MM-DD",
                    help="waive the requirements for bags that started before this "
                         "date (UTC), recorded before the log block was enabled")
    ap.add_argument("--backend", default=None,
                    help="the crazyswarm2 backend of the trial ('sim' waives the requirements)")
    ap.add_argument("--summary-json", action="store_true",
                    help="print one JSON line with the per-drone numbers and exit 0 "
                         "(for a metadata probe with json: true)")
    args = ap.parse_args(argv)

    if not (args.bag / "metadata.yaml").is_file() and not any(args.bag.glob("*.mcap")) \
            and not any(args.bag.glob("*.db3")):
        print(f"VERIFY-FAIL: {args.bag} is not a bag directory")
        return 0 if args.summary_json else 1

    threshold = args.max_age_ms + max(0.0, args.expected_latency_ms)
    waived = requirements_waived(bag_start_utc(args.bag), args.required_from, args.backend)
    ages, flags = read_series(args.bag)
    verdicts = evaluate(ages, flags, max_age_ms=threshold,
                        min_consecutive=args.min_consecutive,
                        require=_names(args.require),
                        require_when_airborne=_names(args.require_when_airborne),
                        waived=waived)
    rc, lines = report(verdicts, max_age_ms=threshold,
                       min_consecutive=args.min_consecutive, waived=waived)
    if args.summary_json:
        summary = {"max_age_ms": threshold, "min_consecutive": args.min_consecutive,
                   "expected_latency_ms": args.expected_latency_ms, "ok": rc == 0,
                   "drones": {v.name: v.as_summary() for v in verdicts}}
        if waived:
            summary["requirements_waived"] = waived
        print(json.dumps(summary, sort_keys=True))
        return 0
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

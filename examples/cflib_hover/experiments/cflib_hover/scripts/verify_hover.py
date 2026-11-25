#!/usr/bin/env python3
"""The verify step of the cflib example: did the drone leave the ground, hover, and come down?

    verify_hover.py <recording> [--min-airborne-s 3] [--airborne-m 0.15] [--landed-m 0.10]

It reads the recording's copy of the flight log (data/flight.jsonl). The action's exit code
cannot show a flight: a refusal before arming is a clean exit of the script, and only the
recorded heights show that the drone flew. The height is the downward range sensor, else
the estimate. A non-zero exit marks the trial aborted, and it does not count.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Tuple


def heights(log: Path) -> List[Tuple[float, float]]:
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("kind") == "state":
            h = row.get("range_m", row.get("z"))
            if h is not None:
                out.append((float(row["t"]), float(h)))
    return out


def airborne_s(samples: List[Tuple[float, float]], above_m: float) -> float:
    """The longest stretch of time with the height above ``above_m``."""
    best, start = 0.0, None
    for t, h in samples:
        if h > above_m:
            start = t if start is None else start
            best = max(best, t - start)
        else:
            start = None
    return best


def judge(log: Path, min_airborne_s: float, airborne_m: float, landed_m: float) -> Tuple[bool, str]:
    if not log.is_file():
        return False, f"no flight log at {log}"
    samples = heights(log)
    if not samples:
        return False, f"no state sample in {log}"
    up = airborne_s(samples, airborne_m)
    if up < min_airborne_s:
        return False, (f"airborne {up:.1f} s above {airborne_m} m, need {min_airborne_s} s "
                       f"(max height {max(h for _, h in samples):.2f} m)")
    if samples[-1][1] > landed_m:
        return False, f"the last height is {samples[-1][1]:.2f} m: the recording ends in the air"
    return True, f"airborne {up:.1f} s above {airborne_m} m, landed ({samples[-1][1]:.2f} m)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recording", type=Path)
    ap.add_argument("--min-airborne-s", type=float, default=3.0)
    ap.add_argument("--airborne-m", type=float, default=0.15)
    ap.add_argument("--landed-m", type=float, default=0.10)
    args = ap.parse_args(argv)
    ok, why = judge(args.recording / "data" / "flight.jsonl", args.min_airborne_s,
                    args.airborne_m, args.landed_m)
    print(f"{'PASS' if ok else 'FAIL'}: {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

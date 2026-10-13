#!/usr/bin/env python3
"""The operator stop latch: its one freshness rule, the setup step and the T-0 gate check.

Every robots.yaml stop command writes the stop latch (``latch_path()``) first, and
the flight actions refuse to START over a latch younger than 120 s (issue
#100). They check it only once they run, which is after the stack, the T-0 gate
and the recorder are already up: a stopped rig was brought up and recorded
for nothing, and the operator learned it a minute late. As the first setup
step this refuses before anything is launched (2026-10-02, lab-day sim test).

THE ONE RULE (2026-10-12, SAC 2027 framework paper). On the lab day of
2026-10-07 the T-0 gate passed 62 s after a latch was written, because only the
setup step and the action's start read the latch, and the gate in between did
not. The gate now carries the same check (``checks/stop_latch.sh`` calls this
script with ``--gate``), so a fresh latch refuses before the operator is asked
to pose the rig. ``STOP_LATCH_MAX_AGE_S`` below is the single source of the
120 s: the setup step, the gate check and the three flight actions
(team_hover_square.py, companion_hover.py, observer_trial_action.py) all import
or call it, so the rule cannot be spelled twice.

    stop_latch_clear.py [--latch /tmp/trial_stop_latch] [--max-age-s 120]
    stop_latch_clear.py --gate [--latch PATH]     # one PASS/FAIL line, for the gate

Exit 0 when there is no latch or it is older than the limit, 1 otherwise,
with the latch's last line and the command that acknowledges it. In ``--gate``
mode the line carries ``age_s=<seconds>`` (``age_s=none`` without a latch), the
measured value the gate log and the trial metadata keep.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

#: A latch younger than this refuses a trial: at setup, at the T-0 gate and at
#: the action's start. Covers the runner's T-0 and pose pause, where a stop can
#: fire before the action process exists.
STOP_LATCH_MAX_AGE_S = 120.0

#: Written FIRST by every robots.yaml stop command, in the rig container. The
#: package default; a project declares its own path as `stop_latch:` in trials.yaml
#: (the lab did, because its robots.yaml and flight actions name that path), and
#: $TRIALS_STOP_LATCH overrides both (the hardware-free tests only, never on the rig).
DEFAULT_STOP_LATCH = "/tmp/trial_stop_latch"
ENV_STOP_LATCH = "TRIALS_STOP_LATCH"


def latch_path() -> Path:
    """$TRIALS_STOP_LATCH, else trials.yaml `stop_latch`, else the package default."""
    explicit = os.environ.get(ENV_STOP_LATCH)
    if explicit:
        return Path(explicit)
    roots = _roots()
    configured = roots.setting("stop_latch") if roots is not None else None
    return Path(configured or DEFAULT_STOP_LATCH)


def _roots():
    """The package's roots module, also when this file runs as a plain script
    (``python3 .../stop_latch_clear.py``, as the stop_latch check does)."""
    try:
        if __package__:
            from . import roots
        else:  # pragma: no cover
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from declarative_robot_trials import roots  # type: ignore[no-redef]
        return roots
    except ImportError:
        return None


def latch_age(path: Path, now: float) -> Optional[float]:
    """Seconds since the latch was last written, or None when there is none."""
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def latch_reason(path: Path) -> str:
    try:
        lines = path.read_text(errors="replace").strip().splitlines()
    except OSError:
        lines = []
    return lines[-1] if lines else "no reason recorded"


def fresh_latch(path: Path, max_age_s: float, now: float) -> str:
    """The latch's reason when it is fresh, else ''."""
    age = latch_age(path, now)
    if age is None or age > max_age_s:
        return ""
    return f"{latch_reason(path)} ({age:.0f} s ago)"


def gate_line(path: Path, now: float, max_age_s: float = STOP_LATCH_MAX_AGE_S) -> tuple[int, str]:
    """(exit code, one PASS/FAIL line) for the T-0 gate check."""
    age = latch_age(path, now)
    if age is None:
        return 0, f"PASS: no operator stop latch at {path} age_s=none"
    if age > max_age_s:
        return 0, (f"PASS: operator stop latch at {path} is {age:.0f} s old, older than "
                   f"{max_age_s:.0f} s age_s={age:.1f}")
    return 1, (f"FAIL: operator stop latch at {path} is {age:.0f} s old (< {max_age_s:.0f} s): "
               f"{latch_reason(path)}. Make the rig safe, then rm {path} age_s={age:.1f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--latch", type=Path, default=None,
                    help=f"default: $TRIALS_STOP_LATCH or {DEFAULT_STOP_LATCH}")
    ap.add_argument("--max-age-s", type=float, default=STOP_LATCH_MAX_AGE_S,
                    help=f"default: STOP_LATCH_MAX_AGE_S = {STOP_LATCH_MAX_AGE_S:.0f}")
    ap.add_argument("--gate", action="store_true",
                    help="print one PASS/FAIL line with age_s=<s> for the T-0 gate check")
    args = ap.parse_args(argv)
    latch = args.latch or latch_path()
    if args.gate:
        rc, line = gate_line(latch, time.time(), args.max_age_s)
        print(line, flush=True)
        return rc
    why = fresh_latch(latch, args.max_age_s, time.time())
    if why:
        print(f"REFUSED: the operator stop latch is fresh: {why}. Nothing was launched. "
              f"Make the rig safe, then rm {latch}", flush=True)
        return 1
    print(f"stop latch clear ({latch})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

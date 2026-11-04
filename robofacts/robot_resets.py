"""Per-robot reset steps, declared once in ``common/config/robots.yaml`` (2026-11-04).

Why. A robot can carry state from the previous session into the next trial, and the stack then
fails in ways that look like something else. On 2026-11-04 the crazyswarm2 server hung twice at
the first trial of a run because the Crazyflie still streamed the log blocks of the previous
session ("Received unrequested data for block: 6", no parameter TOC, killed after 50 s), while the
repeat trials, which ran a campaign's radio power cycle first, never hung. The reset belongs to the
robot, not to one campaign: every experiment that flies that robot needs it.

How. ``robots.yaml`` gives a robot an optional ``reset:`` list, next to its ``estop``::

    crazyflie:
      reset:
        - name: cf_radio_power_cycle
          command: python3 scripts/crazyflie_bench/cf_power_cycle_check.py 1
          when: always           # always (default) or repeat; first is allowed too
          required: true         # default true: a failing reset blocks the trial
        - name: aideck_access_point_back
          command: bash -c '...'
          only_with: aideck      # only for experiments that declare this feature

An experiment declares the robots it uses in ``config/runner.yaml``::

    robots: [crazyflie]                                   # or
    robots: [{name: crazyflie, with: [aideck]}]           # features for only_with
    robots: [{name: crazyflie, reset: false}]             # the experiment opts out

and a campaign opts out with ``robot_reset: false``. The runner runs the resets after
runner.yaml's own setup steps and the pre-launch checks, and before the campaign's setup steps and
the stack launch, filtered by ``when`` like every setup step. Commands run on the trial's host from
the project root, with the same ``{factor}`` and ``{arg_*}`` placeholders as setup steps.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROBOTS_FILE = Path("common") / "config" / "robots.yaml"
STEP_WHEN = ("always", "first", "repeat")


class RobotResetError(ValueError):
    """A robots declaration or a reset entry that cannot be used, with the reason."""


def parse_declaration(raw: Any, where: str) -> List[Dict[str, Any]]:
    """runner.yaml's ``robots:`` as ``[{name, with, reset}]`` (empty when absent)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RobotResetError(f"{where}: robots must be a list of names or {{name, with, reset}}")
    out = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str) and entry.strip():
            out.append({"name": entry.strip(), "with": [], "reset": True})
        elif isinstance(entry, dict) and str(entry.get("name", "")).strip():
            features = entry.get("with") or []
            if not isinstance(features, list):
                raise RobotResetError(f"{where}: robots[{i}].with must be a list of feature names")
            out.append({"name": str(entry["name"]).strip(), "with": [str(f) for f in features],
                        "reset": bool(entry.get("reset", True))})
        else:
            raise RobotResetError(f"{where}: robots[{i}] must be a name or a mapping with a name")
    return out


def load_robots_file(experiments_root: Path) -> Dict[str, Any]:
    path = Path(experiments_root) / ROBOTS_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(data.get("robots") or {})


def reset_steps(experiments_root: Path, declaration: List[Dict[str, Any]], where: str
                ) -> List[Dict[str, Any]]:
    """The reset steps of the declared robots, ``{name, command, required, when, robot}``."""
    if not declaration:
        return []
    robots = load_robots_file(experiments_root)
    steps: List[Dict[str, Any]] = []
    for decl in declaration:
        robot = robots.get(decl["name"])
        if robot is None:
            raise RobotResetError(f"{where}: robot {decl['name']!r} is not in {ROBOTS_FILE} "
                                  f"(known: {', '.join(sorted(robots)) or 'none'})")
        if not decl["reset"]:
            continue
        for i, entry in enumerate(robot.get("reset") or []):
            label = f"{ROBOTS_FILE}: {decl['name']}.reset[{i}]"
            if not isinstance(entry, dict) or not str(entry.get("command", "")).strip():
                raise RobotResetError(f"{label} needs a non-empty command")
            when = str(entry.get("when") or "always")
            if when not in STEP_WHEN:
                raise RobotResetError(f"{label}: when must be one of {', '.join(STEP_WHEN)}, got {when!r}")
            only = entry.get("only_with")
            if only and str(only) not in decl["with"]:
                continue
            steps.append({"name": str(entry.get("name", f"{decl['name']}_reset_{i}")),
                          "command": str(entry["command"]).strip(),
                          "required": bool(entry.get("required", True)),
                          "when": when, "robot": decl["name"]})
    return steps

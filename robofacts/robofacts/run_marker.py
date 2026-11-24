"""The "a trial is running here" marker (2026-10-22).

While the runner executes a trial or a campaign (never a dry run), the file
``<experiments root>/.robofacts_trial_running.json`` exists. Tools that move
data off a trial host read it and wait: a copy must not compete with a flight
for the rig's Wi-Fi, disk or CPU. The marker covers a whole campaign, the gaps
between its trials included, because the next trial may start a second later.

The file holds the host, the process id, the experiment, the label and the
start time. A runner killed hard (``kill -9``, a power cut) leaves it behind,
so a reader treats a marker older than :data:`STALE_AFTER_S` as stale, and the
next run replaces it. Writing or removing it is never fatal to a trial.

Shell readers (the robotics repo's scripts/archive/upload_trials.sh)::

    find <root> -maxdepth 1 -name .robofacts_trial_running.json -mmin -720
"""
from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

MARKER_NAME = ".robofacts_trial_running.json"
#: A marker older than this (12 h) is a leftover of a killed runner.
STALE_AFTER_S = 12 * 3600

_depth: Dict[Path, int] = {}


def marker_path(experiments_root: Path) -> Path:
    return Path(experiments_root) / MARKER_NAME


def read(experiments_root: Path, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The live marker's content, or None when there is none or it is stale."""
    path = marker_path(experiments_root)
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
        if age > STALE_AFTER_S:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_live(experiments_root: Path, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Like :func:`read`, and also None when the marker's runner is gone (2026-11-16).

    A marker written on this machine whose process no longer exists is a leftover of a killed
    runner, even when it is younger than 12 hours. The stop must not follow such a marker. A
    marker written on another machine (a runner inside a container) cannot be checked here and
    counts while it is fresh.
    """
    record = read(experiments_root, now=now)
    if record and record.get("host") == socket.gethostname():
        try:
            os.kill(int(record.get("pid")), 0)
        except ProcessLookupError:
            return None
        except (PermissionError, TypeError, ValueError):
            pass
    return record


def note(experiments_root: Path, **info: Any) -> None:
    """Add or replace fields of the marker this process holds (2026-11-16).

    The runner notes each trial's ``execution_host``, the robots its experiment declares and
    whether it is a simulation, so a stop typed on this machine (``estop.sh``) reaches the
    host that runs the stack. A marker held by no block of this process is left alone.
    Never fatal to a trial.
    """
    root = Path(experiments_root)
    if _depth.get(root, 0) == 0:
        return
    path = marker_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(info)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError) as exc:
        print(f"[runner] could not update {path} ({exc}) - continuing")


@contextmanager
def running(experiments_root: Path, **info: Any) -> Iterator[None]:
    """Hold the marker for the duration of the block. Nested blocks (a campaign
    and its trials) share the outermost marker."""
    root = Path(experiments_root)
    path = marker_path(root)
    outer = _depth.get(root, 0) == 0
    _depth[root] = _depth.get(root, 0) + 1
    if outer:
        record = {"host": socket.gethostname(), "pid": os.getpid(),
                  "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            print(f"[runner] could not write {path} ({exc}) - continuing")
    try:
        yield
    finally:
        _depth[root] -= 1
        if _depth[root] == 0:
            del _depth[root]
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"[runner] could not remove {path} ({exc}) - continuing")

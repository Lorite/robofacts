#!/usr/bin/env python3
"""Composition driver for the modular pre-flight check framework (#87).

Reads an experiment's ``config/preflight_checklist.json``, runs the machine
check referenced by each machine-checkable item, and fails atomically: either
every check passes or the flight is a no-go.

Why the checklist file drives this
----------------------------------
Each experiment already carries a human-readable checklist. Keeping a *separate*
list of machine checks guarantees the two drift apart, and a checklist that no
longer matches what is actually verified is worse than no checklist at all. So
the JSON is the single source of truth: a checklist item either references a
check script (machine-verified) or is explicitly manual (operator confirms it).
``--validate`` enforces that every reference resolves, and is run by the test
suite so drift breaks CI rather than a flight.

Checklist schema (backward compatible with the plain-string format)
-------------------------------------------------------------------
``{"checklist": [ <entry>, ... ]}`` where each entry is either

* a plain string - a manual item, exactly as before, or
* an object::

      {
        "text": "[HARD] MOCAP server running and streaming",
        "check": "mocap_stream",
        "args": {"topic": "/poses", "min_hz": 85}
      }

  ``check`` names a script in ``experiments/common/preflight/checks/`` (without
  the ``.sh``). ``args`` become ``key=value`` arguments. Values may contain
  ``${var}`` placeholders filled from ``--var name=value``, so one checklist
  serves any drone/camera name. An object without ``check`` is a manual item.

  ``"parallel": false`` makes a machine entry run ALONE, at its place in the
  checklist (see "Concurrent machine checks" below). Without the field the
  check script's own classification decides (``PARALLEL_SAFE_CHECKS``).

Concurrent machine checks (2026-10-13, SAC 2027 framework paper)
----------------------------------------------------------------
Most checks are read-only probes that wait on a measurement window (a 5 s or
10 s topic rate), and until this change they ran one after another, so a
hardware gate took about a minute (lab day 2026-10-07). In ``concurrent`` mode
(the default) consecutive entries that may run in parallel form one batch whose
checks run together in a thread pool. An entry that may not runs alone, after
every earlier entry has finished and before any later one starts, so it is
also an ordering barrier (the PX4 ``/fmu`` check that must start after the
ground-truth window, 2026-10-01). ``sequential`` mode is the old loop. Both
modes print, return and report exactly the same lines in checklist order, so
the gate log, the trial metadata's ``preflight`` block and the verdict do not
depend on the mode. The operator confirmations still come after every machine
check. The mode comes from ``--concurrency``, else ``$TRIALS_PREFLIGHT_CONCURRENCY``,
else the checklist's top-level ``"concurrency"``, else ``concurrent``.
``$TRIALS_PREFLIGHT_TIMING_LOG`` (a path) appends one JSON line of per-check
start and end times per run, without touching the printed report.

Exit codes: 0 = every machine check passed (clear to arm), 1 = at least one
failed (NO-GO), 2 = usage/schema error (which is also a no-go: a gate that
cannot run has not passed).
"""

from __future__ import annotations

import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
#: The checks this package ships.
CHECKS_DIR = HERE / "checks"
#: The bash helper every check sources (passed to a check as ``$PF_LIB``).
LIB_SH = HERE / "lib" / "common.sh"
#: Where a project keeps its own checks, relative to its experiments root. A
#: project check with the same name as a packaged one wins.
PROJECT_CHECKS_REL = Path("common") / "preflight" / "checks"


def _roots():
    """The package's roots module, or None when this file runs as a plain script
    outside the package (a copy in a test fixture): then only the packaged checks exist."""
    try:
        if __package__:
            from .. import roots
        else:  # pragma: no cover - python3 path/to/run_preflight.py
            from declarative_robot_trials import roots  # type: ignore[no-redef]
        return roots
    except ImportError:
        return None


def project_checks_dir(experiments_root: Optional[Path] = None) -> Optional[Path]:
    """The project's own checks directory, or None when no project is found."""
    roots = _roots()
    if roots is None:
        return None
    try:
        return roots.experiments_root(experiments_root) / PROJECT_CHECKS_REL
    except roots.RootNotFound:
        return None


def check_dirs(experiments_root: Optional[Path] = None) -> List[Path]:
    """Where a check script may live, in lookup order."""
    project = project_checks_dir(experiments_root)
    return [d for d in (project, CHECKS_DIR) if d is not None]


def find_check(name: str, experiments_root: Optional[Path] = None) -> Optional[Path]:
    """The script of the check ``name``: the project's own first, then the package's."""
    for directory in check_dirs(experiments_root):
        script = directory / f"{name}.sh"
        if script.is_file():
            return script
    return None


def check_env() -> Dict[str, str]:
    """The environment a check script runs with: the caller's plus ``PF_LIB``."""
    env = dict(os.environ)
    env.setdefault("PF_LIB", str(LIB_SH))
    return env

# Exit codes used by the check scripts (see lib/common.sh).
RC_PASS, RC_FAIL, RC_USAGE, RC_SKIP = 0, 1, 2, 3

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")

# --------------------------------------------------------------------------- #
# Which checks may run concurrently (2026-10-13), decided per script from its code
# --------------------------------------------------------------------------- #
CONCURRENCY_MODES = ("sequential", "concurrent")
DEFAULT_CONCURRENCY = "concurrent"
CONCURRENCY_ENV = "TRIALS_PREFLIGHT_CONCURRENCY"
TIMING_LOG_ENV = "TRIALS_PREFLIGHT_TIMING_LOG"
#: Upper bound on checks running at once in one batch (each may start a
#: `ros2 topic echo`, which costs a Python interpreter and a DDS participant).
DEFAULT_JOBS = 8

#: Read-only probes: they read files, /proc, sysctl, ping, `gz topic -l`, the
#: ros2 daemon's graph, or subscribe to a topic for a window (a subscription
#: does not change what another subscriber receives). None starts, stops or
#: signals a process, and none shares state with a sibling check.
#: bag_dir_writable writes and removes a probe file whose name carries its own
#: PID, so two instances cannot collide.
PARALLEL_SAFE_CHECKS = frozenset({
    "address_on_interface", "bag_dir_writable", "battery", "camera_frames",
    "dds_socket_buffer", "disk_space", "drone_armable", "ekf_convergence",
    "estop_reachable", "file_exists", "gpu_memory", "graph_nav_localized",
    "gz_world_matches", "host_reachable", "mocap_stream",
    "node_present", "px4_preflight_pass", "ros_param_equals", "rosout_report",
    "stop_latch", "topic_rate", "watchdog_health",
})

#: Checks that must run alone, and why (read from each script's code).
SEQUENTIAL_CHECKS = {
    "ros_clean_slate": (
        "counts matching processes with pgrep and deliberately does not exclude its "
        "siblings (lib/common.sh pf_count_processes), so a concurrent check whose "
        "command line holds the pattern (file_exists path=.../px4_sitl_default/bin/px4) "
        "would count as a leftover"),
    "rigidbody_continuity": (
        "its entries share one capture file per topic (/tmp/pf_rbc_<uid>_<topic>.cap, "
        "2026-08-19): the first entry captures and its siblings reuse the frames, so two "
        "entries at once would truncate each other's capture"),
    "mem_available": (
        "measures MemAvailable, which the concurrent checks' own ros2 CLI processes "
        "consume, so it reads a quiet machine only when it runs alone"),
}


class ChecklistError(Exception):
    """The checklist file is malformed or references something that is absent."""


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #
def load_variables(path: Path) -> Dict[str, str]:
    """Read the checklist's optional ``variables`` block of ``${name}`` defaults.

    Declaring defaults next to the checks that use them means the per-experiment
    wrapper does not have to repeat them, and ``--validate`` can confirm every
    placeholder resolves without the caller supplying anything — so a renamed
    variable fails in the test suite instead of at T-0. ``--var`` overrides.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    variables = data.get("variables") if isinstance(data, dict) else None
    if not isinstance(variables, dict):
        return {}
    return {str(k): str(v) for k, v in variables.items()}


def load_entries(path: Path) -> List[Dict[str, Any]]:
    """Normalise a checklist file into a list of entry dicts.

    Every entry gets ``text``, ``check`` (or None), ``args`` and ``hard``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChecklistError(f"checklist not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ChecklistError(f"invalid JSON in {path}: {exc}") from exc

    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict) and isinstance(data.get("checklist"), list):
        raw = data["checklist"]
    else:
        # Never fall back to an empty list here: a gate with nothing in it would
        # print "CLEAR TO ARM" after checking nothing, which is the worst
        # possible failure mode for this tool.
        raise ChecklistError(f"{path}: expected a list, or an object with a 'checklist' list")

    entries: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            entries.append({"text": text, "check": None, "args": {}, "hard": _is_hard(text),
                            "parallel": None})
            continue

        if not isinstance(item, dict):
            raise ChecklistError(f"{path}: entry {index} is neither a string nor an object")

        text = str(item.get("text", "")).strip()
        if not text:
            raise ChecklistError(f"{path}: entry {index} has no 'text'")

        check = item.get("check")
        if check is not None and not isinstance(check, str):
            raise ChecklistError(f"{path}: entry {index} has a non-string 'check'")

        args = item.get("args", {})
        if not isinstance(args, dict):
            raise ChecklistError(f"{path}: entry {index} has a non-object 'args'")

        parallel = item.get("parallel")
        if parallel is not None and not isinstance(parallel, bool):
            raise ChecklistError(f"{path}: entry {index} has a non-boolean 'parallel'")

        entries.append(
            {
                "text": text,
                "check": check,
                "args": {str(k): str(v) for k, v in args.items()},
                "hard": bool(item.get("hard", _is_hard(text))),
                "parallel": parallel,
            }
        )

    if not entries:
        raise ChecklistError(f"{path}: checklist is empty - refusing to run a gate that checks nothing")
    return entries


def _is_hard(text: str) -> bool:
    return text.upper().startswith("[HARD]")


def load_concurrency(path: Path) -> Optional[str]:
    """The checklist's top-level ``"concurrency"`` (None when absent or unreadable)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("concurrency") if isinstance(data, dict) else None
    return None if value is None else str(value)


def resolve_concurrency(cli: Optional[str], checklist: Path) -> str:
    """--concurrency, else $TRIALS_PREFLIGHT_CONCURRENCY, else the checklist, else default."""
    for source, value in (("--concurrency", cli),
                          (f"${CONCURRENCY_ENV}", os.environ.get(CONCURRENCY_ENV) or None),
                          (f"{checklist} 'concurrency'", load_concurrency(checklist))):
        if value is None:
            continue
        if value not in CONCURRENCY_MODES:
            raise ChecklistError(f"{source}: unknown concurrency {value!r} "
                                 f"(known: {', '.join(CONCURRENCY_MODES)})")
        return value
    return DEFAULT_CONCURRENCY


def runs_in_parallel(entry: Dict[str, Any]) -> bool:
    """May this machine entry share a batch with its neighbours?

    The entry's own ``parallel`` wins. Without it, only a check classified as a
    read-only probe may (an unknown or new check runs alone until someone reads
    its code and adds it to ``PARALLEL_SAFE_CHECKS``). A project's own check
    (``common/preflight/checks/`` of the experiments root) runs alone unless its
    entry, or the factor check that generates it, says ``parallel: true``.
    """
    if entry.get("parallel") is not None:
        return bool(entry["parallel"])
    return entry.get("check") in PARALLEL_SAFE_CHECKS


def plan_batches(machine: List[Dict[str, Any]], concurrency: str) -> List[List[int]]:
    """Indices of ``machine`` grouped into batches that run one after another.

    Sequential: one entry per batch. Concurrent: consecutive parallel entries
    share a batch, and an entry that runs alone is a batch of its own at its
    place in the checklist, so it also orders what comes before and after it.
    """
    if concurrency == "sequential":
        return [[i] for i in range(len(machine))]
    batches: List[List[int]] = []
    current: List[int] = []
    for i, entry in enumerate(machine):
        if runs_in_parallel(entry):
            current.append(i)
            continue
        if current:
            batches.append(current)
            current = []
        batches.append([i])
    if current:
        batches.append(current)
    return batches


def validate(entries: List[Dict[str, Any]], variables: Dict[str, str]) -> List[str]:
    """Return a list of problems; empty means the checklist is coherent."""
    problems: List[str] = []
    for entry in entries:
        check = entry["check"]
        if check is None:
            continue

        script = find_check(check)
        if script is None:
            problems.append(f"{entry['text'][:60]!r} -> unknown check {check!r} "
                            f"(no {check}.sh in {' or '.join(str(d) for d in check_dirs())})")
            continue

        if entry.get("parallel") is True and check in SEQUENTIAL_CHECKS:
            problems.append(f"check {check!r} cannot be declared parallel: "
                            f"{SEQUENTIAL_CHECKS[check]}")

        for key, value in entry["args"].items():
            for name in _PLACEHOLDER.findall(value):
                if name not in variables:
                    problems.append(
                        f"check {check!r} argument {key}={value!r} uses ${{{name}}}, "
                        f"which was not supplied with --var {name}=..."
                    )
    return problems


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def substitute(value: str, variables: Dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), m.group(0)), value)


def run_check(entry: Dict[str, Any], variables: Dict[str, str], timeout_s: float) -> Tuple[int, str]:
    """Run one check script. Returns (exit code, single-line detail)."""
    script = find_check(entry["check"])
    if script is None:
        return RC_USAGE, f"unknown check {entry['check']!r}"
    # Through bash on purpose: a package installed by pip or colcon may not keep
    # the executable bit, and the script's own shebang is bash anyway.
    argv = ["bash", str(script)] + [f"{k}={substitute(v, variables)}" for k, v in entry["args"].items()]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                              env=check_env())
    except subprocess.TimeoutExpired:
        return RC_FAIL, f"check timed out after {timeout_s:.0f}s (treated as FAIL)"
    except OSError as exc:
        return RC_USAGE, f"could not execute {script.name}: {exc}"

    output = (proc.stdout or proc.stderr or "").strip().splitlines()
    detail = output[-1] if output else "(no output)"
    for prefix in ("PASS: ", "FAIL: ", "SKIP: ", "USAGE: "):
        if detail.startswith(prefix):
            detail = detail[len(prefix):]
            break
    return proc.returncode, detail


def run_machine_checks(machine: List[Dict[str, Any]], variables: Dict[str, str],
                       timeout_s: float, concurrency: str = "sequential",
                       jobs: int = DEFAULT_JOBS,
                       on_result: Optional[Callable[[int, int, str], None]] = None,
                       timings: Optional[List[Dict[str, Any]]] = None
                       ) -> List[Tuple[int, str]]:
    """Run every machine entry; return (exit code, detail) per entry, in checklist order.

    ``on_result(i, code, detail)`` is called in checklist order as soon as entry
    ``i`` and every entry before it have finished, so the printed report is the
    same in both modes and still streams. ``timings`` (when given) receives one
    dict per entry: check, batch, start_s, end_s (seconds since the first start).
    """
    results: List[Optional[Tuple[int, str]]] = [None] * len(machine)
    marks: Dict[int, Tuple[float, float]] = {}
    t0 = time.monotonic()

    def _one(i: int) -> Tuple[int, str]:
        start = time.monotonic()
        try:
            return run_check(machine[i], variables, timeout_s)
        finally:
            marks[i] = (start - t0, time.monotonic() - t0)

    reported = 0
    batches = plan_batches(machine, concurrency)
    for batch_no, batch in enumerate(batches):
        if len(batch) == 1:
            results[batch[0]] = _one(batch[0])
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(batch)))) as pool:
                futures = {i: pool.submit(_one, i) for i in batch}
                for i in batch:
                    results[i] = futures[i].result()
                    # Report in order while the rest of the batch still runs.
                    while reported < len(machine) and results[reported] is not None:
                        if on_result is not None:
                            on_result(reported, *results[reported])
                        reported += 1
        while reported < len(machine) and results[reported] is not None:
            if on_result is not None:
                on_result(reported, *results[reported])
            reported += 1
        if timings is not None:
            for i in batch:
                start, end = marks.get(i, (None, None))
                timings.append({"check": machine[i]["check"], "batch": batch_no,
                                "start_s": None if start is None else round(start, 3),
                                "end_s": None if end is None else round(end, 3),
                                "rc": results[i][0]})
    return [r for r in results if r is not None]


def append_timing_log(path: Path, record: Dict[str, Any]) -> None:
    """One JSON line per gate run. Never fatal: timing is not a verdict."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


DAEMON_STOP_TIMEOUT_S = 15.0
DAEMON_START_TIMEOUT_S = 30.0


def bounce_daemon() -> None:
    """Restart the ros2 daemon.

    A stale daemon reports an empty node/service list on a perfectly healthy
    stack - four false FAILs were observed this way on 2026-07-22 - so every
    gate run starts from a fresh one.
    """
    if shutil.which("ros2") is None:
        print("  (ros2 not on PATH - skipping daemon bounce)")
        return
    # A WEDGED daemon makes `ros2 daemon stop` block forever (2026-10-06: a
    # daemon caught by a SIGINT to a trial's process group hung the next T-0
    # gate until the gate's own 300 s timeout, whose FAIL named no check).
    # Bounded, and a wedged daemon of THIS domain is killed instead.
    try:
        subprocess.run(["ros2", "daemon", "stop"], capture_output=True, timeout=DAEMON_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        domain = os.environ.get("ROS_DOMAIN_ID", "0") or "0"
        print(f"  (ros2 daemon stop hung for {DAEMON_STOP_TIMEOUT_S:.0f} s - killing the wedged "
              f"daemon of domain {domain})")
        subprocess.run(["pkill", "-KILL", "-f",
                        f"[r]os2cli.daemon.daemonize.*--ros-domain-id {domain}( |$)"],
                       capture_output=True)
    try:
        subprocess.run(["ros2", "daemon", "start"], capture_output=True, timeout=DAEMON_START_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"  (ros2 daemon start did not return in {DAEMON_START_TIMEOUT_S:.0f} s - "
              f"checks will start their own discovery)")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_vars(pairs: Optional[List[str]]) -> Dict[str, str]:
    variables: Dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ChecklistError(f"--var expects name=value (got {pair!r})")
        name, _, value = pair.partition("=")
        variables[name.strip()] = value
    return variables


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run an experiment's pre-flight checks (#87).")
    parser.add_argument("--checklist", required=True, type=Path, help="path to config/preflight_checklist.json")
    parser.add_argument("--var", action="append", metavar="NAME=VALUE",
                        help="fill ${NAME} placeholders in check arguments (repeatable)")
    parser.add_argument("--only", action="append", metavar="CHECK",
                        help="run only these checks by name (repeatable)")
    parser.add_argument("--skip", action="append", metavar="CHECK",
                        help="skip these checks by name (repeatable). For checks that are "
                             "MEANINGLESS for a given trial, not ones that are inconvenient: "
                             "a skipped check is reported as skipped and recorded, so a bag "
                             "always says which gates it was held to.")
    parser.add_argument("--list", action="store_true", help="show the plan without running anything")
    parser.add_argument("--validate", action="store_true",
                        help="only verify the checklist is coherent (no ROS needed); exit 2 on problems")
    parser.add_argument("--no-daemon-bounce", action="store_true", help="skip the ros2 daemon restart")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-check timeout in seconds (default: 60)")
    parser.add_argument("--concurrency", choices=CONCURRENCY_MODES, default=None,
                        help="run the machine checks one after another, or the parallel-safe "
                             f"ones together (default: ${CONCURRENCY_ENV}, else the checklist's "
                             f"'concurrency', else {DEFAULT_CONCURRENCY})")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"at most this many checks at once in concurrent mode (default: {DEFAULT_JOBS})")
    args = parser.parse_args(argv)

    try:
        entries = load_entries(args.checklist)
        # Checklist-declared defaults first; --var wins.
        variables = dict(load_variables(args.checklist))
        variables.update(parse_vars(args.var))
        concurrency = resolve_concurrency(args.concurrency, args.checklist)
    except ChecklistError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return RC_USAGE

    problems = validate(entries, variables)
    if problems:
        print(f"Checklist problems in {args.checklist}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return RC_USAGE

    machine = [e for e in entries if e["check"]]
    manual = [e for e in entries if not e["check"]]
    if args.only:
        machine = [e for e in machine if e["check"] in args.only]
    skipped_by_request = []
    if args.skip:
        skipped_by_request = [e for e in machine if e["check"] in args.skip]
        machine = [e for e in machine if e["check"] not in args.skip]
        for entry in skipped_by_request:
            # Announced, never silent: a gate that quietly drops a check is worse than
            # one that fails, because the bag then claims a standard it was not held to.
            print(f"SKIP  {entry['check']}: not applicable to this trial (--skip)")

    if args.validate:
        print(f"✓ {args.checklist}: {len(entries)} items "
              f"({len(machine)} machine-checked, {len(manual)} manual) - coherent.")
        return RC_PASS

    if args.list:
        print(f"=== PRE-FLIGHT PLAN ({args.checklist}) ===")
        print(f"  machine checks: {concurrency}")
        for entry in machine:
            rendered = " ".join(f"{k}={substitute(v, variables)}" for k, v in entry["args"].items())
            alone = "" if runs_in_parallel(entry) else "   (runs alone)"
            print(f"  [auto]   {entry['check']}.sh {rendered}{alone}")
            print(f"           {entry['text']}")
        for entry in manual:
            print(f"  [manual] {entry['text']}")
        return RC_PASS

    if not args.no_daemon_bounce:
        bounce_daemon()

    print("=== T-0 GATE ===")
    if not machine:
        print("  (no machine checks configured for this experiment)")

    counts = {"failures": 0, "skips": 0, "passes": 0}

    def report(i: int, code: int, detail: str) -> None:
        # The same lines, in the same (checklist) order, in both modes: the gate
        # parses them, and its log and the trial metadata must not depend on the mode.
        entry = machine[i]
        if code == RC_PASS:
            counts["passes"] += 1
            print(f"  PASS  {entry['check']}: {detail}", flush=True)
        elif code == RC_SKIP:
            counts["skips"] += 1
            print(f"  SKIP  {entry['check']}: {detail}", flush=True)
        else:
            counts["failures"] += 1
            label = "FAIL" if code == RC_FAIL else "ERR "
            print(f"  {label}  {entry['check']}: {detail}", flush=True)

    timings: List[Dict[str, Any]] = []
    t_start = time.monotonic()
    run_machine_checks(machine, variables, args.timeout, concurrency, args.jobs,
                       on_result=report, timings=timings)
    timing_log = os.environ.get(TIMING_LOG_ENV)
    if timing_log:
        append_timing_log(Path(timing_log), {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "checklist": str(args.checklist), "concurrency": concurrency, "jobs": args.jobs,
            "machine_checks_s": round(time.monotonic() - t_start, 3), "checks": timings})
    failures, skips, passes = counts["failures"], counts["skips"], counts["passes"]

    if manual:
        print("")
        print("--- OPERATOR CONFIRMATIONS (not machine-checkable) ---")
        for entry in manual:
            print(f"  [ ] {entry['text']}")

    print("")
    print("===")
    if failures:
        print(f"{failures} CHECK(S) FAILED ({passes} passed, {skips} skipped) - NO-GO")
        return RC_FAIL
    print(f"ALL {passes} MACHINE CHECKS PASS ({skips} skipped) - CLEAR TO START "
          f"once the {len(manual)} operator confirmation(s) above are done")
    return RC_PASS


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Explore one campaign condition live: its stack, a Foxglove view that may change parameters.

Added 2026-10-16. Before a campaign runs, its author debugs and tunes the
stack: watch it in Foxglove, change a gain, see what happens. Doing that by hand
means retyping the launch command and its arguments, and it leaves no record of
which values were tried. Explore brings up exactly the stack a trial of the
picked condition would start (setup steps, the launch with the condition's
arguments, the aux launches) and adds two things a trial never has:

* a ``foxglove_bridge`` that MAY change parameters, limited to the regular
  expressions of runner.yaml's ``explore.param_whitelist``. The trial
  livestream bridge stays read-only: a browser tab must never be able to change
  a flight that is being recorded as coverage;
* the ``explore.viewers`` of runner.yaml (2026-10-17): windows for a human,
  such as the simulator's GUI attached to its headless server (``gz sim -g``)
  or RViz. They are started after the bridge and stopped first. They read the
  running stack and change nothing in it, so the stack stays exactly the one a
  trial starts (a campaign runs Gazebo headless, and a viewer does not change
  that). A viewer that exits (its window closed) does not end the exploration;
* a parameter log (explore_params.py) that starts before the stack. It is
  read live (the run page's change table) and, at stop, every parameter that
  changed (from, to) goes to
  ``runs/explore/<stamp>_explore[_<label>]/parameter_changes.yaml``;
* since 2026-10-17, ``ExploreControls``: what the run page may do while
  the stack runs. Start one of runner.yaml's ``explore.actions`` at a time
  (take off, fly a route, land) and stop it with SIGTERM, set a changed
  parameter back to its starting value, record the running stack to a scratch
  recording (``bags_scratch/<stamp>_explore_<label>``, ``scratch: true`` and
  ``explore: true`` in its metadata, a slim copy for Foxglove), and write
  timestamped notes and marks to ``notes.md`` in the exploration's folder.

And it leaves out what makes a trial a trial: no T-0 gate, no trial action,
no trial_metadata.json in bags/, no attempt-ledger line. A recording made from
the run page goes to bags_scratch/ and says scratch: true. An exploration can
never count as coverage, and a stack whose parameters were changed is never
handed to a trial (it is not a continuous session, and it stops when the
exploration stops). A value worth keeping goes back into the declarations (a
launch default, a factor level) as a reviewed change, which is why the file
lists the changes rather than applying them anywhere.

Where Explore is allowed:

* ``simulation: true`` experiments, always. The default whitelist is ``['.*']``.
* hardware experiments only when runner.yaml declares an ``explore:`` block.
  Its ``param_whitelist`` defaults to empty (a live view that changes
  nothing), and a pattern that matches every parameter is refused (lint L16
  says so at review time, this module at run time). Each writable parameter of
  a real robot is a reviewed decision, the same rule as a safety limit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from robofacts_ros2.recorder import export_rosout
from robofacts import campaign_spec as cs
from robofacts import roots
from robofacts import trial_runner as tr

#: Where an exploration's files go, under the experiment. runs/ is git-ignored
#: and the coverage index never reads it.
EXPLORE_DIR = Path("runs") / "explore"
DEFAULT_PORT = 8765
DEFAULT_ADDRESS = "127.0.0.1"
#: foxglove_bridge rejects an empty string array, so "nothing" is a pattern that never matches.
NEVER_MATCHES = "(?!)"
#: A parameter name no real node has. A whitelist pattern that matches it matches everything.
MATCH_ALL_PROBE = "/robofacts_probe_ns/robofacts_probe_node.robofacts_probe_param"
#: The bridge's node name, so its own parameters are easy to tell apart in the log.
BRIDGE_NODE = "robofacts_explore_bridge"
VIEW_CAPABILITIES = ["connectionGraph", "assets"]
PARAM_CAPABILITIES = ["parameters", "parametersSubscribe"]


def matches_everything(pattern: str) -> bool:
    """True for a whitelist pattern such as `.*` that would let any parameter change."""
    try:
        return re.search(pattern, MATCH_ALL_PROBE) is not None
    except re.error:
        return False


@dataclass
class ExploreSettings:
    """What an exploration of this experiment may do, decided from runner.yaml."""
    allowed: bool
    #: Why Explore is refused, or empty when it is allowed.
    reason: str
    param_whitelist: List[str]
    topic_whitelist: List[str]
    port: int
    address: str
    #: explore.viewers: the windows Explore opens after the bridge, as {name, command}.
    viewers: List[Dict[str, Any]] = field(default_factory=list)
    #: explore.actions: the run page's buttons, as {name, command}. Never run by a trial.
    actions: List[Dict[str, Any]] = field(default_factory=list)
    #: explore.jog: {topic, max_speed_mps, step_s, max_yaw_rate_rps}, or empty: no jog pad.
    jog: Dict[str, Any] = field(default_factory=dict)
    #: The experiment's `simulation:`. On hardware the UI asks for the arm checkbox.
    simulation: bool = False

    @property
    def writable(self) -> bool:
        return bool(self.param_whitelist)

    def describe(self) -> str:
        if not self.allowed:
            return f"Explore is not available: {self.reason}"
        opens = (", and it opens " + ", ".join(v["name"] for v in self.viewers)) if self.viewers else ""
        if self.actions:
            opens += ". Actions: " + ", ".join(a["name"] for a in self.actions)
        if self.jog:
            opens += f". Jog pad on {self.jog['topic']} (at most {self.jog['max_speed_mps']} m/s)"
        if not self.writable:
            return ("live view only: no parameter may be changed (explore.param_whitelist is empty)"
                    + opens)
        return ("parameters matching " + ", ".join(f"`{p}`" for p in self.param_whitelist)
                + " may be changed" + opens)


def settings(config: tr.RunnerConfig) -> ExploreSettings:
    """The exploration rules for one experiment (see the module docstring)."""
    block = config.explore or {}
    port = int(block.get("port", DEFAULT_PORT))
    address = str(block.get("address", DEFAULT_ADDRESS))
    topics = list(block.get("topic_whitelist", [".*"]))
    viewers = list(block.get("viewers", []))
    actions = list(block.get("actions", []))
    jog = dict(block.get("jog") or {})
    if config.simulation:
        return ExploreSettings(True, "", list(block.get("param_whitelist", [".*"])), topics,
                               port, address, viewers=viewers, actions=actions, jog=jog,
                               simulation=True)
    if not config.explore_declared:
        return ExploreSettings(
            False, "this experiment runs real robots (simulation: false) and its runner.yaml "
                   "declares no `explore:` block. Declare one, with the parameters you allow "
                   "to change in `param_whitelist`, to explore it", [], topics, port, address)
    whitelist = list(block.get("param_whitelist", []))
    wide = [p for p in whitelist if matches_everything(p)]
    if wide:
        return ExploreSettings(
            False, f"explore.param_whitelist has {', '.join(repr(p) for p in wide)}, which matches "
                   f"every parameter. On real robots name the parameters you allow, one pattern each",
            whitelist, topics, port, address, viewers=viewers)
    return ExploreSettings(True, "", whitelist, topics, port, address, viewers=viewers,
                           actions=actions, jog=jog)


def _yaml_list(values: List[str]) -> str:
    """A ROS parameter override for a string array: YAML single quotes keep regex backslashes."""
    return "[" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + "]"


def bridge_command(s: ExploreSettings) -> List[str]:
    """The explore bridge. Built here, not taken from runner.yaml's `livestream`, which stays read-only."""
    capabilities = VIEW_CAPABILITIES + (PARAM_CAPABILITIES if s.writable else [])
    return ["ros2", "run", "foxglove_bridge", "foxglove_bridge", "--ros-args",
            "-r", f"__node:={BRIDGE_NODE}",
            "-p", f"port:={s.port}", "-p", f"address:={s.address}",
            "-p", f"capabilities:=[{','.join(capabilities)}]",
            "-p", f"param_whitelist:={_yaml_list(s.param_whitelist or [NEVER_MATCHES])}",
            "-p", f"topic_whitelist:={_yaml_list(s.topic_whitelist or [NEVER_MATCHES])}",
            # Never steerable beyond parameters: no topic publishing, no service calls.
            "-p", f"client_topic_whitelist:={_yaml_list([NEVER_MATCHES])}",
            "-p", f"service_whitelist:={_yaml_list([NEVER_MATCHES])}"]


def logger_command(target) -> List[str]:
    """The parameter log: by file path locally (works before a rebuild), as a module remotely."""
    if target is None:
        return [sys.executable if sys.executable else "python3",
                str(Path(__file__).resolve().with_name("explore_params.py"))]
    return ["python3", "-m", "robofacts_ros2.explore_params"]


def connect_info(s: ExploreSettings, target) -> Dict[str, Any]:
    """How to open the live view, for the terminal and the run page."""
    url = f"ws://localhost:{s.port}"
    info = {"url": url, "port": s.port,
            "deep_link": f"foxglove://open?ds=foxglove-websocket&ds.url={url}",
            "web_link": f"https://app.foxglove.dev/~/view?ds=foxglove-websocket&ds.url={url}",
            "tunnel": "", "writable": list(s.param_whitelist), "host": "this machine"}
    if target is not None:
        info["tunnel"] = f"ssh -N -L {s.port}:localhost:{s.port} {target.host}"
        info["host"] = target.host
    return info


def parse_events(lines) -> List[Dict[str, Any]]:
    """The parameter events in a log, oldest first. Lines that are not events are skipped."""
    events = []
    for raw in lines:
        text = raw.strip()
        start = text.find("{")
        if start < 0:
            continue
        try:
            event = json.loads(text[start:])
        except ValueError:
            continue
        if isinstance(event, dict) and {"node", "name", "kind"} <= set(event):
            events.append(event)
    return events


def summarize(events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Which parameters ended the exploration with a different value, and which were reverted.

    The starting value of a parameter is the first value the log knew before
    any change: read from the node's services (`initial`) or heard as it was
    declared (`new`). `from: null` means neither source had it, so the
    starting value is unknown, never guessed.
    """
    start: Dict[tuple, Any] = {}
    touched: Dict[tuple, Dict[str, Any]] = {}
    for e in events:
        key = (e["node"], e["name"])
        if e["kind"] in ("new", "initial"):
            if key not in touched and key not in start:
                start[key] = e.get("value")
            continue
        entry = touched.setdefault(key, {"node": e["node"], "parameter": e["name"],
                                         "from": start.get(key), "to": None,
                                         "times_changed": 0})
        entry["times_changed"] += 1
        entry["to"] = e.get("value") if e["kind"] == "changed" else None
        if e["kind"] == "deleted":
            entry["deleted"] = True
    changed = [t for t in touched.values() if t["to"] != t["from"] or t.get("deleted")]
    reverted = [t for t in touched.values() if t not in changed]
    return {"changed": changed, "reverted": reverted}


def write_changes(path: Path, summary: Dict[str, List[Dict[str, Any]]], header: Dict[str, Any]) -> None:
    """parameter_changes.yaml: what was tried, for a reviewed edit of the declarations."""
    body = {**header, "changed": summary["changed"],
            "reverted": [f"{t['node']}.{t['parameter']}" for t in summary["reverted"]]}
    text = ("# Parameters changed during a robofacts Explore session.\n"
            "# Nothing here reached a trial. To keep a value, change the launch default or a\n"
            "# factor level in the experiment's declarations, and review it like any other change.\n"
            "# from: null means the starting value is unknown (the node could not be read).\n"
            "# A changed value took effect only if the node reads it while running (an on-set\n"
            "# parameter callback). Many nodes read a parameter once, at startup.\n"
            "# reverted: set during the session, but ended at the starting value.\n")
    path.write_text(text + yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8")


def change_lines(summary: Dict[str, List[Dict[str, Any]]]) -> List[str]:
    lines = []
    for t in summary["changed"]:
        to = "(deleted)" if t.get("deleted") else json.dumps(t["to"])
        lines.append(f"{t['node']}.{t['parameter']}: {json.dumps(t['from'])} -> {to}"
                     f"  ({t['times_changed']} change(s))")
    return lines


@dataclass
class ExploreResult:
    trial: cs.TrialSpec
    #: explored | refused | failed | dry-run
    status: str
    detail: str = ""
    out_dir: Optional[Path] = None
    commands: List[List[str]] = field(default_factory=list)
    summary: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: {"changed": [], "reverted": []})
    connect: Dict[str, Any] = field(default_factory=dict)


class _Tee:
    """Mirror stdout into the exploration's run log, colour codes stripped."""

    def __init__(self, stream, sink):
        self._stream, self._sink = stream, sink

    def write(self, text):
        try:
            self._sink.write(tr._ANSI_RE.sub("", text))
            self._sink.flush()
        except (OSError, ValueError):
            pass
        return self._stream.write(text)

    def flush(self):
        self._stream.flush()


def _exited(proc) -> Optional[int]:
    """The exit code of a started process, or None while it runs (or cannot tell)."""
    poll = getattr(proc, "poll", None)
    return poll() if callable(poll) else None


def _out_dir(experiments_root: Path, experiment: str, stamp: str, label: str) -> Path:
    base = f"{stamp}_explore" + (f"_{label}" if label else "")
    parent = experiments_root / experiment / EXPLORE_DIR
    out, n = parent / base, 2
    while out.exists():
        out, n = parent / f"{base}-{n}", n + 1
    return out


def current_values(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The last value the log knows for every parameter, as {"<node>.<parameter>": value}."""
    values: Dict[str, Any] = {}
    for e in events:
        key = f"{e['node']}.{e['name']}"
        if e["kind"] == "deleted":
            values.pop(key, None)
        else:
            values[key] = e.get("value")
    return values


def param_value_text(value: Any) -> str:
    """A value as `ros2 param set` reads it (YAML): JSON is valid YAML and keeps the type."""
    return json.dumps(value)


def whitelisted(s: ExploreSettings, node: str, parameter: str) -> bool:
    """Whether the explore bridge would let this parameter change (its param_whitelist)."""
    # foxglove_bridge matches its whitelist against "<node>.<parameter>", the node
    # fully qualified, so this asks the same question of the same string.
    full = f"{node}.{parameter}"
    for pattern in s.param_whitelist:
        try:
            if re.search(pattern, full):
                return True
        except re.error:
            continue
    return False


def jog_command(target, topic: str, vx: float, vy: float, vz: float, yaw: float,
                seconds: float) -> List[str]:
    """One jog step (explore_jog.py): by file path locally, as a module remotely."""
    head = ([sys.executable if sys.executable else "python3",
             str(Path(__file__).resolve().with_name("explore_jog.py"))] if target is None
            else ["python3", "-m", "robofacts_ros2.explore_jog"])
    return head + ["--topic", topic, "--vx", f"{vx:g}", "--vy", f"{vy:g}", "--vz", f"{vz:g}",
                   "--yaw", f"{yaw:g}", "--seconds", f"{seconds:g}"]


def ti_like(bag_dir: Path):
    """The one field run_analysis reads of a trial: its bag directory."""
    from types import SimpleNamespace
    return SimpleNamespace(bag_dir=bag_dir)


def terminate_action(proc, target, cmd: List[str]) -> None:
    """SIGTERM, never SIGINT: the flight scripts land on SIGTERM (rclpy keeps SIGINT).

    Locally the action has its own process group (start_background), so the
    group gets the signal. Remotely the local process is only the ssh tunnel,
    and closing it would hang up the script without a landing, so the script
    itself is signalled in the container by its command line.
    """
    if target is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        return
    from robofacts.remote_exec_utils import _pkill_remote
    _pkill_remote(target, re.escape(" ".join(cmd)), "TERM")


def kill_action(proc, target, cmd: List[str]) -> None:
    """The last resort when an action ignores SIGTERM for too long."""
    if target is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    from robofacts.remote_exec_utils import _pkill_remote
    _pkill_remote(target, re.escape(" ".join(cmd)), "KILL")


class ExploreControls:
    """What the run page may do to a live exploration (2026-10-17).

    run_explore binds it once the stack is READY and unbinds it in its
    ``finally``, before the stack comes down: a running action gets SIGTERM
    (and lands) and a running recorder is stopped first, so the bag is
    finalized. Every method is safe to call from a web request thread and
    returns ``(ok, message)``. The browser names an action, a parameter or a
    note, never a command: what may run is what runner.yaml declares.

    * actions: ``explore.actions``, one at a time. On hardware only when the
      caller says ``armed`` (the run page's arm checkbox).
    * set back: ``ros2 param set`` to the starting value the parameter log
      read, only for a parameter that changed, whose start is known, and
      that the bridge's whitelist allows.
    * record: the trial's own record command, to
      ``bags_scratch/<stamp>_explore_<label>``, with ``scratch: true`` and
      ``explore: true`` in its trial_metadata.json, then a slim copy.
    * notes: timestamped lines in ``notes.md``, a mark says so.
    """

    def __init__(self, *, terminate: Callable = terminate_action, kill: Callable = kill_action,
                 now: Callable[[], float] = time.time, land_wait_s: float = 90.0,
                 slim: Optional[Callable[..., Any]] = None):
        self._lock = threading.Lock()
        self._terminate, self._kill, self._now = terminate, kill, now
        self._land_wait_s = land_wait_s
        self._slim = slim
        self._ctx: Optional[Dict[str, Any]] = None
        self._action: Optional[Dict[str, Any]] = None
        self._last_action: Optional[Dict[str, Any]] = None
        self._record: Optional[Dict[str, Any]] = None
        self.recordings: List[Dict[str, Any]] = []
        self.notes: List[str] = []
        #: Analyses of the recordings, newest last; they outlive the stack.
        self.analyses: List[Dict[str, Any]] = []
        self._kept: Optional[Dict[str, Any]] = None
        self._final: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self.proposals: List[Dict[str, Any]] = []
        self._threads: List[threading.Thread] = []

    # ------------------------------------------------------------------ binding
    def bind(self, **ctx: Any) -> None:
        with self._lock:
            self._ctx = dict(ctx)
            # What an analysis needs after Stop: no stack, only the files and a process.
            self._kept = {k: ctx[k] for k in ("hooks", "target", "cwd", "out_dir", "experiment",
                                              "experiments_root", "spec", "factors", "config",
                                              "settings") if k in ctx}

    @property
    def live(self) -> bool:
        return self._ctx is not None

    def _events(self) -> List[Dict[str, Any]]:
        ctx = self._ctx
        if ctx is None:
            return []
        try:
            lines = (ctx["out_dir"] / "parameter_events.jsonl").read_text(
                encoding="utf-8").splitlines()
        except OSError:
            return []
        return parse_events(lines)

    def changes(self) -> Dict[str, List[Dict[str, Any]]]:
        """The live change table: summarize() over the parameter log so far."""
        summary = summarize(self._events())
        ctx = self._ctx
        for row in summary["changed"] + summary["reverted"]:
            row["settable"] = bool(ctx and row["from"] is not None and not row.get("deleted")
                                   and whitelisted(ctx["settings"], row["node"], row["parameter"]))
        return summary

    def status(self) -> Dict[str, Any]:
        ctx = self._ctx
        with self._lock:
            action = dict(self._action) if self._action else None
            last = dict(self._last_action) if self._last_action else None
            record = dict(self._record) if self._record else None
        for a in (action, last):
            if a:
                a.pop("proc", None)
                a.pop("cmd", None)
        if record:
            # The parameter snapshot is for the metadata, not for a page that polls
            # every 1.5 s (249 values in the Gazebo stand-in, 2026-10-17).
            for key in ("proc", "parameters", "changed"):
                record.pop(key, None)
            record["seconds"] = round(self._now() - record["t0"], 1)
            record.pop("t0", None)
        return {"live": ctx is not None,
                "actions": [a["name"] for a in (ctx["settings"].actions if ctx else [])],
                "jog": dict(ctx["settings"].jog) if ctx else {},
                "simulation": bool(ctx and ctx["settings"].simulation),
                "action": action, "last_action": last, "record": record,
                "recordings": [{k: v for k, v in r.items() if k != "t0"} for r in self.recordings],
                "notes": list(self.notes),
                "analyses": [{k: v for k, v in a.items() if k != "thread"} for a in self.analyses],
                "analysis_names": self.analysis_names(),
                "changes": self.changes() if ctx else {"changed": [], "reverted": []}}

    # ------------------------------------------------------------------ actions
    def start_action(self, name: str, *, armed: bool = False) -> tuple:
        ctx = self._ctx
        if ctx is None:
            return False, "the exploration is not live (not ready yet, or stopping)"
        s: ExploreSettings = ctx["settings"]
        declared = {a["name"]: a for a in s.actions}
        if name not in declared:
            return False, f"no explore action named {name!r} in runner.yaml"
        if not s.simulation and not armed:
            return False, ("this experiment runs real robots: tick 'arm actions' first. "
                           "An action moves a robot without a T-0 gate")
        try:
            cmd = tr.templated_command(declared[name]["command"], ctx["spec"],
                                       f"explore action {name}", **ctx["placeholders"])
        except tr.RunnerError as exc:
            return False, str(exc)
        return self._start_in_slot(name, cmd)

    def jog(self, vx: float = 0.0, vy: float = 0.0, vz: float = 0.0, yaw: float = 0.0, *,
            armed: bool = False) -> tuple:
        """One press of the jog pad: a velocity for explore.jog.step_s, clipped to its limits."""
        ctx = self._ctx
        if ctx is None:
            return False, "the exploration is not live (not ready yet, or stopping)"
        s: ExploreSettings = ctx["settings"]
        if not s.jog:
            return False, "this experiment declares no explore.jog in runner.yaml"
        if not s.simulation and not armed:
            return False, ("this experiment runs real robots: tick 'arm actions' first. "
                           "A jog moves a robot without a T-0 gate")
        try:
            v = [float(x) for x in (vx, vy, vz, yaw)]
        except (TypeError, ValueError):
            return False, "a jog needs numbers"
        if not all(x == x and abs(x) != float("inf") for x in v):
            return False, "a jog needs finite numbers"
        top, rot = s.jog["max_speed_mps"], s.jog.get("max_yaw_rate_rps", 0.0)
        # "+ 0.0" turns a clipped -0.0 into 0.0, so the command line says 0.
        vx, vy, vz = (max(-top, min(top, x)) + 0.0 for x in v[:3])
        yaw = max(-rot, min(rot, v[3])) + 0.0
        if not any((vx, vy, vz, yaw)):
            return False, "a jog of zero moves nothing"
        name = "jog " + " ".join(f"{k}={x:+.2f}" for k, x in
                                 (("vx", vx), ("vy", vy), ("vz", vz), ("yaw", yaw)) if x)
        cmd = jog_command(ctx["target"], s.jog["topic"], vx, vy, vz, yaw, s.jog["step_s"])
        return self._start_in_slot(name, cmd, label="jog")

    def _start_in_slot(self, name: str, cmd: List[str], label: str = "") -> tuple:
        """Start an action or a jog in the one slot they share. ``label`` tags its output."""
        ctx = self._ctx
        if ctx is None:
            return False, "the exploration is not live"
        with self._lock:
            if self._action is not None:
                return False, (f"the action {self._action['name']!r} is still running: "
                               f"wait for it, or stop it first")
            print(f"[explore] {'action ' if not label else ''}{name}: $ {shlex.join(cmd)}")
            tag = f"action:{label or name}"
            proc = ctx["hooks"].start_background(cmd, ctx["target"], cwd=ctx["cwd"], label=tag)
            self._action = {"name": name, "tag": tag, "proc": proc, "cmd": cmd,
                            "started": self._now(), "rc": None, "stopping": False}
            entry = self._action
        t = threading.Thread(target=self._watch_action, args=(entry,),
                             name=f"explore-{entry['tag']}", daemon=True)
        self._threads.append(t)
        t.start()
        return True, f"started {name}"

    def _watch_action(self, entry: Dict[str, Any]) -> None:
        proc = entry["proc"]
        tick = threading.Event()
        while True:
            rc = _exited(proc)
            if rc is not None:
                break
            tick.wait(0.2)
        entry["rc"] = rc
        entry["seconds"] = round(self._now() - entry["started"], 1)
        print(f"[explore] {entry['name'] if entry['tag'] == 'action:jog' else 'action ' + entry['name']}"
              f" exited rc={rc} after {entry['seconds']} s"
              + (" (stopped)" if entry["stopping"] else ""))
        with self._lock:
            if self._action is entry:
                self._action = None
            self._last_action = dict(entry)

    def stop_action(self) -> tuple:
        with self._lock:
            entry = self._action
            if entry is None:
                return False, "no action is running"
            entry["stopping"] = True
        print(f"[explore] action {entry['name']}: SIGTERM (stop from the run page)")
        ctx = self._ctx or {}
        self._terminate(entry["proc"], ctx.get("target"), entry["cmd"])
        return True, f"SIGTERM sent to {entry['name']}"

    def _end_action(self) -> None:
        """At stop: SIGTERM the running action, wait for its landing, kill it if it hangs."""
        entry = self._action
        if entry is None:
            return
        self.stop_action()
        t0 = time.monotonic()
        while self._action is entry and time.monotonic() - t0 < self._land_wait_s:
            if _exited(entry["proc"]) is not None:
                break
            time.sleep(0.2)
        if _exited(entry["proc"]) is None:
            print(f"[explore] action {entry['name']} still running after "
                  f"{self._land_wait_s:.0f} s: SIGKILL")
            self._kill(entry["proc"], (self._ctx or {}).get("target"), entry["cmd"])

    # ------------------------------------------------------------------ set back
    def set_back(self, node: str, parameter: str) -> tuple:
        ctx = self._ctx
        if ctx is None:
            return False, "the exploration is not live"
        summary = self.changes()
        row = next((r for r in summary["changed"]
                    if r["node"] == node and r["parameter"] == parameter), None)
        if row is None:
            return False, f"{node}.{parameter} has not changed in this exploration"
        if not row["settable"]:
            why = ("its starting value is unknown" if row["from"] is None
                   else "the explore whitelist does not allow it")
            return False, f"{node}.{parameter} cannot be set back: {why}"
        cmd = ["ros2", "param", "set", node, parameter, param_value_text(row["from"])]
        print(f"[explore] set back {node}.{parameter} to {json.dumps(row['from'])}: "
              f"$ {shlex.join(cmd)}")
        completed = ctx["hooks"].run_foreground(cmd, ctx["target"], cwd=ctx["cwd"], check=False)
        rc = getattr(completed, "returncode", 0)
        out = (getattr(completed, "stdout", "") or "").strip()
        if rc != 0 or "Setting parameter failed" in out:
            return False, f"ros2 param set failed (rc={rc}): {out[-300:]}"
        return True, f"{node}.{parameter} set back to {json.dumps(row['from'])}"

    # ------------------------------------------------------------------ notes
    def _stamp(self) -> str:
        return datetime.fromtimestamp(self._now()).astimezone().isoformat(timespec="seconds")

    def note(self, text: str, *, mark: bool = False) -> tuple:
        ctx = self._ctx
        if ctx is None:
            return False, "the exploration is not live"
        text = " ".join((text or "").split())
        if not text and not mark:
            return False, "an empty note"
        parts = [f"- {self._stamp()}"]
        if mark:
            parts.append("● mark")
        with self._lock:
            record = self._record
        if record:
            parts.append(f"[recording {record['name']} at +{self._now() - record['t0']:.1f} s]")
        action = self._action
        if action:
            parts.append(f"[action {action['name']}]")
        if text:
            parts.append(text)
        line = " ".join(parts)
        path = ctx["out_dir"] / "notes.md"
        try:
            new = not path.exists()
            with path.open("a", encoding="utf-8") as fh:
                if new:
                    fh.write(f"# Notes: {ctx['experiment']} exploration {ctx['out_dir'].name}\n\n"
                             f"Condition: {ctx['spec'].describe()}\n\n")
                fh.write(line + "\n")
        except OSError as exc:
            return False, f"could not write {path}: {exc}"
        self.notes.append(line)
        print(f"[explore] note: {line[2:]}")
        return True, line

    # ------------------------------------------------------------------ record
    def start_record(self, label: str = "") -> tuple:
        ctx = self._ctx
        if ctx is None:
            return False, "the exploration is not live"
        with self._lock:
            if self._record is not None:
                return False, f"already recording {self._record['name']}"
            safe = re.sub(r"[^A-Za-z0-9_-]+", "-", (label or "").strip()).strip("-")
            base = f"{ctx['clock']()}_explore" + (f"_{safe}" if safe else "")
            bags = ctx["experiments_root"] / ctx["experiment"] / "bags_scratch"
            bag_dir, n = bags / base, 2
            while bag_dir.exists() or any(r["bag_dir"] == str(bag_dir) for r in self.recordings):
                bag_dir, n = bags / f"{base}-{n}", n + 1
            events = self._events()
            cmd = tr.record_command(ctx["spec"], ctx["factors"], ctx["config"], bag_dir)
            if ctx["target"] is None:
                bags.mkdir(parents=True, exist_ok=True)
            print(f"[explore] record {bag_dir.name}: $ {shlex.join(cmd)}")
            proc = ctx["hooks"].start_background(cmd, ctx["target"], cwd=ctx["cwd"],
                                                 label="recorder")
            self._record = {"name": bag_dir.name, "bag_dir": str(bag_dir), "label": safe,
                            "proc": proc, "t0": self._now(), "started": self._stamp(),
                            "parameters": current_values(events),
                            "changed": summarize(events)["changed"]}
        return True, f"recording {bag_dir.name}"

    def stop_record(self, *, wait_slim: bool = False) -> tuple:
        ctx = self._ctx
        with self._lock:
            rec = self._record
            self._record = None
        if rec is None or ctx is None:
            return False, "nothing is recording"
        hooks, target, config = ctx["hooks"], ctx["target"], ctx["config"]
        try:
            hooks.stop_process(rec["proc"], target, name="record", kind="recorder")
        except Exception as exc:  # noqa: BLE001 - still write what we know
            print(f"[explore] stopping the recorder failed: {exc}")
        time.sleep(config.stop_grace_s)
        bag_dir = Path(rec["bag_dir"])
        seconds = round(self._now() - rec["t0"], 1)
        metadata = tr.trial_metadata(ctx["spec"], ctx["factors"], config, trial_id=bag_dir.name)
        metadata.update({
            # Two locks, as for a --scratch flight: bags_scratch/ is never indexed,
            # and scratch: true is refused as coverage even inside bags/.
            "scratch": True, "scratch_label": rec["label"] or "explore",
            "explore": True, "explore_session": ctx["out_dir"].name,
            "explore_condition": dict(ctx["spec"].values),
            "explore_started": rec["started"], "record_duration_sec": seconds,
            # What the stack's parameters were when the recorder started: the bag
            # alone cannot say that a gain had been changed from Foxglove.
            "explore_parameters_at_start": rec["parameters"],
            "explore_changed_at_start": rec["changed"],
        })
        try:
            tr.stamp_code_version(metadata, target)
        except Exception:  # noqa: BLE001 - provenance, never fatal
            pass
        out = {"name": bag_dir.name, "bag_dir": str(bag_dir), "seconds": seconds,
               "started": rec["started"], "slim": "", "slim_status": "pending"}
        try:
            from robofacts.remote_exec_utils import write_trial_file
            write_trial_file(target, bag_dir / "trial_metadata.json",
                             json.dumps(metadata, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001
            print(f"[explore] could not write the recording's metadata: {exc}")
        print(f"[explore] recorded {bag_dir.name} ({seconds} s), scratch, never coverage")
        self.recordings.append(out)

        def slim() -> None:
            try:
                export_rosout(bag_dir, target, hooks, ctx["cwd"])
                if self._slim is not None:
                    self._slim(bag_dir, target, project_root=ctx["cwd"])
                else:
                    from robofacts_ros2.slim_bag_archive import archive_and_sync
                    archive_and_sync(bag_dir, target, project_root=ctx["cwd"])
                out["slim"] = bag_dir.name + "_slim"
                out["slim_status"] = "done"
                print(f"[explore] slim copy of {bag_dir.name} ready: replay it in Foxglove")
            except Exception as exc:  # noqa: BLE001 - the full bag is safe
                out["slim_status"] = f"failed: {exc}"
                print(f"[explore] slim copy of {bag_dir.name} failed: {exc}")

        if wait_slim:
            slim()
        else:
            out["slim_status"] = "slimming"
            t = threading.Thread(target=slim, name=f"explore-slim-{bag_dir.name}", daemon=True)
            self._threads.append(t)
            t.start()
        return True, f"recorded {bag_dir.name} ({seconds} s)"

    # ------------------------------------------------------------------ analyses
    def analysis_names(self) -> List[str]:
        """The experiment's per-bag analyses (analyses.yaml), the ones a recording can feed."""
        kept = self._kept
        if kept is None:
            return []
        from robofacts import analysis_runner as ar
        try:
            declared = ar.load_analyses(kept["experiment"], kept["experiments_root"])
        except Exception:  # noqa: BLE001 - no analyses.yaml, or a broken one
            return []
        return [n for n, a in declared.items() if a.per == "bag"]

    def analyze(self, recording: str, analysis: str) -> tuple:
        """Run one per-bag analysis on one recording of this exploration, in the background.

        The same run_analysis as ``trials analyze``, with the recording as its one
        bag, so the result lands in results/<stamp>_<analysis>/<recording>/ beside
        every other result, and its analysis_metadata.json says scratch and
        explore. Allowed after Stop too: an analysis needs the bag, not the stack.
        """
        kept = self._kept
        if kept is None:
            return False, "the exploration has not started"
        rec = next((r for r in self.recordings if r["name"] == recording), None)
        if rec is None:
            return False, f"no recording {recording!r} in this exploration"
        if analysis not in self.analysis_names():
            return False, f"{analysis!r} is not a per-bag analysis of this experiment (analyses.yaml)"
        if kept["target"] is not None:
            return False, ("the recording is on the rig, and analyses run here. Fetch the bag, "
                           "then run trials analyze")
        if any(a["status"] == "running" for a in self.analyses):
            return False, "an analysis is still running: one at a time"
        from robofacts import analysis_runner as ar
        declared = ar.load_analyses(kept["experiment"], kept["experiments_root"])[analysis]
        log_path = kept["out_dir"] / f"analysis_{analysis}_{recording}.log"
        exp_dir = kept["experiments_root"] / kept["experiment"]
        entry = {"analysis": analysis, "recording": recording, "status": "running",
                 "results": "", "detail": "", "started": self._stamp(),
                 "log": str(log_path.relative_to(exp_dir))}
        hooks, cwd = kept["hooks"], kept["cwd"]

        def executor(cmd: List[str]) -> int:
            # The output goes to its own file: after Stop the run log is closed, and
            # the lines would land on the UI's console (2026-10-17). Its tail is
            # printed once it ends, which reaches the run log while one is open.
            with log_path.open("w", encoding="utf-8") as sink:
                proc = hooks.start_background(cmd, None, cwd=cwd, label=f"analysis:{analysis}",
                                              quiet_sink=sink)
                tick = threading.Event()
                while _exited(proc) is None:
                    tick.wait(0.5)
                tick.wait(0.3)   # the pump thread's last lines
            try:
                for line in log_path.read_text(encoding="utf-8").splitlines()[-15:]:
                    print(line)
            except OSError:
                pass
            return _exited(proc)

        def work() -> None:
            try:
                print(f"[explore] analysis {analysis} of {recording}")
                result = ar.run_analysis(
                    declared, [ti_like(Path(rec["bag_dir"]))], kept["experiment"],
                    kept["experiments_root"], dry_run=False, executor=executor,
                    extra_metadata={"scratch": True, "explore": True,
                                    "explore_session": kept["out_dir"].name,
                                    "selection_note": "a scratch recording of robofacts Explore, "
                                                      "never coverage"})
                entry["status"] = result.status
                entry["detail"] = result.detail
                entry["results"] = str((result.results_dir / recording).relative_to(
                    kept["experiments_root"] / kept["experiment"]))
                print(f"[explore] analysis {analysis} of {recording}: {result.status}"
                      f"{' (' + result.detail + ')' if result.detail else ''}, in {entry['results']}")
            except Exception as exc:  # noqa: BLE001 - shown on the page
                entry["status"], entry["detail"] = "failed", str(exc)
                print(f"[explore] analysis {analysis} of {recording} failed: {exc}")

        t = threading.Thread(target=work, name=f"explore-analysis-{analysis}", daemon=True)
        entry["thread"] = t
        self.analyses.append(entry)
        t.start()
        return True, f"analysis {analysis} of {recording} started"

    # ------------------------------------------------------------------ proposals
    def propose(self, node: str, parameter: str) -> tuple:
        """Where ``<node>.<parameter>`` is declared, and the edit that keeps its current value.

        Read-only (explore_propose.py): nothing is applied. The proposal is also
        appended to ``proposed_changes.md`` in the exploration's folder. Returns
        ``(ok, message, proposal)``. Allowed after Stop, from the last change table.
        """
        from robofacts_ros2 import explore_propose as epr
        kept = self._kept
        if kept is None:
            return False, "the exploration has not started", None
        summary = self.changes() if self._ctx is not None else (self._final or {"changed": []})
        row = next((r for r in summary["changed"]
                    if r["node"] == node and r["parameter"] == parameter), None)
        if row is None:
            return False, f"{node}.{parameter} did not change in this exploration", None
        if row.get("deleted"):
            return False, f"{node}.{parameter} was deleted: there is no value to keep", None
        config = kept["config"]
        proposal = epr.propose(node, parameter, row["from"], row["to"],
                               project_root=Path(config.project_root),
                               launch_package=config.launch_package,
                               launch_file=config.launch_file, factors=kept["factors"],
                               condition=dict(kept["spec"].values))
        path = kept["out_dir"] / "proposed_changes.md"
        try:
            new = not path.exists()
            with path.open("a", encoding="utf-8") as fh:
                if new:
                    fh.write(f"# Proposed changes: {kept['experiment']} exploration {kept['out_dir'].name}\n\n"
                             "Where each kept value is declared, found by robofacts Explore. Nothing was "
                             "applied. Each edit is a reviewed change of the declarations.\n\n")
                fh.write(epr.markdown(proposal, self._stamp()))
        except OSError as exc:
            return False, f"could not write {path}: {exc}", proposal
        self.proposals.append({"node": node, "parameter": parameter, "to": row["to"],
                               "candidates": len(proposal["candidates"])})
        print(f"[explore] proposal for {node}.{parameter} = {json.dumps(row['to'])}: "
              f"{proposal['summary']} (proposed_changes.md)")
        return True, proposal["summary"], proposal

    # ------------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        """Before the stack comes down: land the action, finalize the recording, finish slims."""
        if self._ctx is None:
            return
        self._end_action()
        if self._record is not None:
            self.stop_record(wait_slim=True)
        for t in list(self._threads):
            t.join(timeout=300)
        # The last change table, for a proposal made after Stop.
        self._final = self.changes()
        with self._lock:
            # The stack goes now. _kept stays: a recording can still be analyzed.
            self._ctx = None


def run_explore(campaign: "cs.Campaign", config: tr.RunnerConfig, experiments_root: Path, *,
                dry_run: bool = True, hooks: Optional[tr.ProcessHooks] = None,
                stop: Optional[threading.Event] = None, max_s: Optional[float] = None,
                label: str = "",
                confirm_steps: Optional[Callable[[cs.TrialSpec, List[Dict[str, Any]]], bool]] = None,
                on_ready: Optional[Callable[[Dict[str, Any]], None]] = None,
                controls: Optional[ExploreControls] = None,
                clock: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
                poll_s: float = 0.5, logger_settle_s: float = 1.0) -> ExploreResult:
    """Bring up the campaign's first condition, hold it until ``stop`` is set, tear it down.

    The condition is the campaign's own first combination, as for a scratch
    recording: factor defaults, its `fixed` block, the first level of anything
    swept, and whatever ``--set`` (the UI's picked cell) pinned. The order is
    setup steps, parameter log, stack, aux launches, bridge. The parameter log
    starts before the stack so it hears every node declare its parameters.
    Teardown runs in reverse, in a ``finally``, so a crash still stops the stack.
    ``controls`` is bound when the stack is ready and shut down first in
    the teardown: a running action lands and a recording is finalized before
    anything else stops.
    """
    spec = next(campaign.combinations())
    factors = campaign.factors
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", (label or "").strip()).strip("-")
    s = settings(config)
    print(tr._c("36;1", f"[explore] EXPLORE {campaign.experiment}/{campaign.name}"
                        + (f" [{safe_label}]" if safe_label else "")))
    print(f"[explore]   condition: {spec.describe()}")
    if not s.allowed:
        print(tr._c("31;1", f"[explore] REFUSED - {s.reason}"))
        return ExploreResult(spec, "refused", s.reason)
    print(f"[explore]   {s.describe()}")
    print("[explore]   no T-0 gate, no trial action: an exploration never counts as coverage, "
          "and a recording from the run page goes to bags_scratch/")

    target = config.remote_target(spec.host(factors))
    hooks = hooks or tr.ProcessHooks.default()
    local_env = config.local_setup(spec.host(factors))
    if local_env is not None:
        hooks = hooks.with_local_setup(*local_env)
    out_dir = _out_dir(experiments_root, campaign.experiment, clock(), safe_label)
    arg_ph = {f"arg_{k}": v for k, v in spec.launch_args(factors).items()}
    setup_cmds = [(st["name"], tr.templated_command(st["command"], spec, f"setup step {st['name']}",
                                                    **arg_ph), st["required"])
                  for st in config.setup_steps]
    launch_cmd = tr.launch_command(spec, factors, config)
    aux_cmds = [(a["name"], tr.templated_command(a["command"], spec, f"aux launch {a['name']}",
                                                 **{"bag": str(out_dir), **arg_ph}))
                for a in config.aux_launches]
    log_cmd = logger_command(target)
    bridge_cmd = bridge_command(s)
    viewer_cmds = [(v["name"], tr.templated_command(v["command"], spec, f"viewer {v['name']}",
                                                    **arg_ph))
                   for v in s.viewers]
    commands = ([c for _, c, _ in setup_cmds] + [log_cmd, launch_cmd]
                + [c for _, c in aux_cmds] + [bridge_cmd] + [c for _, c in viewer_cmds])
    info = connect_info(s, target)
    result = ExploreResult(spec, "dry-run", "nothing executed", out_dir, commands, connect=info)
    if dry_run:
        for cmd in commands:
            print(f"[explore]   $ {shlex.join(cmd)}")
        return result

    if campaign.operator_steps:
        confirm = confirm_steps if confirm_steps is not None else tr.confirm_operator_steps_tty
        if not confirm(spec, campaign.operator_steps):
            result.status, result.detail = "refused", "operator steps not confirmed - nothing was launched"
            return result

    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = (out_dir / "run.log").open("w", encoding="utf-8")
    events_file = (out_dir / "parameter_events.jsonl").open("w", encoding="utf-8")
    saved_stdout = sys.stdout
    sys.stdout = _Tee(saved_stdout, log_file)
    cwd = config.project_root
    procs: Dict[str, Any] = {}
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result.status, result.detail = "explored", ""
    try:
        for name, cmd, required in setup_cmds:
            print(f"[setup] {name}")
            completed = hooks.run_foreground(cmd, target, cwd=cwd, check=False)
            rc = getattr(completed, "returncode", 0)
            if rc != 0 and required:
                raise tr.RunnerError(f"setup step {name!r} failed (rc={rc})")
        procs["params"] = hooks.start_background(log_cmd, target, cwd=cwd, label="params",
                                                 quiet_sink=events_file)
        time.sleep(logger_settle_s)
        procs["launch_stack"] = hooks.start_background(launch_cmd, target, cwd=cwd, label="stack")
        time.sleep(config.settle_s)
        # A stack that died while settling (a launch error, a missing package)
        # must not be announced as READY: the bridge would show an empty graph.
        rc = _exited(procs["launch_stack"])
        if rc is not None:
            raise tr.RunnerError(f"the stack exited while settling (rc={rc}): see run.log")
        for name, cmd in aux_cmds:
            print(f"[aux] {name}")
            procs[f"aux:{name}"] = hooks.start_background(cmd, target, cwd=cwd, label=name)
        procs["bridge"] = hooks.start_background(bridge_cmd, target, cwd=cwd, label="bridge")
        for name, cmd in viewer_cmds:
            print(f"[viewer] {name}")
            procs[f"viewer:{name}"] = hooks.start_background(cmd, target, cwd=cwd, label=name)
        print(tr._c("32;1", f"[explore] READY - open Foxglove on {info['url']}"))
        if info["tunnel"]:
            print(f"[explore]   first, on the laptop: {info['tunnel']}")
        print(f"[explore]   Foxglove: Open connection -> Foxglove WebSocket -> {info['url']}")
        print(f"[explore]   {s.describe()}")
        print("[explore]   stop with Ctrl-C (or Stop on the run page); the stack comes down then")
        if controls is not None:
            controls.bind(hooks=hooks, target=target, cwd=cwd, out_dir=out_dir, spec=spec,
                          factors=factors, config=config, settings=s, clock=clock,
                          experiment=campaign.experiment, experiments_root=experiments_root,
                          placeholders=arg_ph)
            if s.actions:
                print("[explore]   actions on the run page: "
                      + ", ".join(a["name"] for a in s.actions))
        if on_ready is not None:
            on_ready(info)
        t0 = time.monotonic()
        while True:
            if stop is not None and stop.is_set():
                print("[explore] stop requested")
                break
            if max_s is not None and time.monotonic() - t0 >= max_s:
                print(f"[explore] {max_s:.0f} s elapsed - stopping")
                break
            rc = _exited(procs.get("launch_stack"))
            if rc is not None:
                result.status = "failed"
                result.detail = f"the stack exited by itself (rc={rc}): see run.log"
                print(tr._c("31;1", f"[explore] {result.detail}"))
                break
            time.sleep(poll_s)
    except KeyboardInterrupt:
        print("\n[explore] Ctrl-C - stopping")
    except Exception as exc:  # noqa: BLE001 - the finally still stops the stack
        result.status = "failed"
        result.detail = str(exc) if isinstance(exc, tr.RunnerError) else f"execution error: {exc}"
        print(tr._c("31;1", f"[explore] {result.detail}"))
    finally:
        if controls is not None:
            try:
                controls.shutdown()
            except Exception as exc:  # noqa: BLE001 - the stack must still come down
                result.detail = f"{result.detail} | ending the actions failed: {exc}".strip(" |")
        names = ([f"viewer:{n}" for n, _ in reversed(viewer_cmds)] + ["bridge"]
                 + [f"aux:{n}" for n, _ in reversed(aux_cmds)] + ["launch_stack", "params"])
        for name in names:
            proc = procs.get(name)
            # A local process that already exited has nothing left to stop, and
            # signalling its process group would fail with "No such process".
            if proc is None or (target is None and _exited(proc) is not None):
                continue
            try:
                hooks.stop_process(proc, target, name=name, kind="launch")
            except Exception as exc:  # noqa: BLE001
                result.detail = f"{result.detail} | stop {name} failed: {exc}".strip(" |")
        # The pump thread of the log may still be writing its last lines.
        time.sleep(0.5)
        events_file.close()
        try:
            lines = (out_dir / "parameter_events.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        result.summary = summarize(parse_events(lines))
        write_changes(out_dir / "parameter_changes.yaml", result.summary, {
            "experiment": campaign.experiment, "campaign": campaign.name,
            "condition": dict(spec.values), "started": started,
            "stopped": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": result.status, "writable": list(s.param_whitelist),
            # What the run page kept: the notes (also in notes.md) and the
            # scratch recordings, so this one file lists everything of the session.
            "notes": list(controls.notes) if controls is not None else [],
            "recordings": ([r["bag_dir"] for r in controls.recordings]
                           if controls is not None else [])})
        changed = change_lines(result.summary)
        print(f"[explore] {len(changed)} parameter(s) changed"
              + (f", {len(result.summary['reverted'])} set but ended at the starting value"
                 if result.summary["reverted"] else ""))
        if changed:
            print("[explore]   a change took effect only in nodes that read the parameter while "
                  "running; many read it once, at startup")
        for line in changed:
            print(f"[explore]   {line}")
        print(f"[explore] written to {out_dir / 'parameter_changes.yaml'}")
        sys.stdout = saved_stdout
        log_file.close()
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="trials explore",
        description="Bring up one campaign condition's stack with a Foxglove bridge that may change "
                    "the parameters runner.yaml allows. No gate, no recording, no action: never coverage.")
    ap.add_argument("campaign", type=Path)
    roots.add_root_arguments(ap)
    ap.add_argument("--set", action="append", default=[], metavar="name=value",
                    help="pin a factor (the first level of each sweep is used otherwise)")
    ap.add_argument("--label", default="", metavar="WORD",
                    help="a word for the output folder, <stamp>_explore_<label>")
    ap.add_argument("--minutes", type=float, default=None,
                    help="stop by itself after this long (default: until Ctrl-C)")
    ap.add_argument("--execute", action="store_true",
                    help="actually launch. Without this it only prints the commands.")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)
    try:
        campaign = cs.load_campaign(args.campaign, args.experiments_root, args.set)
        config = tr.RunnerConfig.load(campaign.experiment, args.experiments_root)
    except (cs.SpecError, tr.RunnerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not args.execute:
        print("=== DRY RUN - nothing will be launched ===")
    result = run_explore(campaign, config, args.experiments_root, dry_run=not args.execute,
                         label=args.label,
                         max_s=args.minutes * 60 if args.minutes else None)
    return 1 if result.status in ("refused", "failed") else 0


if __name__ == "__main__":
    sys.exit(main())

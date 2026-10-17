#!/usr/bin/env python3
"""Explore one campaign condition live: its stack, a Foxglove view that may change parameters.

Added 2026-10-16 (#131). Before a campaign runs, its author debugs and tunes the
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
* a parameter log (explore_params.py) that starts before the stack and, at
  stop, writes every parameter that changed (from, to) to
  ``runs/explore/<stamp>_explore[_<label>]/parameter_changes.yaml``.

And it leaves out what makes a trial a trial: no T-0 gate, no recorder, no
action, no trial_metadata.json, no attempt-ledger line. An exploration can
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
import re
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from . import campaign_spec as cs
from . import roots
from . import trial_runner as tr

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
    #: The experiment's `simulation:`. On hardware the UI asks for the arm checkbox.
    simulation: bool = False

    @property
    def writable(self) -> bool:
        return bool(self.param_whitelist)

    def describe(self) -> str:
        if not self.allowed:
            return f"Explore is not available: {self.reason}"
        opens = (", and it opens " + ", ".join(v["name"] for v in self.viewers)) if self.viewers else ""
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
    if config.simulation:
        return ExploreSettings(True, "", list(block.get("param_whitelist", [".*"])), topics,
                               port, address, viewers=viewers, simulation=True)
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
    return ExploreSettings(True, "", whitelist, topics, port, address, viewers=viewers)


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
    return ["python3", "-m", "robofacts.explore_params"]


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


def run_explore(campaign: "cs.Campaign", config: tr.RunnerConfig, experiments_root: Path, *,
                dry_run: bool = True, hooks: Optional[tr.ProcessHooks] = None,
                stop: Optional[threading.Event] = None, max_s: Optional[float] = None,
                label: str = "",
                confirm_steps: Optional[Callable[[cs.TrialSpec, List[Dict[str, Any]]], bool]] = None,
                on_ready: Optional[Callable[[Dict[str, Any]], None]] = None,
                clock: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
                poll_s: float = 0.5, logger_settle_s: float = 1.0) -> ExploreResult:
    """Bring up the campaign's first condition, hold it until ``stop`` is set, tear it down.

    The condition is the campaign's own first combination, as for a scratch
    recording: factor defaults, its `fixed` block, the first level of anything
    swept, and whatever ``--set`` (the UI's picked cell) pinned. The order is
    setup steps, parameter log, stack, aux launches, bridge. The parameter log
    starts before the stack so it hears every node declare its parameters.
    Teardown runs in reverse, in a ``finally``, so a crash still stops the stack.
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
    print("[explore]   no T-0 gate, no recorder, no action: an exploration never counts as coverage")

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
            "status": result.status, "writable": list(s.param_whitelist)})
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

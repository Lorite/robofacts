#!/usr/bin/env python3
"""Experiments control surface — the web UI (phase 5, redrawn from scratch).

Pick a combination, see how many bags it already has, run it, watch it, and open
the result — from the laptop browser, while the trial runs wherever it runs.

One information architecture on every page: an experiment, its campaigns
(each one a plan: wanted, covered, still missing, the gate's verdicts) and its
trials (one row per trial, every trial, with the slim bag to replay, the layout,
the video, the analyses that used it, the run log and the verify report). The
per-trial join is the package's ``trial_records`` (``trials records``), so the
table shows what the command line prints.

Deliberate design constraints
-----------------------------
* **A client, never a second implementation.** Every route calls the same
  functions the CLI does (``campaign_spec`` / ``campaign_plan`` / ``trial_runner``
  / ``analysis_runner`` / ``estop.sh``). If the UI can do something the CLI
  cannot, that is a bug: batch runs, CI and agent-driven work must keep working
  when the browser is closed.
* **Localhost by default.** Bound to 127.0.0.1 and reached over an SSH tunnel,
  which works on the lab LAN with no internet and no VPN, and is
  authenticated and encrypted by SSH before the password is even seen.
* **The password is defence-in-depth, not the control.** This UI can start a
  flight, so it never binds 0.0.0.0 without being told to, and refuses to start
  without a password unless explicitly waived.
* **Running is a two-step.** A plan is shown first; executing needs an explicit
  confirm. Nothing takes off because a link was prefetched.
* **Stops are a narrow allowlist**, read from ``robots.yaml`` — the same file
  ``scripts/estop.sh`` uses — never a general "run any command" endpoint.

    trials ui                       # this UI
    ssh -N -L 8080:localhost:8080 -L 8765:localhost:8765 <host> # from the laptop
"""

from __future__ import annotations

import errno
import math
import re
import os
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from flask import Flask, Response, redirect, render_template, request, send_file, url_for
from jinja2 import ChoiceLoader, FileSystemLoader


from robofacts import analysis_runner as ar
from robofacts import campaign_live_steps as cls_mod
from robofacts import code_version as cv
from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import backend as backends
from robofacts import trial_decisions as td
from robofacts import trial_index as ti
from robofacts import trial_records as trec
from robofacts import trial_runner as tr
from robofacts import roots
from robofacts import tools


def annotate_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Operator steps as the templates render them, titles from the ONE extractor.

    Starts from campaign_spec's normalized ``{text, run}`` dicts (so prose-only
    steps stay visible and the list indices stay the /operator_step allowlist)
    and merges in what campaign_live_steps.annotate() derives for the runnable
    ones: the pane-label ``title`` and the squashed one-liner ``cmd`` a cockpit
    pane would pre-type. The UI never re-derives either, so its panel and the
    tmux handover window can never disagree about a step.
    """
    out = [dict(step) for step in steps]
    for row in cls_mod.annotate(steps):
        out[row["index"]]["title"] = row["title"]
        out[row["index"]]["cmd"] = row["cmd"]
    return out


# --------------------------------------------------------------------------- #
# Run journal — what the UI is doing right now
# --------------------------------------------------------------------------- #
@dataclass
class RunJournal:
    """In-memory record of UI-triggered actions, newest first.

    Deliberately not persisted: the durable record of a trial is its bag and
    trial_metadata.json. This is only so the operator can see what the UI is
    doing without tailing a terminal.
    """
    entries: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, kind: str, summary: str, detail: str = "") -> None:
        with self._lock:
            self.entries.insert(0, {
                "at": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "kind": kind, "summary": summary, "detail": detail,
            })
            del self.entries[50:]

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.entries)


@dataclass
class ActiveRun:
    """A campaign executing in the background, as the browser sees it."""
    run_id: str
    experiment: str
    campaign: str
    #: The campaign FILE this run came from (the display name carries suffixes
    #: like [session]); the run page posts step indices against it.
    campaign_file: str
    wanted: int
    executed: bool
    results: List[Dict[str, Any]] = field(default_factory=list)
    state: str = "running"          # running | finished | aborting | aborted | error
    error: str = ""
    #: Everything the runner printed, newest last, capped. This is what the
    #: operator would have seen in the terminal — without it the browser shows
    #: only a status, and the reason a trial failed stays on a machine nobody
    #: is looking at.
    log: List[Dict[str, str]] = field(default_factory=list)
    #: Carried onto the run page: the operator's manual steps are needed most
    #: while the trial is running, and the campaign page is gone by then.
    operator_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: What the operator does WHILE this runs — shown most prominently, because
    #: a live step missed is a trial that has to be reflown.
    live_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: Non-empty while the runner is WAITING for the operator in the browser —
    #: the UI's mirror of the CLI's two Enter waits: ``steps`` before anything
    #: launches (operator_steps, 8f1213cc6e) and ``pose`` between the T-0 gate
    #: and the recorder (b0b42729ac). The worker thread parks on _go until
    #: /run/<id>/proceed is posted. Without this surface those waits would
    #: block on the SERVER's stdin: a UI started from a tmux pane keeps a tty,
    #: so isatty() alone cannot tell the UI's worker from a CLI operator.
    awaiting_kind: str = ""     # "" | "steps" | "pose"
    awaiting_what: str = ""     # the pending trial's description
    #: "campaign" (trials, dry or executed) or "explore" (explore.py: a live
    #: stack, never coverage). An exploration has its own page and its Stop
    #: takes the stack down at once, because nothing is recorded or flown.
    kind: str = "campaign"
    #: For an exploration: the condition, how to connect the live view, and at the
    #: end the parameters that changed and where the file is.
    explore: Dict[str, Any] = field(default_factory=dict)
    #: For an exploration: explore.ExploreControls, what the run page may
    #: do while the stack runs (actions, set back, record, notes).
    controls: Any = None
    _go: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)
    _abort: bool = False

    def append_log(self, text: str) -> None:
        """Classify each line so the browser can filter without re-parsing.

        `src` groups by where a line came from (the runner's own steps vs the
        stack vs the recorder), `lvl` marks anything worth not missing. Both
        are derived from the line's own prefix, which the runner and the pump
        threads already write.
        """
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line:
                continue
            low = line.lower()
            if any(k in line for k in ("[ERROR]", "FAIL", "Traceback", "error:",
                                       "NO-GO", "died", "Killed")):
                lvl = "err"
            elif any(k in line for k in ("[WARN]", "Warning", "warn:")):
                lvl = "warn"
            else:
                lvl = "info"
            # Match on the STRIPPED line: the gate indents its per-check
            # verdicts, and classifying those as "other" would hide the most
            # important lines behind an unrelated filter.
            head = line.lstrip()
            if head.startswith(("[runner]", "[explore]", "[viewer]", "[setup]", "[aux]", "[gate]", "[preflight]", "=== T-0",
                                "PREFLIGHT", "PASS", "FAIL",
                                "SKIP", "✓")) \
                    or "CHECK(S)" in line or "MACHINE CHECKS" in line:
                src = "runner"
            elif head.startswith("[stack]"):
                src = "stack"
            elif head.startswith("[recorder]"):
                src = "recorder"
            elif head.startswith(("[launch_stack]", "[aux:", "[record]", "[action:")):
                src = "runner"          # teardown bookkeeping
            elif re.match(r"\[[A-Za-z_][\w/]*\]", head):
                src = "nodes"           # a node's own "[name]" prefix
            else:
                src = "other"
            self.log.append({"src": src, "lvl": lvl, "text": line})
        # Deeper than the screen but bounded: the stack is chatty and the point
        # is to still have the first error after minutes of node output.
        del self.log[:-2000]

    def to_dict(self) -> Dict[str, Any]:
        explore = dict(self.explore)
        if self.controls is not None:
            # Live while the stack runs: the change table, the action, the recorder, the notes.
            explore["live"] = self.controls.status()
        return {"run_id": self.run_id, "experiment": self.experiment,
                "campaign": self.campaign, "state": self.state,
                "executed": self.executed, "wanted": self.wanted,
                "done": len(self.results), "results": list(self.results),
                "awaiting_kind": self.awaiting_kind,
                "awaiting_what": self.awaiting_what,
                "kind": self.kind, "explore": explore,
                # The CLI's closing count, same function, so the two surfaces
                # can never tell a different story about how the batch went.
                "tally": tr.tally(r["status"] for r in self.results),
                "log": list(self.log), "error": self.error}


#: The longest manual window (hold_s) /run accepts, in seconds.
MAX_HOLD_S = 3600.0

#: What the routes answer (409) when the rig's single run slot is taken.
SLOT_BUSY = ("A campaign or an exploration is already running. Two stacks sharing one rig "
             "would fight over the radio, the recorder and the airspace — wait for it, "
             "or stop it first.")


class RunSlotBusy(RuntimeError):
    """An executed run or an exploration was refused: another one holds the rig."""


class RunManager:
    """Runs a campaign off the request thread so the page can show progress.

    Only ONE executed run or exploration at a time, deliberately: two campaigns
    sharing one rig would fight over the radio, the recorder and the airspace.
    The guard is here rather than in the runner because it is a property of the
    lab, not of the code.

    A dry run launches nothing, so it never takes that slot. Before 2026-11-25
    every run took it: a dry run started during an executed campaign became the
    "current" run, ``busy()`` turned False, a second EXECUTE was accepted (two
    stacks on one rig), and the stop button aborted the dry run instead of the
    flight. The check also lives inside ``start`` and ``start_explore``, under
    the lock that claims the slot, so two EXECUTE posts at the same moment
    cannot both pass it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: Dict[str, ActiveRun] = {}
        #: The executed run or exploration that holds the rig, never a dry run.
        self._current: Optional[str] = None

    def _claim(self, run: ActiveRun, after: Optional[ActiveRun] = None) -> None:
        """Give the rig's slot to ``run``, or raise RunSlotBusy. The caller holds the lock.

        ``after`` is an exploration that ``run`` replaces: it is being stopped,
        and the new one waits for its stack to come down before it starts.
        """
        holder = self._runs.get(self._current) if self._current else None
        if holder is not None and holder is not after and holder.state in ("running", "aborting"):
            raise RunSlotBusy(SLOT_BUSY)
        self._current = run.run_id

    def current(self) -> Optional[ActiveRun]:
        with self._lock:
            return self._runs.get(self._current) if self._current else None

    def get(self, run_id: str) -> Optional[ActiveRun]:
        with self._lock:
            return self._runs.get(run_id)

    def all(self) -> List[ActiveRun]:
        """Every run this UI started since it came up, newest first."""
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.run_id, reverse=True)

    def busy(self) -> bool:
        run = self.current()
        return run is not None and run.state in ("running", "aborting")

    def abort(self, run_id: str) -> bool:
        """Ask the campaign to stop AFTER the trial in flight finishes.

        It never interrupts a trial mid-air: stopping the aircraft is the
        e-stop's job, and killing a recorder mid-write loses the bag.
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.state != "running":
                return False
            run._abort = True
            run.state = "aborting"
            # An exploration records nothing and flies nothing, so its Stop
            # takes the stack down now instead of after "the current trial".
            run._stop.set()
            # A worker parked on a browser confirmation must wake up to see
            # the abort, or "abort" would leave the run waiting forever for a
            # confirmation nobody intends to give. The waiting callback
            # re-checks _abort after waking and refuses the trial.
            run._go.set()
            return True

    def start(self, *, experiment: str, campaign_name: str, campaign_file: str, plan, config,
              bags_dir: Path, executed: bool, limit: int, journal,
              gate=None, steps=None, hold_s: float = 60.0,
              keep_stack: bool = False,
              pause_before_action: bool = False,
              operator_steps: Optional[List[str]] = None,
              live_steps: Optional[List[str]] = None,
              extra_verify: Optional[List[Dict[str, Any]]] = None) -> ActiveRun:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        run = ActiveRun(run_id=run_id, experiment=experiment, campaign=campaign_name,
                        campaign_file=campaign_file,
                        wanted=min(limit, plan.needed) if limit else plan.needed,
                        executed=executed, operator_steps=list(operator_steps or []),
                        live_steps=list(live_steps or []))
        with self._lock:
            if executed:
                self._claim(run)
            self._runs[run_id] = run

        def record(result) -> None:
            run.results.append({
                "status": result.status,
                "describe": result.trial.describe(),
                "bag": result.bag_dir.name if result.bag_dir else None,
                "detail": result.detail,
                "commands": [" ".join(c) for c in result.commands],
            })

        class _Tee:
            """Mirror the runner's stdout into the run log AND the terminal.

            The runner prints its stage transitions, gate verdicts and the
            streamed output of remote commands; a UI that cannot see them
            forces the operator back to the terminal the UI exists to replace.

            Caveat: ``redirect_stdout`` swaps the process-wide ``sys.stdout``,
            so anything else printing while a run is active also lands in that
            run's log. Tolerable only because RunManager permits exactly one
            executed run at a time and nothing else here writes to stdout (a
            dry run beside it prints only its plan, which may then show in
            both logs); if either stops being true, pass an explicit log
            callback into run_plan instead.
            """

            def __init__(self, stream, sink):
                self._stream, self._sink = stream, sink

            def write(self, text):
                self._sink.append_log(text)
                return self._stream.write(text)

            def flush(self):
                self._stream.flush()

        def wait_in_browser(kind: str, what: str, log_line: str) -> bool:
            """The UI's confirmation surface for the runner's operator waits.

            The CLI waits for Enter at a tty; here the worker thread parks
            until the operator clicks the confirm button on the run page —
            the run's state says so in the meantime, so the page never just
            looks hung. Waking because of an abort refuses the trial.
            """
            run._go.clear()
            run.awaiting_kind, run.awaiting_what = kind, what
            run.append_log(log_line)
            run._go.wait()
            run.awaiting_kind = run.awaiting_what = ""
            return not run._abort

        def confirm_steps(trial, steps_list) -> bool:
            return wait_in_browser(
                "steps", trial.describe(),
                "[runner] waiting for the operator steps to be confirmed in "
                "the browser — nothing has launched")

        def confirm_pose(trial) -> bool:
            return wait_in_browser(
                "pose", trial.describe(),
                "[runner] T-0 gate done — waiting in the browser: pose the "
                "rig, then confirm to record and run the action")

        def worker() -> None:
            import contextlib
            try:
                with contextlib.redirect_stdout(_Tee(sys.stdout, run)):
                    tr.run_plan(plan, config, bags_dir, dry_run=not executed, limit=limit,
                                slim=executed, on_result=record, gate=gate,
                                steps=steps, hold_s=hold_s, keep_stack=keep_stack,
                                extra_verify=extra_verify,
                                confirm_steps=confirm_steps,
                                confirm_pose=confirm_pose,
                                pause_before_action=pause_before_action,
                                should_continue=lambda: not run._abort)
                    if not run.results and not plan.needed:
                        # The CLI's own line for this case (trial_runner.main), so an
                        # empty run page says why instead of looking broken.
                        print("Nothing to run — the campaign is already covered.", flush=True)
                run.state = "aborted" if run._abort else "finished"
            except Exception as exc:  # noqa: BLE001 - surface it, never hang "running"
                run.state = "error"
                run.error = f"{type(exc).__name__}: {exc}"
            finally:
                # "dry-run" vs "EXECUTE" — a dry run logged as a finished "plan"
                # reads like something flew (real confusion, first hardware day).
                # Executed runs get the CLI's own closing tally, successes first.
                count = (tr.tally(r["status"] for r in run.results)["line"]
                         if executed else f"{len(run.results)} trial(s)")
                journal.add("EXECUTE" if executed else "dry-run",
                            f"{experiment}/{campaign_name}: {count}, "
                            f"{run.state}{' (nothing was launched)' if not executed else ''}",
                            run.error)

        threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
        return run

    def start_explore(self, *, experiment: str, campaign, campaign_file: str, label: str,
                      config, root: Path, journal, operator_steps=None,
                      layouts: Optional[List[str]] = None,
                      picker: Optional[List[Dict[str, Any]]] = None,
                      after: Optional[ActiveRun] = None) -> ActiveRun:
        """Run explore.run_explore off the request thread, until Stop.

        Same single slot as a campaign run: an exploration owns the stack, so a
        trial started next to it would launch a second one on the same graph.
        """
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        spec = next(campaign.combinations())
        engine = explore_engine(root)
        s = engine.settings(config)
        run = ActiveRun(run_id=run_id, experiment=experiment, campaign=label,
                        campaign_file=campaign_file, wanted=0, executed=True, kind="explore",
                        operator_steps=list(operator_steps or []),
                        explore={"condition": dict(spec.values), "describe": spec.describe(),
                                 "settings": s.describe(), "ready": False,
                                 "actions": [a["name"] for a in s.actions],
                                 "jog": dict(s.jog),
                                 "simulation": s.simulation, "layouts": list(layouts or []),
                                 # The campaign's swept factors, for "restart with another
                                 # condition" on the run page.
                                 "picker": list(picker or []),
                                 "after": after.run_id if after is not None else ""},
                        controls=engine.ExploreControls())
        with self._lock:
            self._claim(run, after=after)
            self._runs[run_id] = run

        class _Tee:
            def __init__(self, stream, sink):
                self._stream, self._sink = stream, sink

            def write(self, text):
                self._sink.append_log(text)
                return self._stream.write(text)

            def flush(self):
                self._stream.flush()

        def confirm_steps(trial, steps_list) -> bool:
            run._go.clear()
            run.awaiting_kind, run.awaiting_what = "steps", trial.describe()
            run.append_log("[explore] waiting for the operator steps to be confirmed in the "
                           "browser - nothing has launched")
            run._go.wait()
            run.awaiting_kind = run.awaiting_what = ""
            return not run._abort

        def on_ready(info) -> None:
            run.explore.update(info)
            run.explore["ready"] = True

        def worker() -> None:
            import contextlib
            try:
                if after is not None:
                    # A restart: the previous exploration's stack must be down first,
                    # or two stacks would share one graph.
                    run.append_log(f"[explore] waiting for exploration {after.run_id} to stop")
                    deadline = time.monotonic() + 300
                    while after.state in ("running", "aborting"):
                        if time.monotonic() > deadline:
                            raise RuntimeError(f"exploration {after.run_id} did not stop in 300 s")
                        time.sleep(0.5)
                    run.append_log(f"[explore] exploration {after.run_id} is {after.state}: starting")
                with contextlib.redirect_stdout(_Tee(sys.stdout, run)):
                    result = engine.run_explore(campaign, config, root, dry_run=False,
                                            stop=run._stop, label=campaign_file,
                                            confirm_steps=confirm_steps, on_ready=on_ready,
                                            controls=run.controls)
                run.explore.update({
                    "ready": False, "status": result.status, "detail": result.detail,
                    "changed": result.summary["changed"],
                    "reverted": [f"{t['node']}.{t['parameter']}" for t in result.summary["reverted"]],
                    "out_dir": (str(result.out_dir.relative_to(root / experiment))
                                if result.out_dir and result.out_dir.is_dir() else "")})
                run.state = "error" if result.status in ("failed", "refused") else "finished"
                run.error = result.detail if run.state == "error" else ""
            except Exception as exc:  # noqa: BLE001 - surface it, never hang "running"
                run.state = "error"
                run.error = f"{type(exc).__name__}: {exc}"
            finally:
                changed = len(run.explore.get("changed") or [])
                journal.add("explore", f"{experiment}/{label}: {run.state}, "
                                       f"{changed} parameter(s) changed", run.error)

        threading.Thread(target=worker, name=f"explore-{run_id}", daemon=True).start()
        return run


def explore_settings(root: Path, experiment: str):
    """Whether this experiment can be explored, and how, for the pages' buttons."""
    engine = explore_engine(root)
    if engine is None:
        return ExploreUnavailable()
    try:
        return engine.settings(tr.RunnerConfig.load(experiment, root))
    except tr.RunnerError as exc:
        return engine.ExploreSettings(False, f"its runner.yaml does not load ({exc})", [], [], 0, "")


def summarize_gate_failure(output: str) -> str:
    """The FAILING checks first, then the count — never a raw tail.

    The gate prints failures, then a long operator-confirmation block, then the
    summary. Showing the last N lines therefore showed the part that did NOT
    fail: a real no-go read as a wall of unticked checkboxes with the actual
    cause scrolled off (2026-07-29, first EXECUTE).
    """
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    failures = [ln for ln in lines if ln.startswith("FAIL") or " FAIL " in f" {ln} "]
    summary = next((ln for ln in reversed(lines) if "NO-GO" in ln), "")
    if not failures:
        # Nothing parsed as a check failure — the gate itself broke (missing
        # script, ssh error). Show the tail so that is visible rather than
        # silently reported as "no failing checks".
        return (summary or "gate failed without naming a check") + "\n" + "\n".join(lines[-6:])
    return "\n".join(failures + ([summary] if summary else []))


class _GatePass:
    def as_metadata(self):
        # verdict/status/mode in the gate's own words, so `trials records` and the
        # trials table read a UI-gated trial like a CLI-gated one.
        return {"gate": "preflight.sh", "result": "pass", "verdict": "GO", "status": "pass",
                "mode": "binding", "at": datetime.now(timezone.utc).isoformat()}


class ShellPreflightGate:
    """T-0 gate for executed runs: the experiment's own preflight.sh, run on
    the trial host before EACH trial (a check that passed minutes ago is
    worthless). A NO-GO raises, which the runner reports as a blocked trial."""

    def __init__(self, experiment: str, config, host_level, repo_root: Path,
                 light: bool = False, checklist: str = ""):
        self.experiment, self.config = experiment, config
        self.host_level, self.repo_root = host_level, repo_root
        #: Campaign-specific checklist (relative to the experiment dir), or "".
        self.checklist = checklist
        #: In a continuous session the stack is already up and healthy; bouncing
        #: a middleware daemon between trials would disturb the very thing being
        #: kept alive. The checks still run — only the bounce is skipped.
        self.light = light

    def check(self, name: str, values: Optional[Dict[str, Any]] = None):
        cmd = ["bash", f"experiments/{self.experiment}/scripts/preflight.sh"]
        if self.light:
            cmd.append("--no-daemon-bounce")
        # This CAMPAIGN's gate, when it declares one. A later --checklist wins in
        # run_preflight.py's argparse, so preflight.sh needs no change. The gate is
        # a positive list of what must be true for these trials — not the
        # experiment's list minus some exceptions.
        if self.checklist:
            cmd += ["--checklist", f"experiments/{self.experiment}/{self.checklist}"]
        target = self.config.remote_target(self.host_level)
        if target is not None:
            from robofacts.remote_exec_utils import wrap_command
            cmd = wrap_command(cmd, target)
        elif self.config.local_setup(self.host_level) is not None:
            # On the rig itself, the host's named setup files apply too (2026-10-14).
            from robofacts.remote_exec_utils import local_setup_command
            shell, files = self.config.local_setup(self.host_level)
            cmd = local_setup_command(cmd, files, shell)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=360,
                              cwd=None if target else str(self.repo_root))
        # Print the whole gate report so it reaches the terminal AND the run
        # log: the per-check PASS/FAIL lines are the most useful thing an
        # operator can read, and capturing them into an exception message only
        # (as this did at first) hid the passing context of a failure.
        print(proc.stdout.rstrip() or proc.stderr.rstrip(), flush=True)
        if proc.returncode != 0:
            raise RuntimeError("T-0 GATE NO-GO — " + summarize_gate_failure(
                proc.stdout + "\n" + proc.stderr))
        return _GatePass()


def ui_gate_source(experiments_root: Path, experiment: str, campaign) -> str:
    """Which T-0 gate an EXECUTE from this interface builds: "preflight.sh", "checklist" or "".

    The same order as the /run route: the experiment's scripts/preflight.sh, else the
    campaign's checklist (or the experiment's default one), the file tr.build_gate reads.
    "" means the trials would run with no gate at all.
    """
    if (Path(experiments_root) / experiment / "scripts" / "preflight.sh").is_file():
        return "preflight.sh"
    rel = getattr(campaign, "preflight_checklist", "") or "config/preflight_checklist.json"
    if (Path(experiments_root) / campaign.experiment / rel).is_file():
        return "checklist"
    return ""


def automatic_sequence(config, steps: Optional[Dict[str, bool]] = None,
                       campaign_setup: Optional[List[Dict[str, Any]]] = None,
                       gated: bool = True) -> List[str]:
    """What EXECUTE does by itself, in order, derived from runner.yaml.

    Derived rather than written out, so it cannot drift from what the runner
    actually does — the failure this exists to prevent is an operator reading a
    stale description and assuming a step is covered when it is not. That is
    why an ungated run (``gated=False``: no preflight.sh and no checklist) says
    so instead of promising a T-0 gate.
    """
    steps = {**tr.DEFAULT_STEPS, **(steps or {})}
    seq: List[str] = []
    if steps["setup"]:
        # runner.yaml's, then the campaign's (2026-11-04), each with its `when`.
        for s in list(config.setup_steps) + list(campaign_setup or []):
            if "_invalid" in s:
                continue
            when = s.get("when") or "always"
            label = "" if when == "always" else f"  [{when} trials only]"
            seq.append(f"{s['name']}  ({s['command'].split()[0]}…){label}")
    if steps["launch"]:
        seq.append(f"launch {config.launch_package}/{config.launch_file}, "
                   f"wait {config.settle_s:g}s to settle")
        seq += [f"start aux: {a['name']}" for a in config.aux_launches]
    if steps["livestream"] and config.livestream:
        seq.append("start the livestream bridge")
    if gated:
        seq.append("run the T-0 gate (blocks the trial if it fails)")
    else:
        seq.append("NO T-0 gate: no scripts/preflight.sh and no checklist, so every trial "
                   "runs UNGATED")
    if steps["record"]:
        seq.append("start the bag recorder")
    if steps["action"]:
        seq.append("run the action")
    else:
        seq.append("HOLD the stack open for the manual window (no automatic action)")
    seq.append("stop the recorder first, then tear the stack down")
    seq += [f"verify: {v['name']}" for v in config.verify_steps]
    if steps["record"]:
        seq.append("write trial_metadata.json + sync a slim copy back")
    return seq


def load_preflight_checklist(experiments_root: Path, experiment: str):
    """The pre-flight checklist as (items, variables) for display — [] if none exists.

    Read directly from the JSON the gate itself uses, so the text shown can
    never drift from the checks run (the same no-drift property the pre-flight test
    suite enforces).
    """
    path = experiments_root / experiment / "config" / "preflight_checklist.json"
    if not path.is_file():
        return [], {}
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}
    # The checklist mixes two shapes on purpose: a machine check is an object
    # ({text, check, args}), while an operator confirmation is just a string.
    # Normalise here so the template renders one shape — rendering the raw list
    # showed the string entries as empty bullets, which is worse than omitting
    # them: an operator confirmation you cannot read is one you cannot do.
    items = []
    for entry in data.get("checklist", []):
        if isinstance(entry, str):
            items.append({"text": entry, "check": None})
        elif isinstance(entry, dict):
            items.append({"text": str(entry.get("text", "")).strip(),
                          "check": entry.get("check")})
    return items, dict(data.get("variables", {}))


def discover_campaigns(experiments_root: Path) -> Dict[str, List[Path]]:
    out: Dict[str, List[Path]] = {}
    for path in sorted(experiments_root.glob("*/campaigns/*.yaml")):
        out.setdefault(path.parents[1].name, []).append(path)
    return out


def code_cell(version: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """{short, dirty, title} for a trial's code version (stamped since 2026-08-04)."""
    if not version or not version.get("commit"):
        why = (version or {}).get("error") or "recorded before code stamping"
        return {"short": "—", "dirty": False, "title": f"no code version: {why}"}
    title = cv.summary(version)
    if version.get("dirty"):
        shown = version.get("dirty_files", [])
        title += "\nuncommitted: " + ", ".join(shown)
        if version.get("dirty_file_count", 0) > len(shown):
            title += f", +{version['dirty_file_count'] - len(shown)} more"
    return {"short": version["commit"][:8], "dirty": bool(version.get("dirty")), "title": title}


def dir_mb(d: Optional[Path]) -> Optional[float]:
    if d is None:
        return None
    try:
        return round(sum(f.stat().st_size for f in d.iterdir() if f.is_file()) / 1e6, 1)
    except OSError:
        return None


def parse_where_args(values: List[str]) -> Dict[str, str]:
    """``k=v`` tokens (a select's value may carry several, space-separated) as a dict."""
    out: Dict[str, str] = {}
    for value in values:
        for token in str(value).split():
            if "=" in token:
                key, _, val = token.partition("=")
                if key.strip():
                    out[key.strip()] = val.strip()
    return out


def where_string(where: Dict[str, Any]) -> str:
    return " ".join(f"{k}={where[k]}" for k in sorted(where))


#: Files the /files route may serve from an experiment: its results, its run logs
#: and ledgers, its declarations, and the small sidecars of its bags. Recordings
#: themselves go through the public /bag route (slim only), never through here.
def file_roots(root: Optional[Path] = None) -> Tuple[str, ...]:
    """The top folders of an experiment that the files route serves: the declarations, the
    runs, the results and the recording folders, which the backend names."""
    return ("results", "runs", "config", "campaigns") + tuple(backends.record_dirs(root).values())


def __getattr__(name):
    """``FILE_ROOTS``, the constant from before the backends named the folders."""
    if name == "FILE_ROOTS":
        return file_roots()
    raise AttributeError(name)
def recording_suffixes(root: Optional[Path] = None) -> Tuple[str, ...]:
    """The suffixes of the recorder's data files, which the files route does not serve to an
    operating interface (the recorder's optional ``data_suffixes``)."""
    try:
        return tuple(getattr(backends.active(root).recorder, "data_suffixes", ()))
    except backends.BackendError:
        return ()


def recording_noun(root: Optional[Path] = None) -> str:
    """The backend's word for one recording (Recorder.noun), for the pages' text."""
    try:
        return backends.active(root).recorder.noun
    except backends.BackendError:
        return "recording"


def ui_extension(root: Optional[Path] = None):
    """The module with which the project's backend extends this interface (its feature
    ``ui``), or None. An extension may offer ``TEMPLATES`` (a folder searched before this
    package's: the pages it adds, and its own ``_viewers.html``, which replaces this
    package's), ``template_globals()``, ``layouts(root, experiment)`` and ``register(ui)``,
    which adds its routes."""
    try:
        return backends.active(root).feature("ui")
    except backends.BackendError:
        return None


def explore_engine(root: Optional[Path] = None):
    """The Explore engine of the project's backend (its feature ``explore``), or None."""
    try:
        return backends.active(root).feature("explore")
    except backends.BackendError:
        return None


@dataclass
class ExploreUnavailable:
    """What the pages are given where the backend offers no Explore (they show no control)."""
    allowed: bool = False
    reason: str = "the project's backend offers no Explore"
    simulation: bool = False
    writable: bool = False
    viewers: List[Dict[str, Any]] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    jog: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return f"Explore is not available: {self.reason}"


class ExperimentData:
    """What one experiment's pages need, built from the package's functions.

    Built per request but memoized on a cheap signature (the modification times
    of the experiment's data directories) and a short time to live: an index of
    250 trials is joined with the ledgers and the results once, not on every
    click, and a new bag, ledger line or result folder invalidates it at once.
    """

    TTL_S = 15.0

    def __init__(self, root: Path, experiment: str):
        self.root, self.experiment = root, experiment
        self.campaigns: Dict[str, Any] = {}
        self.campaign_errors: Dict[str, str] = {}
        for path in sorted((root / experiment / "campaigns").glob("*.yaml")):
            try:
                self.campaigns[path.stem] = cs.load_campaign(path, root)
            except cs.SpecError as exc:
                self.campaign_errors[path.stem] = str(exc)
        self.records = trec.load_records(root, experiment, campaigns=self.campaigns)
        self.trials = ti.load_trials(root, experiment)
        ext = ui_extension(root)
        self.layouts = list(ext.layouts(root, experiment)) if hasattr(ext, "layouts") else []
        self.explore = explore_settings(root, experiment)
        # Factor defaults, so a cell can mark its non-default values like the grid does.
        self.defaults: Dict[str, str] = {}
        for campaign in self.campaigns.values():
            for name, factor in campaign.factors.items():
                if getattr(factor, "default", None) is not None:
                    self.defaults.setdefault(name, str(factor.default))
        self._plans: Dict[Tuple[str, bool], Any] = {}

    def plan(self, campaign: str, include_unverified: bool = False):
        key = (campaign, include_unverified)
        if key not in self._plans:
            self._plans[key] = cp.build_plan(self.campaigns[campaign], self.trials,
                                             include_unverified)
        return self._plans[key]

    @property
    def campaign_records(self) -> List[trec.TrialRecord]:
        return [r for r in self.records if r.kind == "campaign"]

    @property
    def scratch_records(self) -> List[trec.TrialRecord]:
        return [r for r in self.records if r.kind == "scratch"]

    def varying_factors(self, records: Optional[List[trec.TrialRecord]] = None) -> Tuple[List[str], Dict[str, str]]:
        """(factors whose value differs between the trials, factors shared by all of them).

        The cell column shows only what varies; what every trial shares is said
        once above the table, so 180 rows do not repeat ``execution_host=laptop``.
        """
        records = self.campaign_records if records is None else records
        seen: Dict[str, set] = {}
        for r in records:
            for k, v in r.values.items():
                seen.setdefault(k, set()).add(v)
        factors = sorted(seen)
        varying = [f for f in factors if len(seen[f]) > 1
                   or any(f not in r.values for r in records)]
        shared = {f: next(iter(seen[f])) for f in factors if f not in varying}
        return varying, shared

    def factor_levels(self, records: Optional[List[trec.TrialRecord]] = None) -> Dict[str, List[str]]:
        """The levels each varying factor takes among the trials: one select per factor,
        so a filter is any subset of ``--where`` pairs, as on the command line."""
        records = self.campaign_records if records is None else records
        varying, _ = self.varying_factors(records)
        levels: Dict[str, set] = {f: set() for f in varying}
        for r in records:
            for f in varying:
                if f in r.values:
                    levels[f].add(r.values[f])
        def order(v: str):
            try:
                return (0, float(v), v)
            except ValueError:
                return (1, 0.0, v)
        return {f: sorted(vals, key=order) for f, vals in levels.items() if vals}

    @staticmethod
    def status_counts(records: List[trec.TrialRecord]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for r in records:
            counts[r.status] = counts.get(r.status, 0) + 1
        return {s: counts[s] for s in trec.STATUSES if s in counts}

    @staticmethod
    def class_summary(records: List[trec.TrialRecord]) -> List[Dict[str, Any]]:
        """The paper's three classes, then the attempts that left no trial: one badge each.

        Each entry is {filter, label, css, n, title}; ``filter`` is the --status value
        that lists exactly those rows, and an excluded entry says why in its title.
        """
        by = ExperimentData.status_counts(records)
        out: List[Dict[str, Any]] = []
        for c in ("usable", "unverified"):
            if by.get(c):
                out.append({"filter": c, "label": c, "css": c, "n": by[c],
                            "title": trec.STATUS_MEANING[c]})
        excluded = [(s, n) for s, n in by.items() if trec.CLASS_OF[s] == "excluded"]
        if excluded:
            out.append({"filter": "excluded", "label": "excluded", "css": "excluded",
                        "n": sum(n for _, n in excluded),
                        "title": "never counts: " + ", ".join(f"{n} {s.replace('-', ' ')}"
                                                              for s, n in excluded)})
        for s, n in by.items():
            if trec.CLASS_OF[s] is None:
                out.append({"filter": s, "label": f"{s} attempt" + ("s" if n > 1 else ""),
                            "css": s, "n": n, "title": trec.STATUS_MEANING[s]})
        return out

    def record(self, name: str) -> Optional[trec.TrialRecord]:
        return next((r for r in self.records if r.name == name), None)


class ExperimentCache:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[tuple, float, ExperimentData]] = {}

    def _signature(self, experiment: str) -> tuple:
        sig = []
        base = self.root / experiment
        for sub in ("campaigns", "config", "runs", "results") + trec.record_dirs_of(self.root) + trec.scratch_dirs_of(self.root):
            try:
                sig.append((sub, (base / sub).stat().st_mtime_ns))
            except OSError:
                sig.append((sub, 0))
        for ledger in ("runs/attempts.jsonl", "runs/preflight_gate.jsonl"):
            try:
                st = (base / ledger).stat()
                sig.append((ledger, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((ledger, 0, 0))
        return tuple(sig)

    def get(self, experiment: str) -> ExperimentData:
        sig = self._signature(experiment)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(experiment)
            if entry and entry[0] == sig and now - entry[1] < ExperimentData.TTL_S:
                return entry[2]
        data = ExperimentData(self.root, experiment)
        with self._lock:
            self._entries[experiment] = (sig, now, data)
        return data

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _host_port(netloc: str, scheme: str) -> Optional[Tuple[str, int]]:
    """(host, port) of a ``host[:port]``, the port defaulted from the scheme, or None."""
    try:
        parts = urlsplit(f"//{netloc}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname.lower(), port if port is not None else _DEFAULT_PORTS.get(scheme, -1)


def foreign_origin(origin: Optional[str], referer: Optional[str], host: str) -> str:
    """The origin a POST came from when it is not ``host`` (this interface), else "".

    The Origin header decides, the Referer only when there is no Origin, and a
    request with neither is not refused: it does not come from a browser form.
    An origin that names no host (``null``, from a sandboxed frame or a file)
    is foreign. The port of either side defaults from the origin's scheme, the
    way a browser leaves the default port out of both headers.
    """
    source = (origin or "").strip() or (referer or "").strip()
    if not source:
        return ""
    try:
        parts = urlsplit(source)
    except ValueError:
        return source
    if parts.scheme not in _DEFAULT_PORTS or not parts.netloc:
        return source
    theirs = _host_port(parts.netloc, parts.scheme)
    ours = _host_port(host, parts.scheme)
    if theirs is None or ours is None or theirs != ours:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def experiment_names(root: Path) -> List[str]:
    """Framework experiments: a folder with campaigns or a runner.yaml (offline studies have neither)."""
    names = {p.parents[1].name for p in root.glob("*/campaigns/*.yaml")}
    names |= {p.parents[1].name for p in root.glob("*/config/runner.yaml")}
    names.discard("common")
    return sorted(names)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def create_app(experiments_root: Optional[Path] = None,
               password: Optional[str] = None,
               repo_root: Optional[Path] = None,
               user: Optional[str] = None,
               view_only: bool = False,
               cache_dir: Optional[Path] = None) -> Flask:
    """The UI app. ``view_only`` registers none of the routes that act (runs,
    stops, robot actions, the backend's own actions, exclude, restore), for an archive viewer such as
    the home server's: those URLs answer 404 and the pages show no button for
    them. ``cache_dir`` is where poster frames go when the experiments tree
    itself is read-only (default: ``runs/posters/`` inside the tree)."""
    experiments_root = roots.experiments_root(experiments_root)
    repo_root = roots.project_root(repo_root, experiments=experiments_root)
    app = Flask(__name__)
    extension = ui_extension(experiments_root)
    if getattr(extension, "TEMPLATES", None):
        # The backend's pages and its _viewers.html come first.
        app.jinja_loader = ChoiceLoader([FileSystemLoader(str(extension.TEMPLATES)), app.jinja_loader])
    app.config["EXPERIMENTS_ROOT"] = Path(experiments_root)
    app.config["REPO_ROOT"] = Path(repo_root)
    app.config["PASSWORD"] = password
    # Optional user name (EXPERIMENTS_UI_USER). Unset means any user name, as before
    # 2026-10-20, so existing setups keep working.
    app.config["USER"] = user
    app.config["VIEW_ONLY"] = bool(view_only)
    app.config["CACHE_DIR"] = Path(cache_dir) if cache_dir else None
    journal = RunJournal()
    app.config["JOURNAL"] = journal
    runs = RunManager()
    app.config["RUNS"] = runs  # main() checks this for a safe Ctrl-C message
    cache = ExperimentCache(Path(experiments_root))
    app.config["CACHE"] = cache

    def requires_auth(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            expected = app.config.get("PASSWORD")
            if expected:
                auth = request.authorization
                # compare_digest on both fields: a plain `!=` leaks length and
                # content through timing, and this endpoint can start a flight.
                expected_user = app.config.get("USER")
                # As UTF-8 bytes: compare_digest raises TypeError on a str with
                # non-ASCII characters, which answered 500 instead of 401.
                eq = lambda a, b: secrets.compare_digest((a or "").encode("utf-8"), b.encode("utf-8"))
                ok = (auth is not None
                      and eq(auth.password, expected)
                      and (not expected_user or eq(auth.username, expected_user)))
                if not ok:
                    return Response(
                        "Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="experiments"'})
            return view(*args, **kwargs)
        return wrapper

    def action(rule: str):
        """A POST route that changes something: a run, a stop, a robot action, a move.

        A view-only app does not register it at all, so the URL answers 404 rather
        than relying on a hidden button. Templates guard their forms with
        ``view_only``, and a url_for() of an unregistered action fails loudly in
        the view-only page tests. A registered action refuses (403) a POST that
        a browser sent from a page of another site (``foreign_origin``).
        """
        def register(view):
            if app.config["VIEW_ONLY"]:
                return view

            @wraps(view)
            def same_site_only(*args, **kwargs):
                # Cross-site request forgery (2026-11-25): a page on any other site could
                # post a form here, and the browser added the cached Basic credentials,
                # so a link could start a run or a stop. A browser names the page a POST
                # comes from; a request from another host:port is refused. A POST with
                # neither header (curl, scripts, the tests) is not a browser form.
                expected = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
                source = foreign_origin(request.headers.get("Origin"),
                                        request.headers.get("Referer"), expected or request.host)
                if source:
                    return error(f"Refused: this request came from {source}, not from this "
                                 f"interface ({expected or request.host}). A page on another site "
                                 f"cannot run, stop or move anything here, even while your browser "
                                 f"holds the password. Open the interface itself and use its "
                                 f"buttons.", 403)
                return view(*args, **kwargs)
            return app.route(rule, methods=["POST"])(same_site_only)
        return register

    def root() -> Path:
        return app.config["EXPERIMENTS_ROOT"]

    def base_url() -> str:
        return request.host_url.rstrip("/")

    def known_experiment(experiment: str) -> bool:
        return experiment in experiment_names(root())

    def campaign_file(experiment: str, name: str) -> Optional[Path]:
        """The campaign file ``name`` of ``experiment``, or None when the pair names no such place.

        The experiment must be one this root holds, the name a plain name (no
        separator, not hidden), and the resolved file must lie in that
        experiment's campaigns/ folder. Before 2026-11-25 the form fields were
        joined onto the path as given, so ``../../../../outside/planted`` loaded
        any YAML on the host as a campaign, and /operator_step and /run executed
        its commands. The file need not exist: loading it says why it does not.
        """
        if not known_experiment(experiment):
            return None
        if not name or "/" in name or "\\" in name or "\0" in name or name.startswith("."):
            return None
        folder = (root() / experiment / "campaigns").resolve()
        path = (folder / f"{name}.yaml").resolve()
        try:
            path.relative_to(folder)
        except ValueError:
            return None
        return path

    def error(message: str, status: int = 400, crumbs=None, title: str = ""):
        return render_template("error.html", message=message, crumbs=crumbs or [],
                               title=title), status

    @app.context_processor
    def inject_globals():
        # The chrome every page shares: the run in progress (so a campaign
        # running in the background is one click away from any page), the stop
        # bar's robots, and the helpers the shared macros need.
        current = runs.current()
        return {
            "active_run": current if current is not None and current.state in
            ("running", "aborting") else None,
            "status_meaning": trec.STATUS_MEANING, "class_of": trec.CLASS_OF,
            **(extension.template_globals() if hasattr(extension, "template_globals") else {}),
            "explore_offered": explore_engine(root()) is not None,
            # The folder names and the word for one recording, from the project's backend.
            "dirs": backends.record_dirs(root()), "noun": recording_noun(root()),
            "base_url": base_url(), "code_cell": code_cell,
            "where_string": where_string,
            "poster_of": lambda record: record.poster(root()),
            "view_only": app.config["VIEW_ONLY"],
            "archive_status": archive_status(root()) if app.config["VIEW_ONLY"] else None,
        }

    @app.template_filter("when")
    def _when(value: str) -> str:
        return (value or "").replace("T", " ")[:16]

    @app.template_filter("relpath")
    def _relpath(path: Optional[Path], experiment: str) -> str:
        if path is None:
            return ""
        try:
            return str(Path(path).relative_to(root() / experiment))
        except ValueError:
            return str(path)

    # ------------------------------------------------------------ overview --
    @app.route("/")
    @requires_auth
    def index():
        rows = []
        for name in experiment_names(root()):
            data = cache.get(name)
            missing = 0
            for cname in data.campaigns:
                missing += data.plan(cname).needed
            records = data.campaign_records
            rows.append({
                "name": name, "campaigns": len(data.campaigns) + len(data.campaign_errors),
                "broken": len(data.campaign_errors), "missing": missing,
                "classes": data.class_summary(records), "total": len(records),
                "scratch": len(data.scratch_records),
                "replayable": sum(1 for r in data.records if r.slim_dir is not None),
                "last": records[0].started if records else "",
                "layouts": data.layouts,
                "explore": data.explore,
                "explore_campaign": next(iter(data.campaigns), ""),
            })
        rows.sort(key=lambda r: r["last"], reverse=True)
        view_only = app.config["VIEW_ONLY"]
        return render_template("index.html", rows=rows,
                               robots=[] if view_only else load_robots(app),
                               robot_actions={} if view_only else load_robot_actions(app),
                               journal=journal.snapshot()[:8], crumbs=[])

    # ---------------------------------------------------------- experiment --
    def trials_view_args(data: ExperimentData, records: List[trec.TrialRecord],
                         *, fixed_campaign: Optional[str] = None) -> Dict[str, Any]:
        """The filter state and the filtered rows of a trials table, from the query string.

        The filters are `trials records`' own flags (--campaign, --status,
        --where), applied by its own function, and the page prints the
        equivalent command so a filtered view can be reproduced in a terminal.
        """
        campaign = fixed_campaign or request.args.get("campaign") or None
        status = request.args.get("status") or None
        where = parse_where_args(request.args.getlist("cell"))
        shown = trec.filter_records(records, campaign=campaign, status=status, where=where)
        # The cell column shows what differs between the rows SHOWN, so a campaign
        # filter on an experiment with 35 factors leaves only that campaign's axes.
        varying, shared = data.varying_factors(shown or records)
        cmd = [f"trials records {data.experiment}"]
        if campaign:
            cmd.append(f"--campaign {campaign}")
        if status:
            cmd.append(f"--status {status}")
        cmd += [f"--where {k}={v}" for k, v in sorted(where.items())]
        if not fixed_campaign and records is data.campaign_records:
            cmd.append("--no-scratch")
        return {"rows": shown, "all_count": len(records), "f_campaign": campaign or "",
                "f_status": status or "", "f_cell": where_string(where), "where": where,
                "varying": varying, "shared": shared, "levels": data.factor_levels(records),
                # Counted over the rows the campaign and cell filters keep, so the
                # status badges answer "of these, how many are usable".
                "counts": data.status_counts(trec.filter_records(records, campaign=campaign,
                                                                 where=where)),
                "classes": data.class_summary(trec.filter_records(records, campaign=campaign,
                                                                  where=where)),
                "cli": " ".join(cmd)}

    @app.route("/e/<experiment>")
    @requires_auth
    def experiment_view(experiment: str):
        if not known_experiment(experiment):
            return error(f"No experiment {experiment!r} under {root()}", 404)
        data = cache.get(experiment)
        campaigns = []
        for name, campaign in data.campaigns.items():
            plan = data.plan(name)
            flown = [r for r in data.campaign_records if r.campaign == name]
            # Last flown: the ledger names the campaign only since 2026-10-01, so an
            # older campaign falls back to the newest trial its plan counts.
            counted = [r for r in data.campaign_records
                       if r.campaign == name or name in r.counts_for]
            campaigns.append({"name": name, "describe": campaign.describe, "plan": plan,
                              "blocked": sum(1 for r in flown if r.status == "blocked"),
                              "last": counted[0].started if counted else ""})
        for name, why in data.campaign_errors.items():
            campaigns.append({"name": name, "error": why})
        table = trials_view_args(data, data.campaign_records)
        return render_template(
            "experiment.html", experiment=experiment, data=data, campaigns=campaigns,
            table=table, scratch=data.scratch_records,
            crumbs=[(experiment, None)], tab="trials")

    @app.route("/e/<experiment>/c/<name>")
    @requires_auth
    def campaign_view(experiment: str, name: str):
        path = campaign_file(experiment, name)
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment)), (name, None)]
        if path is None or not path.is_file():
            return error(f"No campaign {name!r} in {experiment}", 404, crumbs)
        include_unverified = request.args.get("include_unverified") == "1"
        try:
            campaign = cs.load_campaign(path, root())
        except cs.SpecError as exc:
            return error(str(exc), 400, crumbs, title=f"Campaign {name} does not load. "
                         f"`trials plan` refuses it for the same reason:")
        data = cache.get(experiment)
        plan = cp.build_plan(campaign, data.trials, include_unverified)
        warnings = cs.validate_against_metadata_contract(campaign, root())
        checklist, checklist_vars = load_preflight_checklist(root(), experiment)
        try:
            auto_seq = automatic_sequence(tr.RunnerConfig.load(experiment, root()),
                                          campaign_setup=campaign.setup_steps,
                                          gated=bool(ui_gate_source(root(), experiment, campaign)))
            runner_error = ""
        except tr.RunnerError as exc:
            auto_seq, runner_error = [], str(exc)
        gates = trec.campaign_gate_outcomes(root(), experiment, name)
        records = trec.filter_records(data.campaign_records, campaign=name)
        table = trials_view_args(data, records, fixed_campaign=name)
        unverified = sum(c.unverified for c in plan.cells)
        # The grid shows the factors that differ between its cells; the ones every
        # cell shares are stated once above it, so the columns are the experiment.
        all_names = sorted(plan.cells[0].values) if plan.cells else []
        factor_names = [f for f in all_names
                        if len({str(c.values.get(f)) for c in plan.cells}) > 1] or all_names
        fixed_values = {f: plan.cells[0].values[f] for f in all_names if f not in factor_names}
        fixed_nd = {f for f in fixed_values if f in plan.cells[0].non_defaults}
        # Per cell, the attempts the T-0 gate refused (attempt ledger): a cell the
        # gate blocks every time by design reads differently from one nobody flew.
        refused = [r for r in data.campaign_records if r.campaign == name and r.status == "blocked"]
        cell_blocked = [sum(1 for r in refused
                            if all(r.values.get(k) == str(v) for k, v in cell.values.items()
                                   if k in r.values))
                        for cell in plan.cells]
        return render_template(
            "campaign.html", experiment=experiment, name=name, campaign=campaign, plan=plan,
            warnings=warnings, checklist=checklist, checklist_vars=checklist_vars,
            operator_steps=annotate_steps(campaign.operator_steps),
            live_steps=annotate_steps(campaign.operator_live_steps),
            auto_seq=auto_seq, runner_error=runner_error, gates=gates, table=table,
            data=data, unverified=unverified, factor_names=factor_names,
            fixed_values=fixed_values, fixed_nd=fixed_nd, cell_blocked=cell_blocked,
            include_unverified=include_unverified,
            has_gate=(root() / experiment / "scripts" / "preflight.sh").is_file(),
            crumbs=crumbs)

    @app.route("/e/<experiment>/t/<name>")
    @requires_auth
    def trial_view(experiment: str, name: str):
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment))]
        if not known_experiment(experiment):
            return error(f"No experiment {experiment!r} under {root()}", 404, crumbs)
        data = cache.get(experiment)
        record = data.record(name)
        if record is None:
            return error(f"No trial {name!r} in {experiment}. It may have been moved or "
                         f"deleted; the experiment page lists every trial that exists.",
                         404, crumbs)
        if record.campaign and record.campaign in data.campaigns:
            crumbs.append((record.campaign, url_for("campaign_view", experiment=experiment,
                                                    name=record.campaign)))
        crumbs.append((name, None))
        log_tail = ""
        if record.run_log is not None:
            try:
                lines = record.run_log.read_text(encoding="utf-8", errors="replace").splitlines()
                log_tail = "\n".join(lines[-60:])
            except OSError:
                log_tail = ""
        on_disk = [d for d in (record.bag_dir, record.slim_dir) if d is not None]
        excludable = (record.kind == "campaign" and bool(on_disk)
                      and all(d.parent.name in td.included_dirs(root()) for d in on_disk))
        restorable = (record.kind == "campaign" and bool(on_disk)
                      and all(d.parent.name in td.excluded_dirs(root()) for d in on_disk))
        return render_template(
            "trial.html", experiment=experiment, r=record, data=data,
            verify=trec.verify_report(record.run_log), log_tail=log_tail,
            size_full=dir_mb(record.bag_dir), size_slim=dir_mb(record.slim_dir),
            excludable=excludable, restorable=restorable, categories=td.CATEGORIES,
            describe_correction=td.describe_correction, crumbs=crumbs)

    @app.route("/e/<experiment>/analyses")
    @requires_auth
    def analyses_view(experiment: str):
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment)), ("analyses", None)]
        if not known_experiment(experiment):
            return error(f"No experiment {experiment!r} under {root()}", 404, crumbs)
        try:
            analyses = ar.load_analyses(experiment, root())
            analyses_error = ""
        except ar.AnalysisError as exc:
            analyses, analyses_error = {}, str(exc)
        data = cache.get(experiment)
        folders = trec.result_folders(root(), experiment)
        # The cohort preview: `trials analyze <exp> <name> --where ...` WITHOUT
        # --execute, through the same two functions, so the bags and commands
        # shown are the ones the command line would use.
        preview = None
        chosen = request.args.get("analysis", "")
        where = parse_where_args(request.args.getlist("cell"))
        include_unverified = request.args.get("include_unverified") == "1"
        if chosen:
            if chosen not in analyses:
                preview = {"error": f"no analysis named {chosen!r} in {experiment}"}
            else:
                bags = ar.select_bags(experiment, root(), where=where,
                                      include_unverified=include_unverified)
                cmd = f"trials analyze {experiment} {chosen}" + "".join(
                    f" --where {k}={v}" for k, v in sorted(where.items())) + (
                    " --include-unverified" if include_unverified else "")
                try:
                    result = ar.run_analysis(analyses[chosen], bags, experiment, root(),
                                             where=where, dry_run=True)
                    preview = {"bags": [t.bag_dir.name for t in bags],
                               "commands": [shlex.join(c) for c in result.commands],
                               "results_dir": result.results_dir, "cli": cmd}
                except ar.AnalysisError as exc:
                    preview = {"error": str(exc), "cli": cmd}
        return render_template(
            "analyses.html", experiment=experiment, analyses=analyses,
            analyses_error=analyses_error, folders=folders, data=data, preview=preview,
            chosen=chosen, where=where, include_unverified=include_unverified,
            levels=data.factor_levels(), crumbs=crumbs, tab="analyses")

    @app.route("/files/<experiment>/")
    @app.route("/files/<experiment>/<path:rel>")
    @requires_auth
    def files(experiment: str, rel: str = ""):
        """Read-only view of an experiment's results, run logs, declarations and bag sidecars.

        Resolved, then checked to stay inside one of the file roots of this
        experiment, so ``..`` cannot leave it. Recordings are refused here: the
        slim ones are served by /bag, the heavy ones never, because a UI on a
        trial host shares the rig's network with the flights. In ``--view-only``
        mode (the trial archive on a server, 2026-10-22) a heavy recording is a
        password-protected download.
        """
        if not known_experiment(experiment):
            return error(f"No experiment {experiment!r}", 404)
        base = (root() / experiment).resolve()
        target = (base / rel).resolve()
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment)), (rel or "files", None)]
        try:
            inside = target.relative_to(base)
        except ValueError:
            return error("outside the experiment", 403, crumbs)
        served_roots = file_roots(root())
        if not inside.parts or inside.parts[0] not in served_roots:
            if inside.parts:
                return error(f"{inside.parts[0]}/ is not served (only {', '.join(served_roots)})",
                             403, crumbs)
        if target.is_dir():
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            listing = [{"name": p.name + ("/" if p.is_dir() else ""),
                        "rel": str(p.relative_to(base)) + ("/" if p.is_dir() else ""),
                        "size": (p.stat().st_size if p.is_file() else None),
                        "recording": (p.suffix in recording_suffixes(root())
                                      and not app.config["VIEW_ONLY"])}
                       for p in entries]
            if not inside.parts:
                listing = [e for e in listing if e["name"].rstrip("/") in served_roots]
            parent = str(inside.parent) if inside.parts else None
            return render_template("files.html", experiment=experiment, rel=str(inside) if inside.parts else "",
                                   listing=listing, parent=parent, crumbs=crumbs)
        if inside.parts[:2] == trec.POSTER_DIR.parts and target.suffix == ".jpg":
            # A trial card's poster frame: made on its first request and
            # cached, so the page itself never waits for ffmpeg. The video it stands
            # for must be a served file of this experiment too.
            video = trec.video_of_poster(base, target)
            served = False
            if video is not None:
                try:
                    vparts = video.resolve().relative_to(base).parts
                    served = vparts[0] in served_roots and vparts[:2] != trec.POSTER_DIR.parts
                except ValueError:
                    served = False
            made = trec.make_poster(base, video, cache_dir=app.config["CACHE_DIR"]) if served else None
            if made is None:
                return error(f"no poster frame for {rel!r}", 404, crumbs)
            resp = send_file(made, mimetype="image/jpeg", conditional=True)
            resp.headers["Cache-Control"] = "private, max-age=3600"
            return resp
        if not target.is_file():
            return error(f"no file {rel!r} in {experiment}", 404, crumbs)
        if target.suffix in recording_suffixes(root()):
            if not app.config["VIEW_ONLY"]:
                return error("recordings are not served here: open the recording from the "
                             "trial's page", 403, crumbs)
            return send_file(target, mimetype="application/octet-stream", as_attachment=True,
                             download_name=target.name, conditional=True)
        text_types = {".log": "text/plain", ".txt": "text/plain", ".md": "text/plain",
                      ".yaml": "text/plain", ".yml": "text/plain", ".jsonl": "text/plain",
                      ".csv": "text/plain", ".json": "application/json"}
        mimetype = text_types.get(target.suffix)
        resp = send_file(target, mimetype=mimetype, conditional=True)
        if mimetype and mimetype.startswith("text/"):
            resp.headers["Content-Type"] = mimetype + "; charset=utf-8"
        return resp

    # -------------------------------------------------------------- viewing --
    @app.route("/journal")
    @requires_auth
    def journal_view():
        return render_template("journal.html", journal=journal.snapshot(),
                               runs=runs.all(), crumbs=[("journal", None)])

    # Old addresses, kept so bookmarks and the docs of an earlier version still land.
    @app.route("/campaign/<experiment>/<name>")
    @requires_auth
    def legacy_campaign(experiment: str, name: str):
        return redirect(url_for("campaign_view", experiment=experiment, name=name, **request.args))

    @app.route("/analyses/<experiment>")
    @requires_auth
    def legacy_analyses(experiment: str):
        return redirect(url_for("analyses_view", experiment=experiment))

    # -------------------------------------------------------------- actions --
    @action("/run")
    @requires_auth
    def run():
        """Plan first, execute only on an explicit confirm; then show progress."""
        experiment = request.form["experiment"]
        name = request.form["campaign"]
        # Two explicit buttons (go=dry|execute); the checkbox is the arming
        # step for execute only — a dry run must never need arming, and an
        # unarmed execute must fail loudly rather than silently downgrade.
        go = request.form.get("go", "dry")
        armed = request.form.get("confirm") == "yes"
        if go == "execute" and not armed:
            return error(
                "Execute requested but the arm checkbox was not ticked. "
                "Tick “arm” next to the EXECUTE button, or use Dry run.", 400)
        confirm = go == "execute" and armed
        try:
            limit = max(1, int(request.form.get("limit", 1)))
        except ValueError:
            return error("trials must be a whole number", 400)
        include_unverified = request.form.get("include_unverified") == "1"
        skip_gate = request.form.get("skip_gate") == "yes"
        keep_stack = request.form.get("keep_stack") == "yes"
        steps = None
        if request.form.get("steps_present") == "1":
            steps = {k: request.form.get(f"step_{k}") == "1"
                     for k in ("setup", "launch", "record", "action", "livestream")}
        # The manual window holds a live stack open, so it is bounded: before
        # 2026-11-25 any float was accepted, "nan", "-5" or 1e9 seconds included.
        try:
            hold_s = float(request.form.get("hold_s") or 60)
        except ValueError:
            hold_s = math.nan
        if not math.isfinite(hold_s) or not 0 <= hold_s <= MAX_HOLD_S:
            return error(f"The manual window must be a number of seconds from 0 to {MAX_HOLD_S:g} "
                         f"(got {request.form.get('hold_s')!r}).", 400)
        # The pose pause between the T-0 gate and the recorder (b0b42729ac):
        # on by default in the FORM (a missed pose window costs a reflight),
        # off when the field is absent so scripted POSTs never park a run on
        # a confirmation they do not know exists.
        pose_pause = request.form.get("pose_pause") == "1"
        # A picked combination arrives as space-separated name=value tokens and
        # goes through the exact CLI `--set` path, so the UI stays a client.
        pin = request.form.get("pin", "").strip()
        overrides = pin.split() if pin else []

        # An early answer only, before anything is loaded. The check that counts
        # is inside runs.start, under the lock that claims the rig.
        if confirm and runs.busy():
            return error(SLOT_BUSY, 409)

        path = campaign_file(experiment, name)
        if path is None:
            return error(f"No campaign {name!r} in experiment {experiment!r} under {root()}", 404)
        try:
            campaign = cs.load_campaign(path, root(), overrides)
            # The experiment the campaign names, as the CLI does (trial_runner._main).
            experiment = campaign.experiment
            config = tr.RunnerConfig.load(experiment, root())
        except (cs.SpecError, tr.RunnerError) as exc:
            return error(str(exc), 400)

        plan = cp.build_plan(campaign, ti.load_trials(root(), experiment), include_unverified)

        # T-0 gate on executed runs: the experiment's preflight.sh when it ships one,
        # else the CLI's own gate from the campaign's checklist (tr.build_gate).
        # Before 2026-11-25 an experiment without preflight.sh ran ungated from
        # here while the CLI gated it, and those trials counted. The skip is
        # operator-explicit and travels in the run's shown name so the journal
        # records that the gate was waived, and so does a run with no gate at all.
        gate = None
        suffix = f" [{pin}]" if pin else ""
        if confirm:
            script = root() / experiment / "scripts" / "preflight.sh"
            if skip_gate:
                suffix += " ⚠GATE-SKIPPED"
            else:
                if script.is_file():
                    host_level = (plan.cells[0].values.get("execution_host")
                                  if plan.cells else None)
                    gate = ShellPreflightGate(experiment, config, host_level, app.config["REPO_ROOT"],
                                              light=keep_stack,
                                              checklist=getattr(campaign, "preflight_checklist", ""))
                else:
                    # Prints "UNGATED" itself when the campaign has no checklist.
                    gate = tr.build_gate(campaign, root(), execute=True)
                if gate is None:
                    suffix += " ⚠UNGATED"
        if keep_stack:
            suffix += " [session]"
        if steps is not None:
            off = [k for k, v in steps.items() if not v and k != "livestream"]
            if off:
                suffix += f" [no-{'/no-'.join(off)}]"
            if steps.get("livestream"):
                suffix += " [live]"

        # TODO(scratch): the CLI grew `--scratch` (d5cc1a94ee): ONE gated,
        # recorded diagnostic flight to bags_scratch/, never coverage — see
        # tr.run_scratch. Surface it here as a third `go` value next to
        # dry|execute (same arm rule as execute, plus optional label/action
        # fields), calling tr.run_scratch through the same wait_in_browser
        # confirmation surface. Same client-of-the-CLI rule: no UI-side
        # reimplementation of what scratch means. Until then the campaign
        # page shows the CLI command with a copy button.
        operator_steps = annotate_steps(campaign.operator_steps)
        live_steps = annotate_steps(campaign.operator_live_steps)
        try:
            active = runs.start(experiment=experiment, campaign_name=f"{name}{suffix}",
                                campaign_file=name, plan=plan,
                                config=config, bags_dir=root() / experiment / backends.record_dirs(root())["recordings"],
                                executed=confirm, limit=limit, journal=journal,
                                gate=gate, steps=steps, hold_s=hold_s, keep_stack=keep_stack,
                                pause_before_action=pose_pause,
                                operator_steps=operator_steps, live_steps=live_steps,
                                extra_verify=campaign.verify_steps)
        except RunSlotBusy as exc:
            return error(str(exc), 409)
        return redirect(url_for("run_view", run_id=active.run_id))

    def begin_explore(experiment: str, name: str, pin: str, armed: bool,
                      after: Optional[ActiveRun] = None):
        """Load the campaign with ``pin`` (the --set path) and start an exploration.

        Shared by the 🔭 Explore buttons and the run page's restart.
        Returns the new run, or an error response.
        """
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment)), (name, None)]
        path = campaign_file(experiment, name)
        if path is None:
            return error(f"No campaign {name!r} in experiment {experiment!r}", 404, crumbs)
        try:
            campaign = cs.load_campaign(path, root(), pin.split() if pin else [])
            base = cs.load_campaign(path, root(), [])
            config = tr.RunnerConfig.load(experiment, root())
        except (cs.SpecError, tr.RunnerError) as exc:
            return error(str(exc), 400, crumbs)
        engine = explore_engine(root())
        if engine is None:
            return error(f"The project's backend offers no Explore.", 404, crumbs)
        settings = engine.settings(config)
        if not settings.allowed:
            return error(f"Explore is refused for {experiment}: {settings.reason}.", 400, crumbs)
        if not config.simulation and not armed:
            return error("This experiment runs real robots: tick “arm” next to Explore. "
                         "Explore starts the robots' drivers, without a T-0 gate.", 400, crumbs)
        if after is None and runs.busy():
            return error("A campaign or an exploration is already running. Stop it first: "
                         "two stacks on one graph would fight over every topic.", 409, crumbs)
        spec = next(campaign.combinations())
        picker = [{"name": f, "levels": list(levels), "value": spec.values.get(f)}
                  for f, levels in base.sweep.items() if len(levels) > 1]
        label = f"{name} [explore{' ' + pin if pin else ''}]"
        try:
            return runs.start_explore(experiment=experiment, campaign=campaign, campaign_file=name,
                                      label=label, config=config, root=root(), journal=journal,
                                      operator_steps=annotate_steps(campaign.operator_steps),
                                      layouts=cache.get(experiment).layouts, picker=picker,
                                      after=after)
        except RunSlotBusy:
            return error("A campaign or an exploration is already running. Stop it first: "
                         "two stacks on one graph would fight over every topic.", 409, crumbs)

    @action("/explore")
    @requires_auth
    def explore():
        """Bring up one campaign condition with a live view that may change parameters.

        A client of explore.run_explore, like /run is of the runner: the picked
        cell goes through the same `--set` path. No gate, no trial action. On
        hardware it needs the arm checkbox, like EXECUTE, because it starts the
        robots' drivers.
        """
        experiment = request.form.get("experiment", "")
        if not known_experiment(experiment):
            return error(f"No experiment {experiment!r} under {root()}", 404)
        data = cache.get(experiment)
        name = request.form.get("campaign") or next(iter(data.campaigns), "")
        if not name:
            return error(f"{experiment} declares no campaign that loads, so there is no "
                         f"condition to explore.", 400)
        active = begin_explore(experiment, name, request.form.get("pin", "").strip(),
                               request.form.get("explore_confirm") == "yes")
        if not isinstance(active, ActiveRun):
            return active
        return redirect(url_for("run_view", run_id=active.run_id))

    @action("/run/<run_id>/explore_restart")
    @requires_auth
    def explore_restart(run_id: str):
        """Explore another condition of the same campaign from the run page.

        The form carries one ``f_<factor>`` per swept factor. A live exploration
        is stopped first (its action lands, its recording is finalized), and the
        new one waits until that stack is down. A stopped one simply starts again.
        """
        old = runs.get(run_id)
        if old is None or old.kind != "explore":
            return error(f"No exploration {run_id}", 404)
        allowed = {p["name"]: [str(v) for v in p["levels"]] for p in old.explore.get("picker", [])}
        pins = []
        for name, levels in allowed.items():
            value = request.form.get(f"f_{name}", "")
            if value not in levels:
                return error(f"{name}={value!r} is not a level of this campaign ({', '.join(levels)})", 400)
            pins.append(f"{name}={value}")
        live = old.state in ("running", "aborting")
        if not live and runs.busy():
            return error("A campaign or an exploration is already running. Stop it first.", 409)
        if live:
            runs.abort(old.run_id)
            journal.add("explore-restart", f"{old.experiment}/{old.campaign_file}: {run_id} stopped "
                                           f"for {' '.join(pins)}")
        active = begin_explore(old.experiment, old.campaign_file, " ".join(pins),
                               request.form.get("explore_confirm") == "yes",
                               after=old if live else None)
        if not isinstance(active, ActiveRun):
            return active
        return redirect(url_for("run_view", run_id=active.run_id))

    @action("/run/<run_id>/explore/<op>")
    @requires_auth
    def explore_control(run_id: str, op: str):
        """The Explore run page's buttons, answered as JSON for the page's fetch.

        The browser names an action, a parameter or a note, never a command:
        an action must be one runner.yaml's explore.actions declares, a set
        back goes to the starting value the parameter log read, and a
        recording uses the trial's own record command into bags_scratch/.
        """
        run = runs.get(run_id)
        if run is None or run.kind != "explore" or run.controls is None:
            return {"ok": False, "message": f"no exploration {run_id}"}, 404
        # An analysis needs the recording, and a proposal the change table, not the
        # stack, so both may run after Stop.
        if run.state != "running" and op not in ("analyze", "propose"):
            return {"ok": False, "message": f"the exploration is {run.state}"}, 409
        c, form = run.controls, request.form
        if op == "propose":
            ok, msg, proposal = c.propose(form.get("node", ""), form.get("parameter", ""))
            return {"ok": ok, "message": msg, "proposal": proposal}, (200 if ok else 400)
        if op == "analyze":
            ok, msg = c.analyze(form.get("recording", ""), form.get("analysis", ""))
        elif op == "action":
            ok, msg = c.start_action(form.get("name", ""), armed=form.get("arm") == "yes")
        elif op == "jog":
            ok, msg = c.jog(*(form.get(k, "0") for k in ("vx", "vy", "vz", "yaw")),
                            armed=form.get("arm") == "yes")
        elif op == "action_stop":
            ok, msg = c.stop_action()
        elif op == "set_back":
            ok, msg = c.set_back(form.get("node", ""), form.get("parameter", ""))
        elif op == "record":
            ok, msg = c.start_record(form.get("label", ""))
        elif op == "record_stop":
            ok, msg = c.stop_record()
        elif op == "note":
            ok, msg = c.note(form.get("text", ""), mark=form.get("mark") == "yes")
        else:
            return {"ok": False, "message": f"unknown control {op!r}"}, 404
        if op not in ("note", "jog"):
            journal.add(f"explore-{op.replace('_', '-')}",
                        f"{run.experiment}/{run.campaign_file}: {'OK' if ok else 'refused'}, {msg}")
        return {"ok": ok, "message": msg}, (200 if ok else 400)

    @action("/preflight/<experiment>")
    @requires_auth
    def preflight(experiment: str):
        """Run the pre-flight gate on the trial host, WITHOUT running anything else.

        Same client-of-the-CLI rule: this invokes the experiment's own
        scripts/preflight.sh through the same remote wrapper the runner uses.
        """
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment)), ("gate", None)]
        if runs.busy():
            return error(
                "A campaign is running — the gate may restart a middleware daemon and "
                "must not run mid-trial. Wait or abort first.", 409, crumbs)
        if not known_experiment(experiment):
            # Never a path segment such as "..": the script path below is built from it.
            return error(f"No experiment {experiment!r} under {root()}", 404, crumbs)
        script = root() / experiment / "scripts" / "preflight.sh"
        if not script.is_file():
            return error(f"{experiment} has no scripts/preflight.sh", 404, crumbs)
        try:
            config = tr.RunnerConfig.load(experiment, root())
        except tr.RunnerError as exc:
            return error(str(exc), 400, crumbs)
        host_level = request.form.get("host", "").strip() or None
        target = config.remote_target(host_level)
        cmd = ["bash", f"experiments/{experiment}/scripts/preflight.sh"]
        if target is not None:
            from robofacts.remote_exec_utils import wrap_command
            cmd = wrap_command(cmd, target)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=360,
                                  cwd=None if target else str(app.config["REPO_ROOT"]))
            ok, output = proc.returncode == 0, (proc.stdout + "\n" + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            ok, output = False, "the gate timed out after 360 s"
        journal.add("preflight", f"{experiment} on {host_level or 'local'}: "
                                 f"{'PASS — go' if ok else 'FAIL — no-go'}")
        back = request.form.get("back") or url_for("experiment_view", experiment=experiment)
        return render_template("preflight.html", experiment=experiment,
                               host=host_level or "local", ok=ok, output=output,
                               back=back, crumbs=crumbs)

    @action("/operator_step")
    @requires_auth
    def operator_step():
        """Run ONE operator step's declared command on the trial's host.

        The browser posts an INDEX, never a command: the only things runnable
        here are the ``run:`` fields of the campaign's own operator steps, so
        the version-controlled campaign file is the allowlist. That is the
        same rule the e-stop follows with robots.yaml, and the reason this is
        not a remote shell.

        It exists because these commands are typed while a drone is hovering —
        the handover is one of them — and a mistyped host, container or
        service name at that moment is worth more than the convenience.
        """
        experiment = request.form["experiment"]
        name = request.form["campaign"]
        kind = request.form.get("kind", "live")
        try:
            index = int(request.form["index"])
        except (KeyError, ValueError):
            return Response("bad step index", status=400, mimetype="text/plain")
        # Only a campaign file of a known experiment is the allowlist (see campaign_file).
        path = campaign_file(experiment, name)
        if path is None:
            return Response(f"no campaign {name!r} in experiment {experiment!r}", status=404,
                            mimetype="text/plain")
        try:
            campaign = cs.load_campaign(path, root())
            config = tr.RunnerConfig.load(experiment, root())
        except (cs.SpecError, tr.RunnerError) as exc:
            return Response(str(exc), status=400, mimetype="text/plain")

        steps = (campaign.operator_live_steps if kind == "live" else campaign.operator_steps)
        if not 0 <= index < len(steps) or not steps[index].get("run"):
            return Response("no runnable command at that step", status=400,
                            mimetype="text/plain")

        # Fixed factor values only: a step's command must not depend on which
        # combination is being flown, or the button would mean different things
        # on different rows.
        command = cs.substitute_known(steps[index]["run"], campaign.fixed)
        host_level = campaign.fixed.get("execution_host")
        target = config.remote_target(host_level)
        argv = ["bash", "-lc", command]
        if target is not None:
            from robofacts.remote_exec_utils import wrap_command
            argv = wrap_command(argv, target)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                                  cwd=None if target else str(app.config["REPO_ROOT"]))
            ok = proc.returncode == 0
            output = (proc.stdout + "\n" + proc.stderr).strip() or "(no output)"
        except subprocess.TimeoutExpired:
            ok, output = False, "timed out after 30 s"
        journal.add("operator-step",
                    f"{experiment}/{name} [{kind} #{index + 1}]: "
                    f"{'OK' if ok else 'FAILED'} — {command[:70]}")
        return Response(f"$ {command}\n\n{'✅' if ok else '❌'} {output}",
                        status=200, mimetype="text/plain")

    @app.route("/run/<run_id>")
    @requires_auth
    def run_view(run_id: str):
        run = runs.get(run_id)
        if run is None:
            return error(f"No run {run_id}. Runs live in this UI's memory only, so a "
                         f"restart forgets them; the trials themselves are on disk.", 404,
                         [("journal", url_for("journal_view"))])
        crumbs = [(run.experiment, url_for("experiment_view", experiment=run.experiment)),
                  (run.campaign_file, url_for("campaign_view", experiment=run.experiment,
                                              name=run.campaign_file)),
                  ("run " + run.run_id, None)]
        return render_template("explore.html" if run.kind == "explore" else "run.html",
                               run=run, crumbs=crumbs)

    @app.route("/api/run/<run_id>.json")
    @requires_auth
    def api_run(run_id: str):
        run = runs.get(run_id)
        if run is None:
            return {"error": "unknown run"}, 404
        return run.to_dict()

    @action("/run/<run_id>/abort")
    @requires_auth
    def abort_run(run_id: str):
        ok = runs.abort(run_id)
        journal.add("abort", f"{run_id}: {'requested' if ok else 'not running'}")
        return redirect(url_for("run_view", run_id=run_id))

    @action("/run/<run_id>/proceed")
    @requires_auth
    def proceed(run_id: str):
        """The browser's Enter key for the runner's operator waits.

        Releases the worker parked on this run's confirmation surface — the
        operator-steps wait before anything launches (8f1213cc6e) or the pose
        pause between the gate and the recorder (b0b42729ac). Idempotent:
        posting when nothing is waiting changes nothing.
        """
        run = runs.get(run_id)
        if run is None:
            return error(f"No run {run_id}", 404)
        if run.awaiting_kind:
            journal.add("operator-confirm",
                        f"{run_id}: {run.awaiting_kind} confirmed — {run.awaiting_what}")
            run._go.set()
        return redirect(url_for("run_view", run_id=run_id))

    @action("/stop")
    @requires_auth
    def stop():
        """Soft-stop or e-stop, via scripts/estop.sh — a narrow allowlist."""
        action = request.form.get("action", "estop")
        robot = request.form.get("robot", "")
        # The robots.yaml this interface lists in its stop bar, named explicitly: estop.sh
        # otherwise looks for one from the server's working directory, and a UI started
        # elsewhere with --experiments-root listed one project's robots and failed to stop
        # them ("Robot config not found"), or stopped another project's.
        cmd = [str(tools.path("estop.sh")), "--config", str(robots_config(app))]
        if action == "soft":
            cmd.append("--soft")
        if robot:
            cmd += ["--robot", robot]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        ok = completed.returncode == 0
        # A stop ends the EXPERIMENT too, not just the aircraft. Without this
        # the drone lands while the trial keeps running out its hover, the
        # recorder keeps writing, and the campaign moves on to the next trial —
        # so the operator has to stop the same thing twice.
        extra = ""
        run = runs.current()
        if run is not None and run.state == "running":
            runs.abort(run.run_id)
            from robofacts.remote_exec_utils import terminate_current_foreground
            if terminate_current_foreground():
                extra = ("\n\nThe running trial was ended too: its action was stopped, so the "
                         "runner is now closing the bag first and then the stack, in that order. "
                         "No further trials will start.")
            else:
                extra = ("\n\nThe campaign was told to stop after this trial; no further trials "
                         "will start.")
        journal.add("stop", f"{action} {robot or 'ALL'}: {'OK' if ok else 'FAILED'}"
                            f"{' + trial ended' if extra else ''}",
                    (completed.stdout or "") + (completed.stderr or ""))
        return render_template("stop.html", action=action, robot=robot, ok=ok,
                               output=(completed.stdout or "") + (completed.stderr or "") + extra,
                               crumbs=[("stop", None)])

    @action("/robot_action")
    @requires_auth
    def robot_action():
        """One declared operator action (posture/status) via scripts/robot_action.sh.

        Validated against robots.yaml `actions:` HERE as well as in the script:
        the allowlist is the contract, and an undeclared (robot, action) pair
        is refused before any process is spawned.
        """
        robot = request.form.get("robot", "")
        action = request.form.get("action", "")
        declared = load_robot_actions(app)
        if robot not in declared or action not in {a["name"] for a in declared[robot]}:
            journal.add("robot_action", f"REFUSED undeclared {robot}.{action}")
            return error(f"{robot!r}.{action!r} is not a declared action", 400)
        # The same robots.yaml the allowlist above was read from (see /stop).
        cmd = [str(tools.path("robot_action.sh")), "--config", str(robots_config(app)), robot, action]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        ok = completed.returncode == 0
        journal.add("robot_action", f"{robot}.{action}: {'OK' if ok else 'FAILED'}",
                    (completed.stdout or "") + (completed.stderr or ""))
        return render_template("robot_action.html", robot=robot, action=action, ok=ok,
                               output=(completed.stdout or "") + (completed.stderr or ""),
                               lease_owners=lease_owners(completed.stdout or ""),
                               crumbs=[("robot action", None)])

    @action("/exclude/<experiment>/<name>")
    @requires_auth
    def exclude_trial(experiment: str, name: str):
        """`trials exclude`: move a trial and its slim copy into `bags_issues*`, with its reason.

        Exclusion, not deletion: the index excludes `bags_issues*` from coverage
        while the recording stays inspectable, and `exclusion.json` beside it
        says why, which every list then shows. The trial is resolved by
        trial_decisions.locate, which matches existing directory names and never
        joins user input onto a path.
        """
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment)), (name, None)]
        if not known_experiment(experiment):
            return error(f"No experiment {experiment!r} under {root()}", 404, crumbs)
        try:
            record = td.exclude(root(), experiment, name, reason=request.form.get("reason", ""),
                                category=request.form.get("category", ""),
                                by=(request.form.get("by") or "").strip() or None)
        except td.DecisionError as exc:
            return error(f"Not excluded: {exc}", 409, crumbs)
        for line in record["moved"] or [f"{name}: reason added in bags_issues"]:
            journal.add("exclude", line)
        journal.add("exclude", f"{name}: {td.describe_exclusion(record)}")
        cache.clear()
        back = request.form.get("back") or url_for("experiment_view", experiment=experiment)
        return redirect(back)

    @action("/restore/<experiment>/<name>")
    @requires_auth
    def restore_trial(experiment: str, name: str):
        """`trials restore`: move an excluded trial back; its old reason stays as history."""
        crumbs = [(experiment, url_for("experiment_view", experiment=experiment)), (name, None)]
        if not known_experiment(experiment):
            return error(f"No experiment {experiment!r} under {root()}", 404, crumbs)
        try:
            result = td.restore(root(), experiment, name, reason=request.form.get("reason", ""),
                                by=(request.form.get("by") or "").strip() or None)
        except td.DecisionError as exc:
            return error(f"Not restored: {exc}", 409, crumbs)
        for line in result["moved"]:
            journal.add("restore", line)
        cache.clear()
        back = request.form.get("back") or url_for("experiment_view", experiment=experiment)
        return redirect(back)

    # ------------------------------------------------------------------ api --
    @app.route("/api/plan/<experiment>/<name>.json")
    @requires_auth
    def api_plan(experiment: str, name: str):
        path = campaign_file(experiment, name)
        if path is None:
            return {"error": f"no campaign {name!r} in experiment {experiment!r}"}, 404
        try:
            campaign = cs.load_campaign(path, root())
        except cs.SpecError as exc:
            return {"error": str(exc)}, 400
        plan = cp.build_plan(campaign, ti.load_trials(root(), experiment),
                             request.args.get("include_unverified") == "1")
        return plan.to_dict()

    @app.route("/api/records/<experiment>.json")
    @requires_auth
    def api_records(experiment: str):
        """`trials records <exp> --json`, with the same filters as query parameters."""
        if not known_experiment(experiment):
            return {"error": f"no experiment {experiment!r}"}, 404
        data = cache.get(experiment)
        rows = trec.filter_records(data.records, campaign=request.args.get("campaign") or None,
                                   status=request.args.get("status") or None,
                                   where=parse_where_args(request.args.getlist("where")))
        return {"experiment": experiment, "records": [r.to_dict(root()) for r in rows]}

    @app.route("/healthz")
    def healthz():
        return {"ok": True}

    if hasattr(extension, "register"):
        # The backend's routes, with what they share with this app.
        extension.register(SimpleNamespace(
            app=app, requires_auth=requires_auth, action=action, root=root, base_url=base_url,
            known_experiment=known_experiment, error=error, cache=cache, runs=runs,
            journal=journal))
    return app


def robots_config(app) -> Path:
    """The robots.yaml of this interface's experiments root: the stop bar, the robot actions,
    the clean slate and the scripts the stop and action buttons run all read this one file."""
    return app.config["EXPERIMENTS_ROOT"] / "common" / "config" / "robots.yaml"


def robot_hosts(app) -> List[Dict[str, str]]:
    """Distinct {host, container, shell} the robots are reachable on.

    Derived from robots.yaml — the same allowlist the e-stop uses — so the
    kill-all button can only ever reach machines this project already drives.
    """
    import yaml
    path = robots_config(app)
    if not path.is_file():
        return []
    from robofacts import host_registry
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # Each robot's declared host (host_level through hosts.yaml, 2026-11-16), not a running
    # trial's: the clean slate is for the machines the robots normally live on.
    targets = host_registry.robot_targets(path, raw.get("robots") or {})
    seen, out = set(), []
    for target in targets.values():
        host = "" if target.host == "local" else target.host
        key = (host, target.container)
        if key in seen:
            continue
        seen.add(key)
        out.append({"host": host, "container": target.container, "shell": target.shell})
    return out


#: The archive's merge step (scripts/archive/merge_view.py in the PhD repo) writes
#: this file at the top of the merged experiments root: when it ran, how each
#: host's last sync went, and how many files two hosts disagreed on.
ARCHIVE_STATUS_FILE = ".archive_status.json"


def archive_status(experiments_root: Path) -> Optional[Dict[str, Any]]:
    """The archive's status for the view-only banner, or None when there is none."""
    import json
    try:
        data = json.loads((Path(experiments_root) / ARCHIVE_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_robots(app) -> List[str]:
    import yaml
    path = robots_config(app)
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return sorted((raw.get("robots") or {}).keys())


def lease_owners(output: str) -> Optional[List[Dict[str, str]]]:
    """The lease table of an action's output: the first enabled adapter whose
    ``LEASE_TABLE`` reads one from it, else None."""
    from robofacts import adapters
    try:
        names = adapters.enabled()
    except adapters.AdapterError:
        return None
    for name in names:
        reader = getattr(adapters.module(name), "LEASE_TABLE", None)
        table = reader(output) if reader is not None else None
        if table:
            return table
    return None


def load_robot_actions(app) -> Dict[str, List[Dict[str, str]]]:
    """The declared operator actions per robot (robots.yaml `actions:`).

    The same narrow-allowlist contract as the stops (2026-08-10): the UI
    renders and invokes exactly these entries — posture and status commands —
    and nothing else. scripts/robot_action.sh re-validates on its side too.
    """
    import yaml
    path = robots_config(app)
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: Dict[str, List[Dict[str, str]]] = {}
    for name, spec in sorted((raw.get("robots") or {}).items()):
        actions = (spec or {}).get("actions") or {}
        if actions:
            out[name] = [{"name": a, "description": e.get("description", "")}
                         for a, e in sorted(actions.items())]
    return out


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    from robofacts.ui.arguments import build_parser  # the parser lives apart, so the help needs no Flask
    args = build_parser().parse_args(argv)
    roots.resolve_root_arguments(args)

    view_only = args.view_only or os.environ.get("TRIALS_UI_VIEW_ONLY", "") not in ("", "0")
    password = os.environ.get("EXPERIMENTS_UI_PASSWORD")
    if not password and not args.no_auth:
        print("ERROR: set EXPERIMENTS_UI_PASSWORD, or pass --no-auth.\n"
              + ("A view-only UI still shows every trial, so it asks for a password too."
                 if view_only else
                 "This UI can start a flight; it will not run unauthenticated by accident."),
              file=sys.stderr)
        return 2
    if args.host != "127.0.0.1" and not password:
        print("ERROR: refusing to bind a non-localhost address without a password.",
              file=sys.stderr)
        return 2
    if args.host != "127.0.0.1" and not view_only:
        print(f"WARNING: binding {args.host} exposes this on the network. "
              f"The SSH-tunnel setup (127.0.0.1) is safer.", file=sys.stderr)

    # Probe the port BEFORE handing it to Flask: its stock in-use message names
    # no commands, and it swallows the OSError internally (SystemExit), so this
    # is the only reliable place to be helpful. The owner is almost always an
    # earlier instance of this UI surviving a closed terminal or a dropped ssh.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((args.host, args.port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"""\
ERROR: port {args.port} is already in use — almost certainly an earlier instance
of this UI still running (it survives a closed terminal or a dropped ssh).

  See who owns it:      ss -ltnp | grep :{args.port}
  If it is this UI:     pkill -f 'trials ui'
                        …then re-run the exact command you just ran.
  Or use another port:  add  --port {args.port + 1}
                        (if you reach the UI over an ssh tunnel, forward the
                         same port:  ssh -N -L {args.port + 1}:localhost:{args.port + 1} <host>)""",
              file=sys.stderr)
        return 1
    finally:
        probe.close()

    user = os.environ.get("EXPERIMENTS_UI_USER") or None
    # The whole server works on this one experiments root: a setting, the backend and the
    # commands it starts come from this project, not from the project around the working
    # directory (second review of 2026-11-25; `trials run` does the same, see
    # roots.exported_experiments_root).
    os.environ[roots.ENV_EXPERIMENTS_ROOT] = str(Path(args.experiments_root).resolve())
    app = create_app(args.experiments_root, password, user=user, view_only=view_only,
                     cache_dir=args.cache_dir)
    if view_only:
        print("view-only: no route that runs, stops or moves anything is registered",
              file=sys.stderr)
    try:
        app.run(host=args.host, port=args.port, threaded=True)
    except KeyboardInterrupt:
        pass
    # A plain return can leave the interpreter waiting on lingering threads
    # with the socket half-held — the "port blocked after Ctrl-C" report.
    # Exit hard, but say what that means if a run is in flight.
    runs_mgr = app.config.get("RUNS")
    if runs_mgr is not None and runs_mgr.busy():
        # The trial runs in a worker thread that gets no signal, and its action leads its own
        # process group, so the Ctrl-C reached neither. End the trial the way the CLI's
        # Ctrl-C ends it: the action lands, then the worker's teardown stops the recorder and
        # the stack, and no further trial starts. Before the second review of 2026-11-25 the
        # server exited here with the action still flying.
        run = runs_mgr.current()
        print("\nCtrl-C with a campaign run ACTIVE: no further trial starts, the action in "
              "flight lands first, then the recorder and the stack stop. Ctrl-C again exits "
              "at once.", file=sys.stderr)
        try:
            if run is not None:
                runs_mgr.abort(run.run_id)
            from robofacts.remote_exec_utils import LAND_WAIT_S, end_current_action
            end_current_action(KeyboardInterrupt())
            deadline = time.monotonic() + LAND_WAIT_S + 120
            while runs_mgr.busy() and time.monotonic() < deadline:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
    if runs_mgr is not None and runs_mgr.busy():
        print("\nWARNING: a campaign run was ACTIVE. Its remote processes may have "
              "been orphaned or cut mid-trial by this exit.\n"
              "  - if a drone is airborne: scripts/estop.sh\n"
              "  - before the next trial:  the runner's kill_stale_rig step will "
              "clean the rig, or run ./scripts/kill_ros.sh yourself", file=sys.stderr)
        os._exit(1)
    print("\nUI shut down cleanly, port released.", file=sys.stderr)
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())

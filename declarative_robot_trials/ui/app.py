#!/usr/bin/env python3
"""Experiments control surface — the web UI (#96 Phase 5).

Pick a combination, see how many bags it already has, run it, watch it, and open
the result — from the laptop browser, while the trial runs wherever it runs.

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

    ros2 launch <your_package> foxglove_bridge.launch.py   # optional, live view
    trials ui                       # this UI
    ssh -N -L 8080:localhost:8080 -L 8765:localhost:8765 <host> # from the laptop
"""

from __future__ import annotations

import argparse
import errno
import os
import secrets
import shlex
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, Response, redirect, render_template, request, send_file, url_for


from .. import analysis_runner as ar
from .. import campaign_live_steps as cls_mod
from .. import code_version as cv
from .. import campaign_plan as cp
from .. import campaign_spec as cs
from .. import trial_index as ti
from .. import trial_runner as tr
from .. import roots, tools


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
    _go: threading.Event = field(default_factory=threading.Event)
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
            if head.startswith(("[runner]", "[setup]", "[aux]", "PREFLIGHT", "PASS", "FAIL",
                                "SKIP", "✓")) \
                    or "CHECK(S)" in line or "MACHINE CHECKS" in line:
                src = "runner"
            elif head.startswith("[stack]"):
                src = "stack"
            elif head.startswith("[recorder]"):
                src = "recorder"
            elif head.startswith(("[markerless_node]", "[livestream]")):
                src = "nodes"
            elif head.startswith(("[launch_stack]", "[aux:", "[record]")):
                src = "runner"          # teardown bookkeeping
            else:
                src = "other"
            self.log.append({"src": src, "lvl": lvl, "text": line})
        # Deeper than the screen but bounded: the stack is chatty and the point
        # is to still have the first error after minutes of node output.
        del self.log[:-2000]

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "experiment": self.experiment,
                "campaign": self.campaign, "state": self.state,
                "executed": self.executed, "wanted": self.wanted,
                "done": len(self.results), "results": list(self.results),
                "awaiting_kind": self.awaiting_kind,
                "awaiting_what": self.awaiting_what,
                # The CLI's closing count, same function, so the two surfaces
                # can never tell a different story about how the batch went.
                "tally": tr.tally(r["status"] for r in self.results),
                "log": list(self.log), "error": self.error}


class RunManager:
    """Runs a campaign off the request thread so the page can show progress.

    Only ONE run at a time, deliberately: two campaigns sharing one rig would
    fight over the radio, the recorder and the airspace. The guard is here
    rather than in the runner because it is a property of the lab, not of the
    code.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: Dict[str, ActiveRun] = {}
        self._current: Optional[str] = None

    def current(self) -> Optional[ActiveRun]:
        with self._lock:
            return self._runs.get(self._current) if self._current else None

    def get(self, run_id: str) -> Optional[ActiveRun]:
        with self._lock:
            return self._runs.get(run_id)

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
            self._runs[run_id] = run
            self._current = run_id

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
            run at a time and nothing else here writes to stdout; if either
            stops being true, pass an explicit log callback into run_plan
            instead.
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
                "rig, then confirm to record and fly")

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
        return {"gate": "preflight.sh", "result": "pass",
                "at": datetime.now(timezone.utc).isoformat()}


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
        #: the ros2 daemon between trials would disturb the very thing being
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
            from .remote_exec_utils import wrap_command
            cmd = wrap_command(cmd, target)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=360,
                              cwd=None if target else str(self.repo_root))
        # Print the whole gate report so it reaches the terminal AND the run
        # log: the per-check PASS/FAIL lines are the most useful thing an
        # operator can read, and capturing them into an exception message only
        # (as this did at first) hid the passing context of a failure.
        print(proc.stdout.rstrip() or proc.stderr.rstrip(), flush=True)
        if proc.returncode != 0:
            raise RuntimeError("PRE-FLIGHT NO-GO — " + summarize_gate_failure(
                proc.stdout + "\n" + proc.stderr))
        return _GatePass()


def automatic_sequence(config, steps: Optional[Dict[str, bool]] = None) -> List[str]:
    """What EXECUTE does by itself, in order, derived from runner.yaml.

    Derived rather than written out, so it cannot drift from what the runner
    actually does — the failure this exists to prevent is an operator reading a
    stale description and assuming a step is covered when it is not.
    """
    steps = {**tr.DEFAULT_STEPS, **(steps or {})}
    seq: List[str] = []
    if steps["setup"]:
        seq += [f"{s['name']}  ({s['command'].split()[0]}…)" for s in config.setup_steps]
    if steps["launch"]:
        seq.append(f"launch {config.launch_package}/{config.launch_file}, "
                   f"wait {config.settle_s:g}s to settle")
        seq += [f"start aux: {a['name']}" for a in config.aux_launches]
    if steps["livestream"] and config.livestream:
        seq.append("start the livestream bridge")
    seq.append("run the T-0 pre-flight gate (blocks the trial if it fails)")
    if steps["record"]:
        seq.append("start the bag recorder")
    if steps["action"]:
        seq.append("fly the trajectory")
    else:
        seq.append("HOLD the stack open for the manual window (no automatic flight)")
    seq.append("stop the recorder first, then tear the stack down")
    seq += [f"verify: {v['name']}" for v in config.verify_steps]
    if steps["record"]:
        seq.append("write trial_metadata.json + sync a slim copy back")
    return seq


def load_preflight_checklist(experiments_root: Path, experiment: str):
    """The #87 checklist as (items, variables) for display — [] if none exists.

    Read directly from the JSON the gate itself uses, so the text shown can
    never drift from the checks run (the same no-drift property the #87 test
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


def foxglove_link(base_url: str, experiment: str, bag_name: str) -> str:
    """Deep link that opens the laptop's Foxglove on a bag served by this UI.

    Clicked in the operator's browser, so it reaches the laptop's Foxglove even
    though the UI is served from the rig. The URL must end in .mcap.
    """
    return (f"foxglove://open?ds=remote-file&ds.url="
            f"{base_url}/bag/{experiment}/{bag_name}.mcap")


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def create_app(experiments_root: Optional[Path] = None,
               password: Optional[str] = None,
               repo_root: Optional[Path] = None) -> Flask:
    experiments_root = roots.experiments_root(experiments_root)
    repo_root = roots.project_root(repo_root, experiments=experiments_root)
    app = Flask(__name__)
    app.config["EXPERIMENTS_ROOT"] = Path(experiments_root)
    app.config["REPO_ROOT"] = Path(repo_root)
    app.config["PASSWORD"] = password
    journal = RunJournal()
    app.config["JOURNAL"] = journal
    runs = RunManager()
    app.config["RUNS"] = runs  # main() checks this for a safe Ctrl-C message
    app.config["RUNS"] = runs

    def requires_auth(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            expected = app.config.get("PASSWORD")
            if expected:
                auth = request.authorization
                # compare_digest on both fields: a plain `!=` leaks length and
                # content through timing, and this endpoint can start a flight.
                ok = (auth is not None
                      and secrets.compare_digest(auth.password or "", expected))
                if not ok:
                    return Response(
                        "Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="experiments"'})
            return view(*args, **kwargs)
        return wrapper

    # ---------------------------------------------------------------- views --
    @app.route("/")
    @requires_auth
    def index():
        root = app.config["EXPERIMENTS_ROOT"]
        campaigns = discover_campaigns(root)
        trials = ti.load_trials(root)
        by_experiment: Dict[str, Dict[str, int]] = {}
        for trial in trials:
            counts = by_experiment.setdefault(trial.experiment, {})
            counts[trial.status] = counts.get(trial.status, 0) + 1
        return render_template("index.html", campaigns=campaigns,
                               counts=by_experiment, robots=load_robots(app),
                               robot_actions=load_robot_actions(app),
                               journal=journal.snapshot())

    @app.route("/campaign/<experiment>/<name>")
    @requires_auth
    def campaign_view(experiment: str, name: str):
        root = app.config["EXPERIMENTS_ROOT"]
        path = root / experiment / "campaigns" / f"{name}.yaml"
        if not path.is_file():
            return render_template("error.html", message=f"No campaign {name!r} in {experiment}"), 404
        include_unverified = request.args.get("include_unverified") == "1"
        try:
            campaign = cs.load_campaign(path, root)
        except cs.SpecError as exc:
            return render_template("error.html", message=str(exc)), 400
        all_trials = ti.load_trials(root, experiment)
        plan = cp.build_plan(campaign, all_trials, include_unverified)
        warnings = cs.validate_against_metadata_contract(campaign, root)
        checklist, checklist_vars = load_preflight_checklist(root, experiment)
        try:
            auto_seq = automatic_sequence(tr.RunnerConfig.load(experiment, root))
        except tr.RunnerError:
            auto_seq = []
        # The bags themselves, newest first. A coverage count answers "how
        # many"; an operator also needs "which ones, and are they any good" —
        # the page showed the former only, so a flown trial was a number with
        # no way to reach the recording behind it.
        swept = set(campaign.sweep) | set(campaign.fixed)

        def _code_cell(version):
            """{short, dirty, title} for the code-version column."""
            if not version or not version.get("commit"):
                why = (version or {}).get("error") or "recorded before code stamping"
                return {"short": "—", "dirty": False, "title": f"no code version: {why}"}
            title = cv.summary(version)
            if version.get("dirty"):
                shown = version.get("dirty_files", [])
                title += "\nuncommitted: " + ", ".join(shown)
                if version.get("dirty_file_count", 0) > len(shown):
                    title += f", +{version['dirty_file_count'] - len(shown)} more"
            return {"short": version["commit"][:8],
                    "dirty": bool(version.get("dirty")), "title": title}

        def _dir_mb(d):
            try:
                return round(sum(f.stat().st_size for f in d.iterdir() if f.is_file()) / 1e6, 1)
            except OSError:
                return None

        bags = sorted(
            ({"trial_id": t.metadata.get("trial_id", t.bag_dir.name),
              "status": t.status,
              "aborted": t.aborted,
              "notes": str(t.metadata.get("notes", "")),
              "problems": t.problems,
              "dir_name": t.bag_dir.name,
              "is_issue": t.is_issue_bag,
              "size_mb": _dir_mb(t.bag_dir),
              # Provenance, shown short with the full story in the tooltip: an
              # operator comparing two odd trials wants "same commit or not?"
              # at a glance, and the dirty marker warns that the hash alone
              # does not describe what ran.
              "code": _code_cell(t.metadata.get("code_version")),
              # NOT "values": Jinja resolves `b.values` to dict.values().
              "factors": {k: v for k, v in sorted(t.metadata.items())
                         if k in swept and not isinstance(v, (dict, list))}}
             for t in all_trials),
            key=lambda b: b["trial_id"], reverse=True)
        return render_template("campaign.html", experiment=experiment, name=name,
                               checklist=checklist, checklist_vars=checklist_vars,
                               campaign=campaign, plan=plan, warnings=warnings,
                               operator_steps=annotate_steps(campaign.operator_steps),
                               live_steps=annotate_steps(campaign.operator_live_steps),
                               bags=bags, base_url=request.host_url.rstrip("/"),
                               auto_seq=auto_seq,
                               include_unverified=include_unverified,
                               journal=journal.snapshot())

    @app.route("/analyses/<experiment>")
    @requires_auth
    def analyses_view(experiment: str):
        root = app.config["EXPERIMENTS_ROOT"]
        try:
            analyses = ar.load_analyses(experiment, root)
        except ar.AnalysisError as exc:
            return render_template("error.html", message=str(exc)), 404
        results = sorted((root / experiment / "results").glob("*/analysis_metadata.json"),
                         reverse=True)[:15]
        return render_template("analyses.html", experiment=experiment,
                               analyses=analyses, results=results, journal=journal.snapshot())

    # -------------------------------------------------------------- actions --
    @app.route("/run", methods=["POST"])
    @requires_auth
    def run():
        """Plan first, execute only on an explicit confirm; then show progress."""
        root = app.config["EXPERIMENTS_ROOT"]
        experiment = request.form["experiment"]
        name = request.form["campaign"]
        # Two explicit buttons (go=dry|execute); the checkbox is the arming
        # step for execute only — a dry run must never need arming, and an
        # unarmed execute must fail loudly rather than silently downgrade.
        go = request.form.get("go", "dry")
        armed = request.form.get("confirm") == "yes"
        if go == "execute" and not armed:
            return render_template("error.html", message=(
                "Execute requested but the arm checkbox was not ticked. "
                "Tick “arm” next to the EXECUTE button, or use Dry run.")), 400
        confirm = go == "execute" and armed
        limit = int(request.form.get("limit", 1))
        include_unverified = request.form.get("include_unverified") == "1"
        skip_gate = request.form.get("skip_gate") == "yes"
        keep_stack = request.form.get("keep_stack") == "yes"
        steps = None
        if request.form.get("steps_present") == "1":
            steps = {k: request.form.get(f"step_{k}") == "1"
                     for k in ("setup", "launch", "record", "action", "livestream")}
        try:
            hold_s = float(request.form.get("hold_s") or 60)
        except ValueError:
            hold_s = 60.0
        # The pose pause between the T-0 gate and the recorder (b0b42729ac):
        # on by default in the FORM (a missed pose window costs a reflight),
        # off when the field is absent so scripted POSTs never park a run on
        # a confirmation they do not know exists.
        pose_pause = request.form.get("pose_pause") == "1"
        # A picked combination arrives as space-separated name=value tokens and
        # goes through the exact CLI `--set` path, so the UI stays a client.
        pin = request.form.get("pin", "").strip()
        overrides = pin.split() if pin else []

        if confirm and runs.busy():
            return render_template("error.html", message=(
                "A campaign is already running. Two campaigns sharing one rig would "
                "fight over the radio, the recorder and the airspace — wait for it, "
                "or abort it first.")), 409

        try:
            campaign = cs.load_campaign(root / experiment / "campaigns" / f"{name}.yaml", root,
                                        overrides)
            config = tr.RunnerConfig.load(experiment, root)
        except (cs.SpecError, tr.RunnerError) as exc:
            return render_template("error.html", message=str(exc)), 400

        plan = cp.build_plan(campaign, ti.load_trials(root, experiment), include_unverified)

        # T-0 gate on executed runs, when the experiment ships a preflight.sh.
        # The skip is operator-explicit and travels in the run's shown name so
        # the journal records that the gate was waived, not merely absent.
        gate = None
        suffix = f" [{pin}]" if pin else ""
        if confirm:
            script = root / experiment / "scripts" / "preflight.sh"
            if skip_gate:
                suffix += " ⚠GATE-SKIPPED"
            elif script.is_file():
                host_level = (plan.cells[0].values.get("execution_host")
                              if plan.cells else None)
                gate = ShellPreflightGate(experiment, config, host_level, app.config["REPO_ROOT"],
                                          light=keep_stack,
                                          checklist=getattr(campaign, "preflight_checklist", ""))
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
        # reimplementation of what scratch means. Not wired yet on purpose —
        # the CLI flag should get a rig day first.
        operator_steps = annotate_steps(campaign.operator_steps)
        live_steps = annotate_steps(campaign.operator_live_steps)
        active = runs.start(experiment=experiment, campaign_name=f"{name}{suffix}",
                            campaign_file=name, plan=plan,
                            config=config, bags_dir=root / experiment / "bags",
                            executed=confirm, limit=limit, journal=journal,
                            gate=gate, steps=steps, hold_s=hold_s, keep_stack=keep_stack,
                            pause_before_action=pose_pause,
                            operator_steps=operator_steps, live_steps=live_steps,
                            extra_verify=campaign.verify_steps)
        return redirect(url_for("run_view", run_id=active.run_id))

    @app.route("/preflight/<experiment>", methods=["POST"])
    @requires_auth
    def preflight(experiment: str):
        """Run the #87 gate on the trial host, WITHOUT flying anything.

        Same client-of-the-CLI rule: this invokes the experiment's own
        scripts/preflight.sh through the same remote wrapper the runner uses.
        """
        root = app.config["EXPERIMENTS_ROOT"]
        if runs.busy():
            return render_template("error.html", message=(
                "A campaign is running — the gate bounces the ros2 daemon and "
                "must not run mid-trial. Wait or abort first.")), 409
        script = root / experiment / "scripts" / "preflight.sh"
        if not script.is_file():
            return render_template("error.html",
                                   message=f"{experiment} has no scripts/preflight.sh"), 404
        try:
            config = tr.RunnerConfig.load(experiment, root)
        except tr.RunnerError as exc:
            return render_template("error.html", message=str(exc)), 400
        host_level = request.form.get("host", "").strip() or None
        target = config.remote_target(host_level)
        cmd = ["bash", f"experiments/{experiment}/scripts/preflight.sh"]
        if target is not None:
            from .remote_exec_utils import wrap_command
            cmd = wrap_command(cmd, target)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=360,
                                  cwd=None if target else str(app.config["REPO_ROOT"]))
            ok, output = proc.returncode == 0, (proc.stdout + "\n" + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            ok, output = False, "preflight timed out after 360 s"
        journal.add("preflight", f"{experiment} on {host_level or 'local'}: "
                                 f"{'PASS — go' if ok else 'FAIL — no-go'}")
        return render_template("preflight.html", experiment=experiment,
                               host=host_level or "local", ok=ok, output=output)

    @app.route("/operator_step", methods=["POST"])
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
        root = app.config["EXPERIMENTS_ROOT"]
        experiment = request.form["experiment"]
        name = request.form["campaign"]
        kind = request.form.get("kind", "live")
        try:
            index = int(request.form["index"])
        except (KeyError, ValueError):
            return Response("bad step index", status=400, mimetype="text/plain")
        try:
            campaign = cs.load_campaign(root / experiment / "campaigns" / f"{name}.yaml", root)
            config = tr.RunnerConfig.load(experiment, root)
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
            from .remote_exec_utils import wrap_command
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
            return render_template("error.html", message=f"No run {run_id}"), 404
        return render_template("run.html", run=run, journal=journal.snapshot(),
                               base_url=request.host_url.rstrip("/"))

    @app.route("/api/run/<run_id>.json")
    @requires_auth
    def api_run(run_id: str):
        run = runs.get(run_id)
        if run is None:
            return {"error": "unknown run"}, 404
        return run.to_dict()

    @app.route("/run/<run_id>/abort", methods=["POST"])
    @requires_auth
    def abort_run(run_id: str):
        ok = runs.abort(run_id)
        journal.add("abort", f"{run_id}: {'requested' if ok else 'not running'}")
        return redirect(url_for("run_view", run_id=run_id))

    @app.route("/run/<run_id>/proceed", methods=["POST"])
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
            return render_template("error.html", message=f"No run {run_id}"), 404
        if run.awaiting_kind:
            journal.add("operator-confirm",
                        f"{run_id}: {run.awaiting_kind} confirmed — {run.awaiting_what}")
            run._go.set()
        return redirect(url_for("run_view", run_id=run_id))

    @app.route("/stop", methods=["POST"])
    @requires_auth
    def stop():
        """Soft-stop or e-stop, via scripts/estop.sh — a narrow allowlist."""
        action = request.form.get("action", "estop")
        robot = request.form.get("robot", "")
        cmd = [str(tools.path("estop.sh"))]
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
            from .remote_exec_utils import terminate_current_foreground
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
                               journal=journal.snapshot())

    @app.route("/robot_action", methods=["POST"])
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
            return render_template("error.html",
                                   message=f"{robot!r}.{action!r} is not a declared action"), 400
        cmd = [str(tools.path("robot_action.sh")), robot, action]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        ok = completed.returncode == 0
        journal.add("robot_action", f"{robot}.{action}: {'OK' if ok else 'FAILED'}",
                    (completed.stdout or "") + (completed.stderr or ""))
        return render_template("robot_action.html", robot=robot, action=action, ok=ok,
                               output=(completed.stdout or "") + (completed.stderr or ""),
                               journal=journal.snapshot())

    @app.route("/kill_ros", methods=["POST"])
    @requires_auth
    def kill_ros():
        """Clean slate on the rig hosts — recovery BETWEEN trials.

        Deliberately NOT a way to stop a flying drone: killing the stack takes
        away the commander and the position feed, which leaves the aircraft to
        its firmware failsafe rather than landing it. The e-stop is the tool
        for that, so this refuses while a run is executing unless the operator
        overrides, and the page says which one to reach for.
        """
        if runs.busy() and request.form.get("force") != "yes":
            return render_template("error.html", message=(
                "A trial is running. Killing ROS now would remove the drone's commander "
                "and position feed mid-flight — that is not a stop, it is an abandonment. "
                "Use E-STOP to stop the aircraft, or Abort to end the campaign after this "
                "trial; then clean the slate.")), 409
        results = []
        for spec in robot_hosts(app):
            workspace = app.config["REPO_ROOT"]
            argv = ["bash", "-lc", f"cd {shlex.quote(str(workspace))} && {tools.remote_invocation('kill_ros.sh')}"]
            if spec["host"]:
                from .remote_exec_utils import RemoteTarget, wrap_command
                target = RemoteTarget(host=spec["host"], container=spec["container"],
                                      workspace=Path(spec.get("workspace") or workspace),
                                      ros_setup=spec.get("ros_setup") or "/opt/ros/humble/setup.zsh",
                                      ws_setup=spec.get("ws_setup") or str(Path(spec.get("workspace") or workspace) / "ros2_ws/install/setup.zsh"),
                                      shell=spec["shell"])
                argv = wrap_command(argv, target)
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=90)
                results.append((spec["host"] or "local", proc.returncode == 0,
                                (proc.stdout + proc.stderr).strip()[-600:]))
            except subprocess.TimeoutExpired:
                results.append((spec["host"] or "local", False, "timed out after 90 s"))
        ok = all(r[1] for r in results) and bool(results)
        journal.add("kill-ros", f"clean slate on {len(results)} host(s): "
                                f"{'OK' if ok else 'SOME FAILED'}")
        output = "\n\n".join(f"=== {host} ===\n{text}" for host, _, text in results) \
            or "no hosts in robots.yaml"
        return render_template("stop.html", action="kill ROS", robot="all rig hosts",
                               ok=ok, output=output, journal=journal.snapshot())

    # ------------------------------------------------------------------ api --
    @app.route("/api/plan/<experiment>/<name>.json")
    @requires_auth
    def api_plan(experiment: str, name: str):
        root = app.config["EXPERIMENTS_ROOT"]
        try:
            campaign = cs.load_campaign(root / experiment / "campaigns" / f"{name}.yaml", root)
        except cs.SpecError as exc:
            return {"error": str(exc)}, 400
        plan = cp.build_plan(campaign, ti.load_trials(root, experiment),
                             request.args.get("include_unverified") == "1")
        return plan.to_dict()

    @app.route("/bag/<experiment>/<name>.mcap")
    def serve_bag(experiment: str, name: str):
        """Serve a SLIM bag so the laptop's Foxglove can stream it over the tunnel.

        Only slim bags (#94) are served, and only by resolving the name against
        the ones that actually exist — never by joining user input onto a path,
        which is how a `..` traversal would get out of the tree. Slim bags are a
        few MB, so streaming beats copying; the heavy originals stay put.

        Deliberately NOT behind requires_auth, with CORS and Range enabled:
        Foxglove (desktop or app.foxglove.dev) fetches this URL itself and
        cannot present the Basic credentials, so an authed route can never be
        opened from the viewer. Read-only data on a localhost/tunnel service;
        every state-changing route stays authed.
        """
        root = app.config["EXPERIMENTS_ROOT"]
        for slim_root in (root / experiment / "bags_slim",
                          root / experiment / "bags_issues_slim"):
            if not slim_root.is_dir():
                continue
            for candidate in slim_root.iterdir():
                if candidate.name != name or not candidate.is_dir():
                    continue
                splits = sorted(candidate.glob("*.mcap"))
                if splits:
                    resp = send_file(splits[0], mimetype="application/octet-stream",
                                     download_name=f"{name}.mcap", conditional=True)
                    resp.headers["Access-Control-Allow-Origin"] = "*"
                    return resp
        return {"error": f"no slim bag named {name!r} in {experiment}"}, 404

    @app.route("/quarantine/<experiment>/<name>", methods=["POST"])
    @requires_auth
    def quarantine_bag(experiment: str, name: str):
        """Move a recorded bag out of coverage, into its `_issues` sibling.

        Quarantine, not deletion: the index excludes `bags_issues*` from
        coverage while the recording stays inspectable — deleting data from a
        web button is how a mis-click erases a flight. Resolved the same
        traversal-safe way as serve_bag: by matching existing directory names,
        never joining user input onto a path.
        """
        root = app.config["EXPERIMENTS_ROOT"]
        pairs = (("bags", "bags_issues"), ("bags_slim", "bags_issues_slim"))
        for src_name, dst_name in pairs:
            src_root = root / experiment / src_name
            if not src_root.is_dir():
                continue
            for candidate in src_root.iterdir():
                if candidate.name != name or not candidate.is_dir():
                    continue
                dst_root = root / experiment / dst_name
                dst_root.mkdir(exist_ok=True)
                if (dst_root / candidate.name).exists():
                    return {"error": f"{dst_name}/{candidate.name} already exists"}, 409
                candidate.rename(dst_root / candidate.name)
                journal.add("quarantine",
                            f"{src_name}/{candidate.name} -> {dst_name}/")
                back = request.form.get("back") or url_for("index")
                return redirect(back)
        return {"error": f"no bag directory named {name!r} in {experiment}"}, 404

    @app.route("/viewers")
    @requires_auth
    def viewers():
        """How to open the live and recorded views ON THE LAPTOP.

        A page served by the rig cannot spawn RViz on the operator's machine, so
        this hands over a foxglove:// deep link (which the laptop's browser
        resolves to the laptop's Foxglove) and a copy-paste command for RViz.
        """
        root = app.config["EXPERIMENTS_ROOT"]
        base = request.host_url.rstrip("/")
        recent = []
        for slim_root in sorted(root.glob("*/bags_slim")):
            for bag in sorted(slim_root.iterdir(), reverse=True)[:5]:
                if bag.is_dir() and list(bag.glob("*.mcap")):
                    recent.append({
                        "experiment": slim_root.parent.name,
                        "name": bag.name,
                        "link": foxglove_link(base, slim_root.parent.name, bag.name),
                    })
        return render_template("viewers.html", recent=recent, base_url=base,
                               journal=journal.snapshot())

    @app.route("/healthz")
    def healthz():
        return {"ok": True}

    return app


def robot_hosts(app) -> List[Dict[str, str]]:
    """Distinct {host, container, shell} the robots are reachable on.

    Derived from robots.yaml — the same allowlist the e-stop uses — so the
    kill-all button can only ever reach machines this project already drives.
    """
    import yaml
    path = app.config["EXPERIMENTS_ROOT"] / "common" / "config" / "robots.yaml"
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    seen, out = set(), []
    for spec in (raw.get("robots") or {}).values():
        key = (spec.get("host", ""), spec.get("container", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"host": spec.get("host", ""), "container": spec.get("container", ""),
                    "shell": spec.get("shell", "zsh")})
    return out


def load_robots(app) -> List[str]:
    import yaml
    path = app.config["EXPERIMENTS_ROOT"] / "common" / "config" / "robots.yaml"
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return sorted((raw.get("robots") or {}).keys())


def load_robot_actions(app) -> Dict[str, List[Dict[str, str]]]:
    """The declared operator actions per robot (robots.yaml `actions:`).

    The same narrow-allowlist contract as the stops (2026-08-10): the UI
    renders and invokes exactly these entries — posture and status commands —
    and nothing else. scripts/robot_action.sh re-validates on its side too.
    """
    import yaml
    path = app.config["EXPERIMENTS_ROOT"] / "common" / "config" / "robots.yaml"
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
    from .arguments import build_parser  # the parser lives apart, so the help needs no Flask
    args = build_parser().parse_args(argv)
    roots.resolve_root_arguments(args)

    password = os.environ.get("EXPERIMENTS_UI_PASSWORD")
    if not password and not args.no_auth:
        print("ERROR: set EXPERIMENTS_UI_PASSWORD, or pass --no-auth.\n"
              "This UI can start a flight; it will not run unauthenticated by accident.",
              file=sys.stderr)
        return 2
    if args.host != "127.0.0.1" and not password:
        print("ERROR: refusing to bind a non-localhost address without a password.",
              file=sys.stderr)
        return 2
    if args.host != "127.0.0.1":
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

    app = create_app(args.experiments_root, password)
    try:
        app.run(host=args.host, port=args.port, threaded=True)
    except KeyboardInterrupt:
        pass
    # A plain return can leave the interpreter waiting on lingering threads
    # with the socket half-held — the "port blocked after Ctrl-C" report.
    # Exit hard, but say what that means if a run is in flight.
    runs_mgr = app.config.get("RUNS")
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

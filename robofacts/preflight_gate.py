#!/usr/bin/env python3
"""Block a trial from starting without a fresh pre-flight PASS (#87).

``run_preflight.py`` answers "is the stack safe right now". This makes that
answer *binding*: an automated sweep cannot record a trial unless the gate
passed moments ago, and every trial's bag carries the evidence.

Why an override exists
----------------------
A gate with no escape hatch gets bypassed — someone comments out the call, and
then nothing is checked and nothing is recorded. So there is an explicit one:
set ``override=True`` (the "WARNING checkbox") and the trial proceeds after a
loud banner. The difference from commenting the gate out is that the override,
its reason, and the exact checks that failed are written into the trial's
``trial_metadata.json``, so an overridden trial is visible forever afterwards
and can be excluded from analysis on purpose rather than by accident.

Why the default freshness window is zero
----------------------------------------
The 2026-07-22 incident flew with three safety layers down, each "verified"
earlier in the session. The lesson recorded in #87 is that a check which passed
minutes ago is worthless. So ``max_age_s=0`` re-runs the gate before *every*
trial. Raising it trades that guarantee for sweep speed — a deliberate,
per-experiment decision, recorded in the metadata as ``reused_age_s``.

The persistent verdict log (2026-10-01, SAC 2027 framework paper)
------------------------------------------------------------------
A NO-GO produces no bag, so it produces no metadata, and until this change its
verdict survived only in the terminal and the per-trial run log. Every verdict
(GO, NO-GO, overridden, shadow, disabled) is now also appended as one JSON line
to ``experiments/<experiment>/runs/preflight_gate.jsonl`` when the gate knows
where that is (``log_path``, set by the runner's ``build_gate``). The line
format is the one ``experiments/px4_sitl_offboard_demo/GATE_LOG_TODO.md``
specified. Writing it is never fatal: a read-only ``runs/`` must not turn a GO
into a crash.

Modes (2026-10-01, the ``gate`` factor kind)
--------------------------------------------
``binding`` is the normal gate. ``shadow`` runs every check and records the
verdict, but a NO-GO never blocks (it is an override whose reason starts with
``shadow``). ``disabled`` runs nothing. A campaign selects the mode per trial
through a factor of ``maps_to.kind: gate``, which the runner refuses unless the
experiment's ``runner.yaml`` declares ``simulation: true``: a shadow gate on a
real robot would fly a trial the checks said no to.

Trial variables (2026-10-01)
----------------------------
``check(label, values)`` passes every scalar factor value to
``run_preflight.py`` as ``--var <factor>=<level>``, so a checklist can say
``${world}`` or ``${n_vehicles}``. Variables given to the gate explicitly win
over the trial's values, and both win over the checklist's own defaults.

Factor checks (2026-10-06, realized conditions)
-----------------------------------------------
A factor may declare a check whose arguments depend on its level (a lighting
level and its brightness band, ``campaign_spec.factor_check_entries``). The
runner hands those entries to ``with_extra()``, and the gate runs the
checklist with them appended, through a temporary merged copy, so the static
checklist file stays the same for every trial. Each such entry is reported
back in ``GateResult.factor_checks`` (factor, level, result, the check's own
detail with the measured value), which the runner writes into the trial's
metadata and the gate log keeps for a NO-GO.

The stop latch as a measured value (2026-10-12)
-----------------------------------------------
The ``stop_latch`` check (``checks/stop_latch.sh``, the 120 s rule of
``stop_latch_clear.STOP_LATCH_MAX_AGE_S``) reports the latch's age. The gate
lifts it into ``GateResult.stop_latch`` (result, ``age_s``, detail), which the
trial metadata and every gate log line carry, GO and NO-GO alike, so "how old
was the latch when the gate said no" is a field rather than a parsed string.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import shlex
import json
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_RUNNER = Path(__file__).resolve().parent / "preflight" / "run_preflight.py"


def runner_command(runner: Path) -> List[str]:
    """How to start the check runner. The packaged one runs as a module, so its
    relative imports work wherever the package is installed; any other path (a
    test fixture's copy) runs as a plain script."""
    if Path(runner).resolve() == DEFAULT_RUNNER:
        return [sys.executable, "-m", "robofacts.preflight.run_preflight"]
    return [sys.executable, str(runner)]


def runner_env() -> Dict[str, str]:
    """The environment of the check runner: this process's, with the directory
    that holds this package first on PYTHONPATH, so ``-m`` finds the same copy
    of the package whether it is installed or imported from a source tree."""
    env = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = package_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env

RC_PASS, RC_FAIL, RC_USAGE = 0, 1, 2

#: The three gate modes a ``gate`` factor level may select.
GATE_MODES = ("binding", "shadow", "disabled")

#: The measured age in a stop_latch detail: ``age_s=62.0`` or ``age_s=none``.
_LATCH_AGE = re.compile(r"\bage_s=(none|[0-9]+(?:\.[0-9]+)?)\b")

#: One result line of run_preflight.py: ``  FAIL  topic_rate: <detail>``.
_RESULT_LINE = re.compile(r"^\s*(PASS|SKIP|FAIL|ERR)\s+([A-Za-z0-9_]+):\s?(.*)$")


class PreflightBlocked(RuntimeError):
    """Raised when the gate failed and no override was ticked."""


@dataclass
class GateResult:
    status: str                      # "pass" | "fail" | "disabled" | "error"
    allowed: bool                    # may the trial proceed?
    overridden: bool = False
    override_reason: str = ""
    checked_at: str = ""
    reused_age_s: Optional[float] = None
    failed_checks: List[str] = field(default_factory=list)
    output_tail: List[str] = field(default_factory=list)
    checklist: str = ""
    #: binding | shadow | disabled (see the module docstring).
    mode: str = "binding"
    #: Wall time of the whole gate, daemon bounce included.
    duration_s: Optional[float] = None
    #: Machine checks that reported a result (PASS, SKIP, FAIL or ERR).
    n_checks: Optional[int] = None
    #: One item per factor check the trial added: factor, level, check,
    #: result, detail (the measured value). Empty when no factor declares one.
    factor_checks: List[Dict[str, Any]] = field(default_factory=list)
    #: The stop_latch check's result, measured age (s, None = no latch) and
    #: detail. None when the checklist has no stop_latch entry or nothing ran.
    stop_latch: Optional[Dict[str, Any]] = None
    #: Where the checks ran: "local", or "<ssh host>/<container>" for a trial on
    #: another machine (2026-10-14, the gate runs where the trial runs).
    ran_on: str = "local"

    @property
    def verdict(self) -> str:
        """GO / NO-GO / DISABLED, independent of whether the NO-GO blocked."""
        if self.status == "disabled":
            return "DISABLED"
        return "GO" if self.status == "pass" else "NO-GO"

    def as_metadata(self) -> Dict[str, object]:
        """The block merged into the trial's ``trial_metadata.json``."""
        return {
            "status": self.status,
            "allowed": self.allowed,
            "overridden": self.overridden,
            "override_reason": self.override_reason,
            "checked_at": self.checked_at,
            "reused_age_s": self.reused_age_s,
            "failed_checks": list(self.failed_checks),
            "output_tail": list(self.output_tail),
            "checklist": self.checklist,
            "mode": self.mode,
            "verdict": self.verdict,
            "duration_s": self.duration_s,
            "n_checks": self.n_checks,
            "factor_checks": [dict(i) for i in self.factor_checks],
            "stop_latch": dict(self.stop_latch) if self.stop_latch else None,
            "ran_on": self.ran_on,
        }


@dataclass
class PreflightGate:
    """Runs the pre-flight gate and decides whether a trial may start."""

    checklist: Path
    variables: Dict[str, str] = field(default_factory=dict)
    #: 0 = re-run before every trial (the T-0 doctrine). Raise only deliberately.
    max_age_s: float = 0.0
    #: The WARNING checkbox: proceed even when the gate fails.
    override: bool = False
    override_reason: str = ""
    #: Set False only for simulation runs, where there is no hardware to check.
    enabled: bool = True
    runner: Path = DEFAULT_RUNNER
    timeout_s: float = 300.0
    #: Where every verdict is appended as one JSON line (None = no log). The
    #: runner's build_gate points it at experiments/<exp>/runs/preflight_gate.jsonl.
    log_path: Optional[Path] = None
    #: Carried into each log line. build_gate fills them from the campaign.
    experiment: str = ""
    campaign: Optional[str] = None
    #: Fallback for the log's `host` when the trial carries no execution_host.
    host: str = ""
    #: Set by with_mode(). Empty means "derive from enabled/override".
    mode: str = ""
    #: Checklist entries this trial adds (factor checks), set by with_extra().
    extra_entries: List[Dict[str, Any]] = field(default_factory=list)
    #: "sequential" or "concurrent" machine checks (run_preflight.py --concurrency).
    #: Empty leaves the choice to $TRIALS_PREFLIGHT_CONCURRENCY, the checklist's
    #: "concurrency", then run_preflight.py's default (concurrent, 2026-10-13).
    #: The verdict, the gate log and the metadata block do not depend on it.
    concurrency: str = ""
    #: The machine the trial runs on (a remote_exec_utils.RemoteTarget), set by
    #: with_target(). None runs the checks here, in a subprocess of the runner.
    target: Any = None
    #: (shell, files) to source before a LOCAL run (RunnerConfig.local_setup).
    local_setup: Optional[tuple] = None
    #: The project root of THIS checkout, to find the package's source tree on
    #: the target, whose checkout sits at target.workspace.
    project_root: Optional[Path] = None

    _last_pass_monotonic: Optional[float] = field(default=None, init=False, repr=False)
    _last_result: Optional[GateResult] = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------ #
    def with_mode(self, mode: str, reason: str = "") -> "PreflightGate":
        """A copy of this gate in ``mode`` (binding, shadow or disabled).

        A copy, so one trial's shadow mode can never leak into the next trial
        of the same campaign, which may be ``gate=on``.
        """
        if mode not in GATE_MODES:
            raise ValueError(f"unknown gate mode {mode!r} (known: {', '.join(GATE_MODES)})")
        if mode == "binding":
            return dataclasses.replace(self, mode=mode, enabled=True, override=False,
                                       override_reason="")
        if mode == "shadow":
            return dataclasses.replace(self, mode=mode, enabled=True, override=True,
                                       override_reason=reason or "shadow")
        return dataclasses.replace(self, mode=mode, enabled=False, override=False,
                                   override_reason="")

    def with_extra(self, entries: List[Dict[str, Any]]) -> "PreflightGate":
        """A copy of this gate that also runs ``entries`` (the trial's factor checks).

        A copy for the same reason as ``with_mode``: one trial's level must
        never leak into the next trial's gate.
        """
        return dataclasses.replace(self, extra_entries=[dict(e) for e in entries])

    def with_target(self, target: Any, local_setup: Optional[tuple] = None,
                    project_root: Optional[Path] = None) -> "PreflightGate":
        """A copy of this gate that runs its checks where the trial runs.

        Before 2026-10-14 the gate always ran on the runner's machine. A trial
        started on the laptop with ``execution_host=orin`` was gated on the
        laptop's processes, disk, network and stop latch (which robots.yaml
        writes inside the rig's container), so a clean laptop passed the gate
        of a dirty rig. With a target, the checklist is sent along with the
        command and ``run_preflight`` runs inside the target's container. A
        copy, like with_mode, so the host of one trial never leaks into the next.
        """
        return dataclasses.replace(self, target=target, local_setup=local_setup,
                                   project_root=Path(project_root) if project_root else None)

    def ran_on(self) -> str:
        if self.target is None:
            return "local"
        container = getattr(self.target, "container", "")
        return f"{self.target.host}/{container}" if container else str(self.target.host)

    def effective_mode(self) -> str:
        if self.mode:
            return self.mode
        if not self.enabled:
            return "disabled"
        return "shadow" if self.override else "binding"

    def check(self, trial_label: str = "",
              values: Optional[Dict[str, Any]] = None) -> GateResult:
        return self._check(trial_label, values)

    def _check(self, trial_label: str = "",
               values: Optional[Dict[str, Any]] = None) -> GateResult:
        """Gate one trial. Returns the result, or raises PreflightBlocked.

        ``values`` are the trial's factor values: each scalar one is passed to
        the checklist as ``--var <name>=<value>`` (explicit ``variables`` win),
        and ``execution_host`` names the host in the verdict log.
        """
        label = f" [{trial_label}]" if trial_label else ""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t0 = time.monotonic()
        mode = self.effective_mode()

        if not self.enabled:
            result = GateResult(status="disabled", allowed=True, checked_at=now_iso,
                                checklist=str(self.checklist), mode=mode,
                                duration_s=0.0, n_checks=0,
                                factor_checks=self._factor_items([]))
            print(f"[gate]{label} gate DISABLED — recorded in trial metadata")
            self._last_result = result
            self._log(trial_label, values, result, [])
            return result

        # A reused PASS never confirmed THIS trial's level, so factor checks
        # force a fresh run.
        reuse_age = None if self.extra_entries else self._fresh_pass_age()
        if reuse_age is not None:
            print(f"[gate]{label} reusing PASS from {reuse_age:.0f}s ago "
                  f"(max_age_s={self.max_age_s:.0f})")
            result = GateResult(status="pass", allowed=True, checked_at=now_iso,
                                reused_age_s=round(reuse_age, 1),
                                checklist=str(self.checklist), mode=mode,
                                duration_s=round(time.monotonic() - t0, 2), n_checks=0)
            self._last_result = result
            self._log(trial_label, values, result, [])
            return result

        where = "" if self.target is None else f" on {self.ran_on()}"
        print(f"[gate]{label} running gate{where}…")
        checklist, scratch = self._materialize()
        try:
            rc, output = self._run(values, checklist)
            items = _result_items(output, checklist)
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
        failed = _failed_checks(output)
        tail = output.strip().splitlines()[-12:]
        duration = round(time.monotonic() - t0, 2)
        factor_items = self._factor_items(items)
        latch = stop_latch_measurement(items)

        if rc == RC_PASS:
            self._last_pass_monotonic = time.monotonic()
            result = GateResult(status="pass", allowed=True, checked_at=now_iso,
                                output_tail=tail, checklist=str(self.checklist),
                                mode=mode, duration_s=duration, n_checks=len(items),
                                factor_checks=factor_items, stop_latch=latch)
            print(f"[gate]{label} PASS — clear to run")
            self._last_result = result
            self._log(trial_label, values, result, items)
            return result

        status = "fail" if rc == RC_FAIL else "error"
        result = GateResult(status=status, allowed=False, checked_at=now_iso,
                            failed_checks=failed, output_tail=tail,
                            checklist=str(self.checklist), mode=mode,
                            duration_s=duration, n_checks=len(items),
                            factor_checks=factor_items, stop_latch=latch)

        if self.override:
            result.allowed = True
            result.overridden = True
            result.override_reason = self.override_reason or "(no reason given)"
            self._print_override_banner(label, failed, result.override_reason)
            self._last_result = result
            self._log(trial_label, values, result, items)
            return result

        print(output)
        self._last_result = result
        self._log(trial_label, values, result, items)
        raise PreflightBlocked(
            f"T-0 gate {status.upper()}{label}: "
            + (", ".join(failed) if failed else "see output above")
            + ". Fix it, or set the gate's override flag with a reason to proceed anyway."
        )

    # ------------------------------------------------------------------ #
    def _fresh_pass_age(self) -> Optional[float]:
        if self.max_age_s <= 0 or self._last_pass_monotonic is None:
            return None
        age = time.monotonic() - self._last_pass_monotonic
        return age if age <= self.max_age_s else None

    def gate_variables(self, values: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """The --var set: the trial's scalar factor values, then explicit variables."""
        merged: Dict[str, str] = {}
        for name, value in (values or {}).items():
            if isinstance(value, (str, int, float, bool)) and str(name).isidentifier():
                merged[str(name)] = str(value)
        merged.update({str(k): str(v) for k, v in self.variables.items()})
        return merged

    def _materialize(self) -> tuple[Path, Optional[Path]]:
        """(checklist to run, scratch dir to delete) with the extra entries appended."""
        if not self.extra_entries:
            return Path(self.checklist), None
        data = json.loads(Path(self.checklist).read_text(encoding="utf-8"))
        if isinstance(data, list):
            data = {"checklist": data}
        data = dict(data)
        data["checklist"] = list(data.get("checklist") or []) + [
            {"text": e["text"], "check": e["check"], "args": dict(e.get("args") or {})}
            for e in self.extra_entries]
        scratch = Path(tempfile.mkdtemp(prefix="gate_factor_checks_"))
        merged = scratch / Path(self.checklist).name
        merged.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return merged, scratch

    def _factor_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The result of each extra entry, joined by its text (unique by construction)."""
        out = []
        for entry in self.extra_entries:
            match = next((i for i in items if i.get("text") == entry["text"].strip()), None)
            out.append({"factor": entry.get("factor"), "level": entry.get("level"),
                        "check": entry["check"], "args": dict(entry.get("args") or {}),
                        "result": match["result"] if match else "NOT RUN",
                        "detail": match["detail"] if match else ""})
        return out

    def _arguments(self, values: Optional[Dict[str, Any]]) -> List[str]:
        args: List[str] = []
        # A remote run does not see this process's environment, so the choice is spelled out.
        concurrency = self.concurrency or (os.environ.get("TRIALS_PREFLIGHT_CONCURRENCY", "")
                                           if self.target is not None else "")
        if concurrency:
            args += ["--concurrency", concurrency]
        for name, value in self.gate_variables(values).items():
            args += ["--var", f"{name}={value}"]
        return args

    def command(self, values: Optional[Dict[str, Any]] = None,
                checklist: Optional[Path] = None) -> List[str]:
        """The argv that runs the checks: here, here after the host's setup files, or on the target."""
        checklist = Path(checklist or self.checklist)
        if self.target is None:
            cmd = runner_command(self.runner) + ["--checklist", str(checklist)] + self._arguments(values)
            if self.local_setup:
                from .remote_exec_utils import local_setup_command
                shell, files = self.local_setup
                cmd = local_setup_command(cmd, files, shell)
            return cmd
        from .remote_exec_utils import wrap_command
        return wrap_command(["sh", "-c", remote_gate_script(
            checklist.read_bytes(), self._arguments(values), self._remote_pythonpath())], self.target)

    def _remote_pythonpath(self) -> str:
        """This package's directory on the target, when it lives inside this checkout.

        Run from a source tree (or a colcon install inside the checkout), the
        same relative path under target.workspace holds the target's copy, so
        the target runs the gate code of its own checkout. An installed package
        outside the checkout is found by the target's sourced workspace instead.
        """
        if self.project_root is None:
            return ""
        package_parent = Path(__file__).resolve().parents[1]
        try:
            rel = package_parent.relative_to(Path(self.project_root).resolve())
        except ValueError:
            return ""
        return str(Path(self.target.workspace) / rel)

    def _run(self, values: Optional[Dict[str, Any]] = None,
             checklist: Optional[Path] = None) -> tuple[int, str]:
        cmd = self.command(values, checklist)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s,
                                  env=runner_env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return RC_FAIL, f"gate timed out after {self.timeout_s:.0f}s (treated as FAIL)"
        except OSError as exc:
            return RC_USAGE, f"could not run {self.runner}: {exc}"
        output = (proc.stdout or "") + (proc.stderr or "")
        if self.target is not None:
            # ssh -tt gives the remote output CRLF line ends, and a result line must parse alike.
            output = output.replace("\r\n", "\n").replace("\r", "")
            if proc.returncode == 255:
                return RC_USAGE, output + f"\nssh to {self.ran_on()} failed (treated as an ERROR)"
        return proc.returncode, output

    def _log(self, trial_label: str, values: Optional[Dict[str, Any]],
             result: GateResult, items: List[Dict[str, Any]]) -> None:
        """Append one verdict line to the gate log. Never raises."""
        result.ran_on = self.ran_on()
        if self.log_path is None:
            return
        try:
            record = gate_log_record(
                experiment=self.experiment, campaign=self.campaign,
                trial_label=trial_label, result=result, items=items,
                host=str((values or {}).get("execution_host") or self.host
                         or socket.gethostname()))
            append_gate_log(Path(self.log_path), record)
        except Exception as exc:  # noqa: BLE001 - a log line never outranks a trial
            print(f"[gate] gate log not written ({exc}) - continuing")

    @staticmethod
    def _print_override_banner(label: str, failed: List[str], reason: str) -> None:
        bar = "!" * 78
        print(f"\n{bar}")
        print(f"!! T-0 GATE FAILED — PROCEEDING ANYWAY BY OPERATOR OVERRIDE{label}")
        for item in failed or ["(see gate output)"]:
            print(f"!!   FAILED: {item}")
        print(f"!! Reason: {reason}")
        print("!! This override is recorded in the trial's trial_metadata.json.")
        print(f"{bar}\n")

    @property
    def last_result(self) -> Optional[GateResult]:
        return self._last_result


def remote_gate_script(checklist: bytes, arguments: List[str], pythonpath: str = "") -> str:
    """A shell script that runs the gate on another machine.

    The checklist travels inside the script (base64), because the trial's
    checklist may exist only here (factor checks are merged into a temporary
    copy) and the ssh wrapper forces a tty, which makes stdin unreliable. It is
    written to a temporary file there, run, and removed. The exit code is the
    gate's.
    """
    encoded = base64.b64encode(checklist).decode("ascii")
    env = f"PYTHONPATH={shlex.quote(pythonpath)}${{PYTHONPATH:+:$PYTHONPATH}} " if pythonpath else ""
    return (f'f=$(mktemp /tmp/trials_gate_XXXXXX) || exit 2; '
            f'echo {encoded} | base64 -d > "$f" || {{ rm -f "$f"; exit 2; }}; '
            f'{env}python3 -m robofacts.preflight.run_preflight --checklist "$f" '
            f'{shlex.join(arguments)}; rc=$?; rm -f "$f"; exit $rc')


def stop_latch_measurement(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The stop_latch item as {result, age_s, detail}, or None when it did not run."""
    item = next((i for i in items if i.get("check") == "stop_latch"), None)
    if item is None:
        return None
    match = _LATCH_AGE.search(item.get("detail") or "")
    age: Optional[float] = None
    if match and match.group(1) != "none":
        age = float(match.group(1))
    return {"result": item.get("result"), "age_s": age, "detail": item.get("detail", "")}


def _failed_checks(output: str) -> List[str]:
    """Pull the failing check lines out of run_preflight.py's report."""
    failed = []
    for line in output.splitlines():
        stripped = line.strip()
        for marker in ("FAIL  ", "ERR   ", "ERR  "):
            if stripped.startswith(marker):
                failed.append(stripped[len(marker):].strip())
                break
    return failed


def _checklist_machine_entries(checklist: Path) -> List[Dict[str, Any]]:
    """(check, text, hard) of every machine-checked entry, in file order."""
    try:
        data = json.loads(Path(checklist).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("checklist") if isinstance(data, dict) else data
    out = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("check"):
            text = str(item.get("text", "")).strip()
            out.append({"check": str(item["check"]), "text": text,
                        "hard": bool(item.get("hard", text.upper().startswith("[HARD]")))})
    return out


def _result_items(output: str, checklist: Path) -> List[Dict[str, Any]]:
    """Every PASS/SKIP/FAIL/ERR line of run_preflight.py, joined to its entry text.

    run_preflight.py reports the machine entries in file order, so the n-th
    result line belongs to the n-th machine entry. That join is what tells two
    ``topic_rate`` items apart. When the counts disagree (a usage error, a
    --skip), the text falls back to the first entry with the same check name.
    """
    results = []
    for line in output.splitlines():
        match = _RESULT_LINE.match(line)
        if match:
            results.append((match.group(1), match.group(2), match.group(3).strip()))
    entries = _checklist_machine_entries(checklist)
    aligned = len(entries) == len(results) and all(
        e["check"] == r[1] for e, r in zip(entries, results))
    items = []
    for i, (state, check, detail) in enumerate(results):
        if aligned:
            entry = entries[i]
        else:
            entry = next((e for e in entries if e["check"] == check),
                         {"text": "", "hard": False})
        items.append({"check": check, "result": state, "detail": detail,
                      "text": entry["text"], "hard": entry["hard"]})
    return items


def gate_log_record(*, experiment: str, campaign: Optional[str], trial_label: str,
                    result: GateResult, items: List[Dict[str, Any]],
                    host: str) -> Dict[str, Any]:
    """One line of ``runs/preflight_gate.jsonl`` (format: GATE_LOG_TODO.md).

    Two keys beyond the specification: ``mode`` (binding, shadow, disabled)
    and ``skipped``. ``failed`` carries every FAIL or ERR item, each with its
    ``hard`` flag, because run_preflight.py treats a failed soft item as a
    NO-GO too, so a GO with warnings does not exist to be distinguished.
    """
    failed = [{"check": i["check"], "text": i["text"], "detail": i["detail"],
               "hard": i["hard"]}
              for i in items if i["result"] in ("FAIL", "ERR")]
    if not items and result.failed_checks:
        # The runner could not run (rc 2) or printed no result lines.
        failed = [{"check": f.split(":", 1)[0], "text": "", "detail": f, "hard": True}
                  for f in result.failed_checks]
    if result.status == "error" and not failed:
        failed = [{"check": "(gate)", "text": "",
                   "detail": (result.output_tail or ["gate could not run"])[-1],
                   "hard": True}]
    record = {
        "ts": result.checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "experiment": experiment,
        "campaign": campaign,
        "trial_label": trial_label,
        "verdict": result.verdict,
        "mode": result.mode,
        "override": (result.override_reason or None) if result.overridden else None,
        "n_checks": result.n_checks if result.n_checks is not None else len(items),
        "failed": failed,
        "skipped": [i["check"] for i in items if i["result"] == "SKIP"],
        "reused_age_s": result.reused_age_s,
        "duration_s": result.duration_s,
        "host": host,
    }
    if result.ran_on != "local":
        record["ran_on"] = result.ran_on
    if result.stop_latch is not None:
        # The latch's measured age, for a GO as much as for a NO-GO (2026-10-12).
        record["stop_latch"] = {k: result.stop_latch.get(k) for k in ("result", "age_s")}
    if result.factor_checks:
        # The declared level and what the rig measured, kept for a NO-GO too
        # (a NO-GO has no bag to carry it).
        record["factor_checks"] = [
            {k: i.get(k) for k in ("factor", "level", "check", "result", "detail")}
            for i in result.factor_checks]
    return record


def append_gate_log(path: Path, record: Dict[str, Any]) -> None:
    """Append one JSON line. The file is append-only by construction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=False) + "\n")


def read_gate_log(path: Path) -> List[Dict[str, Any]]:
    """Every parseable line of a gate log (a torn last line is skipped)."""
    out: List[Dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out

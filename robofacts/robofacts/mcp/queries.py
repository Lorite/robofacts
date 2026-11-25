"""What the MCP server answers: functions that read and plan, and return JSON-ready dicts.

Every function here reads the declarations and the recorded trials of the project in force
and returns a plain ``dict``. None of them runs a trial, runs a gate check, writes a stop
latch or changes a file: the dry run calls the runner with ``dry_run=True`` and no gate,
and ``validate`` leaves out the experiment's own tests, which are project code. Execution
stays with the operator at the command line. ``test_mcp_read_only.py`` holds this boundary: it
calls every query on a copy of the example project and checks that no file changed and
that no process other than the lint's pytest started.

This module does not import the MCP SDK, so the same functions work, and are tested, where
the SDK is not installed. ``server.py`` registers them as MCP tools.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .. import __version__, roots

#: The tools of the server, in the order a client lists them. Every one is read-only.
#: A new tool is added here and to the read-only test, deliberately.
READ_ONLY_TOOLS = (
    "list_experiments",
    "list_campaigns",
    "validate_experiment",
    "lint",
    "plan_campaign",
    "dry_run_campaign",
    "coverage_index",
    "list_trials",
    "explain_trial",
)

#: The stages of ``trials validate`` this server never runs, and why.
NOT_RUN_HERE = {
    "tests": "not run by the MCP server: an experiment's own test_*.py files are project code, "
             "so the server cannot promise that they only read. Run `trials validate` at the "
             "command line for this stage",
}

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)")
_PYTEST_PROBLEM = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")
_PYTEST_ID = re.compile(r"^(\w+)(?:\[(.*)\])?$")
#: One call at a time: a call redirects the process's stdout and restores its environment,
#: and both are process-wide. Re-entrant, because the server wraps a tool that also uses it.
_LOCK = threading.RLock()
#: Lines of a command's output kept in a result.
TAIL_LINES = 40


class ToolInputError(ValueError):
    """A request the server refuses or cannot answer, with the reason for the caller."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def quiet() -> Iterator[io.StringIO]:
    """Run one call with its printed output captured and the environment restored.

    The framework's functions print their progress. Over stdio that output must not reach
    the client, and a caller may want it, so it is captured. Some functions set environment
    variables for their children (the dry run of ``validate`` sets TRIALS_VOICE). They are
    restored, so one call cannot change the next."""
    buffer = io.StringIO()
    with _LOCK:
        saved_env = dict(os.environ)
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                yield buffer
        finally:
            os.environ.clear()
            os.environ.update(saved_env)


def _tail(text: str, lines: int = TAIL_LINES) -> List[str]:
    return [line for line in text.strip().splitlines()[-lines:]] if text.strip() else []


def _unmounted(root: Path, experiment: str) -> str:
    """Why no plan can be computed because recordings lie on a disk that is not mounted
    (review B2-8: they would read as missing cells), else ""."""
    from .. import trial_index as ti
    try:
        ti.require_recordings_mounted(root, experiment)
    except ti.RecordingsNotMounted as exc:
        return str(exc)
    return ""


def _require_mounted(root: Path, experiment: str) -> None:
    why = _unmounted(root, experiment)
    if why:
        raise ToolInputError(why)


def experiments_root() -> Path:
    try:
        return roots.experiments_root()
    except roots.RootNotFound as exc:
        raise ToolInputError(str(exc)) from exc


def _rel(path: Optional[Path], base: Path) -> Optional[str]:
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def _name(value: str, what: str) -> str:
    """A plain name of an experiment, campaign or trial. Paths are refused, so every read
    stays inside the experiments root."""
    if not isinstance(value, str) or not _NAME.match(value) or ".." in value:
        raise ToolInputError(f"{what} must be a plain name (letters, digits, '_', '.', '-'), "
                             f"not a path: {value!r}")
    return value


def experiment_dir(experiment: str) -> Path:
    root = experiments_root()
    path = root / _name(experiment, "experiment")
    if not (path / "config").is_dir():
        known = ", ".join(experiment_names(root)) or "none"
        raise ToolInputError(f"no experiment {experiment!r} under {root} (experiments: {known})")
    return path


def campaign_file(experiment: str, campaign: str) -> Path:
    stem = _name(campaign, "campaign")
    stem = stem[:-5] if stem.endswith(".yaml") else stem
    path = experiment_dir(experiment) / "campaigns" / f"{stem}.yaml"
    if not path.is_file():
        known = ", ".join(p.stem for p in sorted(path.parent.glob("*.yaml"))) or "none"
        raise ToolInputError(f"no campaign {stem!r} in experiment {experiment!r} "
                             f"(campaigns: {known})")
    return path


def experiment_names(root: Path) -> List[str]:
    """The experiments of a project: folders with campaigns or a runner.yaml (as in the UI)."""
    names = {p.parents[1].name for p in root.glob("*/campaigns/*.yaml")}
    names |= {p.parents[1].name for p in root.glob("*/config/runner.yaml")}
    return sorted(names)


def _set_values(factor_levels: Optional[Dict[str, Sequence[Any]]]) -> List[str]:
    """``{"rate": ["slow"]}`` in the CLI's ``--set rate=slow`` spelling."""
    out = []
    for name, levels in (factor_levels or {}).items():
        _name(name, "factor")
        levels = [levels] if isinstance(levels, (str, int, float, bool)) else list(levels)
        if not levels:
            raise ToolInputError(f"factor_levels names no level for {name!r}")
        for level in levels:
            if "," in str(level):
                raise ToolInputError(
                    f"the level {str(level)!r} of {name!r} contains a comma, which the --set syntax "
                    "reads as two levels. Give each level as its own list item")
        out.append(f"{name}={','.join(str(v) for v in levels)}")
    return out


def _load_campaign(experiment: str, campaign: str, factor_levels=None):
    from .. import campaign_spec as cs
    root = experiments_root()
    path = campaign_file(experiment, campaign)
    try:
        loaded = cs.load_campaign(path, root, _set_values(factor_levels))
    except cs.SpecError as exc:
        raise ToolInputError(f"the campaign does not load: {exc}") from exc
    refusal = cs.contract_refusal(loaded, root)
    if refusal:
        raise ToolInputError(refusal)
    return root, path, loaded


def _cli_path(path: Path) -> str:
    """A path as the operator types it: relative to the project root when inside it."""
    try:
        base = roots.project_root()
    except roots.RootNotFound:
        return str(path)
    return _rel(path, base) or str(path)


def _class_counts(trials) -> Dict[str, int]:
    counts = Counter(t.status for t in trials)
    return {"total": len(trials), "usable": counts["usable"],
            "unverified": counts["unverified"], "excluded": counts["excluded"]}


# --------------------------------------------------------------------------- #
# The tools
# --------------------------------------------------------------------------- #
def list_experiments(with_counts: bool = True) -> Dict[str, Any]:
    from .. import backend, trial_index as ti
    root = experiments_root()
    try:
        project_root: Optional[str] = str(roots.project_root())
    except roots.RootNotFound:
        project_root = None
    project_file = roots.find_project_file()
    active = backend.active(root)
    trials_by_exp: Dict[str, list] = defaultdict(list)
    if with_counts:
        for trial in ti.load_trials(root):
            trials_by_exp[trial.experiment].append(trial)
    experiments = []
    for name in experiment_names(root):
        path = root / name
        entry: Dict[str, Any] = {
            "name": name,
            "campaigns": [p.stem for p in sorted((path / "campaigns").glob("*.yaml"))],
            "has_runner": (path / "config" / "runner.yaml").is_file(),
            "has_gate_checklist": (path / "config" / "preflight_checklist.json").is_file(),
        }
        if with_counts:
            entry["trials"] = _class_counts(trials_by_exp.get(name, []))
        experiments.append(entry)
    return {
        "robofacts_version": __version__,
        "experiments_root": str(root),
        "project_root": project_root,
        "project_file": str(project_file) if project_file else None,
        "backend": backend.configured_name(root),
        "recording_noun": getattr(active.recorder, "noun", "recording"),
        "experiments": experiments,
    }


def list_campaigns(experiment: str) -> Dict[str, Any]:
    from .. import campaign_plan as cp, campaign_spec as cs, trial_index as ti
    root = experiments_root()
    exp_dir = experiment_dir(experiment)
    trials = ti.load_trials(root, experiment)
    out = []
    for path in sorted((exp_dir / "campaigns").glob("*.yaml")):
        entry: Dict[str, Any] = {"name": path.stem, "file": _cli_path(path)}
        try:
            campaign = cs.load_campaign(path, root, [])
        except cs.SpecError as exc:
            entry["error"] = str(exc)
            out.append(entry)
            continue
        entry.update({
            "describe": campaign.describe,
            "sweep": campaign.sweep,
            "fixed": campaign.fixed,
            "repetitions": campaign.repetitions,
            "size": campaign.size(),
            "preflight_checklist": campaign.preflight_checklist or "config/preflight_checklist.json",
            "order": campaign.order or None,
        })
        refusal = cs.contract_refusal(campaign, root) or _unmounted(root, experiment)
        if refusal:
            entry["error"] = refusal
        else:
            plan = cp.build_plan(campaign, trials)
            entry.update({"wanted": plan.wanted, "covered": plan.covered, "needed": plan.needed})
        out.append(entry)
    return {"experiment": experiment, "campaigns": out}


def plan_campaign(experiment: str, campaign: str,
                  factor_levels: Optional[Dict[str, Sequence[Any]]] = None,
                  include_unverified: bool = False) -> Dict[str, Any]:
    from .. import campaign_plan as cp, trial_index as ti
    root, path, loaded = _load_campaign(experiment, campaign, factor_levels)
    _require_mounted(root, loaded.experiment)
    plan = cp.build_plan(loaded, ti.load_trials(root, loaded.experiment), include_unverified)
    result = plan.to_dict()
    result["schedule"] = [{"values": item.cell.values, "repetition": item.repetition,
                           "order": item.order} for item in cp.execution_order(plan)]
    result["file"] = _cli_path(path)
    result["command"] = shlex.join(["trials", "plan", _cli_path(path), *_set_args(factor_levels)])
    return result


def _set_args(factor_levels) -> List[str]:
    return [arg for item in _set_values(factor_levels) for arg in ("--set", item)]


def dry_run_campaign(experiment: str, campaign: str,
                     factor_levels: Optional[Dict[str, Sequence[Any]]] = None,
                     include_unverified: bool = False,
                     limit: Optional[int] = None) -> Dict[str, Any]:
    """The runner's dry run: the exact commands of the next trials, nothing executed."""
    from .. import backend, campaign_plan as cp, trial_index as ti, trial_runner as tr
    from ..preflight_gate import PreflightGate
    if limit is not None and limit < 1:
        raise ToolInputError("limit must be 1 or more")
    root, path, loaded = _load_campaign(experiment, campaign, factor_levels)
    try:
        config = tr.RunnerConfig.load(loaded.experiment, root)
    except tr.RunnerError as exc:
        raise ToolInputError(str(exc)) from exc
    problems = tr.gate_factor_problems(loaded, config, "")
    if problems:
        raise ToolInputError("; ".join(problems))
    _require_mounted(root, loaded.experiment)
    plan = cp.build_plan(loaded, ti.load_trials(root, loaded.experiment), include_unverified)
    recordings = root / loaded.experiment / backend.record_dirs(root)["recordings"]
    with quiet() as log:
        # Nothing may be said aloud from a dry run, even by mistake.
        os.environ["TRIALS_VOICE"] = "off"
        results = tr.run_plan(plan, config, recordings, gate=None, dry_run=True, limit=limit,
                              extra_verify=loaded.verify_steps)
    checklist = root / loaded.experiment / (loaded.preflight_checklist
                                            or "config/preflight_checklist.json")
    pre_launch: List[str] = []
    if checklist.is_file():
        # Only reads the checklist: the names of its pre-launch entries. No check runs.
        with quiet():
            pre_launch = list(PreflightGate(checklist=checklist).stage_checks("pre_launch"))
    trials = []
    for result in results:
        trials.append({
            "values": dict(result.trial.values),
            "describe": result.trial.describe(),
            "status": result.status,
            "detail": result.detail,
            "recording_dir": _rel(result.recording_dir, root),
            "commands": [{"argv": list(cmd), "shell": shlex.join(cmd)} for cmd in result.commands],
        })
    run_args = ["trials", "run", _cli_path(path), *_set_args(factor_levels)]
    if limit:
        run_args += ["--limit", str(limit)]
    return {
        "executed": False,
        "experiment": loaded.experiment,
        "campaign": loaded.name,
        "wanted": plan.wanted, "covered": plan.covered, "needed": plan.needed,
        "trials": trials,
        "gate_checklist": _rel(checklist, root) if checklist.is_file() else None,
        "pre_launch_checks": pre_launch,
        "preview": _tail(log.getvalue(), 200),
        # Text for the operator, never an argument list: no code here builds a command that runs.
        "operator_command": shlex.join(run_args) + " --execute",
        "note": "Nothing was launched, recorded or written. To run these trials, the operator "
                "runs operator_command at the command line in the project root, with the robot "
                "area and the stop ready. The server never runs it.",
    }


def validate_experiment(experiment: str, campaign: Optional[str] = None,
                        skip: Optional[Sequence[str]] = None,
                        verbose: bool = False) -> Dict[str, Any]:
    """``trials validate`` without the experiment's own tests (see NOT_RUN_HERE)."""
    from .. import validate as v
    root = experiments_root()
    unknown = sorted(set(skip or ()) - set(v.STAGES))
    if unknown:
        raise ToolInputError(f"unknown stage(s) {unknown}, the stages are {list(v.STAGES)}")
    target = str(campaign_file(experiment, campaign)) if campaign else str(experiment_dir(experiment))
    with quiet():
        report = v.validate(target, root, skip=sorted(set(skip or ()) | set(NOT_RUN_HERE)))
    stages = []
    for r in report.results:
        stage: Dict[str, Any] = {"stage": r.stage, "subject": r.subject, "status": r.status,
                                 "summary": r.summary, "seconds": round(r.seconds, 2)}
        if r.stage in NOT_RUN_HERE and r.status == "skip" and not (skip and r.stage in skip):
            stage["summary"] = NOT_RUN_HERE[r.stage]
        if r.output.strip() and (verbose or r.status == "FAIL"):
            stage["output_tail"] = _tail(r.output)
        stages.append(stage)
    failed = [s for s in stages if s["status"] == "FAIL"]
    return {
        "experiment": report.experiment,
        "campaigns": [c.stem for c in report.campaigns],
        "ready": report.ready,
        "failed_stages": len(failed),
        "stages": stages,
        "not_run_here": sorted(NOT_RUN_HERE),
        "cli_command": shlex.join(["trials", "validate", _cli_path(Path(target))]),
    }


def lint(experiment: Optional[str] = None) -> Dict[str, Any]:
    """The declaration rules (``trials lint``), as pytest runs them, parsed."""
    from .. import backend, lint as lint_rules, validate as v
    root = experiments_root()
    extra_env = {}
    if experiment:
        experiment_dir(experiment)
        extra_env[v.LINT_ENV] = experiment
    files = [lint_rules.__file__, *[str(f) for f in backend.active(root).lint_files]]
    with quiet():
        rc, output, summary = v._pytest([*files, "-rfE"], root, extra_env)
    counts = {key: 0 for key in ("passed", "failed", "errors", "skipped")}
    for number, word in _PYTEST_COUNT.findall(summary):
        word = "errors" if word.startswith("error") else word
        if word in counts:
            counts[word] += int(number)
    problems = []
    for line in output.splitlines():
        match = _PYTEST_PROBLEM.match(line.strip())
        if match:
            test = match.group(2)
            parts = _PYTEST_ID.match(test.rsplit("::", 1)[-1])
            problems.append({
                "kind": match.group(1).lower(), "test": test,
                # test_every_experiment_declares_a_verify_step -> "every experiment declares a verify step"
                "rule": parts.group(1).removeprefix("test_").replace("_", " ") if parts else test,
                "subject": parts.group(2) if parts else None,
                "message": match.group(3) or ""})
    return {
        "experiment": experiment,
        "ok": rc == 0,
        "returncode": rc,
        "summary": summary,
        "counts": counts,
        "problems": problems,
        "output_tail": _tail(output) if rc != 0 else [],
        "cli_command": shlex.join(["trials", "lint", *(["--experiment", experiment] if experiment else [])]),
    }


def coverage_index(experiment: Optional[str] = None,
                   factors: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The coverage index: the usable, unverified and excluded trials per cell."""
    from .. import trial_index as ti, trial_records as tr_
    root = experiments_root()
    if experiment:
        experiment_dir(experiment)
    for factor in factors or ():
        _name(factor, "factor")
    trials = ti.load_trials(root, experiment)
    by_experiment: Dict[str, list] = defaultdict(list)
    for trial in trials:
        by_experiment[trial.experiment].append(trial)
    out = []
    for name in sorted(by_experiment):
        subset = by_experiment[name]
        found = list(factors) if factors else ti.factors_for(name, subset, root)
        found = [f for f in found if any(f in t.metadata for t in subset)]
        axes = found if factors else found[:3]
        declared = set(ti.declared_factors(name, root))
        cells = ti.coverage(subset, axes)
        excluded = []
        for t in subset:
            if t.status == "excluded":
                status, reason = tr_._status_of(t)
                excluded.append({"trial": t.bag_dir.name, "status": status, "reason": reason})
        out.append({
            "experiment": name,
            "trials": _class_counts(subset),
            "axes": [{"factor": f, "source": "contract" if f in declared else "discovered"}
                     for f in axes],
            "also_varying": [f for f in found if f not in axes],
            "cells": [{"values": dict(zip(axes, c.values)), "usable": c.usable,
                       "unverified": c.unverified, "excluded": c.excluded, "total": c.total}
                      for c in cells],
            "missing_combinations": [dict(zip(axes, m))
                                     for m in ti.missing_combinations(cells, axes)],
            "contract_violations": [{"trial": t.bag_dir.name, "problems": list(t.problems)}
                                    for t in subset if t.status == "unverified"],
            "excluded_trials": excluded,
            "corrected_trials": [t.bag_dir.name for t in subset if t.corrections],
        })
    return {
        "experiments_root": str(root),
        "classes": {"usable": "satisfies the metadata contract, not aborted, not excluded: it counts",
                    "unverified": "the metadata does not satisfy the contract: it does not count",
                    "excluded": "aborted, excluded by a person, without recorded data, or scratch"},
        "trials": _class_counts(trials),
        "experiments": out,
    }


def _records(experiment: str):
    from .. import trial_records as tr_
    root = experiments_root()
    experiment_dir(experiment)
    return root, tr_.load_records(root, experiment, campaigns=tr_._load_campaigns(root, experiment))


def list_trials(experiment: str, campaign: Optional[str] = None, status: Optional[str] = None,
                where: Optional[Dict[str, Any]] = None, limit: int = 100) -> Dict[str, Any]:
    """Every trial of an experiment (``trials records``), newest first, in short form."""
    from .. import trial_records as tr_
    if status is not None and status not in (*tr_.STATUSES, *tr_.CLASSES, "attempt", "excluded-any"):
        raise ToolInputError(f"unknown status {status!r}, use one of "
                             f"{[*tr_.STATUSES, *tr_.CLASSES, 'attempt', 'excluded-any']}")
    root, records = _records(experiment)
    chosen = tr_.filter_records(records, campaign=campaign, status=status,
                                where={k: str(v) for k, v in (where or {}).items()} or None)
    shown = chosen[:max(1, limit)]
    return {
        "experiment": experiment,
        "summary": tr_.summary_line(chosen),
        "total": len(chosen),
        "shown": len(shown),
        "trials": [{"name": r.name, "class": r.trial_class, "label": tr_.class_label(r),
                    "status": r.status, "reason": r.reason, "kind": r.kind,
                    "campaign": r.campaign, "counts_for": list(r.counts_for),
                    "values": dict(r.values), "repetition": r.repetition, "started": r.started,
                    "gate": (r.gate or {}).get("verdict")} for r in shown],
    }


def explain_trial(experiment: str, trial: str) -> Dict[str, Any]:
    """One trial with everything that decides whether it counts."""
    from .. import trial_index as ti, trial_records as tr_
    name = _name(trial, "trial")
    root, records = _records(experiment)
    record = next((r for r in records if r.name == name or r.slim_name == name), None)
    if record is None:
        recent = ", ".join(r.name for r in records[:5]) or "none"
        raise ToolInputError(f"no trial {name!r} in experiment {experiment!r} "
                             f"(the newest: {recent}). list_trials lists them")
    loaded = next((t for t in ti.load_trials(root, experiment)
                   if record.metadata_path is not None and t.bag_dir == record.metadata_path.parent),
                  None)
    result = record.to_dict(root)
    result.update({
        "label": tr_.class_label(record),
        "counts": record.counts,
        "explanation": _explanation(record),
        "trial_metadata": record.metadata,
        "recorded_metadata": (loaded.recorded if loaded is not None and loaded.corrections else None),
        "contract_problems": list(loaded.problems) if loaded is not None else [],
        "reverification": loaded.reverification if loaded is not None else None,
        "verify_steps": tr_.verify_report(record.run_log),
        "gate_log": [row for row in tr_.gate_log(root, experiment) if row.get("trial_label") == record.name],
        "attempts": [row for row in tr_.attempts(root, experiment) if row.get("trial_label") == record.name],
    })
    return result


def _explanation(record) -> str:
    """One sentence: does it count, and why."""
    if record.trial_class is None:
        return (f"An attempt that left no trial ({record.status})"
                + (f": {record.reason}" if record.reason else "") + ". It does not count.")
    if record.counts:
        campaigns = ", ".join(record.counts_for) or "no current campaign"
        return f"Usable: it counts as coverage, for {campaigns}."
    if record.trial_class == "usable":
        return "Usable, but a scratch recording, which never counts."
    why = f": {record.reason}" if record.reason else ""
    return f"{record.trial_class.capitalize()} ({record.status}){why}. It does not count."

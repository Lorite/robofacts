"""Run a recorded trial's verify steps again, after a verify rule was fixed (2026-11-04).

The runner judges a bag once, with the verify steps of that moment. When a step
was wrong (a rule written for one mode that misfires in another), the trial
stays ``aborted`` although its data is valid, and the only ways out were to fly
it again or to edit ``trial_metadata.json`` by hand, which robofacts never does.
The first case was ``20261029T162519_hover_s-60_pose_feed-none_rep01`` of
an experiment that flies a drone: the receive-rate rule of the mocap
flights failed a Flow-deck-only hover whose every block arrived at its rate.

``trials reverify <exp> <trial> --reason "..."`` finds the trial's campaign and
factor values in ``runs/attempts.jsonl`` (with the corrections of ``trials amend`` on
top, as the index counts the trial), renders the CURRENT verify steps
(``config/runner.yaml`` plus the campaign's own) exactly as the runner does
(``{bag}``, ``{<factor>}``, ``{arg_<launch argument>}``), runs every step (not
only up to the first failure) from the project root on the bag, and writes
``reverification.json`` beside the bag and its slim copy: the steps with their
commands, return codes and output tails, the code version, who, when and why.
``trial_metadata.json`` is never edited.

What the index does with it (``trial_decisions.apply_reverification``): it lifts
the abort only when (1) the recorded abort came from a verify step and nothing
else (the runner's ``abort_reason`` starts with ``verify step``, which it writes
only when the action returned 0), (2) every current step passed, and (3) the
record still matches the ``abort_reason`` it was written against. A trial whose
action failed, or that never recorded, can never be lifted. A re-verify that
FAILS on a usable trial is recorded and reported, but it does not exclude the
trial: a slim copy may lack a topic a step reads, so exclusion stays a person's
decision (``trials exclude``).

Run it where the trial's record lives, the host that recorded it (the robot computer's
container for the rig trials), inside the dev container (the steps read bags
with the backend's reader). The archive and the laptop's staging copy mirror that host
with ``rsync --delete``, so a ``reverification.json`` written into a copy is
deleted by the next sync. The bag read is the full bag when it holds a storage
file, else its slim copy.

    trials reverify <exp> <trial> --reason "..." [--campaign NAME] [--by NAME] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import roots
from . import trial_decisions as td

LEDGER = Path("runs") / "attempts.jsonl"
OUTPUT_TAIL_LINES = 12


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def ledger_entry(exp_dir: Path, name: str) -> Optional[Dict[str, Any]]:
    """The newest ledger line of ``name`` that wrote metadata, or None."""
    path = Path(exp_dir) / LEDGER
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("trial_label") == name:
            found = rec
    return found


def storage_dir(dirs: td.TrialDirs) -> Path:
    """The full bag when it holds a storage file, else the slim copy, else an error."""
    from .trial_index import recording_holds_data
    for d in (dirs.full, dirs.slim):
        if d is not None and recording_holds_data(d):
            return d
    raise td.DecisionError(f"{dirs.name}: neither the bag nor its slim copy holds recorded data "
                           f"here. Run it on the host that recorded the trial.")


def placeholders(values: Dict[str, Any], launch_args: Any, bag: Path) -> Dict[str, Any]:
    """What the runner fills into a verify step: the factor values, ``{bag}`` and ``{arg_*}``."""
    out: Dict[str, Any] = dict(values)
    if isinstance(launch_args, dict):
        out.update({f"arg_{k}": v for k, v in launch_args.items()})
    out["bag"] = str(bag)
    out.setdefault("recording", str(bag))   # the same directory, under the general name
    return out


def corrected_inputs(experiments_root: Path, experiment: str, values: Dict[str, Any],
                     launch_args: Any, corrections: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], Any]:
    """The factor values and launch arguments of a trial as a person corrected them.

    ``trials amend`` writes corrections.json, and the index counts the trial in the corrected
    cell. A re-verify that rendered the ledger's values instead checked a trial amended from
    ``slow`` to ``fast`` with ``--rate slow``, and a pass then counted it in the ``fast`` cell
    (2026-11-25). The newest correction of each factor wins, as in
    ``trial_decisions.apply_corrections``. A recorded launch argument that a corrected level
    drives takes the corrected level's value, the other ones stay as recorded.
    """
    if not corrections:
        return values, launch_args
    from . import campaign_spec as cs
    try:
        factors = cs.load_factors(experiment, experiments_root)
    except cs.SpecError as exc:
        raise td.DecisionError(f"the factors of {experiment} do not load, so its corrections "
                               f"cannot be applied: {exc}") from exc
    by_key = {f.metadata_key: name for name, f in factors.items() if f.metadata_key}
    corrected = dict(values)
    for entry in corrections:
        name = entry.get("factor") if entry.get("factor") in factors else by_key.get(str(entry["field"]))
        if name is not None:
            corrected[name] = entry["corrected"]
    if not isinstance(launch_args, dict):
        return corrected, launch_args
    before = cs.TrialSpec(experiment, dict(values), []).launch_args(factors)
    after = cs.TrialSpec(experiment, corrected, []).launch_args(factors)
    args = dict(launch_args)
    for arg in set(before) | set(after):
        if before.get(arg) == after.get(arg):
            continue
        if arg in after:
            args[arg] = after[arg]
        else:
            args.pop(arg, None)
    return corrected, args


def render_steps(steps: Sequence[Dict[str, Any]], values: Dict[str, Any]) -> List[Tuple[str, str]]:
    """``(name, command)`` per step, rendered as the runner's ``templated_command`` renders it
    (``campaign_spec.render_argv``: every value stays inside its own argument, so a value read
    back from the ledger cannot add arguments) and joined with ``shlex.join``, which
    ``run_steps`` splits back into exactly those arguments."""
    from .campaign_spec import render_argv
    out = []
    for step in steps:
        try:
            out.append((step["name"], shlex.join(render_argv(step["command"], values))))
        except KeyError as exc:
            raise td.DecisionError(f"verify step {step['name']!r} references {exc}, which the "
                                   f"trial's ledger values do not hold") from exc
    return out


def current_steps(experiments_root: Path, experiment: str, campaign: str) -> List[Dict[str, Any]]:
    """The verify steps a trial of ``campaign`` gets today: the runner's, then the campaign's."""
    from . import campaign_spec as cs
    from .trial_runner import RunnerConfig, RunnerError
    path = experiments_root / experiment / "campaigns" / f"{campaign}.yaml"
    if not path.is_file():
        raise td.DecisionError(f"no campaign file {path} (the ledger names {campaign!r})")
    try:
        config = RunnerConfig.load(experiment, experiments_root)
        spec = cs.load_campaign(path, experiments_root)
    except (RunnerError, cs.SpecError) as exc:
        raise td.DecisionError(f"the declarations do not load: {exc}") from exc
    return list(config.verify_steps) + list(spec.verify_steps or [])


def reader_problem(root: Optional[Path] = None) -> Optional[str]:
    """Why this machine cannot read the project's recordings, or None when it can. The
    backend's recorder answers (an optional method ``reader_problem``)."""
    from . import backend
    check = getattr(backend.active(root).recorder, "reader_problem", None)
    return check() if check is not None else None


def can_read_bags() -> bool:
    if reader_problem() is not None:
        return False
    return True


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def run_steps(rendered: Sequence[Tuple[str, str]], cwd: Path,
              run: Callable[..., Any] = subprocess.run) -> List[Dict[str, Any]]:
    """Every step, also after a failure, so the record says which ones fail today."""
    results = []
    for name, command in rendered:
        completed = run(shlex.split(command), cwd=str(cwd), capture_output=True, text=True,
                        check=False)
        output = ((getattr(completed, "stdout", "") or "") + (getattr(completed, "stderr", "") or "")).strip()
        results.append({"name": name, "command": command,
                        "rc": int(getattr(completed, "returncode", 0)),
                        "output_tail": output.splitlines()[-OUTPUT_TAIL_LINES:]})
    return results


def _code_version(project_root: Path) -> Dict[str, Any]:
    try:
        from . import code_version
        return code_version.probe(None, workspace=project_root, builds=False)
    except Exception as exc:  # noqa: BLE001 - provenance, never fatal
        return {"error": str(exc)[:200]}


def reverify(experiments_root: Path, experiment: str, trial: str, *, reason: str,
             by: Optional[str] = None, campaign: Optional[str] = None,
             project_root: Optional[Path] = None, dry_run: bool = False,
             now: Optional[float] = None, run: Callable[..., Any] = subprocess.run,
             check_ros: bool = True) -> Dict[str, Any]:
    """Run the trial's current verify steps and record the outcome. Returns the record."""
    root = Path(experiments_root)
    exp_dir = root / experiment
    reason = (reason or "").strip()
    if not reason:
        raise td.DecisionError("a re-verify needs a reason (--reason): which rule changed, and why "
                               "the recorded verdict is in doubt")
    dirs = td.locate(exp_dir, trial)
    td.check_not_being_written(root, experiment, dirs, now=now)
    try:
        recorded = json.loads((dirs.metadata_dir / "trial_metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise td.DecisionError(f"{dirs.name}: trial_metadata.json is unreadable ({exc})") from exc
    entry = ledger_entry(exp_dir, dirs.name)
    campaign = campaign or (entry or {}).get("campaign")
    if not campaign:
        raise td.DecisionError(f"{dirs.name} has no line in {LEDGER} that names its campaign: "
                               f"pass --campaign")
    values = (entry or {}).get("values")
    if not isinstance(values, dict) or not values:
        raise td.DecisionError(f"{dirs.name}: the ledger holds no factor values for it, so its "
                               f"verify steps cannot be rendered as the runner rendered them")
    bag = storage_dir(dirs)
    corrections = max((td.read_corrections(d) for d in dirs.dirs), key=len)
    values, launch_args = corrected_inputs(root, experiment, values, recorded.get("launch_args"),
                                           corrections)
    rendered = render_steps(current_steps(root, experiment, campaign),
                            placeholders(values, launch_args, bag.resolve()))
    if not rendered:
        raise td.DecisionError(f"campaign {campaign} of {experiment} declares no verify steps")
    if dry_run:
        return {"trial": dirs.name, "campaign": campaign, "bag_read": str(bag),
                "steps": [{"name": n, "command": c} for n, c in rendered], "dry_run": True}
    if check_ros and not can_read_bags():
        raise td.DecisionError(reader_problem() or "the recordings cannot be read here")
    cwd = Path(project_root) if project_root else roots.project_root(experiments=root)
    results = run_steps(rendered, cwd, run=run)
    verify_ok = all(r["rc"] == 0 for r in results)
    abort_reason = recorded.get("abort_reason")
    record = {
        "decision": "reverified",
        "trial": dirs.name,
        "date": td._now_iso(now),
        "by": by or td.default_by(),
        "reason": reason,
        "campaign": campaign,
        "bag_read": td._rel(bag, exp_dir),
        "code_version": _code_version(cwd),
        "steps": results,
        "verify_ok": verify_ok,
        "recorded_aborted": bool(recorded.get("aborted")),
        "recorded_abort_reason": abort_reason,
        "lifts_abort": bool(verify_ok and recorded.get("aborted")
                            and td.abort_is_verify_only(abort_reason)),
        "tool": td._tool("reverify"),
    }
    previous = [r for r in (td.read_reverification(d) for d in dirs.dirs) if r]
    if previous:
        old = previous[0]
        record["previous"] = [{k: v for k, v in old.items() if k != "previous"}] + list(old.get("previous") or [])
    for d in dirs.dirs:
        td._write_json(d / td.REVERIFICATION_FILE, record)
    return record


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="trials reverify",
        description="run a recorded trial's CURRENT verify steps again and write "
                    "reverification.json beside the bag and its slim copy; a pass lifts an "
                    "abort that came from a verify step only, trial_metadata.json is never edited")
    roots.add_root_arguments(ap, project=True)
    ap.add_argument("experiment")
    ap.add_argument("trial", help="the trial's bag directory name (as `trials records` lists it)")
    ap.add_argument("--reason", required=True, help="which rule changed and why the verdict is in doubt")
    ap.add_argument("--campaign", help="the campaign (default: the trial's line in runs/attempts.jsonl)")
    ap.add_argument("--by", help=f"who decided (default: ${td.ENV_BY}, else the login name)")
    ap.add_argument("--dry-run", action="store_true", help="print the rendered steps, run nothing, write nothing")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)
    try:
        record = reverify(args.experiments_root, args.experiment, args.trial, reason=args.reason,
                          by=args.by, campaign=args.campaign, project_root=args.project_root,
                          dry_run=args.dry_run)
    except td.DecisionError as exc:
        print(f"trials: {exc}", file=sys.stderr)
        return 1
    print(f"{record['trial']} (campaign {record['campaign']}), reading {record['bag_read']}")
    for step in record["steps"]:
        if record.get("dry_run"):
            print(f"  would run {step['name']}: {step['command']}")
            continue
        last = step["output_tail"][-1] if step["output_tail"] else ""
        print(f"  {step['name']}: rc={step['rc']}  {last}")
    if record.get("dry_run"):
        return 0
    if record["lifts_abort"]:
        print("re-verified: every step passes and the recorded abort came from a verify step, "
              "so the trial counts again")
    elif record["verify_ok"]:
        print("re-verified: every step passes" + (
            "; the recorded abort did not come from a verify step alone, so it stands"
            if record["recorded_aborted"] else ""))
    else:
        print("re-verify FAILED: recorded in reverification.json, the trial's status is unchanged "
              "(`trials exclude` takes a usable trial out of coverage)")
    return 0 if record["verify_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

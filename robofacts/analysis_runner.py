#!/usr/bin/env python3
"""Named analyses as a headless API (#96 Phase 4b).

The analysers already exist as CLI scripts writing into
``experiments/<name>/results/<timestamp>_*/``. What was missing is everything
around them: choosing *which* bags to feed in, and recording afterwards which
bags a result actually came from.

That second part is why notebooks were losing information. A ``results/`` folder
told you a number but not the set it was computed over, so re-running a figure
months later meant reconstructing the bag list by memory. Every run here writes
``analysis_metadata.json`` recording the analysis, the exact bag set, the filter
that selected it, and the command — the analysis-side counterpart of
``trial_metadata.json``.

Bag selection comes from the Phase 1 index, so a cohort is expressed as factor
values ("all the position_only figure8 trials on the rig") rather than a
hand-maintained list, and **only usable trials are fed in by default** — an
aborted or contract-violating bag must not silently enter a mean.

Analyses are declared in ``experiments/<name>/config/analyses.yaml``:

    analyses:
      apriltag_vs_mocap:
        describe: "AprilTag pose against mocap ground truth"
        per: bag                       # or: cohort (one run over all of them)
        command: "python3 .../analyze_apriltag_vs_mocap_bag.py {bag} --output-dir {results}"

so adding an analysis is a config edit, and the UI (Phase 5) drives the same
declarations rather than growing its own list.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml


from . import trial_index as ti
from . import roots


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)
ANALYSES_CONFIG_NAME = "analyses.yaml"
ANALYSIS_METADATA_NAME = "analysis_metadata.json"


class AnalysisError(Exception):
    """The analysis declaration is unusable, or the selection is empty."""


@dataclass
class Analysis:
    name: str
    command: str
    describe: str = ""
    per: str = "bag"            # "bag" -> once per bag; "cohort" -> once for all
    requires_topics: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.per not in ("bag", "cohort"):
            raise AnalysisError(f"analysis {self.name!r}: per must be 'bag' or 'cohort', "
                                f"got {self.per!r}")
        if not self.command.strip():
            raise AnalysisError(f"analysis {self.name!r}: command is required")


def load_analyses(experiment: str, experiments_root: Optional[Path] = None) -> Dict[str, Analysis]:
    experiments_root = roots.experiments_root(experiments_root)
    path = experiments_root / experiment / "config" / ANALYSES_CONFIG_NAME
    if not path.is_file():
        raise AnalysisError(f"{experiment} declares no analyses ({path} missing)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("analyses")
    if entries is None or (isinstance(entries, dict) and not entries):
        # A scaffolded experiment ships `analyses: {}` and fills it in later.
        # "None declared yet" is a legitimate state, distinct from a malformed
        # file — erroring here made `--list` crash on every new experiment.
        return {}
    if not isinstance(entries, dict):
        raise AnalysisError(f"{path}: 'analyses' must be a mapping, got {type(entries).__name__}")
    out: Dict[str, Analysis] = {}
    for name, spec in entries.items():
        if not isinstance(spec, dict):
            raise AnalysisError(f"{path}: analysis {name!r} must be a mapping")
        out[name] = Analysis(
            name=name,
            command=str(spec.get("command", "")),
            describe=str(spec.get("describe", "")).strip(),
            per=str(spec.get("per", "bag")),
            requires_topics=list(spec.get("requires_topics") or []),
        )
    return out


# --------------------------------------------------------------------------- #
# Cohort selection
# --------------------------------------------------------------------------- #
def select_bags(experiment: str, experiments_root: Optional[Path] = None,
                where: Optional[Dict[str, str]] = None,
                include_unverified: bool = False,
                include_excluded: bool = False) -> List[ti.Trial]:
    """Trials matching a factor filter, usable-only unless told otherwise.

    Feeding an aborted or contract-violating bag into a mean is exactly the
    quiet corruption the Phase 1 three-state split exists to prevent, so the
    caller has to ask for it explicitly.
    """
    allowed = {"usable"}
    if include_unverified:
        allowed.add("unverified")
    if include_excluded:
        allowed.add("excluded")

    trials = []
    for trial in ti.load_trials(experiments_root, experiment):
        if trial.status not in allowed:
            continue
        if any(trial.value(key) != str(value) for key, value in (where or {}).items()):
            continue
        trials.append(trial)
    return sorted(trials, key=lambda t: t.bag_dir.name)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
@dataclass
class AnalysisResult:
    analysis: str
    results_dir: Path
    bags: List[str]
    commands: List[List[str]] = field(default_factory=list)
    status: str = "dry-run"      # dry-run | ok | failed
    detail: str = ""


def _render(command: str, mapping: Dict[str, str], analysis_name: str) -> List[str]:
    try:
        return shlex.split(command.format(**mapping))
    except KeyError as exc:
        raise AnalysisError(
            f"analysis {analysis_name!r} references unknown placeholder {exc}; "
            f"available: {', '.join(sorted(mapping))}") from exc


def run_analysis(analysis: Analysis, bags: List[ti.Trial], experiment: str,
                 experiments_root: Optional[Path] = None, *,
                 where: Optional[Dict[str, str]] = None,
                 dry_run: bool = True,
                 executor: Optional[Callable[[List[str]], int]] = None,
                 clock: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
                 ) -> AnalysisResult:
    if not bags:
        raise AnalysisError(
            f"no bags selected for {analysis.name!r} — the filter matched nothing, or every "
            f"match is aborted/unverified (pass --include-unverified to widen)")

    stamp = clock()
    results_dir = experiments_root / experiment / "results" / f"{stamp}_{analysis.name}"
    bag_names = [t.bag_dir.name for t in bags]

    commands: List[List[str]] = []
    if analysis.per == "cohort":
        commands.append(_render(analysis.command, {
            "bags": " ".join(str(t.bag_dir) for t in bags),
            "results": str(results_dir),
            "experiment": experiment,
        }, analysis.name))
    else:
        for trial in bags:
            commands.append(_render(analysis.command, {
                "bag": str(trial.bag_dir),
                "results": str(results_dir / trial.bag_dir.name),
                "experiment": experiment,
            }, analysis.name))

    result = AnalysisResult(analysis.name, results_dir, bag_names, commands)
    if dry_run:
        return result

    results_dir.mkdir(parents=True, exist_ok=True)
    run = executor or (lambda cmd: subprocess.run(cmd, check=False).returncode)
    failures = 0
    for cmd in commands:
        if run(cmd) != 0:
            failures += 1

    # The analysis-side counterpart of trial_metadata.json: what was computed,
    # over exactly which bags, selected how. Without this a results folder is a
    # number with no provenance.
    (results_dir / ANALYSIS_METADATA_NAME).write_text(json.dumps({
        "schema_version": 1,
        "analysis_kind": "experiment_analysis",
        "analysis": analysis.name,
        "analysis_notes": analysis.describe,
        "experiment": experiment,
        "generated_at": stamp,
        "selection": {"where": dict(where or {}), "n_bags": len(bags)},
        "bags": bag_names,
        "commands": [shlex.join(c) for c in commands],
        "failed_commands": failures,
    }, indent=2), encoding="utf-8")

    result.status = "failed" if failures else "ok"
    result.detail = f"{failures} of {len(commands)} command(s) failed" if failures else ""
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_where(pairs: Optional[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise AnalysisError(f"--where expects key=value (got {pair!r})")
        key, _, value = pair.partition("=")
        out[key.strip()] = value.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("experiment")
    ap.add_argument("analysis", nargs="?", help="which declared analysis to run")
    roots.add_root_arguments(ap)
    ap.add_argument("--where", action="append", default=[], metavar="factor=value",
                    help="restrict the cohort (repeatable)")
    ap.add_argument("--include-unverified", action="store_true")
    ap.add_argument("--include-excluded", action="store_true")
    ap.add_argument("--list", action="store_true", help="list declared analyses and exit")
    ap.add_argument("--execute", action="store_true",
                    help="actually run; without it only the commands are printed")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    try:
        analyses = load_analyses(args.experiment, args.experiments_root)
        where = parse_where(args.where)
    except AnalysisError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list or not args.analysis:
        print(f"Analyses declared for {args.experiment}:")
        for name, analysis in sorted(analyses.items()):
            print(f"  {name}  [per {analysis.per}]")
            if analysis.describe:
                print(f"      {analysis.describe}")
        return 0

    if args.analysis not in analyses:
        print(f"ERROR: unknown analysis {args.analysis!r}; declared: "
              f"{', '.join(sorted(analyses))}", file=sys.stderr)
        return 2

    bags = select_bags(args.experiment, args.experiments_root, where,
                       args.include_unverified, args.include_excluded)
    try:
        result = run_analysis(analyses[args.analysis], bags, args.experiment,
                              args.experiments_root, where=where, dry_run=not args.execute)
    except AnalysisError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"analysis: {result.analysis}   bags: {len(result.bags)}   -> {result.results_dir}")
    for name in result.bags[:10]:
        print(f"    {name}")
    if len(result.bags) > 10:
        print(f"    … and {len(result.bags) - 10} more")
    print("")
    if not args.execute:
        print("=== DRY RUN — nothing executed ===")
    for cmd in result.commands[:10]:
        print(f"  $ {shlex.join(cmd)}")
    if len(result.commands) > 10:
        print(f"  … and {len(result.commands) - 10} more")
    if result.detail:
        print(f"\n{result.detail}")
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The trial-metadata contract: shared core + per-experiment fields.

An index is only as trustworthy as the metadata under it, and a coverage grid
built on holes looks authoritative while being wrong. So the contract is a file
(``experiments/common/config/trial_metadata_core.yaml`` plus an optional
``experiments/<name>/config/trial_metadata_schema.yaml``), validation is a
function, and a test enforces it — the same shape as the pre-flight
checklists.

Two entry points:

* ``validate(metadata, experiment)`` — called **at record time**, so a bad trial
  is caught while the rig is still up and can be re-run, not months later.
* ``audit`` (the CLI) — sweeps every recorded bag and reports what is missing,
  which is how the existing holes were found.

Deliberately **non-fatal at record time**: a schema complaint must never destroy
a finished trial. It warns loudly and records the problems into the metadata
itself under ``_schema_problems``, so the bag carries its own health flag and
the index can exclude it on purpose rather than by accident.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import roots


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)
#: The shared contract, relative to the experiments root.
CORE_SCHEMA_REL = roots.CORE_SCHEMA_REL
PER_EXPERIMENT_NAME = "trial_metadata_schema.yaml"

_TYPES = {"str": str, "int": int, "float": (int, float), "bool": bool,
          "list": list, "dict": dict}


class SchemaError(Exception):
    """The schema file itself is malformed — distinct from a trial failing it."""


@dataclass
class FieldSpec:
    name: str
    required: bool = False
    type: Optional[str] = None
    enum: Optional[List[Any]] = None
    factor: bool = False
    describe: str = ""


@dataclass
class Schema:
    fields: Dict[str, FieldSpec] = field(default_factory=dict)
    sources: List[Path] = field(default_factory=list)

    @property
    def required(self) -> List[str]:
        return sorted(n for n, f in self.fields.items() if f.required)

    @property
    def factors(self) -> List[str]:
        """Fields the coverage grid may group by (phase 1)."""
        return sorted(n for n, f in self.fields.items() if f.factor)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
#: Parsed schema files by (path, mtime, size). ``trial_index.load_trials`` validates
#: every trial against its experiment's schema, and parsing the YAML again for each
#: one made the index of a 128-trial experiment take 6 s (2026-10-16: the web
#: UI's trials table). A changed file has a new mtime, so an edit is never missed.
_PARSED: Dict[tuple, Dict[str, "FieldSpec"]] = {}


def _load_file(path: Path) -> Dict[str, FieldSpec]:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None and key in _PARSED:
        return dict(_PARSED[key])
    out = _parse_file(path)
    if key is not None:
        _PARSED[key] = dict(out)
    return out


def _parse_file(path: Path) -> Dict[str, FieldSpec]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SchemaError(f"{path}: invalid YAML: {exc}") from exc
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        raise SchemaError(f"{path}: expected a top-level 'fields' mapping")

    out: Dict[str, FieldSpec] = {}
    for name, spec in fields.items():
        if not isinstance(spec, dict):
            raise SchemaError(f"{path}: field {name!r} must be a mapping")
        declared_type = spec.get("type")
        if declared_type is not None and declared_type not in _TYPES:
            raise SchemaError(f"{path}: field {name!r} has unknown type {declared_type!r} "
                              f"(known: {', '.join(sorted(_TYPES))})")
        out[name] = FieldSpec(
            name=name,
            required=bool(spec.get("required", False)),
            type=declared_type,
            enum=spec.get("enum"),
            factor=bool(spec.get("factor", False)),
            describe=str(spec.get("describe", "")).strip(),
        )
    return out


def load_schema(experiment: Optional[str] = None,
                experiments_root: Optional[Path] = None) -> Schema:
    """Core schema, with the experiment's own fields merged on top."""
    root = roots.experiments_root(experiments_root)
    core_path = root / "common" / "config" / "trial_metadata_core.yaml"
    if not core_path.is_file():
        raise SchemaError(f"core metadata contract not found: {core_path}")

    fields = _load_file(core_path)
    sources = [core_path]

    if experiment:
        per = root / experiment / "config" / PER_EXPERIMENT_NAME
        if per.is_file():
            # Experiment fields override the core entry of the same name, which
            # lets an experiment tighten a field (e.g. make it required) without
            # forking the core file.
            fields.update(_load_file(per))
            sources.append(per)
    return Schema(fields=fields, sources=sources)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate(metadata: Dict[str, Any], experiment: Optional[str] = None,
             schema: Optional[Schema] = None,
             experiments_root: Optional[Path] = None) -> List[str]:
    """Return a list of problems; empty means the trial satisfies the contract."""
    schema = schema or load_schema(experiment, experiments_root)
    problems: List[str] = []

    for name, spec in sorted(schema.fields.items()):
        present = name in metadata
        value = metadata.get(name)

        if spec.required and (not present or value is None):
            why = f" — {spec.describe}" if spec.describe else ""
            problems.append(f"{name}: required but {'missing' if not present else 'null'}{why}")
            continue
        if not present or value is None:
            continue

        if spec.type and not isinstance(value, _TYPES[spec.type]):
            # bool is a subclass of int in Python; don't let True satisfy int.
            problems.append(f"{name}: expected {spec.type}, got {type(value).__name__}")
            continue
        if spec.type == "int" and isinstance(value, bool):
            problems.append(f"{name}: expected int, got bool")
            continue

        if spec.enum and value not in spec.enum:
            problems.append(f"{name}: {value!r} is not one of {spec.enum}")

    # Cross-field rule: an aborted trial without a reason is a dead end later.
    if metadata.get("aborted") is True and not str(metadata.get("abort_reason") or "").strip():
        problems.append("abort_reason: required when aborted is true — "
                        "an unexplained abort cannot be interpreted afterwards")
    return problems


def annotate(metadata: Dict[str, Any], experiment: Optional[str] = None,
             experiments_root: Optional[Path] = None) -> List[str]:
    """Validate and record the verdict **into** the metadata, non-fatally.

    Called at record time. The trial is never rejected — it is already flown —
    but the bag carries its own health flag so the index can exclude it
    deliberately instead of silently averaging bad data.
    """
    problems = validate(metadata, experiment, experiments_root=experiments_root)
    if problems:
        metadata["_schema_problems"] = problems
        print(f"[trial-metadata] {len(problems)} contract problem(s) — recorded in the bag:")
        for problem in problems:
            print(f"[trial-metadata]   - {problem}")
    else:
        metadata.pop("_schema_problems", None)
    return problems


# --------------------------------------------------------------------------- #
# Audit CLI
# --------------------------------------------------------------------------- #
def audit(experiments_root: Path, experiment: Optional[str] = None) -> Dict[str, List[str]]:
    """Validate every recorded trial. Returns {trial directory: problems}.

    The trials are the ones ``trial_index.load_trials`` reads, so this audit and the index,
    the plan and the records judge the same set (2026-11-25). Before, it read only the full
    recordings: on a laptop that holds only the slim copies (the view load_trials documents)
    the index counted the trials and this audit said "No recorded trials found". A slim copy
    is audited when its full recording is not here, as load_trials reads it. Each trial is
    validated as the runner wrote it (corrections are not applied), and a metadata file that
    cannot be read is reported as before.
    """
    from . import backend
    from . import trial_index as ti

    def key(directory: Path):
        name = directory.name
        return directory.parents[1].name, name[:-len("_slim")] if name.endswith("_slim") else name

    results: Dict[str, List[str]] = {}
    loaded = set()
    for trial in ti.load_trials(experiments_root, experiment, validate=False):
        recorded = trial.recorded if trial.recorded is not None else trial.metadata
        results[str(trial.bag_dir)] = validate(recorded, trial.experiment,
                                               experiments_root=experiments_root)
        loaded.add(key(trial.bag_dir))
    # load_trials skips a file it cannot read: the audit names it, unless another copy of
    # the same trial was read (a broken slim copy beside a good full recording).
    prefix = f"{experiment}/" if experiment else "*/"
    rd = backend.record_dirs(experiments_root)
    for record_dir in (rd["recordings"], rd["issues"], rd["slim"], rd["issues_slim"]):
        for meta_path in experiments_root.glob(f"{prefix}{record_dir}/*/trial_metadata.json"):
            if str(meta_path.parent) in results or key(meta_path.parent) in loaded:
                continue
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                results[str(meta_path.parent)] = [f"unreadable: {exc}"]
                continue
            if not isinstance(metadata, dict):
                results[str(meta_path.parent)] = ["unreadable: not a JSON object"]
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    roots.add_root_arguments(ap)
    ap.add_argument("--experiment", help="restrict to one experiment")
    ap.add_argument("--show-schema", action="store_true", help="print the merged contract and exit")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any trial violates the contract")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    if args.show_schema:
        schema = load_schema(args.experiment, args.experiments_root)
        print("sources: " + ", ".join(str(s) for s in schema.sources))
        print(f"required ({len(schema.required)}): {', '.join(schema.required)}")
        print(f"coverage axes ({len(schema.factors)}): {', '.join(schema.factors)}  (fields with factor: true)")
        return 0

    results = audit(args.experiments_root, args.experiment)
    if not results:
        print("No recorded trials found.")
        return 0

    bad = {k: v for k, v in results.items() if v}
    if not args.quiet:
        for bag, problems in sorted(bad.items()):
            print(f"✗ {Path(bag).name}")
            for problem in problems:
                print(f"    {problem}")

    # Which fields hurt most, so the fix can be prioritised.
    tally: Dict[str, int] = {}
    for problems in bad.values():
        for problem in problems:
            tally[problem.split(":", 1)[0]] = tally.get(problem.split(":", 1)[0], 0) + 1

    print(f"\n{len(results) - len(bad)}/{len(results)} trials satisfy the contract.")
    if tally:
        print("most common gaps:")
        for name, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"   {count:4d}  {name}")
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())

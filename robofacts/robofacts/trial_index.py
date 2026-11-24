#!/usr/bin/env python3
"""Cross-experiment trial index and coverage report (phase 1).

Answers the question that used to need reading notebooks: **which combinations
do we already have bags for, and which cells are still empty?**

Where the axes come from
------------------------
Not guessed. Fields marked ``factor: true`` in the Phase 0 contract
(``trial_metadata_schema.py``) are the declared axes; anything else that
actually varies within an experiment is offered as a *discovered* axis. That
keeps the grid meaningful when a factor is renamed — the contract moves and the
grid follows — while still surfacing dimensions nobody has declared yet.

Health, not just counts
-----------------------
A coverage grid built on bad metadata looks authoritative while being wrong, so
every row carries its contract verdict. Trials that violate the schema are
counted separately and never silently inflate a cell — the audit found 63 of 179
existing trials missing ``aborted`` alone. Aborted trials are likewise counted
apart from usable ones: 22 walking-robot trials really were aborted, and a cell showing
"22 bags" that are all aborted is worse than a cell showing zero.

Usage
-----
    # what varies, per experiment
    python3 experiments/common/scripts/trial_index.py --summary

    # a coverage grid over chosen axes
    python3 .../trial_index.py --experiment my_experiment \\
        --factors detector_backend,arm_camera_mode

    # a report to drop into Obsidian
    python3 .../trial_index.py --report /tmp/coverage.md
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


from . import roots
from . import trial_decisions as td


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)

#: Never offered as a coverage axis: identifiers, prose, telemetry readings and
#: bookkeeping. Grouping by these produces one row per trial, which is a list,
#: not a grid.
NON_FACTOR_FIELDS = {
    "trial_id", "notes", "operations_log", "abort_reason", "_schema_problems",
    "bag_label", "battery_start_v", "battery_end_v", "demo_returncode",
    "record_duration_sec", "preflight",
    # Which disk the bytes went to (repo or external:<path>, written by the
    # runner since 2026-08-04). Provenance an offload needs, not an axis the
    # experiment varies: grouping results by storage medium would be noise.
    "bag_storage",
    # Which commit produced the trial (written by the runner since 2026-08-04).
    # Provenance, never an axis: grouping coverage by code version would open a
    # new level on every commit, so every cell of every campaign would go back
    # to STILL TO FLY after any change to the repo.
    "code_version",
    # The runner's --scratch marker (2026-08-12). A scratch bag should never be
    # loaded at all (bags_scratch/ is not in the allow-list below), and if one
    # is ever moved into bags/ by hand its status is forced to excluded, so
    # neither field can be an axis of anything.
    "scratch", "scratch_label",
    # Why the trial has its domain (2026-10-06): provenance of `domain`.
    "domain_basis",
}


#: An archive view (a copy of the trial records without the heavy recordings,
#: such as the home server's) puts this file in a bag directory to say that the
#: recording exists on a trial host: which host, which files, how many bytes.
#: The trial then has recorded data although no data file is in this copy.
RECORDINGS_ELSEWHERE = "recordings_elsewhere.json"


@functools.lru_cache(maxsize=64)
def _recorder_at(start: str):
    """The recorder of the project at or above ``start``, resolved once per directory:
    the index asks it of every trial."""
    from . import backend
    return backend.active(start).recorder


def recording_holds_data(directory: Path) -> bool:
    """Does this trial directory hold recorded data? The project's recorder says what a
    data file is (backend.Recorder.holds_data). The project is found from the experiments
    root, three levels above ``<root>/<experiment>/bags/<trial>``."""
    directory = Path(directory)
    start = directory.parents[2] if len(directory.parents) > 2 else directory
    return _recorder_at(str(start)).holds_data(directory)


@dataclass
class Trial:
    experiment: str
    bag_dir: Path
    metadata: Dict[str, Any]
    problems: List[str] = field(default_factory=list)
    is_issue_bag: bool = False
    #: A person's decisions beside the bag (trial_decisions.py, 2026-10-22).
    #: ``metadata`` is the recorded metadata with ``corrections`` applied on
    #: top; ``recorded`` is trial_metadata.json as the runner wrote it.
    corrections: List[Dict[str, Any]] = field(default_factory=list)
    exclusion: Optional[Dict[str, Any]] = None
    recorded: Optional[Dict[str, Any]] = None
    #: ``trials reverify`` (2026-11-04): the current verify steps run again on
    #: the bag. When it lifts the abort, ``metadata`` is the lifted copy.
    reverification: Optional[Dict[str, Any]] = None

    @property
    def reverified(self) -> bool:
        """Counts because a re-verify lifted the runner's verify-step abort."""
        return bool(self.recorded and td.reverification_lifts(self.recorded, self.reverification))

    @property
    def excluded_by_hand(self) -> bool:
        """In bags_issues*, or carries an exclusion.json: a person said it must not count."""
        return self.is_issue_bag or self.exclusion is not None

    @property
    def aborted(self) -> bool:
        return bool(self.metadata.get("aborted"))

    @property
    def has_bag_data(self) -> bool:
        """Is there actually a recording here, or only a claim of one?

        Belt to the runner's braces. A directory holding trial_metadata.json
        and nothing else is not a trial — and it is the shape a *remote* run
        used to leave behind on the laptop when the recording on the rig had
        failed, so it counted as coverage while the real bag never existed.
        """
        if not self.bag_dir.is_dir():
            return False
        if (self.bag_dir / RECORDINGS_ELSEWHERE).is_file():
            return True
        return recording_holds_data(self.bag_dir)

    @property
    def status(self) -> str:
        """One of usable / unverified / excluded.

        The three-way split matters. A trial that merely predates the metadata
        contract is *unverified*, not *bad* — collapsing it into the same bucket
        as an aborted trial made the first run of this report show "0 usable"
        for 37 perfectly good position-only flights, which is exactly the kind
        of misleading authority a coverage grid must not have.

        No bag data is a fourth case, and it is *excluded*: there is nothing to
        analyse, so counting it as coverage would leave a cell that can never
        be filled because it already looks full.
        """
        if self.aborted or self.excluded_by_hand or not self.has_bag_data:
            return "excluded"
        # A scratch flight (trial_runner --scratch) is recorded for the
        # operator's ad-hoc question and must never count as coverage. Its home
        # is bags_scratch/, which load_trials never reads. This is the second
        # lock: even a scratch bag moved into bags/ by hand stays excluded.
        if self.metadata.get("scratch"):
            return "excluded"
        if self.problems:
            return "unverified"
        return "usable"

    @property
    def healthy(self) -> bool:
        return self.status == "usable"

    def value(self, factor: str) -> str:
        value = self.metadata.get(factor)
        if value is None:
            return "—"
        if isinstance(value, (list, dict)):
            return json.dumps(value, sort_keys=True)[:40]
        return str(value)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def refuse_another_backends_stack(experiments_root: Optional[Path] = None,
                                  experiment: Optional[str] = None) -> None:
    """Raise backend.BackendError when an experiment declares the stack of another backend
    than the one in force.

    The recorder of the backend says what recorded data is. Under the wrong backend every
    recording would read as empty, and the index and the planner would report a project
    without trials instead of an error. The usual cause is a ``trials.yaml`` without its
    ``backend:`` line, so the launcher's own refusal, which names that line, is raised here."""
    import yaml

    from . import backend
    root = roots.experiments_root(experiments_root)
    names = [experiment] if experiment else sorted(p.parent.parent.name
                                                   for p in root.glob("*/config/runner.yaml"))
    launcher = backend.active(root).launcher
    for name in names:
        path = root / name / "config" / "runner.yaml"
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue   # a broken file is reported by the commands that read it
        if isinstance(raw, dict):
            launcher.parse(raw, path)


def load_trials(experiments_root: Optional[Path] = None,
                experiment: Optional[str] = None,
                validate: bool = True) -> List[Trial]:
    """Every recorded trial, with its contract verdict attached."""
    try:
        from .trial_metadata_schema import validate as validate_metadata
    except Exception:  # pragma: no cover - schema module is part of the repo
        validate_metadata = None

    # The slim directories carry a COPY of trial_metadata.json alongside the
    # slim bag, so counting them naively doubled coverage for every
    # slimmed trial — which is why they were excluded outright. They are read
    # again now, deduplicated by trial_id with the full bag winning, because
    # excluding them made every REMOTELY recorded trial invisible: the runner
    # records on the rig and syncs only the slim copy back, so a trial flown
    # from the laptop UI could never appear in the laptop's own coverage view
    # (2026-07-29, first runner flight).
    experiments = [experiment] if experiment else None
    refuse_another_backends_stack(experiments_root, experiment)
    trials: List[Trial] = []
    meta_paths = []
    # An ALLOW-LIST, deliberately, not a bags* glob: the runner's --scratch
    # diagnostic flights live in bags_scratch/ (slim copies in
    # bags_scratch_slim/) and are excluded from coverage by not being named
    # here. Their metadata also carries "scratch": true, which Trial.status
    # forces to excluded should a scratch bag ever be moved into bags/ by hand.
    for record_dir in ("bags", "bags_issues", "bags_slim", "bags_issues_slim"):
        prefix = f"{experiment}/" if experiment else "*/"
        meta_paths.extend(experiments_root.glob(f"{prefix}{record_dir}/*/trial_metadata.json"))
    seen_recordings: set = set()
    for meta_path in sorted(meta_paths, key=lambda p: ("_slim" in p.parents[1].name, str(p))):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        # A person's corrections (corrections.json, `trials amend`) apply on top
        # of what the runner wrote, which is never edited, so every reader of
        # Trial.metadata (the plan, the records, the UI) sees the corrected cell.
        corrections = td.read_corrections(meta_path.parent)
        recorded = metadata
        metadata = td.apply_corrections(metadata, corrections)
        # A re-verify (reverification.json, `trials reverify`) lifts an abort
        # that came from a verify step alone, when every current step passes.
        reverification = td.read_reverification(meta_path.parent)
        metadata = td.apply_reverification(metadata, reverification)
        # Dedupe ONLY a slim copy against its own full bag. A slim copy is
        # named after its full bag plus "_slim" (slim_bag.py), so the key is the
        # experiment plus the directory name without that suffix. Keying on
        # trial_id was wrong: the notebook-era trials all carry the generic id
        # "trial_001", so every slim copy after the first one with that id was
        # dropped although it was a different recording (65 trials invisible,
        # found 2026-10-01 by the retrospective analysis for the SAC paper).
        exp = meta_path.parents[2].name
        is_slim = meta_path.parents[1].name.endswith("_slim")
        stem = meta_path.parent.name
        if is_slim and stem.endswith("_slim"):
            stem = stem[: -len("_slim")]
        key = (exp, stem)
        if is_slim and key in seen_recordings:
            continue
        seen_recordings.add(key)
        problems: List[str] = list(metadata.get("_schema_problems") or [])
        if validate and validate_metadata is not None and not problems:
            problems = validate_metadata(metadata, exp, experiments_root=experiments_root)
        trials.append(Trial(
            experiment=exp,
            bag_dir=meta_path.parent,
            metadata=metadata,
            problems=problems,
            # startswith, not equality: bags_issues_slim must quarantine too — an
            # exact match let a usable-by-metadata bag in bags_issues_slim keep
            # counting as coverage (2026-07-31, the retune-quarantined cv trial).
            is_issue_bag=any(p.startswith("bags_issues") for p in meta_path.parts),
            corrections=corrections,
            exclusion=td.read_exclusion(meta_path.parent),
            recorded=recorded,
            reverification=reverification,
        ))
    return trials


# --------------------------------------------------------------------------- #
# Axes
# --------------------------------------------------------------------------- #
def declared_factors(experiment: str, experiments_root: Optional[Path] = None) -> List[str]:
    """Factors the Phase 0 contract declares for this experiment."""
    try:
        from .trial_metadata_schema import load_schema
        return load_schema(experiment, experiments_root).factors
    except Exception:
        return []


def discovered_factors(trials: Iterable[Trial], min_levels: int = 2,
                       max_levels: int = 12) -> List[str]:
    """Fields that actually vary, and could sensibly be a grid axis.

    ``max_levels`` filters out per-trial values (timestamps, voltages) that
    technically vary but produce a row per trial rather than a grid.
    """
    trials = list(trials)
    levels: Dict[str, set] = defaultdict(set)
    for trial in trials:
        for key, value in trial.metadata.items():
            if key in NON_FACTOR_FIELDS or key.startswith("_"):
                continue
            if isinstance(value, (list, dict)):
                continue
            levels[key].add(str(value))
    return sorted(k for k, v in levels.items() if min_levels <= len(v) <= max_levels)


def factors_for(experiment: str, trials: Iterable[Trial],
                experiments_root: Optional[Path] = None) -> List[str]:
    """Declared factors first (they are intentional), then discovered ones."""
    trials = list(trials)
    declared = [f for f in declared_factors(experiment, experiments_root)
                if any(f in t.metadata for t in trials)]
    discovered = [f for f in discovered_factors(trials) if f not in declared]
    return declared + discovered


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
@dataclass
class Cell:
    values: Tuple[str, ...]
    total: int = 0
    usable: int = 0
    unverified: int = 0
    excluded: int = 0


def coverage(trials: Iterable[Trial], factors: List[str]) -> List[Cell]:
    """Group trials by the chosen axes, splitting usable from unusable."""
    cells: Dict[Tuple[str, ...], Cell] = {}
    for trial in trials:
        key = tuple(trial.value(f) for f in factors)
        cell = cells.setdefault(key, Cell(values=key))
        cell.total += 1
        setattr(cell, trial.status, getattr(cell, trial.status) + 1)
    return sorted(cells.values(), key=lambda c: (-(c.usable + c.unverified), c.values))


def missing_combinations(cells: List[Cell], factors: List[str]) -> List[Tuple[str, ...]]:
    """Cells of the full cross product that have no trial at all.

    The levels are taken from what has been recorded, so this answers "which
    combinations of things we have actually used are still missing", not "every
    conceivable combination" — the latter needs the Phase 2 campaign spec.
    """
    if not factors:
        return []
    levels: List[List[str]] = []
    for i in range(len(factors)):
        levels.append(sorted({c.values[i] for c in cells}))
    seen = {c.values for c in cells}
    out: List[Tuple[str, ...]] = []

    def walk(prefix: Tuple[str, ...], depth: int) -> None:
        if depth == len(factors):
            if prefix not in seen:
                out.append(prefix)
            return
        for level in levels[depth]:
            walk(prefix + (level,), depth + 1)

    walk((), 0)
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_grid(cells: List[Cell], factors: List[str], missing: List[Tuple[str, ...]]) -> str:
    lines = []
    header = "| " + " | ".join(factors) + " | usable | unverified | excluded | total |"
    lines.append(header)
    lines.append("|" + "---|" * (len(factors) + 4))
    for cell in cells:
        flags = []
        if cell.usable == 0 and cell.unverified == 0:
            flags.append("**nothing usable here**")
        elif cell.usable == 0:
            flags.append("_(all unverified — metadata gap, not necessarily bad data)_")
        row = ("| " + " | ".join(cell.values) + f" | {cell.usable} | {cell.unverified} | "
               f"{cell.excluded} | {cell.total} |")
        lines.append(row + (f" {' '.join(flags)}" if flags else ""))
    if missing:
        lines.append("")
        lines.append(f"**{len(missing)} combination(s) of already-used levels have no bag at all:**")
        for values in missing[:20]:
            lines.append("- " + ", ".join(f"`{f}={v}`" for f, v in zip(factors, values)))
        if len(missing) > 20:
            lines.append(f"- … and {len(missing) - 20} more")
    return "\n".join(lines)


def render_report(trials: List[Trial], experiments_root: Path,
                  factors_override: Optional[List[str]] = None) -> str:
    by_experiment: Dict[str, List[Trial]] = defaultdict(list)
    for trial in trials:
        by_experiment[trial.experiment].append(trial)

    out = ["# Trial coverage", ""]
    counts = Counter(t.status for t in trials)
    out.append(f"**{len(trials)} recorded trials** — {counts['usable']} usable, "
               f"{counts['unverified']} unverified, {counts['excluded']} excluded.")
    out.append("")
    out.append("_**usable** = satisfies the metadata contract, not aborted, not excluded by a person "
               "(`trials exclude`, `bags_issues/`). "
               "**unverified** = the data may be fine but the metadata does not prove it (most of "
               "these simply predate the contract). **excluded** = aborted or already triaged into "
               "`bags_issues/`. They are kept apart deliberately: a cell reading 22 bags that are "
               "all aborted is worse than one reading zero._")
    out.append("")

    for experiment in sorted(by_experiment):
        subset = by_experiment[experiment]
        out.append(f"## {experiment}")
        out.append("")
        factors = factors_override or factors_for(experiment, subset, experiments_root)
        factors = [f for f in factors if any(f in t.metadata for t in subset)]
        if not factors:
            out.append(f"{len(subset)} trial(s); no varying factors found.")
            out.append("")
            continue
        used = factors[:3]
        out.append(f"{len(subset)} trial(s). Axes: {', '.join(f'`{f}`' for f in used)}"
                   + (f" _(also varying: {', '.join(factors[3:8])})_" if len(factors) > 3 else ""))
        out.append("")
        cells = coverage(subset, used)
        out.append(render_grid(cells, used, missing_combinations(cells, used)))
        out.append("")

        broken = [t for t in subset if t.problems]
        if broken:
            tally = Counter(p.split(":", 1)[0] for t in broken for p in t.problems)
            out.append(f"⚠️ {len(broken)} trial(s) violate the metadata contract — "
                       + ", ".join(f"`{k}` ×{v}" for k, v in tally.most_common(5)))
            out.append("")
        corrected = [t for t in subset if t.corrections]
        if corrected:
            out.append(f"✎ {len(corrected)} trial(s) counted with a person's correction "
                       f"(`corrections.json`, `trials amend`):")
            for t in corrected:
                out.append(f"- `{t.bag_dir.name}`: "
                           + "; ".join(td.describe_correction(e) for e in t.corrections))
            out.append("")
        excluded = [t for t in subset if t.exclusion is not None]
        if excluded:
            out.append(f"{len(excluded)} trial(s) excluded by a person (`exclusion.json`, "
                       f"`trials exclude`):")
            for t in excluded:
                out.append(f"- `{t.bag_dir.name}`: {td.describe_exclusion(t.exclusion)}")
            out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Domain and the condition-space view (2026-10-06, SAC 2027 framework paper)
# --------------------------------------------------------------------------- #
DOMAINS = ("simulation", "hardware")


class DomainResolver:
    """The domain of a trial: recorded by the runner, else derived, else unknown.

    Trials recorded before 2026-10-06 carry no ``domain``. For them it is
    DERIVED from the same two sources the runner uses (a factor level's
    ``domain:`` map in factors.yaml, then runner.yaml's ``simulation:``) and
    marked derived. An experiment with no runner.yaml, or one that does not
    declare ``simulation:``, gives ``unknown``: a guess would put a simulated
    trial in the hardware column, which is the error this view exists to show.
    """

    def __init__(self, experiments_root: Optional[Path] = None):
        self.root = roots.experiments_root(experiments_root)
        self._cache: Dict[str, Tuple[Optional[bool], Dict[str, Any]]] = {}

    def _experiment(self, experiment: str):
        if experiment not in self._cache:
            flag: Optional[bool] = None
            path = self.root / experiment / "config" / "runner.yaml"
            try:
                import yaml
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(raw.get("simulation"), bool):
                    flag = raw["simulation"]
            except Exception:  # noqa: BLE001 - no runner.yaml = unknown
                flag = None
            try:
                from . import campaign_spec as cs
                factors = cs.load_factors(experiment, self.root)
            except Exception:  # noqa: BLE001
                factors = {}
            self._cache[experiment] = (flag, factors)
        return self._cache[experiment]

    def resolve(self, trial: "Trial") -> Tuple[str, str]:
        """(domain, source) with source recorded | derived | unknown."""
        recorded = trial.metadata.get("domain")
        if recorded in DOMAINS:
            return recorded, "recorded"
        flag, factors = self._experiment(trial.experiment)
        for factor in factors.values():
            if not factor.domain or not factor.metadata_key:
                continue
            value = trial.metadata.get(factor.metadata_key)
            if value is not None and factor.canonical(value) in {str(k) for k in factor.domain}:
                level = next(k for k in factor.domain if str(k) == factor.canonical(value))
                return factor.domain[level], "derived"
        if flag is None:
            return "unknown", "unknown"
        return ("simulation" if flag else "hardware"), "derived"


def _factor_check_passed(trial: "Trial", factor: str, level: str) -> bool:
    """Did this trial's T-0 gate confirm the declared level, without an override?"""
    pre = trial.metadata.get("preflight")
    if not isinstance(pre, dict) or pre.get("overridden"):
        return False
    for item in pre.get("factor_checks") or []:
        if (item.get("factor") == factor and str(item.get("level")) == level
                and item.get("result") == "PASS"):
            return True
    return False


def _refused_attempts(experiments_root: Path, experiment: str,
                      factor: str) -> Counter:
    """Blocked attempts whose factor check FAILED, per declared level (runs/attempts.jsonl)."""
    out: Counter = Counter()
    path = Path(experiments_root) / experiment / "runs" / "attempts.jsonl"
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("status") != "blocked":
            continue
        for item in ((rec.get("gate") or {}).get("factor_checks") or []):
            if item.get("factor") == factor and item.get("result") in ("FAIL", "ERR"):
                out[str(item.get("level"))] += 1
    return out


def load_condition_space(path: Path) -> Dict[str, Any]:
    import yaml
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw.get("levels"), list) or not raw["levels"]:
        raise ValueError(f"{path}: a condition space needs a non-empty `levels` list")
    experiments = raw.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError(f"{path}: a condition space needs an `experiments` list")
    for i, entry in enumerate(experiments):
        if not isinstance(entry, dict) or not entry.get("experiment") or not entry.get("factor"):
            raise ValueError(f"{path}: experiments[{i}] needs `experiment` and `factor`")
    return raw


def condition_coverage(space: Dict[str, Any], trials: Iterable["Trial"],
                       experiments_root: Optional[Path] = None) -> Dict[str, Any]:
    """Per level of one condition, across experiments: usable trials per domain.

    A trial counts for a level when it is usable (trial_index's status), its
    experiment is listed in the space, its factor's recorded value maps onto
    the level, and, with ``require_check: true`` (the default), its T-0 gate
    confirmed that level with a PASS of the factor's check and no override.
    Usable trials that fail only the last condition are reported as
    ``unchecked`` and never counted. The status per level is both,
    simulation only, hardware only or none.
    """
    from . import campaign_spec as cs
    root = Path(experiments_root)
    levels = [str(lv) for lv in space["levels"]]
    require_check = bool(space.get("require_check", True))
    resolver = DomainResolver(root)
    rows = {lv: {"simulation": 0, "hardware": 0, "unknown_domain": 0, "unchecked": 0,
                 "refused_at_t0": 0, "by_experiment": Counter(), "derived_domain": 0}
            for lv in levels}
    by_exp: Dict[str, List[Trial]] = defaultdict(list)
    for trial in trials:
        by_exp[trial.experiment].append(trial)
    notes = []
    for entry in space["experiments"]:
        experiment, factor_name = entry["experiment"], entry["factor"]
        level_map = {str(k): str(v) for k, v in (entry.get("map") or {}).items()}
        try:
            factor = cs.load_factors(experiment, root).get(factor_name)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{experiment}: factors not loadable ({exc})")
            continue
        if factor is None:
            notes.append(f"{experiment}: declares no factor {factor_name!r} (yet)")
            continue
        key = factor.metadata_key or factor_name
        for trial in by_exp.get(experiment, []):
            if trial.status != "usable" or key not in trial.metadata:
                continue
            declared = factor.canonical(trial.metadata[key])
            level = level_map.get(declared, declared)
            if level not in rows:
                continue
            if require_check and not _factor_check_passed(trial, factor_name, declared):
                rows[level]["unchecked"] += 1
                continue
            domain, source = resolver.resolve(trial)
            if domain == "unknown":
                rows[level]["unknown_domain"] += 1
                continue
            rows[level][domain] += 1
            rows[level]["by_experiment"][f"{experiment} ({domain})"] += 1
            if source == "derived":
                rows[level]["derived_domain"] += 1
        for declared, n in _refused_attempts(root, experiment, factor_name).items():
            level = level_map.get(declared, declared)
            if level in rows:
                rows[level]["refused_at_t0"] += n
    out_rows = []
    for lv in levels:
        r = rows[lv]
        status = ("both" if r["simulation"] and r["hardware"] else
                  "simulation only" if r["simulation"] else
                  "hardware only" if r["hardware"] else "none")
        out_rows.append({"level": lv, "simulation": r["simulation"], "hardware": r["hardware"],
                         "status": status, "unchecked": r["unchecked"],
                         "unknown_domain": r["unknown_domain"],
                         "refused_at_t0": r["refused_at_t0"],
                         "derived_domain": r["derived_domain"],
                         "by_experiment": dict(sorted(r["by_experiment"].items()))})
    return {"name": space.get("name", ""), "require_check": require_check,
            "experiments": [e["experiment"] for e in space["experiments"]],
            "levels": out_rows, "notes": notes}


def render_condition_coverage(cov: Dict[str, Any]) -> str:
    lines = [f"# Condition coverage: {cov['name']}", "",
             f"Experiments: {', '.join(cov['experiments'])}. "
             + ("Counted: usable trials whose T-0 gate CONFIRMED the declared level "
                "(factor check PASS, no override)." if cov["require_check"] else
                "Counted: usable trials (the factor check is not required)."), "",
             "| level | simulation | hardware | status | unchecked (not counted) | refused at T-0 |",
             "|---|---:|---:|---|---:|---:|"]
    for r in cov["levels"]:
        lines.append(f"| {r['level']} | {r['simulation']} | {r['hardware']} | {r['status']} "
                     f"| {r['unchecked']} | {r['refused_at_t0']} |")
    extra = [r for r in cov["levels"] if r["unknown_domain"] or r["derived_domain"]]
    if extra:
        lines.append("")
        for r in extra:
            lines.append(f"- {r['level']}: {r['unknown_domain']} usable trial(s) of unknown "
                         f"domain (not counted), {r['derived_domain']} counted with a derived "
                         f"domain (recorded before the runner wrote `domain`).")
    for note in cov["notes"]:
        lines.append(f"- note: {note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    roots.add_root_arguments(ap)
    ap.add_argument("--experiment", help="restrict to one experiment")
    ap.add_argument("--factors", help="comma-separated axes (default: from the contract, then discovered)")
    ap.add_argument("--report", type=Path, help="write a markdown report here")
    ap.add_argument("--summary", action="store_true", help="just list what varies per experiment")
    ap.add_argument("--no-validate", action="store_true", help="skip contract validation (faster)")
    ap.add_argument("--condition-space", type=Path, metavar="YAML",
                    help="coverage of ONE condition across experiments, per level and domain "
                         "(simulation, hardware), e.g. experiments/common/config/"
                         "condition_spaces/lighting.yaml")
    ap.add_argument("--json", action="store_true", help="with --condition-space: print JSON")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    if args.condition_space:
        space = load_condition_space(args.condition_space)
        trials = []
        for entry in space["experiments"]:
            trials += load_trials(args.experiments_root, entry["experiment"],
                                  validate=not args.no_validate)
        cov = condition_coverage(space, trials, args.experiments_root)
        text = json.dumps(cov, indent=2) if args.json else render_condition_coverage(cov)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(text + "\n", encoding="utf-8")
            print(f"Wrote {args.report}")
        else:
            print(text)
        return 0

    trials = load_trials(args.experiments_root, args.experiment, validate=not args.no_validate)
    if not trials:
        print("No recorded trials found.")
        return 0

    if args.summary:
        by_experiment: Dict[str, List[Trial]] = defaultdict(list)
        for trial in trials:
            by_experiment[trial.experiment].append(trial)
        for experiment in sorted(by_experiment):
            subset = by_experiment[experiment]
            usable = sum(1 for t in subset if t.healthy)
            factors = factors_for(experiment, subset, args.experiments_root)
            declared = set(declared_factors(experiment, args.experiments_root))
            print(f"\n{experiment}  ({len(subset)} trials, {usable} usable)")
            for factor in factors[:10]:
                levels = sorted({t.value(factor) for t in subset})
                mark = "contract" if factor in declared else "discovered"
                print(f"   {factor:34s} [{mark:10s}] {len(levels):2d} level(s): "
                      f"{', '.join(levels[:5])}{' …' if len(levels) > 5 else ''}")
        return 0

    factors_override = [f.strip() for f in args.factors.split(",")] if args.factors else None
    report = render_report(trials, args.experiments_root, factors_override)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {args.report}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

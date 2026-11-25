"""A person's decision about a recorded trial, kept beside the runner's files and never inside them (2026-10-22).

The runner writes ``trial_metadata.json`` once, from the plan and the checks of
that moment. Two things can still be wrong afterwards, and only a person can
know them:

* the trial must not count (the network stalled, the operator walked the wrong
  route, the estimator diverged): ``trials exclude`` moves the bag and its slim
  copy into ``bags_issues*`` and writes ``exclusion.json`` beside each, with a
  category, a reason, who and when;
* a recorded factor level is wrong (operator levels are not checked at T-0, so
  the recorded value follows the plan): ``trials amend`` writes
  ``corrections.json`` beside the bag and its slim copy, one entry per change
  with the recorded value, the corrected one, who, when and why.

Neither command edits ``trial_metadata.json``. The coverage index
(``trial_index.load_trials``) reads the corrected values on top of the recorded
ones and treats an ``exclusion.json`` as excluded, so the plan, ``trials
records`` and the web UI follow without a second reading of the files.
``trials restore`` moves an excluded trial back and keeps its old
``exclusion.json`` as ``exclusion_restored_<time>.json``, so the history stays.

Before 2026-10-22 this was a hand ``mv`` and a hand edit of the metadata, which
left no reason for the index to show and destroyed the recorded value.

    trials exclude <exp> <trial> --reason "..." --category infrastructure [--by NAME] [--evidence PATH]
    trials restore <exp> <trial> [--reason "..."] [--by NAME]
    trials amend   <exp> <trial> route=building_loop --reason "..." [--by NAME]
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import roots

EXCLUSION_FILE = "exclusion.json"
CORRECTIONS_FILE = "corrections.json"
#: ``trials reverify`` (trial_reverify.py, 2026-11-04): the current verify steps run again.
REVERIFICATION_FILE = "reverification.json"
#: How the runner's ``abort_reason`` starts when a verify step failed after an action that
#: returned 0 (trial_runner: an action failure is written first, as "action returned rc=").
VERIFY_ABORT_PREFIX = "verify step "
#: What kind of cause excluded a trial, so a reader can count them.
CATEGORIES = ("infrastructure", "operator", "estimator", "other")
#: Where a bag lives when it counts, and where it lives when excluded.
def included_dirs(root: Optional[Path] = None) -> Tuple[str, str]:
    """The folders that count (the recordings and their slim copies), named by the backend."""
    from . import backend
    rd = backend.record_dirs(root)
    return rd["recordings"], rd["slim"]


def excluded_dirs(root: Optional[Path] = None) -> Tuple[str, str]:
    """The folders of excluded trials, in the order of included_dirs."""
    from . import backend
    rd = backend.record_dirs(root)
    return rd["issues"], rd["issues_slim"]


def __getattr__(name):
    """``INCLUDED_DIRS`` and ``EXCLUDED_DIRS``, the constants from before the backends named
    the folders, resolved for the project in the working directory."""
    if name == "INCLUDED_DIRS":
        return included_dirs()
    if name == "EXCLUDED_DIRS":
        return excluded_dirs()
    raise AttributeError(name)
#: A bag with a file younger than this may still be written (the recorder, the
#: metadata, the slim copy): refuse to move or amend it.
WRITE_QUIET_S = 120
#: Who decided, when ``--by`` is not given.
ENV_BY = "TRIALS_BY"


class DecisionError(Exception):
    """A decision that cannot be recorded, with the reason in the message."""


# --------------------------------------------------------------------------- #
# Reading (used by trial_index for every trial)
# --------------------------------------------------------------------------- #
def read_exclusion(bag_dir: Path) -> Optional[Dict[str, Any]]:
    """The ``exclusion.json`` beside a bag, or None. An unreadable file still excludes."""
    path = Path(bag_dir) / EXCLUSION_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"category": "other", "reason": f"{EXCLUSION_FILE} is unreadable ({exc})"}
    return data if isinstance(data, dict) else {"category": "other",
                                                "reason": f"{EXCLUSION_FILE} is not an object"}


def read_corrections(bag_dir: Path) -> List[Dict[str, Any]]:
    """The entries of the ``corrections.json`` beside a bag, oldest first ([] without one)."""
    path = Path(bag_dir) / CORRECTIONS_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("corrections") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and e.get("field")
            and "corrected" in e]


def read_reverification(bag_dir: Path) -> Optional[Dict[str, Any]]:
    """The ``reverification.json`` beside a bag, or None (also when it is unreadable)."""
    path = Path(bag_dir) / REVERIFICATION_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def abort_is_verify_only(abort_reason: Any) -> bool:
    """Did the runner abort the trial for a failed verify step and nothing else?"""
    return isinstance(abort_reason, str) and abort_reason.startswith(VERIFY_ABORT_PREFIX)


def reverification_lifts(metadata: Dict[str, Any], record: Optional[Dict[str, Any]]) -> bool:
    """A re-verify lifts the abort when every current step passed, the recorded abort came
    from a verify step alone, and the record was written against THIS abort_reason."""
    return bool(record and record.get("verify_ok") is True and record.get("lifts_abort") is True
                and metadata.get("aborted")
                and abort_is_verify_only(metadata.get("abort_reason"))
                and record.get("recorded_abort_reason") == metadata.get("abort_reason"))


def apply_reverification(metadata: Dict[str, Any], record: Optional[Dict[str, Any]]
                         ) -> Dict[str, Any]:
    """The metadata as re-verified: a copy without the abort when the record lifts it."""
    if not reverification_lifts(metadata, record):
        return metadata
    out = {k: v for k, v in metadata.items() if k != "abort_reason"}
    out["aborted"] = False
    return out


def describe_reverification(record: Dict[str, Any]) -> str:
    """``re-verified 2026-11-04 by lori: 3 of 3 steps pass, the abort is lifted (reason)``."""
    steps = record.get("steps") or []
    passed = sum(1 for s in steps if s.get("rc") == 0)
    outcome = ("the abort is lifted" if record.get("lifts_abort") else
               "no change to its status" if record.get("verify_ok") else "it fails today")
    reason = str(record.get("reason") or "").strip()
    text = (f"re-verified {str(record.get('date') or '')[:10]} by {record.get('by') or '?'}: "
            f"{passed} of {len(steps)} steps pass, {outcome}")
    return f"{text} ({reason})" if reason else text


def apply_corrections(metadata: Dict[str, Any], corrections: Sequence[Dict[str, Any]]
                      ) -> Dict[str, Any]:
    """The metadata as corrected: a copy, with the newest correction of each field on top."""
    if not corrections:
        return metadata
    out = dict(metadata)
    for entry in corrections:
        out[str(entry["field"])] = entry["corrected"]
        # The runner's summary of the plan follows the corrected level too.
        factor, nondefault = entry.get("factor"), out.get("non_default_factors")
        if factor and "default" in entry and isinstance(nondefault, list):
            rest = [f for f in nondefault if f != factor]
            out["non_default_factors"] = rest + ([factor] if str(entry["corrected"]) != str(entry["default"]) else [])
    return out


def describe_exclusion(exclusion: Dict[str, Any]) -> str:
    """``infrastructure: Wi-Fi stall (Alejandro, 2026-10-22)``: the one line a list shows."""
    who = ", ".join(str(x) for x in (exclusion.get("by"), str(exclusion.get("date") or "")[:10]) if x)
    text = f"{exclusion.get('category') or 'other'}: {exclusion.get('reason') or 'no reason given'}"
    return f"{text} ({who})" if who else text


def describe_correction(entry: Dict[str, Any]) -> str:
    """``route: block_loop -> building_loop (reason)``."""
    reason = str(entry.get("reason") or "").strip()
    text = f"{entry.get('field')}: {entry.get('recorded')} -> {entry.get('corrected')}"
    return f"{text} ({reason})" if reason else text


# --------------------------------------------------------------------------- #
# Finding a trial
# --------------------------------------------------------------------------- #
@dataclass
class TrialDirs:
    """Where one trial's full bag and slim copy are on this machine."""
    name: str
    full: Optional[Path]
    slim: Optional[Path]

    @property
    def dirs(self) -> List[Path]:
        return [d for d in (self.full, self.slim) if d is not None]

    @property
    def excluded(self) -> bool:
        return any(d.parent.name in excluded_dirs(d.parents[2]) for d in self.dirs)

    @property
    def metadata_dir(self) -> Path:
        """The directory whose ``trial_metadata.json`` is the record: the full bag's, else the slim copy's."""
        return self.full if self.full is not None else self.slim  # type: ignore[return-value]


def locate(exp_dir: Path, trial: str) -> TrialDirs:
    """The bag and slim directories of ``trial`` (its bag directory name, ``_slim`` optional)."""
    name = trial[:-len("_slim")] if trial.endswith("_slim") else trial
    if not name or "/" in name or name in (".", ".."):
        raise DecisionError(f"{trial!r} is not a trial name (the bag directory name, such as "
                            f"20261022T140606_rep01)")
    (recordings, slim_dir), (issues, issues_slim) = included_dirs(exp_dir.parent), excluded_dirs(exp_dir.parent)
    full = [exp_dir / d / name for d in (recordings, issues) if (exp_dir / d / name).is_dir()]
    slim = [exp_dir / d / n for d in (slim_dir, issues_slim)
            for n in (f"{name}_slim", name) if (exp_dir / d / n).is_dir()]
    if len(full) > 1 or len(slim) > 1:
        raise DecisionError(f"{name} is in more than one place ({', '.join(str(p.relative_to(exp_dir)) for p in full + slim)}). "
                            f"Resolve that by hand first: one copy must go.")
    if not full and not slim:
        raise DecisionError(f"no trial {name!r} in {exp_dir} (looked in {recordings}, {issues}, "
                            f"{slim_dir}, {issues_slim}). `trials records "
                            f"{exp_dir.name}` lists the trial names.")
    dirs = TrialDirs(name=name, full=full[0] if full else None, slim=slim[0] if slim else None)
    if len({d.parent.name in excluded_dirs(exp_dir.parent) for d in dirs.dirs}) > 1:
        raise DecisionError(f"{name}: the bag and its slim copy disagree "
                            f"({', '.join(str(p.relative_to(exp_dir)) for p in dirs.dirs)}). "
                            f"Move one by hand so both are included or both excluded.")
    return dirs


def _newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for p in path.rglob("*"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def check_not_being_written(root: Path, experiment: str, dirs: TrialDirs, *,
                            now: Optional[float] = None) -> None:
    """Refuse a trial the runner may still be writing: no metadata yet, a fresh file,
    or a bag created during the run that the run marker announces."""
    now = time.time() if now is None else now
    if not (dirs.metadata_dir / "trial_metadata.json").is_file():
        raise DecisionError(f"{dirs.name} has no trial_metadata.json: the runner has not "
                            f"finished it (or it failed before the metadata, and the index "
                            f"never reads it). Nothing to decide yet.")
    for d in dirs.dirs:
        age = now - _newest_mtime(d)
        if age < WRITE_QUIET_S:
            raise DecisionError(f"{d} changed {age:.0f} s ago: the runner may still be writing "
                                f"it. Wait {WRITE_QUIET_S} s after the trial ends.")
    from . import run_marker
    marker = run_marker.read(root, now=now)
    if marker and marker.get("experiment") == experiment:
        try:
            started = datetime.fromisoformat(str(marker.get("started"))).timestamp()
        except ValueError:
            started = 0.0
        created = min(d.stat().st_mtime for d in dirs.dirs)
        if marker.get("label") == dirs.name or created >= started:
            raise DecisionError(f"a run of {experiment} is in progress on "
                                f"{marker.get('host', '?')} (pid {marker.get('pid', '?')}, since "
                                f"{marker.get('started', '?')}) and {dirs.name} belongs to it. "
                                f"Decide after the run ends.")


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _now_iso(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now,
                                  timezone.utc).astimezone().isoformat(timespec="seconds")


def default_by() -> str:
    return os.environ.get(ENV_BY) or getpass.getuser()


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _tool(command: str) -> str:
    from . import __version__
    return f"trials {command} (robofacts {__version__})"


def _rel(path: Path, exp_dir: Path) -> str:
    try:
        return str(path.resolve().relative_to(exp_dir.resolve()))
    except ValueError:
        return str(path.resolve())


def _resolve_evidence(paths: Sequence[str], exp_dir: Path, dirs: TrialDirs) -> List[Path]:
    out = []
    for raw in paths:
        candidates = [Path(raw), exp_dir / raw] + [d / raw for d in dirs.dirs]
        found = next((p.resolve() for p in candidates if p.exists()), None)
        if found is None:
            raise DecisionError(f"evidence {raw!r} not found (looked relative to the working "
                                f"directory, the experiment and the trial's directories)")
        out.append(found)
    return out


def exclude(experiments_root: Path, experiment: str, trial: str, *, reason: str, category: str,
            by: Optional[str] = None, evidence: Sequence[str] = (),
            now: Optional[float] = None) -> Dict[str, Any]:
    """Move a trial out of coverage, with its reason. Returns the record written.

    A trial already in ``bags_issues*`` without an ``exclusion.json`` (a hand
    quarantine from before this command) is not moved: it only gets its reason.
    """
    root = Path(experiments_root)
    exp_dir = root / experiment
    reason = (reason or "").strip()
    if not reason:
        raise DecisionError("an exclusion needs a reason (--reason): the index shows it "
                            "wherever the trial is listed")
    if category not in CATEGORIES:
        raise DecisionError(f"category {category!r} is not one of {', '.join(CATEGORIES)}")
    dirs = locate(exp_dir, trial)
    existing = [d for d in dirs.dirs if (d / EXCLUSION_FILE).is_file()]
    if existing:
        old = read_exclusion(existing[0]) or {}
        raise DecisionError(f"{dirs.name} is already excluded ({describe_exclusion(old)}). "
                            f"To change the reason, `trials restore` it and exclude it again.")
    if not dirs.excluded:
        check_not_being_written(root, experiment, dirs, now=now)
    evidence_paths = _resolve_evidence(evidence, exp_dir, dirs)

    moves: List[Tuple[Path, Path]] = []
    if not dirs.excluded:
        for d in dirs.dirs:
            target_root = exp_dir / excluded_dirs(exp_dir.parent)[included_dirs(exp_dir.parent).index(d.parent.name)]
            target = target_root / d.name
            if target.exists():
                raise DecisionError(f"{_rel(target, exp_dir)} already exists: resolve that by hand")
            moves.append((d, target))
    for src, dst in moves:
        dst.parent.mkdir(exist_ok=True)
        src.rename(dst)
    moved = {src.resolve(): dst for src, dst in moves}

    def after_move(p: Path) -> Path:
        for src, dst in moved.items():
            if p == src or src in p.parents:
                return dst / p.relative_to(src)
        return p

    record = {
        "decision": "excluded",
        "trial": dirs.name,
        "date": _now_iso(now),
        "by": by or default_by(),
        "category": category,
        "reason": reason,
        "evidence": [_rel(after_move(p), exp_dir) for p in evidence_paths],
        "moved": [f"{_rel(s, exp_dir)} -> {_rel(d, exp_dir)}" for s, d in moves],
        "tool": _tool("exclude"),
    }
    for d in [after_move(d.resolve()) for d in dirs.dirs]:
        _write_json(d / EXCLUSION_FILE, record)
    return record


def restore(experiments_root: Path, experiment: str, trial: str, *, by: Optional[str] = None,
            reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """Move an excluded trial back into coverage; its ``exclusion.json`` is kept as history."""
    exp_dir = Path(experiments_root) / experiment
    dirs = locate(exp_dir, trial)
    if not dirs.excluded:
        raise DecisionError(f"{dirs.name} is not excluded (it is in "
                            f"{', '.join(d.parent.name for d in dirs.dirs)})")
    stamp = datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y%m%dT%H%M%S")
    history_name = f"exclusion_restored_{stamp}.json"
    moves = []
    for d in dirs.dirs:
        target = exp_dir / included_dirs(exp_dir.parent)[excluded_dirs(exp_dir.parent).index(d.parent.name)] / d.name
        if target.exists():
            raise DecisionError(f"{_rel(target, exp_dir)} already exists: resolve that by hand")
        moves.append((d, target))
    restored = {"date": _now_iso(now), "by": by or default_by(),
                "reason": (reason or "").strip(), "tool": _tool("restore")}
    previous: Optional[Dict[str, Any]] = None
    for src, dst in moves:
        dst.parent.mkdir(exist_ok=True)
        src.rename(dst)
        old = dst / EXCLUSION_FILE
        if old.is_file():
            data = read_exclusion(dst) or {}
            previous = previous or data
            _write_json(dst / history_name, {**data, "restored": restored})
            old.unlink()
    return {"trial": dirs.name, "restored": restored, "previous": previous,
            "history": history_name if previous is not None else None,
            "moved": [f"{_rel(s, exp_dir)} -> {_rel(d, exp_dir)}" for s, d in moves]}


def _parse_assignment(text: str) -> Tuple[str, str]:
    if "=" not in text:
        raise DecisionError(f"{text!r} is not <factor>=<level>")
    name, level = text.split("=", 1)
    if not name.strip() or not level.strip():
        raise DecisionError(f"{text!r} is not <factor>=<level>")
    return name.strip(), level.strip()


def amend(experiments_root: Path, experiment: str, trial: str, assignments: Sequence[str], *,
          reason: str, by: Optional[str] = None, now: Optional[float] = None
          ) -> List[Dict[str, Any]]:
    """Correct recorded factor levels in ``corrections.json``. Returns the new entries."""
    from . import campaign_spec as cs
    root = Path(experiments_root)
    exp_dir = root / experiment
    reason = (reason or "").strip()
    if not reason:
        raise DecisionError("a correction needs a reason (--reason): how do you know the "
                            "recorded value is wrong")
    if not assignments:
        raise DecisionError("name at least one <factor>=<level>")
    try:
        factors = cs.load_factors(experiment, root)
    except cs.SpecError as exc:
        raise DecisionError(f"the factors of {experiment} do not load: {exc}") from exc
    by_key = {f.metadata_key: f for f in factors.values() if f.metadata_key}

    dirs = locate(exp_dir, trial)
    check_not_being_written(root, experiment, dirs, now=now)
    try:
        recorded = json.loads((dirs.metadata_dir / "trial_metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DecisionError(f"{dirs.name}: trial_metadata.json is unreadable ({exc})") from exc
    entries = max((read_corrections(d) for d in dirs.dirs), key=len)
    current = apply_corrections(recorded, entries)

    date = _now_iso(now)
    new: List[Dict[str, Any]] = []
    for text in assignments:
        name, level_text = _parse_assignment(text)
        factor = factors.get(name) or by_key.get(name)
        if factor is None:
            raise DecisionError(f"{experiment} declares no factor {name!r} (its factors: "
                                f"{', '.join(sorted(factors))})")
        if not factor.metadata_key:
            raise DecisionError(f"factor {factor.name!r} has no metadata_key, so no trial "
                                f"records it and there is nothing to correct")
        level_text = factor.canonical(level_text)
        level = next((lv for lv in factor.levels if str(lv) == level_text), None)
        if level is None:
            raise DecisionError(f"{level_text!r} is not a level of {factor.name} "
                                f"({', '.join(str(lv) for lv in factor.levels)})")
        key = factor.metadata_key
        if str(current.get(key)) == str(level):
            raise DecisionError(f"{dirs.name} already has {key}={level}"
                                f"{' (corrected before)' if key in {e['field'] for e in entries} else ''}")
        entry = {"field": key, "factor": factor.name, "recorded": recorded.get(key),
                 "corrected": level, "default": factor.default, "date": date,
                 "by": by or default_by(), "reason": reason}
        if key in current and current.get(key) != recorded.get(key):
            entry["replaces"] = current.get(key)
        new.append(entry)
        current[key] = level

    data = {"trial": dirs.name,
            "note": "Corrections by a person, applied on top of trial_metadata.json, which "
                    "stays as the runner wrote it. The newest entry of a field wins.",
            "tool": _tool("amend"),
            "corrections": entries + new}
    for d in dirs.dirs:
        _write_json(d / CORRECTIONS_FILE, data)
    return new


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _common(ap: argparse.ArgumentParser) -> None:
    roots.add_root_arguments(ap)
    ap.add_argument("experiment")
    ap.add_argument("trial", help="the trial's bag directory name (as `trials records` lists it)")


def _run(fn, args) -> int:
    try:
        return fn(args)
    except DecisionError as exc:
        print(f"trials: {exc}", file=sys.stderr)
        return 1


def main_exclude(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="trials exclude",
        description="move a trial out of coverage (bags -> bags_issues, its slim copy too) and "
                    "write exclusion.json beside each, with the reason; nothing is deleted")
    _common(ap)
    ap.add_argument("--reason", required=True, help="why it must not count, one or two sentences")
    ap.add_argument("--category", required=True, choices=CATEGORIES,
                    help="infrastructure (network, power, disk), operator (the trial was not "
                         "run as planned), estimator (the method under test failed), other")
    ap.add_argument("--by", help=f"who decided (default: ${ENV_BY}, else the login name)")
    ap.add_argument("--evidence", action="append", default=[], metavar="PATH",
                    help="a file that shows it (a plot, a log), repeatable")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    def go(a):
        record = exclude(a.experiments_root, a.experiment, a.trial, reason=a.reason,
                         category=a.category, by=a.by, evidence=a.evidence)
        for line in record["moved"] or ["already in bags_issues: only the reason was added"]:
            print(f"  {line}")
        print(f"excluded {record['trial']}: {describe_exclusion(record)}")
        return 0
    return _run(go, args)


def main_restore(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="trials restore",
        description="move an excluded trial back into coverage (bags_issues -> bags); its "
                    "exclusion.json stays beside it as exclusion_restored_<time>.json")
    _common(ap)
    ap.add_argument("--reason", default="", help="why it counts again")
    ap.add_argument("--by", help=f"who decided (default: ${ENV_BY}, else the login name)")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    def go(a):
        result = restore(a.experiments_root, a.experiment, a.trial, by=a.by, reason=a.reason)
        for line in result["moved"]:
            print(f"  {line}")
        if result["history"]:
            print(f"  kept the old reason as {result['history']}")
        print(f"restored {result['trial']}: it counts again if its metadata proves its cell")
        return 0
    return _run(go, args)


def main_amend(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="trials amend",
        description="correct a recorded factor level: corrections.json beside the bag and its "
                    "slim copy, read on top of trial_metadata.json, which is never edited")
    _common(ap)
    ap.add_argument("assignments", nargs="+", metavar="FACTOR=LEVEL",
                    help="the true level, which must be one the experiment declares")
    ap.add_argument("--reason", required=True, help="how you know the recorded value is wrong")
    ap.add_argument("--by", help=f"who decided (default: ${ENV_BY}, else the login name)")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    def go(a):
        for entry in amend(a.experiments_root, a.experiment, a.trial, a.assignments,
                           reason=a.reason, by=a.by):
            print(f"corrected {a.trial}: {describe_correction(entry)}")
        return 0
    return _run(go, args)

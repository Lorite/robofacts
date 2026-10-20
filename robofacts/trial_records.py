#!/usr/bin/env python3
"""Every trial of an experiment as one record: the join behind ``trials records`` and the web UI (#131).

The coverage index (``trial_index``) answers "which bags count". The operator who
just ran a trial and the analyst who reads a cohort also ask "where is everything
about this trial", and the answer is spread over six places on disk:

* the bag and its ``trial_metadata.json`` (``bags/``, ``bags_issues/``),
* the slim copy to replay (``bags_slim/``, ``bags_issues_slim/``),
* the campaign that ran it and the gate's verdict (``runs/attempts.jsonl``, the
  attempt ledger, and ``runs/preflight_gate.jsonl``),
* the run log with the verify steps' lines (``runs/<bag>.log``),
* the analyses that included it (``results/*/analysis_metadata.json``, whose
  ``bags`` list names the exact bag set) and the videos inside those folders,
* the experiment's Foxglove layout (``config/foxglove*.json``).

This module joins them once. ``trials records <exp>`` prints the result, and the
web UI renders the same records, so the UI never computes an answer the command
line cannot give (the rule of the whole package).

Scratch recordings (``bags_scratch/``) are records too, with ``kind == "scratch"``
and the class excluded: listed with their slim copies so a diagnostic trial can be
replayed, and never coverage. Blocked attempts (a NO-GO gate records no bag) are records with status
``blocked``, so "what happened to the trial I just tried to run" has an answer.

    trials records <exp>                                  every trial, newest first
    trials records <exp> --campaign <c> --status usable   one campaign's usable trials
    trials records <exp> --where noise_level=high --json  one cell, as JSON
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import roots
from . import trial_index as ti


#: The three classes of a trial, the words of the SAC 2027 paper (Equation 4 there,
#: ``trial_index.Trial.status`` here): only a usable trial counts as coverage.
CLASSES = ("usable", "unverified", "excluded")

#: Every status a record can have, in the order filters list them: the class of a
#: usable or unverified trial; the reason of an excluded one (the paper's aborted,
#: quarantined or no data, plus scratch for a scratch recording); and the outcome
#: of an *attempt* that left no trial behind (blocked by the gate, failed before
#: its metadata, interrupted), from the attempt ledger.
STATUSES = ("usable", "unverified", "aborted", "quarantined", "no-data", "scratch",
            "blocked", "failed", "interrupted")

#: The class each status belongs to; None for an attempt that left no trial.
CLASS_OF = {"usable": "usable", "unverified": "unverified", "aborted": "excluded",
            "quarantined": "excluded", "no-data": "excluded", "scratch": "excluded",
            "blocked": None, "failed": None, "interrupted": None}

#: What each class and status means, one sentence each, shared by the CLI and the UI.
STATUS_MEANING = {
    "usable": "counts as coverage: the metadata proves the cell",
    "unverified": "a bag exists but its metadata cannot prove the cell (breaks the contract)",
    "excluded": "never counts: aborted, quarantined, no data, or a scratch recording",
    "aborted": "excluded: the trial did not finish (aborted: true)",
    "quarantined": "excluded: moved to bags_issues by hand",
    "no-data": "excluded: the directory holds no .mcap or .db3",
    "scratch": "excluded: a scratch recording (--scratch), a diagnostic, replayable, never coverage",
    "blocked": "an attempt the gate refused (NO-GO): no trial, nothing recorded",
    "failed": "an attempt that failed before a trial_metadata.json was written: no trial",
    "interrupted": "an attempt the operator interrupted before it finished: no trial",
}

RECORD_DIRS = ("bags", "bags_issues", "bags_slim", "bags_issues_slim")
SCRATCH_DIRS = ("bags_scratch", "bags_scratch_slim")
_STAMP = re.compile(r"^(\d{8}T\d{6})")


@dataclass
class TrialRecord:
    """One trial (or one attempt that left none), with everything that belongs to it."""
    experiment: str
    #: The bag directory's name, which is the runner's trial label: the key of
    #: the record everywhere (ledger, run log, results subfolders, URLs).
    name: str
    kind: str = "campaign"                  # campaign | scratch
    status: str = "usable"
    reason: str = ""
    started: str = ""                       # ISO time, from the ledger or the name
    campaign: Optional[str] = None          # the campaign that ran it (ledger)
    counts_for: List[str] = field(default_factory=list)   # campaigns whose plan counts it
    values: Dict[str, str] = field(default_factory=dict)  # declared factor values
    repetition: Optional[int] = None
    bag_dir: Optional[Path] = None          # the full recording, when on this machine
    slim_dir: Optional[Path] = None         # the slim copy, when one exists
    metadata_path: Optional[Path] = None
    run_log: Optional[Path] = None
    gate: Optional[Dict[str, Any]] = None   # {verdict, mode, failed, n_checks, overridden}
    analyses: List[Dict[str, Any]] = field(default_factory=list)
    videos: List[Path] = field(default_factory=list)
    code_version: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def slim_name(self) -> Optional[str]:
        """The name the public bag route serves the slim copy under."""
        return self.slim_dir.name if self.slim_dir is not None else None

    @property
    def counts(self) -> bool:
        return self.status == "usable" and self.kind == "campaign"

    @property
    def trial_class(self) -> Optional[str]:
        """usable, unverified or excluded; None for an attempt that left no trial."""
        return CLASS_OF.get(self.status)

    def cell(self, factors: Optional[Iterable[str]] = None) -> str:
        """``k=v`` pairs of the factor values, the CLI's ``--where`` spelling."""
        keys = list(factors) if factors is not None else sorted(self.values)
        return " ".join(f"{k}={self.values[k]}" for k in keys if k in self.values)

    def to_dict(self, root: Optional[Path] = None) -> Dict[str, Any]:
        def rel(p: Optional[Path]) -> Optional[str]:
            if p is None:
                return None
            if root is not None:
                try:
                    return str(p.relative_to(root))
                except ValueError:
                    pass
            return str(p)
        return {
            "experiment": self.experiment, "name": self.name, "kind": self.kind,
            "status": self.status, "class": self.trial_class, "reason": self.reason,
            "started": self.started,
            "campaign": self.campaign, "counts_for": list(self.counts_for),
            "values": dict(self.values), "repetition": self.repetition,
            "bag_dir": rel(self.bag_dir), "slim_dir": rel(self.slim_dir),
            "slim_name": self.slim_name, "metadata": rel(self.metadata_path),
            "run_log": rel(self.run_log), "gate": self.gate,
            "analyses": [{**a, "path": rel(a["path"]), "folder": rel(a["folder"]),
                          "videos": [rel(v) for v in a.get("videos", [])]}
                         for a in self.analyses],
            "videos": [rel(v) for v in self.videos],
            # Where the trial card's still lives (made on first view, or by
            # `trials records --posters`); null without a video.
            "poster": rel(self.poster(root)) if root is not None else None,
        }

    def poster(self, experiments_root: Optional[Path]) -> Optional[Path]:
        """The cache path of the first video's poster frame (it may not exist yet), or None."""
        if not self.videos or experiments_root is None:
            return None
        try:
            return poster_path(Path(experiments_root) / self.experiment, self.videos[0])
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Ledgers
# --------------------------------------------------------------------------- #
def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def attempts(experiments_root: Path, experiment: str) -> List[Dict[str, Any]]:
    """The attempt ledger of the experiment (``runs/attempts.jsonl``), oldest first."""
    return _read_jsonl(Path(experiments_root) / experiment / "runs" / "attempts.jsonl")


def gate_log(experiments_root: Path, experiment: str) -> List[Dict[str, Any]]:
    """Every T-0 gate verdict of the experiment (``runs/preflight_gate.jsonl``), oldest first."""
    return _read_jsonl(Path(experiments_root) / experiment / "runs" / "preflight_gate.jsonl")


def _failed_names(failed: Any) -> List[str]:
    """Failed checks as short strings, from either the ledger's or the gate log's shape."""
    out = []
    for item in failed or []:
        if isinstance(item, dict):
            name = item.get("check") or "?"
            detail = item.get("detail") or ""
            out.append(f"{name}: {detail}" if detail else str(name))
        else:
            out.append(str(item))
    return out


def _gate_summary(source: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(source, dict):
        return None
    verdict = source.get("verdict")
    if verdict is None and "status" in source:
        verdict = "GO" if source.get("status") == "pass" else "NO-GO"
    if verdict is None and "result" in source:
        # The web UI's shell gate before #131 stamped {"gate", "result"} only.
        verdict = "GO" if source.get("result") == "pass" else "NO-GO"
    return {
        "verdict": verdict or "?",
        "mode": source.get("mode") or "",
        "failed": _failed_names(source.get("failed_checks") or source.get("failed")),
        "n_checks": source.get("n_checks"),
        "overridden": bool(source.get("overridden") or source.get("override")),
    }


@lru_cache(maxsize=4096)
def _verify_lines_cached(path: str, mtime: float, size: int) -> Tuple[Tuple[str, Optional[int], str], ...]:
    out = []
    pattern = re.compile(r"^\[verify\] (\S+) rc=(-?\d+)(?: in ([\d.]+) s)?")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.startswith("[verify]"):
                    continue
                match = pattern.match(line)
                if match:
                    out.append((match.group(1), int(match.group(2)), match.group(3) or ""))
    except OSError:
        pass
    return tuple(out)


def verify_report(run_log: Optional[Path]) -> List[Dict[str, Any]]:
    """The verify steps of one trial, from the ``[verify] <name> rc=<n>`` lines of its run log.

    The runner prints one such line per step (``trial_runner._run_trial``) and
    writes no other record of them, so the run log is the report. An empty list
    means no step ran (none declared, a scratch recording, or no run log).
    """
    if run_log is None or not run_log.is_file():
        return []
    stat = run_log.stat()
    return [{"name": n, "rc": rc, "seconds": s, "ok": rc == 0}
            for n, rc, s in _verify_lines_cached(str(run_log), stat.st_mtime, stat.st_size)]


# --------------------------------------------------------------------------- #
# Results: analyses and videos
# --------------------------------------------------------------------------- #
def _strip_stamp(folder_name: str) -> str:
    match = _STAMP.match(folder_name)
    return folder_name[len(match.group(1)) + 1:] if match else folder_name


def result_folders(experiments_root: Path, experiment: str) -> List[Dict[str, Any]]:
    """Every folder under ``results/``, newest first, with what its metadata says.

    ``analysis`` and ``bags`` come from ``analysis_metadata.json`` when the folder
    has one (``trials analyze --execute`` writes it); a folder made by hand has
    neither, and its analysis is read from the name (``<stamp>_<analysis>``).
    """
    results = Path(experiments_root) / experiment / "results"
    out: List[Dict[str, Any]] = []
    if not results.is_dir():
        return out
    for folder in sorted((p for p in results.iterdir() if p.is_dir()), reverse=True):
        meta_path = folder / "analysis_metadata.json"
        meta: Dict[str, Any] = {}
        if meta_path.is_file():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                meta = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                meta = {}
        out.append({
            "folder": folder,
            "name": folder.name,
            "analysis": meta.get("analysis") or _strip_stamp(folder.name),
            "generated_at": meta.get("generated_at") or (_STAMP.match(folder.name).group(1)
                                                         if _STAMP.match(folder.name) else ""),
            "bags": [str(b) for b in meta.get("bags") or []],
            "where": (meta.get("selection") or {}).get("where") or {},
            "failed_commands": meta.get("failed_commands"),
            "has_metadata": bool(meta),
            "metadata_path": meta_path if meta else None,
        })
    return out


def _videos_in(path: Path) -> List[Path]:
    """The .mp4 files of a trial's own result folder, H.264 first (it plays in a browser)."""
    if not path.is_dir():
        return []
    found = sorted(path.glob("*.mp4")) + sorted(path.glob("*/*.mp4"))
    return sorted(found, key=lambda p: (0 if "h264" in p.name else 1, str(p)))


# --------------------------------------------------------------------------- #
# Poster frames: the still a trial card shows before its video plays (#137)
# --------------------------------------------------------------------------- #
#: Where the poster frames live, inside the experiment: ``runs/`` is git-ignored
#: and served read-only by the UI's files route, so no new route and no new root.
POSTER_DIR = Path("runs") / "posters"
#: The poster is the frame this many seconds in: the first frames of a recorded
#: trial are usually a black or a still scene. A shorter video falls back to 0 s.
POSTER_AT_S = 3.0
POSTER_WIDTH = 480


def poster_path(exp_dir: Path, video: Path) -> Path:
    """Where the poster of ``video`` is cached: ``runs/posters/<video's path>.jpg``.

    The path mirrors the video's own path inside the experiment, so two videos with
    the same file name in different result folders never share a poster.
    """
    rel = Path(video).resolve().relative_to(Path(exp_dir).resolve())
    return Path(exp_dir) / POSTER_DIR / (rel.as_posix() + ".jpg")


def video_of_poster(exp_dir: Path, poster: Path) -> Optional[Path]:
    """The video a poster path stands for (the inverse of :func:`poster_path`), or None."""
    try:
        rel = Path(poster).relative_to(Path(exp_dir) / POSTER_DIR)
    except ValueError:
        return None
    if not rel.name.endswith(".mp4.jpg"):
        return None
    return Path(exp_dir) / rel.parent / rel.name[:-len(".jpg")]


def make_poster(exp_dir: Path, video: Path, *, at: float = POSTER_AT_S,
                ffmpeg: str = "ffmpeg", timeout: float = 30.0) -> Optional[Path]:
    """The poster frame of ``video``, made once with ffmpeg and cached; None when it cannot be made.

    A poster older than its video is made again (the video was re-rendered). The
    frame is written to a temporary file and renamed, so two requests for the same
    poster never serve a half-written JPEG. No ffmpeg, a broken video, or a timeout
    return None, and the caller shows its placeholder instead.
    """
    import os
    import subprocess
    import tempfile
    video = Path(video)
    if not video.is_file():
        return None
    out = poster_path(exp_dir, video)
    if out.is_file() and out.stat().st_size > 0 and out.stat().st_mtime >= video.stat().st_mtime:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".jpg", dir=out.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        for start in ((at, 0.0) if at > 0 else (0.0,)):
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:g}",
                   "-i", str(video), "-frames:v", "1",
                   "-vf", f"scale='min({POSTER_WIDTH},iw)':-2", "-q:v", "5", str(tmp)]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired):
                return None
            # A seek past the end exits 0 and writes nothing: try the first frame.
            if tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, out)
                return out
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _associate_results(folders: List[Dict[str, Any]], names: Iterable[str]
                       ) -> Dict[str, List[Dict[str, Any]]]:
    """Which result folders include which trial, by name.

    Three ways, most explicit first: the folder's ``analysis_metadata.json``
    lists the bag; the folder has a subfolder named after the bag (a per-bag
    analysis); or the folder is named after the bag (``<bag>_<what>``, or the bag's
    name alone, the shapes of a video made by hand for one scratch recording).
    """
    known = set(names)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for f in folders:
        folder: Path = f["folder"]
        hits: Dict[str, Tuple[Path, str]] = {}
        for bag in f["bags"]:
            if bag in known:
                sub = folder / bag
                hits[bag] = (sub if sub.is_dir() else folder, "listed")
        if not f["has_metadata"]:
            for sub in folder.iterdir():
                if sub.is_dir() and sub.name in known and sub.name not in hits:
                    hits[sub.name] = (sub, "subfolder")
            for bag in known:
                if bag not in hits and (folder.name == bag or folder.name.startswith(bag + "_")):
                    hits[bag] = (folder, "named")
        cohort = len(f["bags"]) > 1 or (not f["has_metadata"] and len(hits) > 1)
        for bag, (path, how) in hits.items():
            analysis = f["analysis"]
            if how == "named":
                # results/<bag>_<what>/ names its analysis; results/<bag>/ does not.
                analysis = folder.name[len(bag) + 1:] or ("video" if _videos_in(path) else "result")
            out.setdefault(bag, []).append({
                "analysis": analysis, "folder": folder, "path": path,
                "per": "cohort" if cohort and path == folder else "bag",
                "n_bags": len(f["bags"]) or len(hits),
                "generated_at": f["generated_at"], "failed_commands": f["failed_commands"],
                # A cohort folder's videos are not this trial's video.
                "videos": [] if (cohort and path == folder) else _videos_in(path),
            })
    return out


# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #
def foxglove_layouts(experiments_root: Path) -> Dict[str, List[Path]]:
    """The Foxglove layouts each experiment ships: ``experiments/<exp>/config/foxglove*.json``.

    A convention, not a schema: any JSON exported from Foxglove (Layouts -> Export)
    whose name starts with ``foxglove`` (case does not matter) belongs to the bags
    of its experiment.
    """
    out: Dict[str, List[Path]] = {}
    for f in sorted(Path(experiments_root).glob("*/config/*.json")):
        if f.name.lower().startswith("foxglove"):
            out.setdefault(f.parents[1].name, []).append(f)
    return out


# --------------------------------------------------------------------------- #
# The join
# --------------------------------------------------------------------------- #
def _started(name: str, ledger_row: Optional[Dict[str, Any]]) -> str:
    if ledger_row and ledger_row.get("ts_start"):
        return str(ledger_row["ts_start"])[:19]
    match = _STAMP.match(name)
    if match:
        s = match.group(1)
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}"
    return ""


def _slim_for(exp_dir: Path, name: str, roots_: Iterable[str]) -> Optional[Path]:
    for root_name in roots_:
        candidate = exp_dir / root_name / f"{name}_slim"
        if candidate.is_dir() and any(candidate.glob("*.mcap")):
            return candidate
    return None


def _status_of(trial: ti.Trial) -> Tuple[str, str]:
    if trial.status in ("usable", "unverified"):
        return trial.status, "; ".join(trial.problems)
    if trial.aborted:
        reason = trial.metadata.get("abort_reason") or trial.metadata.get("notes") or "marked aborted"
        return "aborted", str(reason)
    if trial.is_issue_bag:
        return "quarantined", "in bags_issues: excluded from coverage by hand, not deleted"
    if not trial.has_bag_data:
        return "no-data", "no .mcap or .db3 in the bag directory: nothing to analyse"
    return "scratch", "carries scratch: true, so it never counts"


def _values(metadata: Dict[str, Any], factors: List[str]) -> Dict[str, str]:
    out = {}
    for f in factors:
        value = metadata.get(f)
        if value is None or isinstance(value, (dict, list)):
            continue
        out[f] = str(value)
    return out


def load_records(experiments_root: Optional[Path] = None, experiment: str = "", *,
                 include_scratch: bool = True, include_attempts: bool = True,
                 campaigns: Optional[Dict[str, Any]] = None) -> List[TrialRecord]:
    """Every trial of one experiment, newest first, joined with everything that belongs to it.

    ``campaigns`` maps a campaign name to its loaded ``campaign_spec.Campaign``;
    when given, each record's ``counts_for`` lists the campaigns whose plan
    (``campaign_plan.build_plan``) counts it, the same matching the coverage
    numbers use. Without it the field stays empty and nothing is loaded twice.
    """
    root = roots.experiments_root(experiments_root)
    exp_dir = root / experiment
    trials = ti.load_trials(root, experiment)
    try:
        factors = ti.declared_factors(experiment, root)
    except Exception:  # noqa: BLE001 - an experiment without a contract still lists its trials
        factors = []
    ledger = attempts(root, experiment)
    by_label = {row.get("trial_label"): row for row in ledger if row.get("trial_label")}
    gates = {row.get("trial_label"): row for row in gate_log(root, experiment)
             if row.get("trial_label")}

    records: List[TrialRecord] = []
    seen = set()
    for trial in trials:
        in_slim = trial.bag_dir.parent.name.endswith("_slim")
        name = trial.bag_dir.name[:-len("_slim")] if in_slim and trial.bag_dir.name.endswith("_slim") \
            else trial.bag_dir.name
        if name in seen:
            continue
        seen.add(name)
        row = by_label.get(name)
        status, reason = _status_of(trial)
        slim = trial.bag_dir if in_slim else _slim_for(
            exp_dir, name, ("bags_issues_slim", "bags_slim") if trial.is_issue_bag
            else ("bags_slim", "bags_issues_slim"))
        log = exp_dir / "runs" / f"{name}.log"
        records.append(TrialRecord(
            experiment=experiment, name=name, kind="campaign", status=status, reason=reason,
            started=_started(name, row),
            campaign=(row or {}).get("campaign") or trial.metadata.get("campaign"),
            values=_values(trial.metadata, factors or sorted(
                k for k in trial.metadata if k not in ti.NON_FACTOR_FIELDS)),
            repetition=trial.metadata.get("repetition"),
            bag_dir=None if in_slim else trial.bag_dir,
            slim_dir=slim,
            metadata_path=trial.bag_dir / "trial_metadata.json",
            run_log=log if log.is_file() else None,
            gate=_gate_summary(trial.metadata.get("preflight")) or _gate_summary(
                (row or {}).get("gate")) or _gate_summary(gates.get(name)),
            code_version=trial.metadata.get("code_version"),
            metadata=trial.metadata,
        ))

    if include_scratch:
        records.extend(_scratch_records(root, experiment, factors, by_label, gates, seen))

    if include_attempts:
        # Attempts that left no trial: a NO-GO records nothing, a failure before
        # the metadata leaves a directory the index cannot read.
        for row in ledger:
            label = row.get("trial_label")
            if not label or label in seen:
                continue
            seen.add(label)
            status = str(row.get("status") or "failed")
            if status not in ("blocked", "failed", "interrupted"):
                status = "failed"
            scratch = bool(row.get("scratch")) or row.get("campaign") == "scratch"
            gate = _gate_summary(row.get("gate")) or _gate_summary(gates.get(label))
            reason = str(row.get("abort_reason") or row.get("detail") or "").strip()
            if row.get("status") == "ran":
                reason = "the ledger says it ran, but its bag is not on this machine"
            log = exp_dir / "runs" / f"{label}.log"
            records.append(TrialRecord(
                experiment=experiment, name=label, kind="scratch" if scratch else "campaign",
                status=status, reason=reason, started=_started(label, row),
                campaign=None if scratch else row.get("campaign"),
                values={k: str(v) for k, v in (row.get("values") or {}).items()
                        if not isinstance(v, (dict, list))},
                repetition=row.get("repetition"),
                run_log=log if log.is_file() else None, gate=gate,
            ))

    by_name = {r.name: r for r in records}
    for name, entries in _associate_results(result_folders(root, experiment), by_name).items():
        record = by_name[name]
        record.analyses = entries
        record.videos = [v for e in entries for v in e["videos"]]

    if campaigns:
        from . import campaign_plan as cp
        for cname, campaign in sorted(campaigns.items()):
            plan = cp.build_plan(campaign, trials, include_unverified=True)
            for cell in plan.cells:
                for bag in cell.matched_bags:
                    stem = bag[:-len("_slim")] if bag.endswith("_slim") else bag
                    record = by_name.get(stem)
                    if record is not None and cname not in record.counts_for:
                        record.counts_for.append(cname)

    records.sort(key=lambda r: (r.started, r.name), reverse=True)
    return records


def _scratch_records(root: Path, experiment: str, factors: List[str],
                     by_label: Dict[str, Dict[str, Any]], gates: Dict[str, Dict[str, Any]],
                     seen: set) -> List[TrialRecord]:
    """Scratch recordings with their slim copies: replayable, never coverage."""
    exp_dir = root / experiment
    names: Dict[str, Dict[str, Optional[Path]]] = {}
    for dir_name in SCRATCH_DIRS:
        base = exp_dir / dir_name
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            if d.name.endswith("_slim"):
                if any(d.glob("*.mcap")):
                    names.setdefault(d.name[:-len("_slim")], {})["slim"] = d
            elif (d / "trial_metadata.json").is_file() or any(d.glob("*.mcap")):
                names.setdefault(d.name, {})["full"] = d
    out = []
    for name, found in names.items():
        if name in seen:
            continue
        seen.add(name)
        source = found.get("full") or found.get("slim")
        meta_path = source / "trial_metadata.json" if source is not None else None
        metadata: Dict[str, Any] = {}
        if meta_path is not None and meta_path.is_file():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                metadata = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                metadata = {}
        else:
            meta_path = None
        row = by_label.get(name)
        aborted = bool(metadata.get("aborted"))
        log = exp_dir / "runs" / f"{name}.log"
        out.append(TrialRecord(
            experiment=experiment, name=name, kind="scratch",
            status="aborted" if aborted else "scratch",
            reason=str(metadata.get("abort_reason") or "") if aborted else "",
            started=_started(name, row),
            values=_values(metadata, factors),
            bag_dir=found.get("full"), slim_dir=found.get("slim"),
            metadata_path=meta_path, run_log=log if log.is_file() else None,
            gate=_gate_summary(metadata.get("preflight")) or _gate_summary(
                (row or {}).get("gate")) or _gate_summary(gates.get(name)),
            code_version=metadata.get("code_version"), metadata=metadata,
        ))
    return out


#: What ``--status`` accepts beyond the statuses themselves.
STATUS_FILTERS = STATUSES + ("excluded", "attempt", "excluded-any")


def _status_matches(r: "TrialRecord", status: str) -> bool:
    if status == "excluded-any":
        return r.status not in ("usable", "unverified")
    if status == "attempt":
        return r.trial_class is None
    if status in CLASSES:
        return r.trial_class == status
    return r.status == status


def filter_records(records: Iterable[TrialRecord], *, campaign: Optional[str] = None,
                   status: Optional[str] = None, where: Optional[Dict[str, str]] = None,
                   kind: Optional[str] = None) -> List[TrialRecord]:
    """The records that match every given filter (the CLI flags and the UI's filter bar).

    ``campaign`` matches the campaign that ran a trial OR a campaign whose plan
    counts it, because a trial run by an older campaign still covers a newer
    one's cell. ``status`` is one of STATUSES, or a class (``excluded`` is every
    excluded trial, whatever the reason), or ``attempt`` (every attempt that left
    no trial), or ``excluded-any`` (everything that does not count, attempts
    included). ``where`` is ``analysis_runner``'s spelling: factor=value pairs.
    """
    out = []
    for r in records:
        if kind and r.kind != kind:
            continue
        if campaign and r.campaign != campaign and campaign not in r.counts_for:
            continue
        if status and not _status_matches(r, status):
            continue
        if where and any(r.values.get(k) != str(v) for k, v in where.items()):
            continue
        out.append(r)
    return out


def campaign_gate_outcomes(experiments_root: Path, experiment: str,
                           campaign: str) -> Dict[str, Any]:
    """The gate's verdicts for one campaign: counts, the newest rows, the checks that failed most.

    Read from ``runs/preflight_gate.jsonl``, which the gate writes on every run
    (a GO and a NO-GO alike), so a campaign page can say "the gate refused 3 of
    the last 10 attempts, every time on watchdog_health" without a log dive.
    """
    rows = [r for r in gate_log(experiments_root, experiment) if r.get("campaign") == campaign]
    counts: Dict[str, int] = {}
    failing: Dict[str, int] = {}
    for r in rows:
        verdict = str(r.get("verdict") or "?")
        counts[verdict] = counts.get(verdict, 0) + 1
        for item in r.get("failed") or []:
            check = item.get("check") if isinstance(item, dict) else str(item).split(":")[0]
            failing[str(check)] = failing.get(str(check), 0) + 1
    recent = [{"ts": str(r.get("ts", ""))[:19], "trial_label": r.get("trial_label"),
               "verdict": r.get("verdict"), "mode": r.get("mode"),
               "failed": _failed_names(r.get("failed")), "host": r.get("host"),
               "overridden": bool(r.get("override"))}
              for r in reversed(rows[-10:])]
    blocked = [r for r in attempts(experiments_root, experiment)
               if r.get("campaign") == campaign and r.get("status") == "blocked"]
    return {"total": len(rows), "counts": counts,
            "failing": sorted(failing.items(), key=lambda kv: -kv[1]),
            "recent": recent, "blocked_attempts": len(blocked)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_campaigns(root: Path, experiment: str) -> Dict[str, Any]:
    from . import campaign_spec as cs
    out = {}
    for path in sorted((root / experiment / "campaigns").glob("*.yaml")):
        try:
            out[path.stem] = cs.load_campaign(path, root)
        except Exception:  # noqa: BLE001 - one broken campaign must not hide the trials
            continue
    return out


def class_label(r: "TrialRecord") -> str:
    """``usable``, ``excluded (aborted)``, or ``attempt: blocked``: the paper's words."""
    if r.trial_class is None:
        return f"attempt: {r.status}"
    if r.trial_class == "excluded":
        return f"excluded ({r.status.replace('-', ' ')})"
    return r.trial_class


def summary_line(records: List["TrialRecord"]) -> str:
    """``32 record(s): 24 usable, 5 excluded (2 aborted, 3 scratch), 3 attempt(s) ...``"""
    by_status: Dict[str, int] = {}
    for r in records:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    parts = [f"{by_status[c]} {c}" for c in ("usable", "unverified") if c in by_status]
    excluded = [(s, by_status[s]) for s in STATUSES if CLASS_OF[s] == "excluded" and s in by_status]
    if excluded:
        parts.append(f"{sum(n for _, n in excluded)} excluded ("
                     + ", ".join(f"{n} {s.replace('-', ' ')}" for s, n in excluded) + ")")
    attempts = [(s, by_status[s]) for s in STATUSES if CLASS_OF[s] is None and s in by_status]
    if attempts:
        parts.append(f"{sum(n for _, n in attempts)} attempt(s) without a trial ("
                     + ", ".join(f"{n} {s}" for s, n in attempts) + ")")
    return f"{len(records)} record(s): " + ", ".join(parts)


def render(records: List[TrialRecord], factors: Optional[List[str]] = None) -> str:
    if not records:
        return "no trials match"
    width = max(len(class_label(r)) for r in records)
    lines = []
    for r in records:
        bits = [r.started.replace("T", " ") or "-", f"{class_label(r):<{width}}", r.name]
        if r.kind == "scratch" and r.status != "scratch":
            bits.append("[scratch recording, never coverage]")
        if r.campaign:
            bits.append(f"campaign={r.campaign}")
        if r.slim_name:
            bits.append(f"slim={r.slim_name}")
        if r.videos:
            bits.append(f"video={len(r.videos)}")
        if r.analyses:
            bits.append("analyses=" + ",".join(sorted({a["analysis"] for a in r.analyses})))
        if r.gate:
            bits.append(f"gate={r.gate['verdict']}")
        lines.append("  ".join(bits))
        if r.reason and r.status != "usable":
            lines.append(f"      why: {r.reason.splitlines()[0][:160]}")
    lines.append(summary_line(records))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="trials records",
        description="every trial of an experiment, joined with its campaign, class, gate "
                    "verdict, slim bag, run log, analyses and videos (newest first)")
    roots.add_root_arguments(ap)
    ap.add_argument("experiment")
    ap.add_argument("--campaign", help="flown by, or counted by, this campaign")
    ap.add_argument("--status", choices=STATUS_FILTERS,
                    help="a class (usable, unverified, excluded), the reason of an excluded "
                         "trial (aborted, quarantined, no-data, scratch), an attempt outcome "
                         "(blocked, failed, interrupted), attempt for every attempt that left "
                         "no trial, or excluded-any for everything that does not count")
    ap.add_argument("--where", action="append", metavar="FACTOR=VALUE",
                    help="one cell (repeatable), the spelling of `trials analyze --where`")
    ap.add_argument("--no-scratch", action="store_true", help="leave out scratch recordings")
    ap.add_argument("--scratch-only", action="store_true", help="only scratch recordings")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    ap.add_argument("--posters", action="store_true",
                    help="also make the poster frames the UI's card view shows, for the "
                         "records listed (ffmpeg, cached under runs/posters/)")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)
    root = roots.experiments_root(args.experiments_root)
    if not (root / args.experiment).is_dir():
        print(f"trials records: no experiment {args.experiment!r} under {root}", file=sys.stderr)
        return 2
    from .analysis_runner import AnalysisError, parse_where
    try:
        where = parse_where(args.where)
    except AnalysisError as exc:
        print(f"trials records: {exc}", file=sys.stderr)
        return 2
    records = load_records(root, args.experiment, include_scratch=not args.no_scratch,
                           campaigns=_load_campaigns(root, args.experiment))
    records = filter_records(records, campaign=args.campaign, status=args.status, where=where,
                             kind="scratch" if args.scratch_only else None)
    if args.posters:
        made = [r for r in records
                if r.videos and make_poster(root / args.experiment, r.videos[0]) is not None]
        with_video = sum(1 for r in records if r.videos)
        print(f"posters: {len(made)} of {with_video} trial(s) with a video under "
              f"{root / args.experiment / POSTER_DIR}", file=sys.stderr)
    if args.json:
        print(json.dumps([r.to_dict(root) for r in records], indent=2))
    else:
        print(render(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())

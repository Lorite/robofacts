#!/usr/bin/env python3
"""Keep a slim, transferable copy of every recorded bag (#94).

``slim_bag.py`` slims one bag on demand. This is the layer above it: a stable
place for slim copies, a post-record hook so one is made automatically, and a
backfill mode for bags recorded before any of this existed.

Where slim bags go
------------------
A sibling directory, **not** inside ``bags/``::

    experiments/<name>/bags/         2026-07-01T15-24-39_flight/         (17 GB, stays on the rig)
    experiments/<name>/bags_slim/    2026-07-01T15-24-39_flight_slim/    (MBs, synced to the laptop)

This placement is deliberate. ``bag_selection_utils.is_complete_bag_dir`` calls
anything with a ``metadata.yaml`` and an ``*.mcap`` a bag, so a slim copy left
inside ``bags/`` would be picked up by ``select_bag_dir`` as if it were its own
trial, silently corrupting trial selection and the analysis manifests. Any
``<dir>`` maps to ``<dir>_slim``, so ``bags_issues/`` -> ``bags_issues_slim/``
(the incident bags are the ones you most want to be able to open quickly).

Sidecars (``trial_metadata.json``, ``rosout.txt``, ...) are copied next to the
slim bag, so the slim copy is self-describing and analysis can run against it
without the heavy original. A ``slim_provenance.json`` records where it came
from, the host, and the size reduction.

Usage
-----
    # backfill, one bag at a time (largest first) - start here to sanity-check
    python3 experiments/common/scripts/slim_bag_archive.py --limit 1

    # see what would happen, touch nothing
    python3 experiments/common/scripts/slim_bag_archive.py --list

    # everything not already archived
    python3 experiments/common/scripts/slim_bag_archive.py --all

    # one specific bag
    python3 experiments/common/scripts/slim_bag_archive.py --bag experiments/<x>/bags/<trial>

Run it inside the dev container: it shells out to ``ros2 bag convert``.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional


from . import roots
from .record_profiles import HEAVY_TOPIC_PATTERNS, is_heavy_topic  # noqa: E402
from .slim_bag import bag_topics, dir_size_bytes, human, slim  # noqa: E402

SLIM_SUFFIX = "_slim"
# commanded_waypoints.json (2026-08-13) is the flight script's own record of
# the commanded reference. It must survive into the slim copy: the heavy bag
# is what gets offloaded, and the slim tree is what the laptop and the
# analyses keep.
SIDECAR_NAMES = ("trial_metadata.json", "rosout.txt", "analysis_metadata.json",
                 "commanded_waypoints.json")
PROVENANCE_NAME = "slim_provenance.json"
RECORD_DIR_NAMES = ("bags", "bags_issues")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def slim_root_for(bag_dir: Path) -> Path:
    """``.../bags/<trial>`` -> ``.../bags_slim`` (``bags_issues`` -> ``bags_issues_slim``)."""
    record_dir = bag_dir.parent
    return record_dir.parent / f"{record_dir.name}{SLIM_SUFFIX}"


def slim_path_for(bag_dir: Path) -> Path:
    return slim_root_for(bag_dir) / f"{bag_dir.name}{SLIM_SUFFIX}"


def is_bag_dir(path: Path) -> bool:
    return path.is_dir() and (path / "metadata.yaml").exists()


def find_bag_dirs(experiments_root: Path, experiment: Optional[str] = None,
                  record_dirs: Optional[tuple] = None) -> List[Path]:
    """Every recorded bag under experiments/*/<record_dirs>/, newest-name last.

    ``record_dirs`` defaults to RECORD_DIR_NAMES, the tiers a bag can be GATED
    into. Callers with a different notion of "worth handling" pass their own:
    offload_bags.py includes ``bags_scratch`` because a scratch trial is still
    data on a disk that is about to be left behind, while slimming one would be
    pointless - it never counts toward coverage, so there is nothing to keep
    visible to trial_index.
    """
    found: List[Path] = []
    for exp_dir in sorted(experiments_root.iterdir()):
        if not exp_dir.is_dir() or (experiment and exp_dir.name != experiment):
            continue
        for record_name in (record_dirs or RECORD_DIR_NAMES):
            record_dir = exp_dir / record_name
            if not record_dir.is_dir():
                continue
            found.extend(sorted(p for p in record_dir.iterdir() if is_bag_dir(p)))
    return found


# --------------------------------------------------------------------------- #
# Archiving one bag
# --------------------------------------------------------------------------- #
def _has_heavy_topics(bag_dir: Path) -> bool:
    try:
        return any(is_heavy_topic(t, HEAVY_TOPIC_PATTERNS) for t in bag_topics(bag_dir))
    except Exception:
        return False


def _copy_sidecars(bag_dir: Path, out_dir: Path) -> List[str]:
    copied = []
    for name in SIDECAR_NAMES:
        src = bag_dir / name
        if src.is_file():
            shutil.copy2(src, out_dir / name)
            copied.append(name)
    return copied


def archive_bag(bag_dir: Path, *, force: bool = False, dry_run: bool = False,
                compress: bool = True, copy_if_light: bool = False) -> dict:
    """Slim one bag into its ``*_slim`` sibling directory.

    Returns a result dict with ``status`` in {archived, skipped, failed} and a
    ``reason`` when skipped. Never raises for the ordinary skip cases, so a
    backfill over hundreds of bags does not abort on the first oddity.
    """
    bag_dir = bag_dir.resolve()
    out_dir = slim_path_for(bag_dir)
    result = {"bag": str(bag_dir), "slim": str(out_dir), "status": "skipped", "reason": ""}

    if not is_bag_dir(bag_dir):
        result["reason"] = "not a bag directory (no metadata.yaml)"
        return result

    if out_dir.exists() and not force:
        result["reason"] = "already archived"
        return result

    # A bag with no heavy topics would just be duplicated at ~1x. On the machine
    # that holds the original that is pure waste, and it confuses "which one is
    # real" -- so locally we skip. But when the bag is on a *rig*, bags_slim is
    # not a space optimisation, it is the copy that crosses the wire; skipping
    # it there means the laptop silently receives nothing. Hence copy_if_light.
    if not _has_heavy_topics(bag_dir) and not force:
        if not copy_if_light:
            result["reason"] = "no heavy topics to drop (already light)"
            return result
        if dry_run:
            result["status"] = "would-copy"
            return result
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bag_dir, out_dir)
        result["status"] = "copied"
        result["reason"] = "already light - copied whole so it can be transferred"
        result["source_bytes"] = result["slim_bytes"] = dir_size_bytes(bag_dir)
        return result

    before = dir_size_bytes(bag_dir)
    result["source_bytes"] = before

    if dry_run:
        result["status"] = "would-archive"
        return result

    if out_dir.exists() and force:
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    produced = slim(bag_dir, out_dir=out_dir, compress=compress)
    if produced is None:
        result["status"] = "failed"
        result["reason"] = "ros2 bag convert failed"
        return result

    after = dir_size_bytes(out_dir)
    sidecars = _copy_sidecars(bag_dir, out_dir)
    (out_dir / PROVENANCE_NAME).write_text(json.dumps({
        "source_bag": str(bag_dir),
        "source_host": socket.gethostname(),
        "source_bytes": before,
        "slim_bytes": after,
        "reduction_x": round(before / after, 1) if after else None,
        "sidecars_copied": sidecars,
        "generated_by": "experiments/common/scripts/slim_bag_archive.py",
    }, indent=2), encoding="utf-8")

    result.update(status="archived", slim_bytes=after,
                  reduction_x=round(before / after, 1) if after else None,
                  sidecars=sidecars)
    return result


def archive_all(bag_dirs: Iterable[Path], *, limit: Optional[int] = None,
                force: bool = False, dry_run: bool = False,
                compress: bool = True, largest_first: bool = True) -> List[dict]:
    """Archive bags, biggest win first, stopping after ``limit`` NEW archives.

    ``limit`` counts bags actually archived, not bags inspected, so
    ``--limit 1`` reliably does exactly one new bag however many are already
    done.
    """
    candidates = list(bag_dirs)
    if largest_first:
        candidates.sort(key=lambda p: dir_size_bytes(p) if p.is_dir() else 0, reverse=True)

    results: List[dict] = []
    archived = 0
    for bag_dir in candidates:
        if limit is not None and archived >= limit:
            break
        res = archive_bag(bag_dir, force=force, dry_run=dry_run, compress=compress)
        results.append(res)
        if res["status"] in ("archived", "would-archive", "copied", "would-copy"):
            archived += 1
    return results


# --------------------------------------------------------------------------- #
# Post-record hook (used by the run notebooks)
# --------------------------------------------------------------------------- #
def archive_and_sync(bag_dir: Path, remote_target=None, *, project_root: Optional[Path] = None,
                     compress: bool = True) -> Optional[Path]:
    """Slim a just-recorded bag and bring the slim copy to this machine.

    When the trial ran on a remote rig, the slimming happens **there** (local
    disk, no network) and only the slim result crosses the wire — which is the
    whole point: minutes of rsync become seconds.

    Returns the local slim bag path, or None on failure. Never raises: a
    post-record convenience must not be able to lose a finished trial.
    """
    bag_dir = Path(bag_dir)
    out_dir = slim_path_for(bag_dir)
    try:
        if remote_target is None:
            res = archive_bag(bag_dir, compress=compress)
            print(f"[slim] {res['status']}: {res.get('reason') or out_dir}")
            return out_dir if res["status"] in ("archived", "copied") else None

        from .remote_exec_utils import rsync_back, run_foreground

        from .remote_exec_utils import package_module_command

        root = Path(project_root or remote_target.workspace)
        # The project's shim when the rig's checkout has one, else the installed package
        # (2026-10-14: before, a project without the lab's shims could not slim remotely).
        args = ["--bag", str(bag_dir), "--copy-if-light"]
        if not compress:
            args.append("--no-compress")
        cmd = package_module_command(root, "slim_bag_archive", args)
        print(f"[slim] slimming on {remote_target.host} (nothing transferred yet)")
        completed = run_foreground(cmd, remote_target, cwd=root, check=False)
        if completed.returncode != 0:
            print(f"[slim] remote slimming failed (rc={completed.returncode})")
            return None

        print(f"[slim] pulling {slim_root_for(bag_dir)} back to this machine")
        for path, code in rsync_back(remote_target, [slim_root_for(bag_dir)]).items():
            print(f"[slim]   rsync {path}: rc={code}")
            if code != 0:
                return None
        return out_dir
    except Exception as exc:  # noqa: BLE001 - never break a finished trial
        print(f"[slim] post-record archiving error (trial is safe, bag untouched): {exc}")
        return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_table(results: List[dict]) -> None:
    archived = [r for r in results if r["status"] in
                ("archived", "would-archive", "copied", "would-copy")]
    total_before = sum(r.get("source_bytes", 0) for r in archived)
    total_after = sum(r.get("slim_bytes", 0) for r in archived)
    for r in results:
        name = Path(r["bag"]).name
        if r["status"] == "archived":
            print(f"  ✓ {name}\n      {human(r['source_bytes'])} -> {human(r['slim_bytes'])}"
                  f"  ({r['reduction_x']}x)  sidecars={r.get('sidecars') or 'none'}")
        elif r["status"] == "would-archive":
            print(f"  · {name}  ({human(r.get('source_bytes', 0))}) would be archived")
        elif r["status"] == "failed":
            print(f"  ✗ {name}: {r['reason']}")
        else:
            print(f"  - {name}: {r['reason']}")
    if archived and total_after:
        print(f"\n  total: {human(total_before)} -> {human(total_after)} "
              f"({total_before / total_after:.0f}x smaller)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    roots.add_root_arguments(ap)
    ap.add_argument("--experiment", help="restrict to one experiment directory name")
    ap.add_argument("--bag", type=Path, help="archive exactly this bag directory")
    ap.add_argument("--limit", type=int, help="stop after N newly archived bags")
    ap.add_argument("--all", action="store_true", help="archive every not-yet-archived bag")
    ap.add_argument("--list", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--force", action="store_true", help="re-archive bags already done")
    ap.add_argument("--no-compress", action="store_true", help="skip zstd compression")
    ap.add_argument("--copy-if-light", action="store_true",
                    help="copy a bag that has no heavy topics instead of skipping it "
                         "(used for remote trials, where bags_slim is what comes back)")
    args = ap.parse_args(argv)
    if not args.bag:
        roots.resolve_root_arguments(args)

    compress = not args.no_compress
    if args.bag:
        results = [archive_bag(args.bag, force=args.force, dry_run=args.list,
                               compress=compress, copy_if_light=args.copy_if_light)]
    else:
        if not (args.all or args.limit or args.list):
            ap.error("choose one of --bag, --limit N, --all, or --list")
        bags = find_bag_dirs(args.experiments_root, args.experiment)
        if not bags:
            print(f"No bags found under {args.experiments_root}")
            return 0
        print(f"Found {len(bags)} recorded bag(s); largest first.\n")
        results = archive_all(bags, limit=args.limit, force=args.force,
                              dry_run=args.list, compress=compress)

    _print_table(results)
    return 1 if any(r["status"] == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

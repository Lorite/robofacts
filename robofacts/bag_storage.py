#!/usr/bin/env python3
"""Where heavy bags are written and where they are moved to (2026-08-04).

The plumbing shared by the two halves of the replay-grade storage path:

* ``trial_runner`` asks :func:`resolve_bags_dir` where to WRITE a trial's bag,
  so a campaign can put fat replay-grade bags straight onto external
  storage instead of the rig's internal disk.
* ``offload_bags`` asks :func:`external_dest` where to MOVE finished bags, and
  uses :func:`file_manifest` / :func:`verify_copy` to prove a copy landed
  intact before anything is deleted from the source.

Both read one declaration, ``experiments/common/config/storage.yaml``, and both
FAIL LOUDLY when no destination is declared. That is the point of the module.
A path invented at runtime is the failure mode that matters here: an unmounted
``/media/<user>/<LABEL>`` is an ordinary empty directory on the internal disk,
so "move the bags to the SSD" would fill the very disk it was meant to relieve,
and the operator would find out at the end of a lab day. Hence:

* no destination declared  ->  :class:`StorageError` naming the file AND the flag
* destination on the same filesystem as the source  ->  refused by the caller
  (``offload_bags --allow-same-filesystem`` to override, for a deliberate
  same-disk reorganisation)

Nothing here imports ROS. It runs inside the dev container and equally on the
bare rig host over ssh, which is what lets an offload happen without touching
the running containers.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


from . import roots


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)
STORAGE_CONFIG = "common/config/storage.yaml"
DEFAULT_SUBDIR = "trial_bags"

#: The record directories an experiment keeps, mirrored verbatim under an
#: external destination so a bag's provenance survives the move: a quarantined
#: bag stays quarantined, which the index depends on (``is_issue_bag``).
RECORD_DIR_NAMES = ("bags", "bags_issues")

#: Written into an offloaded bag at the destination, so the copy is
#: self-describing exactly like ``slim_provenance.json`` is for a slim bag.
MANIFEST_NAME = "offload_manifest.json"

#: A bag directory being written by rosbag2, or a partial rsync, is never a
#: candidate for offloading.
UNSETTLED_SUFFIXES = (".active", ".tmp", ".partial")

#: Chunk for hashing. Large enough that a 6 GB bag is not 6 million syscalls.
CHUNK = 4 * 1024 * 1024


class StorageError(Exception):
    """No usable destination, or a destination that must not be used."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def storage_config_path(experiments_root: Optional[Path] = None) -> Path:
    return roots.experiments_root(experiments_root) / STORAGE_CONFIG


def load_storage(experiments_root: Optional[Path] = None) -> Dict[str, Any]:
    """Read storage.yaml. A missing file is not fatal: it means "nothing declared"."""
    path = storage_config_path(experiments_root)
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise StorageError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise StorageError(f"{path}: expected a top-level mapping")
    return raw


def host_level(name: Optional[str] = None,
               experiments_root: Optional[Path] = None) -> str:
    """This machine (or ``name``) as an ``execution_host`` factor level.

    Reuses the factor's own ``aliases`` rather than keeping a second list of
    machine names: the rig answers to three spellings already, and a storage
    config that disagreed with the factor file would be a silent mismatch.
    """
    name = name or socket.gethostname()
    try:
        from . import campaign_spec as cs
        factors = cs.load_factors(None, roots.experiments_root(experiments_root))
        factor = factors.get("execution_host")
        if factor is not None:
            return factor.canonical(name)
    except Exception:  # noqa: BLE001 - a missing factor file must not break offload
        pass
    return str(name)


def external_dest(level: Optional[str] = None, *,
                  config: Optional[Dict[str, Any]] = None,
                  experiments_root: Optional[Path] = None,
                  required: bool = False,
                  flag: str = "--dest") -> Optional[Path]:
    """The declared external destination for a host level, or None.

    With ``required=True`` a missing declaration raises with the two things the
    operator needs: which file to edit and which flag to pass instead.
    """
    level = level or host_level(experiments_root=experiments_root)
    config = config if config is not None else load_storage(experiments_root)
    hosts = config.get("hosts") or {}
    spec = hosts.get(level) or {}
    raw = spec.get("external_dest")
    if raw:
        return normalize_dest(Path(str(raw)).expanduser(), config=config)
    if required:
        raise StorageError(
            f"no external destination declared for host {level!r}.\n"
            f"  Either pass {flag} <path> explicitly, or set "
            f"hosts.{level}.external_dest in {storage_config_path(experiments_root)}.\n"
            f"  Nothing is guessed on purpose: an unmounted mount point is an "
            f"ordinary directory on the internal disk, so a guessed path would "
            f"fill the disk it was meant to relieve.")
    return None


def normalize_dest(dest: Path, *, config: Optional[Dict[str, Any]] = None) -> Path:
    """Append the configured subdirectory when handed a bare mount point.

    Pointing at the root of a disk is the natural thing to type and the wrong
    thing to do: trial folders would land beside whatever else the disk holds,
    and a later ``--move`` would have to be trusted with the whole volume.
    """
    dest = Path(dest)
    subdir = str((config or {}).get("external_subdir") or DEFAULT_SUBDIR)
    if subdir and os.path.ismount(str(dest)):
        return dest / subdir
    return dest


# --------------------------------------------------------------------------- #
# Where a trial's bag is written
# --------------------------------------------------------------------------- #
def resolve_bags_dir(spec: Optional[str], *, experiment: str, repo_bags_dir: Path,
                     level: Optional[str] = None, record_dir_name: str = "bags",
                     experiments_root: Optional[Path] = None) -> Tuple[Path, bool]:
    """Resolve a ``bags_root`` setting to the directory a trial records into.

    ``spec`` comes from ``runner.yaml`` (top level or per host) or the runner's
    ``--bags-root`` flag:

        None / "" / "repo" / "internal"   the repo's experiments/<name>/bags
                                         (the default, and what every trial
                                          recorded before 2026-08-04)
        "external"                       storage.yaml's external_dest for this host
        "/abs/path"                      that root, laid out the same way

    Returns ``(directory, is_external)``. The external layout MIRRORS the repo
    (``<root>/<experiment>/bags``) so an offloaded bag and a directly recorded
    one are indistinguishable at the destination, and one offload command
    handles both.
    """
    text = (spec or "").strip()
    if text in ("", "repo", "internal", "none", "default"):
        return Path(repo_bags_dir), False
    if text == "external":
        root = external_dest(level, experiments_root=experiments_root,
                             required=True, flag="--bags-root")
    else:
        root = normalize_dest(Path(text).expanduser(),
                             config=load_storage(experiments_root))
    return Path(root) / experiment / record_dir_name, True


# --------------------------------------------------------------------------- #
# Filesystem facts
# --------------------------------------------------------------------------- #
def free_bytes(path: Path) -> int:
    """Free space on the filesystem holding ``path`` (nearest existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    stat = os.statvfs(str(probe))
    return stat.f_bavail * stat.f_frsize


def device_id(path: Path) -> Optional[int]:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return probe.stat().st_dev
    except OSError:
        return None


def same_filesystem(a: Path, b: Path) -> bool:
    """True when both paths sit on the same device.

    The check that catches the unmounted-SSD case: a "move" onto the same
    filesystem reclaims nothing, and if the destination is an empty mount point
    it actively consumes the space the move was supposed to free.
    """
    dev_a, dev_b = device_id(a), device_id(b)
    return dev_a is not None and dev_a == dev_b


def bag_is_settled(bag_dir: Path, min_age_s: float = 60.0) -> Tuple[bool, str]:
    """Is this bag finished, or still being written?

    A bag is settled when it has a ``metadata.yaml`` (rosbag2 writes it last),
    carries no ``*.active`` / ``*.tmp`` / ``*.partial`` file, and nothing in it
    has been touched for ``min_age_s``. Offloading a bag mid-recording would
    copy a truncated file and then, in move mode, delete the real one.
    """
    bag_dir = Path(bag_dir)
    if not bag_dir.is_dir():
        return False, "not a directory"
    if not (bag_dir / "metadata.yaml").is_file():
        return False, "no metadata.yaml (bag never finalized)"
    newest = 0.0
    for path in bag_dir.rglob("*"):
        if path.name.endswith(UNSETTLED_SUFFIXES):
            return False, f"still being written ({path.name})"
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    import time

    age = time.time() - newest
    if min_age_s and age < min_age_s:
        return False, f"modified {age:.0f} s ago (younger than min age {min_age_s:.0f} s)"
    return True, "settled"


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, chunk: int = CHUNK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def file_manifest(root: Path, *, checksum: bool = True,
                  skip: Tuple[str, ...] = (MANIFEST_NAME,)) -> Dict[str, Dict[str, Any]]:
    """``relative path -> {bytes, sha256}`` for every regular file under ``root``.

    Symlinks are skipped rather than followed: an offload must copy the bag, not
    silently pull in whatever a link points at.
    """
    root = Path(root)
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if rel in skip:
            continue
        entry: Dict[str, Any] = {"bytes": path.stat().st_size}
        if checksum:
            entry["sha256"] = sha256_file(path)
        out[rel] = entry
    return out


def verify_copy(src: Path, dst: Path, *, checksum: bool = True,
                src_manifest: Optional[Dict[str, Dict[str, Any]]] = None
                ) -> List[str]:
    """Problems that make ``dst`` an untrustworthy copy of ``src``. Empty is good.

    Size AND sha256 by default. Size alone catches a truncated transfer and
    nothing else, and the whole reason to verify before deleting is the case
    where the bytes are wrong rather than absent.
    """
    src, dst = Path(src), Path(dst)
    problems: List[str] = []
    if not dst.is_dir():
        return [f"destination missing: {dst}"]
    source = src_manifest if src_manifest is not None else file_manifest(src, checksum=checksum)
    for rel, want in sorted(source.items()):
        target = dst / rel
        if not target.is_file():
            problems.append(f"missing at destination: {rel}")
            continue
        got_bytes = target.stat().st_size
        if got_bytes != want["bytes"]:
            problems.append(f"size differs: {rel} ({want['bytes']} vs {got_bytes})")
            continue
        if checksum and "sha256" in want:
            if sha256_file(target) != want["sha256"]:
                problems.append(f"checksum differs: {rel}")
    return problems


def write_manifest(dst: Path, payload: Dict[str, Any]) -> Path:
    path = Path(dst) / MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_manifest(dst: Path) -> Optional[Dict[str, Any]]:
    path = Path(dst) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def human(n: float) -> str:
    """Size for humans. Same formatting as slim_bag.human, duplicated in three
    lines rather than importing it, so this module stays ROS-free and importable
    on a bare rig host."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def main(argv=None) -> int:
    """Print what this host has declared, which is the first thing to check."""
    import argparse

    ap = argparse.ArgumentParser(description="Report the declared bag storage for a host")
    ap.add_argument("--host-level", help="report for this level instead of this machine")
    roots.add_root_arguments(ap)
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    config = load_storage(args.experiments_root)
    level = args.host_level or host_level(experiments_root=args.experiments_root)
    print(f"config      : {storage_config_path(args.experiments_root)}")
    print(f"host        : {socket.gethostname()}  ->  level {level!r}")
    dest = external_dest(level, config=config, experiments_root=args.experiments_root)
    if dest is None:
        print("external    : NOT DECLARED (offload and `bags_root: external` will refuse)")
        return 0
    print(f"external    : {dest}")
    print(f"  exists    : {dest.exists()}")
    if dest.exists():
        print(f"  free      : {human(free_bytes(dest))}")
        repo_bags = Path(args.experiments_root)
        print(f"  same fs as {repo_bags}: {same_filesystem(dest, repo_bags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

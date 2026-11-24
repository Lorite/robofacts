#!/usr/bin/env python3
"""Named ``ros2 bag record`` topic profiles, declared as data by the project.

A record profile is a named topic list. A trial picks one with a factor whose ``maps_to`` has
``kind: record_profile`` (or ``default_record_profile`` in ``trials.yaml``), and the runner
records exactly those topics. An experiment whose topics are its own lists them in
``runner.yaml`` as ``record_topics`` instead.

The profiles are not code. They live in YAML files, merged in this order, where a later file
replaces a block or a profile of the same name:

1. the package's own ``record_profiles.yaml`` (next to this module, generic blocks only),
2. ``record_profiles.yaml`` of every robot adapter the project enables, when there is one,
3. the project's ``<experiments root>/common/config/record_profiles.yaml``.

The format::

    blocks:                      # reusable topic groups
      drone_telemetry:
        topics: ["/{cf_name}/pose", "/{cf_name}/battery", /tf, /rosout]
    profiles:
      flight:
        blocks: [drone_telemetry]
        topics: [/joy]           # optional topics of this profile alone
        size: "~5 MB/min"        # optional, printed next to the command

A topic may hold ``{name}`` placeholders. They are filled from the names in force: the
package's neutral ``DEFAULT_NAMES`` under the project's ``record_profiles:`` mapping in
``trials.yaml``. A profile's topic list keeps the order of its blocks and drops repeats.
``ros2 bag record`` does not subscribe a topic that nobody publishes, so a profile may list
optional topics.

Use::

    from robofacts.record_profiles import build_topics
    topics = build_topics("flight", cf_name="cf2")

or standalone::

    trials profiles flight --topics-only
"""
from __future__ import annotations

import argparse
import string
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from . import roots

#: The core knows no robot, so it has no default names. Each enabled robot adapter adds
#: its own (see robofacts/adapters), and a project sets its own under ``record_profiles:``
#: in trials.yaml, any name it uses.
DEFAULT_NAMES: Dict[str, str] = {}

#: The profile file of a project, relative to its experiments root.
PROJECT_PROFILES_REL = Path("common") / "config" / "record_profiles.yaml"

#: The package's own profile file (generic blocks only, it may be absent).
PACKAGE_PROFILES_FILE = Path(__file__).resolve().parent / "record_profiles.yaml"


class ProfileError(ValueError):
    """A record-profile file is malformed, or a profile names something that does not exist."""


def names() -> Dict[str, str]:
    """The names in force: the enabled adapters' defaults under the project's ``record_profiles``."""
    from . import adapters

    merged = dict(DEFAULT_NAMES)
    merged.update({str(k): str(v) for k, v in adapters.merged_dict("NAMES").items()})
    project = roots.setting("record_profiles") or {}
    if not isinstance(project, dict):
        raise ValueError(f"trials.yaml: record_profiles must be a mapping, got {project!r}")
    merged.update({str(k): str(v) for k, v in project.items()})
    return merged


def default_cf_name() -> Optional[str]:
    """The drone the profiles name their topics after, unless runner.yaml says ``cf_name:``.
    None when neither the project nor an enabled adapter names one."""
    return names().get("cf_name")


def default_record_profile() -> Optional[str]:
    """The profile of a trial that has no record_profile factor: ``trials.yaml``'s
    ``default_record_profile``, else None (the runner then refuses to guess)."""
    value = roots.setting("default_record_profile")
    return None if value in (None, "") else str(value)


# ── loading ──────────────────────────────────────────────────────────────────────────────


def project_profiles_file(experiments_root: Optional[Path] = None) -> Optional[Path]:
    """The project's profile file, or None when no project is found."""
    try:
        return roots.experiments_root(experiments_root) / PROJECT_PROFILES_REL
    except roots.RootNotFound:
        return None


def adapter_profile_files() -> List[Path]:
    """The ``record_profiles.yaml`` of each enabled robot adapter (none before adapters exist)."""
    try:
        from . import adapters  # noqa: WPS433 (optional until the adapters land)
    except ImportError:
        return []
    return [p for p in adapters.enabled_files("record_profiles.yaml") if p.is_file()]


def profile_files(experiments_root: Optional[Path] = None) -> List[Path]:
    """Every profile file in force, in merge order (a later one wins)."""
    files: List[Path] = []
    if PACKAGE_PROFILES_FILE.is_file():
        files.append(PACKAGE_PROFILES_FILE)
    files += adapter_profile_files()
    project = project_profiles_file(experiments_root)
    if project is not None and project.is_file():
        files.append(project)
    return files


def _topic_list(where: str, raw: Any) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(t, str) and t for t in raw):
        raise ProfileError(f"{where}: topics must be a list of topic names, got {raw!r}")
    return list(raw)


def _read(path: Path) -> Dict[str, Dict[str, Dict[str, Any]]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: the top level must be a mapping with blocks: and profiles:")
    unknown = sorted(set(raw) - {"blocks", "profiles"})
    if unknown:
        raise ProfileError(f"{path}: unknown top-level keys {unknown} (only blocks, profiles)")
    out: Dict[str, Dict[str, Dict[str, Any]]] = {"blocks": {}, "profiles": {}}
    for name, spec in (raw.get("blocks") or {}).items():
        where = f"{path}: block {name!r}"
        if isinstance(spec, list):            # short form: the block is its topic list
            spec = {"topics": spec}
        if not isinstance(spec, dict) or set(spec) - {"topics"}:
            raise ProfileError(f"{where}: must be a topic list or a mapping with topics:")
        out["blocks"][str(name)] = {"topics": _topic_list(where, spec.get("topics")),
                                    "source": path}
    for name, spec in (raw.get("profiles") or {}).items():
        where = f"{path}: profile {name!r}"
        if not isinstance(spec, dict) or set(spec) - {"blocks", "topics", "size"}:
            raise ProfileError(f"{where}: must be a mapping of blocks:, topics: and size:")
        blocks = spec.get("blocks") or []
        if not isinstance(blocks, list) or not all(isinstance(b, str) for b in blocks):
            raise ProfileError(f"{where}: blocks must be a list of block names")
        out["profiles"][str(name)] = {"blocks": list(blocks),
                                      "topics": _topic_list(where, spec.get("topics")),
                                      "size": str(spec.get("size") or ""),
                                      "source": path}
    return out


def load(experiments_root: Optional[Path] = None) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """The merged blocks and profiles in force, each checked for unknown blocks."""
    merged: Dict[str, Dict[str, Dict[str, Any]]] = {"blocks": {}, "profiles": {}}
    for path in profile_files(experiments_root):
        part = _read(path)
        merged["blocks"].update(part["blocks"])
        merged["profiles"].update(part["profiles"])
    for name, spec in merged["profiles"].items():
        missing = [b for b in spec["blocks"] if b not in merged["blocks"]]
        if missing:
            raise ProfileError(f"{spec['source']}: profile {name!r} names unknown blocks "
                               f"{missing}; known blocks: {sorted(merged['blocks'])}")
    return merged


def profiles(experiments_root: Optional[Path] = None) -> Dict[str, List[str]]:
    """Profile name -> its block names (the shape of the former ``PROFILES`` constant)."""
    return {name: list(spec["blocks"]) for name, spec in load(experiments_root)["profiles"].items()}


def profile_notes(experiments_root: Optional[Path] = None) -> Dict[str, str]:
    """Profile name -> its declared size note (the former ``PROFILE_NOTES``)."""
    return {name: spec["size"] for name, spec in load(experiments_root)["profiles"].items()
            if spec["size"]}


def __getattr__(name):
    """``PROFILES`` and ``PROFILE_NOTES``, resolved from the files and the project settings
    on access."""
    if name == "PROFILES":
        return profiles()
    if name == "PROFILE_NOTES":
        return profile_notes()
    raise AttributeError(name)


# ── building ─────────────────────────────────────────────────────────────────────────────


def _fill(topic: str, values: Dict[str, str], where: str) -> str:
    fields = [f for _, f, _, _ in string.Formatter().parse(topic) if f is not None]
    missing = sorted({f for f in fields if f not in values})
    if missing:
        raise ProfileError(f"{where}: topic {topic!r} uses the unknown name(s) {missing}. "
                           f"Set them under record_profiles: in trials.yaml. Known names: "
                           f"{sorted(values)}")
    return topic.format(**values)


def _dedupe(topics: Iterable[str]) -> List[str]:
    seen: set = set()
    return [t for t in topics if not (t in seen or seen.add(t))]


def build_topics(profile: str, cf_name: str = None, *,
                 experiments_root: Optional[Path] = None, **name_overrides) -> List[str]:
    """The topic list of a named profile, placeholders filled, order kept, repeats dropped.

    ``cf_name`` and any other name given as a keyword (``camera=``) override the names in
    force for this call. None means "not overridden"."""
    data = load(experiments_root)
    if profile not in data["profiles"]:
        raise ValueError(f"unknown record profile {profile!r}; "
                         f"expected one of {sorted(data['profiles'])}")
    values = names()
    overrides = {"cf_name": cf_name, **name_overrides}
    values.update({k: str(v) for k, v in overrides.items() if v is not None})
    spec = data["profiles"][profile]
    topics: List[str] = []
    for block in spec["blocks"]:
        where = f"block {block!r} of profile {profile!r}"
        topics += [_fill(t, values, where) for t in data["blocks"][block]["topics"]]
    topics += [_fill(t, values, f"profile {profile!r}") for t in spec["topics"]]
    return _dedupe(topics)


# ── the command line ─────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="trials profiles", description=__doc__.splitlines()[0])
    ap.add_argument("profile", nargs="?", help="a profile name (omit with --list)")
    ap.add_argument("--list", action="store_true",
                    help="list the profiles in force, their size notes and their files")
    ap.add_argument("--output", default="", help="the recording directory of the printed command")
    ap.add_argument("--topics-only", action="store_true",
                    help="print just the list of sources, one per line")
    args = ap.parse_args(argv)

    if args.list or not args.profile:
        data = load()
        if not data["profiles"]:
            print("no record profiles in force. A project declares them in "
                  f"<experiments root>/{PROJECT_PROFILES_REL}", file=sys.stderr)
            return 0 if args.list else 2
        for name, spec in sorted(data["profiles"].items()):
            print(f"{name:32s} {spec['size'] or '-'}")
        print("files: " + ", ".join(str(p) for p in profile_files()), file=sys.stderr)
        return 0
    topics = build_topics(args.profile)
    if args.topics_only:
        print("\n".join(topics))
        return 0
    if not args.output:
        ap.error("--output is required unless --topics-only is given")
    from . import backend
    print(" ".join(backend.active().recorder.command(Path(args.output), topics, {})))
    return 0


if __name__ == "__main__":
    sys.exit(main())

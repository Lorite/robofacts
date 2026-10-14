#!/usr/bin/env python3
"""Which code produced a trial (#96, 2026-08-04).

The metadata contract already answered *which machine* ran a trial
(``execution_host``) and *which image* it ran in (``execution_container``), but
nothing recorded *which code*. That gap is the reason a six-week-old bag cannot
be reasoned about: the framework itself keeps changing under the trials (the
``markerless_replay`` profile appeared mid-campaign, the metadata contract grew
fields, the verify steps got stricter), so "this old bag looks odd" and "this
old bag predates commit X" are the same observation with and without a stamp.

Two decisions are baked in here, and both matter more than the hash:

**The dirty flag outranks the commit.** A bare sha recorded against a modified
working tree claims a reproducibility that does not exist, which is worse than
recording nothing — it looks authoritative while being wrong, the same failure
the metadata contract was written to stop. So the probe reports tracked-file
modifications alongside the hash, and a consumer should read ``dirty`` first.

**Untracked files do not count as dirty.** This repo permanently carries
untracked directories (vendored SDK checkouts under ``lib/``, results folders,
sub-repos not yet added), so counting them would pin ``dirty`` to True forever
and the flag would carry no information. Only tracked modifications — including
a submodule whose checked-out content moved — make HEAD a lie about what ran.

**The probe runs on the host that flew the trial.** Trials execute remotely
through ``remote_exec_utils`` against the rig's own checkout, and that checkout
is routinely behind the laptop's after the usual edit-here / pull-there flow.
Stamping the laptop's HEAD onto a bag the rig recorded would be confidently
wrong, so the same ``docker exec`` path the bag verification uses asks the rig.
``captured_on`` records which host answered.

**The build, not only the checkout (2026-10-14).** The container executes
``install/``, not the source tree, so a commit made after the last ``colcon
build`` is in the hash but not in the binaries. For every colcon workspace of
the project (the ``colcon_workspaces`` setting of ``trials.yaml``, else ``.``
and ``ros2_ws`` when they hold ``src/`` and ``install/``), the probe also
records the HEAD of each source repository under ``src/`` that is its own git
repository, and the time of the newest entry in ``install/``. A repository whose
HEAD commit is newer than that build is listed in ``possibly_unbuilt``. The
check is one-sided: a pull can bring commits whose commit time is older than
the build, so an empty list bounds the code that ran, it does not prove it.

Nothing here is ever fatal. A trial that has already flown must never be lost
to a failed git query, so every failure path returns a dict with ``commit:
None`` and an ``error`` string instead of raising.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


from . import roots

#: git exports these to hooks. From a worktree they are absolute paths, so a git call that
#: inherits them answers for the hook's repository instead of ``path`` (2026-10-14).
_GIT_HOOK_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR")


def _env_without_git_hook_vars() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_HOOK_VARS}


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)

#: Enough to identify what changed without turning the metadata into a diff.
#: A tree with more modifications than this is not one anybody will reconstruct
#: from a file list anyway — `dirty_file_count` stays exact.
MAX_DIRTY_FILES = 20

DEFAULT_TIMEOUT_S = 20.0

#: Tried when ``trials.yaml`` declares no ``colcon_workspaces``: the project is itself a
#: colcon workspace, or it holds one at ``ros2_ws/`` (the layout of the lab this came from).
DEFAULT_WORKSPACES = (".", "ros2_ws")

# `safe.directory` because the workspace is a bind mount owned by the host user
# while the container runs as another: without it git refuses with "detected
# dubious ownership" and the probe would report every trial as unknown.
_GIT = "git -c safe.directory='*'"


def _script(workspace: str, workspaces: Sequence[str] = ()) -> str:
    """A /bin/sh snippet emitting key=value lines, identical local and remote.

    One script for both paths so the parser is exercised the same way wherever
    the trial ran — the remote branch is the one that cannot be unit-tested
    without a rig, so it must not have a parser of its own.
    """
    d = shlex.quote(str(workspace))
    return (
        f"cd {d} 2>/dev/null || {{ echo error=no-workspace; exit 0; }}; "
        f"{_GIT} rev-parse --git-dir >/dev/null 2>&1 || {{ echo error=not-a-repo; exit 0; }}; "
        f"echo commit=$({_GIT} rev-parse HEAD 2>/dev/null); "
        f"echo branch=$({_GIT} rev-parse --abbrev-ref HEAD 2>/dev/null); "
        # --untracked-files=no: see the module docstring. Tracked modifications
        # only, which is what makes the recorded HEAD an incomplete description.
        f"{_GIT} status --porcelain --untracked-files=no 2>/dev/null | sed 's/^/dirty=/'; "
        f"echo committed_at=$({_GIT} log -1 --format=%ct 2>/dev/null)"
        + _workspace_script(workspaces)
    )


def _workspace_script(workspaces: Sequence[str]) -> str:
    """Lines ``ws=``, ``install=`` and ``src=`` for each colcon workspace (see the docstring).

    A directory under ``src/`` counts only when it is the top level of its own repository,
    so a package that lives in the project's own repository is covered by ``commit``.
    """
    if not workspaces:
        return ""
    names = " ".join(shlex.quote(w) for w in workspaces)
    return (
        f"; for ws in {names}; do "
        '[ -d "$ws/src" ] && [ -d "$ws/install" ] || continue; '
        'echo "ws=$ws"; '
        "printf 'install=%s\\t%s\\n' \"$ws\" \"$(find \"$ws/install\" -printf '%T@\\n' 2>/dev/null | sort -n | tail -1)\"; "
        'for d in "$ws"/src/*/; do d="${d%/}"; '
        f'top=$(cd "$d" && {_GIT} rev-parse --show-toplevel 2>/dev/null) || continue; '
        '[ "$top" = "$(cd "$d" && pwd -P)" ] || continue; '
        f'c=$({_GIT} -C "$d" rev-parse HEAD 2>/dev/null); '
        f't=$({_GIT} -C "$d" log -1 --format=%ct 2>/dev/null); '
        f'n=$({_GIT} -C "$d" status --porcelain --untracked-files=no 2>/dev/null | wc -l); '
        'printf "src=%s\\t%s\\t%s\\t%s\\t%s\\n" "$ws" "${d##*/}" "$c" "$t" "$n"; '
        "done; done"
    )


def workspaces_to_probe() -> List[str]:
    """The colcon workspaces to describe, relative to the checkout.

    ``colcon_workspaces`` in ``trials.yaml`` wins. Without it, ``.`` and ``ros2_ws`` are
    tried, and the probe itself skips a candidate without ``src/`` and ``install/``.
    """
    declared = roots.setting("colcon_workspaces")
    if declared is None:
        return list(DEFAULT_WORKSPACES)
    if isinstance(declared, str):
        declared = [declared]
    return [str(w) for w in declared]


_ERRORS = {
    "no-workspace": "workspace directory not found",
    "not-a-repo": "workspace is not a git repository",
}


def unknown(reason: str, captured_on: str, workspace: Any) -> Dict[str, Any]:
    """The shape returned when the probe could not answer.

    Deliberately the same keys as a successful probe: a consumer that reads
    ``code_version["dirty"]`` must not have to care whether the query worked,
    and `None` (unknown) is a different claim from `False` (verified clean).
    """
    return {"commit": None, "branch": None, "dirty": None,
            "dirty_files": [], "dirty_file_count": 0,
            "committed_at": None, "builds": [],
            "captured_on": captured_on, "workspace": str(workspace),
            "error": reason}


def parse(stdout: str, captured_on: str, workspace: Any) -> Dict[str, Any]:
    """Turn the script's key=value lines into the metadata dict."""
    commit = branch = committed_at = None
    dirty_files = []
    builds: Dict[str, Dict[str, Any]] = {}
    for line in stdout.splitlines():
        key, _, raw = line.partition("=")
        key = key.strip()
        # `raw` is NOT stripped: porcelain's first status column is a SPACE for
        # an unstaged modification (" M path"), so stripping it here shifts the
        # path by one character and every dirty file is reported truncated.
        value = raw.strip()
        if key == "error":
            return unknown(_ERRORS.get(value, value), captured_on, workspace)
        if key == "commit":
            commit = value or None
        elif key == "branch":
            branch = value or None
        elif key == "committed_at":
            committed_at = _int_or_none(value)
        elif key == "ws":
            builds.setdefault(value, {"workspace": value, "install_newest": None, "sources": {}})
        elif key == "install":
            ws, _, stamp = raw.partition("\t")
            build = builds.setdefault(ws.strip(), {"workspace": ws.strip(), "install_newest": None, "sources": {}})
            build["install_newest"] = _float_or_none(stamp)
        elif key == "src":
            fields = raw.rstrip("\n").split("\t")
            if len(fields) == 5:
                ws, name, src_commit, src_time, src_dirty = (f.strip() for f in fields)
                build = builds.setdefault(ws, {"workspace": ws, "install_newest": None, "sources": {}})
                build["sources"][name] = {"commit": src_commit or None,
                                          "committed_at": _int_or_none(src_time),
                                          "dirty_file_count": _int_or_none(src_dirty) or 0}
        elif key == "dirty" and value:
            # porcelain XY: two status columns, a space, then the path.
            dirty_files.append(raw[3:].strip() if len(raw) > 3 else value)

    if not commit:
        return unknown("git produced no commit", captured_on, workspace)
    version = {
        "commit": commit,
        "branch": branch,
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files[:MAX_DIRTY_FILES],
        "dirty_file_count": len(dirty_files),
        "captured_on": captured_on,
        "workspace": str(workspace),
        "committed_at": committed_at,
        "builds": [_with_unbuilt(build, committed_at) for build in builds.values()],
    }
    return version


def _with_unbuilt(build: Dict[str, Any], committed_at: Optional[int]) -> Dict[str, Any]:
    """Add ``possibly_unbuilt``: the repositories whose HEAD commit is newer than the build.

    The project's own repository is listed as ``.`` because its packages under ``src/`` are
    built from its HEAD too. Without an ``install/`` time nothing can be compared.
    """
    built = build.get("install_newest")
    late = []
    if built is not None:
        if committed_at is not None and committed_at > built:
            late.append(".")
        late += sorted(name for name, src in build["sources"].items()
                       if src.get("committed_at") is not None and src["committed_at"] > built)
    build["possibly_unbuilt"] = late
    return build


def _int_or_none(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except (AttributeError, ValueError):
        return None


def _float_or_none(value: str) -> Optional[float]:
    try:
        return float(value.strip())
    except (AttributeError, ValueError):
        return None


def probe(target=None, workspace: Optional[Path] = None,
          timeout: float = DEFAULT_TIMEOUT_S, builds: bool = True) -> Dict[str, Any]:
    """Ask the host that runs the trials which code it has checked out.

    ``target`` is a ``remote_exec_utils.RemoteTarget`` (the rig) or None for a
    local run. ``workspace`` defaults to the target's workspace remotely and to
    this repo locally. ``builds=False`` skips the colcon workspaces.
    """
    if target is None:
        where = "local"
        path = Path(workspace or roots.project_root())
    else:
        where = f"remote:{target.host}"
        path = Path(workspace or target.workspace)

    workspaces = workspaces_to_probe() if builds else []
    script = _script(str(path), workspaces)
    try:
        if target is None:
            result = subprocess.run(["sh", "-c", script], capture_output=True,
                                    text=True, timeout=timeout,
                                    env=_env_without_git_hook_vars())
        else:
            from .remote_exec_utils import remote_sh
            result = remote_sh(target, script, timeout=timeout)
    except subprocess.TimeoutExpired:
        if workspaces:
            # The build part walks every source repository and the whole install tree,
            # so it is the slow part. The commit of the checkout matters more: ask again
            # without it, and say why the build part is missing.
            version = probe(target, path, timeout, builds=False)
            version["builds_error"] = f"build query timed out after {timeout:g} s"
            return version
        return unknown(f"git query timed out after {timeout:g} s", where, path)
    except Exception as exc:  # noqa: BLE001 - a finished trial outranks a probe
        return unknown(f"git query failed: {exc}", where, path)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return unknown(f"git query exited {result.returncode}"
                       + (f": {detail[-1]}" if detail else ""), where, path)
    return parse(result.stdout, where, path)


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
#: A campaign runs dozens of trials against one unchanging checkout, and each
#: remote probe is an ssh round-trip inside the per-trial critical path. Cached
#: per (host, workspace) for the life of the process: the runner is started
#: after the code is in place, so a mid-invocation change is not a case worth
#: paying 40 round-trips to catch.
_CACHE: Dict[tuple, Dict[str, Any]] = {}


def cached(target=None, workspace: Optional[Path] = None,
           timeout: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """:func:`probe`, once per (host, workspace) per process."""
    host = getattr(target, "host", None) or "local"
    path = str(workspace or getattr(target, "workspace", None) or roots.project_root())
    key = (host, path)
    if key not in _CACHE:
        _CACHE[key] = probe(target, workspace, timeout)
    return dict(_CACHE[key])


def reset_cache() -> None:
    _CACHE.clear()


def summary(version: Dict[str, Any]) -> str:
    """One line for a log or a table cell."""
    if not version or not version.get("commit"):
        return f"unknown ({(version or {}).get('error', 'not probed')})"
    line = f"{version['commit'][:8]} ({version.get('branch') or 'detached'})"
    if version.get("dirty"):
        line += f" +{version.get('dirty_file_count', 0)} uncommitted"
    unbuilt = sorted({name for build in version.get("builds") or [] for name in build.get("possibly_unbuilt") or []})
    if unbuilt:
        line += f", built before the last commit of {', '.join(unbuilt)}"
    return f"{line} on {version.get('captured_on', '?')}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", type=Path, default=None,
                    help="the checkout to describe (default: the project root)")
    ap.add_argument("--json", action="store_true", help="print the metadata dict")
    args = ap.parse_args(argv)

    version = probe(None, args.workspace or roots.project_root())
    print(json.dumps(version, indent=2) if args.json else summary(version))
    return 0 if version.get("commit") else 1


if __name__ == "__main__":
    sys.exit(main())

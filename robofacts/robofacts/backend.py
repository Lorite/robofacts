"""The backend of a project: how its stack starts and how a trial is recorded.

The trial loop, the gate protocol, the planner and the coverage index do not depend on
the middleware of the system under test. Two things do: starting the stack and recording
the trial. A backend supplies both as two small interfaces, a :class:`Launcher` and a
:class:`Recorder`. Both return command lines. The runner keeps everything that handles
processes (the background start, the ssh and container wrapper, process groups, the signal
sequence, the run log), so a backend never starts or stops a process itself.

A project names its backend in ``trials.yaml``::

    backend: process       # the built-in one: a shell command and a file recorder
    backend: <name>        # another backend: the package robofacts_<name>
    backend: my_pkg.trials # any importable module with a BACKEND object

``$TRIALS_BACKEND`` overrides the file for one session.

What a backend must provide is the two interfaces below. The other members of
:class:`Backend` are optional hooks with working defaults.
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Protocol, Tuple, runtime_checkable

ENV_BACKEND = "TRIALS_BACKEND"

#: There is no default backend (Alejandro, 2026-11-25): a project names its backend in
#: trials.yaml, so a project is never read with the wrong recorder. ``trials init`` writes
#: the line. The built-in backend is the one ``trials init`` chooses when asked for none.
DEFAULT_INIT_BACKEND = "process"

#: Backends that ship with this package, by the name a project writes. Any other name
#: loads the module ``robofacts_<name>``.
BUILT_IN = {
    "process": "robofacts.backends.process",
}


#: The folders of an experiment, by role: what the coverage index reads (recordings, slim),
#: what a person excluded (issues, issues_slim) and the diagnostic runs it never reads
#: (scratch, scratch_slim). The names come from the backend: a backend whose projects
#: already have folders keeps their names, a new backend gets the general names.
RECORDING_DIRS = {
    "recordings": "recordings", "issues": "recordings_issues",
    "slim": "recordings_slim", "issues_slim": "recordings_issues_slim",
    "scratch": "recordings_scratch", "scratch_slim": "recordings_scratch_slim",
}
#: The older names, for a backend whose recording is called a bag.
BAG_DIRS = {
    "recordings": "bags", "issues": "bags_issues",
    "slim": "bags_slim", "issues_slim": "bags_issues_slim",
    "scratch": "bags_scratch", "scratch_slim": "bags_scratch_slim",
}


class BackendError(ValueError):
    """A backend that cannot be loaded, or a declaration that the backend refuses."""


@runtime_checkable
class Launcher(Protocol):
    """Starts the stack of one trial."""

    def parse(self, raw: Dict[str, Any], path: Path) -> Dict[str, Any]:
        """The stack declaration of one ``runner.yaml`` (its parsed content ``raw``, read
        from ``path``), validated. Raises :class:`BackendError` with the reason. The
        result is what :meth:`command` receives as ``stack``."""

    def command(self, stack: Dict[str, Any], args: Dict[str, Any],
                values: Dict[str, Any]) -> Optional[List[str]]:
        """The command line that starts the stack and runs until it is stopped, or None
        when the experiment has no stack. ``args`` are the trial's resolved launch
        arguments, ``values`` its factor values."""

    def stop_patterns(self) -> Dict[str, str]:
        """Process patterns for the stop sweeps, by kind (``all``, ``recorder``,
        ``launch``): regular expressions with a ``{workspace}`` placeholder, matched
        against command lines on the host. Empty when stopping the process group is
        enough."""


@runtime_checkable
class Recorder(Protocol):
    """Records one trial into one directory."""

    #: The word for one recording in printed messages (a backend may call it a bag, a log).
    noun: str

    def command(self, directory: Path, sources: List[str],
                options: Dict[str, Any]) -> List[str]:
        """The command line that records ``sources`` into ``directory`` until it receives
        SIGINT, and then finalizes the recording."""

    def has_data(self, target: Any, directory: Path) -> Tuple[bool, str]:
        """Did the recorder leave a complete recording, asked on the host that recorded
        (``target`` is None for this machine)? Returns ``(ok, reason if not)``."""

    def holds_data(self, directory: Path) -> bool:
        """Does this local directory hold recorded data? The coverage index asks this of
        every trial, so it must be cheap."""

    def unsettled_reason(self, directory: Path) -> Optional[str]:
        """Why this local recording is not finalized yet, or None when it is. The storage
        tools ask before they move a recording."""

    def after_trial(self, directory: Path, target: Any, hooks: Any, project_root: Path, *,
                    reduced: bool = True) -> None:
        """Optional work on a finished recording: a text export, a reduced copy. Never
        raises: the trial has happened. ``reduced`` is False when the caller wants no
        reduced copy."""


def _default_host_setup(spec: Dict[str, Any], workspace: Path) -> Tuple[str, str]:
    return str(spec.get("ros_setup", "") or ""), str(spec.get("ws_setup", "") or "")


@dataclass
class Backend:
    """A launcher, a recorder and a few optional hooks."""

    name: str
    launcher: Launcher
    recorder: Recorder
    #: The two setup files a command sources on a remote host, from the host's settings
    #: (the keys ``ros_setup`` and ``ws_setup``, whose names are older than the backends).
    #: A backend supplies its defaults for a host that names none.
    host_setup: Callable[[Dict[str, Any], Path], Tuple[str, str]] = _default_host_setup
    #: Run once before the machine checks of a gate (a middleware daemon restart). The
    #: default does nothing.
    gate_prepare: Callable[[], None] = field(default=lambda: None)
    #: Gate checks the backend ships: a directory of ``<name>.sh`` scripts, found after the
    #: project's and the adapters' checks and before the core's. Which of them are read-only
    #: probes that may share a batch, which must run alone (with the reason), and the
    #: sentence the voice says when one fails.
    checks_dir: Optional[Path] = None
    parallel_safe_checks: FrozenSet[str] = frozenset()
    sequential_checks: Dict[str, str] = field(default_factory=dict)
    gate_sentences: Dict[str, Callable[[str], str]] = field(default_factory=dict)
    #: The bash helper the backend's checks source, a superset of the core's. None means
    #: the core's own (preflight/lib/common.sh).
    checks_lib: Optional[Path] = None
    #: The dotted package that holds the backend's robot adapters (``<package>.<name>``),
    #: which a project enables in trials.yaml ``adapters:``.
    adapters_package: str = ""
    #: Optional parts of the framework that only this backend can offer, by name, as dotted
    #: module paths loaded on first use (:meth:`feature`).
    features: Dict[str, str] = field(default_factory=dict)
    #: Extra ``trials`` subcommands: name -> ("module:function", help).
    commands: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    #: Lint rules of the backend: pytest files that ``trials lint`` runs after the core's.
    lint_files: Tuple[Path, ...] = ()
    #: The backend's templates for ``trials new`` and ``trials init``: a directory like the
    #: core's ``skeleton/``. Its ``experiment/`` replaces the core's experiment template, and
    #: a file under its ``project/`` replaces the core's file of the same path.
    skeleton_dir: Optional[Path] = None
    #: Shell tools the backend ships, found by ``trials tool`` after the core's.
    tools_dir: Optional[Path] = None
    #: The one of those tools that stops every process of the backend's stacks on a host (the
    #: clean slate of the web interface), or "" when the backend ships none. The recovery of a
    #: killed run names it for the host where that run's stack may still run.
    clean_slate_tool: str = ""
    #: How to start ``trials`` on another machine where the package is installed.
    cli: Tuple[str, ...] = ("trials",)
    #: The package whose modules run on another machine as ``python3 -m <package>.<module>``.
    package: str = "robofacts"
    #: Build workspaces of a project that declares none in trials.yaml ``build_workspaces:``,
    #: relative to the checkout. The code version of a trial says whether each one was built
    #: after the last commit. ``workspaces_setting`` is the backend's older name of that key.
    build_workspaces: Tuple[str, ...] = (".",)
    workspaces_setting: str = ""
    #: The folder names of an experiment, by role (see RECORDING_DIRS).
    record_dirs: Dict[str, str] = field(default_factory=lambda: dict(RECORDING_DIRS))

    def feature(self, name: str) -> Optional[ModuleType]:
        """The module of an optional feature, or None when this backend does not offer it."""
        dotted = self.features.get(name)
        return importlib.import_module(dotted) if dotted else None


_LOADED: Dict[str, Backend] = {}


def configured_name(start: Optional[Any] = None) -> str:
    """The backend name in force: ``$TRIALS_BACKEND``, else ``backend:`` of the
    ``trials.yaml`` at or above ``start`` (default the working directory). Raises
    :class:`BackendError` when neither names one."""
    from_env = os.environ.get(ENV_BACKEND, "").strip()
    if from_env:
        return from_env
    from . import roots
    try:
        value = roots.setting("backend", start=start)
        project_file = roots._project_file_in_force(start)
    except Exception:  # noqa: BLE001 - an unreadable project file is reported by its own reader
        value, project_file = None, None
    if value:
        return str(value).strip()
    where = str(project_file) if project_file else "no trials.yaml above this directory"
    raise BackendError(
        f"the project names no backend ({where}). Add one line to trials.yaml: "
        f"`backend: process` (built in: a command and a log recorder) or `backend: <name>` "
        f"(the package robofacts_<name>). Or set "
        f"${ENV_BACKEND} for one session")


def load(name: str) -> Backend:
    """The backend called ``name``: a built-in one, the module ``robofacts_<name>``, or a
    dotted module path. The module holds a ``BACKEND`` object."""
    if name in _LOADED:
        return _LOADED[name]
    candidates = [BUILT_IN[name]] if name in BUILT_IN else (
        [name] if "." in name else [f"robofacts_{name}", name])
    module = None
    errors = []
    for candidate in candidates:
        try:
            module = importlib.import_module(candidate)
            break
        except ImportError as exc:
            errors.append(f"{candidate}: {exc}")
    if module is None:
        raise BackendError(
            f"backend {name!r} cannot be loaded ({'; '.join(errors)}). trials.yaml `backend:` "
            f"names one of {', '.join(sorted(BUILT_IN))}, or an importable module")
    found = getattr(module, "BACKEND", None)
    if not isinstance(found, Backend):
        raise BackendError(f"backend {name!r}: the module {module.__name__} has no BACKEND object")
    _LOADED[name] = found
    return found


def source_dirs(start: Optional[Any] = None) -> List[Path]:
    """The directories that hold this package and the package of the project's backend, for
    the PYTHONPATH of a child process or of another machine that runs them from a checkout."""
    dirs = [Path(__file__).resolve().parents[1]]
    try:
        found = active(start)
        top = type(found.launcher).__module__.split(".")[0]
        location = Path(importlib.import_module(top).__file__).resolve().parents[1]
        if location not in dirs:
            dirs.append(location)
    except Exception:  # noqa: BLE001 - an unknown backend adds nothing
        pass
    return dirs


def record_dirs(start: Optional[Any] = None) -> Dict[str, str]:
    """The folder names of the project at or above ``start``, by role (RECORDING_DIRS)."""
    try:
        return dict(active(start).record_dirs)
    except BackendError:
        return dict(RECORDING_DIRS)


def active(start: Optional[Any] = None) -> Backend:
    """The backend of the project at or above ``start`` (default the working directory)."""
    return load(configured_name(start))

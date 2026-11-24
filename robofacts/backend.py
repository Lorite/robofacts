"""The backend of a project: how its stack starts and how a trial is recorded.

The trial loop, the gate protocol, the planner and the coverage index do not depend on
the middleware of the system under test. Two things do: starting the stack and recording
the trial. A backend supplies both as two small interfaces, a :class:`Launcher` and a
:class:`Recorder`. Both return command lines. The runner keeps everything that handles
processes (the background start, the ssh and container wrapper, process groups, the signal
sequence, the run log), so a backend never starts or stops a process itself.

A project names its backend in ``trials.yaml``::

    backend: ros2          # the ROS 2 backend
    backend: process       # the built-in one: a shell command and a file recorder
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
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

ENV_BACKEND = "TRIALS_BACKEND"

#: The backend of a project that names none. While the ROS 2 code still lives inside this
#: package it is ``ros2``, so a project written before the backends existed runs unchanged.
DEFAULT_BACKEND = "ros2"

#: Backends that ship with this package, by the name a project writes.
BUILT_IN = {
    "process": "robofacts.backends.process",
    "ros2": "robofacts.backends.ros2",
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

    #: The word for one recording in printed messages ("bag" for ROS 2).
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
    #: Run once before the machine checks of a gate. The ROS 2 backend restarts the ros2
    #: daemon here, a backend without such a daemon keeps the default, which does nothing.
    gate_prepare: Callable[[], None] = field(default=lambda: None)
    #: Run once before the machine checks of a gate (a middleware daemon restart).
    gate_prepare: Callable[[], None] = field(default=lambda: None)


_LOADED: Dict[str, Backend] = {}


def configured_name(start: Optional[Any] = None) -> str:
    """The backend name in force: ``$TRIALS_BACKEND``, else ``backend:`` of the
    ``trials.yaml`` at or above ``start`` (default the working directory), else
    :data:`DEFAULT_BACKEND`."""
    from_env = os.environ.get(ENV_BACKEND, "").strip()
    if from_env:
        return from_env
    from . import roots
    try:
        value = roots.setting("backend", start=start)
    except Exception:  # noqa: BLE001 - an unreadable project file is reported by its own reader
        value = None
    return str(value).strip() if value else DEFAULT_BACKEND


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


def active(start: Optional[Any] = None) -> Backend:
    """The backend of the project at or above ``start`` (default the working directory)."""
    return load(configured_name(start))

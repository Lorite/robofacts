"""Robot adapters: what the framework knows about one kind of robot, enabled per project.

The core of the package names no robot. What only makes sense for one robot family lives
in an adapter, a folder that the project's backend ships (``<backend package>/adapters/<name>/``):

- ``checks/``: gate checks (``<name>.sh``), found after the project's own checks and before
  the core ones,
- ``record_profiles.yaml``: record-profile blocks the project's profiles may use,
- ``__init__.py``: the default ``NAMES`` the robot's topics are built from, the checks that
  are read-only probes (``PARALLEL_SAFE_CHECKS``) or must run alone (``SEQUENTIAL_CHECKS``),
  and the spoken sentence of a failed check (``GATE_SENTENCES``),
- helper modules, such as verify steps, imported as ``<backend package>.adapters.<name>.<module>``.

A project enables adapters in ``trials.yaml``::

    adapters: [my_drone, my_arm]

Nothing of a disabled adapter is in force: its checks are unknown (the error names the
adapter to enable), and its names and sentences do not apply.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, FrozenSet, List, Optional

class AdapterError(ValueError):
    """``trials.yaml`` names an adapter that does not exist, or ``adapters`` is malformed."""


def _package(start: Optional[Path] = None) -> Optional[ModuleType]:
    """The package that holds the adapters of the project's backend, or None when the
    backend has none (the process backend). ``start``: the experiments root (or any
    directory of the project) whose ``trials.yaml`` is read, default the working directory."""
    try:
        from .. import backend
        dotted = backend.active(start).adapters_package
    except Exception:  # noqa: BLE001 - run_preflight.py as a plain script, or an unknown backend
        return None
    return importlib.import_module(dotted) if dotted else None


def adapters_dir(start: Optional[Path] = None) -> Optional[Path]:
    package = _package(start)
    return Path(package.__file__).resolve().parent if package is not None else None


def available(start: Optional[Path] = None) -> tuple:
    """Every adapter the project's backend ships."""
    directory = adapters_dir(start)
    if directory is None:
        return ()
    return tuple(sorted(d.name for d in directory.iterdir()
                        if d.is_dir() and (d / "__init__.py").is_file()))


def __getattr__(name):
    """``AVAILABLE`` and ``ADAPTERS_DIR``, the constants from before the backends existed."""
    if name == "AVAILABLE":
        return available()
    if name == "ADAPTERS_DIR":
        return adapters_dir()
    raise AttributeError(name)


def _setting(start: Optional[Path] = None) -> Any:
    try:
        from .. import roots
    except ImportError:  # run_preflight.py run as a plain script, outside the package
        return []
    return roots.setting("adapters", start=start) or []


def enabled(start: Optional[Path] = None) -> List[str]:
    """The adapters the project enables, in the order ``trials.yaml`` lists them. ``start``:
    the experiments root of the project, default the working directory's project."""
    raw = _setting(start)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        raise AdapterError(f"trials.yaml: adapters must be a list of names, got {raw!r}")
    shipped = available(start)
    unknown = [a for a in raw if a not in shipped]
    if unknown:
        hint = "" if shipped else (" The project's backend ships no adapters: the robot adapters "
                                   "come with a backend, named by `backend:` in trials.yaml.")
        raise AdapterError(f"trials.yaml: unknown adapters {unknown}; the package ships "
                           f"{list(shipped)}.{hint}")
    return list(dict.fromkeys(raw))


def module(name: str, start: Optional[Path] = None) -> ModuleType:
    shipped = available(start)
    if name not in shipped:
        raise AdapterError(f"no adapter {name!r}; the package ships {list(shipped)}")
    return importlib.import_module(f"{_package(start).__name__}.{name}")


def enabled_files(relative: str, start: Optional[Path] = None) -> List[Path]:
    """``<adapter>/<relative>`` of each enabled adapter (present or not)."""
    names = enabled(start)
    return [adapters_dir(start) / name / relative for name in names]


def checks_dirs(start: Optional[Path] = None) -> List[Path]:
    """The ``checks/`` directories of the enabled adapters that have one."""
    return [d for d in enabled_files("checks", start) if d.is_dir()]


def owner_of_check(check: str, start: Optional[Path] = None) -> Optional[str]:
    """The adapter that ships the check ``check``, enabled or not (for error messages)."""
    for name in available(start):
        if (adapters_dir(start) / name / "checks" / f"{check}.sh").is_file():
            return name
    return None


def merged_dict(attribute: str, start: Optional[Path] = None) -> Dict[str, Any]:
    """One ``dict`` attribute of every enabled adapter, merged in the listed order."""
    out: Dict[str, Any] = {}
    for name in enabled(start):
        out.update(getattr(module(name, start), attribute, {}) or {})
    return out


def merged_set(attribute: str, start: Optional[Path] = None) -> FrozenSet[str]:
    """One ``set`` attribute of every enabled adapter, joined."""
    out: set = set()
    for name in enabled(start):
        out |= set(getattr(module(name, start), attribute, ()) or ())
    return frozenset(out)

"""Robot adapters: what the framework knows about one kind of robot, enabled per project.

The core of the package names no robot. What only makes sense for one robot family lives
in an adapter, a folder under this package:

- ``checks/``: gate checks (``<name>.sh``), found after the project's own checks and before
  the core ones,
- ``record_profiles.yaml``: record-profile blocks the project's profiles may use,
- ``__init__.py``: the default ``NAMES`` the robot's topics are built from, the checks that
  are read-only probes (``PARALLEL_SAFE_CHECKS``) or must run alone (``SEQUENTIAL_CHECKS``),
  and the spoken sentence of a failed check (``GATE_SENTENCES``),
- helper modules, such as verify steps, imported as ``robofacts.adapters.<name>.<module>``.

A project enables adapters in ``trials.yaml``::

    adapters: [crazyflie, spot]

Nothing of a disabled adapter is in force: its checks are unknown (the error names the
adapter to enable), and its names and sentences do not apply.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, FrozenSet, List, Optional

ADAPTERS_DIR = Path(__file__).resolve().parent

#: Every adapter this package ships.
AVAILABLE = ("crazyflie", "px4", "spot")


class AdapterError(ValueError):
    """``trials.yaml`` names an adapter that does not exist, or ``adapters`` is malformed."""


def _setting() -> Any:
    try:
        from .. import roots
    except ImportError:  # run_preflight.py run as a plain script, outside the package
        return []
    return roots.setting("adapters") or []


def enabled() -> List[str]:
    """The adapters the project enables, in the order ``trials.yaml`` lists them."""
    raw = _setting()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        raise AdapterError(f"trials.yaml: adapters must be a list of names, got {raw!r}")
    unknown = [a for a in raw if a not in AVAILABLE]
    if unknown:
        raise AdapterError(f"trials.yaml: unknown adapters {unknown}; the package ships "
                           f"{list(AVAILABLE)}")
    return list(dict.fromkeys(raw))


def module(name: str) -> ModuleType:
    if name not in AVAILABLE:
        raise AdapterError(f"no adapter {name!r}; the package ships {list(AVAILABLE)}")
    return importlib.import_module(f"{__name__}.{name}")


def enabled_files(relative: str) -> List[Path]:
    """``<adapter>/<relative>`` of each enabled adapter (present or not)."""
    return [ADAPTERS_DIR / name / relative for name in enabled()]


def checks_dirs() -> List[Path]:
    """The ``checks/`` directories of the enabled adapters that have one."""
    return [d for d in enabled_files("checks") if d.is_dir()]


def owner_of_check(check: str) -> Optional[str]:
    """The adapter that ships the check ``check``, enabled or not (for error messages)."""
    for name in AVAILABLE:
        if (ADAPTERS_DIR / name / "checks" / f"{check}.sh").is_file():
            return name
    return None


def merged_dict(attribute: str) -> Dict[str, Any]:
    """One ``dict`` attribute of every enabled adapter, merged in the listed order."""
    out: Dict[str, Any] = {}
    for name in enabled():
        out.update(getattr(module(name), attribute, {}) or {})
    return out


def merged_set(attribute: str) -> FrozenSet[str]:
    """One ``set`` attribute of every enabled adapter, joined."""
    out: set = set()
    for name in enabled():
        out |= set(getattr(module(name), attribute, ()) or ())
    return frozenset(out)

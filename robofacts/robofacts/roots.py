"""Where the experiments live, and where the project is.

The framework used to find its experiments root from its own file location
(``experiments/common/scripts/`` is two levels below ``experiments/``) and the
project root from a hardcoded dev-container path. Neither works once the code is
an installed package, so every command resolves the two roots here, in this
order (first answer wins):

1. an explicit value: the ``--experiments-root`` / ``--project-root`` flag, or
   the argument a caller passes to a function,
2. the environment: ``TRIALS_EXPERIMENTS_ROOT`` / ``TRIALS_PROJECT_ROOT``,
3. a ``trials.yaml`` file found by walking up from the current directory, with
   the keys ``experiments_root`` and ``project_root`` (relative paths are
   relative to that file),
4. the layout: walking up from the current directory, the first directory that
   is an experiments root (it holds ``common/config/trial_metadata_core.yaml``)
   or that contains one named ``experiments/``,
5. otherwise a :class:`RootNotFound` error that says how to set it.

The project root defaults to the parent of the experiments root. It is the
working directory of the commands a runner.yaml declares and the checkout the
code-version probe stamps into every trial.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator, Optional, Union

ENV_EXPERIMENTS_ROOT = "TRIALS_EXPERIMENTS_ROOT"
ENV_PROJECT_ROOT = "TRIALS_PROJECT_ROOT"
PROJECT_FILE = "trials.yaml"
#: The file whose presence makes a directory an experiments root.
CORE_SCHEMA_REL = Path("common") / "config" / "trial_metadata_core.yaml"
EXPERIMENTS_DIR_NAME = "experiments"

PathLike = Union[str, "os.PathLike[str]"]


class RootNotFound(RuntimeError):
    """No experiments root could be found. The message says how to set one."""


def is_experiments_root(path: PathLike) -> bool:
    """True when ``path`` holds the shared metadata contract."""
    return (Path(path) / CORE_SCHEMA_REL).is_file()


def find_project_file(start: Optional[PathLike] = None) -> Optional[Path]:
    """The nearest ``trials.yaml`` at or above ``start`` (default: cwd)."""
    here = Path(start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def _read_project_file(path: Path) -> dict:
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RootNotFound(f"{path}: expected a mapping with experiments_root and project_root")
    return raw


def _project_file_in_force(start: Optional[PathLike]) -> Optional[Path]:
    """The project file the settings come from: the one at ``$TRIALS_PROJECT_ROOT`` when
    that is set and holds one (an explicit root beats the layout), else the nearest one
    at or above ``start``. Without ``start``, the nearest one at or above
    ``$TRIALS_EXPERIMENTS_ROOT`` when that is set and has one, else at or above the working
    directory: the settings belong to the project whose experiments are in force, not to
    the project around the working directory (found 2026-11-25: a gate started from inside
    another project read that project's ``adapters:``)."""
    env_root = os.environ.get(ENV_PROJECT_ROOT)
    if env_root and (Path(env_root).expanduser() / PROJECT_FILE).is_file():
        return (Path(env_root).expanduser() / PROJECT_FILE).resolve()
    if start is None:
        env_experiments = os.environ.get(ENV_EXPERIMENTS_ROOT)
        if env_experiments:
            found = find_project_file(Path(env_experiments).expanduser())
            if found is not None:
                return found
    return find_project_file(start)


def _from_project_file(key: str, start: Optional[PathLike]) -> Optional[Path]:
    project_file = _project_file_in_force(start)
    if project_file is None:
        return None
    value = _read_project_file(project_file).get(key)
    if not value:
        return None
    return (project_file.parent / str(value)).resolve()


def _by_layout(start: Optional[PathLike]) -> Optional[Path]:
    here = Path(start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if is_experiments_root(directory):
            return directory
        nested = directory / EXPERIMENTS_DIR_NAME
        if is_experiments_root(nested):
            return nested
    return None


def experiments_root(explicit: Optional[PathLike] = None, *,
                     start: Optional[PathLike] = None) -> Path:
    """Resolve the experiments root (see the module docstring for the order)."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_EXPERIMENTS_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    found = _from_project_file("experiments_root", start) or _by_layout(start)
    if found is not None:
        return found
    raise RootNotFound(
        "no experiments root found. Run the command inside a project (a directory that holds "
        f"'{EXPERIMENTS_DIR_NAME}/{CORE_SCHEMA_REL}'), or pass --experiments-root, or set "
        f"{ENV_EXPERIMENTS_ROOT}, or write a {PROJECT_FILE} with 'experiments_root: <path>' at the "
        "project root. 'trials init <dir>' creates a new project.")


def project_root(explicit: Optional[PathLike] = None, *,
                 experiments: Optional[PathLike] = None,
                 start: Optional[PathLike] = None) -> Path:
    """Resolve the project root: explicit, environment, trials.yaml, else the
    parent of the experiments root.

    The trials.yaml is the one of the given experiments root when ``start`` is not given:
    the project whose experiments run, not the project around the working directory. Before
    the second review of 2026-11-25, ``trials ui --experiments-root X`` and ``trials roots``
    started inside another project took that project's root and backend."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_PROJECT_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    if start is None and experiments:
        start = Path(experiments).expanduser()
    found = _from_project_file("project_root", start)
    if found is not None:
        return found
    return experiments_root(experiments, start=start).parent


def project_settings(start: Optional[PathLike] = None) -> dict:
    """The project's settings: every key of the nearest ``trials.yaml`` (at or above
    ``start``, default the working directory, or the file at ``$TRIALS_PROJECT_ROOT``)
    besides the two roots. Empty when there is no project file. A project keeps here
    what used to be a lab constant in the code: ``stop_latch`` (the path every stop
    command writes first) and ``record_profiles`` (the robot, camera and node names the
    named record profiles are built from)."""
    project_file = _project_file_in_force(start)
    if project_file is None:
        return {}
    raw = _read_project_file(project_file)
    return {k: v for k, v in raw.items() if k not in ("experiments_root", "project_root")}


def setting(key: str, default=None, start: Optional[PathLike] = None):
    """One project setting, or ``default`` when the project file does not set it."""
    value = project_settings(start).get(key)
    return default if value is None else value


@contextlib.contextmanager
def exported_experiments_root(path: PathLike) -> Iterator[None]:
    """``$TRIALS_EXPERIMENTS_ROOT`` set to ``path`` while the block runs, and restored
    afterwards. Inside it, a setting read without a ``start`` comes from the project of
    ``path`` (see _project_file_in_force), and the processes started inherit the root."""
    old = os.environ.get(ENV_EXPERIMENTS_ROOT)
    os.environ[ENV_EXPERIMENTS_ROOT] = str(Path(path).expanduser().resolve())
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(ENV_EXPERIMENTS_ROOT, None)
        else:
            os.environ[ENV_EXPERIMENTS_ROOT] = old


def add_root_arguments(parser, *, project: bool = False) -> None:
    """``--experiments-root`` (and optionally ``--project-root``) on an argparse parser.
    Both default to None, so :func:`experiments_root` resolves them after parsing."""
    parser.add_argument("--experiments-root", type=Path, default=None,
                        help=f"the experiments directory (default: {ENV_EXPERIMENTS_ROOT}, "
                             f"{PROJECT_FILE}, or the nearest experiments/ directory)")
    if project:
        parser.add_argument("--project-root", type=Path, default=None,
                            help=f"the working directory of the runner's commands (default: "
                                 f"{ENV_PROJECT_ROOT}, {PROJECT_FILE}, or the parent of the "
                                 f"experiments root)")


def resolve_root_arguments(args) -> None:
    """Fill ``args.experiments_root`` (and ``args.project_root`` when present) in place."""
    args.experiments_root = experiments_root(getattr(args, "experiments_root", None))
    if hasattr(args, "project_root"):
        args.project_root = project_root(args.project_root, experiments=args.experiments_root)


def module_root_attribute(name: str):
    """The lazy ``EXPERIMENTS_ROOT`` / ``REPO_ROOT`` a module exposes for callers
    that still read the old constants. Resolved on every access, never cached,
    so a test that sets the environment variable sees its own root."""
    if name == "EXPERIMENTS_ROOT":
        return experiments_root()
    if name in ("REPO_ROOT", "PROJECT_ROOT"):
        return project_root()
    raise AttributeError(name)

"""The shell tools the package ships: the clean-slate kill, the e-stop and the
robot actions. They live in ``tools/`` next to this module."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

TOOLS_DIR = Path(__file__).resolve().parent / "tools"


def tool_dirs() -> List[Path]:
    """Where the tools are: this package's, then the ones the project's backend ships."""
    dirs = [TOOLS_DIR]
    try:
        from . import backend
        extra = backend.active().tools_dir
    except Exception:  # noqa: BLE001 - an unknown backend has no tools
        extra = None
    if extra is not None:
        dirs.append(Path(extra))
    return dirs


def available() -> List[str]:
    """The names of the packaged tools, for ``trials tool`` to list."""
    return sorted({p.name for d in tool_dirs() for p in d.glob("*.sh") if p.is_file()})


def path(name: str) -> Path:
    """The absolute path of a packaged tool, for example ``estop.sh``."""
    for directory in tool_dirs():
        tool = directory / name
        if tool.is_file():
            return tool
    raise FileNotFoundError(f"{name} is not a packaged tool (looked in "
                            f"{', '.join(str(d) for d in tool_dirs())}; the tools are {', '.join(available())})")


def remote_invocation(name: str, args: Optional[List[str]] = None) -> str:
    """How to start a packaged tool on another machine, where the package is installed
    but its path is unknown here. The backend says how ``trials`` starts there."""
    from . import backend
    return " ".join([*backend.active().cli, "tool", name, *(args or [])])


def run(name: str, args: Optional[List[str]] = None, **kwargs) -> subprocess.CompletedProcess:
    """Run a packaged tool with ``args``; ``kwargs`` go to :func:`subprocess.run`."""
    return subprocess.run(["bash", str(path(name)), *(args or [])], **kwargs)


def exec_tool(name: str, args: List[str]) -> int:
    """Replace this process with the tool (the ``trials tool`` subcommand)."""
    tool = path(name)
    os.execvp("bash", ["bash", str(tool), *args])
    return 1  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    sys.exit(exec_tool(sys.argv[1], sys.argv[2:]))

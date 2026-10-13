"""The shell tools the package ships: the clean-slate kill, the e-stop and the
robot actions. They live in ``tools/`` next to this module."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

TOOLS_DIR = Path(__file__).resolve().parent / "tools"


def path(name: str) -> Path:
    """The absolute path of a packaged tool, for example ``estop.sh``."""
    tool = TOOLS_DIR / name
    if not tool.is_file():
        raise FileNotFoundError(f"{name} is not a packaged tool (looked in {TOOLS_DIR})")
    return tool


def remote_invocation(name: str, args: Optional[List[str]] = None) -> str:
    """How to start a packaged tool on another machine, where this package is
    installed in the sourced ROS 2 workspace but its path is unknown here."""
    return " ".join(["ros2", "run", "declarative_robot_trials", "trials", "tool", name, *(args or [])])


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

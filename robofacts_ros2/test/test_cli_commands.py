"""The three command defects of 2026-10-14: `trials tool --help` raised instead of listing the
tools, `trials lint -q` rejected the pytest flags its help promised, and `trials ui --help`
imported Flask before printing help, so it failed on a host without Flask."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from robofacts import cli, tools

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE_ROOT / "example_project"
#: Both packages, for a child process that runs them from this source tree.
SOURCE_PATH = os.pathsep.join([str(PACKAGE_ROOT.parent / "robofacts"), str(PACKAGE_ROOT)])


def run_cli(*args):
    env = {**os.environ, "PYTHONPATH": SOURCE_PATH + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run([sys.executable, "-m", "robofacts.cli", *args], cwd=EXAMPLE,
                          capture_output=True, text=True, env=env, timeout=600)


def test_tool_help_lists_the_packaged_tools():
    out = run_cli("tool", "--help")
    assert out.returncode == 0, out.stderr
    for name in ("estop.sh", "kill_ros.sh", "robot_action.sh"):
        assert name in out.stdout
    assert "Traceback" not in out.stderr


def test_tool_without_a_name_and_with_an_unknown_name_fail_cleanly():
    out = run_cli("tool")
    assert out.returncode == 2 and "kill_ros.sh" in out.stderr and "Traceback" not in out.stderr
    out = run_cli("tool", "no_such_tool.sh")
    assert out.returncode == 2 and "no_such_tool.sh is not a packaged tool" in out.stderr
    assert "estop.sh" in out.stderr and "Traceback" not in out.stderr


def test_available_tools_are_the_shipped_scripts():
    # The core's tools, and the ones the project's backend ships (the ROS 2 clean slate).
    shipped = {p.name for d in tools.tool_dirs() for p in d.glob("*.sh")}
    assert tools.available() == sorted(shipped)
    assert "estop.sh" in tools.available() and "kill_ros.sh" in tools.available()


@pytest.mark.parametrize("argv", [["lint", "--collect-only", "-q"], ["lint", "--", "--collect-only", "-q"]])
def test_lint_passes_pytest_flags_through(argv):
    out = run_cli(*argv)
    assert "unrecognized arguments" not in out.stderr
    assert out.returncode == 0, out.stdout + out.stderr
    assert "lint.py" in out.stdout  # the collected cases, as "lint.py: 18" with -q twice or as ids with -q once



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
EXAMPLE = PACKAGE_ROOT.parent / "robofacts_ros2" / "example_project"


def run_cli(*args):
    env = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
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
    assert tools.available() == sorted(p.name for p in tools.TOOLS_DIR.glob("*.sh"))
    assert "estop.sh" in tools.available()


@pytest.mark.parametrize("argv", [["lint", "--collect-only", "-q"], ["lint", "--", "--collect-only", "-q"]])
def test_lint_passes_pytest_flags_through(argv):
    out = run_cli(*argv)
    assert "unrecognized arguments" not in out.stderr
    assert out.returncode == 0, out.stdout + out.stderr
    assert "lint.py" in out.stdout  # the collected cases, as "lint.py: 18" with -q twice or as ids with -q once


def test_ui_help_needs_no_flask(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "flask", None)  # `import flask` now raises ImportError
    monkeypatch.delitem(sys.modules, "robofacts.ui.app", raising=False)
    import robofacts.ui as ui_package
    monkeypatch.delattr(ui_package, "app", raising=False)
    assert cli.main(["ui", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--no-auth" in out


def test_ui_without_flask_says_so_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "flask", None)
    monkeypatch.delitem(sys.modules, "robofacts.ui.app", raising=False)
    # The package keeps the submodule as an attribute once any test imported it, and
    # `from .ui import app` returns that attribute without importing. The test then
    # started the real server and the whole suite hung on port 8080 (2026-10-14).
    import robofacts.ui as ui_package
    monkeypatch.delattr(ui_package, "app", raising=False)
    assert cli.main(["ui", "--no-auth"]) == 2
    assert "needs Flask" in capsys.readouterr().err

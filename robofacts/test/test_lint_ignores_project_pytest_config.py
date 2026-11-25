"""The lint never reads the project's pytest configuration or its conftest.py files.

When the package is installed in a virtual environment inside the project (project/.venv),
pytest finds the project's pytest.ini and conftest.py above the lint file. Both would then
change the lint: a conftest.py runs project code, and addopts can break the run. The MCP
server promises to be read-only, so the lint must not depend on either.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"


@pytest.fixture()
def hostile_project(tmp_path):
    """A copy of the example whose own pytest setup writes a marker and breaks addopts,
    with the package installed below it (project/.venv/site/robofacts)."""
    root = tmp_path / "project"
    shutil.copytree(EXAMPLE, root)
    site = root / ".venv" / "site"
    site.mkdir(parents=True)
    shutil.copytree(PACKAGE / "robofacts", site / "robofacts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    marker = tmp_path / "conftest_ran"
    (root / "conftest.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\naddopts = --no-such-option-of-the-project\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "--cov=src"\n', encoding="utf-8")
    return root, site, marker


def _run(code: str, root: Path, site: Path):
    env = {**os.environ, "PYTHONPATH": str(site), "TRIALS_PROJECT_ROOT": str(root),
           "TRIALS_EXPERIMENTS_ROOT": str(root / "experiments"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, env=env)


def test_trials_lint_ignores_the_conftest_and_the_addopts_of_the_project(hostile_project):
    root, site, marker = hostile_project
    out = _run("import sys; from robofacts.cli import main; sys.exit(main(['lint']))", root, site)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert not marker.exists(), "the project's conftest.py ran during the lint"


def test_the_validate_helper_used_by_the_mcp_tools_ignores_them_too(hostile_project):
    root, site, marker = hostile_project
    code = ("from pathlib import Path; from robofacts import lint, validate as v\n"
            f"rc, out, summary = v._pytest([lint.__file__], Path({str(root / 'experiments')!r}))\n"
            "print(rc); print(summary)\n"
            "raise SystemExit(rc)")
    out = _run(code, root, site)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert not marker.exists(), "the project's conftest.py ran during the lint"

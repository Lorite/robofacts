"""Lint rule L6: every experiment declares a verify step or a written reason for having none.

Until 2026-11-24 the rule looked for flight words in the action and in the factor levels, so
only an experiment that flies had to verify its recording. A ground robot whose action exits 0
while the robot stands still produces the same false trial, so the rule binds every experiment.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from robofacts import scaffold

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def lint():
    """The lint module. It resolves an experiments root when it is imported (pytest builds
    its cases then), so it is imported here with the example project as the root, not at
    the top of this file, where no project may be in sight."""
    saved = {name: os.environ.get(name) for name in ("TRIALS_EXPERIMENTS_ROOT", "TRIALS_PROJECT_ROOT")}
    os.environ["TRIALS_EXPERIMENTS_ROOT"] = str(PACKAGE / "example_project" / "experiments")
    os.environ["TRIALS_PROJECT_ROOT"] = str(PACKAGE / "example_project")
    try:
        from robofacts import lint as module
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return module
STEP = [{"name": "moved", "command": "python3 check.py {bag}"}]


def test_a_verify_step_satisfies_the_rule(lint):
    assert lint.verify_step_problem({"verify_steps": STEP}) is None


def test_no_verify_step_and_no_reason_is_a_problem_whatever_the_action_says(lint):
    for action in ("python3 fly_trajectory.py", "python3 drive_route.py", "python3 listen.py", ""):
        raw = {"action": {"command": action}} if action else {}
        problem = lint.verify_step_problem(raw)
        assert problem and "declares no verify_steps" in problem and lint.NO_VERIFY_KEY in problem


def test_an_empty_list_is_no_verify_step(lint):
    assert lint.verify_step_problem({"verify_steps": []})


def test_a_written_reason_satisfies_the_rule(lint):
    assert lint.verify_step_problem({lint.NO_VERIFY_KEY: "a bench recording, nothing moves"}) is None


@pytest.mark.parametrize("reason", [True, "", "   ", None, 0, ["because"]])
def test_a_reason_must_be_a_sentence(lint, reason):
    problem = lint.verify_step_problem({lint.NO_VERIFY_KEY: reason})
    assert problem and "must be a sentence" in problem


def test_verify_steps_and_a_reason_together_contradict(lint):
    problem = lint.verify_step_problem({"verify_steps": STEP, lint.NO_VERIFY_KEY: "not needed"})
    assert problem and "together" in problem


def test_the_rule_names_no_kind_of_robot(lint):
    """The rule must not depend on what the robot is: no flight word decides it."""
    source = Path(lint.__file__).read_text(encoding="utf-8")
    body = source[source.index("def verify_step_problem"):source.index("def test_commands_reference_scripts_that_exist")]
    for word in ("fly", "flies", "flight", "trajector", "drone"):
        assert word not in body, f"L6 reads the word {word!r} again"


def _lint(project: Path, experiment: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "robofacts.cli", "lint", "--experiments-root", str(project / "experiments"),
         "--experiment", experiment, "-k", "test_every_experiment_declares_a_verify_step"],
        capture_output=True, text=True, cwd=project,
        env={**os.environ, "PYTHONPATH": str(PACKAGE)})


def test_the_lint_fails_a_generated_experiment_that_loses_its_verify_steps(lint, tmp_path):
    """End to end on a generated project: pass as generated, fail without verify steps,
    pass again with a written reason."""
    project = tmp_path / "proj"
    assert scaffold.main_init([str(project), "--experiment", "rover"]) == 0
    runner = project / "experiments" / "rover" / "config" / "runner.yaml"
    assert _lint(project, "rover").returncode == 0

    raw = yaml.safe_load(runner.read_text(encoding="utf-8"))
    raw.pop("verify_steps")
    runner.write_text(yaml.safe_dump(raw), encoding="utf-8")
    out = _lint(project, "rover")
    assert out.returncode != 0 and "declares no verify_steps" in out.stdout

    raw[lint.NO_VERIFY_KEY] = "a recording of a parked robot, nothing can fail to happen"
    runner.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert _lint(project, "rover").returncode == 0

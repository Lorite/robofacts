"""A placeholder typo in a verify step fails before anything runs (2026-11-25).

A verify step is rendered only after its trial recorded, because it gets the recording's
directory. A typo in one (``{rates}`` for ``{rate}``) passed ``trials validate`` ("READY ...
every command resolved") and the dry run, and then every executed trial ran its stack and
its recorder and stopped with "unknown factor 'rates'". The runner now renders every command
of every planned attempt before the first one starts.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import trial_index as ti
from robofacts import trial_runner as tr
from robofacts import validate

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"
EXPERIMENT = "demo_log_recorder"
TYPO_STEP = """
verify_steps:
  - name: rate_matches
    command: "python3 experiments/demo_log_recorder/scripts/verify_log.py {recording} --rate {rates}"
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    shutil.copytree(EXAMPLE, root)                      # the links become files
    campaign = root / "experiments" / EXPERIMENT / "campaigns" / "grid.yaml"
    campaign.write_text(campaign.read_text(encoding="utf-8") + TYPO_STEP, encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(root / "experiments"))
    monkeypatch.setenv("PYTHONPATH", str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("TRIALS_VOICE", "off")
    ti._recorder_at.cache_clear()
    yield root / "experiments"
    ti._recorder_at.cache_clear()


def test_trials_validate_fails_the_dry_run_on_a_verify_step_typo(project):
    report = validate.validate(EXPERIMENT, project, skip=("lint", "tests", "gate"))
    assert not report.ready
    (dry_run,) = [r for r in report.results if r.stage == "dry_run"]
    assert dry_run.status == "FAIL"
    assert "verify step rate_matches references unknown factor 'rates'" in dry_run.output


def test_an_executed_campaign_with_the_typo_runs_and_records_nothing(project, capsys):
    campaign = project / EXPERIMENT / "campaigns" / "grid.yaml"
    # `trials run` ends the refusal with its message and exit code 1, not a traceback.
    rc = tr.main([str(campaign), "--experiments-root", str(project), "--no-pause", "--execute"])
    assert rc == 1 and "unknown factor 'rates'" in capsys.readouterr().out
    recordings = project / EXPERIMENT / "recordings"
    assert not [d for d in recordings.iterdir() if d.is_dir()]
    assert not (project / EXPERIMENT / "runs" / tr.ATTEMPT_LEDGER_NAME).exists()


def test_every_attempt_is_rendered_and_each_problem_is_named_once(project):
    campaign = cs.load_campaign(project / EXPERIMENT / "campaigns" / "grid.yaml", project, [])
    config = tr.RunnerConfig.load(EXPERIMENT, project)
    plan = cp.build_plan(campaign, [], False)
    problems = tr.plan_command_problems(plan, config, project / EXPERIMENT / "recordings",
                                        extra_verify=campaign.verify_steps)
    assert len(problems) == 1 and "'rates'" in problems[0]
    # A trial without a recording never runs its verify steps, so they are not rendered.
    assert tr.plan_command_problems(plan, config, project, extra_verify=campaign.verify_steps,
                                    steps={"record": False}) == []

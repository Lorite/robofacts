"""A trial run with a step turned off says so in its metadata and in its record (2026-11-25).

The web interface's steps form can turn off the setup, the launch or the action of a trial.
A trial whose action never ran was written with ``aborted: false`` like a full one, and
nothing in its record said that a step was missing. The runner now writes
``steps_disabled`` into trial_metadata.json and ``trials records`` shows it. Whether such a
trial counts is not changed here.
"""
from __future__ import annotations

import json

import pytest

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import trial_index as ti
from robofacts import trial_records as trc
from robofacts import trial_runner as tr

from test_process_backend import use_project, write_project


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    yield experiments
    ti._recorder_at.cache_clear()


def _run(experiments, steps):
    campaign = cs.load_campaign(experiments / "plant" / "campaigns" / "rates.yaml", experiments, [])
    config = tr.RunnerConfig.load("plant", experiments)
    plan = cp.build_plan(campaign, ti.load_trials(experiments, "plant"), False)
    return tr.run_plan(plan, config, experiments / "plant" / "recordings", dry_run=False, limit=1,
                       steps=steps, hold_s=1.0, pause_before_action=False)


@pytest.mark.real_sleep
def test_a_trial_without_its_action_records_that_the_action_did_not_run(project, capsys):
    (result,) = _run(project, {"action": False})
    assert result.status == "ran", result.detail
    metadata = json.loads((result.bag_dir / "trial_metadata.json").read_text(encoding="utf-8"))
    assert metadata["steps_disabled"] == ["action"]
    assert metadata.get("aborted") is not True     # whether it counts is not decided here
    capsys.readouterr()                             # the run's own output
    assert trc.main(["plant", "--experiments-root", str(project)]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if result.bag_dir.name in l)
    assert "steps_disabled=action" in line
    (record,) = [r for r in trc.load_records(project, "plant") if r.name == result.bag_dir.name]
    assert record.to_dict()["steps_disabled"] == ["action"]


@pytest.mark.real_sleep
def test_a_full_trial_writes_no_steps_disabled(project, capsys):
    (result,) = _run(project, None)
    metadata = json.loads((result.bag_dir / "trial_metadata.json").read_text(encoding="utf-8"))
    assert "steps_disabled" not in metadata
    (record,) = trc.load_records(project, "plant")
    assert record.steps_disabled == [] and "steps_disabled" not in trc.render([record])

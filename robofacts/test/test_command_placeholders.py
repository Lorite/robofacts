"""A placeholder value is one argument, whatever characters it holds (2026-11-25).

The commands of runner.yaml, the campaigns and analyses.yaml were filled with ``str.format``
and then split with ``shlex``. A value therefore became shell syntax: a project in a folder
whose name has a space made ``{recording}`` two arguments and every trial of the shipped
example aborted in its verify step, a level with an apostrophe raised ValueError, and a value
read back from the ledger by ``trials reverify`` could add arguments. Now the template is split
first and each argument is filled, so a value stays inside its own argument.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from robofacts import analysis_runner as ar
from robofacts import campaign_spec as cs
from robofacts import trial_index as ti
from robofacts import trial_reverify as rv
from robofacts import trial_runner as tr
from robofacts.backends import process as pb

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"
EXPERIMENT = "demo_log_recorder"


def test_a_value_stays_inside_the_argument_its_placeholder_sits_in():
    argv = cs.render_argv('run.py {path} --rate={rate} --note "x {rate} y"',
                          {"path": "/a b/c", "rate": "it's fast"})
    assert argv == ["run.py", "/a b/c", "--rate=it's fast", "--note", "x it's fast y"]


def test_a_list_value_that_is_a_whole_argument_becomes_one_argument_per_item():
    """``{bags}`` of a cohort analysis: the lab's analyses rely on one argument per bag."""
    assert cs.render_argv("cohort.py {bags} --out {results}",
                          {"bags": ["/r/b 1", "/r/b2"], "results": "/o"}) == \
        ["cohort.py", "/r/b 1", "/r/b2", "--out", "/o"]


def test_a_list_value_inside_a_larger_argument_is_joined_with_spaces():
    """A3-6 (round 3): inside a larger argument the list was written as a Python list
    (``--bags=['/r/t1', '/r/t2']``). Before round 2 the bags were space-joined there."""
    values = {"bags": ["/r/t1", "/r/t2"], "results": "/o"}
    assert cs.render_argv('bash -c "python3 a.py {bags} | tee {results}/log"', values) == \
        ["bash", "-c", "python3 a.py /r/t1 /r/t2 | tee /o/log"]
    assert cs.render_argv("a.py --bags={bags}", values) == ["a.py", "--bags=/r/t1 /r/t2"]


def test_a_cohort_analysis_in_a_shell_pipe_gets_every_bag(tmp_path):
    bags = [ti.Trial(experiment=EXPERIMENT, bag_dir=tmp_path / name, metadata={})
            for name in ("t1", "t2")]
    cohort = ar.Analysis(name="c", command='bash -c "an.py {bags} | tee {results}/log"',
                         per="cohort")
    result = ar.run_analysis(cohort, bags, EXPERIMENT, tmp_path, dry_run=True)
    assert result.commands[0][2].startswith(f"an.py {tmp_path / 't1'} {tmp_path / 't2'} | tee ")


def test_an_unknown_placeholder_still_raises_key_error():
    with pytest.raises(KeyError):
        cs.render_argv("run.py {rates}", {"rate": "slow"})


def _trial(rate: str) -> cs.TrialSpec:
    return cs.TrialSpec(EXPERIMENT, {"rate": rate, "duration": "short"}, ["rate"])


def test_a_level_with_an_apostrophe_is_one_argument_of_every_runner_command():
    config = tr.RunnerConfig.load(EXPERIMENT, EXAMPLE / "experiments")
    trial = _trial("it's fast")
    recording = Path("/data/my project/recordings/t1")
    (step,) = config.verify_steps
    verify = tr.templated_command(step["command"], trial, "verify step",
                                  **tr.recording_placeholders(recording, trial.values))
    assert verify[verify.index("--rate") + 1] == "it's fast"
    assert str(recording) in verify
    action = tr.action_command(
        cs.TrialSpec(EXPERIMENT, {"rate": "slow", "duration": "it's long"}, ["duration"]), config)
    assert action[action.index("--duration") + 1] == "it's long"
    launch = pb.ProcessLauncher().command(config.stack_spec(), {}, dict(trial.values))
    assert launch[launch.index("--rate") + 1] == "it's fast"


def test_reverify_renders_a_ledger_value_as_one_argument():
    steps = [{"name": "check", "command": "verify.py {bag} --rate {rate}"}]
    values = rv.placeholders({"rate": "slow --min-lines 0"}, {}, Path("/r/my bag"))
    ((name, command),) = rv.render_steps(steps, values)
    seen = []
    rv.run_steps([(name, command)], Path("."),
                 run=lambda argv, **kw: seen.append(argv) or type("C", (), {"returncode": 0})())
    assert seen == [["verify.py", "/r/my bag", "--rate", "slow --min-lines 0"]]


def test_an_analysis_keeps_a_bag_path_with_a_space_whole(tmp_path):
    bag_dir = tmp_path / "my bags" / "t1"
    trial = ti.Trial(experiment=EXPERIMENT, bag_dir=bag_dir, metadata={})
    per_bag = ar.Analysis(name="a", command="an.py {bag} --out {results}")
    result = ar.run_analysis(per_bag, [trial], EXPERIMENT, tmp_path, dry_run=True)
    assert result.commands[0][1] == str(bag_dir)
    cohort = ar.Analysis(name="c", command="an.py {bags} --out {results}", per="cohort")
    result = ar.run_analysis(cohort, [trial, trial], EXPERIMENT, tmp_path, dry_run=True)
    assert result.commands[0][1:3] == [str(bag_dir), str(bag_dir)]


@pytest.mark.real_sleep
def test_the_example_runs_a_trial_in_a_folder_whose_name_has_a_space(tmp_path, monkeypatch, capsys):
    root = tmp_path / "my project"
    shutil.copytree(EXAMPLE, root)                      # the links become files
    experiments = root / "experiments"
    monkeypatch.chdir(root)
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(experiments))
    monkeypatch.setenv("PYTHONPATH", str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("TRIALS_VOICE", "off")
    ti._recorder_at.cache_clear()
    try:
        rc = tr.main([str(experiments / EXPERIMENT / "campaigns" / "grid.yaml"),
                      "--experiments-root", str(experiments), "--no-pause", "--execute",
                      "--limit", "1"])
        out = capsys.readouterr().out
        assert rc == 0, out[-3000:]
        assert "1 trial(s): 1 OK" in out
        assert "[verify] log_was_recorded rc=0" in out
        (trial,) = ti.load_trials(experiments, EXPERIMENT)
        assert trial.status == "usable"
    finally:
        ti._recorder_at.cache_clear()

"""First and repeat trials of one runner invocation (2026-11-04).

A repeated trial of a campaign rarely needs the whole setup again (a fresh battery and a
re-placement of the drone for a 16 s square), but it may need a reset between trials (the
Crazyflie's radio power cycle). The rules these tests protect:

* an operator or setup step's ``when`` is first, repeat or always (default always);
* the first trial of a run gets the ``first`` and ``always`` steps, every later trial the
  ``repeat`` and ``always`` ones, and a trial after a BLOCKED one starts over as first;
* a repeat trial whose operator steps all drop out still pauses once (a ready step);
* campaign setup steps run after runner.yaml's, before the stack launches;
* the T-0 gate still runs on every trial.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import trial_runner as tr

CORE = """
version: 1
factors:
  execution_host:
    levels: [laptop, orin]
    default: orin
    metadata_key: execution_host
    maps_to: {kind: host}
  record_profile:
    levels: [position_only]
    default: position_only
    metadata_key: record_profile
    maps_to: {kind: record_profile}
  trajectory:
    levels: [hover, circle]
    default: hover
    metadata_key: trajectory
    maps_to: {kind: launch_arg, name: trajectory}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  record_profile: {required: false, type: str, factor: true}
  trajectory: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

RUNNER = """
launch:
  package: lorite_experiments
  file: flight.launch.py
action:
  command: "python3 fly.py --shape {trajectory}"
setup_steps:
  - name: clean_slate
    command: bash kill_ros.sh
  - name: first_only_calibration
    command: python3 calibrate.py
    when: first
cf_name: cf1
hosts:
  laptop: {local: true}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 2
sweep:
  trajectory: [hover]
fixed:
  execution_host: laptop
operator_steps:
  - text: "Fresh battery, drone on the mark BEFORE the battery goes in."
    when: first
  - text: "The drone stays where it landed. Area clear, e-stop ready."
    when: repeat
  - "Prop guards on."
setup_steps:
  - name: radio_power_cycle
    command: python3 power_cycle.py {trajectory}
    when: repeat
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "common" / "config" / "record_profiles.yaml").write_text(
        "profiles: {position_only: {topics: [/poses, /tf, /rosout]}}\n", encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    return tmp_path


def campaign_file(root: Path, text: str) -> Path:
    path = root / "expA" / "campaigns" / "x.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The spec
# --------------------------------------------------------------------------- #
def test_the_spec_reads_when_and_campaign_setup_steps(root):
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    assert [s["when"] for s in c.operator_steps] == ["first", "repeat", "always"]
    assert c.setup_steps == [{"name": "radio_power_cycle", "command": "python3 power_cycle.py {trajectory}",
                              "required": True, "when": "repeat"}]
    assert [s["text"][:5] for s in cs.steps_for(c.operator_steps, repeat=False)] == ["Fresh", "Prop "]
    assert [s["text"][:5] for s in cs.steps_for(c.operator_steps, repeat=True)] == ["The d", "Prop "]


@pytest.mark.parametrize("snippet,expected", [
    ("operator_steps:\n  - {text: a, when: second}\n", "operator_steps[0]: when must be one of"),
    ("setup_steps:\n  - {name: r, command: x, when: sometimes}\n", "setup_steps[0]: when must be one of"),
    ("setup_steps:\n  - {name: r}\n", "setup_steps[0] must be a mapping with a non-empty command"),
    ("setup_steps: reset\n", "setup_steps[0] must be a mapping"),
    ("operator_live_steps:\n  - {text: a, when: first}\n", "operator_live_steps take no `when`"),
])
def test_the_spec_refuses_a_bad_when_or_a_bad_setup_step(root, snippet, expected):
    path = campaign_file(root, "name: x\nexperiment: expA\nsweep: {trajectory: [hover]}\n" + snippet)
    problems = cs.campaign_problems(path, root)
    assert any(expected in p for p in problems), problems


@pytest.mark.ros2
def test_runner_yaml_refuses_a_bad_when(root):
    (root / "expA" / "config" / "runner.yaml").write_text(
        RUNNER.replace("when: first", "when: once"), encoding="utf-8")
    with pytest.raises(tr.RunnerError, match="when must be one of"):
        tr.RunnerConfig.load("expA", root)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_a_repeat_with_no_steps_left_still_pauses_once():
    only_first = [{"text": "Fresh battery.", "run": "", "when": "first"}]
    assert tr.operator_steps_for(only_first, repeat=False) == only_first
    assert tr.operator_steps_for(only_first, repeat=True) == [tr.REPEAT_READY_STEP]
    assert tr.operator_steps_for([], repeat=True) == []      # no steps declared: no pause, as before


@pytest.mark.ros2
def test_campaign_setup_steps_follow_the_runners_and_honour_when(root):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    assert [s["name"] for s in tr.setup_steps_for(config, c.setup_steps, repeat=False)] == [
        "clean_slate", "first_only_calibration"]
    assert [s["name"] for s in tr.setup_steps_for(config, c.setup_steps, repeat=True)] == [
        "clean_slate", "radio_power_cycle"]


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
def dry_run_two(root):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    out = io.StringIO()
    with redirect_stdout(out):
        results = tr.run_plan(cp.build_plan(c, []), config, root / "expA" / "bags",
                              dry_run=True, limit=2)
    return results, out.getvalue()


@pytest.mark.ros2
def test_a_dry_run_of_two_shows_the_first_then_the_repeat_steps(root):
    results, text = dry_run_two(root)
    assert len(results) == 2
    first, second = text.split("operator steps of this repeat trial")
    assert "first trial of this run: 2 operator step(s)" in first
    assert "Fresh battery" in first and "stays where it landed" not in first
    assert "setup steps (runner.yaml): clean_slate, first_only_calibration" in first
    assert "setup steps, before the launch" not in first
    assert "stays where it landed" in second and "Fresh battery" not in second
    assert "setup steps (runner.yaml): clean_slate" in second
    assert "setup steps, before the launch: radio_power_cycle" in second
    flat = [[" ".join(c) for c in r.commands] for r in results]
    assert not any("power_cycle.py" in c for c in flat[0])
    assert any(c == "python3 power_cycle.py hover" for c in flat[1])
    # setup before the launch, in both trials
    for cmds in flat:
        launch = next(i for i, c in enumerate(cmds) if c.startswith("ros2 launch"))
        assert all(i < launch for i, c in enumerate(cmds) if c.startswith(("bash kill_ros", "python3 power_cycle")))


@pytest.mark.ros2
def test_a_trial_after_a_blocked_one_starts_over_as_first(root, monkeypatch):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    c.repetitions = 4
    seen = []
    statuses = iter(["ran", "blocked", "ran", "ran"])

    def fake_run_trial(spec, factors, config, bags_dir, **kw):
        seen.append(kw["repeat"])
        assert kw["campaign_setup_steps"] == c.setup_steps
        return SimpleNamespace(status=next(statuses), trial=spec, detail="", commands=[])

    monkeypatch.setattr(tr, "run_trial", fake_run_trial)
    tr.run_plan(cp.build_plan(c, []), config, root / "expA" / "bags", dry_run=True,
                stop_on_block=False)
    assert seen == [False, True, False, True]


@pytest.mark.ros2
def test_the_confirmation_gets_the_repeat_steps_and_the_gate_still_runs(root, monkeypatch):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    factors = cs.load_factors("expA", root)
    trial = next(c.combinations())
    shown, gated, ran = [], [], []

    class Gate:
        def check(self, *a, **k):
            gated.append(True)
            return SimpleNamespace(allowed=False, verdict="NO-GO", status="blocked",
                                   as_metadata=lambda: {}, summary=lambda: "no-go",
                                   failed_checks=[], mode="binding")

    hooks = tr.ProcessHooks(start_background=lambda cmd, *a, **k: ran.append(cmd) or object(),
                            run_foreground=lambda cmd, *a, **k: ran.append(cmd) or SimpleNamespace(returncode=0, stdout=""),
                            stop_process=lambda *a, **k: None)
    tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False, hooks=hooks,
                 operator_steps=c.operator_steps, campaign_setup_steps=c.setup_steps, repeat=True,
                 confirm_steps=lambda t, steps: shown.append([s["text"][:5] for s in steps]) or True,
                 gate=Gate(), slim=False)
    assert shown == [["The d", "Prop "]]
    assert ["python3", "power_cycle.py", "hover"] in ran
    assert ran.index(["python3", "power_cycle.py", "hover"]) < next(
        i for i, cmd in enumerate(ran) if cmd[:2] == ["ros2", "launch"])
    assert gated, "the T-0 gate must run on a repeat trial too"

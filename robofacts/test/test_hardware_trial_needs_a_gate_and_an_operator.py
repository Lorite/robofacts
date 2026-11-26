"""A hardware trial needs a T-0 gate and someone to answer its pose pause (2026-11-26).

Two of Alejandro's decisions in the review before 0.1.1 (findings A4, A6/B6 and C3-2). A
hardware experiment without a gate checklist ran every trial with an UNGATED warning only,
and the web interface's "skip the gate" did the same, while the paper says that the gate
runs before every trial. And a hardware run whose stdin was not a terminal (an agent, a
script, nohup) skipped the pose pause ("type fly") and the operator steps without a word.
Both are refused now. A simulation keeps both behaviors: an ungated simulation runs with
the warning, and a headless simulation needs no operator.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from robofacts import trial_index as ti
from robofacts import trial_runner as tr

from test_process_backend import use_project, write_project

EXPERIMENT = "plant"


class _Stdin(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__("")
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _make(tmp_path: Path, monkeypatch, *, hardware: bool, checklist: bool = True,
          operator_steps: bool = False) -> Path:
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    exp = experiments / EXPERIMENT
    if hardware:
        runner = exp / "config" / "runner.yaml"
        runner.write_text(runner.read_text(encoding="utf-8").replace(
            "simulation: true\n", "simulation: false\n"), encoding="utf-8")
    if not checklist:
        (exp / "config" / "preflight_checklist.json").unlink()
    if operator_steps:
        campaign = exp / "campaigns" / "rates.yaml"
        campaign.write_text(campaign.read_text(encoding="utf-8")
                            + "operator_steps:\n  - \"Put the robot on its mark ({rate}).\"\n",
                            encoding="utf-8")
    return experiments


def _run(experiments: Path, capsys, *extra: str):
    rc = tr.main([str(experiments / EXPERIMENT / "campaigns" / "rates.yaml"),
                  "--experiments-root", str(experiments), "--execute", "--limit", "1", *extra])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _nothing_ran(experiments: Path) -> bool:
    runs = experiments / EXPERIMENT / "runs"
    return (ti.load_trials(experiments, EXPERIMENT) == []
            and not (runs / tr.ATTEMPT_LEDGER_NAME).exists()
            and not (runs / "preflight_gate.jsonl").exists())


# --------------------------------------------------------------------------- #
# The rule itself
# --------------------------------------------------------------------------- #
def test_the_rule_applies_to_hardware_only():
    kwargs = dict(gated=False, pause_before_action=True, has_pose_surface=False,
                  stdin_is_tty=False)
    assert tr.hardware_trial_problems("simulation", **kwargs) == []
    assert tr.hardware_trial_problems("hardware", **kwargs) == [tr.NO_GATE_ON_HARDWARE,
                                                                tr.NO_TERMINAL_ON_HARDWARE]


@pytest.mark.parametrize("change", [{"pause_before_action": False}, {"has_pose_surface": True},
                                    {"stdin_is_tty": True}])
def test_a_pose_pause_that_someone_can_answer_is_not_refused(change):
    kwargs = dict(gated=True, pause_before_action=True, has_pose_surface=False,
                  stdin_is_tty=False)
    assert tr.hardware_trial_problems("hardware", **{**kwargs, **change}) == []


# --------------------------------------------------------------------------- #
# No gate on hardware (decision 3)
# --------------------------------------------------------------------------- #
@pytest.mark.real_sleep
def test_a_hardware_run_without_a_checklist_is_refused_before_the_plan(tmp_path, monkeypatch, capsys):
    experiments = _make(tmp_path, monkeypatch, hardware=True, checklist=False)
    rc, out, err = _run(experiments, capsys, "--no-pause")
    assert rc == 2 and tr.NO_GATE_ON_HARDWARE in err, (out, err)
    assert "STILL MISSING" not in out and "UNGATED" not in out
    assert _nothing_ran(experiments)


@pytest.mark.real_sleep
def test_a_simulation_without_a_checklist_still_runs_with_the_warning(tmp_path, monkeypatch, capsys):
    experiments = _make(tmp_path, monkeypatch, hardware=False, checklist=False)
    rc, out, err = _run(experiments, capsys, "--no-pause")
    assert rc == 0, (out, err)
    assert "UNGATED" in out and "1 OK" in out


def test_a_hardware_scratch_flight_without_a_checklist_is_refused(tmp_path, monkeypatch, capsys):
    experiments = _make(tmp_path, monkeypatch, hardware=True, checklist=False)
    rc, out, err = _run(experiments, capsys, "--scratch", "--no-pause")
    assert rc == 2 and tr.NO_GATE_ON_HARDWARE in err, (out, err)
    assert "SCRATCH FLIGHT" not in out
    assert not (experiments / EXPERIMENT / "recordings_scratch").is_dir() or not any(
        (experiments / EXPERIMENT / "recordings_scratch").iterdir())


def test_the_plan_check_reads_the_domain_of_each_cell_still_needed(tmp_path, monkeypatch):
    from robofacts import campaign_plan as cp
    from robofacts import campaign_spec as cs
    experiments = _make(tmp_path, monkeypatch, hardware=True, checklist=False)
    campaign = cs.load_campaign(experiments / EXPERIMENT / "campaigns" / "rates.yaml",
                                experiments, [])
    config = tr.RunnerConfig.load(EXPERIMENT, experiments)
    plan = cp.build_plan(campaign, [], False)
    kwargs = dict(gated=False, pause_before_action=False, has_pose_surface=False)
    assert tr.hardware_plan_problems(plan, config, **kwargs) == [tr.NO_GATE_ON_HARDWARE]
    config.simulation = True
    assert tr.hardware_plan_problems(plan, config, **kwargs) == []
    config.simulation = False
    for cell in plan.cells:   # a covered campaign starts nothing, so nothing is refused
        cell.usable = cell.wanted
    assert tr.hardware_plan_problems(plan, config, **kwargs) == []


# --------------------------------------------------------------------------- #
# No terminal on hardware (decision 2)
# --------------------------------------------------------------------------- #
@pytest.mark.real_sleep
def test_a_hardware_run_without_a_terminal_is_refused_unless_no_pause(tmp_path, monkeypatch, capsys):
    experiments = _make(tmp_path, monkeypatch, hardware=True)
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    rc, out, err = _run(experiments, capsys)
    assert rc == 2 and tr.NO_TERMINAL_ON_HARDWARE in err, (out, err)
    assert _nothing_ran(experiments)
    rc, out, err = _run(experiments, capsys, "--scratch")
    assert rc == 2 and tr.NO_TERMINAL_ON_HARDWARE in err, (out, err)


@pytest.mark.real_sleep
def test_with_no_pause_the_operator_steps_are_printed_and_not_waited_on(tmp_path, monkeypatch, capsys):
    experiments = _make(tmp_path, monkeypatch, hardware=True, operator_steps=True)
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    rc, out, err = _run(experiments, capsys, "--no-pause")
    assert rc == 0, (out, err)
    # The step names the trial's own level.
    assert re.search(r"Put the robot on its mark \((slow|fast)\)\.", out), out
    assert "nobody confirms the steps above" in out
    assert "the pose pause is skipped" not in out   # --no-pause: there is no pause to skip


@pytest.mark.real_sleep
def test_a_simulation_without_a_terminal_still_runs_with_the_pause_on(tmp_path, monkeypatch, capsys):
    experiments = _make(tmp_path, monkeypatch, hardware=False)
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    rc, out, err = _run(experiments, capsys)
    assert rc == 0 and "1 OK" in out, (out, err)
    # Skipped, but not without a word.
    assert "stdin is not a terminal: the pose pause is skipped" in out


# --------------------------------------------------------------------------- #
# The web interface (decision 3: its "skip the gate" on hardware)
# --------------------------------------------------------------------------- #
@pytest.fixture
def hardware_ui(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from robofacts.ui import app as ui
    package = Path(__file__).resolve().parents[1]
    project = tmp_path / "example_project"
    shutil.copytree(package / "example_project", project)
    experiments = project / "experiments"
    runner = experiments / "demo_log_recorder" / "config" / "runner.yaml"
    runner.write_text(runner.read_text(encoding="utf-8").replace(
        "simulation: true\n", "simulation: false\n"), encoding="utf-8")
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(experiments))
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(project))
    monkeypatch.setenv("PYTHONPATH", str(package) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("TRIALS_VOICE", "off")
    monkeypatch.chdir(project)
    ti._recorder_at.cache_clear()
    started = []
    monkeypatch.setattr(tr, "run_plan", lambda *a, **k: started.append(k) or [])
    app = ui.create_app(experiments, password="pw")
    app.config["TESTING"] = True
    yield app.test_client(), experiments, started
    ti._recorder_at.cache_clear()


def _post(client, **extra):
    import base64
    data = {"experiment": "demo_log_recorder", "campaign": "grid", "limit": "1",
            "go": "execute", "confirm": "yes", "pose_pause": "1", **extra}
    return client.post("/run", data=data, headers={
        "Authorization": "Basic " + base64.b64encode(b"user:pw").decode()})


def test_the_interface_refuses_to_skip_the_gate_of_a_hardware_experiment(hardware_ui):
    client, _experiments, started = hardware_ui
    response = _post(client, skip_gate="yes")
    assert response.status_code == 400
    assert "runs only after a T-0 gate" in response.get_data(as_text=True)
    assert started == []


def test_the_interface_refuses_a_hardware_experiment_without_a_checklist(hardware_ui):
    client, experiments, started = hardware_ui
    (experiments / "demo_log_recorder" / "config" / "preflight_checklist.json").unlink()
    response = _post(client)
    assert response.status_code == 400 and started == []


def test_the_interface_runs_a_gated_hardware_experiment_with_no_terminal(hardware_ui, monkeypatch):
    """The browser answers the pose pause, so the server needs no terminal."""
    client, _experiments, started = hardware_ui
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    response = _post(client)
    assert response.status_code == 302, response.get_data(as_text=True)[:800]

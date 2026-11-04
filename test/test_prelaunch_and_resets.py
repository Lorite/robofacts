"""Robot resets, the pre-launch check stage and the spoken NO-GO reasons (2026-11-04).

The three problems of the lab day these protect against: the first trial of a run hung because the
Crazyflie still streamed the previous session's log blocks (a robot reset), a low battery was found
only after the whole stack was up (a pre-launch check), and the voice said only "the gate says no
go" to an operator who cannot read the screen (spoken reasons).
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import gate_speech as gs
from robofacts import preflight_gate as pg
from robofacts import robot_resets as rr
from robofacts import trial_runner as tr

CORE = """
version: 1
factors:
  execution_host:
    levels: [laptop]
    default: laptop
    metadata_key: execution_host
    maps_to: {kind: host}
"""
CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  aborted: {required: true, type: bool}
"""
ROBOTS = """
version: 1
robots:
  crazyflie:
    estop: {command: "true"}
    reset:
      - name: cf_radio_power_cycle
        command: python3 power_cycle.py
      - name: aideck_access_point_back
        command: python3 wait_ap.py
        only_with: aideck
      - name: repeat_only
        command: python3 repeat_only.py
        when: repeat
  spot:
    estop: {command: "true"}
"""
RUNNER = """
launch: {package: demo_pkg, file: demo.launch.py}
action: {command: "python3 fly.py"}
robots: [{name: crazyflie, with: [aideck]}]
setup_steps:
  - name: kill_stale_rig
    command: bash kill.sh
hosts:
  laptop: {local: true}
"""
CAMPAIGN = """
name: c
experiment: expA
repetitions: 2
fixed: {execution_host: laptop}
preflight_checklist: config/checklist.json
setup_steps:
  - {name: campaign_step, command: python3 campaign_step.py}
"""
CHECKLIST = {"checklist": [
    {"text": "[HARD] battery over the radio", "check": "crazyflie_radio_battery",
     "args": {"uri": "radio://0/80/2M/E7E7E7E7E7", "min_v": "3.85"}, "stage": "pre_launch"},
    {"text": "[HARD] flow log rate", "check": "topic_rate", "args": {"topic": "/cf/flow_log", "min_hz": "30"},
     "speak": "Flow log too slow: {detail}"},
    "Operator: the area is clear.",
]}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE)
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT)
    (tmp_path / "common" / "config" / "robots.yaml").write_text(ROBOTS)
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER)
    (tmp_path / "expA" / "config" / "checklist.json").write_text(json.dumps(CHECKLIST))
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN)
    return tmp_path


# --------------------------------------------------------------------------- #
# Robot resets
# --------------------------------------------------------------------------- #
def test_the_declared_robots_resets_load_with_their_features(root):
    config = tr.RunnerConfig.load("expA", root)
    assert [s["name"] for s in config.robot_resets] == [
        "cf_radio_power_cycle", "aideck_access_point_back", "repeat_only"]
    assert all(s["robot"] == "crazyflie" for s in config.robot_resets)


def test_only_with_needs_the_feature_and_reset_false_opts_the_experiment_out(root):
    decl = rr.parse_declaration(["crazyflie"], "x")
    assert [s["name"] for s in rr.reset_steps(root, decl, "x")] == ["cf_radio_power_cycle", "repeat_only"]
    assert rr.reset_steps(root, rr.parse_declaration([{"name": "crazyflie", "reset": False}], "x"), "x") == []
    assert rr.reset_steps(root, rr.parse_declaration(["spot"], "x"), "x") == []
    with pytest.raises(rr.RobotResetError, match="not in"):
        rr.reset_steps(root, rr.parse_declaration(["drone_x"], "x"), "x")
    with pytest.raises(rr.RobotResetError, match="list"):
        rr.parse_declaration("crazyflie", "x")


def test_an_unknown_robot_stops_the_runner_config(root):
    (root / "expA" / "config" / "runner.yaml").write_text(RUNNER.replace("name: crazyflie", "name: nope"))
    with pytest.raises(tr.RunnerError, match="not in"):
        tr.RunnerConfig.load("expA", root)


def test_setup_order_runner_then_resets_then_campaign_and_the_campaign_opt_out(root):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    before, after = tr.setup_groups(config, c.setup_steps, repeat=False)
    assert [s["name"] for s in before] == ["kill_stale_rig"]
    assert [s["name"] for s in after] == ["cf_radio_power_cycle", "aideck_access_point_back", "campaign_step"]
    _, after_repeat = tr.setup_groups(config, c.setup_steps, repeat=True)
    assert "repeat_only" in [s["name"] for s in after_repeat]
    _, opted_out = tr.setup_groups(config, c.setup_steps, repeat=False, robot_reset=False)
    assert [s["name"] for s in opted_out] == ["campaign_step"]
    bad = root / "expA" / "campaigns" / "bad.yaml"
    bad.write_text(CAMPAIGN.replace("name: c\n", "name: bad\nrobot_reset: maybe\n"))
    assert any("robot_reset must be true or false" in p for p in cs.campaign_problems(bad, root))


# --------------------------------------------------------------------------- #
# The gate's stages and failed items
# --------------------------------------------------------------------------- #
def test_the_stages_split_the_checklist(root):
    gate = pg.PreflightGate(checklist=root / "expA" / "config" / "checklist.json")
    assert gate.stage_checks("pre_launch") == ["crazyflie_radio_battery"]
    pre, scratch = gate.with_stage("pre_launch")._materialize()
    entries = json.loads(pre.read_text())["checklist"]
    assert [e["check"] for e in entries] == ["crazyflie_radio_battery"]
    t0, _ = gate.with_stage("t0")._materialize()
    t0_entries = json.loads(t0.read_text())["checklist"]
    assert [e.get("check") if isinstance(e, dict) else "manual" for e in t0_entries] == ["topic_rate", "manual"]
    whole, none = gate._materialize()
    assert none is None and whole == gate.checklist
    with pytest.raises(ValueError):
        gate.with_stage("later")


def test_failed_items_carry_the_entry_speak(root):
    checklist = root / "expA" / "config" / "checklist.json"
    items = [{"check": "topic_rate", "result": "FAIL", "detail": "~28.7 Hz (need >= 30)",
              "text": "[HARD] flow log rate", "hard": True},
             {"check": "crazyflie_radio_battery", "result": "PASS", "detail": "ok",
              "text": "[HARD] battery over the radio", "hard": True}]
    out = pg._failed_items(items, checklist)
    assert out == [{"check": "topic_rate", "detail": "~28.7 Hz (need >= 30)",
                    "text": "[HARD] flow log rate", "speak": "Flow log too slow: {detail}"}]


# --------------------------------------------------------------------------- #
# Spoken reasons
# --------------------------------------------------------------------------- #
def test_the_spoken_reasons_of_the_lab_day():
    items = [{"check": "crazyflie_radio_battery", "detail": "battery 3.758 V is below the 3.85 V launch threshold"},
             {"check": "topic_rate", "detail": "/crazylori_brushless_1/flow_log advertised but only ~28.7 Hz over 5s (need >= 30) - publisher stalled or dead"},
             {"check": "mocap_stream", "detail": "/poses is not advertised at all"},
             {"check": "disk_space", "detail": "2 GB free"}]
    assert gs.sentences(items) == ["Battery 3.76 volts, below 3.85. Swap the battery.",
                                   "Flow log at 28.7 hertz, needs 30.",
                                   "Mocap stream missing. And 1 more."]
    assert gs.sentence({"check": "topic_rate", "speak": "Flow too slow: {detail}", "detail": "x"}) == "Flow too slow: x"
    assert gs.sentence({"check": "weird_check", "detail": ""}) == "Weird check failed."
    assert gs.sentence({"check": "topic_value", "detail": "/cf/flow_log median 0 < min 50 | values[2]"}) == "Flow log at 0, needs 50."
    assert gs.sentence({"check": "battery", "detail": "battery voltage UNREADABLE on /cf/battery"}).startswith("Battery voltage unreadable")


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
class StageGate:
    """A gate double with stages: the pre-launch stage says NO-GO with one failed item."""

    def __init__(self, checklist, stage="", log=None):
        self.checklist, self.stage, self.log = checklist, stage, log if log is not None else []
        self.last_result = None

    def stage_checks(self, stage):
        return ["crazyflie_radio_battery"] if stage == "pre_launch" else []

    def with_stage(self, stage):
        return StageGate(self.checklist, stage, self.log)

    def check(self, label, values=None):
        self.log.append(self.stage)
        if self.stage == "pre_launch":
            self.last_result = pg.GateResult(
                status="fail", allowed=False, failed_checks=["crazyflie_radio_battery low"],
                failed_items=[{"check": "crazyflie_radio_battery", "text": "",
                               "detail": "battery 3.758 V is below the 3.85 V launch threshold", "speak": ""}])
            raise pg.PreflightBlocked("pre-launch NO-GO: battery")
        self.last_result = pg.GateResult(status="pass", allowed=True)
        return self.last_result


def test_a_pre_launch_no_go_stops_before_the_resets_and_the_stack_and_speaks(root, monkeypatch):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    factors = cs.load_factors("expA", root)
    trial = next(c.combinations())
    ran, spoken = [], []
    monkeypatch.setattr(tr.voice, "speak", lambda text, **kw: spoken.append(text) or True)
    hooks = tr.ProcessHooks(start_background=lambda cmd, *a, **k: ran.append(cmd) or object(),
                            run_foreground=lambda cmd, *a, **k: ran.append(cmd) or SimpleNamespace(returncode=0, stdout=""),
                            stop_process=lambda *a, **k: None)
    gate = StageGate(root / "expA" / "config" / "checklist.json")
    result = tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False, hooks=hooks,
                          campaign_setup_steps=c.setup_steps, gate=gate, slim=False)
    assert result.status == "blocked"
    assert ran == [["bash", "kill.sh"]], ran          # the runner's step ran, no reset, no launch
    assert gate.log == ["pre_launch"]
    assert "Battery 3.76 volts, below 3.85. Swap the battery." in spoken


def test_a_pre_launch_go_runs_the_resets_then_the_t0_gate_without_the_pre_launch_entries(root):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    factors = cs.load_factors("expA", root)
    trial = next(c.combinations())
    log, ran = [], []

    class PassGate(StageGate):
        def with_stage(self, stage):
            return PassGate(self.checklist, stage, self.log)

        def check(self, label, values=None):
            self.log.append(self.stage)
            self.last_result = pg.GateResult(status="pass", allowed=True)
            return self.last_result

    hooks = tr.ProcessHooks(start_background=lambda cmd, *a, **k: ran.append(cmd) or object(),
                            run_foreground=lambda cmd, *a, **k: ran.append(cmd) or SimpleNamespace(returncode=0, stdout=""),
                            stop_process=lambda *a, **k: None)
    tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False, hooks=hooks,
                 campaign_setup_steps=c.setup_steps, gate=PassGate(root / "expA" / "config" / "checklist.json", log=log),
                 slim=False)
    flat = [" ".join(x) for x in ran]
    assert flat[:4] == ["bash kill.sh", "python3 power_cycle.py", "python3 wait_ap.py",
                        "python3 campaign_step.py"], flat
    assert log == ["pre_launch", "t0"]


def test_the_dry_run_lists_the_groups(root):
    config = tr.RunnerConfig.load("expA", root)
    c = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    out = io.StringIO()
    with redirect_stdout(out):
        tr.run_plan(cp.build_plan(c, []), config, root / "expA" / "bags", dry_run=True, limit=1,
                    gate=StageGate(root / "expA" / "config" / "checklist.json"))
    text = out.getvalue()
    assert "setup steps (runner.yaml): kill_stale_rig" in text
    assert "pre-launch checks, before the stack: crazyflie_radio_battery" in text
    assert "setup steps, before the launch: cf_radio_power_cycle, aideck_access_point_back, campaign_step" in text

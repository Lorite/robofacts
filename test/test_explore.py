"""Explore mode (explore.py, 2026-10-16): one condition's stack, live, never coverage.

The properties that matter, asserted without ROS:

* where it is allowed: always in simulation, on hardware only with an `explore:`
  block that names its parameters (never one pattern that opens all of them);
* the bridge it starts may change parameters and nothing else, and the trial
  livestream is not touched;
* the order (setup, parameter log, stack, aux, bridge) and the reverse teardown;
* nothing a trial leaves behind: no bag, no metadata, no attempt-ledger line;
* parameter_changes.yaml says from what to what, and never guesses a start value.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from robofacts import campaign_spec as cs
from robofacts import explore as ex
from robofacts import trial_runner as tr

FACTORS = """
version: 1
factors:
  execution_host:
    levels: [laptop, orin]
    default: laptop
    metadata_key: execution_host
    maps_to: {kind: host}
  gain:
    levels: [low, high]
    default: low
    metadata_key: gain
    maps_to: {kind: launch_arg, name: gain}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  gain: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

RUNNER_SIM = """
simulation: true
launch: {package: demo_pkg, file: stack.launch.py}
action: {command: "echo fly"}
setup_steps:
  - {name: kill, command: "scripts/kill_ros.sh"}
aux_launches:
  - {name: helper, command: "ros2 run demo_pkg helper --out {bag}"}
livestream: ros2 launch demo_pkg read_only_bridge.launch.py
hosts:
  laptop: {local: true}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 1
sweep:
  gain: [low, high]
"""


@pytest.fixture(autouse=True)
def _explore_sleeps_cost_nothing(monkeypatch):
    class _T:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def sleep(_s):
            return None

    monkeypatch.setattr(ex, "time", _T())


def make_root(tmp_path: Path, runner: str = RUNNER_SIM) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(FACTORS, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(runner, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    return tmp_path


def hardware(extra: str = "") -> str:
    return RUNNER_SIM.replace("simulation: true", "simulation: false") + extra


class FakeHooks:
    """Records what was started and stopped; the parameter log writes canned events."""

    def __init__(self, events=(), stack_rc=None):
        self.started, self.stopped, self.foreground = [], [], []
        self.events, self.stack_rc = list(events), stack_rc

    def hooks(self):
        return tr.ProcessHooks(start_background=self._start, run_foreground=self._fg,
                               stop_process=self._stop)

    def _start(self, cmd, target, *, cwd, label="", quiet_sink=None):
        self.started.append((label, cmd))
        if label == "params" and quiet_sink is not None:
            quiet_sink.write("[params] [explore_params] listening\n")
            for e in self.events:
                quiet_sink.write("[params] " + json.dumps(e) + "\n")
        rc = self.stack_rc if label == "stack" else None
        return SimpleNamespace(poll=lambda: rc)

    def _fg(self, cmd, target, *, cwd, check=True):
        self.foreground.append(cmd)
        return SimpleNamespace(returncode=0)

    def _stop(self, proc, target, *, name, kind):
        self.stopped.append(name)


def load(root: Path, sets=()):
    campaign = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root, list(sets))
    config = tr.RunnerConfig.load("expA", root)
    config.settle_s = 0
    return campaign, config


# --------------------------------------------------------------------------- #
# Where Explore is allowed
# --------------------------------------------------------------------------- #
def test_simulation_is_open_by_default(tmp_path):
    _, config = load(make_root(tmp_path))
    s = ex.settings(config)
    assert s.allowed and s.writable and s.param_whitelist == [".*"] and s.simulation


def test_hardware_without_an_explore_block_is_refused(tmp_path):
    _, config = load(make_root(tmp_path, hardware()))
    s = ex.settings(config)
    assert not s.allowed and "explore:" in s.reason


def test_hardware_with_an_empty_block_is_a_view_that_changes_nothing(tmp_path):
    _, config = load(make_root(tmp_path, hardware("explore: {}\n")))
    s = ex.settings(config)
    assert s.allowed and not s.writable
    cmd = " ".join(ex.bridge_command(s))
    assert "parameters" not in cmd.split("capabilities:=")[1].split()[0]
    assert "param_whitelist:=['(?!)']" in cmd


def test_hardware_may_not_open_every_parameter(tmp_path):
    _, config = load(make_root(tmp_path, hardware("explore:\n  param_whitelist: ['^/cf1/.*', '.*']\n")))
    s = ex.settings(config)
    assert not s.allowed and "'.*'" in s.reason


def test_hardware_with_named_parameters_is_allowed(tmp_path):
    _, config = load(make_root(tmp_path, hardware(
        "explore:\n  param_whitelist: ['^/cf1/controller\\\\.kp$']\n  port: 9000\n")))
    s = ex.settings(config)
    assert s.allowed and s.writable and s.port == 9000 and not s.simulation


@pytest.mark.parametrize("block, words", [
    ("explore: [a]\n", "mapping"),
    ("explore:\n  param_whitlist: ['.*']\n", "unknown key"),
    ("explore:\n  param_whitelist: ['(']\n", "not a regular expression"),
    ("explore:\n  param_whitelist: '.*'\n", "list of regular expressions"),
    ("explore:\n  port: 0\n", "port number"),
])
def test_a_malformed_explore_block_refuses_to_load(tmp_path, block, words):
    root = make_root(tmp_path, RUNNER_SIM + block)
    with pytest.raises(tr.RunnerError, match=words):
        tr.RunnerConfig.load("expA", root)


@pytest.mark.parametrize("pattern, wide", [(".*", True), (".+", True), ("^.*$", True),
                                           ("", True), ("^/cf1/.*", False),
                                           ("controller\\.kp$", False)])
def test_matches_everything(pattern, wide):
    assert ex.matches_everything(pattern) is wide


# --------------------------------------------------------------------------- #
# The bridge: parameters, and nothing else
# --------------------------------------------------------------------------- #
def test_the_bridge_may_change_parameters_but_never_publish_or_call(tmp_path):
    _, config = load(make_root(tmp_path))
    cmd = ex.bridge_command(ex.settings(config))
    joined = " ".join(cmd)
    caps = joined.split("capabilities:=")[1].split()[0]
    assert "parameters" in caps and "clientPublish" not in caps and "services" not in caps
    assert "client_topic_whitelist:=['(?!)']" in joined and "service_whitelist:=['(?!)']" in joined
    assert "address:=127.0.0.1" in joined and f"__node:={ex.BRIDGE_NODE}" in joined


def test_the_trial_livestream_is_not_what_explore_starts(tmp_path):
    campaign, config = load(make_root(tmp_path))
    result = ex.run_explore(campaign, config, tmp_path, dry_run=True)
    flat = [" ".join(c) for c in result.commands]
    assert not any("read_only_bridge" in c for c in flat)
    assert config.livestream.endswith("read_only_bridge.launch.py")


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def test_a_dry_run_launches_nothing_and_shows_the_order(tmp_path):
    campaign, config = load(make_root(tmp_path))
    fake = FakeHooks()
    result = ex.run_explore(campaign, config, tmp_path, dry_run=True, hooks=fake.hooks())
    assert result.status == "dry-run" and not fake.started and not fake.foreground
    assert result.commands[0] == ["scripts/kill_ros.sh"]
    assert "explore_params" in " ".join(result.commands[1])
    assert result.commands[2][:2] == ["ros2", "launch"] and "gain:=low" in result.commands[2]
    assert "helper" in result.commands[3] and result.commands[-1][2] == "foxglove_bridge"


def test_an_exploration_starts_in_order_and_stops_in_reverse(tmp_path):
    campaign, config = load(make_root(tmp_path), ["gain=high"])
    events = [
        {"node": "/stack", "name": "kp", "kind": "initial", "value": 1.0},
        {"node": "/stack", "name": "kp", "kind": "changed", "value": 2.0},
        {"node": "/stack", "name": "kp", "kind": "changed", "value": 3.0},
        {"node": "/stack", "name": "ki", "kind": "initial", "value": 0.1},
        {"node": "/stack", "name": "ki", "kind": "changed", "value": 0.2},
        {"node": "/stack", "name": "ki", "kind": "changed", "value": 0.1},
    ]
    fake = FakeHooks(events)
    ready = {}
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False, hooks=fake.hooks(),
                            max_s=0, on_ready=ready.update)
    assert result.status == "explored", result.detail
    assert [label for label, _ in fake.started] == ["params", "stack", "helper", "bridge"]
    assert fake.foreground == [["scripts/kill_ros.sh"]]
    assert fake.stopped == ["bridge", "aux:helper", "launch_stack", "params"]
    assert ready["url"] == "ws://localhost:8765" and not ready["tunnel"]
    # The aux launch's {bag} is the exploration's own folder, never a bag directory.
    helper = dict(fake.started)["helper"]
    assert str(result.out_dir) in " ".join(helper) and "bags" not in " ".join(helper)
    changes = yaml.safe_load((result.out_dir / "parameter_changes.yaml").read_text())
    assert changes["condition"]["gain"] == "high" and changes["status"] == "explored"
    assert changes["changed"] == [{"node": "/stack", "parameter": "kp", "from": 1.0, "to": 3.0,
                                   "times_changed": 2}]
    assert changes["reverted"] == ["/stack.ki"]
    assert (result.out_dir / "run.log").is_file()


def test_an_exploration_leaves_nothing_a_trial_leaves(tmp_path):
    campaign, config = load(make_root(tmp_path))
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False,
                            hooks=FakeHooks().hooks(), max_s=0)
    exp = tmp_path / "expA"
    assert result.out_dir.parent == exp / "runs" / "explore"
    for name in ("bags", "bags_scratch", "bags_slim"):
        assert not (exp / name).exists()
    assert not list(exp.rglob("trial_metadata.json"))
    assert not list(exp.rglob("attempts*.jsonl"))


def test_stop_ends_it(tmp_path):
    campaign, config = load(make_root(tmp_path))
    stop = threading.Event()
    stop.set()
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False,
                            hooks=FakeHooks().hooks(), stop=stop)
    assert result.status == "explored"


def test_a_stack_that_dies_while_settling_is_never_ready(tmp_path):
    campaign, config = load(make_root(tmp_path))
    fake = FakeHooks(stack_rc=1)
    ready = {}
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False, hooks=fake.hooks(),
                            max_s=60, on_ready=ready.update)
    assert result.status == "failed" and "while settling" in result.detail
    assert not ready
    assert "bridge" not in [label for label, _ in fake.started]
    # Already exited locally: nothing to signal, so no "No such process" error.
    assert "launch_stack" not in fake.stopped and "params" in fake.stopped


def test_hardware_without_a_block_launches_nothing(tmp_path):
    campaign, config = load(make_root(tmp_path, hardware()))
    fake = FakeHooks()
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False, hooks=fake.hooks(), max_s=0)
    assert result.status == "refused" and not fake.started and not fake.foreground


def test_unconfirmed_operator_steps_launch_nothing(tmp_path):
    root = make_root(tmp_path)
    (root / "expA" / "campaigns" / "c.yaml").write_text(
        CAMPAIGN + "operator_steps:\n  - Put the drone on the pad\n", encoding="utf-8")
    campaign, config = load(root)
    fake = FakeHooks()
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False, hooks=fake.hooks(),
                            max_s=0, confirm_steps=lambda trial, steps: False)
    assert result.status == "refused" and not fake.started


def test_a_remote_host_gets_a_tunnel_and_the_installed_logger(tmp_path):
    root = make_root(tmp_path, RUNNER_SIM + "  orin: {host: orin-rig, container: ros2_jetson_dev}\n")
    campaign, config = load(root, ["execution_host=orin"])
    result = ex.run_explore(campaign, config, tmp_path, dry_run=True)
    assert result.connect["tunnel"] == "ssh -N -L 8765:localhost:8765 orin-rig"
    assert result.commands[1] == ["python3", "-m", "robofacts.explore_params"]


# --------------------------------------------------------------------------- #
# The change summary
# --------------------------------------------------------------------------- #
def test_parse_events_skips_what_is_not_an_event():
    lines = ["[params] [explore_params] listening", "[params] {not json",
             '[params] {"node": "/a", "name": "x", "kind": "new", "value": 1}', '{"other": 1}']
    assert ex.parse_events(lines) == [{"node": "/a", "name": "x", "kind": "new", "value": 1}]


def test_the_start_value_is_never_guessed():
    s = ex.summarize([{"node": "/a", "name": "x", "kind": "changed", "value": 5}])
    assert s["changed"][0]["from"] is None


def test_a_snapshot_after_the_first_change_is_not_the_start_value():
    s = ex.summarize([{"node": "/a", "name": "x", "kind": "changed", "value": 5},
                      {"node": "/a", "name": "x", "kind": "initial", "value": 5}])
    assert s["changed"][0]["from"] is None


def test_the_first_known_value_is_the_start_and_a_deletion_counts():
    s = ex.summarize([{"node": "/a", "name": "x", "kind": "new", "value": 1},
                      {"node": "/a", "name": "x", "kind": "initial", "value": 1},
                      {"node": "/a", "name": "y", "kind": "initial", "value": "on"},
                      {"node": "/a", "name": "y", "kind": "deleted", "value": None}])
    assert s["changed"] == [{"node": "/a", "parameter": "y", "from": "on", "to": None,
                             "times_changed": 1, "deleted": True}]


def test_the_logger_turns_events_into_lines():
    from robofacts import explore_params as ep
    value = SimpleNamespace(type=3, double_value=2.5)
    event = SimpleNamespace(node="/stack", stamp=SimpleNamespace(sec=10, nanosec=0),
                            new_parameters=[], deleted_parameters=[],
                            changed_parameters=[SimpleNamespace(name="kp", value=value)])
    [line] = ep.event_lines(event, "/robofacts_explore_params")
    assert json.loads(line) == {"t": 10.0, "node": "/stack", "name": "kp", "kind": "changed",
                                "value": 2.5}
    assert ep.event_lines(SimpleNamespace(node="/robofacts_explore_params"),
                          "/robofacts_explore_params") == []

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


# --------------------------------------------------------------------------- #
# Viewers (explore.viewers, 2026-10-17): windows for a human, never part of the stack
# --------------------------------------------------------------------------- #
VIEWERS = "explore:\n  viewers:\n    - {name: window, command: 'ros2 launch demo_pkg gui.launch.py gain:={arg_gain}'}\n"


def test_viewers_start_after_the_bridge_and_stop_first(tmp_path):
    campaign, config = load(make_root(tmp_path, RUNNER_SIM + VIEWERS), ["gain=high"])
    fake = FakeHooks()
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False, hooks=fake.hooks(), max_s=0)
    assert result.status == "explored"
    assert [label for label, _ in fake.started] == ["params", "stack", "helper", "bridge", "window"]
    assert fake.stopped[0] == "viewer:window" and fake.stopped[1] == "bridge"
    assert dict(fake.started)["window"][-1] == "gain:=high"
    assert "opens window" in ex.settings(config).describe()


def test_viewers_do_not_change_the_stack(tmp_path):
    campaign, config = load(make_root(tmp_path))
    plain = ex.run_explore(campaign, config, tmp_path, dry_run=True)
    campaign, config = load(make_root(tmp_path / "v", RUNNER_SIM + VIEWERS))
    with_viewer = ex.run_explore(campaign, config, tmp_path / "v", dry_run=True)
    # The aux launch names each run's own folder, so compare with the folders made equal.
    same = [[a.replace(str(with_viewer.out_dir), str(plain.out_dir)) for a in c]
            for c in with_viewer.commands]
    assert same[:len(plain.commands)] == plain.commands
    assert with_viewer.commands[-1][:3] == ["ros2", "launch", "demo_pkg"]


def test_a_trial_never_runs_a_viewer(tmp_path):
    root = make_root(tmp_path, RUNNER_SIM + VIEWERS)
    campaign, config = load(root)
    spec = next(campaign.combinations())
    fake = FakeHooks()
    tr.run_trial(spec, campaign.factors, config, root / "expA" / "bags", dry_run=False,
                 hooks=fake.hooks(), pause_before_action=False)
    assert not any("gui.launch.py" in " ".join(cmd) for _, cmd in fake.started)


def test_a_viewer_without_a_command_refuses_to_load(tmp_path):
    root = make_root(tmp_path, RUNNER_SIM + "explore:\n  viewers:\n    - {name: window}\n")
    with pytest.raises(tr.RunnerError, match="explore.viewers"):
        tr.RunnerConfig.load("expA", root)


# --------------------------------------------------------------------------- #
# The run page's controls (#133, 2026-10-17): actions, set back, record, notes
# --------------------------------------------------------------------------- #
ACTIONS = ("explore:\n  actions:\n"
           "    - {name: take_off, command: 'python3 fly.py --mode take_off --gain {gain}'}\n"
           "    - {name: land, command: 'python3 fly.py --mode land'}\n")


class FakeProc:
    """A background process whose exit the test decides."""

    def __init__(self):
        self.rc = None
        self.pid = 0

    def poll(self):
        return self.rc


class ControlHooks(FakeHooks):
    """FakeHooks whose actions and recorder run until signalled."""

    def __init__(self, events=()):
        super().__init__(events)
        self.procs = {}

    def _start(self, cmd, target, *, cwd, label="", quiet_sink=None):
        proc = super()._start(cmd, target, cwd=cwd, label=label, quiet_sink=quiet_sink)
        if label.startswith("action:") or label == "recorder":
            proc = FakeProc()
            self.procs[label] = proc
        if label.startswith("analysis:"):
            proc = FakeProc()
            proc.rc = 1 if "broken" in " ".join(cmd) else 0
        return proc

    def _stop(self, proc, target, *, name, kind):
        self.stopped.append(name)
        if isinstance(proc, FakeProc):
            proc.rc = 0


def wait_for(predicate, timeout=5.0):
    t0 = time.monotonic()
    while not predicate():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("timed out")
        time.sleep(0.01)


def bound(tmp_path, runner=RUNNER_SIM + ACTIONS, events=(), sets=("gain=high",), **kw):
    """A controls object bound the way run_explore binds it, without running a stack."""
    root = make_root(tmp_path, runner)
    campaign, config = load(root, list(sets))
    spec = next(campaign.combinations())
    out_dir = root / "expA" / "runs" / "explore" / "20261017T100000_explore_c"
    out_dir.mkdir(parents=True)
    (out_dir / "parameter_events.jsonl").write_text(
        "".join("[params] " + json.dumps(e) + "\n" for e in events), encoding="utf-8")
    fake = ControlHooks()
    terminated = []

    def terminate(proc, target, cmd):
        terminated.append(cmd)
        proc.rc = 2

    controls = ex.ExploreControls(terminate=terminate, kill=lambda *a: None,
                                  slim=lambda *a, **k: None, **kw)
    controls.bind(hooks=fake.hooks(), target=None, cwd=root, out_dir=out_dir, spec=spec,
                  factors=campaign.factors, config=config, settings=ex.settings(config),
                  clock=lambda: "20261017T100500", experiment="expA", experiments_root=root,
                  placeholders={f"arg_{k}": v for k, v in spec.launch_args(campaign.factors).items()})
    return controls, fake, terminated, root, out_dir


def test_actions_are_declared_like_viewers(tmp_path):
    _, config = load(make_root(tmp_path, RUNNER_SIM + ACTIONS))
    s = ex.settings(config)
    assert [a["name"] for a in s.actions] == ["take_off", "land"]
    assert "Actions: take_off, land" in s.describe()


@pytest.mark.parametrize("block, words", [
    ("explore:\n  actions:\n    - {name: a}\n", "explore.actions"),
    ("explore:\n  actions:\n    - {name: a, command: x}\n    - {name: a, command: y}\n", "more than once"),
])
def test_a_malformed_action_refuses_to_load(tmp_path, block, words):
    with pytest.raises(tr.RunnerError, match=words):
        tr.RunnerConfig.load("expA", make_root(tmp_path, RUNNER_SIM + block))


def test_a_trial_never_runs_an_explore_action(tmp_path):
    root = make_root(tmp_path, RUNNER_SIM + ACTIONS)
    campaign, config = load(root)
    fake = FakeHooks()
    tr.run_trial(next(campaign.combinations()), campaign.factors, config, root / "expA" / "bags",
                 dry_run=False, hooks=fake.hooks(), pause_before_action=False)
    ran = [" ".join(c) for _, c in fake.started] + [" ".join(c) for c in fake.foreground]
    assert not any("fly.py" in c for c in ran)


def test_one_action_at_a_time_and_stop_sends_sigterm(tmp_path):
    controls, fake, terminated, _, _ = bound(tmp_path)
    ok, _ = controls.start_action("take_off")
    assert ok
    label, cmd = fake.started[-1]
    assert label == "action:take_off" and cmd == ["python3", "fly.py", "--mode", "take_off",
                                                    "--gain", "high"]
    ok, msg = controls.start_action("land")
    assert not ok and "still running" in msg
    assert controls.status()["action"]["name"] == "take_off"
    ok, _ = controls.stop_action()
    assert ok and terminated == [cmd]
    wait_for(lambda: controls.status()["action"] is None)
    last = controls.status()["last_action"]
    assert last["name"] == "take_off" and last["rc"] == 2 and last["stopping"]
    assert "proc" not in last and "cmd" not in last
    # The slot is free again.
    assert controls.start_action("land")[0]


def test_an_action_that_exits_shows_its_exit_code(tmp_path):
    controls, fake, _, _, _ = bound(tmp_path)
    controls.start_action("land")
    fake.procs["action:land"].rc = 0
    wait_for(lambda: controls.status()["last_action"] is not None)
    assert controls.status()["last_action"]["rc"] == 0 and not controls.status()["last_action"]["stopping"]


def test_only_declared_actions_run(tmp_path):
    controls, fake, _, _, _ = bound(tmp_path)
    ok, msg = controls.start_action("rm -rf /")
    assert not ok and "no explore action" in msg and not any(
        label.startswith("action:") for label, _ in fake.started)


def test_hardware_actions_need_the_arm_checkbox(tmp_path):
    runner = hardware("explore:\n  param_whitelist: ['^/stack\\\\.kp$']\n  actions:\n"
                      "    - {name: land, command: 'python3 fly.py --mode land'}\n")
    controls, fake, _, _, _ = bound(tmp_path, runner)
    ok, msg = controls.start_action("land")
    assert not ok and "arm" in msg
    assert controls.start_action("land", armed=True)[0]


def test_stopping_the_exploration_lands_the_action_before_the_stack_stops(tmp_path):
    campaign, config = load(make_root(tmp_path, RUNNER_SIM + ACTIONS))
    fake = ControlHooks()
    order = []

    def terminate(proc, target, cmd):
        order.append("action")
        proc.rc = 2

    controls = ex.ExploreControls(terminate=terminate, kill=lambda *a: None)
    original_stop = fake._stop
    fake._stop = lambda proc, target, *, name, kind: (order.append(name),
                                                      original_stop(proc, target, name=name, kind=kind))
    result = ex.run_explore(campaign, config, tmp_path, dry_run=False, hooks=fake.hooks(), max_s=0,
                            controls=controls,
                            on_ready=lambda info: controls.start_action("take_off"))
    assert result.status == "explored"
    assert order[0] == "action" and order[1] == "bridge"
    assert not controls.live and not controls.start_action("land")[0]


EVENTS = [
    {"node": "/stack", "name": "kp", "kind": "initial", "value": 1.0},
    {"node": "/stack", "name": "kp", "kind": "changed", "value": 2.5},
    {"node": "/stack", "name": "mode", "kind": "changed", "value": "fast"},
    {"node": "/stack", "name": "ki", "kind": "initial", "value": 0.1},
]


def test_the_live_table_reads_the_log_while_it_runs(tmp_path):
    controls, _, _, _, _ = bound(tmp_path, events=EVENTS)
    changes = controls.status()["changes"]
    rows = {r["parameter"]: r for r in changes["changed"]}
    assert rows["kp"]["from"] == 1.0 and rows["kp"]["to"] == 2.5 and rows["kp"]["settable"]
    # The start of `mode` is unknown, so it cannot be set back.
    assert rows["mode"]["from"] is None and not rows["mode"]["settable"]


def test_set_back_sets_the_starting_value(tmp_path):
    controls, fake, _, _, _ = bound(tmp_path, events=EVENTS)
    ok, msg = controls.set_back("/stack", "kp")
    assert ok, msg
    assert fake.foreground[-1] == ["ros2", "param", "set", "/stack", "kp", "1.0"]
    assert not controls.set_back("/stack", "mode")[0]          # start unknown
    assert not controls.set_back("/stack", "ki")[0]            # never changed


def test_set_back_respects_the_hardware_whitelist(tmp_path):
    runner = hardware("explore:\n  param_whitelist: ['^/stack\\\\.ki$']\n")
    controls, fake, _, _, _ = bound(tmp_path, runner, events=EVENTS)
    ok, msg = controls.set_back("/stack", "kp")
    assert not ok and "whitelist" in msg and not fake.foreground


def test_set_back_values_keep_their_type():
    assert ex.param_value_text(True) == "true" and ex.param_value_text(4.0) == "4.0"
    assert ex.param_value_text("1.0") == '"1.0"' and ex.param_value_text([1, 2]) == "[1, 2]"
    assert yaml.safe_load(ex.param_value_text("fast")) == "fast"


def test_a_recording_is_scratch_and_names_the_exploration(tmp_path):
    from robofacts import trial_records as trec
    controls, fake, _, root, out_dir = bound(tmp_path, events=EVENTS)
    ok, msg = controls.start_record("gain test")
    assert ok, msg
    label, cmd = fake.started[-1]
    bag = root / "expA" / "bags_scratch" / "20261017T100500_explore_gain-test"
    assert label == "recorder" and cmd[:3] == ["ros2", "bag", "record"] and str(bag) in cmd
    assert not controls.start_record()[0]
    assert controls.status()["record"]["name"] == bag.name
    # The page polls this: the parameter snapshot stays in the metadata only.
    assert "parameters" not in controls.status()["record"]
    ok, msg = controls.stop_record(wait_slim=True)
    assert ok, msg
    assert "record" in fake.stopped
    meta = json.loads((bag / "trial_metadata.json").read_text())
    assert meta["scratch"] is True and meta["explore"] is True
    assert meta["explore_session"] == out_dir.name and meta["explore_condition"]["gain"] == "high"
    assert meta["explore_parameters_at_start"]["/stack.kp"] == 2.5
    assert [c["parameter"] for c in meta["explore_changed_at_start"]] == ["kp", "mode"]
    [rec] = controls.status()["recordings"]
    assert rec["name"] == bag.name and rec["slim_status"] == "done" and rec["slim"] == bag.name + "_slim"
    # Never coverage: listed as a scratch record, never under bags/.
    records = trec.load_records(root, "expA")
    assert [r.kind for r in records if r.name == bag.name] == ["scratch"]
    assert not (root / "expA" / "bags").exists()


def test_notes_and_marks_go_to_notes_md(tmp_path):
    controls, _, _, _, out_dir = bound(tmp_path, now=lambda: 1792245600.0)
    assert not controls.note("   ")[0]
    assert controls.note("the drone drifts at the partition")[0]
    controls.start_record("x")
    ok, line = controls.note("", mark=True)
    assert ok and "● mark" in line and "[recording 20261017T100500_explore_x at +0.0 s]" in line
    text = (out_dir / "notes.md").read_text()
    assert text.startswith("# Notes: expA exploration 20261017T100000_explore_c")
    assert "gain=high" in text and "the drone drifts at the partition" in text
    assert len(controls.status()["notes"]) == 2


def test_parameter_changes_lists_the_notes_and_recordings(tmp_path):
    campaign, config = load(make_root(tmp_path, RUNNER_SIM + ACTIONS))
    controls = ex.ExploreControls(slim=lambda *a, **k: None)

    def on_ready(info):
        controls.note("first look")
        controls.start_record("look")

    result = ex.run_explore(campaign, config, tmp_path, dry_run=False, hooks=ControlHooks().hooks(),
                            max_s=0, controls=controls, on_ready=on_ready)
    changes = yaml.safe_load((result.out_dir / "parameter_changes.yaml").read_text())
    assert changes["notes"][0].endswith("first look")
    assert len(changes["recordings"]) == 1 and "bags_scratch" in changes["recordings"][0]
    # The recording was finalized at stop, with its metadata.
    assert (Path(changes["recordings"][0]) / "trial_metadata.json").is_file()


# --------------------------------------------------------------------------- #
# The jog pad (explore.jog, #133): a velocity for a moment, from the runner
# --------------------------------------------------------------------------- #
JOG = "explore:\n  jog: {topic: /cmd_vel, max_speed_mps: 0.3, step_s: 1.5}\n"


@pytest.mark.parametrize("block, words", [
    ("explore:\n  jog: {topic: cmd_vel, max_speed_mps: 0.3}\n", "absolute topic"),
    ("explore:\n  jog: {topic: /cmd_vel}\n", "needs max_speed_mps"),
    ("explore:\n  jog: {topic: /cmd_vel, max_speed_mps: 0}\n", "positive number"),
    ("explore:\n  jog: {topic: /cmd_vel, max_speed_mps: 0.3, step_s: 30}\n", "at most 5.0"),
    ("explore:\n  jog: {topic: /cmd_vel, max_speed_mps: 0.3, speed: 1}\n", "unknown key"),
])
def test_a_malformed_jog_refuses_to_load(tmp_path, block, words):
    with pytest.raises(tr.RunnerError, match=words):
        tr.RunnerConfig.load("expA", make_root(tmp_path, RUNNER_SIM + block))


def test_a_jog_is_clipped_to_its_limits_and_shares_the_action_slot(tmp_path):
    controls, fake, terminated, _, _ = bound(tmp_path, RUNNER_SIM + JOG)
    assert controls.status()["jog"]["topic"] == "/cmd_vel"
    ok, msg = controls.jog(vx=2.0, vz=-0.1)
    assert ok, msg
    label, cmd = fake.started[-1]
    assert label == "action:jog" and cmd[1].endswith("explore_jog.py")
    args = dict(zip(cmd[2::2], cmd[3::2]))
    assert args == {"--topic": "/cmd_vel", "--vx": "0.3", "--vy": "0", "--vz": "-0.1",
                    "--yaw": "0", "--seconds": "1.5"}
    # One thing at a time: a second jog waits, and the stop ends it.
    assert not controls.jog(vy=0.1)[0]
    assert controls.status()["action"]["name"] == "jog vx=+0.30 vz=-0.10"
    controls.stop_action()
    assert terminated == [cmd]
    wait_for(lambda: controls.status()["action"] is None)
    # No yaw limit declared: yaw is clipped to zero, and a jog of nothing is refused.
    assert not controls.jog(yaw=1.0)[0]
    assert not controls.jog(vx="fast")[0] and not controls.jog(vx=float("nan"))[0]


def test_no_jog_pad_without_a_declaration(tmp_path):
    controls, fake, _, _, _ = bound(tmp_path)
    ok, msg = controls.jog(vx=0.1)
    assert not ok and "explore.jog" in msg


def test_a_hardware_jog_needs_the_arm_checkbox(tmp_path):
    controls, _, _, _, _ = bound(tmp_path, hardware(
        "explore:\n  param_whitelist: ['^/stack\\\\.kp$']\n  jog: {topic: /cf1/cmd_vel, max_speed_mps: 0.2}\n"))
    assert "arm" in controls.jog(vx=0.1)[1]
    assert controls.jog(vx=0.1, armed=True)[0]
    assert "Jog pad on /cf1/cmd_vel" in ex.settings(controls._ctx["config"]).describe()


def test_a_remote_jog_runs_the_installed_module():
    cmd = ex.jog_command(object(), "/cmd_vel", 0.1, 0, 0, 0, 1.0)
    assert cmd[:3] == ["python3", "-m", "robofacts.explore_jog"]


# --------------------------------------------------------------------------- #
# Analyze a recording (#133): the experiment's per-bag analyses, on a scratch bag
# --------------------------------------------------------------------------- #
ANALYSES = """
analyses:
  look:
    describe: a quick look
    per: bag
    command: "python3 look.py {bag} --output-dir {results}"
  broken:
    describe: always fails
    per: bag
    command: "python3 broken.py {bag}"
  table:
    describe: a cohort table
    per: cohort
    command: "python3 table.py {bags}"
"""


def test_a_recording_can_be_analyzed_even_after_stop(tmp_path):
    controls, fake, _, root, out_dir = bound(tmp_path)
    (root / "expA" / "config" / "analyses.yaml").write_text(ANALYSES, encoding="utf-8")
    assert controls.status()["analysis_names"] == ["look", "broken"]   # per-bag only
    assert not controls.analyze("nothing", "look")[0]
    controls.start_record("a")
    controls.stop_record(wait_slim=True)
    name = controls.recordings[0]["name"]
    assert not controls.analyze(name, "table")[0]                       # a cohort analysis
    controls.shutdown()                                                 # the stack is gone
    ok, msg = controls.analyze(name, "look")
    assert ok, msg
    wait_for(lambda: controls.status()["analyses"][0]["status"] != "running")
    [a] = controls.status()["analyses"]
    assert a["status"] == "ok" and a["results"].startswith("results/") and a["results"].endswith(name)
    label, cmd = fake.started[-1]
    assert label == "analysis:look" and str(root / "expA" / "bags_scratch" / name) in cmd
    meta = json.loads((root / "expA" / Path(a["results"]).parent / "analysis_metadata.json").read_text())
    assert meta["bags"] == [name] and meta["scratch"] is True and meta["explore"] is True
    assert meta["explore_session"] == out_dir.name
    controls.analyze(name, "broken")
    wait_for(lambda: controls.status()["analyses"][-1]["status"] != "running")
    assert controls.status()["analyses"][-1]["status"] == "failed"


def test_a_remote_recording_is_not_analyzed_here(tmp_path):
    controls, _, _, root, _ = bound(tmp_path)
    (root / "expA" / "config" / "analyses.yaml").write_text(ANALYSES, encoding="utf-8")
    controls.start_record()
    controls.stop_record(wait_slim=True)
    controls._kept["target"] = object()
    ok, msg = controls.analyze(controls.recordings[0]["name"], "look")
    assert not ok and "on the rig" in msg


# --------------------------------------------------------------------------- #
# Keep a value: where it is declared, and the edit that would keep it (#133)
# --------------------------------------------------------------------------- #
LAUNCH = """
from launch.actions import DeclareLaunchArgument
def generate_launch_description():
    declare_gain = DeclareLaunchArgument(
        'stack_gain', default_value='1.0', description='the gain')
    node = Node(package='demo_pkg', executable='stack', name='stack',
                parameters=[params_file, {'kp': ParameterValue(LaunchConfiguration('stack_gain'), value_type=float),
                             'ki': 0.1}])
"""
PARAMS_YAML = "stack:\n  ros__parameters:\n    kp: 1.0\n    kd: 0.5\nother:\n  ros__parameters:\n    kd: 9.0\n"
FACTORS_GAIN = FACTORS + """  stack_gain:
    levels: [low, high]
    default: low
    metadata_key: stack_gain
    maps_to: {kind: launch_arg, name: stack_gain, level_values: {low: "1.0", high: "2.5"}}
"""


def _project(tmp_path):
    pkg = tmp_path / "ros2_ws" / "src" / "demo_pkg"
    (pkg / "launch").mkdir(parents=True)
    (pkg / "config").mkdir()
    (pkg / "package.xml").write_text("<package><name>demo_pkg</name></package>", encoding="utf-8")
    (pkg / "launch" / "stack.launch.py").write_text(LAUNCH, encoding="utf-8")
    (pkg / "config" / "stack.yaml").write_text(PARAMS_YAML, encoding="utf-8")
    # A stale installed copy must never be the one proposed.
    inst = tmp_path / "ros2_ws" / "install" / "demo_pkg" / "launch"
    inst.mkdir(parents=True)
    (inst / "stack.launch.py").write_text("'kp': 7.0", encoding="utf-8")
    return tmp_path


def test_a_proposal_names_the_factor_the_launch_default_and_the_yaml(tmp_path):
    from robofacts import campaign_spec as cs2
    from robofacts import explore_propose as epr
    root = make_root(tmp_path / "exp")
    (root / "common" / "config" / "factors.yaml").write_text(FACTORS_GAIN, encoding="utf-8")
    factors = cs2.load_factors("expA", root)
    p = epr.propose("/stack", "kp", 1.0, 2.5, project_root=_project(tmp_path), launch_package="demo_pkg",
                    launch_file="stack.launch.py", factors=factors, condition={"stack_gain": "low"})
    kinds = {c["kind"]: c for c in p["candidates"]}
    assert set(kinds) == {"launch_arg", "launch_default", "factor", "yaml"}
    assert kinds["launch_arg"]["file"] == "ros2_ws/src/demo_pkg/launch/stack.launch.py"
    assert kinds["launch_default"]["suggestion"] == "'stack_gain', default_value='2.5', description='the gain')"
    assert "level 'high' of factor stack_gain gives 2.5" in kinds["factor"]["note"]
    assert "overridden by the launch file" in kinds["yaml"]["note"]
    # A literal in the launch file, and a YAML value only in the node's own section.
    lit = epr.propose("/stack", "ki", 0.1, 0.2, project_root=tmp_path, launch_package="demo_pkg",
                      launch_file="stack.launch.py", factors=factors)
    assert [c["kind"] for c in lit["candidates"]] == ["literal"]
    assert "'ki': 0.2" in lit["candidates"][0]["suggestion"]
    kd = epr.propose("/stack", "kd", 0.5, 0.7, project_root=tmp_path, launch_package="demo_pkg",
                     launch_file="stack.launch.py", factors=factors)
    assert [(c["kind"], c["suggestion"]) for c in kd["candidates"]] == [("yaml", "kd: 0.7")]
    none = epr.propose("/stack", "kz", 1, 2, project_root=tmp_path, launch_package="demo_pkg",
                       launch_file="stack.launch.py", factors=factors)
    assert not none["candidates"] and "declare_parameter('kz'" in none["summary"]
    assert "proposed: `kd: 0.7`" in epr.markdown(kd, "t")


def test_keep_it_writes_proposed_changes_md_and_works_after_stop(tmp_path):
    controls, _, _, root, out_dir = bound(tmp_path, events=EVENTS)
    controls._kept["config"].project_root = _project(tmp_path / "proj")
    controls._kept["config"].launch_package = "demo_pkg"
    ok, msg, proposal = controls.propose("/stack", "kp")
    assert ok and proposal["to"] == 2.5 and proposal["candidates"]
    assert not controls.propose("/stack", "ki")[0]            # never changed
    controls.shutdown()
    ok, _, again = controls.propose("/stack", "kp")            # from the last table
    assert ok and again["from"] == 1.0
    text = (out_dir / "proposed_changes.md").read_text()
    assert text.startswith("# Proposed changes: expA") and text.count("## /stack.kp: 1.0 -> 2.5") == 2
    assert "Nothing was applied" in text

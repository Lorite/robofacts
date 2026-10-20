"""Tests for the trial runner (#96 Phase 4).

No hardware and no ROS: commands are built purely and executed through an
injected executor, so every ordering and safety property is asserted offline.
"""

from __future__ import annotations

import json
import sys
import shlex
from types import SimpleNamespace
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import trial_index as ti
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
cf_name: cf1
hosts:
  laptop: {local: true}
  orin: {host: orin-host, container: ros2_jetson_dev, shell: zsh}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 2
sweep:
  trajectory: [hover, circle]
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    return tmp_path


@pytest.fixture
def pieces(root: Path):
    factors = cs.load_factors("expA", root)
    config = tr.RunnerConfig.load("expA", root)
    trial = cs.TrialSpec("expA", {"execution_host": "laptop", "record_profile": "position_only",
                                  "trajectory": "circle"}, ["execution_host", "trajectory"], 1)
    return factors, config, trial


class FakeHooks:
    """Stands in for the three process primitives, recording what was asked."""

    def __init__(self, action_rc: int = 0, raise_in_action: bool = False,
                 records_bag: bool = True):
        self.started: list = []
        self.foreground: list = []
        self.stopped: list = []
        self.action_rc = action_rc
        self.raise_in_action = raise_in_action
        # A recorder that starts and then writes nothing is a real failure mode
        # (unwritable mount, bad path, no subscription), so it is worth being
        # able to fake it.
        self.records_bag = records_bag

    def hooks(self):
        return tr.ProcessHooks(start_background=self._start,
                               run_foreground=self._foreground,
                               stop_process=self._stop)

    def _start(self, cmd, target, *, cwd, label="", quiet_sink=None):
        self.started.append(cmd)
        self.quiet_sinks = getattr(self, "quiet_sinks", []) + [(label, quiet_sink)]
        if self.records_bag and "record" in cmd and "-o" in cmd:
            bag = Path(cmd[cmd.index("-o") + 1])
            bag.mkdir(parents=True, exist_ok=True)
            (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n",
                                               encoding="utf-8")
            (bag / f"{bag.name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")
        return object()

    def _foreground(self, cmd, target, *, cwd, check=True):
        self.foreground.append(cmd)
        if self.raise_in_action:
            raise RuntimeError("boom")
        return SimpleNamespace(returncode=self.action_rc)

    def _stop(self, proc, target, *, name, kind):
        self.stopped.append((name, kind))


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #
def test_launch_args_come_only_from_launch_arg_factors(pieces):
    factors, config, trial = pieces
    cmd = tr.launch_command(trial, factors, config)
    assert cmd[:4] == ["ros2", "launch", "lorite_experiments", "flight.launch.py"]
    assert "trajectory:=circle" in cmd
    assert not any(c.startswith("execution_host") for c in cmd), "the host is not a launch arg"
    assert not any(c.startswith("record_profile") for c in cmd)


def test_an_empty_launch_arg_value_is_dropped_not_passed_bare(root):
    """An empty factor value must not become `name:=`, which kills the whole stack.

    Regression for 2026-08-14 (lab day 5). `pose_source: mocap` carries
    `observer_pose_topic_override: ""`, and the runner rendered it as a bare
    `observer_pose_topic_override:=`. `ros2 launch` rejects that token outright,
    so the ENTIRE rig failed to start and the T-0 gate then reported three
    hardware-shaped failures — no Spot driver node, no /mocap/drone_pose, no
    mocap frames — none of which were hardware. The launch file declares the
    same "" default, so dropping the argument is behaviour-preserving.
    """
    (root / "common" / "config" / "factors.yaml").write_text(
        CORE + """  pose_source:
    levels: [mocap, markerless]
    default: mocap
    metadata_key: pose_source
    maps_to:
      kind: launch_arg
      names: [observer_pose_topic_override]
      level_values:
        mocap: {observer_pose_topic_override: ""}
        markerless: {observer_pose_topic_override: /markerless/drone_pose}
""", encoding="utf-8")
    factors = cs.load_factors("expA", root)
    config = tr.RunnerConfig.load("expA", root)

    empty = cs.TrialSpec("expA", {"execution_host": "laptop",
                                  "record_profile": "position_only",
                                  "trajectory": "circle",
                                  "pose_source": "mocap"},
                         ["execution_host", "trajectory"], 1)
    cmd = tr.launch_command(empty, factors, config)
    assert "observer_pose_topic_override:=" not in cmd
    assert not any(c.startswith("observer_pose_topic_override") for c in cmd), \
        "an empty value must be omitted entirely, not passed in any form"
    assert "trajectory:=circle" in cmd, "the other launch args still go through"

    # The non-empty level is unaffected — this must not silently swallow real values.
    real = cs.TrialSpec("expA", {"execution_host": "laptop",
                                 "record_profile": "position_only",
                                 "trajectory": "circle",
                                 "pose_source": "markerless"},
                        ["execution_host", "trajectory"], 1)
    assert "observer_pose_topic_override:=/markerless/drone_pose" in \
        tr.launch_command(real, factors, config)


def test_a_launch_arg_value_with_a_space_stays_one_argument(root):
    """The guide's advice for a launch file without a headless switch (2026-10-15): append
    the simulator's own flag to an argument the launch file pastes into its command line, as
    the adoption test did with `world_file: "<world>.sdf -s"`. That works only if the value
    reaches `ros2 launch` as ONE argument, locally (Popen gets the list) and remotely
    (ssh + docker exec + shell -c)."""
    from robofacts import remote_exec_utils as rx

    (root / "common" / "config" / "factors.yaml").write_text(
        CORE + """  world:
    levels: [ground]
    default: ground
    metadata_key: world
    maps_to:
      kind: launch_arg
      names: [world_file]
      level_values:
        ground: {world_file: "/w/ground_plane.sdf -s"}
""", encoding="utf-8")
    factors = cs.load_factors("expA", root)
    config = tr.RunnerConfig.load("expA", root)
    trial = cs.TrialSpec("expA", {"execution_host": "laptop", "record_profile": "position_only",
                                  "trajectory": "circle", "world": "ground"},
                         ["execution_host", "trajectory"], 1)
    cmd = tr.launch_command(trial, factors, config)
    assert "world_file:=/w/ground_plane.sdf -s" in cmd

    local = rx.local_setup_command(cmd, ["/ws/install/setup.zsh"])
    assert shlex.split(local[2].split("&& exec ", 1)[1]) == cmd
    target = rx.RemoteTarget(host="orin-host", container="ros2_jetson_dev", workspace="/ws",
                             ros_setup="/opt/ros/humble/setup.zsh", ws_setup="/ws/setup.zsh")
    inner = shlex.split(rx.wrap_command(cmd, target)[-1])[-1]
    assert shlex.split(inner.split("&& exec ", 1)[1]) == cmd


def test_launch_mode_run_uses_ros2_run(root):
    """A single-node stack (and every demo) is `ros2 run`, not a launch file."""
    (root / "expA" / "config" / "runner.yaml").write_text(
        "launch_mode: run\nlaunch: {package: demo_nodes_cpp, file: talker}\n"
        "hosts: {laptop: {local: true}}\n", encoding="utf-8")
    config = tr.RunnerConfig.load("expA", root)
    factors = cs.load_factors("expA", root)
    trial = cs.TrialSpec("expA", {"trajectory": "circle"}, [], 1)
    assert tr.launch_command(trial, factors, config) == [
        "ros2", "run", "demo_nodes_cpp", "talker"]


def test_an_invalid_launch_mode_is_rejected(root):
    (root / "expA" / "config" / "runner.yaml").write_text(
        "launch_mode: teleport\nlaunch: {package: p, file: f}\n", encoding="utf-8")
    with pytest.raises(tr.RunnerError, match="launch_mode must be"):
        tr.RunnerConfig.load("expA", root)


def test_explicit_record_topics_beat_the_profile_lookup(root):
    """An experiment whose topics are not one of the flight sets lists them
    outright instead of inventing a profile in record_profiles.py."""
    (root / "expA" / "config" / "runner.yaml").write_text(
        "launch: {package: p, file: f}\nrecord_topics: ['/chatter', '/rosout']\n"
        "hosts: {laptop: {local: true}}\n", encoding="utf-8")
    config = tr.RunnerConfig.load("expA", root)
    cmd = tr.record_command(cs.TrialSpec("expA", {}, [], 1), {}, config, Path("/bags/x"))
    assert cmd[:3] == ["ros2", "bag", "record"]
    assert cmd[-2:] == ["/chatter", "/rosout"]


def test_record_profile_factor_selects_the_topic_set(pieces, monkeypatch):
    factors, config, trial = pieces
    captured = {}

    def fake_build(profile, output, **kwargs):
        captured["profile"] = profile
        return ["ros2", "bag", "record", "-o", output]

    monkeypatch.setitem(sys.modules, "robofacts.record_profiles",
                        type("m", (), {"build_record_command": staticmethod(fake_build),
                                       "default_cf_name": staticmethod(lambda: "cf1")}))
    tr.record_command(trial, factors, config, Path("/bags/x"))
    assert captured["profile"] == "position_only"


def test_action_is_templated_from_factor_values(pieces):
    _, config, trial = pieces
    assert tr.action_command(trial, config) == ["python3", "fly.py", "--shape", "circle"]


def test_action_referencing_an_unknown_factor_is_rejected(pieces):
    _, config, trial = pieces
    config.action = "python3 fly.py --shape {nonexistent}"
    with pytest.raises(tr.RunnerError, match="unknown factor"):
        tr.action_command(trial, config)


def test_action_gets_the_bag_placeholder(pieces):
    """{bag} in the action renders like it does for the verify steps: it is how
    a flight script learns where to write commanded_waypoints.json."""
    _, config, trial = pieces
    config.action = ("python3 fly.py --shape {trajectory} "
                     "--waypoints-json {bag}/commanded_waypoints.json")
    cmd = tr.action_command(trial, config, extra={"bag": "/b/trial01"})
    assert cmd == ["python3", "fly.py", "--shape", "circle",
                   "--waypoints-json", "/b/trial01/commanded_waypoints.json"]


def test_run_trial_hands_the_action_its_own_bag_dir(root, pieces):
    """End to end: the {bag} the action receives is THE trial's bag directory,
    the one the recorder writes, so the flight-time commanded_waypoints.json
    lands beside the bag it describes."""
    factors, config, trial = pieces
    config.action = ("python3 fly.py --shape {trajectory} "
                     "--waypoints-json {bag}/commanded_waypoints.json")
    fake = FakeHooks()
    result = tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                          hooks=fake.hooks(), slim=False)
    action = fake.foreground[0]
    assert action[-1] == f"{result.bag_dir}/commanded_waypoints.json"


def test_bag_name_carries_the_non_defaults_and_repetition(pieces):
    factors, _, trial = pieces
    name = tr.bag_name(trial, factors, "20260725T120000")
    assert name.startswith("20260725T120000")
    assert "trajectory-circle" in name and "rep01" in name
    assert "record_profile" not in name, "defaults would only add noise"


# --------------------------------------------------------------------------- #
# The loop-closing property
# --------------------------------------------------------------------------- #
def test_metadata_records_every_factor_under_its_metadata_key(pieces):
    """Without this the bag the runner just produced is invisible to the index
    and uncountable by the expander, so the campaign asks for it forever."""
    factors, config, trial = pieces
    metadata = tr.trial_metadata(trial, factors, config)
    assert metadata["trajectory"] == "circle"
    assert metadata["execution_host"] == "laptop"
    assert metadata["record_profile"] == "position_only"
    assert metadata["aborted"] is False and metadata["repetition"] == 1


def test_a_produced_bag_is_findable_by_the_expander(root, pieces):
    """End-to-end: run a trial, then confirm the plan now counts it."""
    factors, config, _ = pieces
    campaign = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root,
                                ["execution_host=laptop"])
    bags = root / "expA" / "bags"
    before = cp.build_plan(campaign, ti.load_trials(root, "expA"))
    assert before.needed == 4

    tr.run_plan(before, config, bags, dry_run=False, limit=1,
                hooks=FakeHooks().hooks(), slim=False)
    after = cp.build_plan(campaign, ti.load_trials(root, "expA"))
    assert after.needed == 3, "the trial the runner just flew must count as coverage"


# --------------------------------------------------------------------------- #
# Remote wrapping
# --------------------------------------------------------------------------- #
def test_a_local_host_level_yields_no_remote_target(pieces):
    _, config, _ = pieces
    assert config.remote_target("laptop") is None
    assert config.remote_target(None) is None


def test_a_remote_host_level_yields_a_remote_target(pieces):
    _, config, _ = pieces
    target = config.remote_target("orin")
    assert target.host == "orin-host" and target.container == "ros2_jetson_dev"
    assert target.shell == "zsh", "bash loses CYCLONEDDS_URI and DDS discovery"


def test_a_host_spec_without_a_target_is_rejected(pieces):
    _, config, _ = pieces
    config.hosts["broken"] = {"container": "c"}
    with pytest.raises(tr.RunnerError, match="neither"):
        config.remote_target("broken")


def test_running_inside_the_target_container_short_circuits_to_local(pieces, monkeypatch):
    """On the rig, the ssh+docker wrapper would loop back to where the runner
    already is — and the ssh leg is a network dependency a trial must not have
    (2026-08-10: it hung the on-rig shadow session through a dead Tailscale)."""
    _, config, _ = pieces
    config.hosts["orin"]["container_hostname"] = "jetson-dev"
    monkeypatch.setattr(tr.socket, "gethostname", lambda: "jetson-dev")
    assert config.remote_target("orin") is None


def test_elsewhere_the_container_hostname_changes_nothing(pieces, monkeypatch):
    _, config, _ = pieces
    config.hosts["orin"]["container_hostname"] = "jetson-dev"
    monkeypatch.setattr(tr.socket, "gethostname", lambda: "somewhere-else")
    target = config.remote_target("orin")
    assert target is not None and target.host == "orin-host"


# --------------------------------------------------------------------------- #
# Execution order and the gate
# --------------------------------------------------------------------------- #
def test_dry_run_executes_nothing(root, pieces):
    factors, config, trial = pieces
    fake = FakeHooks()
    result = tr.run_trial(trial, factors, config, root / "bags",
                          dry_run=True, hooks=fake.hooks())
    assert result.status == "dry-run"
    assert fake.started == [] and fake.foreground == [], "--dry-run must not touch anything"
    assert len(result.commands) == 3, "it should still show launch, record and action"


def test_execution_order_is_launch_then_record_then_action(root, pieces):
    factors, config, trial = pieces
    fake = FakeHooks()
    tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                 hooks=fake.hooks(), slim=False)
    assert fake.started[0][:2] == ["ros2", "launch"]
    assert fake.started[1][:3] == ["ros2", "bag", "record"]
    assert fake.foreground[0][0] == "python3"


def test_teardown_stops_the_recorder_before_the_stack(root, pieces):
    """rosbag2 must finalize before the stack goes down; the other order has
    produced truncated bags."""
    factors, config, trial = pieces
    fake = FakeHooks()
    tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                 hooks=fake.hooks(), slim=False)
    assert fake.stopped == [("record", "recorder"), ("launch_stack", "launch")]


def test_teardown_still_runs_when_the_action_raises(root, pieces):
    """Teardown lives in a finally block, so a crash must not leave an orphaned
    recorder holding an unfinalized bag."""
    factors, config, trial = pieces
    fake = FakeHooks(raise_in_action=True)
    result = tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                          hooks=fake.hooks(), slim=False)
    assert [n for n, _ in fake.stopped] == ["record", "launch_stack"]
    assert result.status == "failed" and "boom" in result.detail


def test_a_failing_action_is_recorded_but_the_bag_is_still_closed(root, pieces):
    """A trial whose action failed never flew, so it must not read as success.

    It is written up as ABORTED (which the index excludes from coverage) rather
    than dropped: the bag stays accountable, with its reason attached. Before
    2026-07-29 it was reported as `ran` and counted as coverage, so a campaign
    could be satisfied by trials that never left the ground.
    """
    factors, config, trial = pieces
    fake = FakeHooks(action_rc=3)
    result = tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                          hooks=fake.hooks(), slim=False)
    assert "rc=3" in result.detail
    assert result.status == "failed", "a non-flight must not be reported as ran"
    assert fake.stopped, "the recorder must still be stopped"
    metadata = json.loads((result.bag_dir / "trial_metadata.json").read_text())
    assert "rc=3" in metadata["notes"], "the failure must travel with the bag"
    assert metadata["aborted"] is True, "a non-flight must not count as coverage"


class BlockingGate:
    def check(self, label):
        raise RuntimeError("pre-flight gate FAIL: battery unreadable")


class PassingGate:
    class _R:
        def as_metadata(self):
            return {"status": "pass", "allowed": True}

    def check(self, label):
        return self._R()


def test_a_blocked_gate_prevents_recording_and_flying(root, pieces):
    """A trial that cannot pass pre-flight must not record or fly, and must not
    leave the stack it brought up running.

    The stack IS started first, deliberately: the gate checks camera frames,
    mocap continuity, battery and e-stop reachability, none of which exist
    until the rig is up. What must never happen is a bag or a flight.
    """
    factors, config, trial = pieces
    fake = FakeHooks()
    result = tr.run_trial(trial, factors, config, root / "bags", gate=BlockingGate(),
                          dry_run=False, hooks=fake.hooks(), slim=False)
    assert result.status == "blocked"
    assert result.bag_dir is None, "a blocked trial must not claim a bag directory"
    assert not any("record" in cmd for cmd in fake.started), "a no-go still recorded"
    assert fake.foreground == [], "a no-go still ran the action"
    assert fake.stopped, "the stack brought up for the gate was left running"


def test_the_gate_result_is_written_into_the_bag(root, pieces):
    factors, config, trial = pieces
    result = tr.run_trial(trial, factors, config, root / "bags", gate=PassingGate(),
                          dry_run=False, hooks=FakeHooks().hooks(), slim=False)
    metadata = json.loads((result.bag_dir / "trial_metadata.json").read_text())
    assert metadata["preflight"]["status"] == "pass"


# --------------------------------------------------------------------------- #
# Plan-driven running
# --------------------------------------------------------------------------- #
def test_run_plan_runs_exactly_what_is_needed(root, pieces):
    _, config, _ = pieces
    campaign = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    plan = cp.build_plan(campaign, [])
    results = tr.run_plan(plan, config, root / "bags", dry_run=True)
    assert len(results) == plan.needed == 4


def test_run_plan_honours_limit(root, pieces):
    _, config, _ = pieces
    plan = cp.build_plan(cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root), [])
    assert len(tr.run_plan(plan, config, root / "bags", dry_run=True, limit=1)) == 1


def test_run_plan_stops_on_a_blocked_gate(root, pieces):
    """If the stack cannot pass pre-flight, the next trial will not either -
    grinding through a campaign producing nothing is worse than stopping."""
    _, config, _ = pieces
    plan = cp.build_plan(cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root), [])
    results = tr.run_plan(plan, config, root / "bags", gate=BlockingGate(),
                          dry_run=False, hooks=FakeHooks().hooks(), slim=False)
    assert len(results) == 1 and results[0].status == "blocked"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_a_missing_runner_config_is_a_clear_error(tmp_path):
    with pytest.raises(tr.RunnerError, match="no config/runner.yaml"):
        tr.RunnerConfig.load("nope", tmp_path)


def test_a_runner_config_without_a_launch_file_is_rejected(root):
    (root / "expA" / "config" / "runner.yaml").write_text(
        "launch: {package: p}\n", encoding="utf-8")
    with pytest.raises(tr.RunnerError, match="launch.file is required"):
        tr.RunnerConfig.load("expA", root)


def test_shipped_runner_configs_load():
    for path in tr.EXPERIMENTS_ROOT.glob(f"*/config/{tr.RUNNER_CONFIG_NAME}"):
        assert tr.RunnerConfig.load(path.parents[1].name)


# --------------------------------------------------------------------------- #
# An empty bag must never become coverage
#
# Found on the first real remote run (laptop -> Lab PC): recording failed on the
# rig because the mounted workspace was not writable by the container user, yet
# the runner wrote trial_metadata.json *locally* and reported "0 failed". The
# laptop was left holding metadata-only directories that the coverage grid
# happily counted, so the campaign declared itself complete having recorded
# nothing at all. Both halves of that are tested here.
# --------------------------------------------------------------------------- #
def test_a_trial_whose_recorder_wrote_nothing_is_failed(root, pieces):
    factors, config, trial = pieces
    result = tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                          hooks=FakeHooks(records_bag=False).hooks(), slim=False)
    assert result.status == "failed"
    assert "no bag recorded" in result.detail


def test_a_trial_whose_recorder_wrote_nothing_leaves_no_metadata(root, pieces):
    """Writing trial_metadata.json is what makes a directory count as a trial,
    so a failed recording must not get one -- otherwise the empty cell it should
    have left behind looks permanently filled."""
    factors, config, trial = pieces
    result = tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                          hooks=FakeHooks(records_bag=False).hooks(), slim=False)
    assert not (result.bag_dir / "trial_metadata.json").exists()


def test_a_metadata_only_directory_is_not_counted_as_a_trial(tmp_path):
    """The index's own guard against the same shape, whatever produced it."""
    bag = tmp_path / "bags" / "20260101T000000_rep01"
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text("{}", encoding="utf-8")
    trial = ti.Trial(experiment="x", bag_dir=bag, metadata={})
    assert not trial.has_bag_data
    assert trial.status == "excluded"


def test_an_archive_view_counts_a_recording_kept_on_a_trial_host(tmp_path):
    """The home server's archive copies the records without the heavy .mcap and
    says where the recording is. The same trial must keep its class there."""
    bag = tmp_path / "bags" / "20260101T000000_rep01"
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text("{}", encoding="utf-8")
    (bag / ti.RECORDINGS_ELSEWHERE).write_text(
        '{"copies": [{"host": "orin", "files": [{"name": "x_0.mcap", "bytes": 1}]}]}',
        encoding="utf-8")
    trial = ti.Trial(experiment="x", bag_dir=bag, metadata={})
    assert trial.has_bag_data
    assert trial.status != "excluded"


def test_trial_id_is_the_bag_directory_name(root, pieces):
    """The id inside the bag must identify the bag it is inside. It used to be
    re-derived from the clock when the metadata was written, so it drifted by
    the duration of the run — and rep01's recorded id could match rep02's
    directory name, which is worse than useless for tracing a result back."""
    factors, config, trial = pieces
    result = tr.run_trial(trial, factors, config, root / "bags", dry_run=False,
                          hooks=FakeHooks().hooks(), slim=False)
    metadata = json.loads((result.bag_dir / "trial_metadata.json").read_text())
    assert metadata["trial_id"] == result.bag_dir.name


# --------------------------------------------------------------------------- #
# Setup steps and auxiliary launches (#96 Phase 6)
#
# The real experiments are not "one launch + one action". The spot run notebook
# sets Spot's arm-camera mode over the SDK, brings the stack up, runs a
# foreground spot setup, optionally starts arm-tracking alongside, and only then
# flies. Without these two concepts the notebooks cannot be retired, because the
# framework would be unable to express what they do.
# --------------------------------------------------------------------------- #
SETUP_RUNNER = """
launch: {package: p, file: f.launch.py}
setup_steps:
  - {name: arm_camera, command: "python3 set_camera.py --mode {trajectory}"}
  - {name: spot_setup, command: "ros2 run lorite_experiments spot_setup"}
aux_launches:
  - {name: arm_track, command: "ros2 launch lorite_experiments arm_track.launch.py"}
hosts: {laptop: {local: true}}
"""


@pytest.fixture
def with_setup(root: Path):
    (root / "expA" / "config" / "runner.yaml").write_text(SETUP_RUNNER, encoding="utf-8")
    return tr.RunnerConfig.load("expA", root)


def test_setup_steps_run_before_the_stack_and_are_templated(root, with_setup, pieces):
    factors, _, trial = pieces
    fake = FakeHooks()
    tr.run_trial(trial, factors, with_setup, root / "bags", dry_run=False,
                 hooks=fake.hooks(), slim=False)
    assert fake.foreground[0] == ["python3", "set_camera.py", "--mode", "circle"]
    assert fake.foreground[1][:2] == ["ros2", "run"]


def test_a_failing_required_setup_step_aborts_the_trial(root, with_setup, pieces):
    """A rig that was never configured must not produce a bag that looks like a
    valid trial — the setup steps are preconditions, not best-effort."""
    factors, _, trial = pieces
    result = tr.run_trial(trial, factors, with_setup, root / "bags", dry_run=False,
                          hooks=FakeHooks(action_rc=0).hooks(), slim=False)
    assert result.status == "ran"  # sanity: the passing case still runs

    failing = FakeHooks()
    failing._foreground = lambda cmd, target, *, cwd, check=True: SimpleNamespace(returncode=3)
    result = tr.run_trial(trial, factors, with_setup, root / "bags", dry_run=False,
                          hooks=tr.ProcessHooks(start_background=failing._start,
                                                run_foreground=failing._foreground,
                                                stop_process=failing._stop),
                          slim=False)
    assert result.status == "failed" and "arm_camera" in result.detail


def test_aux_launches_start_after_the_stack_and_stop_before_it(root, with_setup, pieces):
    """Order matters on teardown: the recorder finalizes first, then the helper
    launches, then the stack they depend on."""
    factors, _, trial = pieces
    fake = FakeHooks()
    tr.run_trial(trial, factors, with_setup, root / "bags", dry_run=False,
                 hooks=fake.hooks(), slim=False)
    stopped = [name for name, _ in fake.stopped]
    assert stopped == ["record", "aux:arm_track", "launch_stack"]


def test_a_setup_step_without_a_command_is_rejected(root):
    (root / "expA" / "config" / "runner.yaml").write_text(
        "launch: {package: p, file: f}\nsetup_steps: [{name: x}]\n", encoding="utf-8")
    with pytest.raises(tr.RunnerError, match="needs a non-empty 'command'"):
        tr.RunnerConfig.load("expA", root)


def test_dry_run_shows_setup_and_aux_commands_too(root, with_setup, pieces):
    """--dry-run is the safety review before a flight; hiding the setup steps
    would make it an incomplete preview of what touches the robot."""
    factors, _, trial = pieces
    result = tr.run_trial(trial, factors, with_setup, root / "bags", dry_run=True,
                          hooks=FakeHooks().hooks())
    flat = [" ".join(c) for c in result.commands]
    assert any("set_camera.py" in c for c in flat)
    assert any("arm_track.launch.py" in c for c in flat)


# --------------------------------------------------------------------------- #
# build_gate — the CLI's T-0 gate wiring (2026-08-11: until then the CLI
# never constructed a gate, so --execute ran every trial UNGATED while the
# checklist sat unread; found via a 2x120s recording against an unreachable
# Spot that host_reachable would have refused before the recorder started)
# --------------------------------------------------------------------------- #
def _campaign(root, experiment="expG", checklist_rel=""):
    (root / experiment / "config").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(experiment=experiment, preflight_checklist=checklist_rel)


def test_dry_run_builds_no_gate_even_with_a_checklist(tmp_path):
    campaign = _campaign(tmp_path)
    (tmp_path / "expG" / "config" / "preflight_checklist.json").write_text("{}")
    assert tr.build_gate(campaign, tmp_path, execute=False) is None


def test_execute_arms_the_campaign_scoped_checklist(tmp_path):
    campaign = _campaign(tmp_path, checklist_rel="config/custom_gate.json")
    (tmp_path / "expG" / "config" / "custom_gate.json").write_text("{}")
    gate = tr.build_gate(campaign, tmp_path, execute=True)
    assert gate is not None
    assert gate.checklist.name == "custom_gate.json"
    assert gate.override is False


def test_execute_falls_back_to_the_experiment_default_checklist(tmp_path):
    campaign = _campaign(tmp_path)
    (tmp_path / "expG" / "config" / "preflight_checklist.json").write_text("{}")
    gate = tr.build_gate(campaign, tmp_path, execute=True)
    assert gate is not None
    assert gate.checklist.name == "preflight_checklist.json"


def test_execute_without_any_checklist_warns_ungated(tmp_path, capsys):
    campaign = _campaign(tmp_path)
    gate = tr.build_gate(campaign, tmp_path, execute=True)
    assert gate is None
    assert "UNGATED" in capsys.readouterr().out


def test_gate_override_reason_is_carried_into_the_gate(tmp_path):
    campaign = _campaign(tmp_path)
    (tmp_path / "expG" / "config" / "preflight_checklist.json").write_text("{}")
    gate = tr.build_gate(campaign, tmp_path, execute=True,
                         override_reason="radio dead, static trial, discussed")
    assert gate.override is True
    assert gate.override_reason == "radio dead, static trial, discussed"


# --------------------------------------------------------------------------- #
# --quiet-stack (2026-08-11, the cockpit-readability request): child process
# output goes only to the run log so the operator terminal keeps the campaign
# narrative instead of duplicating the log pane
# --------------------------------------------------------------------------- #
def test_quiet_children_hands_the_run_log_to_every_background_start(root, with_setup, pieces):
    factors, _, trial = pieces
    fake = FakeHooks()
    tr.run_trial(trial, factors, with_setup, root / "bags", dry_run=False,
                 hooks=fake.hooks(), slim=False, quiet_children=True)
    sinks = dict(fake.quiet_sinks)
    assert sinks.get("stack") is not None, "the launch stack must pump into the log"
    assert sinks.get("recorder") is not None
    assert all(s is not None for _, s in fake.quiet_sinks)


def test_default_keeps_children_on_the_terminal(root, with_setup, pieces):
    factors, _, trial = pieces
    fake = FakeHooks()
    tr.run_trial(trial, factors, with_setup, root / "bags", dry_run=False,
                 hooks=fake.hooks(), slim=False)
    assert all(s is None for _, s in fake.quiet_sinks)


# --------------------------------------------------------------------------- #
# SCRATCH FLIGHTS (2026-08-12). Diagnostic flights that are not campaign trials
# used to be flown through hand-built tmux panes, and the panes collected every
# failure the framework exists to prevent: a drone that silently never armed
# (no T-0 gate), a recorder pre-typed with an already-existing bag name that
# captured 53 s of a landed drone, five panes juggled mid-flight. --scratch is
# run_trial with the campaign's own apparatus, recording to bags_scratch/,
# which coverage must never read.
# --------------------------------------------------------------------------- #
def _scratch_campaign(root):
    return cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root,
                            ["execution_host=laptop"])


def test_scratch_records_to_bags_scratch_with_the_marker(root, pieces):
    _, config, _ = pieces
    result = tr.run_scratch(_scratch_campaign(root), config,
                            root / "expA" / "bags_scratch", dry_run=False,
                            hooks=FakeHooks().hooks(), label="battery check!")
    assert result.status == "ran", result.detail
    assert result.bag_dir.parent.name == "bags_scratch"
    assert "_scratch_" in result.bag_dir.name
    assert "battery-check" in result.bag_dir.name, "the label is sanitised into the name"
    metadata = json.loads((result.bag_dir / "trial_metadata.json").read_text())
    assert metadata["scratch"] is True, "the marker is explicit, not inferred from the path"
    assert metadata["scratch_label"] == "battery-check"
    assert metadata["aborted"] is False


def test_scratch_bags_are_invisible_to_coverage_and_the_plan(root, pieces):
    """The whole point: a scratch flight satisfies no cell, ever."""
    _, config, _ = pieces
    campaign = _scratch_campaign(root)
    before = cp.build_plan(campaign, ti.load_trials(root, "expA")).needed
    result = tr.run_scratch(campaign, config, root / "expA" / "bags_scratch",
                            dry_run=False, hooks=FakeHooks().hooks())
    assert (result.bag_dir / "trial_metadata.json").exists(), "sanity: it did record"
    assert ti.load_trials(root, "expA") == [], "the index must never load bags_scratch/"
    after = cp.build_plan(campaign, ti.load_trials(root, "expA")).needed
    assert after == before == 4, "the campaign still wants every trial it wanted before"


def test_a_scratch_bag_moved_into_bags_is_still_excluded(tmp_path):
    """The second lock: the explicit metadata marker holds even where the
    directory-name exclusion cannot."""
    bag = tmp_path / "bags" / "20260812T000000_scratch_x"
    bag.mkdir(parents=True)
    (bag / f"{bag.name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")
    trial = ti.Trial(experiment="x", bag_dir=bag, metadata={"scratch": True})
    assert trial.status == "excluded"


def test_the_gate_still_blocks_a_scratch_flight(root, pieces):
    """The motivating incident: the manual path skipped the T-0 gate and a
    drone with a latched firmware e-stop silently never armed. Scratch mode
    exists so that gate runs, so it must block exactly like a real trial."""
    _, config, _ = pieces
    fake = FakeHooks()
    result = tr.run_scratch(_scratch_campaign(root), config,
                            root / "expA" / "bags_scratch",
                            gate=BlockingGate(), dry_run=False, hooks=fake.hooks())
    assert result.status == "blocked"
    assert result.bag_dir is None, "a blocked scratch flight must not claim a bag"
    assert not any(cmd[:3] == ["ros2", "bag", "record"] for cmd in fake.started)
    assert fake.foreground == [], "a no-go still flew the scratch action"
    assert fake.stopped, "the stack brought up for the gate was left running"


def test_scratch_skips_the_verify_steps(root, pieces):
    _, config, _ = pieces
    config.verify_steps = [{"name": "flight_happened",
                            "command": "python3 verify.py {bag}", "required": True}]
    campaign = _scratch_campaign(root)
    campaign.verify_steps = [{"name": "campaign_check",
                              "command": "python3 campaign_verify.py {bag}"}]
    fake = FakeHooks()
    result = tr.run_scratch(campaign, config, root / "expA" / "bags_scratch",
                            dry_run=False, hooks=fake.hooks())
    assert result.status == "ran", result.detail
    flat = [" ".join(c) for c in fake.foreground]
    assert not any("verify.py" in c or "campaign_verify.py" in c for c in flat), \
        "scratch recordings answer ad-hoc questions; the campaign's verify contract does not apply"


def test_consecutive_scratch_flights_never_collide(root, pieces):
    """The recorder-name incident: a pre-typed, already-existing bag name
    captured 53 s of a landed drone. Scratch names are generated at run time
    and de-collided against the disk."""
    factors, config, trial = pieces
    fixed_clock = lambda: "20260812T120000"  # noqa: E731 - same second, twice
    first = tr.run_trial(trial, factors, config, root / "expA" / "bags_scratch",
                         dry_run=False, hooks=FakeHooks().hooks(), slim=False,
                         scratch_label="hover", clock=fixed_clock)
    second = tr.run_trial(trial, factors, config, root / "expA" / "bags_scratch",
                          dry_run=False, hooks=FakeHooks().hooks(), slim=False,
                          scratch_label="hover", clock=fixed_clock)
    assert first.bag_dir.name == "20260812T120000_scratch_hover"
    assert second.bag_dir.name == "20260812T120000_scratch_hover-2"
    assert second.status == "ran", second.detail


def test_scratch_recording_cannot_be_disabled(root, pieces):
    """An unrecorded diagnostic is an anecdote, so --scratch forces the
    recorder on even if a caller tries to toggle it off."""
    factors, config, trial = pieces
    fake = FakeHooks()
    result = tr.run_trial(trial, factors, config, root / "expA" / "bags_scratch",
                          dry_run=False, hooks=fake.hooks(), slim=False,
                          scratch_label="x", steps={"record": False})
    assert any(cmd[:3] == ["ros2", "bag", "record"] for cmd in fake.started)
    assert (result.bag_dir / "trial_metadata.json").exists()


def test_scratch_default_action_is_the_campaigns_own_shape(root, pieces):
    """No --action means the campaign's templated action with its first
    combination: defaults + fixed + the first swept level."""
    _, config, _ = pieces
    result = tr.run_scratch(_scratch_campaign(root), config,
                            root / "expA" / "bags_scratch", dry_run=True)
    assert result.status == "dry-run"
    assert ["python3", "fly.py", "--shape", "hover"] in result.commands


def test_scratch_action_override_wins(root, pieces):
    _, config, _ = pieces
    result = tr.run_scratch(_scratch_campaign(root), config,
                            root / "expA" / "bags_scratch", dry_run=True,
                            action="python3 fly.py --shape {trajectory} --hold 45")
    assert ["python3", "fly.py", "--shape", "hover", "--hold", "45"] in result.commands


def test_scratch_tally_says_scratch_and_counts_for_nothing(root, pieces, capsys):
    _, config, _ = pieces
    tr.run_scratch(_scratch_campaign(root), config, root / "expA" / "bags_scratch",
                   dry_run=False, hooks=FakeHooks().hooks(), label="fixcheck")
    out = capsys.readouterr().out
    assert "1 scratch recording saved to" in out
    assert "bags_scratch" in out
    assert "counts for nothing" in out


def test_scratch_leaves_no_slim_copy_by_default(root, pieces, monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "robofacts.slim_bag_archive",
                        type("m", (), {"archive_and_sync": staticmethod(
                            lambda bag, target, **kw: calls.append(bag))}))
    _, config, _ = pieces
    tr.run_scratch(_scratch_campaign(root), config, root / "expA" / "bags_scratch",
                   dry_run=False, hooks=FakeHooks().hooks())
    assert calls == [], "scratch flights default to no slim copy"
    tr.run_scratch(_scratch_campaign(root), config, root / "expA" / "bags_scratch",
                   dry_run=False, hooks=FakeHooks().hooks(), slim=True)
    assert len(calls) == 1, "but --slim opts back in"


def test_scratch_only_flags_are_refused_without_scratch(root):
    with pytest.raises(SystemExit):
        tr.main([str(root / "expA" / "campaigns" / "c.yaml"),
                 "--experiments-root", str(root), "--label", "x"])


def test_cli_scratch_dry_run_previews_and_touches_nothing(root, capsys):
    rc = tr.main([str(root / "expA" / "campaigns" / "c.yaml"),
                  "--experiments-root", str(root), "--scratch",
                  "--set", "execution_host=laptop"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SCRATCH FLIGHT" in out
    assert "fly.py --shape hover" in out
    assert not (root / "expA" / "bags_scratch").exists(), "a dry run creates nothing"


def test_start_background_quiet_sink_bypasses_stdout(tmp_path, capsys):
    """The real pump: with a sink, the child's lines reach the sink verbatim
    (tagged) and never the terminal."""
    import io
    import time as _time
    from robofacts import remote_exec_utils as reu
    sink = io.StringIO()
    proc = reu.start_background(["bash", "-c", "echo pumped-line"], None,
                                cwd=tmp_path, label="stack", quiet_sink=sink)
    proc.wait(timeout=10)
    for _ in range(50):                      # the pump thread drains async
        if "pumped-line" in sink.getvalue():
            break
        _time.sleep(0.1)
    assert "[stack] pumped-line" in sink.getvalue()
    assert "pumped-line" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# rosout.txt on the host that holds the bag (2026-10-14)
# --------------------------------------------------------------------------- #
def test_a_package_module_runs_through_the_shim_or_the_installed_package():
    cmd = tr.package_module_command(Path("/ws"), "export_rosout_text", ["/ws/bags/t 1"])
    assert cmd[:2] == ["sh", "-c"]
    assert "if [ -f /ws/experiments/common/scripts/export_rosout_text.py ]" in cmd[2]
    assert "python3 -m robofacts.export_rosout_text '/ws/bags/t 1'" in cmd[2]


def test_a_remote_trial_exports_rosout_on_the_rig(capsys):
    calls = []

    class Done:
        returncode = 0

    class Hooks:
        def run_foreground(self, cmd, target, cwd=None, check=False):
            calls.append((cmd, target, cwd))
            return Done()

    class Target:
        host = "rig"
        workspace = "/rig/ws"

    tr.export_rosout(Path("/rig/ws/experiments/e/bags/t"), Target(), Hooks(), None)
    (cmd, target, cwd), = calls
    assert "export_rosout_text" in cmd[2] and "/rig/ws/experiments/e/bags/t" in cmd[2]
    assert cwd == Path("/rig/ws")
    assert "written on the rig" in capsys.readouterr().out


def test_a_failed_export_never_raises(tmp_path, capsys):
    tr.export_rosout(tmp_path / "not_a_bag", None, None, tmp_path)
    assert "rosout export skipped" in capsys.readouterr().out

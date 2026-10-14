"""Live lifecycle test for the trial runner, with no robot (#96 Phase 4).

Uses ``demo_nodes_cpp talker`` as a stand-in "stack" and records ``/chatter``,
which exercises the part that pure unit tests cannot: real background processes,
a real rosbag2 recorder, ordered teardown, and a bag that actually finalizes.

The property that matters is **teardown order**. rosbag2 must be stopped and
allowed to finalize *before* the stack goes down; doing it the other way round
has produced truncated bags. A bag that reads back with messages is the only
proof that ordering is right.

Skipped automatically where ROS is unavailable (e.g. on the host); runs inside
the dev container.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from declarative_robot_trials import campaign_spec as cs
from declarative_robot_trials import trial_runner as tr
pytestmark = [
    pytest.mark.skipif(shutil.which("ros2") is None,
                       reason="needs ROS 2 (run inside the dev container)"),
    # Real talker and recorder: the settle and stop grace are what let them
    # discover each other and let the bag finalize.
    pytest.mark.real_sleep,
]

FACTORS = """
version: 1
factors:
  execution_host:
    levels: [laptop]
    default: laptop
    metadata_key: execution_host
    maps_to: {kind: host}
  rate:
    levels: [slow, fast]
    default: slow
    metadata_key: rate
    maps_to: {kind: launch_arg, name: rate}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  rate: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""


@pytest.fixture
def demo_root(tmp_path: Path) -> Path:
    """A synthetic experiment whose 'stack' is the talker demo."""
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(FACTORS, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "demo" / "config").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def demo_config(demo_root: Path) -> tr.RunnerConfig:
    """Hand-built so the launch/record/action commands are demo ones."""
    return tr.RunnerConfig(
        experiment="demo",
        launch_package="demo_nodes_cpp",
        launch_file="talker",           # overridden below; see launch_command patch
        action="",
        settle_s=2.0,
        stop_grace_s=3.0,
        hosts={"laptop": {"local": True}},
        project_root=Path.cwd(),
    )


@pytest.fixture(autouse=True)
def demo_commands(monkeypatch):
    """Point the runner at the talker demo and record /chatter.

    Only the three command builders are replaced — the lifecycle under test
    (backgrounding, ordering, teardown, finalization) is the real one.
    """
    monkeypatch.setattr(tr, "launch_command",
                        lambda trial, factors, config: ["ros2", "run", "demo_nodes_cpp", "talker"])
    monkeypatch.setattr(tr, "record_command",
                        lambda trial, factors, config, bag_dir: [
                            "ros2", "bag", "record", "-s", "mcap", "-o", str(bag_dir), "/chatter"])
    monkeypatch.setattr(tr, "action_command",
                        lambda trial, config, extra=None: ["sleep", "6"])


def _trial() -> cs.TrialSpec:
    return cs.TrialSpec("demo", {"execution_host": "laptop", "rate": "fast"}, ["rate"], 1)


def _bag_message_count(bag_dir: Path) -> int:
    import rosbag2_py
    meta = rosbag2_py.Info().read_metadata(str(bag_dir), "")
    return meta.message_count


# --------------------------------------------------------------------------- #
def test_full_lifecycle_produces_a_finalized_bag_with_messages(demo_root, demo_config):
    """The end-to-end proof: background launch -> record -> action -> ordered
    teardown, leaving a bag that reads back with real messages."""
    bags = demo_root / "demo" / "bags"
    factors = cs.load_factors("demo", demo_root)

    result = tr.run_trial(_trial(), factors, demo_config, bags,
                          dry_run=False, slim=False)

    assert result.status == "ran", result.detail
    bag_dir = result.bag_dir
    assert (bag_dir / "metadata.yaml").is_file(), "rosbag2 never finalized the bag"
    assert list(bag_dir.glob("*.mcap")), "no mcap split was written"

    count = _bag_message_count(bag_dir)
    assert count > 0, "the bag finalized but is empty — teardown order is wrong"


def test_the_trial_metadata_lands_next_to_the_bag(demo_root, demo_config):
    bags = demo_root / "demo" / "bags"
    factors = cs.load_factors("demo", demo_root)
    result = tr.run_trial(_trial(), factors, demo_config, bags, dry_run=False, slim=False)

    metadata = json.loads((result.bag_dir / "trial_metadata.json").read_text())
    assert metadata["rate"] == "fast", "factor values must be recorded to be countable"
    assert metadata["execution_host"] == "laptop"
    assert metadata["aborted"] is False


def test_no_processes_survive_the_trial(demo_root, demo_config):
    """An orphaned talker or recorder would poison the next trial — this is the
    class of failure that produced 'the camera is up but publishes nothing'."""
    import subprocess

    bags = demo_root / "demo" / "bags"
    factors = cs.load_factors("demo", demo_root)
    tr.run_trial(_trial(), factors, demo_config, bags, dry_run=False, slim=False)

    for pattern in ("demo_nodes_cpp", "bag record"):
        leftovers = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True)
        count = int((leftovers.stdout or "0").strip() or 0)
        assert count == 0, f"{count} leftover process(es) matching {pattern!r}"


def test_a_crashing_action_still_closes_the_bag(demo_root, demo_config, monkeypatch):
    """Teardown lives in a finally block, so a failing action must not leave an
    orphaned recorder holding an unfinalized file."""
    monkeypatch.setattr(tr, "action_command",
                        lambda trial, config, extra=None: ["false"])
    bags = demo_root / "demo" / "bags"
    factors = cs.load_factors("demo", demo_root)

    result = tr.run_trial(_trial(), factors, demo_config, bags, dry_run=False, slim=False)
    assert (result.bag_dir / "metadata.yaml").is_file(), "the bag was not finalized"
    assert "rc=1" in result.detail, "a failing action should be recorded, not hidden"


def test_continuous_session_reuses_one_stack_and_closes_it(demo_root, demo_config):
    """Continuous mode must launch ONE stack, record a separate bag per trial,
    and still leave nothing running — the time saving is worthless if it leaks
    a rig or truncates the second bag."""
    import subprocess

    bags = demo_root / "demo" / "bags"
    factors = cs.load_factors("demo", demo_root)
    session: dict = {}

    first = tr.run_trial(_trial(), factors, demo_config, bags, dry_run=False,
                         slim=False, session=session)
    launched = session.get("procs", {}).get("launch_stack")
    assert launched is not None, "the session did not adopt the stack"

    second = tr.run_trial(_trial(), factors, demo_config, bags, dry_run=False,
                          slim=False, session=session)
    assert session["procs"]["launch_stack"] is launched, "the stack was relaunched"

    for result in (first, second):
        assert result.status == "ran", result.detail
        assert (result.bag_dir / "metadata.yaml").is_file(), "a bag was not finalized"
    assert first.bag_dir != second.bag_dir, "both trials wrote the same bag"
    assert _bag_message_count(second.bag_dir) > 0, "the reused stack stopped publishing"

    tr.close_session(session)
    leftovers = subprocess.run(["pgrep", "-fc", "demo_nodes_cpp"], capture_output=True, text=True)
    assert int((leftovers.stdout or "0").strip() or 0) == 0, "the shared stack survived"



def test_the_trial_leaves_rosout_txt_beside_the_bag(demo_root, demo_config, monkeypatch):
    """experiments/AGENTS.md asks for rosout.txt beside every bag. Before 2026-10-14 only the
    run notebooks wrote it, and no runner-written bag had one."""
    monkeypatch.setattr(tr, "record_command",
                        lambda trial, factors, config, bag_dir: [
                            "ros2", "bag", "record", "-s", "mcap", "-o", str(bag_dir),
                            "/chatter", "/rosout"])
    bags = demo_root / "demo" / "bags"
    factors = cs.load_factors("demo", demo_root)
    result = tr.run_trial(_trial(), factors, demo_config, bags, dry_run=False, slim=False)

    assert result.status == "ran", result.detail
    text = (result.bag_dir / "rosout.txt").read_text(encoding="utf-8")
    assert "Publishing" in text, "the talker's log lines should be in rosout.txt"


def test_a_bag_without_rosout_still_finishes_the_trial(demo_root, demo_config):
    """The default demo records only /chatter: the export is skipped and the trial counts."""
    bags = demo_root / "demo" / "bags"
    factors = cs.load_factors("demo", demo_root)
    result = tr.run_trial(_trial(), factors, demo_config, bags, dry_run=False, slim=False)
    assert result.status == "ran", result.detail
    assert not (result.bag_dir / "rosout.txt").exists()
    assert (result.bag_dir / "trial_metadata.json").is_file()

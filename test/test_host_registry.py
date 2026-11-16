"""The host registry fills each experiment's host levels from common/config/hosts.yaml (2026-11-16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from robofacts import host_registry as hr
from robofacts import trial_runner as tr

REGISTRY = """\
version: 1
rig: robot
hosts:
  laptop: {local: true}
  robot:
    host: robot-lan
    container: ros2_robot_dev
    container_hostname: robot-dev
    shell: zsh
"""

RUNNER = """\
launch: {package: demo_nodes_cpp, file: talker_listener.launch.py}
hosts:
  laptop:
  robot:
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "hosts.yaml").write_text(REGISTRY, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER, encoding="utf-8")
    return tmp_path


def _runner(root: Path, text: str) -> tr.RunnerConfig:
    (root / "expA" / "config" / "runner.yaml").write_text(text, encoding="utf-8")
    return tr.RunnerConfig.load("expA", root)


def test_an_empty_level_takes_the_registry_spec(root, monkeypatch):
    monkeypatch.delenv("RIG_HOST", raising=False)
    config = tr.RunnerConfig.load("expA", root)
    assert config.hosts["robot"] == {"host": "robot-lan", "container": "ros2_robot_dev",
                                    "container_hostname": "robot-dev", "shell": "zsh"}
    assert config.remote_target("laptop") is None
    target = config.remote_target("robot")
    assert target.host == "robot-lan" and target.container == "ros2_robot_dev"
    assert target.shell == "zsh"


def test_an_experiment_overrides_only_the_keys_it_writes(root, monkeypatch):
    monkeypatch.delenv("RIG_HOST", raising=False)
    config = _runner(root, RUNNER.replace(
        "  robot:\n", "  robot:\n    env_setup: [/ws/loopback_dds.zsh]\n"))
    target = config.remote_target("robot")
    assert target.host == "robot-lan", "the alias still comes from the registry"
    assert target.env_setup == ("/ws/loopback_dds.zsh",)


def test_an_experiment_may_run_a_registry_remote_level_locally(root):
    config = _runner(root, RUNNER.replace("  robot:\n", "  robot: {local: true}\n"))
    assert config.remote_target("robot") is None


def test_levels_the_experiment_does_not_declare_are_not_added(root):
    config = _runner(root, RUNNER.replace("  robot:\n", ""))
    assert sorted(config.hosts) == ["laptop"]


def test_an_undeclared_level_is_refused_instead_of_running_locally(root):
    """Before 2026-11-16 an undeclared level ran on the runner's machine without a word."""
    config = _runner(root, RUNNER.replace("  robot:\n", ""))
    with pytest.raises(tr.RunnerError, match="not declared"):
        config.remote_target("robot")
    assert config.remote_target(None) is None, "no host factor still means this machine"


def test_rig_host_retargets_the_rig_level_only(root, monkeypatch):
    monkeypatch.setenv("RIG_HOST", "robot-vpn")
    config = tr.RunnerConfig.load("expA", root)
    assert config.remote_target("robot").host == "robot-vpn"
    assert config.remote_target("laptop") is None


def test_rig_host_does_not_turn_a_local_rig_level_remote(root, monkeypatch):
    monkeypatch.setenv("RIG_HOST", "somewhere")
    config = _runner(root, RUNNER.replace("  robot:\n", "  robot: {local: true}\n"))
    assert config.remote_target("robot") is None


def test_inside_the_rig_container_the_registry_spec_runs_locally(root, monkeypatch):
    monkeypatch.setattr(tr.socket, "gethostname", lambda: "robot-dev")
    assert tr.RunnerConfig.load("expA", root).remote_target("robot") is None


def test_without_a_registry_the_runner_yaml_spec_is_used_as_written(tmp_path):
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(
        RUNNER.replace("  robot:\n", "  robot: {host: my-robot, container: c, shell: zsh}\n"),
        encoding="utf-8")
    config = tr.RunnerConfig.load("expA", tmp_path)
    assert config.remote_target("robot").host == "my-robot"


def test_a_rig_level_the_registry_does_not_declare_is_rejected(tmp_path):
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "hosts.yaml").write_text(
        "rig: spot\nhosts: {laptop: {local: true}}\n", encoding="utf-8")
    with pytest.raises(hr.HostRegistryError, match="rig"):
        hr.load(tmp_path)


def test_a_malformed_level_names_the_file(root):
    with pytest.raises(tr.RunnerError, match="hosts.robot"):
        _runner(root, RUNNER.replace("  robot:\n", "  robot: robot-lan\n"))


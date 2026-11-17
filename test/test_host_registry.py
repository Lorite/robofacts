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



# --------------------------------------------------------------------------- #
# Where a robot's stop goes (robot_targets, estop.sh, robot_action.sh)
# --------------------------------------------------------------------------- #
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from robofacts import run_marker  # noqa: E402

STOP_REGISTRY = """\
rig: robot
hosts:
  here: {local: true}
  robot: {host: robot-lan, shell: zsh}
"""

ROBOTS = """\
robots:
  drone:
    host_level: robot
    verified: true
    estop: {description: cut, command: 'echo ESTOP-DRONE'}
  arm:
    host_level: robot
    verified: true
    estop: {description: cut, command: 'echo ESTOP-ARM'}
"""

TOOLS = Path(__file__).resolve().parents[1] / "robofacts" / "tools"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    cfg = tmp_path / "common" / "config"
    cfg.mkdir(parents=True)
    (cfg / "hosts.yaml").write_text(STOP_REGISTRY, encoding="utf-8")
    (cfg / "robots.yaml").write_text(ROBOTS, encoding="utf-8")
    return tmp_path


def _robots(project: Path) -> dict:
    import yaml
    return yaml.safe_load((project / "common/config/robots.yaml").read_text())["robots"]


def _targets(project: Path, **kw):
    return hr.robot_targets(project / "common/config/robots.yaml", _robots(project),
                            env=kw.pop("env", {}), **kw)


def test_a_host_level_resolves_through_the_registry(project):
    t = _targets(project)["drone"]
    assert (t.host, t.container, t.shell) == ("robot-lan", "", "zsh")
    assert "host_level robot" in t.source


def test_the_flag_wins_over_everything(project):
    marker = {"execution_host": "robot", "simulation": False, "robots": None}
    assert _targets(project, level="here", marker=marker)["drone"].host == "local"


def test_a_hardware_trial_moves_the_stop_of_its_robots_only(project):
    marker = {"execution_host": "here", "simulation": False, "robots": ["drone"], "label": "t1"}
    targets = _targets(project, marker=marker)
    assert targets["drone"].host == "local" and "running trial" in targets["drone"].source
    assert targets["arm"].host == "robot-lan", "a robot the trial does not use keeps its host"


def test_a_trial_that_declares_no_robots_moves_every_stop(project):
    marker = {"execution_host": "here", "simulation": False, "robots": None}
    assert {t.host for t in _targets(project, marker=marker).values()} == {"local"}


def test_a_simulation_never_moves_a_stop(project):
    """A simulation on the laptop must not pull the stop of a real robot away from the rig."""
    marker = {"execution_host": "here", "simulation": True, "robots": None}
    assert {t.host for t in _targets(project, marker=marker).values()} == {"robot-lan"}


def test_an_unknown_level_is_an_error_not_a_guess(project):
    with pytest.raises(hr.StopTargetError, match="nowhere"):
        _targets(project, level="nowhere")


def test_rig_host_replaces_the_rig_alias(project):
    t = _targets(project, env={"RIG_HOST": "robot-vpn"})["drone"]
    assert t.host == "robot-vpn" and "RIG_HOST" in t.source


def test_a_robot_without_host_level_keeps_its_own_fields(tmp_path):
    path = tmp_path / "robots.yaml"
    robots = {"a": {"host": "box", "container": "c"}}
    t = hr.robot_targets(path, robots, env={})["a"]
    assert (t.host, t.container) == ("box", "c")
    assert hr.robot_targets(path, robots, env={"RIG_HOST": "vpn"})["a"].host == "vpn"


def test_the_runner_notes_the_trial_in_the_marker(tmp_path):
    with run_marker.running(tmp_path, experiment="e"):
        run_marker.note(tmp_path, execution_host="here", robots=None, simulation=False)
        record = run_marker.read_live(tmp_path)
        assert record["execution_host"] == "here" and record["experiment"] == "e"
    assert run_marker.read(tmp_path) is None


def test_note_without_a_held_marker_writes_nothing(tmp_path):
    run_marker.note(tmp_path, execution_host="here")
    assert not run_marker.marker_path(tmp_path).exists()


def test_a_marker_of_a_dead_runner_on_this_machine_is_ignored(tmp_path):
    import socket
    dead = subprocess.Popen(["true"])
    dead.wait()
    run_marker.marker_path(tmp_path).write_text(json.dumps(
        {"host": socket.gethostname(), "pid": dead.pid, "execution_host": "here"}))
    assert run_marker.read(tmp_path) is not None
    assert run_marker.read_live(tmp_path) is None


def _estop(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("RIG_HOST", "RIG_CONTAINER")}
    return subprocess.run(["bash", str(TOOLS / "estop.sh"), "--config",
                           str(project / "common/config/robots.yaml"), *args],
                          capture_output=True, text=True, timeout=60, env=env)


def test_estop_dials_the_declared_level(project):
    out = _estop(project, "--dry-run").stdout
    assert "ssh" in out and "robot-lan" in out


def test_estop_host_level_flag_runs_here(project):
    result = _estop(project, "--dry-run", "--host-level", "here")
    assert result.returncode == 0, result.stderr
    assert "robot-lan" not in result.stdout and "echo ESTOP-DRONE" in result.stdout


def test_estop_follows_a_live_hardware_trial_and_says_so(project):
    run_marker.marker_path(project).write_text(json.dumps(
        {"host": "another-machine", "pid": 1, "execution_host": "here", "simulation": False,
         "robots": ["drone"], "label": "t1"}))
    out = _estop(project, "--list").stdout
    drone, arm = out.split("  arm ")[0], out.split("  arm ")[1]
    assert "host=local" in drone and "running trial" in drone
    assert "host=robot-lan" in arm


def test_estop_ignores_a_stale_marker(project):
    path = run_marker.marker_path(project)
    path.write_text(json.dumps({"host": "x", "pid": 1, "execution_host": "here",
                                "simulation": False, "robots": None}))
    old = time.time() - run_marker.STALE_AFTER_S - 60
    os.utime(path, (old, old))
    assert "host=robot-lan" in _estop(project, "--list").stdout


def test_estop_refuses_an_unknown_level(project):
    result = _estop(project, "--dry-run", "--host-level", "nowhere")
    assert result.returncode == 2 and "nowhere" in result.stderr


def test_robot_action_follows_the_flag(project):
    robots = project / "common/config/robots.yaml"
    robots.write_text(ROBOTS.replace("    estop: {description: cut, command: 'echo ESTOP-ARM'}\n",
                                     "    estop: {description: cut, command: 'echo ESTOP-ARM'}\n"
                                     "    actions: {sit: {command: 'echo SIT'}}\n"))
    env = {k: v for k, v in os.environ.items() if k != "RIG_HOST"}
    run = lambda *a: subprocess.run(["bash", str(TOOLS / "robot_action.sh"), "--config",
                                     str(robots), "--dry-run", *a],
                                    capture_output=True, text=True, timeout=60, env=env)
    assert "robot-lan" in run("arm", "sit").stdout
    assert "robot-lan" not in run("--host-level", "here", "arm", "sit").stdout


def test_the_runner_prints_where_the_stop_goes_for_a_hardware_trial(project, capsys,
                                                                   monkeypatch):
    monkeypatch.delenv("RIG_HOST", raising=False)
    with run_marker.running(project, experiment="e"):
        run_marker.note(project, execution_host="here", robots=["drone"], simulation=False)
        tr._print_stop_targets(project)
        run_marker.note(project, simulation=True)
        tr._print_stop_targets(project)
    out = capsys.readouterr().out
    assert out.count("estop.sh on this machine reaches") == 1
    assert "drone -> local" in out and "arm -> robot-lan" in out


# --------------------------------------------------------------------------- #
# A stop typed inside a container runs there only when it is the target's container
# --------------------------------------------------------------------------- #
import socket  # noqa: E402

CONTAINER_REGISTRY = """\
rig: robot
hosts:
  here: {local: true}
  robot: {host: robot-lan, container: robot_dev, container_hostname: HOSTNAME, shell: zsh}
"""


def test_runs_directly_only_in_the_targets_own_container():
    t = hr.StopTarget("drone", "robot-lan", "robot_dev", "zsh", "s", "robot-dev")
    assert t.runs_directly_here(in_container=True, hostname="robot-dev")
    assert not t.runs_directly_here(in_container=True, hostname="laptop-dev"), \
        "the laptop's container must not run the robot computer's stop"
    assert not t.runs_directly_here(in_container=False, hostname="robot-dev")
    legacy = hr.StopTarget("drone", "robot-lan", "robot_dev", "zsh", "s", "")
    assert legacy.runs_directly_here(in_container=True, hostname="anything"), \
        "a target without container_hostname keeps the old rule"


def _container_project(tmp_path: Path, chost: str) -> Path:
    cfg = tmp_path / "common" / "config"
    cfg.mkdir(parents=True)
    (cfg / "hosts.yaml").write_text(CONTAINER_REGISTRY.replace("HOSTNAME", chost))
    (cfg / "robots.yaml").write_text(ROBOTS)
    (tmp_path / "dockerenv").write_text("")
    return tmp_path


def _estop_in_container(project: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("RIG_HOST", "RIG_CONTAINER")}
    env["TRIALS_DOCKERENV"] = str(project / "dockerenv")
    return subprocess.run(["bash", str(TOOLS / "estop.sh"), "--config",
                           str(project / "common/config/robots.yaml"), *args],
                          capture_output=True, text=True, timeout=60, env=env).stdout


def test_estop_in_another_container_goes_over_ssh(tmp_path):
    project = _container_project(tmp_path, "some-other-container")
    out = _estop_in_container(project, "--dry-run", "--robot", "drone")
    assert "ssh" in out and "robot-lan" in out and "docker\\ exec\\ robot_dev" in out
    listing = _estop_in_container(project, "--list")
    assert "route:  ssh robot-lan, then docker exec robot_dev" in listing


def test_estop_in_the_targets_own_container_runs_directly(tmp_path):
    project = _container_project(tmp_path, socket.gethostname())
    out = _estop_in_container(project, "--dry-run", "--robot", "drone")
    assert "ssh" not in out and "[drone] echo ESTOP-DRONE" in out
    assert "route:  runs directly here, in this container" in _estop_in_container(project, "--list")


def test_robot_action_in_another_container_goes_over_ssh(tmp_path):
    project = _container_project(tmp_path, "some-other-container")
    robots = project / "common/config/robots.yaml"
    robots.write_text(ROBOTS + "    actions: {sit: {command: 'echo SIT'}}\n")
    env = {k: v for k, v in os.environ.items() if k != "RIG_HOST"}
    env["TRIALS_DOCKERENV"] = str(project / "dockerenv")
    run = lambda: subprocess.run(["bash", str(TOOLS / "robot_action.sh"), "--config", str(robots),
                                  "--dry-run", "arm", "sit"],
                                 capture_output=True, text=True, timeout=60, env=env).stdout
    assert "ssh" in run() and "robot-lan" in run()
    (project / "common/config/hosts.yaml").write_text(
        CONTAINER_REGISTRY.replace("HOSTNAME", socket.gethostname()))
    direct = run()
    assert "ssh" not in direct and "docker exec" not in direct and "zsh -lc" in direct

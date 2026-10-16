"""The stop and action scripts bound the command inside the container, not only the local client.

`timeout` around `docker exec` kills the local docker client, but the process that
`docker exec` started keeps running in the container. A hung stop could then hold DDS
state until the container restarts. These tests replace `docker` with a fake whose `exec`
detaches the command from the client (setsid), as the real daemon does, and check that the
command dies at the inner limit instead of finishing later.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "robofacts" / "tools"
ESTOP = TOOLS / "estop.sh"
ROBOT_ACTION = TOOLS / "robot_action.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
# `docker container inspect <name>` succeeds, so the scripts take the docker exec path.
if [ "$1" = container ]; then exit 0; fi
if [ "$1" = exec ]; then
    shift 2   # exec <container>
    # The daemon runs the command outside the client's process group, so killing the
    # client leaves it running. setsid reproduces that.
    setsid "$@" </dev/null >/dev/null 2>&1 &
    wait $!
    exit $?
fi
exit 1
"""


def fake_path(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("RIG_HOST", None)
    env.pop("RIG_CONTAINER", None)
    return env


def config_with(tmp_path: Path, marker: Path) -> Path:
    # The command outlives every limit in the test unless something kills it.
    command = f"sleep 5; touch {marker}"
    path = tmp_path / "robots.yaml"
    path.write_text(f"""
version: 1
robots:
  box:
    host: local
    container: fakebox
    shell: bash
    verified: true
    estop: {{description: "d", command: '{command}'}}
    actions:
      hang: {{description: "d", command: '{command}'}}
""", encoding="utf-8")
    return path


def assert_killed_inside(marker: Path, started: float) -> None:
    # Wait past the moment the unbounded command would have finished.
    time.sleep(max(0.0, started + 6.5 - time.monotonic()))
    assert not marker.exists(), "the command kept running in the container after the stop gave up"


@pytest.mark.skipif(
    os.path.exists("/.dockerenv"),
    reason="inside a container estop.sh runs the stop directly, without docker exec",
)
def test_estop_bounds_the_command_inside_the_container(tmp_path):
    marker = tmp_path / "finished"
    env = fake_path(tmp_path)
    config = config_with(tmp_path, marker)
    dry = subprocess.run(["bash", str(ESTOP), "--config", str(config), "--timeout", "3", "--dry-run"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert "docker exec fakebox timeout -k 2 1 bash -lc" in dry.stdout, dry.stdout
    started = time.monotonic()
    result = subprocess.run(["bash", str(ESTOP), "--config", str(config), "--timeout", "3"],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL  box (rc=124" in result.stdout, result.stdout
    assert_killed_inside(marker, started)


def test_robot_action_bounds_the_command_inside_the_container(tmp_path):
    marker = tmp_path / "finished"
    env = fake_path(tmp_path)
    config = config_with(tmp_path, marker)
    dry = subprocess.run(["bash", str(ROBOT_ACTION), "--config", str(config), "--timeout", "3",
                          "--dry-run", "box", "hang"],
                         capture_output=True, text=True, env=env, timeout=30)
    assert "docker exec fakebox timeout -k 2 1 bash -lc" in dry.stdout, dry.stdout
    started = time.monotonic()
    result = subprocess.run(["bash", str(ROBOT_ACTION), "--config", str(config), "--timeout", "3",
                             "box", "hang"],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode != 0, result.stdout + result.stderr
    assert_killed_inside(marker, started)


@pytest.mark.parametrize("script", [ESTOP, ROBOT_ACTION], ids=["estop", "robot_action"])
def test_a_timeout_that_is_not_a_number_is_refused(tmp_path, script):
    env = fake_path(tmp_path)
    config = config_with(tmp_path, tmp_path / "unused")
    args = ["bash", str(script), "--config", str(config), "--timeout", "soon", "--dry-run"]
    if script == ROBOT_ACTION:
        args += ["box", "hang"]
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 2 and "--timeout" in result.stderr

"""estop.sh runs every documented form of a stop command whole, and a stop it cannot use fails.

Review finding (2026-11-25). The reader of robots.yaml emitted TAB-separated lines. A stop
written as a plain string, the form that both skeleton robots.yaml files document, crashed the
reader, so the robots after it got no stop while the script printed "All robots confirmed" and
exited 0. A multi-line ``command: |`` ran only its first line, and its second line became a
robot of its own. These tests use echo-only commands, and fake ``docker`` and ``ssh`` commands
on PATH, so nothing outside the test's directory is touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ESTOP = Path(__file__).resolve().parents[1] / "robofacts" / "tools" / "estop.sh"

#: `docker container inspect` finds the container only when FAKE_DOCKER_HERE=1. `docker exec`
#: runs the command, as the daemon would.
FAKE_DOCKER = """#!/usr/bin/env bash
if [ "$1" = container ]; then [ "${FAKE_DOCKER_HERE:-0}" = 1 ]; exit $?; fi
if [ "$1" = exec ]; then shift 2; exec "$@"; fi
exit 1
"""

#: ssh hands its last argument to the remote login shell. A POSIX sh stands in for it, so the
#: quoting of a multi-line command must work in the plainest shell.
FAKE_SSH = """#!/usr/bin/env bash
for last; do :; done
exec sh -c "$last"
"""


def fake_env(tmp_path: Path, **extra: str) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, text in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH)):
        path = bin_dir / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    for name in ("RIG_HOST", "RIG_CONTAINER", "TRIALS_EXPERIMENTS_ROOT", "TRIALS_ROBOTS_CONFIG"):
        env.pop(name, None)
    # Not inside a container, wherever the test runs.
    env["TRIALS_DOCKERENV"] = str(tmp_path / "no-dockerenv")
    env.update(extra)
    return env


def estop(tmp_path: Path, config: str, *args: str, **env: str) -> subprocess.CompletedProcess:
    path = tmp_path / "robots.yaml"
    path.write_text(config, encoding="utf-8")
    return subprocess.run(["bash", str(ESTOP), "--config", str(path), *args], capture_output=True,
                          text=True, env=fake_env(tmp_path, **env), timeout=60)


def test_the_string_form_of_a_stop_runs_like_the_mapping_form(tmp_path):
    ran = tmp_path / "ran"
    result = estop(tmp_path, f"""
version: 1
robots:
  drone1:
    host: local
    estop: {{description: "mapping form", command: "echo drone1 >> {ran}"}}
  drone2:
    host: local
    estop: "echo drone2 >> {ran}"
""")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK    drone1" in result.stdout and "OK    drone2" in result.stdout
    # The robots stop in parallel, so the order of the lines is free.
    assert sorted(ran.read_text().split()) == ["drone1", "drone2"]


@pytest.mark.parametrize("robot, env", [
    ("host: local", {}),
    ("host: local\n    container: box\n    shell: bash", {"FAKE_DOCKER_HERE": "1"}),
    ("host: my-robot-host\n    container: box\n    shell: bash", {}),
], ids=["here", "docker-exec", "ssh-then-docker-exec"])
def test_a_multi_line_command_runs_whole_on_every_route(tmp_path, robot, env):
    ran = tmp_path / "ran"
    result = estop(tmp_path, f"""
version: 1
robots:
  drone1:
    {robot}
    estop:
      description: "two lines"
      command: |
        echo "line 1 it's" >> {ran}
        echo line2 >> {ran}
""", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ran.read_text().splitlines() == ["line 1 it's", "line2"]
    assert "FAIL" not in result.stdout and "All robots confirmed the estop." in result.stdout


@pytest.mark.parametrize("bad", [
    'estop: {description: "no command"}',
    "estop: [echo, stop]",
    "estop: 42",
    'estop: {description: "d", command: ""}',
], ids=["mapping-without-command", "list", "number", "empty-command"])
def test_a_stop_entry_that_cannot_be_used_is_a_failed_stop(tmp_path, bad):
    ran = tmp_path / "ran"
    config = f"""
version: 1
robots:
  broken:
    host: local
    {bad}
  good:
    host: local
    estop: "echo good >> {ran}"
"""
    result = estop(tmp_path, config)
    assert result.returncode == 1, result.stdout + result.stderr
    assert ran.read_text().split() == ["good"], "the robot after the broken one still gets its stop"
    assert "FAIL  broken (no stop sent: its estop entry cannot be used" in result.stdout
    assert "All robots confirmed" not in result.stdout
    for flag in ("--dry-run", "--list"):
        checked = estop(tmp_path, config, flag)
        assert checked.returncode == 1, f"{flag}: {checked.stdout + checked.stderr}"
        assert "broken" in checked.stdout and "cannot be used" in checked.stdout


def test_a_robot_entry_that_is_not_a_mapping_is_a_failed_stop(tmp_path):
    ran = tmp_path / "ran"
    result = estop(tmp_path, f"""
version: 1
robots:
  broken: "echo broken >> {ran}"
  good:
    host: local
    estop: "echo good >> {ran}"
""")
    assert result.returncode == 1, result.stdout + result.stderr
    assert ran.read_text().split() == ["good"]
    assert "FAIL  broken" in result.stdout and "All robots confirmed" not in result.stdout


def test_a_robot_without_a_stop_is_still_skipped(tmp_path):
    ran = tmp_path / "ran"
    result = estop(tmp_path, f"""
version: 1
robots:
  quiet:
    host: local
    soft_stop: {{description: "d", command: "echo soft >> {ran}"}}
  good:
    host: local
    estop: {{description: "d", command: "echo good >> {ran}"}}
""")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIP  quiet (no estop configured)" in result.stdout
    assert ran.read_text().split() == ["good"]

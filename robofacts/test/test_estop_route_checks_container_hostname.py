"""estop.sh sends a stop into a local container only when it is the robot's container.

Review finding (2026-11-25). The host_registry docstring and the skeleton robots.yaml files name
the robot's container like a common local dev container. With the registry of that docstring
(robot host my-robot-host, container_hostname robot-dev), ``estop.sh --list`` on a laptop that
runs a container of the same name printed ``docker exec <container> on this machine``, although
that container's hostname was the laptop's. The stop never reached the robot computer. These
tests replace ``docker`` with a fake that only answers ``inspect``, and run only --list and
--dry-run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ESTOP = Path(__file__).resolve().parents[1] / "robofacts" / "tools" / "estop.sh"

#: `docker container inspect [-f FORMAT] NAME` finds only dev_container, whose hostname is
#: $FAKE_HOSTNAME. Anything else fails, so no real container is ever reached.
FAKE_DOCKER = """#!/usr/bin/env bash
if [ "$1" = container ] && [ "$2" = inspect ]; then
    shift 2
    fmt=""
    if [ "$1" = -f ]; then fmt="$2"; shift 2; fi
    [ "$1" = dev_container ] || exit 1
    if [ -n "$fmt" ]; then echo "$FAKE_HOSTNAME"; else echo '[{}]'; fi
    exit 0
fi
exit 97
"""


def run(tmp_path: Path, hostname_line: str, here_hostname: str, *args: str,
        **extra: str) -> subprocess.CompletedProcess:
    (tmp_path / "hosts.yaml").write_text(f"""
version: 1
rig: robot
hosts:
  laptop: {{local: true}}
  robot:
    host: my-robot-host
    container: dev_container
    {hostname_line}
    shell: zsh
""", encoding="utf-8")
    (tmp_path / "robots.yaml").write_text("""
version: 1
robots:
  drone:
    host_level: robot
    estop: {description: "echo only", command: "echo estop drone"}
""", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    for name in ("RIG_HOST", "RIG_CONTAINER", "TRIALS_EXPERIMENTS_ROOT", "TRIALS_ROBOTS_CONFIG"):
        env.pop(name, None)
    env["TRIALS_DOCKERENV"] = str(tmp_path / "no-dockerenv")   # not inside a container
    env["FAKE_HOSTNAME"] = here_hostname
    env.update(extra)
    return subprocess.run(["bash", str(ESTOP), "--config", str(tmp_path / "robots.yaml"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


SSH = "ssh my-robot-host, then docker exec dev_container"
HERE = "docker exec dev_container on this machine"


def test_a_local_twin_of_the_robots_container_does_not_take_the_stop(tmp_path):
    """The reviewer's case: the container here has the robot's container name, not its hostname."""
    listed = run(tmp_path, "container_hostname: robot-dev", "laptop-dev", "--list")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert f"route:  {SSH}" in listed.stdout, listed.stdout
    dry = run(tmp_path, "container_hostname: robot-dev", "laptop-dev", "--dry-run")
    line = next(line for line in dry.stdout.splitlines() if line.startswith("  [drone]"))
    assert line.startswith("  [drone] ssh ") and " my-robot-host docker\\ exec\\ dev_container" in line, line


@pytest.mark.parametrize("hostname_line, here, extra, route", [
    # On the robot computer itself: the container here IS the robot's container.
    ("container_hostname: robot-dev", "robot-dev", {}, HERE),
    # No container_hostname declared: the container's name is all there is to go by.
    ("", "laptop-dev", {}, HERE),
    # RIG_CONTAINER names the container on purpose (a stack on the laptop, for a bench test).
    ("container_hostname: robot-dev", "laptop-dev", {"RIG_CONTAINER": "dev_container"}, HERE),
], ids=["on-the-robot-computer", "no-container-hostname", "rig-container"])
def test_the_robots_own_container_still_takes_the_stop_here(tmp_path, hostname_line, here, extra, route):
    listed = run(tmp_path, hostname_line, here, "--list", **extra)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert f"route:  {route}" in listed.stdout, listed.stdout
    dry = run(tmp_path, hostname_line, here, "--dry-run", **extra)
    assert "  [drone] docker exec dev_container timeout" in dry.stdout, dry.stdout

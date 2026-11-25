"""robot_action.sh runs every documented form of an action whole, on the route robots.yaml names.

Review finding A2-8 (2026-11-25). robot_action.sh still had the reader that round 1 replaced in
estop.sh (7099412, 6d8844d): one TAB-separated line read with ``IFS=$'\t' read``. A multi-line
action ran only its first line, and the container_hostname after it was lost, so inside any
container the action ran right there instead of in the robot's container. An action written as
a plain string crashed the reader. These tests replace ``docker`` and ``ssh`` with stubs that
only record their arguments, and the commands they hand over only append to a file of the test.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
from pathlib import Path

import pytest

ROBOT_ACTION = Path(__file__).resolve().parents[1] / "robofacts" / "tools" / "robot_action.sh"

#: Each call writes its arguments, NUL-separated, to a file of its own in $FAKE_CALLS.
RECORDER = """#!/usr/bin/env bash
n=$(ls "$FAKE_CALLS" | wc -l)
printf '%s\\0' "$(basename "$0")" "$@" > "$FAKE_CALLS/$(printf '%03d' "$n").call"
"""

TWO_LINES = 'echo "line 1 it\'s" >> {ran}\necho line2 >> {ran}'


def run(tmp_path: Path, robot: str, action: str = "go", *args: str, inside_container: bool = False,
        command: str = "") -> subprocess.CompletedProcess:
    """robot_action.sh <args> drone <action>, with `robot` as the robot's settings."""
    (tmp_path / "robots.yaml").write_text(f"version: 1\nrobots:\n  drone:\n    {robot}\n",
                                          encoding="utf-8")
    bin_dir, calls = tmp_path / "bin", tmp_path / "calls"
    bin_dir.mkdir(exist_ok=True)
    calls.mkdir(exist_ok=True)
    for name in ("docker", "ssh"):
        path = bin_dir / name
        path.write_text(RECORDER, encoding="utf-8")
        path.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    for name in ("RIG_HOST", "RIG_CONTAINER", "TRIALS_EXPERIMENTS_ROOT", "TRIALS_ROBOTS_CONFIG"):
        env.pop(name, None)
    dockerenv = tmp_path / "dockerenv"
    if inside_container:
        dockerenv.write_text("", encoding="utf-8")
    env["TRIALS_DOCKERENV"] = str(dockerenv)
    env["FAKE_CALLS"] = str(calls)
    return subprocess.run(["bash", str(ROBOT_ACTION), "--config", str(tmp_path / "robots.yaml"),
                           *args, "drone", action], capture_output=True, text=True, env=env,
                          timeout=60)


def recorded(tmp_path: Path) -> list:
    """The argv of each docker or ssh call, in order."""
    return [path.read_bytes().decode().split("\0")[:-1]
            for path in sorted((tmp_path / "calls").glob("*.call"))]


def multi_line(ran: Path) -> str:
    """A `command: |` block whose two lines append to `ran`."""
    return "".join(f"\n          {line}" for line in TWO_LINES.format(ran=ran).splitlines())


def test_the_string_form_of_an_action_runs_like_the_mapping_form(tmp_path):
    ran = tmp_path / "ran"
    robot = f'host: local\n    actions:\n      go: "echo string-form >> {ran}"\n      sit: {{description: "sit down", command: "echo sit"}}'
    result = run(tmp_path, robot)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ran.read_text().split() == ["string-form"]
    listed = run(tmp_path, robot, "go", "--list")
    assert listed.returncode == 0, listed.stderr
    assert "go" in listed.stdout and "sit down" in listed.stdout


def test_a_multi_line_action_runs_whole_here(tmp_path):
    ran = tmp_path / "ran"
    result = run(tmp_path, f"host: local\n    actions:\n      go:\n        command: |{multi_line(ran)}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ran.read_text().splitlines() == ["line 1 it's", "line2"]


def test_a_multi_line_action_reaches_the_container_whole(tmp_path):
    ran = tmp_path / "ran"
    result = run(tmp_path, "host: local\n    container: box\n    shell: bash\n"
                           f"    actions:\n      go:\n        command: |{multi_line(ran)}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded(tmp_path) == [["docker", "exec", "box", "timeout", "-k", "2", "88", "bash", "-lc",
                                   TWO_LINES.format(ran=ran)]]


def test_a_multi_line_action_reaches_the_robot_host_whole_and_quoted_for_a_posix_shell(tmp_path):
    ran = tmp_path / "ran"
    result = run(tmp_path, "host: my-robot-host\n    container: box\n    shell: bash\n"
                           f"    actions:\n      go:\n        command: |{multi_line(ran)}")
    assert result.returncode == 0, result.stdout + result.stderr
    (call,) = recorded(tmp_path)
    assert call[0] == "ssh" and call[-2] == "my-robot-host"
    # The remote login shell may be a plain POSIX sh: shlex reads the line the way it does.
    assert shlex.split(call[-1]) == ["docker", "exec", "box", "timeout", "-k", "2", "88", "bash", "-lc",
                                     TWO_LINES.format(ran=ran)]


def test_a_twin_of_the_robots_container_does_not_take_a_multi_line_action(tmp_path):
    """Inside a container whose hostname is not the robot's container_hostname (the laptop's
    dev container, where the web interface may run), the action goes over ssh to the robot."""
    ran = tmp_path / "ran"
    assert socket.gethostname() != "robot-dev"
    result = run(tmp_path, "host: my-robot-host\n    container: box\n    container_hostname: robot-dev\n"
                           f"    actions:\n      go:\n        command: |{multi_line(ran)}",
                 inside_container=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not ran.exists(), "the action ran in this container, not on the robot"
    (call,) = recorded(tmp_path)
    assert call[0] == "ssh" and call[-2] == "my-robot-host"
    assert shlex.split(call[-1])[:3] == ["docker", "exec", "box"]


@pytest.mark.parametrize("bad, why", [
    ('{description: "no command"}', "it has no command"),
    ("[echo, go]", "got list"),
    ("42", "got int"),
], ids=["mapping-without-command", "list", "number"])
def test_an_action_entry_that_cannot_be_used_is_refused_with_its_reason(tmp_path, bad, why):
    result = run(tmp_path, f"host: local\n    actions:\n      go: {bad}")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot be used" in result.stderr and why in result.stderr
    assert recorded(tmp_path) == []


def test_an_undeclared_action_is_still_refused(tmp_path):
    result = run(tmp_path, 'host: local\n    actions:\n      sit: "echo sit"', "fly")
    assert result.returncode == 2 and "has no action 'fly'; declared: sit" in result.stderr

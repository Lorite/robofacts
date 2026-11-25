"""A stop reaches the remote processes also when their ssh client has already exited.

Review finding (2026-11-25, third round). stop_remote_process asked os.getpgid for the local
ssh client's group before it sent the remote pkill. When the client had already exited and
been reaped (Explore polls its stack, and a lost Wi-Fi ends ssh), getpgid raised
ProcessLookupError, and the pkill that should have reached the launch still running in the
container never left. These tests replace ``ssh`` with a stub that logs its argv: the "stack"
connection ends at once, a pkill call only writes its line. Nothing leaves this machine.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from robofacts import remote_exec_utils as rex

#: Writes its argv to $STUB_LOG. A pkill call succeeds, any other call is the stack's
#: connection, which is lost after 0.1 s (exit 255, as ssh exits when the link dies).
FAKE_SSH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$*" in *pkill*) exit 0 ;; esac
sleep 0.1
exit 255
"""


def _target() -> rex.RemoteTarget:
    return rex.RemoteTarget(host="robot-host", container="robot_dev", workspace=Path("/ws"),
                            ros_setup="/opt/ros/setup.zsh", ws_setup="/ws/install/setup.zsh")


def _stub(tmp_path: Path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH, encoding="utf-8")
    ssh.chmod(0o755)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    return log


def _reaped_stack(tmp_path: Path) -> object:
    proc = rex.start_background(["ros2", "launch", "pkg", "stack.launch.py"], _target(),
                                cwd=tmp_path, label="stack")
    t0 = time.monotonic()
    while proc.poll() is None and time.monotonic() - t0 < 10:   # what Explore's loop does
        time.sleep(0.05)
    assert proc.returncode == 255, "the stub connection should have ended"
    return proc


def test_the_remote_pkill_leaves_although_the_client_was_reaped(tmp_path, monkeypatch, capsys):
    log = _stub(tmp_path, monkeypatch)
    proc = _reaped_stack(tmp_path)
    log.write_text("", encoding="utf-8")
    rex.stop_remote_process(proc, _target(), name="launch_stack", kind="launch",
                            timeouts=(1, 1, 1))
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if "pkill" in line]
    assert calls, "no pkill reached the remote launch"
    assert "-INT" in calls[0] and "stack\\.launch\\.py" in calls[0], calls[0]
    out = capsys.readouterr().out
    assert "cannot be watched from here" in out, out
    assert "stopped gracefully" not in out, "a stop that watched nothing must not say so"


def test_a_reaped_leader_does_not_hide_the_rest_of_its_local_group(tmp_path):
    """Locally, the group id is the leader's pid also after the leader was reaped, so members
    it left behind still get the stop."""
    child = tmp_path / "child.pid"
    proc = rex.start_background(
        ["bash", "-c", f"sleep 30 & echo $! > {child}; exit 0"], None, cwd=tmp_path, label="stack")
    t0 = time.monotonic()
    while proc.poll() is None and time.monotonic() - t0 < 10:
        time.sleep(0.05)
    t0 = time.monotonic()
    while not child.exists() and time.monotonic() - t0 < 10:
        time.sleep(0.05)
    pid = int(child.read_text())
    try:
        rex.stop_remote_process(proc, None, name="launch_stack", kind="launch", timeouts=(1, 1, 1))
        assert not rex._group_members(proc.pid), "a member of the stack's group survived the stop"
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

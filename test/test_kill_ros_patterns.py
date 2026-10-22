"""kill_ros.sh matches a node of any *_ws overlay workspace, not only ros2_ws (2026-10-14:
an orphaned OpenVINS run_subscribe_msckf from ~/ov_ws survived the clean slate)."""
import os
import shutil
import subprocess
import time

import pytest

from robofacts import tools

pytestmark = pytest.mark.skipif(shutil.which("ps") is None, reason="needs ps")


def _spawn(argv0, env=None):
    """A sleeping process whose command line starts with `argv0`, once it does.

    Popen returns after the fork, before bash has run `exec -a`, so a scan right away can
    see `bash -c ...` instead (2026-10-22: two failures on CI, green on the rerun). Wait
    for the new command line before the test scans for it.
    """
    proc = subprocess.Popen(["bash", "-c", f"exec -a {argv0} sleep 30"], env=env)
    cmdline = f"/proc/{proc.pid}/cmdline"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with open(cmdline, "rb") as f:
                if f.read().split(b"\0", 1)[0] == argv0.encode():
                    return proc
        except FileNotFoundError:
            break
        time.sleep(0.01)
    proc.kill()
    proc.wait()
    pytest.fail(f"the process never took the command line {argv0}")


@pytest.mark.parametrize("argv0", ["/home/vscode/ov_ws/install/ov_msckf/lib/ov_msckf/run_subscribe_msckf",
                                   "/workspaces/x/ros2_ws/install/pkg/lib/pkg/node"])
def test_dry_run_lists_a_node_of_an_overlay_workspace(argv0):
    # exec -a gives the sleeping process the command line a workspace node would have.
    proc = _spawn(argv0)
    try:
        out = subprocess.run(["bash", str(tools.path("kill_ros.sh")), "--dry-run", "--verbose"],
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        assert f"{proc.pid}  {argv0}" in out.stdout
        assert "nothing killed" in out.stdout
    finally:
        proc.kill()
        proc.wait()


def test_dry_run_leaves_an_unrelated_process_alone():
    proc = _spawn("/home/vscode/notes/install_guide")
    try:
        out = subprocess.run(["bash", str(tools.path("kill_ros.sh")), "--dry-run", "--verbose"],
                             capture_output=True, text=True, timeout=30)
        assert f"{proc.pid}  " not in out.stdout
    finally:
        proc.kill()
        proc.wait()


# --------------------------------------------------------------------------- #
# Only the caller's ROS_DOMAIN_ID (2026-10-15): sessions sharing a container each use
# their own domain, and the old kill-everything run stopped their trials.
# --------------------------------------------------------------------------- #
FAKE_NODE = "/opt/ros/humble/lib/fake_pkg/fake_node"


def _fake_node(domain):
    """A sleeping process with a ROS command line on `domain` (None: the variable unset)."""
    env = {k: v for k, v in os.environ.items() if k != "ROS_DOMAIN_ID"}
    if domain is not None:
        env["ROS_DOMAIN_ID"] = str(domain)
    return _spawn(FAKE_NODE, env=env)


def _kill_ros(caller_domain, *args):
    # No ros2 on PATH, so a real run does not start a daemon on the test's domain.
    env = {**os.environ, "ROS_DOMAIN_ID": str(caller_domain), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["bash", str(tools.path("kill_ros.sh")), "--verbose", *args],
                          capture_output=True, text=True, timeout=30, env=env)


def _sections(stdout):
    found, _, left_alone = stdout.partition("Left alone")
    return found, left_alone


@pytest.fixture
def procs():
    started = []
    yield started
    for proc in started:
        proc.kill()
        proc.wait()


def test_a_process_on_another_domain_is_listed_not_targeted(procs):
    procs.append(_fake_node(97))
    out = _kill_ros(98, "--dry-run")
    found, left_alone = _sections(out.stdout)
    assert out.returncode == 0, out.stderr
    assert f"{procs[0].pid}  " not in found
    assert f"{procs[0].pid}  [domain 97] {FAKE_NODE}" in left_alone
    assert "--all kills them too" in left_alone


def test_a_process_on_the_callers_domain_is_targeted(procs):
    procs.append(_fake_node(98))
    found, left_alone = _sections(_kill_ros(98, "--dry-run").stdout)
    assert "on ROS_DOMAIN_ID=98" in found
    assert f"{procs[0].pid}  {FAKE_NODE}" in found
    assert f"{procs[0].pid}  " not in left_alone


def test_an_unset_domain_is_domain_0(procs):
    procs.append(_fake_node(None))
    found, _ = _sections(_kill_ros(0, "--dry-run").stdout)
    assert f"{procs[0].pid}  {FAKE_NODE}" in found


def test_all_targets_every_domain(procs):
    procs.append(_fake_node(97))
    found, left_alone = _sections(_kill_ros(98, "--dry-run", "--all").stdout)
    assert f"{procs[0].pid}  [domain 97] {FAKE_NODE}" in found
    assert f"{procs[0].pid}  " not in left_alone


def test_a_real_run_kills_its_domain_and_spares_the_other(procs):
    procs.extend([_fake_node(97), _fake_node(98)])
    other, mine = procs
    out = _kill_ros(98, "--grace", "1")
    assert out.returncode == 0, out.stderr
    assert mine.wait(timeout=10) is not None, "the caller's domain was not killed"
    assert other.poll() is None, "a process on another domain was killed"

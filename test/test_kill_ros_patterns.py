"""kill_ros.sh matches a node of any *_ws overlay workspace, not only ros2_ws (2026-10-14:
an orphaned OpenVINS run_subscribe_msckf from ~/ov_ws survived the clean slate)."""
import shutil
import subprocess

import pytest

from declarative_robot_trials import tools

pytestmark = pytest.mark.skipif(shutil.which("ps") is None, reason="needs ps")


@pytest.mark.parametrize("argv0", ["/home/vscode/ov_ws/install/ov_msckf/lib/ov_msckf/run_subscribe_msckf",
                                   "/workspaces/x/ros2_ws/install/pkg/lib/pkg/node"])
def test_dry_run_lists_a_node_of_an_overlay_workspace(argv0):
    # exec -a gives the sleeping process the command line a workspace node would have.
    proc = subprocess.Popen(["bash", "-c", f"exec -a {argv0} sleep 30"])
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
    proc = subprocess.Popen(["bash", "-c", "exec -a /home/vscode/notes/install_guide sleep 30"])
    try:
        out = subprocess.run(["bash", str(tools.path("kill_ros.sh")), "--dry-run", "--verbose"],
                             capture_output=True, text=True, timeout=30)
        assert f"{proc.pid}  " not in out.stdout
    finally:
        proc.kill()
        proc.wait()

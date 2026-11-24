"""The ROS 2 backend: ``ros2 launch`` or ``ros2 run`` starts the stack, ``ros2 bag record``
records the trial, and a recording is a rosbag2 directory, called a bag."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Tuple

from robofacts.backend import Backend
from robofacts_ros2.launcher import Ros2Launcher
from robofacts_ros2.recorder import Ros2Recorder

#: Setup files of a host that names none: the distribution's, and the workspace's overlay.
DEFAULT_ROS_SETUP = "/opt/ros/humble/setup.zsh"
DEFAULT_WS_SETUP = "ros2_ws/install/setup.zsh"


def host_setup(spec: Dict[str, Any], workspace: Path) -> Tuple[str, str]:
    """``ros_setup`` and ``ws_setup`` of a host, with the defaults of a ROS 2 workspace."""
    return (str(spec.get("ros_setup", DEFAULT_ROS_SETUP)),
            str(spec.get("ws_setup", Path(workspace) / DEFAULT_WS_SETUP)))


DAEMON_STOP_TIMEOUT_S = 15.0
DAEMON_START_TIMEOUT_S = 30.0


def bounce_daemon() -> None:
    """Restart the ros2 daemon.

    A stale daemon reports an empty node/service list on a perfectly healthy
    stack - four false FAILs were observed this way on 2026-07-22 - so every
    gate run starts from a fresh one.
    """
    if shutil.which("ros2") is None:
        print("  (ros2 not on PATH - skipping daemon bounce)")
        return
    # A WEDGED daemon makes `ros2 daemon stop` block forever (2026-10-06: a
    # daemon caught by a SIGINT to a trial's process group hung the next T-0
    # gate until the gate's own 300 s timeout, whose FAIL named no check).
    # Bounded, and a wedged daemon of THIS domain is killed instead.
    try:
        subprocess.run(["ros2", "daemon", "stop"], capture_output=True, timeout=DAEMON_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        domain = os.environ.get("ROS_DOMAIN_ID", "0") or "0"
        print(f"  (ros2 daemon stop hung for {DAEMON_STOP_TIMEOUT_S:.0f} s - killing the wedged "
              f"daemon of domain {domain})")
        subprocess.run(["pkill", "-KILL", "-f",
                        f"[r]os2cli.daemon.daemonize.*--ros-domain-id {domain}( |$)"],
                       capture_output=True)
    try:
        subprocess.run(["ros2", "daemon", "start"], capture_output=True, timeout=DAEMON_START_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"  (ros2 daemon start did not return in {DAEMON_START_TIMEOUT_S:.0f} s - "
              f"checks will start their own discovery)")


_HERE = Path(__file__).resolve().parent

#: Read-only probes among the ROS 2 checks: they read sysctl, `gz topic -l`, the ros2
#: daemon's graph, or subscribe to a topic for a window (a subscription does not change
#: what another subscriber receives). None starts, stops or signals a process, and none
#: shares state with a sibling check.
PARALLEL_SAFE_CHECKS = frozenset({
    "camera_frames", "dds_socket_buffer", "estop_reachable", "gz_world_matches",
    "mocap_stream", "node_present", "ros_param_equals", "rosout_report",
    "topic_rate", "topic_value",
})

#: ROS 2 checks that must run alone, and why (read from each script's code).
SEQUENTIAL_CHECKS = {
    "ros_clean_slate": (
        "counts matching processes with pgrep and deliberately does not exclude its "
        "siblings (lib/common.sh pf_count_processes), so a concurrent check whose "
        "command line holds the pattern (a file_exists path that names the same binary) "
        "would count as a leftover"),
    "rigidbody_continuity": (
        "its entries share one capture file per topic (/tmp/pf_rbc_<uid>_<topic>.cap, "
        "2026-08-19): the first entry captures and its siblings reuse the frames, so two "
        "entries at once would truncate each other's capture"),
    "rigidbody_near": (
        "one `ros2 topic echo --once` on the same NamedPoseArray topic that "
        "rigidbody_continuity captures (2026-10-29): on the rig only the first echo on a "
        "topic reliably attaches within the window, so it must not race that capture"),
}

from robofacts_ros2.gate_sentences import GATE_SENTENCES  # noqa: E402

BACKEND = Backend(
    name="ros2", launcher=Ros2Launcher(), recorder=Ros2Recorder(),
    host_setup=host_setup, gate_prepare=bounce_daemon,
    checks_dir=_HERE / "checks", checks_lib=_HERE / "lib" / "common.sh",
    parallel_safe_checks=PARALLEL_SAFE_CHECKS, sequential_checks=SEQUENTIAL_CHECKS,
    gate_sentences=GATE_SENTENCES,
    adapters_package="robofacts_ros2.adapters",
    features={"explore": "robofacts_ros2.explore", "trial_media": "robofacts_ros2.trial_media",
              "slim": "robofacts_ros2.slim_bag_archive", "viewers": "robofacts_ros2.ui.app"},
    commands={
        "explore": ("robofacts_ros2.explore:main",
                    "bring up one condition's stack with a Foxglove view that may change "
                    "parameters (dry run without --execute, never coverage)"),
        "slim": ("robofacts_ros2.slim_bag_archive:main", "archive slim copies of recorded bags"),
        "profiles": ("robofacts_ros2.record_profiles:main",
                     "the record profiles in force (--list), or one profile's topics or record command"),
        "ui": ("robofacts_ros2.ui.cli:main", "the web interface"),
    },
    lint_files=(_HERE / "lint_ros2.py",),
    skeleton_dir=_HERE / "skeleton",
    tools_dir=_HERE / "tools",
    cli=("ros2", "run", "robofacts_ros2", "trials"),
    package="robofacts_ros2",
    build_workspaces=(".", "ros2_ws"), workspaces_setting="colcon_workspaces",
)

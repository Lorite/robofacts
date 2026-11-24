"""The ROS 2 backend: ``ros2 launch`` or ``ros2 run`` starts the stack, ``ros2 bag record``
records the trial, and a recording is a rosbag2 directory, called a bag."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from ...backend import Backend
from .launcher import Ros2Launcher
from .recorder import Ros2Recorder

#: Setup files of a host that names none: the distribution's, and the workspace's overlay.
DEFAULT_ROS_SETUP = "/opt/ros/humble/setup.zsh"
DEFAULT_WS_SETUP = "ros2_ws/install/setup.zsh"


def host_setup(spec: Dict[str, Any], workspace: Path) -> Tuple[str, str]:
    """``ros_setup`` and ``ws_setup`` of a host, with the defaults of a ROS 2 workspace."""
    return (str(spec.get("ros_setup", DEFAULT_ROS_SETUP)),
            str(spec.get("ws_setup", Path(workspace) / DEFAULT_WS_SETUP)))


def restart_daemon() -> None:
    """Before a gate: a fresh ros2 daemon, so no check reads a stale graph. The function
    itself still lives in the gate engine and moves here with the ROS 2 package."""
    from ...preflight.run_preflight import bounce_daemon
    bounce_daemon()


BACKEND = Backend(name="ros2", launcher=Ros2Launcher(), recorder=Ros2Recorder(),
                  host_setup=host_setup, gate_prepare=restart_daemon)

"""The ROS 2 launcher: ``ros2 launch <package> <file> name:=value ...`` or, for a
single-node stack, ``ros2 run <package> <executable>``."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from robofacts.backend import BackendError

LAUNCH_MODES = ("launch", "run")


def _alternation(names: Iterable[str]) -> str:
    return "|".join(re.escape(str(n)) for n in names)


class Ros2Launcher:
    """``runner.yaml`` declares ``launch: {package, file}`` and optionally
    ``launch_mode: launch | run``."""

    def parse(self, raw: Dict[str, Any], path: Path) -> Dict[str, Any]:
        launch = raw.get("launch") or {}
        for required in ("package", "file"):
            if not launch.get(required):
                raise BackendError(f"{path}: launch.{required} is required")
        mode = str(raw.get("launch_mode", "launch"))
        if mode not in LAUNCH_MODES:
            raise BackendError(f"{path}: launch_mode must be 'launch' or 'run', got {mode!r}")
        return {"package": str(launch["package"]), "file": str(launch["file"]), "mode": mode}

    def command(self, stack: Dict[str, Any], args: Dict[str, Any],
                values: Optional[Dict[str, Any]] = None) -> Optional[List[str]]:
        if stack.get("mode", "launch") == "run":
            # `ros2 run` takes no name:=value arguments; a single-node stack that
            # needs parameters should use a launch file instead of growing a
            # half-supported argument path here.
            return ["ros2", "run", stack["package"], stack["file"]]
        cmd = ["ros2", "launch", stack["package"], stack["file"]]
        for name, value in sorted(args.items()):
            # An EMPTY value is dropped, not passed as a bare `name:=`. `ros2 launch`
            # rejects that outright ("malformed launch argument ..., expected format
            # '<name>:=<value>'") and refuses to start the whole stack, so a single
            # empty factor value takes down the rig rather than the one node it
            # describes. There is no CLI spelling for "pass an empty string" either
            # — the shell strips the quotes in `name:=""` and launch sees the same
            # malformed token — so omitting the argument is the only thing an empty
            # value CAN mean, and it lands on the launch file's own default.
            #
            # Found 2026-08-14, lab day 5: `pose_source: mocap` sets
            # `observer_pose_topic_override: ""` (added with the Exp 3 Part A wiring
            # in 6e06c10db9 and first RUN today), which blocked the T-0 gate with
            # three failures that all read like dead hardware — no robot driver node,
            # no /mocap/drone_pose, no mocap frames — because the stack never
            # started at all. The declared default for that argument is likewise "",
            # so dropping it is behaviour-preserving.
            if value == "":
                continue
            cmd.append(f"{name}:={value}")
        return cmd

    def stop_patterns(self) -> Dict[str, str]:
        """The three kinds of ROS 2 processes under a workspace, with what the project
        adds in trials.yaml ``stop_patterns:`` (``recorder_scripts``, wrappers that exec
        ``ros2 bag record``, and ``owned_containers``, composable containers that the
        project's launches start).

        The ROS 2 daemon (/usr/bin/python3 -c "from ros2cli.daemon…") is intentionally
        NOT matched by any of these: never kill the daemon.
        """
        from robofacts.remote_exec_utils import _stop_settings
        settings = _stop_settings()
        scripts = _alternation(settings.get("recorder_scripts") or [])
        containers = _alternation(settings.get("owned_containers") or [])
        script_part = f"|{{workspace}}.*({scripts})" if scripts else ""
        container_part = f"|component_container.*({containers})" if containers else ""
        return {
            # Everything spawned by this workspace. Used by the full-stack stop.
            'all': 'ros2 (launch|bag record|run ).*{workspace}' + script_part + container_part,
            # Only the bag recorder (ros2 bag record + the bash wrappers that exec it).
            # Used when stopping a recorder so the long-running main launch
            # (ros2 launch ...) on the same host keeps running.
            'recorder': 'ros2 bag record.*{workspace}' + script_part,
            # Only ros2 launch / ros2 run processes + their composable containers
            # (no bag recorders).
            'launch': 'ros2 (launch|run ).*{workspace}' + container_part,
        }

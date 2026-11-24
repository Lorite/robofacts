#!/usr/bin/env python3
"""Temporal trimming utilities derived from /rosout log events.

This module provides functions to determine time-window bounds from crazyflie_server
flight events (takeoff, start_trajectory, land) published on the /rosout topic
(rcl_interfaces/msg/Log).

**Trim Modes**

``no_trim``
    All bag data is used.

``trim_take_off_to_land``
    Window ``[t_takeoff, t_land]`` — first ``function=takeoff`` to first
    ``function=land`` from crazyflie_server.

``trim_only_trajectory``
    Start bound: ``function=start_trajectory`` (or ``takeoff`` as fallback).
    End bound: ``function=land``.

``trim_command_activity``
    Start/end bounds are derived from the first and last non-zero samples in
    ``/{cf_name}/commands_received_position``. This is useful for removing
    zero-command pre-roll and post-roll from command-tracking analyses.

**Usage**

    >>> bounds = get_rosout_trim_bounds(Path('my_bag'), mode='trim_only_trajectory')
    >>> if bounds:
    ...     trimmed = apply_trim(mocap_samples, apriltag_samples, 'trim_only_trajectory', Path('my_bag'))
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message


# The two generic bag helpers live in the core. They are re-exported for the callers of this module.
from ...bag_io_utils import detect_storage_id, stamp_to_float  # noqa: E402,F401

__all__ = [
    "get_rosout_trim_bounds",
    "get_command_activity_bounds",
    "apply_trim",
]



def get_command_activity_bounds(commanded, epsilon: float = 1e-6) -> Optional[Tuple[float, float]]:
    """Return the first and last timestamp where the command is non-zero.

    The helper expects samples with ``.t``, ``.x``, ``.y``, and ``.z`` fields.
    It treats samples whose absolute position components are all within
    ``epsilon`` of zero as idle padding.
    """
    t_start: Optional[float] = None
    t_end: Optional[float] = None

    for sample in commanded:
        if max(abs(float(sample.x)), abs(float(sample.y)), abs(float(sample.z))) <= epsilon:
            continue
        t = float(sample.t)
        if t_start is None:
            t_start = t
        t_end = t

    if t_start is None or t_end is None:
        print("[cmd_trim] No non-zero command samples found in commands_received_position")
        return None

    print(f"[cmd_trim] first non-zero command at t={t_start:.3f}")
    print(f"[cmd_trim] last non-zero command at t={t_end:.3f}")
    return (t_start, t_end)


_VALID_TRIM_MODES = frozenset({"no_trim", "trim_take_off_to_land", "trim_only_trajectory"})


def get_rosout_trim_bounds(
    bag_dir: Path,
    mode: str,
) -> Optional[Tuple[float, float]]:
    """Determine trim time bounds from crazyflie_server flight events on /rosout.

    Scans ``/rosout`` (``rcl_interfaces/msg/Log``) for messages where
    ``name == "crazyflie_server"``.  The ``stamp`` field of each matching
    message provides the event timestamp.

    **Trim Modes**

    ``trim_take_off_to_land``
        ``[t_takeoff, t_land]`` — first ``function=takeoff`` to first
        ``function=land``.

    ``trim_only_trajectory``
        ``[t_start_trajectory, t_land]`` — first ``function=start_trajectory``
        to first ``function=land``.  Falls back to ``t_takeoff`` as start if
        ``start_trajectory`` is absent from the log.

    Args:
        bag_dir: Path to the bag directory.
        mode: One of ``"trim_take_off_to_land"`` or ``"trim_only_trajectory"``.

    Returns:
        Tuple ``(t_start, t_end)`` in seconds, or ``None`` if /rosout is absent
        from the bag or the required events are not found.
    """
    storage_id = detect_storage_id(bag_dir)
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id=storage_id),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )

    topic_types = reader.get_all_topics_and_types()
    rosout_type = next(
        (tt.type for tt in topic_types if tt.name == "/rosout"), None
    )
    if rosout_type is None:
        print("[rosout_trim] /rosout topic not found in bag")
        return None

    reader.set_filter(rosbag2_py.StorageFilter(topics=["/rosout"]))
    msg_class = get_message(rosout_type)

    t_takeoff: Optional[float] = None
    t_start_traj: Optional[float] = None
    t_land: Optional[float] = None

    while reader.has_next():
        _, data, _ = reader.read_next()
        msg = deserialize_message(data, msg_class)
        t = stamp_to_float(msg.stamp)
        text = getattr(msg, "msg", "") or ""

        # crazyflie_server emits takeoff / start_trajectory / land from functions of
        # those names (uploadTrajectory-based experiments, and take-off/land in general).
        if msg.name == "crazyflie_server":
            if msg.function == "takeoff" and t_takeoff is None:
                t_takeoff = t
                print(f"[rosout_trim] takeoff at t={t:.3f}")
            elif msg.function == "start_trajectory" and t_start_traj is None:
                t_start_traj = t
                print(f"[rosout_trim] start_trajectory at t={t:.3f}")
            elif msg.function == "land" and t_land is None:
                t_land = t
                print(f"[rosout_trim] land at t={t:.3f}")

        # goTo-based drivers do not call the firmware startTrajectory, so they emit
        # text markers 'start_trajectory: ...' / 'land: ...' from their OWN node. These
        # mark the precise path-start / land instants (after take-off + move-to-start),
        # giving a tight path-only window. Matched by message text, any node.
        if t_start_traj is None and text.startswith("start_trajectory"):
            t_start_traj = t
            print(f"[rosout_trim] start_trajectory (driver marker) at t={t:.3f}")
        if t_land is None and text.startswith("land:"):
            t_land = t
            print(f"[rosout_trim] land (driver marker) at t={t:.3f}")

    if t_land is None:
        print("[rosout_trim] 'land' event not found in /rosout")
        return None

    if mode == "trim_take_off_to_land":
        if t_takeoff is None:
            print("[rosout_trim] 'takeoff' event not found in /rosout")
            return None
        return (t_takeoff, t_land)

    # trim_only_trajectory
    if t_start_traj is not None:
        t_start = t_start_traj
    elif t_takeoff is not None:
        print("[rosout_trim] 'start_trajectory' not found — using 'takeoff' as trajectory start")
        t_start = t_takeoff
    else:
        print("[rosout_trim] Neither 'start_trajectory' nor 'takeoff' found in /rosout")
        return None
    return (t_start, t_land)


def apply_trim(
    mocap: List,
    apriltag: List,
    trim_mode: str,
    bag_dir: Path,
) -> Tuple[List, List, Dict]:
    """Filter sample lists to the time window defined by *trim_mode*.

    Bounds are derived from ``crazyflie_server`` flight events on the
    ``/rosout`` topic via :func:`get_rosout_trim_bounds`.  Falls back to
    ``no_trim`` with a warning if the required log events are absent.

    This function is generic with respect to sample type — it assumes samples
    have a ``.t`` attribute (timestamp in seconds).

    Args:
        mocap: List of ground-truth samples (with `.t` attribute).
        apriltag: List of estimate samples (with `.t` attribute).
        trim_mode: One of ``"no_trim"``, ``"trim_take_off_to_land"``, or
            ``"trim_only_trajectory"``.
        bag_dir: Path to the bag directory.

    Returns:
        Tuple of ``(trimmed_mocap, trimmed_apriltag, trim_info)`` where *trim_info*
        is a dict with keys: ``"trim_mode"``, ``"bounds_source"``, ``"t_start_sec"``,
        ``"t_end_sec"``, ``"window_duration_sec"``, ``"mocap_before"``,
        ``"mocap_after"``, ``"apriltag_before"``, ``"apriltag_after"``.
    """
    if trim_mode == "trim_command_activity":
        # This mode needs the commands_received_position samples, which
        # apply_trim does not receive — callers derive the bounds via
        # get_command_activity_bounds() and filter themselves (as both
        # analyze_commanded_vs_followed_bag.py copies do).
        print("[trim] trim_command_activity must be handled by the caller via "
              "get_command_activity_bounds() — falling back to no_trim")
        trim_mode = "no_trim"
    elif trim_mode not in _VALID_TRIM_MODES:
        print(f"[trim] Unknown trim_mode '{trim_mode}' — falling back to no_trim")
        trim_mode = "no_trim"

    if trim_mode == "no_trim":
        return mocap, apriltag, {"trim_mode": "no_trim"}

    bounds = get_rosout_trim_bounds(bag_dir, trim_mode)
    if bounds is None:
        print("[trim] rosout trim bounds unavailable — falling back to no_trim")
        return mocap, apriltag, {"trim_mode": "no_trim", "fallback": True}

    t_start, t_end = bounds

    def _filter(samples: List) -> List:
        return [s for s in samples if t_start <= s.t <= t_end]

    trimmed_mocap = _filter(mocap)
    trimmed_apriltag = _filter(apriltag)

    trim_info = {
        "trim_mode": trim_mode,
        "bounds_source": "rosout",
        "t_start_sec": t_start,
        "t_end_sec": t_end,
        "window_duration_sec": t_end - t_start,
        "mocap_before": len(mocap),
        "mocap_after": len(trimmed_mocap),
        "apriltag_before": len(apriltag),
        "apriltag_after": len(trimmed_apriltag),
    }
    print(
        f"[trim] mode={trim_mode} source=rosout "
        f"window=[{t_start:.3f}, {t_end:.3f}] ({t_end - t_start:.1f}s) "
        f"mocap: {len(mocap)}→{len(trimmed_mocap)}"
        f"  apriltag: {len(apriltag)}→{len(trimmed_apriltag)}"
    )
    return trimmed_mocap, trimmed_apriltag, trim_info

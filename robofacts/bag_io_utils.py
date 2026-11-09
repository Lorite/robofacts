#!/usr/bin/env python3
"""Generic ROS 2 bag I/O helpers for experiment analysis scripts.

Provides auto-detecting bag reading, PoseStamped loading, JSON I/O, and CSV
writing utilities shared across localization analysis scripts.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message

from .pose_error_analysis import PoseSample


__all__ = [
    "detect_storage_id",
    "stamp_to_float",
    "read_messages",
    "read_messages_with_time",
    "load_pose_samples",
    "load_camera_info_once",
    "save_csv",
    "read_json",
    "write_json",
    "write_csv_columns",
]


def detect_storage_id(bag_dir: Path) -> str:
    """Detect the storage plugin used by a bag directory.

    Reads ``metadata.yaml`` first; falls back to probing for ``.db3`` files.
    Returns ``"sqlite3"`` or ``"mcap"`` (default).
    """
    metadata_file = bag_dir / "metadata.yaml"
    if metadata_file.exists():
        for line in metadata_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("storage_identifier:"):
                return stripped.split(":", 1)[1].strip()
    if any(bag_dir.glob("*.db3")):
        return "sqlite3"
    return "mcap"


def stamp_to_float(stamp) -> float:
    """Convert a ROS builtin_interfaces/Time stamp to a float (seconds)."""
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9



def read_messages(input_bag: Path, topics=None):
    """Yield (topic, deserialized_msg) for every message in the bag.

    Auto-detects storage plugin (MCAP or SQLite3) via detect_storage_id and
    deserialises each CDR-encoded message to its Python type.

    ``topics`` (2026-09-11): an iterable of topic names, or a predicate on the
    name, restricting the read to those topics at the STORAGE level. Without it
    every message of every topic is deserialised, images included; the three
    verify steps the runner runs after each flight each paid a full pass over a
    65 to 80 MB bag that way (about 110 s of post-flight per trial).
    """
    for topic, msg, _t in read_messages_with_time(input_bag, topics=topics):
        yield topic, msg


def read_messages_with_time(input_bag: Path, topics=None):
    """Yield (topic, deserialized_msg, t_s) for every message in the bag.

    ``t_s`` is the bag RECEIVE time in seconds (float, Unix epoch): the
    recorder's clock, i.e. the one timeline every topic in the bag shares.
    That matters for cross-topic windows (verify_vision_in_the_loop scopes
    the bridge latch against the drone's IS_FLYING edges): header stamps are
    set on different machines (the crazyflie status is stamped on the drone,
    a bridge status String has no header at all), receive time is not.
    """
    storage_id = detect_storage_id(input_bag)
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(input_bag), storage_id=storage_id),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )

    topic_types = reader.get_all_topics_and_types()
    type_of = {tt.name: tt.type for tt in topic_types}
    if topics is not None:
        wanted = [t for t in type_of if topics(t)] if callable(topics) else \
                 [t for t in topics if t in type_of]
        if not wanted:
            return                      # none of the requested topics exist in this bag
        reader.set_filter(rosbag2_py.StorageFilter(topics=wanted))
    msg_types: dict = {}

    while reader.has_next():
        topic, data, t_ns = reader.read_next()
        msg_type = msg_types.get(topic)
        if msg_type is None:
            if topic not in type_of:
                raise ValueError(f"topic {topic} not found in bag")
            msg_type = msg_types[topic] = get_message(type_of[topic])
        msg = deserialize_message(data, msg_type)
        yield topic, msg, t_ns * 1e-9


def load_pose_samples(
    bag_dir: Path, topic: str, rigid_body: Optional[str] = None
) -> List[PoseSample]:
    """Read pose samples on *topic* from the bag, sorted by time.

    Handles two message shapes:

    * ``geometry_msgs/PoseStamped`` — a single ``pose`` + ``header`` (the default,
      e.g. an AprilTag or ``/mocap/drone_pose`` topic).
    * ``motion_capture_tracking_interfaces/NamedPoseArray`` — a ``poses`` list of
      named bodies + ``header`` (the MOCAP stream: ``/poses``, ``/poses_full``,
      ``/poses_full_gt``). When *rigid_body* is given, the matching named body is
      taken; otherwise the first body in each message is used.

    Messages of neither shape are silently skipped.
    """
    samples: List[PoseSample] = []
    # Filter at the storage level (2026-10-13): without it every message of every topic was
    # deserialized, images included, only to be skipped below, and a cohort run over heavy bags
    # spent minutes per bag. The result is identical, since only `topic` was ever used.
    for msg_topic, msg in read_messages(bag_dir, topics=[topic]):
        if msg_topic != topic:
            continue
        if not hasattr(msg, "header"):
            continue
        if hasattr(msg, "pose"):
            pose = msg.pose  # PoseStamped
        elif hasattr(msg, "poses"):
            pose = None  # NamedPoseArray: pick the requested (or first) named body
            for named in msg.poses:
                if rigid_body is None or getattr(named, "name", None) == rigid_body:
                    pose = named.pose
                    break
            if pose is None:
                continue
        else:
            continue
        o = pose.orientation
        samples.append(
            PoseSample(
                t=stamp_to_float(msg.header.stamp),
                x=float(pose.position.x),
                y=float(pose.position.y),
                z=float(pose.position.z),
                qx=float(o.x),
                qy=float(o.y),
                qz=float(o.z),
                qw=float(o.w),
            )
        )
    samples.sort(key=lambda s: s.t)
    return samples


def load_camera_info_once(bag_dir: Path, topic: str) -> Optional[Any]:
    """Return the first ``sensor_msgs/CameraInfo`` seen on *topic*, or ``None``.

    Camera intrinsics rarely change within a bag, so reading until the first
    message arrives is much cheaper than walking the full bag. Returns the
    raw message so callers can decide whether to wrap it (see
    ``camera_projection_utils.CameraIntrinsics.from_camera_info``).
    """
    for msg_topic, msg in read_messages(bag_dir):
        if msg_topic == topic:
            return msg
    return None


def save_csv(path: Path, rows: List[Dict]) -> None:
    """Write *rows* to a CSV file at *path*.

    Column order follows the key insertion order of the first row dict.
    An empty *rows* list produces a header-only file with a single ``t`` column.
    """
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not rows:
            csv.writer(handle).writerow(["t"])
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON file and return the parsed object, or None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write *payload* as pretty-printed, sorted-key JSON to *path*."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_csv_columns(path: Path, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    """Write *rows* to *path* using an explicit *columns* list.

    Only the specified columns are written; extra keys in each row dict are
    ignored (``extrasaction='ignore'``).  ``None`` values become empty strings.
    """
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: (row.get(col) if row.get(col) is not None else "") for col in columns})

"""One timestamped 6-DOF pose, the record the bag readers return (pure Python, no ROS)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PoseSample:
    """6-DOF pose sample with timestamp.

    Attributes:
        t:  Timestamp in seconds (float, from ROS header stamp).
        x, y, z:  Position in the world frame [m].
        qx, qy, qz, qw:  Orientation as a unit quaternion (Hamilton convention).
            Defaults to identity (0, 0, 0, 1) when orientation is unavailable.
    """

    t: float
    x: float
    y: float
    z: float
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    qw: float = 1.0

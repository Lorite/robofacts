#!/usr/bin/env python3
"""Pure-Python pose error analysis utilities for localization experiments.

This module provides reusable math functions for computing Absolute Trajectory Error (ATE)
and Relative Pose Error (RPE) metrics, with support for temporal interpolation and
orientation error decomposition into roll/pitch/yaw and geodesic angle.

**Methodology**

Absolute Trajectory Error (ATE)
    Per-pair 6-DOF pose errors:

    * Position  — signed per-axis errors ex/ey/ez, horizontal norm e_xy,
      3-D norm ‖e‖.  Metrics: RMSE, MAE, mean (bias), std, P95, max.
    * Orientation — error quaternion q_err = q_gt^{-1} ⊗ q_est, decomposed into
      signed roll/pitch/yaw errors and a scalar geodesic angle.
      Metrics: RMSE, MAE, mean (bias), std, P95, max per axis and geodesic.

Relative Pose Error (RPE)
    For each consecutive aligned pair the incremental motion in translation
    and rotation is compared between the estimate and the ground truth.
    Translation RPE is computed in the world frame; rotation RPE uses the
    standard geodesic formulation.  Captures local drift rather than global offset.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


__all__ = [
    "PoseSample",
    "rmse",
    "percentile",
    "mean_val",
    "std_val",
    "mae",
    "rolling_stats",
    "quat_to_rpy_deg",
    "orientation_error_deg",
    "interpolate_sample",
    "nearest_sample",
    "align_apriltag_to_mocap",
    "horn_se3_align_rows",
    "markley_so3_align_rows",
    "compute_rpe",
    "analyze_detection_coverage",
]


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


# ============================================================================
# Scalar Statistics
# ============================================================================


def rmse(values: Iterable[float]) -> float:
    """Root Mean Square Error of *values* (returns NaN for empty input)."""
    vals = list(values)
    if not vals:
        return float("nan")
    return math.sqrt(sum(v * v for v in vals) / len(vals))


def percentile(values: List[float], p: float) -> float:
    """Return the p-th percentile of *values* using nearest-rank method (NaN if empty)."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def mean_val(values: Iterable[float]) -> float:
    """Arithmetic mean of *values* (returns NaN for empty input)."""
    vals = list(values)
    if not vals:
        return float("nan")
    return sum(vals) / len(vals)


def std_val(values: Iterable[float]) -> float:
    """Sample standard deviation of *values* (Bessel-corrected; NaN for fewer than 2 items)."""
    vals = list(values)
    if len(vals) < 2:
        return float("nan")
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def mae(values: Iterable[float]) -> float:
    """Mean Absolute Error of *values* (returns NaN for empty input)."""
    vals = list(values)
    if not vals:
        return float("nan")
    return sum(abs(v) for v in vals) / len(vals)


def rolling_stats(
    values: List[float], window: int
) -> Tuple[List[float], List[float]]:
    """Rolling mean and sample standard deviation with a centered window.

    Edge samples use a clamped window (no padding).  Returns ``(means, stds)``
    lists of the same length as *values*.  Useful for smoothed time-series
    overlays in analysis plots.
    """
    n = len(values)
    half = max(1, window // 2)
    means: List[float] = []
    stds: List[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        chunk = values[lo:hi]
        m = sum(chunk) / len(chunk)
        means.append(m)
        stds.append(math.sqrt(sum((v - m) ** 2 for v in chunk) / max(len(chunk) - 1, 1)))
    return means, stds


# ============================================================================
# Quaternion Operations
# ============================================================================


def _quat_conj(qx: float, qy: float, qz: float, qw: float) -> Tuple[float, float, float, float]:
    """Return the conjugate (= inverse for unit quaternions) of q = (qx, qy, qz, qw)."""
    return (-qx, -qy, -qz, qw)


def _quat_mul(
    ax: float, ay: float, az: float, aw: float,
    bx: float, by: float, bz: float, bw: float,
) -> Tuple[float, float, float, float]:
    """Hamilton product a ⊗ b for two quaternions (ax, ay, az, aw) and (bx, by, bz, bw)."""
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _quat_to_rpy(qx: float, qy: float, qz: float, qw: float) -> Tuple[float, float, float]:
    """Return (roll, pitch, yaw) in radians (ZYX / aerospace convention)."""
    sinr_cosp = 2.0 * (qw * qx + qy * qz)
    cosr_cosp = 1.0 - 2.0 * (qx * qx + qy * qy)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    sinp = 2.0 * (qw * qy - qz * qx)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)

    siny_cosp = 2.0 * (qw * qz + qx * qy)
    cosy_cosp = 1.0 - 2.0 * (qy * qy + qz * qz)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return roll, pitch, yaw


def quat_to_rpy_deg(
    qx: float, qy: float, qz: float, qw: float
) -> Tuple[float, float, float]:
    """Return ``(roll_deg, pitch_deg, yaw_deg)`` from a unit quaternion (ZYX convention)."""
    roll, pitch, yaw = _quat_to_rpy(qx, qy, qz, qw)
    rad2deg = 180.0 / math.pi
    return roll * rad2deg, pitch * rad2deg, yaw * rad2deg


def orientation_error_deg(
    gt: PoseSample, est: PoseSample
) -> Tuple[float, float, float, float]:
    """
    Return (roll_err_deg, pitch_err_deg, yaw_err_deg, geodesic_angle_deg).

    The error quaternion is defined as q_err = q_gt^{-1} ⊗ q_est.
    RPY components of q_err represent the rotation needed to bring the
    estimated frame into the ground-truth frame.
    The geodesic angle is the minimal rotation magnitude.
    """
    cj = _quat_conj(gt.qx, gt.qy, gt.qz, gt.qw)
    qe = _quat_mul(*cj, est.qx, est.qy, est.qz, est.qw)
    n = math.sqrt(qe[0] ** 2 + qe[1] ** 2 + qe[2] ** 2 + qe[3] ** 2)
    if n > 1e-10:
        qe = tuple(v / n for v in qe)
    roll, pitch, yaw = _quat_to_rpy(*qe)
    geodesic = 2.0 * math.acos(min(1.0, abs(qe[3])))
    rad2deg = 180.0 / math.pi
    return roll * rad2deg, pitch * rad2deg, yaw * rad2deg, geodesic * rad2deg


# ============================================================================
# Temporal Interpolation
# ============================================================================


def _slerp(
    ax: float, ay: float, az: float, aw: float,
    bx: float, by: float, bz: float, bw: float,
    alpha: float,
) -> Tuple[float, float, float, float]:
    """Spherical linear interpolation between two unit quaternions."""
    dot = ax * bx + ay * by + az * bz + aw * bw
    if dot < 0.0:
        bx, by, bz, bw = -bx, -by, -bz, -bw
        dot = -dot
    dot = min(1.0, dot)
    if dot > 0.9995:
        # Nearly identical — normalized linear interpolation avoids division by ~0
        rx, ry, rz, rw = ax + alpha * (bx - ax), ay + alpha * (by - ay), az + alpha * (bz - az), aw + alpha * (bw - aw)
        n = math.sqrt(rx * rx + ry * ry + rz * rz + rw * rw)
        return rx / n, ry / n, rz / n, rw / n
    theta0 = math.acos(dot)
    theta = theta0 * alpha
    sin_theta0 = math.sin(theta0)
    s0 = math.cos(theta) - dot * math.sin(theta) / sin_theta0
    s1 = math.sin(theta) / sin_theta0
    return s0 * ax + s1 * bx, s0 * ay + s1 * by, s0 * az + s1 * bz, s0 * aw + s1 * bw


def interpolate_sample(
    samples: List[PoseSample],
    t: float,
    max_sync_gap_sec: float,
) -> Tuple[Optional[PoseSample], float]:
    """
    Return a linearly-interpolated PoseSample at time t (SLERP for rotation).

    If the bracketing interval exceeds max_sync_gap_sec the pair is rejected
    (returns None). sync_gap_sec is the distance to the nearest endpoint of
    the bracket — it bounds the interpolation error relative to nearest-neighbour.

    Args:
        samples: Sorted list of PoseSamples (assumed sorted by time).
        t: Target timestamp for interpolation.
        max_sync_gap_sec: Maximum acceptable bracketing interval (seconds).

    Returns:
        Tuple of (interpolated_sample, sync_gap_sec) where interpolated_sample
        is None if the bracketing interval exceeds max_sync_gap_sec.
    """
    if not samples:
        return None, float("inf")

    times = [s.t for s in samples]
    idx = bisect_left(times, t)

    # Extrapolation at boundaries — fall back to nearest endpoint
    if idx <= 0:
        gap = abs(samples[0].t - t)
        return (samples[0] if gap <= max_sync_gap_sec else None), gap
    if idx >= len(samples):
        gap = abs(samples[-1].t - t)
        return (samples[-1] if gap <= max_sync_gap_sec else None), gap

    s0, s1 = samples[idx - 1], samples[idx]
    bracket = s1.t - s0.t
    if bracket > max_sync_gap_sec:
        return None, bracket

    alpha = (t - s0.t) / bracket if bracket > 1e-12 else 0.0
    sync_gap = min(t - s0.t, s1.t - t)

    px = s0.x + alpha * (s1.x - s0.x)
    py = s0.y + alpha * (s1.y - s0.y)
    pz = s0.z + alpha * (s1.z - s0.z)
    qx, qy, qz, qw = _slerp(s0.qx, s0.qy, s0.qz, s0.qw, s1.qx, s1.qy, s1.qz, s1.qw, alpha)

    return PoseSample(t=t, x=px, y=py, z=pz, qx=qx, qy=qy, qz=qz, qw=qw), sync_gap


# ============================================================================
# Higher-Level Metrics
# ============================================================================


def align_apriltag_to_mocap(
    mocap_samples: List[PoseSample],
    apriltag_samples: List[PoseSample],
    max_sync_gap_sec: float,
) -> List[Dict[str, float]]:
    """Temporally align AprilTag estimates to MOCAP ground-truth samples.

    For every MOCAP sample the AprilTag stream is interpolated at the same
    timestamp using linear+SLERP interpolation.  Pairs whose bracketing
    interval exceeds *max_sync_gap_sec* are dropped.

    Returns a list of row dicts with keys:
    ``t``, ``mocap_{x,y,z}``, ``apriltag_{x,y,z}``, ``ex``, ``ey``, ``ez``,
    ``e_xy``, ``e_norm``, ``e_roll_deg``, ``e_pitch_deg``, ``e_yaw_deg``,
    ``e_angle_deg``, ``sync_gap_sec``, ``mocap_q{x,y,z,w}``,
    ``apriltag_q{x,y,z,w}``.
    """
    rows: List[Dict[str, float]] = []
    for gt in mocap_samples:
        est, sync_gap = interpolate_sample(apriltag_samples, gt.t, max_sync_gap_sec)
        if est is None:
            continue
        ex = est.x - gt.x
        ey = est.y - gt.y
        ez = est.z - gt.z
        e_xy = math.sqrt(ex * ex + ey * ey)
        e_norm = math.sqrt(ex * ex + ey * ey + ez * ez)
        e_roll_deg, e_pitch_deg, e_yaw_deg, e_angle_deg = orientation_error_deg(gt, est)
        rows.append(
            {
                "t": gt.t,
                "mocap_x": gt.x,
                "mocap_y": gt.y,
                "mocap_z": gt.z,
                "apriltag_x": est.x,
                "apriltag_y": est.y,
                "apriltag_z": est.z,
                "ex": ex,
                "ey": ey,
                "ez": ez,
                "e_xy": e_xy,
                "e_norm": e_norm,
                "e_roll_deg": e_roll_deg,
                "e_pitch_deg": e_pitch_deg,
                "e_yaw_deg": e_yaw_deg,
                "e_angle_deg": e_angle_deg,
                "sync_gap_sec": sync_gap,
                "mocap_qx": gt.qx,
                "mocap_qy": gt.qy,
                "mocap_qz": gt.qz,
                "mocap_qw": gt.qw,
                "apriltag_qx": est.qx,
                "apriltag_qy": est.qy,
                "apriltag_qz": est.qz,
                "apriltag_qw": est.qw,
            }
        )
    return rows


def horn_se3_align_rows(
    rows: List[Dict[str, float]],
) -> Tuple[List[List[float]], List[float], List[Dict[str, float]]]:
    """Solve the rigid SE(3) Horn/Umeyama alignment (no scale).

    Finds (R, t) that minimize ``Σ ‖R · p_apriltag + t − p_mocap‖²`` over all
    aligned rows, then applies the transform to the apriltag positions and
    rewrites the position-error columns.  Orientation columns are left untouched
    — Horn alignment optimizes position residuals only, so reusing R to rotate
    the estimated quaternions would conflate position-extrinsic and orientation-
    extrinsic calibration error.

    Returns ``(R_3x3, t_3, aligned_rows)`` where ``aligned_rows`` mirrors the
    input schema but with ``apriltag_{x,y,z}``, ``ex``, ``ey``, ``ez``,
    ``e_xy``, ``e_norm`` recomputed against the SE(3)-aligned trajectory.
    Empty input returns the identity transform and an empty list.
    """
    if not rows:
        return ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0], [])

    import numpy as np  # local import — keeps the rest of this module pure-Python

    src = np.array([[r["apriltag_x"], r["apriltag_y"], r["apriltag_z"]] for r in rows], dtype=float)
    dst = np.array([[r["mocap_x"], r["mocap_y"], r["mocap_z"]] for r in rows], dtype=float)
    src_c = src.mean(axis=0)
    dst_c = dst.mean(axis=0)
    H = (src - src_c).T @ (dst - dst_c)
    U, _S, Vt = np.linalg.svd(H)
    # Reflection guard: enforce det(R) = +1 by flipping the sign of the last
    # singular vector when U·Vᵀ is improper. Standard Umeyama trick.
    d = 1.0 if np.linalg.det(Vt.T @ U.T) > 0 else -1.0
    D = np.diag([1.0, 1.0, d])
    R = Vt.T @ D @ U.T
    t = dst_c - R @ src_c

    out: List[Dict[str, float]] = []
    for row, p in zip(rows, src):
        p_a = R @ p + t
        ex = float(p_a[0] - row["mocap_x"])
        ey = float(p_a[1] - row["mocap_y"])
        ez = float(p_a[2] - row["mocap_z"])
        new_row = dict(row)
        new_row["apriltag_x"] = float(p_a[0])
        new_row["apriltag_y"] = float(p_a[1])
        new_row["apriltag_z"] = float(p_a[2])
        new_row["ex"] = ex
        new_row["ey"] = ey
        new_row["ez"] = ez
        new_row["e_xy"] = math.sqrt(ex * ex + ey * ey)
        new_row["e_norm"] = math.sqrt(ex * ex + ey * ey + ez * ez)
        out.append(new_row)

    return (R.tolist(), t.tolist(), out)


def markley_so3_align_rows(
    rows: List[Dict[str, float]],
) -> Tuple[List[float], List[Dict[str, float]]]:
    """Solve a constant SO(3) offset that minimises orientation error.

    The orientation analog of Horn/Umeyama: find ``q_offset`` such that
    ``q_offset ⊗ q_apriltag`` is closest (in geodesic distance) to ``q_mocap``
    across all rows.  Solved by F. L. Markley's eigenvalue method: form
    ``M = Σ q_bias_i · q_bias_iᵀ`` where ``q_bias_i = q_mocap_i ⊗ q_apriltag_i⁻¹``,
    then take the eigenvector of ``M`` with the largest eigenvalue.  This is
    the proper average rotation (handles antipodal sign ambiguity correctly).

    The returned ``aligned_rows`` mirrors the input schema but with
    ``apriltag_q{x,y,z,w}`` replaced by ``q_offset ⊗ q_apriltag`` and the
    orientation-error columns recomputed.  Position columns are untouched —
    keep SO(3) and SE(3) alignment independent so each isolates a single
    extrinsic-calibration aspect.

    Returns ``(q_offset_xyzw, aligned_rows)``.  Empty input returns identity.
    """
    if not rows:
        return ([0.0, 0.0, 0.0, 1.0], [])

    import numpy as np

    # Build per-row bias quaternions q_bias = q_mocap ⊗ q_apriltag⁻¹
    M = np.zeros((4, 4))
    for r in rows:
        ax_, ay_, az_, aw_ = _quat_conj(r["apriltag_qx"], r["apriltag_qy"],
                                        r["apriltag_qz"], r["apriltag_qw"])
        bx, by, bz, bw = _quat_mul(
            r["mocap_qx"], r["mocap_qy"], r["mocap_qz"], r["mocap_qw"],
            ax_, ay_, az_, aw_,
        )
        q = np.array([bx, by, bz, bw], dtype=float)
        # Normalise — guards against floating-point drift in stored quaternions.
        n = float(np.linalg.norm(q))
        if n > 0.0:
            q /= n
        M += np.outer(q, q)

    # Largest-eigenvalue eigenvector of M is the mean rotation (Markley 2007).
    eigvals, eigvecs = np.linalg.eigh(M)
    q_offset = eigvecs[:, int(np.argmax(eigvals))]
    # Convention: w ≥ 0 for a unique representative (q and −q are the same rotation).
    if q_offset[3] < 0.0:
        q_offset = -q_offset
    qox, qoy, qoz, qow = (float(q_offset[0]), float(q_offset[1]),
                          float(q_offset[2]), float(q_offset[3]))

    out: List[Dict[str, float]] = []
    for r in rows:
        new_qx, new_qy, new_qz, new_qw = _quat_mul(
            qox, qoy, qoz, qow,
            r["apriltag_qx"], r["apriltag_qy"], r["apriltag_qz"], r["apriltag_qw"],
        )
        gt = PoseSample(
            t=r["t"], x=r["mocap_x"], y=r["mocap_y"], z=r["mocap_z"],
            qx=r["mocap_qx"], qy=r["mocap_qy"], qz=r["mocap_qz"], qw=r["mocap_qw"],
        )
        est = PoseSample(
            t=r["t"], x=r["apriltag_x"], y=r["apriltag_y"], z=r["apriltag_z"],
            qx=new_qx, qy=new_qy, qz=new_qz, qw=new_qw,
        )
        e_roll, e_pitch, e_yaw, e_angle = orientation_error_deg(gt, est)
        new_row = dict(r)
        new_row["apriltag_qx"] = new_qx
        new_row["apriltag_qy"] = new_qy
        new_row["apriltag_qz"] = new_qz
        new_row["apriltag_qw"] = new_qw
        new_row["e_roll_deg"] = e_roll
        new_row["e_pitch_deg"] = e_pitch
        new_row["e_yaw_deg"] = e_yaw
        new_row["e_angle_deg"] = e_angle
        out.append(new_row)

    return ([qox, qoy, qoz, qow], out)


def compute_rpe(rows: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Relative Pose Error (RPE) over consecutive aligned pairs.

    Expects rows produced by align_apriltag_to_mocap — each row must contain
    ``mocap_{x,y,z,qx,qy,qz,qw}`` and ``apriltag_{x,y,z,qx,qy,qz,qw}`` keys.

    Translation RPE: world-frame ``||Δp_est - Δp_gt||``.
    Rotation RPE: geodesic angle of ``q_err = q_gt_rel^{-1} ⊗ q_est_rel``.
    """
    rpe: List[Dict[str, float]] = []
    for i in range(len(rows) - 1):
        r0, r1 = rows[i], rows[i + 1]
        dt = r1["t"] - r0["t"]
        if dt <= 0.0:
            continue

        dp_gt_x = r1["mocap_x"] - r0["mocap_x"]
        dp_gt_y = r1["mocap_y"] - r0["mocap_y"]
        dp_gt_z = r1["mocap_z"] - r0["mocap_z"]
        dp_est_x = r1["apriltag_x"] - r0["apriltag_x"]
        dp_est_y = r1["apriltag_y"] - r0["apriltag_y"]
        dp_est_z = r1["apriltag_z"] - r0["apriltag_z"]
        rpe_trans = math.sqrt(
            (dp_est_x - dp_gt_x) ** 2
            + (dp_est_y - dp_gt_y) ** 2
            + (dp_est_z - dp_gt_z) ** 2
        )

        gt_rel = _quat_mul(
            *_quat_conj(r0["mocap_qx"], r0["mocap_qy"], r0["mocap_qz"], r0["mocap_qw"]),
            r1["mocap_qx"], r1["mocap_qy"], r1["mocap_qz"], r1["mocap_qw"],
        )
        est_rel = _quat_mul(
            *_quat_conj(r0["apriltag_qx"], r0["apriltag_qy"], r0["apriltag_qz"], r0["apriltag_qw"]),
            r1["apriltag_qx"], r1["apriltag_qy"], r1["apriltag_qz"], r1["apriltag_qw"],
        )
        q_err = _quat_mul(*_quat_conj(*gt_rel), *est_rel)
        n = math.sqrt(q_err[0] ** 2 + q_err[1] ** 2 + q_err[2] ** 2 + q_err[3] ** 2)
        if n > 1e-10:
            q_err = tuple(v / n for v in q_err)
        rpe_rot = 2.0 * math.acos(min(1.0, abs(q_err[3]))) * 180.0 / math.pi

        rpe.append({"t": r0["t"], "dt": dt, "rpe_trans_m": rpe_trans, "rpe_rot_deg": rpe_rot})
    return rpe


def analyze_detection_coverage(apriltag_samples: List[PoseSample]) -> Dict[str, float]:
    """Characterise temporal coverage of AprilTag detections via inter-detection gap analysis.

    Returns a dict of coverage statistics including effective update rate, gap
    statistics, and counts of gaps exceeding 0.5 s, 1.0 s, and 2.0 s.
    Returns an empty dict when *apriltag_samples* is empty.
    """
    if not apriltag_samples:
        return {}

    gaps = [
        apriltag_samples[i + 1].t - apriltag_samples[i].t
        for i in range(len(apriltag_samples) - 1)
    ]

    result: Dict[str, float] = {
        "num_detections": float(len(apriltag_samples)),
        "total_span_sec": apriltag_samples[-1].t - apriltag_samples[0].t,
    }

    if not gaps:
        result.update({
            "effective_rate_hz": float("nan"),
            "mean_gap_sec": float("nan"),
            "std_gap_sec": float("nan"),
            "median_gap_sec": float("nan"),
            "p95_gap_sec": float("nan"),
            "max_gap_sec": float("nan"),
            "gaps_gt_0_5s": 0.0,
            "gaps_gt_1_0s": 0.0,
            "gaps_gt_2_0s": 0.0,
        })
        return result

    mean_gap = sum(gaps) / len(gaps)
    result.update({
        "effective_rate_hz": 1.0 / mean_gap if mean_gap > 0 else float("nan"),
        "mean_gap_sec": mean_gap,
        "std_gap_sec": std_val(gaps),
        "median_gap_sec": percentile(gaps, 50.0),
        "p95_gap_sec": percentile(gaps, 95.0),
        "max_gap_sec": max(gaps),
        "gaps_gt_0_5s": float(sum(1 for g in gaps if g > 0.5)),
        "gaps_gt_1_0s": float(sum(1 for g in gaps if g > 1.0)),
        "gaps_gt_2_0s": float(sum(1 for g in gaps if g > 2.0)),
    })
    return result


def nearest_sample(
    samples: List[PoseSample],
    t: float,
    max_sync_gap_sec: float,
) -> Optional[PoseSample]:
    """Return the sample in *samples* whose timestamp is closest to *t*.

    Returns ``None`` if the nearest sample is farther than *max_sync_gap_sec*.
    Kept for reference; the primary alignment path uses :func:`interpolate_sample`.

    Args:
        samples: Sorted list of PoseSamples (assumed sorted by time).
        t: Target timestamp.
        max_sync_gap_sec: Maximum acceptable distance (seconds).

    Returns:
        Nearest sample if within max_sync_gap_sec, otherwise None.
    """
    if not samples:
        return None

    times = [s.t for s in samples]
    idx = bisect_left(times, t)
    if idx <= 0:
        nearest = samples[0]
    elif idx >= len(samples):
        nearest = samples[-1]
    else:
        a = samples[idx - 1]
        b = samples[idx]
        nearest = a if abs(a.t - t) <= abs(b.t - t) else b
    if abs(nearest.t - t) > max_sync_gap_sec:
        return None
    return nearest

#!/usr/bin/env python3
"""Unit tests for the #94 record profiles and the bag-slimming topic selection.

Pure Python — no ROS graph, no bag, no hardware. These pin the two properties
that make the "light bag instead of a forensics video" loop work:

  * the light profile carries every #94 debug channel and NO image/depth topic
    (that ratio is the whole 6-10 GB -> tens-of-MB difference);
  * ``position_only`` is bit-identical to the topic list the run notebook used
    before the profiles existed, so old trials stay comparable;
  * ``slim_bag``'s selection drops exactly the heavy topics of an existing bag
    and keeps everything else, including the compressed overlay.

Run directly (inside the container, with the workspace sourced):
  python3 test_record_profiles.py
or under pytest:
  pytest test_record_profiles.py
"""

from __future__ import annotations

import sys
from pathlib import Path


from declarative_robot_trials.record_profiles import (  # noqa: E402
    PROFILES,
    PROFILE_NOTES,
    build_record_command,
    build_topics,
    is_heavy_topic,
)
from declarative_robot_trials.slim_bag import select_topics  # noqa: E402
from declarative_robot_trials import record_profiles  # noqa: E402

CF = "cf1"

# The exact list the notebook's build_record_command() hard-coded before #94.
LEGACY_POSITION_ONLY = [
    "/poses", "/poses_full", "/poses_full_gt", "/poses_degraded",
    f"/{CF}/pose", f"/{CF}/odom",
    f"/{CF}/cmd_full_state",
    f"/{CF}/commands_received_position",
    f"/{CF}/ext_pose_age",
    f"/{CF}/localization_cov", f"/{CF}/altitude_debug",
    f"/{CF}/motors", f"/{CF}/battery", f"/{CF}/status",
    "/tf", "/tf_static", "/rosout",
]


def test_position_only_profile_is_unchanged():
    assert build_topics("position_only", cf_name=CF) == LEGACY_POSITION_ONLY


def test_light_profile_has_the_debug_channels_and_no_pixels():
    topics = build_topics("markerless_light", cf_name=CF)
    for required in ("/estimator/position",
                     "/estimator/status",
                     "/estimator/debug/markers",
                     "/estimator/debug/metrics",
                     "/estimator/debug/overlay/compressed",
                     "/estimator_bridge/status",
                     "/estimator_bridge/debug/metrics",
                     "/localization_watchdog/health",
                     "/tf", "/tf_static", "/poses", "/poses_full"):
        assert required in topics, f"light profile is missing {required}"
    heavy = [t for t in topics if is_heavy_topic(t)]
    assert heavy == [], f"the light profile must carry no pixels, got {heavy}"


def test_light_profile_is_a_superset_of_position_only():
    light = build_topics("markerless_light", cf_name=CF)
    assert all(t in light for t in LEGACY_POSITION_ONLY)


def test_full_profile_adds_the_camera_streams():
    full = build_topics("markerless_full", cf_name=CF)
    light = build_topics("markerless_light", cf_name=CF)
    added = [t for t in full if t not in light]
    assert added == ["/camera/color/image_raw",
                     "/camera/aligned_depth_to_color/image_raw",
                     "/camera/aligned_depth_to_color/camera_info"]


def test_replay_profile_adds_only_the_compressed_streams():
    # The 08-03 replay-grade tier: compressed pixels + intrinsics, no raw.
    replay = build_topics("markerless_replay", cf_name=CF)
    light = build_topics("markerless_light", cf_name=CF)
    added = [t for t in replay if t not in light]
    assert added == [
        "/camera/color/image_raw/compressed",
        "/camera/aligned_depth_to_color/image_raw/compressedDepth",
        "/camera/aligned_depth_to_color/camera_info",
    ]
    assert "/camera/color/image_raw" not in replay
    assert "/camera/aligned_depth_to_color/image_raw" not in replay


def test_replay_profile_is_a_superset_of_light():
    replay = build_topics("markerless_replay", cf_name=CF)
    light = build_topics("markerless_light", cf_name=CF)
    assert all(t in replay for t in light)


def test_replay_streams_stay_heavy_for_slimming():
    # slim_bag must still be able to strip a replay bag down to a light one —
    # the compressed pixel streams are heavy on purpose, only the debug
    # overlay and camera_info are exempt.
    assert is_heavy_topic("/camera/color/image_raw/compressed")
    assert is_heavy_topic(
        "/camera/aligned_depth_to_color/image_raw/compressedDepth")
    assert not is_heavy_topic("/camera/aligned_depth_to_color/camera_info")


def test_camera_info_is_light_but_image_raw_is_not():
    # camera_info is a handful of floats and is needed to re-project the estimate.
    assert not is_heavy_topic("/camera/color/camera_info")
    assert is_heavy_topic("/camera/color/image_raw")
    assert is_heavy_topic("/camera/aligned_depth_to_color/image_raw")
    assert is_heavy_topic("/nvblox_node/static_esdf_pointcloud")
    assert not is_heavy_topic("/estimator/debug/overlay/compressed")


def test_profiles_have_no_duplicate_topics():
    for name in PROFILES:
        topics = build_topics(name, cf_name=CF)
        assert len(topics) == len(set(topics)), f"{name} has duplicates"


def test_unknown_profile_raises():
    try:
        build_topics("nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("an unknown profile must raise")


def test_record_command_shape():
    cmd = build_record_command("markerless_light", "/tmp/bag_x", cf_name=CF,
                               max_duration_sec=30, compression=True)
    # Humble's ros2bag has no --max-duration: the duration is a timeout -s INT
    # wrapper in front of the argv (2026-08-10), so the bag closes as on Ctrl-C.
    assert cmd[:5] == ["timeout", "--preserve-status", "-s", "INT", "30"]
    assert cmd[5:11] == ["ros2", "bag", "record", "-s", "mcap", "-o"]
    assert "/tmp/bag_x" in cmd
    assert "--max-duration" not in cmd
    assert cmd[cmd.index("--compression-format") + 1] == "zstd"
    assert cmd[-1].startswith("/")


def test_record_command_no_duration_has_no_wrapper():
    cmd = build_record_command("markerless_light", "/tmp/bag_x", cf_name=CF)
    assert cmd[:6] == ["ros2", "bag", "record", "-s", "mcap", "-o"]
    assert "timeout" not in cmd


def test_custom_names_propagate():
    topics = build_topics("markerless_light", cf_name="drone9",
                          node="/mk", bridge="/br", watchdog="/wd", camera="/cam")
    assert "/drone9/battery" in topics
    assert "/mk/debug/metrics" in topics
    assert "/br/status" in topics and "/br/debug/metrics" in topics
    assert "/wd/health" in topics
    assert "/cam/color/camera_info" in topics


# --- slim_bag topic selection ------------------------------------------------

BAG_TOPICS = [
    "/tf", "/tf_static", "/rosout", "/poses", "/poses_full",
    "/camera/color/image_raw",
    "/camera/aligned_depth_to_color/image_raw",
    "/camera/color/camera_info",
    "/estimator/position",
    "/estimator/debug/overlay/compressed",
]


def test_slim_drops_only_the_heavy_topics():
    kept = select_topics(BAG_TOPICS, [], [])
    assert "/camera/color/image_raw" not in kept
    assert "/camera/aligned_depth_to_color/image_raw" not in kept
    assert "/camera/color/camera_info" in kept
    assert "/estimator/debug/overlay/compressed" in kept
    assert len(kept) == len(BAG_TOPICS) - 2


def test_slim_extra_excludes_and_keeps():
    kept = select_topics(BAG_TOPICS, ["rosout"], [])
    assert "/rosout" not in kept
    kept = select_topics(BAG_TOPICS, [], ["/camera/color/image_raw"])
    assert "/camera/color/image_raw" in kept, "--keep must override the heavy rule"


# --------------------------------------------------------------------------- #
# The route-demo profile has to satisfy the verifier that reads its bags
# --------------------------------------------------------------------------- #
#: Every topic verify_route_demo.py reads. A profile that flies this campaign
#: and omits one of them produces a bag whose verifier silently reports nothing
#: about it, which is indistinguishable from "nothing happened".
ROUTE_DEMO_TOPICS_THE_VERIFIER_READS = (
    "/poses",
    "/mocap/poses",
    "/estimator/status",
    "/estimator/position",
    "/estimator_bridge/status",
    "/localization_watchdog/health",
    "/route_demo/loop_closure_cm",
    "/graph_nav_map_tf/status",
)


def test_route_profile_records_everything_the_verifier_reads():
    """Close the loop between what is recorded and what is checked afterwards."""
    topics = set(build_topics("spot_route_replay", cf_name=CF))
    missing = [t for t in ROUTE_DEMO_TOPICS_THE_VERIFIER_READS if t not in topics]
    assert not missing, f"spot_route_replay omits topics verify_route_demo reads: {missing}"


def test_route_profile_records_the_localization_watchdog():
    """REGRESSION (2026-08-05): the only Spot campaign that ARMS had no watchdog.

    spot_route_replay composes markerless_debug_handcam, which drops the
    watchdog health channel because "the shadow campaigns fly nothing" -- true
    of those campaigns, false of this one. On this route the 0.3 s localization
    watchdog is what lands the drone when the hand camera loses it, so "did it
    fire, and on which leg" is the first question a mid-route landing raises.
    """
    assert "/localization_watchdog/health" in build_topics(
        "spot_route_replay", cf_name=CF)


def test_the_handcam_shadow_profile_still_has_no_watchdog():
    """The fix belongs to the route group, not to the shared handcam one."""
    assert "/localization_watchdog/health" not in build_topics(
        "spot_observer_handcam", cf_name=CF)


def test_route_profile_carries_replay_grade_pixels():
    """Outside the volume the camera is the only witness there is."""
    # Looked up in the test, not at import: the project settings (and with them the robot's
    # name) are the conftest fixture's example project only while a test runs.
    SPOT = record_profiles.SPOT_NAME
    topics = build_topics("spot_route_replay", cf_name=CF)
    assert f"/{SPOT}/camera/hand/compressed" in topics
    assert f"/{SPOT}/depth_registered/hand/image" in topics
    assert f"/{SPOT}/camera/hand/camera_info" in topics


def test_spot_hand_pixels_are_heavy_but_their_intrinsics_are_not():
    """Both halves of the 2026-08-05 slimming fix, pinned.

    A slim copy must drop Spot's JPEG colour and its REGISTERED depth (neither
    matched the old patterns, so slim copies were the same order of size as the
    heavy bag and got pulled over the lab Wi-Fi) and must keep camera_info,
    without which a recorded estimate cannot be re-projected.
    """
    SPOT = record_profiles.SPOT_NAME
    assert is_heavy_topic(f"/{SPOT}/camera/hand/compressed")
    assert is_heavy_topic(f"/{SPOT}/depth_registered/hand/image")
    assert not is_heavy_topic(f"/{SPOT}/camera/hand/camera_info")
    assert not is_heavy_topic(f"/{SPOT}/depth_registered/hand/camera_info")


def test_the_debug_overlay_survives_the_compressed_rule():
    """"/compressed" is a heavy pattern, and the 2 Hz overlay is the exception."""
    assert not is_heavy_topic("/estimator/debug/overlay/compressed")
    assert is_heavy_topic("/camera/color/image_raw/compressed")


def test_route_profile_is_a_superset_of_the_handcam_observer_profile():
    base = set(build_topics("spot_observer_handcam", cf_name=CF))
    route = set(build_topics("spot_route_replay", cf_name=CF))
    assert base <= route


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())


# --- Livox Mid-360S profiles (#109) -------------------------------------------

def test_livox_mapping_has_the_raw_stream_and_fast_lio_outputs():
    topics = build_topics("livox_mapping")
    for t in ("/livox/lidar", "/livox/imu", "/Odometry", "/cloud_registered", "/tf",
              "/tf_static"):
        assert t in topics, t
    # the accumulated map is redundant with the registered scans, and multi-GB
    assert "/Laser_map" not in topics
    # no camera or mocap in the desk profile
    assert not any("/camera" in t or "/mocap" in t for t in topics)


def test_livox_mapping_spot_adds_spot_state_and_mocap():
    desk = build_topics("livox_mapping")
    spot = build_topics("livox_mapping_spot")
    assert set(desk) <= set(spot)
    for t in ("/spot/odometry", "/spot/joint_states",
              "/mocap/poses"):
        assert t in spot, t


def test_livox_point_stream_is_heavy_but_the_imu_is_not():
    assert is_heavy_topic("/livox/lidar")
    assert is_heavy_topic("/cloud_registered")
    assert not is_heavy_topic("/livox/imu")
    assert not is_heavy_topic("/Odometry")


# --- AI deck + mocap profile (Paper 3, #107) -----------------------------------

def test_aideck_mocap_records_both_stream_formats_and_the_raw_mocap():
    topics = build_topics("aideck_mocap", cf_name=CF)
    for t in (f"/{CF}/aideck/image_raw", f"/{CF}/aideck/image_raw/compressed",
              f"/{CF}/aideck/camera_info", "/mocap/poses", "/tf", "/tf_static", "/rosout"):
        assert t in topics, t
    # no crazyflie_server in a handheld trial: no firmware telemetry, no /poses bridge
    assert "/poses" not in topics
    assert f"/{CF}/pose" not in topics


def test_aideck_mocap_follows_the_drone_name():
    topics = build_topics("aideck_mocap", cf_name="other_cf")
    assert "/other_cf/aideck/image_raw" in topics
    assert not any(CF in t for t in topics)


def test_aideck_frames_are_heavy_and_its_intrinsics_are_not():
    # slim_bag must strip the frames (a slim copy cannot host a relocalization run) and
    # keep camera_info, the capture-time calibration provenance.
    assert is_heavy_topic(f"/{CF}/aideck/image_raw")
    assert is_heavy_topic(f"/{CF}/aideck/image_raw/compressed")
    assert not is_heavy_topic(f"/{CF}/aideck/camera_info")
    assert not is_heavy_topic("/mocap/poses")


# --- AI deck VIO in shadow (Paper 3, #122) ---------------------------------------

def test_vio_shadow_is_aideck_mocap_plus_the_imu_log_and_openvins():
    base = build_topics("aideck_mocap", cf_name=CF)
    topics = build_topics("aideck_vio_shadow", cf_name=CF)
    assert topics[:len(base)] == base
    assert topics[len(base):] == [f"/{CF}/imu_log", "/ov_msckf/odomimu", "/ov_msckf/poseimu"]


def test_vio_shadow_keeps_the_relayed_imu_and_rosout():
    topics = build_topics("aideck_vio_shadow", cf_name=CF)
    # the relay output that OpenVINS reads, next to the raw log topic it came from
    assert f"/{CF}/imu" in topics and f"/{CF}/imu_log" in topics
    assert "/rosout" in topics  # L11: the relay's rate and gap reports live there


def test_vio_shadow_follows_the_drone_name():
    topics = build_topics("aideck_vio_shadow", cf_name="other_cf")
    assert "/other_cf/imu_log" in topics
    assert not any(CF in t for t in topics)


def test_vio_shadow_estimates_and_imu_are_light():
    # slim_bag keeps the shadow estimate and both IMU topics, it strips only the frames
    for t in (f"/{CF}/imu_log", f"/{CF}/imu", "/ov_msckf/odomimu", "/ov_msckf/poseimu"):
        assert not is_heavy_topic(t), t
    assert is_heavy_topic(f"/{CF}/aideck/image_raw")


def test_vio_shadow_record_command():
    cmd = build_record_command("aideck_vio_shadow", "bags/x", cf_name=CF)
    assert cmd[:6] == ["ros2", "bag", "record", "-s", "mcap", "-o"]
    assert cmd[-3:] == [f"/{CF}/imu_log", "/ov_msckf/odomimu", "/ov_msckf/poseimu"]
    assert "aideck_vio_shadow" in PROFILE_NOTES

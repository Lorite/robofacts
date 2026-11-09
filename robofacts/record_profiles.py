#!/usr/bin/env python3
"""Named `ros2 bag record` topic profiles (#94) — light debug bags vs full bags.

WHY: a full markerless flight bag is 6-10 GB because it carries the raw colour +
aligned-depth streams, so every debugging question means copying gigabytes off
the rig and re-rendering an offline forensics video. Since #94 the perception
node publishes its own state (TF, markers, diagnostics, a rate-limited compressed
overlay), so a bag that records ONLY those channels answers the same questions,
lands in the TENS OF MB, transfers in seconds and replays straight into
``experiments/common/config/rviz/markerless_debug.rviz``.

Profiles (``PROFILES``):

  ``position_only``    the #77/#80 position-only flight set (mocap + firmware
                       telemetry). Unchanged default of the run notebook.
  ``markerless_light`` position_only + the markerless estimator/bridge/debug
                       channels, and deliberately NO image or depth stream.
                       THE #94 PROFILE — record this for markerless flights.
  ``markerless_replay`` markerless_light + COMPRESSED colour (JPEG) +
                       compressedDepth (lossless PNG for 16UC1) + camera_info:
                       the replay-grade middle tier (2026-08-03 directive).
                       Needs the rig's replay republishers (on by default in
                       the camera launch) — see the PROFILES comment.
  ``markerless_full``  markerless_light + raw colour/depth (+ camera_info): the
                       full-fidelity capture, for building datasets or when a
                       question genuinely needs the pixels. Multi-GB.
  ``spot_route_replay`` the replay-grade tier for the SPOT rig (hand camera, not
                       the d435) plus the operator's loop-closure annotation:
                       the beyond-the-volume route demo, where the camera is the
                       only witness there is. See _spot_replay_streams.
  ``aideck_mocap``     the Crazyflie AI deck camera (raw frames, the JPEG
                       sub-topic when the GAP8 streams JPEG, camera_info) plus
                       the raw Motive stream: the Paper 3 handheld
                       relocalization bags (#107). See _aideck_mocap.
  ``aideck_vio_shadow`` aideck_mocap + crazyswarm2's raw IMU log topic and the
                       OpenVINS outputs: the Paper 3 drone VIO in shadow (#122),
                       next to the flight's own recorder. See _vio_shadow.
  ``shadow_flight``    position_only + aideck_mocap + vio_shadow + the raw
                       flow, range and barometer blocks: a Paper 3 flight on
                       mocap with every drone-side estimator in shadow (#158,
                       #164). See _shadow_sensors.
  ``teleop_shadow_flight`` shadow_flight + ``/joy``: the same flight flown by a
                       pilot on a gamepad (#168). See _teleop_pad.
  ``spot_observer_handcam_vio`` spot_observer_handcam + aideck_mocap +
                       vio_shadow: Spot's markerless estimate and the drone's AI
                       deck VIO in one bag, with mocap as truth (#170). See the
                       PROFILES comment.
  ``spot_observer_handcam_teleop`` spot_observer_handcam_replay + ``/joy``: the
                       hand camera's replay-grade pixels of a drone the pilot
                       flies on a gamepad, as detector training data (#170).
                       See the PROFILES comment.

Every profile is a plain list of topic names, exactly like the hand-written
lists this repo already uses (``build_record_command()`` in the run notebook,
``TOPICS=( ... )`` in the record shell scripts) — ``ros2 bag record`` simply does
not subscribe topics that are absent, so a profile can list optional topics.

Use from the run notebook (``experiments/common/scripts`` is already on sys.path):

    from .record_profiles import build_topics
    topics = build_topics(RECORD_PROFILE, cf_name=CF_NAME)

or standalone on the rig:

    python3 experiments/common/scripts/record_profiles.py markerless_light \\
        --cf-name cf1 --output bags/$(date +%FT%H-%M-%S)_markerless
    # add --run to execute the printed `ros2 bag record` instead of printing it
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Dict, List

from . import roots

#: Neutral defaults of the names the profiles are built from: the drone (``cf_name``),
#: the quadruped's namespace (``spot_name``), the camera namespace and the estimator,
#: bridge and watchdog node namespaces. A project sets its own under
#: ``record_profiles:`` in trials.yaml (the lab's names live there, not here).
DEFAULT_NAMES: Dict[str, str] = {
    "cf_name": "cf1", "spot_name": "spot", "camera": "/camera",
    "markerless_node": "/estimator", "extpos_bridge": "/estimator_bridge",
    "watchdog": "/localization_watchdog",
}


def names() -> Dict[str, str]:
    """The names in force: the package defaults under the project's ``record_profiles``."""
    merged = dict(DEFAULT_NAMES)
    project = roots.setting("record_profiles") or {}
    if not isinstance(project, dict):
        raise ValueError(f"trials.yaml: record_profiles must be a mapping, got {project!r}")
    merged.update({str(k): str(v) for k, v in project.items()})
    return merged


def _pick(value, key: str) -> str:
    return names()[key] if value is None else value


def default_cf_name() -> str:
    """The drone the profiles name their topics after, unless runner.yaml says ``cf_name:``."""
    return names()["cf_name"]


def __getattr__(name):
    """The old module constants (MARKERLESS_NODE, EXTPOS_BRIDGE, WATCHDOG, CAMERA, SPOT_NAME),
    resolved from the project's names on access."""
    keys = {"MARKERLESS_NODE": "markerless_node", "EXTPOS_BRIDGE": "extpos_bridge",
            "WATCHDOG": "watchdog", "CAMERA": "camera", "SPOT_NAME": "spot_name"}
    if name in keys:
        return names()[keys[name]]
    raise AttributeError(name)

#: Topics whose payload is an image/point cloud — the reason full bags are huge.
#: Also used by ``slim_bag.py`` as the default exclusion set.
HEAVY_TOPIC_PATTERNS = (
    "image_raw", "image_rect", "image_color", "/depth/", "aligned_depth",
    "/points", "point_cloud", "pointcloud", "/rgb/", "compressedDepth", "/infra",
    # A JPEG stream is still a pixel stream: Spot's hand camera publishes only
    # `<ns>/camera/hand/compressed`, which is the bulk of those bags and was
    # being kept because nothing else here matched it.
    "/compressed",
    # Spot's REGISTERED DEPTH (`<ns>/depth_registered/hand/image`) matched none
    # of the patterns above: not "image_raw", not "/depth/" (it is
    # "/depth_registered/"), not "aligned_depth". So a bag carrying it would
    # have been "slimmed" with the depth left in — ~0.6 MB per 640x480 16UC1
    # frame, i.e. the slim copy would have been the same order as the heavy one
    # and `fetch_slim_bags.sh` would have pulled it over the lab Wi-Fi. Found
    # while adding `spot_route_replay` (2026-08-05); the /camera_info sibling is
    # protected by LIGHT_TOPIC_SUFFIXES below, so intrinsics still survive.
    "depth_registered",
    # Livox Mid-360S (#109): the CustomMsg point stream (~200k pts/s, ~3 MB/s)
    # and the FAST-LIO2 registered clouds. `/livox/imu` stays light on purpose:
    # 200 Hz of sensor_msgs/Imu is ~60 KB/s and the odometry cannot be
    # re-run without it.
    "/livox/lidar", "/cloud_registered", "/cloud_effected", "/Laser_map",
)

#: Never treated as heavy however they are named. Two kinds live here:
#: camera_info (a few hundred bytes each, and without the intrinsics a recorded
#: estimate cannot be re-projected), and the #94 debug overlay - which is a
#: *compressed image* and so would be caught by "/compressed" above, except
#: that carrying it is the entire point of a light bag (~18 KB/frame at 2 Hz).
LIGHT_TOPIC_SUFFIXES = ("/camera_info", "/debug/overlay/compressed")




def _mocap_and_firmware(cf_name: str) -> List[str]:
    """The #77 position-only set: what the drone was told and what it did."""
    return [
        # /poses_full = clean MOCAP; /poses_degraded = degrade-shim output
        # (present only when DEGRADE); /poses = what the firmware actually got;
        # /poses_full_gt = GT incl. yaw.
        "/poses", "/poses_full", "/poses_full_gt", "/poses_degraded",
        f"/{cf_name}/pose", f"/{cf_name}/odom",
        f"/{cf_name}/cmd_full_state",
        f"/{cf_name}/commands_received_position",
        f"/{cf_name}/ext_pose_age",
        f"/{cf_name}/localization_cov", f"/{cf_name}/altitude_debug",
        f"/{cf_name}/motors", f"/{cf_name}/battery", f"/{cf_name}/status",
        "/tf", "/tf_static", "/rosout",
    ]


def _markerless_debug(node: str = None, bridge: str = None,
                      watchdog: str = None, camera: str = None) -> List[str]:
    """The #94 debug channels: everything the estimator knows, none of the pixels."""
    node = _pick(node, 'markerless_node')
    bridge = _pick(bridge, 'extpos_bridge')
    watchdog = _pick(watchdog, 'watchdog')
    camera = _pick(camera, 'camera')
    return [
        f"{node}/position",                     # the estimate (PointStamped)
        f"{node}/status",                       # fired / dropout:<reason> lines
        f"{node}/debug/markers",                # verdict-coloured MarkerArray
        f"{node}/debug/metrics",                # DiagnosticArray scalars
        f"{node}/debug/overlay/compressed",     # ~18 KB JPEG at debug_overlay_hz
        f"{bridge}/status",                     # safety-gate / latch state
        f"{bridge}/debug/metrics",              # same numbers, plottable
        f"{watchdog}/health",                   # armed | disarmed-ok | BLIND:...
        f"{camera}/color/camera_info",          # tiny; needed to re-project
    ]


def _markerless_debug_handcam(node: str = None,
                              bridge: str = None) -> List[str]:
    """The #94 debug channels when the estimator watches SPOT'S HAND names()['camera'].

    Same estimator channels as ``_markerless_debug``, two deliberate
    differences: no watchdog (the shadow campaigns fly nothing, and when they
    do the flight watchdog is already in ``mocap_firmware``'s /rosout), and no
    d435 camera_info — the intrinsics that matter are the hand camera's, which
    ``_spot_observer`` already records. Still NO PIXELS: the 59.8 -> 7.4 Hz
    starvation was measured on this hardware and the overlay JPEG at 2 Hz is
    the eyeball channel.
    """
    node = _pick(node, 'markerless_node')
    bridge = _pick(bridge, 'extpos_bridge')
    return [
        f"{node}/position",                     # the estimate (PointStamped)
        f"{node}/status",                       # fired / dropout:<reason> lines
        f"{node}/debug/markers",                # verdict-coloured MarkerArray
        f"{node}/debug/metrics",                # DiagnosticArray scalars
        f"{node}/debug/overlay/compressed",     # ~18 KB JPEG at debug_overlay_hz
        f"{bridge}/status",                     # shadow / latch state
        f"{bridge}/debug/metrics",              # same numbers, plottable
        # THE OBSERVER'S ACTUAL INPUT on a pose_source=markerless trial, i.e. the
        # estimate AFTER point_to_pose_relay's plausibility gate (2026-08-19).
        # Without it a bag records what the estimator SAID and what Spot DID with
        # no way to see what Spot was told, so "did a phantom get through the
        # gate, or did the observer chase a stale one" cannot be answered from the
        # bag. That question cost a full diagnosis on 20260819T121527, where the
        # raw estimate was 2.02 m off median and the gated stream was invisible.
        # Same lesson observer_closed_loop already records about spot_observer
        # recording none of the estimator's channels: record the thing the trial
        # is ABOUT, not just its neighbours.
        "/markerless/drone_pose",               # post-gate, what the observer sees
    ]


def _camera_streams(camera: str = None) -> List[str]:
    """The multi-GB half of a full bag."""
    camera = _pick(camera, 'camera')
    return [
        f"{camera}/color/image_raw",
        f"{camera}/aligned_depth_to_color/image_raw",
        f"{camera}/aligned_depth_to_color/camera_info",
    ]


def _replay_streams(camera: str = None) -> List[str]:
    """The replay-grade compressed pixel streams (2026-08-03 directive).

    These topics exist ONLY because ``ros2_realsense_camera_apriltags.launch.py``
    starts two ``image_transport republish`` nodes (``enable_replay_compression``,
    default on). The RealSense driver itself publishes NO image_transport
    sub-topics on this rig: with ``use_intra_process_comms: True`` (which the rig
    needs for NITROS zero-copy) realsense-ros 4.58 constructs a plain
    ``image_rcl_publisher`` instead of an ``image_transport_publisher``
    (rs_node_setup.cpp, verified 2026-08-03). Recording this profile against a
    rig launched with ``enable_replay_compression:=false`` writes empty channels.

    Colour is JPEG (detector replay does not need lossless colour); depth is
    ``compressedDepth``, whose 16UC1 path is a LOSSLESS PNG of the raw
    millimetre values — the plugin only zeroes pixels beyond ``depth_max``
    (default 10 m, deeper than anything in the lab volume), so a replayed
    depth frame is bit-identical where it matters.
    """
    camera = _pick(camera, 'camera')
    return [
        f"{camera}/color/image_raw/compressed",
        f"{camera}/aligned_depth_to_color/image_raw/compressedDepth",
        f"{camera}/aligned_depth_to_color/camera_info",
    ]


def _spot_replay_streams(spot: str = None) -> List[str]:
    """Replay-grade pixels for the SPOT HAND names()['camera'] (the `markerless_replay` tier, this rig's spelling).

    Why this exists instead of reusing ``replay_streams``: that group names the
    ``/camera/d435/...`` topics of the tripod metrology rig, published only
    because that rig's launch starts two ``image_transport republish`` nodes.
    None of those topics exist on the Spot rig, so recording ``markerless_replay``
    here would have written three EMPTY channels and produced a bag that looks
    replay-grade and is not. The deployed observer sensor is Spot's hand camera +
    ToF (the 2026-07-31 hand-camera pivot), so this is where the replay tier has
    to live for anything Spot-side.

    Two deliberate differences from the d435 tier:

    * **Colour needs no republisher.** ``spot_ros2`` publishes
      ``<ns>/camera/hand/compressed`` (JPEG) natively — verified in the recorded
      2026-05-28/29 bags' ``metadata.yaml`` — so the colour half is free.
    * **Depth is recorded RAW 16UC1, not ``compressedDepth``.** No republisher
      exists for the hand depth stream on this rig, and the one that does
      (``enable_replay_compression``) belongs to the RealSense launch. Raw 16UC1
      at 640x480 is ~0.6 MB/frame, so this is the honest cost, not a smaller one.
      If a session needs it smaller the knob is throttling the depth rate, not a
      lossier codec (the same conclusion the d435 tier reached).
    """
    spot = _pick(spot, 'spot_name')
    return [
        f"/{spot}/camera/hand/compressed",       # JPEG colour, native from the driver
        f"/{spot}/camera/hand/camera_info",      # the real intrinsics (also in _spot_observer)
        f"/{spot}/depth_registered/hand/image",  # 16UC1 mm, registered to colour
        f"/{spot}/depth_registered/hand/camera_info",
    ]


def _route_demo() -> List[str]:
    """Operator-published annotations that only a human can produce.

    ``/route_demo/loop_closure_cm`` carries the TAPE-MEASURED offset between the
    landing pad's mark and where the drone actually touched down — the paper's
    beyond-volume loop-closure number. It is in the record list, not in a
    notebook or a lab book, because a number that lives outside the bag is a
    number the analysis cannot join to the flight that produced it.

    The operator publishes it during the action's post-land hold (see the route
    campaign's ``operator_live_steps``). rosbag2 keeps polling for listed topics
    that do not exist yet, so a topic first published mid-trial is still
    recorded — which is what makes this work without a node.

    The two ``graph_nav_map_tf`` channels are here for the same reason: on a
    ``spot_localization=graph_nav`` trial that node OWNS the edge that ties the
    camera to the world, so "was Spot actually localized to its map, and for how
    much of the route" is a question about the demo itself rather than about the
    rig. ``~/status`` carries ``localized`` / ``not_localized`` plus the
    correction age, and it is the only place a frozen map alignment (odometry
    still running, corrections stale) is visible after the fact.
    """
    return [
        "/route_demo/loop_closure_cm",
        "/graph_nav_map_tf/status",
        "/graph_nav_map_tf/body_pose",
        # THE names()['watchdog'] BELONGS IN A FLIGHT BAG. spot_route_replay composes
        # markerless_debug_handcam, which drops `{watchdog}/health` on the
        # grounds that "the shadow campaigns fly nothing" - true for those
        # campaigns, false for this one, which is the only Spot-rig campaign
        # that ARMS. On this route the 0.3 s localization watchdog is the thing
        # that lands the drone when the camera loses it, so "did the watchdog
        # fire, and on which leg" is the first question a mid-route landing
        # raises. verify_route_demo.py already reads and summarises this topic;
        # without it recorded that summary was silently empty on every trial.
        f"{names()['watchdog']}/health",
    ]


def _mocap_rig(camera: str = None) -> List[str]:
    """The `ros2_crazyflie_crazyswarm2_realsense_apriltag_mocap` rig's mocap set.

    That rig is NOT the flight stack and does not use flight-stack topic names:
    `motion_capture_tracking` publishes the NamedPoseArray on ``/mocap/poses``
    (both rigid bodies) plus per-body PoseStamped conveniences, and there is no
    ``/poses_full``. Recorded separately so a static-capture bag carries its GT
    under the name it actually has -- analyses take ``--gt-topic /mocap/poses``.
    """
    camera = _pick(camera, 'camera')
    return [
        "/mocap/poses",            # NamedPoseArray: drone + camera rigid bodies
        "/mocap/drone_pose", "/mocap/camera_pose",
        "/apriltag/poses", "/apriltag/drone_pose", "/apriltag_detections",
        "/tf", "/tf_static", "/rosout",
        f"{camera}/color/camera_info",
    ]


def _spot_observer(spot: str = None) -> List[str]:
    """The Spot active-observer channels (issue #79).

    Two groups, and both are needed to score an observer ablation:

    * the **geometry the observer is scored on** — ``/mocap/poses`` carries every rigid
      body (drone, ``spot``, ``spot_hand``), which with ``/tf`` is enough to reconstruct
      the camera pose and hence off-axis angle, range and in-frame fraction OFFLINE,
      with no detector in the loop. That is deliberately how the first flights are
      scored: it isolates the controller from the perception stack.
      NOTE there is no AprilTag on the drone, so no ``/apriltag/*`` here. When the
      yoloseg estimator goes in the loop, add its topics — its availability then
      becomes the dependent variable and must be recorded or the trial cannot answer
      the question it was flown for.
    * the **controller's own telemetry** — what it believed about framing, range and
      its solve rate. Not evidence on its own (it is the controller's opinion), but it
      is what distinguishes "the controller was wrong" from "the controller was
      starved", which are different fixes.

    Spot's TF tree comes in via ``/tf`` from the mocap/firmware part, which is what lets
    the analysis reconstruct the camera pose offline.
    """
    spot = _pick(spot, 'spot_name')
    return [
        "/mocap/drone_pose", "/mocap/poses",
        "/perception_aware/offaxis_deg", "/perception_aware/pred_availability",
        "/perception_aware/range_m", "/perception_aware/solve_ms",
        "/perception_aware/drone_speed_cmd", "/perception_aware/ref_track_error_m",
        "/perception_aware/cmd_body", "/perception_aware/cmd_hand",
        f"/{spot}/camera/hand/camera_info",
        f"/{spot}/status/leases",
    ]


def _aideck_mocap(cf_name: str) -> List[str]:
    """The AI deck camera and its ground truth (Paper 3, #107, added 2026-10-01).

    For the handheld H3 relocalization experiment
    (``experiments/ros2_crazyflie_aideck_mocap_relocalization``): AI deck frames, each paired
    offline with the drone's motion-capture pose. No crazyflie_server runs (motors off, no
    radio), so none of the ``mocap_firmware`` telemetry exists, and listing it would only add
    empty channels.

    * ``image_raw`` is the 324 x 244 mono8 stream (79,056 B per frame). ``image_raw/compressed``
      is listed for the GAP8 JPEG mode that #110 is adding: a recorder polls for a listed topic
      that is not advertised yet, so the profile already covers both stream formats.
    * ``camera_info`` is what the node loaded at capture time (the calibration provenance).
    * ``/mocap/poses`` is the RAW Motive stream (NamedPoseArray, every enabled rigid body),
      under the name the experiment's launch gives it. Analyses take
      ``--gt-topic /mocap/poses --rigid-body <asset>``.
    * ``imu`` is the Crazyflie IMU from ``crazyflie_imu_node`` (100 Hz, firmware-clock stamps),
      present only when the launch runs with ``start_imu:=true``, for the drone VIO recordings
      of #122 (added 2026-10-13).
    """
    return [
        f"/{cf_name}/aideck/image_raw",
        f"/{cf_name}/aideck/image_raw/compressed",
        f"/{cf_name}/aideck/camera_info",
        f"/{cf_name}/imu",
        "/mocap/poses",
        "/tf", "/tf_static", "/rosout",
    ]


def _vio_shadow(cf_name: str) -> List[str]:
    """What OpenVINS in shadow adds to ``aideck_mocap`` (Paper 3 drone VIO, #122, 2026-10-13).

    For ``ros2_crazyflie_aideck_vio_shadow.launch.py``: OpenVINS runs live on the AI deck
    camera and the Crazyflie IMU but never steers the drone.

    * ``imu_log`` is crazyswarm2's raw ``LogDataGeneric`` custom topic (firmware timestamp,
      header stamp at the server, the six values in g and deg/s), present in flights with
      ``crazyflies_vio_shadow.yaml``. Recorded next to the relay's ``/<cf>/imu`` (already in
      ``aideck_mocap``), so the firmware-clock stamping can be redone offline.
    * ``/ov_msckf/odomimu`` (nav_msgs/Odometry) and ``/ov_msckf/poseimu`` are the shadow
      estimate that the analysis compares with mocap.

    The flight's own topics (firmware telemetry, observer) stay in the flight's own profile:
    this profile is meant for a second recorder next to it, or for a handheld recording.
    """
    return [
        f"/{cf_name}/imu_log",
        "/ov_msckf/odomimu",
        "/ov_msckf/poseimu",
    ]


def _shadow_sensors(cf_name: str) -> List[str]:
    """The in-flight raw sensor blocks of Paper 3 (#158, added 2026-10-29).

    For ``experiments/ros2_crazyflie_shadow_sensors_flight``: crazyswarm2's custom log topics
    ``flow_log`` (``motion.deltaX``, ``motion.deltaY``, ``motion.squal``, ``motion.shutter``,
    ``range.zrange``, ``baro.asl``, 50 or 100 Hz) and ``flow_quality`` (``motion.maxRaw``,
    ``minRaw``, ``Rawsum``, ``outlierCount``, ``std``, ``motion``, 10 Hz), as
    ``LogDataGeneric``. The shadow height sources of design v2 part 2 (the firmware's
    flat-floor height, the barometer and the map-aware height, #167) replay from these.
    """
    return [
        f"/{cf_name}/flow_log",
        f"/{cf_name}/flow_quality",
    ]


def _teleop_pad(joy_topic: str = "/joy") -> List[str]:
    """The gamepad of a teleoperated flight (#168, added 2026-10-29).

    ``/joy`` (``sensor_msgs/Joy``, joy_node at its autorepeat rate, 20 Hz): every stick and
    button the pilot moved. The teleop node's setpoint stream, ``/<cf>/cmd_full_state``, is
    already in ``_mocap_and_firmware``. Light: a few hundred bytes per message.
    """
    return [joy_topic]


def _livox_lidar() -> List[str]:
    """The Livox Mid-360S driver outputs (#109): the point stream and the IMU.

    `/livox/lidar` is `livox_ros_driver2/msg/CustomMsg` when the launch file runs with
    `xfer_format:=1` (the default, and the only format FAST-LIO accepts because it carries
    per-point timestamps) or `sensor_msgs/PointCloud2` with `xfer_format:=0`. Same topic
    name either way, so the profile does not care. `/livox/imu` is 200 Hz sensor_msgs/Imu.
    """
    return ["/livox/lidar", "/livox/imu"]


def _fast_lio() -> List[str]:
    """FAST-LIO2 outputs (#109): odometry, path and the registered scan.

    `/Odometry` (camera_init -> body) is the trajectory that gets compared with mocap;
    `/cloud_registered` is the per-scan cloud in the map frame, from which the map is
    rebuilt offline (the full `/Laser_map` is not recorded: it is the accumulated map,
    re-published every scan, and is redundant with the registered scans). TF carries the
    same odometry as a transform and the static mount TF when on Spot.
    """
    return ["/Odometry", "/path", "/cloud_registered", "/cloud_registered_body",
            "/tf", "/tf_static", "/rosout"]


def _spot_body_state(spot: str = None) -> List[str]:
    """Spot's own kinematic odometry and joint states (#109 mapping on the robot).

    Recorded next to FAST-LIO so the LiDAR-inertial trajectory can be compared with the
    robot's leg odometry as well as with mocap (`/mocap/poses` from the lab rig).
    """
    spot = _pick(spot, 'spot_name')
    return [f"/{spot}/odometry", f"/{spot}/joint_states", f"/{spot}/status/leases",
            "/mocap/poses"]


def build_topics(profile: str, cf_name: str = None,
                 node: str = None, bridge: str = None,
                 watchdog: str = None, camera: str = None) -> List[str]:
    """Return the topic list for a named profile (order preserved, deduped)."""
    cf_name = _pick(cf_name, 'cf_name')
    node = _pick(node, 'markerless_node')
    bridge = _pick(bridge, 'extpos_bridge')
    watchdog = _pick(watchdog, 'watchdog')
    camera = _pick(camera, 'camera')
    if profile not in PROFILES:
        raise ValueError(f"unknown record profile {profile!r}; "
                         f"expected one of {sorted(PROFILES)}")
    topics: List[str] = []
    for part in PROFILES[profile]:
        if part == "mocap_firmware":
            topics += _mocap_and_firmware(cf_name)
        elif part == "markerless_debug":
            topics += _markerless_debug(node, bridge, watchdog, camera)
        elif part == "camera_streams":
            topics += _camera_streams(camera)
        elif part == "replay_streams":
            topics += _replay_streams(camera)
        elif part == "mocap_rig":
            topics += _mocap_rig(camera)
        elif part == "spot_observer":
            topics += _spot_observer()
        elif part == "markerless_debug_handcam":
            topics += _markerless_debug_handcam(node, bridge)
        elif part == "spot_replay_streams":
            topics += _spot_replay_streams()
        elif part == "route_demo":
            topics += _route_demo()
        elif part == "livox_lidar":
            topics += _livox_lidar()
        elif part == "fast_lio":
            topics += _fast_lio()
        elif part == "spot_body_state":
            topics += _spot_body_state()
        elif part == "aideck_mocap":
            topics += _aideck_mocap(cf_name)
        elif part == "vio_shadow":
            topics += _vio_shadow(cf_name)
        elif part == "shadow_sensors":
            topics += _shadow_sensors(cf_name)
        elif part == "teleop_pad":
            topics += _teleop_pad()
    seen = set()
    return [t for t in topics if not (t in seen or seen.add(t))]


#: profile name -> ordered list of topic groups
PROFILES: Dict[str, List[str]] = {
    # ⚠️ MEASURED COST (2026-07-29, Orin): recording the raw camera streams
    # STARVES THE PIPELINE BEING MEASURED. Aligned depth, same rig, same code:
    #     rig alone .............. 59.8 Hz
    #     + markerless estimator . 52.4 Hz
    #     + markerless_full bag ... 7.4 Hz   (load average 4.1)
    # The estimator can only publish as fast as the depth it consumes, so a
    # trial recorded on markerless_full measures a system throttled by its own
    # instrumentation — and its availability number is not the deployed one.
    # markerless_light still carries the debug overlay (compressed), which is
    # what forensics actually need. Use markerless_full only when raw pixels
    # are the point, and never for an availability or accuracy cell.
    "position_only": ["mocap_firmware"],
    # Paper 3 flight on mocap with every drone-side estimator recorded in shadow (#158, #164,
    # 2026-10-29): the flight's own telemetry, the AI deck frames and the relay's IMU, OpenVINS,
    # and the raw flow, range and barometer blocks. One flight feeds every shadow estimator.
    "shadow_flight": ["mocap_firmware", "aideck_mocap", "vio_shadow", "shadow_sensors"],
    # shadow_flight flown by a pilot on a gamepad (#168, 2026-10-29): the same bag plus /joy, so
    # the sticks and buttons sit next to the setpoints they produced. Added together with its
    # trial_metadata_core.yaml enum entry (L9).
    "teleop_shadow_flight": ["mocap_firmware", "aideck_mocap", "vio_shadow", "shadow_sensors",
                             "teleop_pad"],
    "markerless_light": ["mocap_firmware", "markerless_debug"],
    # The replay-grade middle tier (the lab, 2026-08-03: future experiments
    # must carry replay-grade data). WHY IT EXISTS: the three 07-29 fig-8 cell
    # bags were recorded on markerless_light — the right call against the
    # starvation above, but it left them with NO pixels, so the 08-03
    # v1-vs-v3 detector replay could not use a single 07-29 bag and had to
    # fall back to a 2026-07-27 markerless_full bag with different GT. This
    # profile is what would have allowed that replay on the bags in question:
    # JPEG colour (plenty for detector replay) + compressedDepth (LOSSLESS
    # PNG for the 16UC1 aligned depth, see _replay_streams) + camera_info.
    # ⚠️ PENDING WEEK-33 DAY-1 STATIC VERIFICATION on the rig: the frame
    # sizes below are measured, but the live JPEG/PNG encoding CPU cost on
    # the rig host is not — the 59.8 -> 7.4 Hz starvation was measured on
    # that hardware, so do not fly this profile before the static check.
    # Requires enable_replay_compression:=true on the rig launch (default).
    "markerless_replay": ["mocap_firmware", "markerless_debug", "replay_streams"],
    "markerless_full": ["mocap_firmware", "markerless_debug", "camera_streams"],
    # Static operator-guided capture on the realsense_apriltag_mocap rig: pixels
    # + aligned depth + that rig's own mocap naming, and no firmware topics
    # (nothing is flying). Recorded PER POSE by static_residual_sweep.py, only
    # during each dwell -- recording continuously while the operator walks
    # between poses wrote 19-28 GB of unusable frames per session (2026-07-27).
    "static_sweep": ["mocap_rig", "camera_streams"],
    # The same session's continuous companion: no pixels, so it costs ~5 MB/min
    # and can run start-to-finish. This is what the camera-rigid-body stability
    # check needs -- it must see the WHOLE session, including the walking
    # between poses, or it cannot correlate the camera body's reported pose
    # against the drone's position.
    "static_sweep_light": ["mocap_rig"],
    # Spot active-observer ablation (#79). mocap_firmware carries the drone and the
    # TF tree; spot_observer adds the AprilTag estimator being protected (the
    # dependent variable) and the controller's own telemetry. No pixels: the hand
    # camera's image stream is not needed to score framing, and recording it would
    # cost the same starvation that markerless_full measured (59.8 -> 7.4 Hz).
    "spot_observer": ["mocap_firmware", "spot_observer"],
    # spot_observer + the #76 estimator shadow-running on Spot's hand camera
    # (#79 detector_model axis). The estimator's channels ARE the dependent
    # variable of the handcam shadow campaigns; still no pixels.
    "spot_observer_handcam": ["mocap_firmware", "spot_observer",
                              "markerless_debug_handcam"],
    # spot_observer_handcam + the drone's AI deck frames, the relay's IMU and OpenVINS
    # (#170, 2026-11-05): the quadruped's markerless estimate and the drone's own VIO in ONE
    # bag with mocap truth, so a gap in Spot's view can be cut offline and bridged by the VIO.
    # No bag before this profile had both (0 of 4,842 in the archive on 2026-11-05). The AI
    # deck frames are small (320 x 320 mono at about 8 fps, about 0.8 MB/s), not the hand
    # camera's pixels, which this profile still leaves out because of the 59.8 -> 7.4 Hz
    # starvation above. Whether the estimator keeps its rate with OpenVINS and this recorder
    # on the same Orin is NOT measured yet: check it on the ground before the first flight.
    "spot_observer_handcam_vio": ["mocap_firmware", "spot_observer",
                                  "markerless_debug_handcam", "aideck_mocap", "vio_shadow"],
    # spot_observer_handcam PLUS the hand camera's replay-grade pixels and
    # NOTHING of the route demo (2026-08-11, the depth-confound follow-up to
    # the 08-10 background-lock finding). WHY IT EXISTS: the 08-10 shadow bags
    # proved the in-mask ToF depth sits at ~3.8 m at every true range, but they
    # carry no pixels, so the one alternative hypothesis still alive — the
    # depth-to-color REGISTRATION being offset/scaled so a correct mask samples
    # the wrong depth pixels — cannot be tested on them. This profile is the
    # shadow rig's bag made able to answer that: native JPEG colour + raw 16UC1
    # registered depth + both camera_infos (see _spot_replay_streams).
    # STATIC (non-flying) MATERIAL ONLY, same rule the 08-10 D435 static check
    # imposed on markerless_replay: the per-external-subscriber raw
    # serialization starvation is unmeasured on this rig, and a static carry
    # with a halved estimator is still a usable registration bag, while a
    # flight with one is not.
    "spot_observer_handcam_replay": ["mocap_firmware", "spot_observer",
                                     "markerless_debug_handcam",
                                     "spot_replay_streams"],
    # spot_observer_handcam_replay + the gamepad (#170, 2026-11-09): the hand camera's
    # replay-grade pixels of a drone that a pilot FLIES with the PS5 DualSense, as training
    # data for the hand-camera detector (the drone with pink guards, auto-labeled from the
    # mocap projection). /joy proves the pad flew it; the teleop setpoints /<cf>/cmd_full_state
    # are already in mocap_firmware. This is a flight with the replay streams the profile
    # above keeps to static material, accepted for one reason: the drone flies on MOCAP and
    # the estimator only listens, so a starved estimator costs detections, not the flight.
    # What the flight does depend on, the mocap feed, the campaign's gate and verify watch.
    "spot_observer_handcam_teleop": ["mocap_firmware", "spot_observer",
                                     "markerless_debug_handcam",
                                     "spot_replay_streams", "teleop_pad"],
    # The beyond-the-volume route demo (ICRA Paper 2 Experiment 3). It is
    # spot_observer_handcam plus the two things that demo specifically needs:
    #   * REPLAY-GRADE PIXELS from the hand camera, because outside the mocap
    #     volume the camera is the ONLY witness of where the drone was. There is
    #     no ground truth to re-derive the flight from, so if a detector question
    #     comes up afterwards the bag must be able to answer it. This is the
    #     2026-08-03 replay-grade directive applied to the rig that has no
    #     RealSense (see _spot_replay_streams for why markerless_replay itself
    #     cannot be used here).
    #   * the operator's TAPE-MEASURED loop-closure annotation, so the paper's
    #     cheapest honest metric ends up inside the bag rather than beside it.
    # ⚠️ COST IS AN ESTIMATE, not a measurement: ~0.4 to 0.8 GB/min, the same
    # order as markerless_replay, and the SAME open question — the live encoding
    # and IO cost on the rig is unverified (week-33 day-1 static check). The
    # 59.8 -> 7.4 Hz starvation was measured on this hardware, so run the static
    # check before the first route flight, not after.
    # ⚠️ 2026-08-10: markerless_replay FAILED its static check on the D435 rig —
    # the starvation is per-external-subscriber raw serialization in the camera
    # container, not encoder CPU (measured with no-op subscribers). The hand
    # camera's frames are ~5-10x smaller, so this profile MAY still pass, but
    # the same static check MUST run on the route rig before day 6, and the
    # observer estimator's rate is the number to watch.
    "spot_route_replay": ["mocap_firmware", "spot_observer",
                          "markerless_debug_handcam", "spot_replay_streams",
                          "route_demo"],
    # Livox Mid-360S (#109). `livox_mapping` is the desk and first-map profile: the
    # raw point stream + IMU (so FAST-LIO can be re-run offline with other settings)
    # plus FAST-LIO's outputs. `livox_mapping_spot` adds Spot's leg odometry, joint
    # states and the lab mocap for the trajectory comparison of Phase 7.
    "livox_mapping": ["livox_lidar", "fast_lio"],
    "livox_mapping_spot": ["livox_lidar", "fast_lio", "spot_body_state"],
    # Paper 3 handheld relocalization (#107, 2026-10-01): AI deck frames + the raw Motive
    # stream, nothing else. Added together with its trial_metadata_core.yaml enum entry (L9).
    "aideck_mocap": ["aideck_mocap"],
    # Paper 3 drone VIO in shadow (#122, 2026-10-13): aideck_mocap + the raw IMU log topic
    # and the OpenVINS outputs. Added together with its trial_metadata_core.yaml enum entry (L9).
    "aideck_vio_shadow": ["aideck_mocap", "vio_shadow"],
}

#: Rough per-flight sizes, for picking a profile (see the module docstring and
#: ``experiments/AGENTS.md``). Measured/estimated 2026-07-25.
PROFILE_NOTES = {
    # From the 2026-10-01 handheld AI deck bag (raw frames only, no mocap): 98.7 MiB for
    # 124.5 s, i.e. about 0.79 MiB/s. Motive at 120 Hz adds little next to the frames.
    "aideck_mocap": "~48 MiB/min of raw AI deck frames at 10.6-11 fps (2026-10-01 handheld "
                    "bag), plus the mocap NamedPoseArray. The GAP8 JPEG build sends a median "
                    "of 5,303 B per frame at 8 fps (2026-10-01), about 15x smaller",
    # The handheld bag 20261013T143551 (JPEG frames decoded to raw plus the JPEG bytes, IMU
    # at 100 Hz, mocap at 120 Hz) is 77 MB for 99.8 s. The OpenVINS outputs add little.
    "aideck_vio_shadow": "MEASURED 2026-10-13 without the OpenVINS outputs: ~46 MB/min "
                         "(raw and JPEG AI deck frames at 8 fps, IMU 100 Hz, mocap 120 Hz)",
    "livox_mapping": "MEASURED 2026-10-13 (desk, static): 117 MB for 19.7 s = ~0.36 GB/min "
                     "(CustomMsg + registered scans at 10 Hz, IMU at 200 Hz)",
    "livox_mapping_spot": "~0.2-0.4 GB/min ESTIMATE (livox_mapping + Spot odometry, "
                          "joint states and mocap, all small)",
    "position_only": "~5-15 MB/min (mocap + telemetry only)",
    "markerless_light": "~5-10 MB/min (adds ~40 KB/s of debug channels)",
    # ESTIMATE until the week-33 day-1 static verification. Per-frame sizes
    # MEASURED 2026-08-03 on 40 real 848x480 frames of the 2026-07-01
    # seg_dataset_flight bag (plugin defaults except JPEG quality 80, which
    # the launch sets): colour JPEG q80 ~51 KB/frame (q95 would be ~111 KB),
    # aligned-depth lossless PNG level 3 ~177 KB/frame (4.5x vs raw). At the
    # rig's nominal 60 Hz that is ~3.1 + ~10.6 MB/s — DEPTH DOMINATES, and
    # honesty requires saying this is ~0.8 GB/min, NOT tens of MB/min: ~9x
    # smaller than raw at equal rate, not 100x. Lossless 16UC1 depth has a
    # hard floor; if week 33 needs it smaller, the knob is recording depth at
    # a throttled rate, not a lossier codec.
    # WHERE THESE BAGS GO (2026-08-04): a four-day replay-grade lab week is
    # ~40 GB expected and ~115 GB worst case, which the rig's 305 GB free
    # absorbs with room to spare, so week 33 needs no external disk. The season
    # does: bag_storage.py + offload_bags.py own that, and the sizing table is
    # in experiments/AGENTS.md -> "Storage: where the fat bags go".
    "markerless_replay": "MEASURED 2026-08-10 (week-33 day-1 static check): "
                         "encoders sustain 59.2 Hz colour / 47.6 Hz depth "
                         "(depth PNG pegs one core), but the estimator HALVES "
                         "(24.4 -> 11.7 Hz pair rate) because the camera "
                         "container serializes raw frames to each external "
                         "subscriber — measured with no-op subscribers, so no "
                         "throttle or nice can fix it. FLIGHT CELLS MUST NOT "
                         "PIN THIS PROFILE on the D435 rig; static/non-flying "
                         "recording only. (Old estimate: ~0.4-0.8 GB/min.)",
    "markerless_full": "~2-4 GB/min (adds raw colour + aligned depth)",
    "static_sweep": "~2-4 GB/min while recording (raw colour + aligned depth); "
                    "a sweep only dwells a few seconds per pose",
    "static_sweep_light": "~5 MB/min (mocap + TF only) — safe to run continuously",
    "spot_observer": "~5-15 MB/min (mocap + TF + AprilTag + observer telemetry; no pixels)",
    "spot_observer_handcam": "~5-20 MB/min (spot_observer + estimator debug "
                             "channels incl. the 2 Hz overlay JPEG; no pixels)",
    "spot_observer_handcam_replay": "~0.4-0.8 GB/min ESTIMATE (spot_observer_handcam "
                                    "+ the hand camera's native JPEG colour + RAW "
                                    "16UC1 registered depth at ~0.6 MB/frame; same "
                                    "unverified-Orin-cost caveat as spot_route_replay "
                                    "— STATIC recording only until measured)",
    "spot_observer_handcam_teleop": "~0.4-0.8 GB/min ESTIMATE (spot_observer_handcam_replay "
                                    "+ /joy, a few kB/s). The replay streams in FLIGHT are "
                                    "unmeasured on the Orin: the drone flies on mocap, so "
                                    "check the estimator's and the mocap feed's rates in the "
                                    "first bag",
    "spot_observer_handcam_vio": "~55-75 MB/min (spot_observer_handcam + the AI deck "
                                 "frames, IMU and OpenVINS; the shadow flight bag of "
                                 "2026-11-04 measured 53 MB/min for the drone part). The "
                                 "estimator's rate with OpenVINS on the same Orin is not "
                                 "measured yet: check it on the ground first",
    "spot_route_replay": "~0.4-0.8 GB/min ESTIMATE (spot_observer_handcam + the "
                         "hand camera's native JPEG colour + RAW 16UC1 registered "
                         "depth at ~0.6 MB/frame + the operator's loop-closure "
                         "annotation; live rate and Orin CPU cost unverified until "
                         "the week-33 day-1 static check)",
}


def is_heavy_topic(topic: str, patterns=HEAVY_TOPIC_PATTERNS) -> bool:
    """True for image/depth/pointcloud-class topics (the ones bags choke on)."""
    if topic.endswith(LIGHT_TOPIC_SUFFIXES):
        return False
    return any(p in topic for p in patterns)


def build_record_command(profile: str, output: str, cf_name: str = None,
                         storage: str = "mcap", max_duration_sec: int = 0,
                         compression: bool = False, **kwargs) -> List[str]:
    """Assemble the full ``ros2 bag record`` argv for a profile."""
    cmd = ["ros2", "bag", "record", "-s", storage, "-o", output]
    if compression:
        cmd += ["--compression-mode", "file", "--compression-format", "zstd"]
    if max_duration_sec and max_duration_sec > 0:
        # Humble's ros2bag has no --max-duration flag (found 2026-08-10, the
        # first time this path actually ran). A SIGINT at expiry closes the
        # bag cleanly, exactly like the operator's Ctrl-C.
        cmd = ["timeout", "--preserve-status", "-s", "INT",
               str(max_duration_sec)] + cmd
    return cmd + build_topics(profile, cf_name=cf_name, **kwargs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile", choices=sorted(PROFILES))
    ap.add_argument("--cf-name", default=None, help="default: trials.yaml record_profiles.cf_name, else cf1")
    ap.add_argument("--output", default="", help="bag output directory (-o)")
    ap.add_argument("--storage", default="mcap")
    ap.add_argument("--max-duration-sec", type=int, default=0)
    ap.add_argument("--compression", action="store_true",
                    help="zstd file compression (light bags are small anyway)")
    ap.add_argument("--topics-only", action="store_true",
                    help="print just the topic list, one per line")
    ap.add_argument("--run", action="store_true", help="execute instead of printing")
    args = ap.parse_args(argv)

    topics = build_topics(args.profile, cf_name=args.cf_name)
    if args.topics_only:
        print("\n".join(topics))
        return 0
    if not args.output:
        ap.error("--output is required unless --topics-only is given")
    cmd = build_record_command(args.profile, args.output, cf_name=args.cf_name,
                               storage=args.storage,
                               max_duration_sec=args.max_duration_sec,
                               compression=args.compression)
    if not args.run:
        print(" ".join(cmd))
        print(f"# profile {args.profile}: {len(topics)} topics, "
              f"{PROFILE_NOTES.get(args.profile, '')}", file=sys.stderr)
        return 0
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())

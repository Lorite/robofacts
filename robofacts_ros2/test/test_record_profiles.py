#!/usr/bin/env python3
"""Record profiles are project data: the loader, the merge order, the placeholders, the
record command, and the bag-slimming topic selection.

Pure Python: no ROS graph, no bag, no hardware. A project's own profiles are tested in that
project (the robotics lab pins its profiles in its own test_lab_record_profiles.py).
"""
from __future__ import annotations

import pytest
import yaml

from robofacts import record_profiles as rp
from robofacts import roots
from robofacts_ros2.record_profiles import build_record_command, build_topics, is_heavy_topic
from robofacts_ros2.slim_bag import select_topics

PROFILE_FILE = {
    "blocks": {
        "drone": ["/{cf_name}/pose", "/{cf_name}/battery", "/tf", "/rosout"],
        "estimator": {"topics": ["{markerless_node}/position", "{watchdog}/health",
                                 "{camera}/color/camera_info"]},
        "pixels": ["{camera}/color/image_raw"],
        "robot": ["/{spot_name}/odometry", "/tf"],
    },
    "profiles": {
        "flight": {"blocks": ["drone"], "size": "~5 MB/min"},
        "debug": {"blocks": ["drone", "estimator"], "topics": ["/joy"]},
        "full": {"blocks": ["drone", "estimator", "pixels"]},
        "with_robot": {"blocks": ["drone", "robot"]},
    },
}


#: A project with the two robot adapters whose default names the topics use, and the
#: estimator's names of its own.
BASE_TRIALS_YAML = (
    "experiments_root: experiments\nproject_root: .\nbackend: ros2\nadapters: [crazyflie, spot]\n"
    "record_profiles: {camera: /camera, markerless_node: /estimator, "
    "watchdog: /localization_watchdog}\n")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with a trials.yaml and a record_profiles.yaml, and no root variables."""
    for var in (roots.ENV_EXPERIMENTS_ROOT, roots.ENV_PROJECT_ROOT):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "trials.yaml").write_text(BASE_TRIALS_YAML)
    config = tmp_path / "experiments" / rp.PROJECT_PROFILES_REL
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump(PROFILE_FILE))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(project, data):
    (project / "experiments" / rp.PROJECT_PROFILES_REL).write_text(yaml.safe_dump(data))


def test_the_package_ships_no_profiles():
    """The package holds the mechanism, a project holds the topic sets."""
    assert not rp.PACKAGE_PROFILES_FILE.is_file() or not rp._read(rp.PACKAGE_PROFILES_FILE)["profiles"]


def test_profiles_come_from_the_project_file(project):
    assert rp.profile_files() == [project / "experiments" / rp.PROJECT_PROFILES_REL]
    assert rp.PROFILES == {"flight": ["drone"], "debug": ["drone", "estimator"],
                           "full": ["drone", "estimator", "pixels"],
                           "with_robot": ["drone", "robot"]}
    assert rp.PROFILE_NOTES == {"flight": "~5 MB/min"}


def test_blocks_are_concatenated_in_order_with_the_neutral_names(project):
    assert build_topics("flight") == ["/cf1/pose", "/cf1/battery", "/tf", "/rosout"]
    assert build_topics("debug") == ["/cf1/pose", "/cf1/battery", "/tf", "/rosout",
                                     "/estimator/position", "/localization_watchdog/health",
                                     "/camera/color/camera_info", "/joy"]


def test_repeated_topics_are_recorded_once(project):
    assert build_topics("with_robot") == ["/cf1/pose", "/cf1/battery", "/tf", "/rosout",
                                          "/spot/odometry"]


def test_names_come_from_trials_yaml_and_the_call(project):
    (project / "trials.yaml").write_text(
        "experiments_root: experiments\nproject_root: .\nbackend: ros2\nadapters: [crazyflie, spot]\n"
        "record_profiles: {cf_name: lab_drone, camera: /lab_cam, spot_name: lab_spot, "
        "markerless_node: /estimator, watchdog: /localization_watchdog}\n")
    assert build_topics("flight")[0] == "/lab_drone/pose"
    assert "/lab_spot/odometry" in build_topics("with_robot")
    listed = build_topics("full", cf_name="drone9", markerless_node="/mk", watchdog="/wd",
                          camera="/cam")
    assert "/drone9/battery" in listed and "/mk/position" in listed
    assert "/wd/health" in listed and "/cam/color/image_raw" in listed


def test_the_adapters_bring_the_robot_names(project):
    """The core has no names: cf_name and spot_name come from the enabled adapters."""
    (project / "trials.yaml").write_text("experiments_root: experiments\nproject_root: .\n")
    assert rp.names() == {}
    with pytest.raises(rp.ProfileError, match="cf_name"):
        build_topics("flight")
    (project / "trials.yaml").write_text(
        "experiments_root: experiments\nproject_root: .\nbackend: ros2\nadapters: [crazyflie]\n")
    assert rp.names() == {"cf_name": "cf1"}
    assert build_topics("flight")[0] == "/cf1/pose"


def test_an_unknown_name_names_itself(project):
    _write(project, {"blocks": {"b": ["/{rover}/odom"]}, "profiles": {"p": {"blocks": ["b"]}}})
    with pytest.raises(rp.ProfileError, match="rover"):
        build_topics("p")


def test_an_unknown_profile_raises(project):
    with pytest.raises(ValueError, match="nope"):
        build_topics("nope")


def test_an_unknown_block_names_its_profile(project):
    _write(project, {"blocks": {}, "profiles": {"p": {"blocks": ["missing"]}}})
    with pytest.raises(rp.ProfileError, match="'p' names unknown blocks"):
        rp.load()


@pytest.mark.parametrize("data, match", [
    ({"profile": {}}, "unknown top-level keys"),
    ({"blocks": {"b": {"topic": ["/x"]}}}, "topic list or a mapping"),
    ({"blocks": {"b": [1]}}, "list of topic names"),
    ({"profiles": {"p": {"blocks": "b"}}}, "list of block names"),
    ({"profiles": {"p": {"block": ["b"]}}}, "mapping of blocks"),
])
def test_a_malformed_file_is_refused(project, data, match):
    _write(project, data)
    with pytest.raises(rp.ProfileError, match=match):
        rp.load()


def test_a_later_file_replaces_a_block_of_the_same_name(project, monkeypatch, tmp_path):
    adapter = tmp_path / "adapter_profiles.yaml"
    adapter.write_text(yaml.safe_dump({"blocks": {"drone": ["/adapter"], "extra": ["/x"]},
                                       "profiles": {"adapter_only": {"blocks": ["extra"]}}}))
    monkeypatch.setattr(rp, "adapter_profile_files", lambda: [adapter])
    assert rp.profile_files()[0] == adapter
    assert build_topics("flight") == ["/cf1/pose", "/cf1/battery", "/tf", "/rosout"]
    assert build_topics("adapter_only") == ["/x"]


def test_no_project_means_no_profiles(tmp_path, monkeypatch):
    for var in (roots.ENV_EXPERIMENTS_ROOT, roots.ENV_PROJECT_ROOT):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    assert rp.profiles() == {}


def test_the_default_profile_is_a_project_setting(project):
    assert rp.default_record_profile() is None
    (project / "trials.yaml").write_text("experiments_root: experiments\nproject_root: .\n"
                                         "default_record_profile: flight\n")
    assert rp.default_record_profile() == "flight"


def test_the_cli_lists_and_prints(project, capsys):
    assert rp.main(["--list"]) == 0
    assert "flight" in capsys.readouterr().out
    assert rp.main(["flight", "--topics-only"]) == 0
    assert capsys.readouterr().out.split() == ["/cf1/pose", "/cf1/battery", "/tf", "/rosout"]


# --- the record command ----------------------------------------------------------

def test_record_command_shape(project):
    cmd = build_record_command("debug", "/tmp/bag_x", max_duration_sec=30, compression=True)
    # Humble's ros2bag has no --max-duration: the duration is a timeout -s INT
    # wrapper in front of the argv (2026-08-10), so the bag closes as on Ctrl-C.
    assert cmd[:5] == ["timeout", "--preserve-status", "-s", "INT", "30"]
    assert cmd[5:11] == ["ros2", "bag", "record", "-s", "mcap", "-o"]
    assert "/tmp/bag_x" in cmd and "--max-duration" not in cmd
    assert cmd[cmd.index("--compression-format") + 1] == "zstd"
    assert cmd[-1] == "/joy"


def test_record_command_no_duration_has_no_wrapper(project):
    cmd = build_record_command("flight", "/tmp/bag_x", cf_name="cf2")
    assert cmd == ["ros2", "bag", "record", "-s", "mcap", "-o", "/tmp/bag_x",
                   "/cf2/pose", "/cf2/battery", "/tf", "/rosout"]


# --- heavy topics and slim_bag ---------------------------------------------------

def test_camera_info_is_light_but_pixels_are_not():
    assert not is_heavy_topic("/camera/color/camera_info")
    assert is_heavy_topic("/estimator/debug/overlay/compressed"), "a JPEG stream is heavy by default"
    assert is_heavy_topic("/camera/color/image_raw")
    assert is_heavy_topic("/camera/aligned_depth_to_color/image_raw")
    assert is_heavy_topic("/camera/color/image_raw/compressed")
    assert is_heavy_topic("/camera/aligned_depth_to_color/image_raw/compressedDepth")
    assert is_heavy_topic("/nvblox_node/static_esdf_pointcloud")


def test_registered_depth_and_jpeg_streams_are_heavy_but_their_intrinsics_are_not():
    """The 2026-08-05 slimming fix: a JPEG stream and a registered depth stream matched no
    pattern, so slim copies kept them and were the same order of size as the heavy bag."""
    assert is_heavy_topic("/robot/camera/hand/compressed")
    assert is_heavy_topic("/robot/depth_registered/hand/image")
    assert not is_heavy_topic("/robot/camera/hand/camera_info")
    assert not is_heavy_topic("/robot/depth_registered/hand/camera_info")


def test_a_project_adds_heavy_patterns_and_light_suffixes(project):
    """slim_topics in trials.yaml: a lidar driver's stream the core cannot know, and a small
    debug image that is the point of a light bag."""
    assert not is_heavy_topic("/lidar/raw") and is_heavy_topic("/estimator/debug/overlay/compressed")
    (project / "trials.yaml").write_text(
        "experiments_root: experiments\nproject_root: .\n"
        "slim_topics: {heavy_patterns: [/lidar/raw], light_suffixes: [/debug/overlay/compressed]}\n")
    assert is_heavy_topic("/lidar/raw") and not is_heavy_topic("/lidar/imu")
    assert not is_heavy_topic("/estimator/debug/overlay/compressed")
    assert is_heavy_topic("/camera/color/image_raw/compressed")
    from robofacts_ros2 import record_profiles as ros_rp
    assert ros_rp.HEAVY_TOPIC_PATTERNS[-1] == "/lidar/raw"
    (project / "trials.yaml").write_text(
        "experiments_root: experiments\nproject_root: .\nslim_topics: {heavy_patterns: x}\n")
    with pytest.raises(ValueError, match="heavy_patterns"):
        is_heavy_topic("/x")


BAG_TOPICS = [
    "/tf", "/tf_static", "/rosout", "/poses", "/poses_full",
    "/camera/color/image_raw",
    "/camera/aligned_depth_to_color/image_raw",
    "/camera/color/camera_info",
    "/estimator/position",
    "/estimator/debug/overlay/compressed",
]


def test_slim_drops_only_the_heavy_topics(project):
    (project / "trials.yaml").write_text(
        "experiments_root: experiments\nproject_root: .\n"
        "slim_topics: {light_suffixes: [/debug/overlay/compressed]}\n")
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

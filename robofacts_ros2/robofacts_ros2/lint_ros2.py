#!/usr/bin/env python3
"""The lint rules that are facts about ROS 2. `trials lint` runs them after the core's rules
(robofacts.lint) for a project on the ROS 2 backend. The rule numbers are the core's.

L2  a launch_arg factor must name an argument the launch file declares.
    (`trajectory` was mapped to a launch_arg this launch does not have; ros2
    launch rejects unknown arguments, so every trial would have failed.)
L5  a campaign's pre-flight operator_steps must not start ROS processes: a
    trial's setup steps tear down the previous stack first (scripts/kill_ros.sh,
    which matches /opt/ros/ and any *_ws/install/, or an experiment's own stop
    script), so anything the operator starts beforehand is killed by the trial
    it was started for. Such things belong in aux_launches, or in
    operator_live_steps.
L11 every record profile, and every explicit record_topics list, must record
    /rosout. On goTo flights the /rosout log is a recorder of the commanded
    reference itself (the swarm server logs every go_to call, and the
    2026-08-13 goto_tracking analysis reconstructs lateral tracking from it),
    besides being the trim-window and backfill source for every bag. A profile
    edit that drops it would silently make future flights unanalyzable, so the
    guard lives here rather than in prose.
L16 on a real robot (`simulation: false`), runner.yaml's `explore:` block may
    not open every parameter (2026-10-16, Explore mode). Its
    `param_whitelist` is what a Foxglove tab may change on a running stack, so
    a pattern such as `.*` would make every gain and every safety limit of the
    robot writable from a browser. Name each parameter you allow. In
    simulation the default is `['.*']` and this rule does not apply.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from robofacts import campaign_spec as cs
from robofacts import trial_runner as tr
from robofacts.lint import EXPERIMENTS_ROOT, REPO_ROOT, campaigns_of, experiments_with_runner
from robofacts_ros2 import explore as ex


def launch_dirs(package: str) -> list[Path]:
    """Where a source package's launch files may live under the project root."""
    return [REPO_ROOT / "ros2_ws" / "src" / package / "launch",
            REPO_ROOT / "src" / package / "launch",
            REPO_ROOT / package / "launch"]

STARTS_ROS_PROCESS = re.compile(r"\bros2\s+(run|launch)\b")

def declared_launch_args(launch_file: str, package: str = "") -> set[str] | None:
    """Arguments a launch file declares, or None if it cannot be read."""
    path = next((d / launch_file for d in launch_dirs(package) if (d / launch_file).is_file()), None)
    if path is None:
        return None
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r"DeclareLaunchArgument\(\s*[\"']([a-zA-Z_][a-zA-Z0-9_]*)[\"']", src))

@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_launch_arg_factors_name_real_launch_arguments(experiment):
    """L2 — ros2 launch REJECTS an unknown argument, so this fails every trial."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    declared = declared_launch_args(config.launch_file, config.launch_package)
    if declared is None:
        pytest.skip(f"{config.launch_file} not found (external package?)")
    for name, factor in cs.load_factors(experiment, EXPERIMENTS_ROOT).items():
        if factor.maps_to.get("kind") != "launch_arg":
            continue
        targets = factor.maps_to.get("names") or [factor.maps_to.get("name")]
        for target in targets:
            assert target in declared, (
                f"{experiment}: factor {name!r} maps to launch argument {target!r}, which "
                f"{config.launch_file} does not declare — ros2 launch would reject it")

@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_pre_flight_operator_steps_do_not_start_ros_processes(experiment):
    """L5: the setup steps' stack teardown would kill them. They belong in aux or live steps."""
    for path in campaigns_of(experiment):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for step in raw.get("operator_steps") or []:
            assert not STARTS_ROS_PROCESS.search(str(step)), (
                f"{path.name}: an operator_step starts a ROS process:\n  {str(step)[:120]}\n"
                f"A trial's setup steps tear down the previous stack before the launch "
                f"(scripts/kill_ros.sh matches /opt/ros/ and any *_ws/install/), so it would "
                f"be killed by the very trial it was started for. Put it in aux_launches "
                f"(the runner starts it) or operator_live_steps (started while the trial "
                f"runs)")

def test_every_record_profile_records_rosout():
    """L11 - /rosout is a recorder of the commanded reference, not just a log.

    On goTo flights nothing streams a setpoint topic: the reference is
    reconstructed from the swarm server's go_to log on /rosout (the
    goto_tracking analysis, 2026-08-13), cross-checked against the flight-time
    commanded_waypoints.json. /rosout is also what the trim windows and the
    metadata backfill read. Today every profile inherits it from a composed
    topic group; this pins that so a future profile (or a group edit) cannot
    silently drop the channel and make every flight recorded on it
    unanalyzable.
    """
    from robofacts.record_profiles import build_topics, profiles

    for profile in profiles(EXPERIMENTS_ROOT):
        topics = build_topics(profile, experiments_root=EXPERIMENTS_ROOT)
        assert "/rosout" in topics, (
            f"record profile {profile!r} does not record /rosout. That channel "
            f"carries the go_to log (the commanded reference of goTo flights), "
            f"the analysis trim events and the backfill evidence, so a bag "
            f"without it cannot be trimmed, backfilled, or scored for "
            f"tracking. Add /rosout (or a group that carries it) to the "
            f"profile.")

@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_explicit_record_topics_record_rosout(experiment):
    """L11 (explicit-list form) - a runner.yaml that bypasses the named
    profiles with its own record_topics list must still record /rosout."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    if not config.record_topics:
        pytest.skip("uses a named record profile (covered by the profile rule)")
    assert "/rosout" in config.record_topics, (
        f"{experiment}: config/runner.yaml record_topics does not include "
        f"/rosout. The trim windows, the metadata backfill and (for goTo "
        f"flights) the commanded reference all read it.")

@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_explore_does_not_open_every_parameter_of_a_real_robot(experiment):
    """L16 - on hardware, each parameter Explore may change is named."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    if config.simulation or not config.explore_declared:
        return
    wide = [p for p in config.explore.get("param_whitelist", []) if ex.matches_everything(p)]
    assert not wide, (
        f"{experiment}: explore.param_whitelist has {wide}, which matches every parameter. "
        f"On real robots Explore lets a Foxglove tab change these on the running stack, so "
        f"name the parameters you allow, one pattern each (for example '^/drone1/controller\\.kp$')")

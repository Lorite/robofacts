"""Test setup of the core package: import it from this source tree when it is not installed.

These tests run with the core alone on the path. They must not need the ROS 2 package, a
ROS installation or the ROS example project: the tests of the two packages together are in
robofacts_ros2/test.
"""
import os
import sys
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


#: The example project of this package: the project in force for these tests.
EXAMPLE_PROJECT = PACKAGE_ROOT / "example_project"
#: The ROS 2 package's example project, for the tests marked ``ros2``.
ROS2_EXAMPLE = PACKAGE_ROOT.parent / "robofacts_ros2" / "example_project"


@pytest.fixture(autouse=True)
def _project_environment(request, monkeypatch):
    """A root or a backend set in the caller's shell must not decide what these tests see.

    A test marked ``ros2`` exercises the core together with the ROS 2 backend (its fixtures
    declare a ROS 2 stack or hold bags). It runs with that backend and the ROS 2 example
    project in force, and it is skipped where the package robofacts_ros2 is not installed,
    which is the case in the job that tests the core alone."""
    monkeypatch.delenv("TRIALS_BACKEND", raising=False)
    # The project in force is this package's example, never one that happens to lie above
    # the working directory. A test with its own project sets the two variables itself.
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(EXAMPLE_PROJECT / "experiments"))
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(EXAMPLE_PROJECT))
    if request.node.get_closest_marker("ros2") is None:
        return
    pytest.importorskip("robofacts_ros2")
    monkeypatch.setenv("TRIALS_BACKEND", "ros2")
    if ROS2_EXAMPLE.is_dir():
        monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(ROS2_EXAMPLE / "experiments"))
        monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(ROS2_EXAMPLE))
    from robofacts import trial_index
    trial_index._recorder_at.cache_clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "ros2: the test needs the package robofacts_ros2 (its backend and its example "
        "project). Skipped where that package is not installed")
    config.addinivalue_line(
        "markers", "real_sleep: keep the trial runner's real time.sleep (settle, recorder "
        "start, stop grace, manual hold), for tests that drive real processes")


class _TimeWithoutSleep:
    """The real time module, except that sleep returns at once."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(_seconds):
        return None


@pytest.fixture(autouse=True)
def _runner_sleeps_cost_nothing(request, monkeypatch):
    """The runner waits on fixed timers (settle_s, 1 s after the recorder starts,
    stop_grace_s, the manual hold) that only matter for real processes. Here they
    would cost about 165 s of real time per suite run, so the runner module gets a
    time whose sleep is a no-op. monotonic, time and the global time module stay
    real, so tests that measure timing, and other modules, keep real clocks.
    Mark a test (or module) real_sleep to keep the real waits."""
    if request.node.get_closest_marker("real_sleep"):
        return
    from robofacts import trial_runner
    monkeypatch.setattr(trial_runner, "time", _TimeWithoutSleep())

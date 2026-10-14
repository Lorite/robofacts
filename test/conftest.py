"""Test setup: import the package from this source tree when it is not installed,
and point the framework at the shipped example project unless the caller set a root."""
import os
import sys
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROJECT = PACKAGE_ROOT / "example_project"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
os.environ.setdefault("TRIALS_EXPERIMENTS_ROOT", str(EXAMPLE_PROJECT / "experiments"))
os.environ.setdefault("TRIALS_PROJECT_ROOT", str(EXAMPLE_PROJECT))


def pytest_configure(config):
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
    from declarative_robot_trials import trial_runner
    monkeypatch.setattr(trial_runner, "time", _TimeWithoutSleep())

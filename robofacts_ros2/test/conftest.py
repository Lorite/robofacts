"""Test setup of the ROS 2 package: import both packages from this source tree when they
are not installed, and point the framework at the shipped example project (which names the
ros2 backend in its trials.yaml) unless the caller set a root."""
import os
import sys
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PACKAGE_ROOT.parent / "robofacts"
EXAMPLE_PROJECT = PACKAGE_ROOT / "example_project"
for _root in (CORE_ROOT, PACKAGE_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
#: The roots the caller set before pytest started, which the fixture below keeps.
_CALLER_ROOTS = {name: os.environ.get(name) for name in ("TRIALS_EXPERIMENTS_ROOT", "TRIALS_PROJECT_ROOT")}


@pytest.fixture(autouse=True)
def _example_project_roots(monkeypatch):
    """The package's tests run against the shipped example project, unless the caller set a root.

    A fixture, not os.environ at import time (2026-10-15): pytest imports every conftest
    before it runs a test, so in a project that collects this suite next to its own tests
    (the lab repo's pytest.ini) the import-time variables pointed the project's tests at
    the example project too. Its lint then checked one experiment instead of all of them,
    and its settings tests failed, in the full run only."""
    defaults = {"TRIALS_EXPERIMENTS_ROOT": str(EXAMPLE_PROJECT / "experiments"),
                "TRIALS_PROJECT_ROOT": str(EXAMPLE_PROJECT)}
    for name, value in defaults.items():
        if not _CALLER_ROOTS[name]:
            monkeypatch.setenv(name, value)


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
    from robofacts import trial_runner
    monkeypatch.setattr(trial_runner, "time", _TimeWithoutSleep())


@pytest.fixture
def enable_adapters(monkeypatch):
    """Enable robot adapters for one test, as `adapters: [...]` in trials.yaml would."""
    from robofacts import adapters

    def enable(*names):
        monkeypatch.setattr(adapters, "_setting", lambda: list(names))
    return enable

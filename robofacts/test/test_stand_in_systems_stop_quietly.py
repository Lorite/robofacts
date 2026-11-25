"""The stand-in systems of a new project and of the example exit quietly on SIGINT.

The runner stops a stack with SIGINT. A stand-in that does not handle it prints a
KeyboardInterrupt traceback after every trial, which looks like a failure to a first user.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
SYSTEMS = [
    PACKAGE / "robofacts" / "skeleton" / "experiment" / "scripts" / "system.py",
    PACKAGE / "example_project" / "experiments" / "demo_log_recorder" / "scripts" / "system.py",
]


@pytest.mark.real_sleep
@pytest.mark.parametrize("script", SYSTEMS, ids=lambda p: p.parts[-4] + "/" + p.parts[-3])
def test_sigint_ends_the_stand_in_without_a_traceback(script, tmp_path):
    log = tmp_path / "stack.log"
    proc = subprocess.Popen([sys.executable, str(script), "--log", str(log)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (log.exists() and log.stat().st_size):
        time.sleep(0.05)
    assert log.exists() and log.stat().st_size, "the stand-in never wrote a line"
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=10)
    assert "Traceback" not in err and "KeyboardInterrupt" not in err, err
    assert proc.returncode == 0

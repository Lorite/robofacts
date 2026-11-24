"""Tests for the topic_value gate check (2026-11-04): a sensor stream is alive, not only flowing.

The case it was written for: the Crazyflie Flow deck's PMW3901 came up stuck after a boot
(motion.squal 0, motion.shutter frozen at 900) while /<cf>/flow_log streamed at 50 Hz, so
topic_rate passed a dead sensor. A stubbed ``ros2`` serves canned samples.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[1] / "robofacts" / "preflight" / "checks" / "topic_value.sh"
TOPIC = "/cf1/flow_log"


def log_samples(rows):
    """``ros2 topic echo`` output of LogDataGeneric messages with these values."""
    out = []
    for i, values in enumerate(rows):
        out.append(f"header:\n  stamp:\n    sec: {100 + i}\n    nanosec: 0\n  frame_id: world\n"
                   f"timestamp: {1000 + 20 * i}\nvalues:\n" + "".join(f"- {v}\n" for v in values) + "---")
    return "\n".join(out) + "\n"


def run(tmp_path: Path, echo: str, *argv, advertised: bool = True) -> subprocess.CompletedProcess:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    canned = tmp_path / "echo.txt"
    canned.write_text(echo)
    listing = TOPIC if advertised else "/other"
    stub = stub_bin / "ros2"
    stub.write_text("#!/usr/bin/env bash\n"
                    f'if [ "$1 $2" = "topic list" ]; then echo "{listing}"; exit 0; fi\n'
                    f'if [ "$1 $2" = "topic echo" ]; then cat "{canned}"; exit 0; fi\n'
                    "exit 1\n")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub_bin}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(CHECK), f"topic={TOPIC}", *argv],
                          capture_output=True, text=True, env=env, timeout=60)


HEALTHY = [[3, -2, 105 + i % 8, 3721 + 50 * i, 12, 31.2] for i in range(40)]
STUCK = [[0, 0, 0, 900, 12, 31.2] for _ in range(40)]


def test_a_healthy_flow_deck_passes_both_checks(tmp_path):
    squal = run(tmp_path, log_samples(HEALTHY), "field=values[2]", "min=50")
    assert squal.returncode == 0 and squal.stdout.startswith("PASS:"), squal.stdout
    assert "median" in squal.stdout and "40 samples" in squal.stdout
    shutter = run(tmp_path, log_samples(HEALTHY), "field=values[3]", "vary=true")
    assert shutter.returncode == 0, shutter.stdout


def test_the_stuck_pmw3901_of_2026_11_04_fails_both_checks(tmp_path):
    squal = run(tmp_path, log_samples(STUCK), "field=values[2]", "min=50")
    assert squal.returncode == 1 and "median 0 < min 50" in squal.stdout, squal.stdout
    shutter = run(tmp_path, log_samples(STUCK), "field=values[3]", "vary=true")
    assert shutter.returncode == 1 and "constant at 900" in shutter.stdout, shutter.stdout


def test_one_odd_sample_does_not_fail_a_healthy_median(tmp_path):
    rows = [list(r) for r in HEALTHY]
    rows[5][2] = 0
    assert run(tmp_path, log_samples(rows), "field=values[2]", "min=50").returncode == 0


def test_max_and_a_dotted_path(tmp_path):
    echo = "".join(f"pose:\n  position:\n    x: 0.0\n    y: 0.0\n    z: {0.01 * i}\n---\n" for i in range(10))
    assert run(tmp_path, echo, "field=pose.position.z", "max=0.2").returncode == 0
    high = run(tmp_path, echo, "field=pose.position.z", "max=0.01")
    assert high.returncode == 1 and "> max" in high.stdout, high.stdout


def test_too_few_samples_or_a_missing_field_fail(tmp_path):
    few = run(tmp_path, log_samples(HEALTHY[:3]), "field=values[2]", "min=50")
    assert few.returncode == 1 and "need 5" in few.stdout, few.stdout
    missing = run(tmp_path, log_samples(HEALTHY), "field=values[9]", "min=50")
    assert missing.returncode == 1 and "without a numeric" in missing.stdout, missing.stdout
    silent = run(tmp_path, "", "field=values[2]", "min=50")
    assert silent.returncode == 1


def test_absent_topic_fails_or_skips(tmp_path):
    assert run(tmp_path, "", "field=values[2]", "min=50", advertised=False).returncode == 1
    skipped = run(tmp_path, "", "field=values[2]", "min=50", "required=false", advertised=False)
    assert skipped.returncode == 3 and skipped.stdout.startswith("SKIP:"), skipped.stdout


@pytest.mark.parametrize("argv", [["field=values[2]"], ["field=values[2", "min=1"]])
def test_usage_errors(tmp_path, argv):
    result = run(tmp_path, log_samples(HEALTHY), *argv)
    assert result.returncode in (1, 2), result.stdout + result.stderr
    if argv == ["field=values[2]"]:
        assert result.returncode == 2
    else:
        assert "is not a path" in result.stdout

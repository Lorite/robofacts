"""The lab-specific names left the package: each has a neutral default here and a
project sets its own in trials.yaml (the robotics lab's values are pinned by the lab's
own test, experiments/common/scripts/test_lab_project_settings.py)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from robofacts import record_profiles as rp
from robofacts import roots, stop_latch_clear as slc
from robofacts.preflight import run_preflight as rpf


@pytest.fixture
def no_project(tmp_path, monkeypatch):
    """A working directory with no trials.yaml above it, and no root variables."""
    for var in (roots.ENV_EXPERIMENTS_ROOT, roots.ENV_PROJECT_ROOT, slc.ENV_STOP_LATCH):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def project(no_project):
    """A project whose trials.yaml sets every lab-style name."""
    (no_project / "trials.yaml").write_text(yaml.safe_dump({
        "experiments_root": "experiments", "project_root": ".",
        "stop_latch": "/tmp/lab_stop_latch",
        "record_profiles": {"cf_name": "lab_drone", "spot_name": "lab_spot", "camera": "/lab_cam",
                            "markerless_node": "/lab_estimator", "extpos_bridge": "/lab_bridge",
                            "watchdog": "/lab_watchdog"}}))
    return no_project


def test_the_neutral_defaults_without_a_project(no_project):
    assert roots.project_settings() == {}
    assert slc.latch_path() == Path("/tmp/trial_stop_latch")
    assert rp.names() == rp.DEFAULT_NAMES
    assert rp.default_cf_name() == "cf1"
    topics = rp.build_topics("markerless_light")
    assert "/cf1/pose" in topics and "/estimator/position" in topics and "/camera/color/camera_info" in topics
    assert any(t.startswith("/spot/") for t in rp.build_topics("spot_observer"))
    assert rp.SPOT_NAME == "spot" and rp.CAMERA == "/camera"


def test_a_project_sets_its_own_names(project):
    assert slc.latch_path() == Path("/tmp/lab_stop_latch")
    assert rp.default_cf_name() == "lab_drone"
    topics = rp.build_topics("markerless_light")
    assert "/lab_drone/pose" in topics and "/lab_estimator/position" in topics
    assert "/lab_cam/color/camera_info" in topics and "/lab_watchdog/health" in topics
    assert all(t.startswith("/lab_spot/") or t == "/mocap/poses"
               for t in rp.build_topics("livox_mapping_spot") if "spot" in t)
    assert rp.SPOT_NAME == "lab_spot"
    cmd = rp.build_record_command("position_only", "/tmp/x")
    assert "/lab_drone/pose" in cmd


def test_the_environment_beats_the_project_for_the_latch(project, monkeypatch):
    monkeypatch.setenv(slc.ENV_STOP_LATCH, "/tmp/env_latch")
    assert slc.latch_path() == Path("/tmp/env_latch")


def test_no_lab_name_and_no_legacy_variable_in_the_package():
    package = Path(rp.__file__).resolve().parent
    lab = ("lorite", "crazylori", "spot_BD_", "helix", "LORITE_")
    hits = []
    for path in package.rglob("*"):
        if path.suffix not in (".py", ".sh", ".md", ".html", ".yaml", ".json") or "__pycache__" in path.parts:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(word in line for word in lab):
                hits.append(f"{path.relative_to(package)}:{i}: {line.strip()[:80]}")
    assert not hits, "lab names in the package:\n" + "\n".join(hits)
    assert rpf.CONCURRENCY_ENV == "TRIALS_PREFLIGHT_CONCURRENCY"
    assert not hasattr(rpf, "LEGACY_CONCURRENCY_ENV")


def test_the_stop_latch_check_resolves_the_project_latch(project, tmp_path, monkeypatch):
    """The check passes no --latch when neither the entry nor the environment names one,
    so stop_latch_clear.py resolves the project's path: a fresh latch there is a NO-GO."""
    import subprocess, time
    latch = Path("/tmp/lab_stop_latch")
    if latch.exists():
        pytest.skip("a real /tmp/lab_stop_latch exists on this machine")
    env = {k: v for k, v in os.environ.items() if k != slc.ENV_STOP_LATCH}
    env["PF_LIB"] = str(rpf.LIB_SH)
    script = rpf.CHECKS_DIR / "stop_latch.sh"
    ok = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert ok.returncode == 0 and "age_s=none" in ok.stdout, ok.stdout
    latch.write_text("estop test\n")
    try:
        bad = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, cwd=tmp_path)
        assert bad.returncode == 1 and str(latch) in bad.stdout, bad.stdout
    finally:
        latch.unlink()

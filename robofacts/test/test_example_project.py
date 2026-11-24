"""The example project without ROS (robofacts/example_project) and its quickstart.

The example is the proof that the general package is complete on its own: a system that is
a process and a log file goes through the whole loop, from the plan to the coverage index.
The quickstart test runs real processes, on a copy, so the source tree stays clean.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from robofacts import backend, campaign_plan as cp, campaign_spec as cs, trial_index as ti

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"
EXPERIMENT = "demo_log_recorder"
CAMPAIGN = Path("experiments") / EXPERIMENT / "campaigns" / "grid.yaml"


def test_the_example_names_the_process_backend_and_its_shared_config_resolves():
    assert backend.configured_name(EXAMPLE) == "process"
    config = EXAMPLE / "experiments" / "common" / "config"
    for name in ("factors.yaml", "record_profiles.yaml", "robots.yaml", "storage.yaml",
                 "trial_metadata_core.yaml"):
        assert (config / name).is_file(), f"{name} of the example does not resolve"
        yaml.safe_load((config / name).read_text(encoding="utf-8"))


def test_the_example_campaign_is_a_grid_of_two_factors():
    campaign = cs.load_campaign(EXAMPLE / CAMPAIGN, EXAMPLE / "experiments", [])
    assert sorted(campaign.sweep) == ["duration", "rate"]
    plan = cp.build_plan(campaign, [], False)
    cells = [(item.cell.values["rate"], item.cell.values["duration"]) for item in cp.execution_order(plan)]
    assert sorted(cells) == [("fast", "long"), ("fast", "short"), ("slow", "long"), ("slow", "short")]


def test_the_example_passes_the_lint():
    out = subprocess.run([sys.executable, "-m", "robofacts.cli", "lint"], cwd=EXAMPLE,
                         capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(PACKAGE)})
    assert out.returncode == 0, out.stdout[-3000:]
    assert "failed" not in out.stdout


@pytest.mark.real_sleep
def test_the_quickstart_runs_the_example_from_plan_to_coverage_index(tmp_path):
    """What the CI job of the general package runs, on a copy of the example."""
    shutil.copytree(EXAMPLE, tmp_path / "example_project")     # the links become files
    shutil.copy(PACKAGE / "quickstart.sh", tmp_path / "quickstart.sh")
    # Run from the source tree: no `trials` of another install may answer instead.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "python3", "dirname", "env", "sh", "df", "awk", "sed", "grep", "cat", "rm",
                 "mkdir", "touch", "date", "id", "stat", "tr", "head", "tail", "cut", "sort", "wc",
                 "ls", "readlink", "basename", "sleep", "timeout", "pgrep", "ps", "kill", "uname",
                 "hostname", "git", "true", "false", "test", "printf", "echo", "tee", "find", "xargs"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    (bin_dir / "python3").unlink()
    (bin_dir / "python3").symlink_to(sys.executable)
    env = {"PATH": str(bin_dir), "PYTHONPATH": str(PACKAGE), "HOME": str(tmp_path), "TRIALS_VOICE": "off"}
    # quickstart.sh adds its own folder to PYTHONPATH when `trials` is not installed. Here
    # the package is next to the test, not next to the copy, so the variable carries it.
    out = subprocess.run(["bash", str(tmp_path / "quickstart.sh")], capture_output=True, text=True,
                         env=env, timeout=300)
    text = out.stdout + out.stderr
    assert out.returncode == 0, text[-4000:]
    assert "wanted 4 · covered 0 · STILL MISSING 4" in text
    assert "4 trial(s): 4 OK, 0 blocked, 0 failed" in text
    assert "wanted 4 · covered 4 · STILL MISSING 0" in text
    assert text.count("[verify] log_was_recorded rc=0") == 4

    experiments = tmp_path / "example_project" / "experiments"
    ti._recorder_at.cache_clear()
    trials = ti.load_trials(experiments, EXPERIMENT)
    assert len(trials) == 4 and all(t.status == "usable" for t in trials)
    assert {(t.metadata["rate"], t.metadata["duration"]) for t in trials} == {
        ("slow", "short"), ("slow", "long"), ("fast", "short"), ("fast", "long")}
    for trial in trials:
        lines = (trial.bag_dir / "data" / "system.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 3 and all(f"rate={trial.metadata['rate']}" in line for line in lines)
        assert trial.metadata["domain"] == "simulation"
    ti._recorder_at.cache_clear()
    from test_process_backend import leftover_processes
    assert leftover_processes(str(tmp_path) + ".*system.py") == ""

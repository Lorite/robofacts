"""`trials reverify` renders the corrected factor values (2026-11-25).

The index counts an amended trial in its corrected cell (``corrections.json``, written by
``trials amend``). The re-verify took the factor values from the ledger and ignored the
corrections, so a trial amended from ``slow`` to ``fast`` was re-verified with
``--rate slow``, and a pass then counted it in the ``fast`` cell.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from robofacts import trial_decisions as td
from robofacts import trial_index as ti
from robofacts import trial_reverify as rv

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"
EXPERIMENT = "demo_log_recorder"
LATER = time.time() + 3600


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="PASS\n", stderr="")


def test_an_amended_trial_is_reverified_with_its_corrected_level(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    shutil.copytree(EXAMPLE, root)
    experiments = root / "experiments"
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(experiments))
    ti._recorder_at.cache_clear()
    exp = experiments / EXPERIMENT
    name = "20261125T194733_rep01"
    bag = exp / "recordings" / name
    (bag / "data").mkdir(parents=True)
    (bag / "data" / "system.log").write_text("tick 0 rate=fast\n", encoding="utf-8")
    (bag / "recording.json").write_text("{}", encoding="utf-8")
    (bag / "trial_metadata.json").write_text(json.dumps(
        {"trial_id": name, "experiment": EXPERIMENT, "rate": "slow", "duration": "short",
         "aborted": False, "non_default_factors": []}), encoding="utf-8")
    (exp / "runs").mkdir(exist_ok=True)
    (exp / "runs" / "attempts.jsonl").write_text(json.dumps(
        {"trial_label": name, "campaign": "grid", "metadata_written": True,
         "values": {"rate": "slow", "duration": "short", "execution_host": "laptop"}}) + "\n",
        encoding="utf-8")

    td.amend(experiments, EXPERIMENT, name, ["rate=fast"], reason="the log shows the fast rate",
             by="tester", now=LATER)
    (trial,) = ti.load_trials(experiments, EXPERIMENT)
    assert trial.metadata["rate"] == "fast"              # the index counts it as fast

    run = FakeRun()
    record = rv.reverify(experiments, EXPERIMENT, name, reason="rule fixed", by="tester",
                         now=LATER + 600, run=run, check_ros=False, project_root=root)
    (argv,) = run.calls
    assert argv[argv.index("--rate") + 1] == "fast"
    assert "--rate fast" in record["steps"][0]["command"]
    ti._recorder_at.cache_clear()


FACTORS = """
version: 1
factors:
  airframe:
    levels: [bare, patched]
    default: bare
    metadata_key: airframe
    maps_to: {kind: launch_arg, name: rigid_body, level_values: {bare: cf_bare, patched: cf_patched}}
  route:
    levels: [a, b]
    default: a
    metadata_key: route
    maps_to: {kind: operator}
"""


def test_a_launch_argument_that_the_corrected_level_drives_follows_the_correction(tmp_path):
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(FACTORS, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    values, args = rv.corrected_inputs(
        tmp_path, "expA", {"airframe": "bare", "route": "a"},
        {"rigid_body": "cf_bare", "world": "lab"},
        [{"field": "airframe", "factor": "airframe", "recorded": "bare", "corrected": "patched"}])
    assert values == {"airframe": "patched", "route": "a"}
    assert args == {"rigid_body": "cf_patched", "world": "lab"}
    # Without corrections nothing changes.
    assert rv.corrected_inputs(tmp_path, "expA", {"route": "a"}, {"x": 1}, []) == ({"route": "a"}, {"x": 1})

"""`trials metadata` audits the trials that the index reads (2026-11-25).

The audit read only the full recordings, while ``trial_index.load_trials`` also reads a
slim copy whose full recording is not on this machine (the laptop view). With only slim
copies present, the index, the plan and the records counted the trials and the audit said
"No recorded trials found." It now audits the set that load_trials reads.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from robofacts import trial_index as ti
from robofacts import trial_metadata_schema as tms

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"
EXPERIMENT = "demo_log_recorder"


def _trial(directory: Path, rate: str) -> None:
    directory.mkdir(parents=True)
    (directory / "trial_metadata.json").write_text(json.dumps(
        {"trial_id": directory.name.removesuffix("_slim"), "experiment": EXPERIMENT,
         "rate": rate, "duration": "short"}), encoding="utf-8")


def test_the_audit_reads_the_trials_the_index_reads(tmp_path, capsys):
    shutil.copytree(EXAMPLE, tmp_path / "proj")
    experiments = tmp_path / "proj" / "experiments"
    exp = experiments / EXPERIMENT
    _trial(exp / "recordings" / "t1", "slow")                 # full, with a slim copy
    _trial(exp / "recordings_slim" / "t1_slim", "slow")       # a copy: not a second trial
    _trial(exp / "recordings_slim" / "t2_slim", "fast")       # slim only (the laptop view)
    _trial(exp / "recordings_slim" / "t3_slim", "fast")
    (exp / "recordings_slim" / "t4_slim").mkdir()
    (exp / "recordings_slim" / "t4_slim" / "trial_metadata.json").write_text("{", encoding="utf-8")

    indexed = {str(t.bag_dir) for t in ti.load_trials(experiments, EXPERIMENT)}
    assert len(indexed) == 3
    results = tms.audit(experiments, EXPERIMENT)
    assert {k for k, v in results.items() if not (v and v[0].startswith("unreadable"))} == indexed
    assert results[str(exp / "recordings_slim" / "t4_slim")][0].startswith("unreadable")

    assert tms.main(["--experiments-root", str(experiments), "--experiment", EXPERIMENT]) == 0
    out = capsys.readouterr().out
    assert "No recorded trials found" not in out
    assert "trials satisfy the contract" in out


def test_with_only_slim_copies_the_audit_still_finds_the_trials(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "proj")
    experiments = tmp_path / "proj" / "experiments"
    for name in ("a", "b"):
        _trial(experiments / EXPERIMENT / "recordings_slim" / f"{name}_slim", "slow")
    assert len(tms.audit(experiments, EXPERIMENT)) == 2


def test_a_broken_slim_copy_beside_a_good_recording_is_not_a_second_trial(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "proj")
    experiments = tmp_path / "proj" / "experiments"
    exp = experiments / EXPERIMENT
    _trial(exp / "recordings" / "t1", "slow")
    (exp / "recordings_slim" / "t1_slim").mkdir(parents=True)
    (exp / "recordings_slim" / "t1_slim" / "trial_metadata.json").write_text("{", encoding="utf-8")
    assert list(tms.audit(experiments, EXPERIMENT)) == [str(exp / "recordings" / "t1")]

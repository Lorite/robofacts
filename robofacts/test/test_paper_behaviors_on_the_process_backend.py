"""The behaviors that the SAC 2027 paper names, on the process backend, with real processes.

The paper says that the gate blocks a trial and leaves no recording, that an override is
stored with its reason, that a failed verify step aborts the trial, that a trial without a
recording gets no metadata, and that the coverage index counts only usable trials. Before
2026-11-25 these behaviors were tested mostly through ROS 2 fixtures, in the other package's
suite. These tests run them where only `robofacts` is installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import trial_decisions as td
from robofacts import trial_index as ti
from robofacts import trial_runner as tr

from test_process_backend import use_project, write_project

pytestmark = pytest.mark.real_sleep

EXPERIMENT = "plant"


def campaign_file(experiments: Path) -> Path:
    return experiments / EXPERIMENT / "campaigns" / "rates.yaml"


def add_gate_entry(experiments: Path, entry: dict) -> None:
    path = experiments / EXPERIMENT / "config" / "preflight_checklist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checklist"].append(entry)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run(experiments: Path, capsys, *extra: str):
    rc = tr.main([str(campaign_file(experiments)), "--experiments-root", str(experiments),
                  "--no-pause", "--execute", *extra])
    return rc, capsys.readouterr().out


def ledger(experiments: Path) -> list:
    path = experiments / EXPERIMENT / "runs" / tr.ATTEMPT_LEDGER_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def gate_log(experiments: Path) -> list:
    path = experiments / EXPERIMENT / "runs" / "preflight_gate.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def plan_counts(experiments: Path):
    campaign = cs.load_campaign(campaign_file(experiments), experiments, [])
    plan = cp.build_plan(campaign, ti.load_trials(experiments, EXPERIMENT), False)
    return plan.wanted, plan.covered


def recordings(experiments: Path) -> list:
    bags = experiments / EXPERIMENT / "recordings"
    return sorted(d for d in bags.iterdir() if d.is_dir()) if bags.is_dir() else []


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    yield experiments
    ti._recorder_at.cache_clear()


# --------------------------------------------------------------------------- #
# (a) The gate blocks, and nothing is recorded
# --------------------------------------------------------------------------- #
def test_a_failed_hard_check_blocks_the_trial_and_leaves_no_recording(project, capsys):
    add_gate_entry(project, {"text": "[HARD] The calibration file is present.", "check": "file_exists",
                             "args": {"path": "/no/such/calibration.yaml"}})
    rc, out = run(project, capsys, "--limit", "1")
    assert rc == 1
    assert "GATE NO-GO - not recording, the action does not run" in out
    assert "0 OK, 1 blocked" in out
    # No recording and no metadata: nothing for the index to count.
    assert not any(ti.recording_holds_data(d) for d in recordings(project))
    assert not list(project.glob(f"{EXPERIMENT}/recordings/*/trial_metadata.json"))
    assert ti.load_trials(project, EXPERIMENT) == []
    # The attempt is in the ledger and the verdict in the gate log, with the failed check.
    (attempt,) = ledger(project)
    assert attempt["status"] == "blocked" and attempt["metadata_written"] is False
    (verdict,) = gate_log(project)
    assert verdict["verdict"] == "NO-GO" and verdict["override"] is None
    assert [f["check"] for f in verdict["failed"]] == ["file_exists"]
    assert plan_counts(project) == (2, 0)


def test_a_blocked_trial_stops_the_campaign(project, capsys):
    """A stack that fails the gate usually fails it again, so the runner stops."""
    add_gate_entry(project, {"text": "[HARD] The calibration file is present.", "check": "file_exists",
                             "args": {"path": "/no/such/calibration.yaml"}})
    rc, out = run(project, capsys)
    assert rc == 1 and "1 trial(s): 0 OK, 1 blocked" in out
    assert len(ledger(project)) == 1


# --------------------------------------------------------------------------- #
# (b) An override is stored with its reason
# --------------------------------------------------------------------------- #
def test_an_override_runs_the_trial_and_stores_the_reason_and_the_failed_check(project, capsys):
    add_gate_entry(project, {"text": "The calibration file is present.", "check": "file_exists",
                             "args": {"path": "/no/such/calibration.yaml"}})
    reason = "calibration known good from this morning, file not synced yet"
    rc, out = run(project, capsys, "--limit", "1", "--gate-override", reason)
    assert rc == 0, out
    assert "PROCEEDING ANYWAY BY OPERATOR OVERRIDE" in out
    (trial,) = ti.load_trials(project, EXPERIMENT)
    preflight = trial.metadata["preflight"]
    assert preflight["overridden"] is True and preflight["override_reason"] == reason
    assert preflight["allowed"] is True and "file_exists" in " ".join(preflight["failed_checks"])
    (verdict,) = gate_log(project)
    assert verdict["override"] == reason
    # The trial counts: an override is visible, not a hidden pass.
    assert trial.status == "usable" and plan_counts(project) == (2, 1)


def test_an_override_does_not_pass_a_failed_hard_check(project, capsys):
    """The experiment template and the paper say that a text starting with [HARD] cannot be
    overridden and that a failed check never counts. Until 2026-11-26 the override passed a
    hard check too and the trial counted (review finding A2/C3-1, Alejandro's decision)."""
    add_gate_entry(project, {"text": "[HARD] The calibration file is present.", "check": "file_exists",
                             "args": {"path": "/no/such/calibration.yaml"}})
    rc, out = run(project, capsys, "--limit", "1", "--gate-override", "I am sure")
    assert rc == 1 and "0 OK, 1 blocked" in out, out
    assert "PROCEEDING ANYWAY" not in out
    assert "The override does not pass [HARD] file_exists" in out
    # Blocked like a NO-GO without the override: no recording, no metadata, nothing counts.
    assert ti.load_trials(project, EXPERIMENT) == []
    (verdict,) = gate_log(project)
    assert verdict["verdict"] == "NO-GO" and verdict["override"] is None
    assert [(f["check"], f["hard"]) for f in verdict["failed"]] == [("file_exists", True)]
    (attempt,) = ledger(project)
    assert attempt["status"] == "blocked" and "override does not pass" in attempt["detail"]
    assert plan_counts(project) == (2, 0)


# --------------------------------------------------------------------------- #
# (c) A failed verify step aborts the trial, and the index excludes it
# --------------------------------------------------------------------------- #
def test_a_failed_verify_step_aborts_the_trial_and_the_index_excludes_it(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root, min_lines=100000)
    use_project(root, monkeypatch)
    rc, out = run(experiments, capsys, "--limit", "1")
    assert rc == 1 and "a verify step rejected the recorded trial" in out
    (trial,) = ti.load_trials(experiments, EXPERIMENT)
    assert trial.has_bag_data, "the trial was recorded"
    assert trial.metadata["aborted"] is True
    assert trial.metadata["abort_reason"].startswith("verify step 'plant_log_recorded' failed")
    assert trial.status == "excluded"
    assert plan_counts(experiments) == (2, 0)
    (attempt,) = ledger(experiments)
    assert attempt["status"] == "failed" and attempt["aborted"] is True


# --------------------------------------------------------------------------- #
# (d) A trial without a recording gets no metadata
# --------------------------------------------------------------------------- #
def test_a_trial_without_a_recording_gets_no_metadata(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root, with_stack=False)   # nothing writes the log
    use_project(root, monkeypatch)
    rc, out = run(experiments, capsys, "--limit", "1")
    assert rc == 1 and "no recording recorded" in out and "holds no data" in out
    assert not list(experiments.glob(f"{EXPERIMENT}/recordings/*/trial_metadata.json"))
    assert ti.load_trials(experiments, EXPERIMENT) == []
    (attempt,) = ledger(experiments)
    assert attempt["status"] == "failed" and attempt["metadata_written"] is False


# --------------------------------------------------------------------------- #
# (e) The coverage index counts only usable trials
# --------------------------------------------------------------------------- #
def test_the_index_counts_only_usable_trials(project, capsys, monkeypatch):
    # A person's decision waits until a trial's files are quiet (td.WRITE_QUIET_S, 120 s).
    # These trials are seconds old, so the test does not wait.
    monkeypatch.setattr(td, "WRITE_QUIET_S", 0)
    rc, out = run(project, capsys)
    assert rc == 0 and "2 trial(s): 2 OK" in out
    assert plan_counts(project) == (2, 2)
    slow, fast = sorted(ti.load_trials(project, EXPERIMENT), key=lambda t: t.metadata["rate"] != "slow")

    # A person excludes one trial: it no longer counts, and its cell is missing again.
    assert td.main_exclude([EXPERIMENT, fast.bag_dir.name, "--experiments-root", str(project),
                            "--reason", "the log rotated during the trial", "--category", "infrastructure",
                            "--by", "tester"]) == 0
    by_rate = {t.metadata["rate"]: t for t in ti.load_trials(project, EXPERIMENT)}
    assert by_rate["fast"].status == "excluded" and by_rate["slow"].status == "usable"
    assert plan_counts(project) == (2, 1)

    # A trial whose metadata breaks the contract is unverified, and does not count either.
    meta_path = slow.bag_dir / "trial_metadata.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["rate"] = "medium"            # not a level of the contract
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    statuses = sorted(t.status for t in ti.load_trials(project, EXPERIMENT))
    assert statuses == ["excluded", "unverified"]
    assert plan_counts(project) == (2, 0)

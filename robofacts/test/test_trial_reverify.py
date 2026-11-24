"""Tests for trial_reverify: `trials reverify` (2026-11-04).

The rule it protects: a trial aborted by a verify rule that was later fixed can
count again, but only through a record beside the bag (``reverification.json``)
that says which current steps passed, and only when the runner's abort came from
a verify step alone. ``trial_metadata.json`` is never edited, an action failure
is never lifted, and a failing re-verify never excludes a trial by itself.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from robofacts import cli
from robofacts import trial_decisions as td
from robofacts import trial_index as ti
from robofacts import trial_records as trec
from robofacts import trial_reverify as tr

FACTORS = """
version: 1
factors:
  execution_host:
    levels: [laptop, orin]
    default: orin
    metadata_key: execution_host
    maps_to: {kind: host}
  route:
    levels: [block_loop, building_loop]
    default: block_loop
    metadata_key: route
    maps_to: {kind: operator}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  route: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
  abort_reason: {required: false, type: str}
"""

RUNNER = """
simulation: true
launch: {package: demo_pkg, file: demo.launch.py}
action: {command: "true"}
verify_steps:
  - name: rates
    command: python3 check_rates.py {bag} --route {route}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 1
sweep:
  route: [block_loop, building_loop]
fixed:
  execution_host: laptop
verify_steps:
  - name: drift
    command: python3 check_drift.py {bag} --host {execution_host}
"""

VERIFY_ABORT = "verify step 'rates' failed (rc=1) — the trial did not happen\nVERIFY-FAIL: 9 warnings"
LATER = time.time() + 3600


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(FACTORS, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER, encoding="utf-8")
    return tmp_path


def trial(root: Path, name: str, *, mcap_in_full: bool = True, ledger: bool = True,
          **metadata) -> Path:
    meta = {"trial_id": name, "execution_host": "laptop", "aborted": False, "route": "block_loop",
            "non_default_factors": []}
    meta.update(metadata)
    full = root / "expA" / "bags" / name
    slim = root / "expA" / "bags_slim" / f"{name}_slim"
    for d in (full, slim):
        d.mkdir(parents=True)
        (d / "trial_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if mcap_in_full:
        (full / f"{name}_0.mcap").write_bytes(b"mcap")
    (slim / f"{name}_slim_0.mcap").write_bytes(b"mcap")
    if ledger:
        runs = root / "expA" / "runs"
        runs.mkdir(exist_ok=True)
        line = {"trial_label": name, "campaign": "c", "metadata_written": True,
                "values": {"route": meta["route"], "execution_host": "laptop"}}
        with (runs / "attempts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    return full


class FakeRun:
    """subprocess.run stand-in: the return code per script name, every call kept."""

    def __init__(self, rcs=None):
        self.rcs = rcs or {}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        rc = self.rcs.get(argv[1], 0)
        return SimpleNamespace(returncode=rc, stdout=f"{argv[1]}: {'VERIFY-OK' if rc == 0 else 'VERIFY-FAIL'}\n",
                               stderr="")


def go(root, name, run, **kw):
    return tr.reverify(root, "expA", name, reason="the rates rule misfired without broadcasts",
                       by="tester", now=LATER, run=run, check_ros=False, project_root=root.parent, **kw)


def status(root, name):
    return next(t for t in ti.load_trials(root, "expA") if t.bag_dir.name == name)


@pytest.mark.ros2
def test_a_pass_lifts_a_verify_only_abort_and_never_edits_the_metadata(root):
    full = trial(root, "t1", aborted=True, abort_reason=VERIFY_ABORT)
    before = (full / "trial_metadata.json").read_text()
    assert status(root, "t1").status == "excluded"
    record = go(root, "t1", FakeRun())
    assert record["verify_ok"] and record["lifts_abort"]
    assert [s["name"] for s in record["steps"]] == ["rates", "drift"]
    for d in (full, root / "expA" / "bags_slim" / "t1_slim"):
        assert json.loads((d / td.REVERIFICATION_FILE).read_text())["lifts_abort"] is True
    assert (full / "trial_metadata.json").read_text() == before
    t = status(root, "t1")
    assert t.status == "usable" and t.reverified and t.recorded["aborted"] is True
    st, why = trec._status_of(t)
    assert st == "usable" and "re-verified" in why and "abort is lifted" in why


@pytest.mark.ros2
def test_every_step_runs_and_a_failure_changes_nothing(root):
    trial(root, "t2", aborted=True, abort_reason=VERIFY_ABORT)
    run = FakeRun({"check_rates.py": 1})
    record = go(root, "t2", run)
    assert len(run.calls) == 2                      # the second step ran after the first failed
    assert not record["verify_ok"] and not record["lifts_abort"]
    assert status(root, "t2").status == "excluded"
    trial(root, "t3")                               # a usable trial that fails today stays usable
    assert not go(root, "t3", FakeRun({"check_drift.py": 1}))["verify_ok"]
    assert status(root, "t3").status == "usable"


@pytest.mark.ros2
def test_an_action_failure_is_never_lifted(root):
    trial(root, "t4", aborted=True, abort_reason="action returned rc=1 | " + VERIFY_ABORT)
    record = go(root, "t4", FakeRun())
    assert record["verify_ok"] and not record["lifts_abort"]
    assert status(root, "t4").status == "excluded"


@pytest.mark.ros2
def test_a_record_written_against_another_abort_reason_does_not_lift(root):
    full = trial(root, "t5", aborted=True, abort_reason=VERIFY_ABORT)
    go(root, "t5", FakeRun())
    meta = json.loads((full / "trial_metadata.json").read_text())
    meta["abort_reason"] = "verify step 'drift' failed (rc=1) — a later run of the runner"
    (full / "trial_metadata.json").write_text(json.dumps(meta))
    assert status(root, "t5").status == "excluded"


@pytest.mark.ros2
def test_steps_are_rendered_as_the_runner_renders_them(root):
    full = trial(root, "t6", route="building_loop", aborted=True, abort_reason=VERIFY_ABORT)
    run = FakeRun()
    go(root, "t6", run)
    argv = [c[0] for c in run.calls]
    assert argv[0] == ["python3", "check_rates.py", str(full.resolve()), "--route", "building_loop"]
    assert argv[1] == ["python3", "check_drift.py", str(full.resolve()), "--host", "laptop"]
    assert run.calls[0][1]["cwd"] == str(root.parent)


@pytest.mark.ros2
def test_a_staging_copy_without_the_mcap_reads_the_slim_copy(root):
    trial(root, "t7", mcap_in_full=False, aborted=True, abort_reason=VERIFY_ABORT)
    run = FakeRun()
    record = go(root, "t7", run)
    assert record["bag_read"] == "bags_slim/t7_slim"
    assert run.calls[0][0][2].endswith("bags_slim/t7_slim")


@pytest.mark.ros2
def test_dry_run_writes_nothing_and_a_rerun_keeps_the_history(root):
    full = trial(root, "t8", aborted=True, abort_reason=VERIFY_ABORT)
    run = FakeRun()
    assert go(root, "t8", run, dry_run=True)["dry_run"] and not run.calls
    assert not (full / td.REVERIFICATION_FILE).exists()
    go(root, "t8", FakeRun({"check_rates.py": 1}))
    second = go(root, "t8", FakeRun())
    assert second["lifts_abort"] and second["previous"][0]["verify_ok"] is False


def test_refusals(root):
    trial(root, "t9", ledger=False, aborted=True, abort_reason=VERIFY_ABORT)
    with pytest.raises(td.DecisionError, match="campaign"):
        go(root, "t9", FakeRun())
    with pytest.raises(td.DecisionError, match="values"):
        go(root, "t9", FakeRun(), campaign="c")
    with pytest.raises(td.DecisionError, match="reason"):
        tr.reverify(root, "expA", "t9", reason=" ", now=LATER, check_ros=False)


def test_the_command_is_registered():
    assert "reverify" in cli.COMMANDS

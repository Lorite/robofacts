"""The abort reason of a verify failure says the trial ran but does not count (2026-11-04).

A verify step runs on a recorded bag after an action that returned 0, so the robot usually did
fly: the Flow-only square of that day landed 0.67 m from its takeoff point and its failure report
said "the trial did not happen". The reason must still START with "verify step", because
``trial_decisions.abort_is_verify_only`` (``trials reverify``) reads that prefix.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from robofacts import campaign_spec as cs
from robofacts import trial_decisions as td
from robofacts import trial_runner as tr

CORE = """
version: 1
factors:
  execution_host:
    levels: [laptop]
    default: laptop
    metadata_key: execution_host
    maps_to: {kind: host}
"""
CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  aborted: {required: true, type: bool}
"""
RUNNER = """
record_topics: [/rosout]
launch: {package: demo_pkg, file: demo.launch.py}
action: {command: "python3 fly.py"}
verify_steps:
  - name: landed_near_takeoff
    command: python3 verify_landing.py {bag}
hosts:
  laptop: {local: true}
"""


def test_a_verify_failure_says_the_trial_ran_but_does_not_count(tmp_path: Path):
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE)
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT)
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER)
    factors = cs.load_factors("expA", tmp_path)
    config = tr.RunnerConfig.load("expA", tmp_path)
    trial = cs.TrialSpec("expA", {"execution_host": "laptop"}, [], 1)

    def start(cmd, *a, **k):
        if "record" in cmd and "-o" in cmd:
            bag = Path(cmd[cmd.index("-o") + 1])
            bag.mkdir(parents=True, exist_ok=True)
            (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n")
            (bag / f"{bag.name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")
        return object()

    def foreground(cmd, *a, **k):
        rc = 1 if "verify_landing.py" in cmd else 0
        return SimpleNamespace(returncode=rc, stdout="VERIFY-FAIL: landed 0.67 m from takeoff\n")

    hooks = tr.ProcessHooks(start_background=start, run_foreground=foreground,
                            stop_process=lambda *a, **k: None)
    result = tr.run_trial(trial, factors, config, tmp_path / "expA" / "bags", dry_run=False,
                          hooks=hooks, slim=False)
    import json
    meta = json.loads((result.bag_dir / "trial_metadata.json").read_text())
    reason = meta["abort_reason"]
    assert meta["aborted"] is True
    assert "did not happen" not in reason
    assert "ran and was recorded, but it does not count" in reason
    assert td.abort_is_verify_only(reason)

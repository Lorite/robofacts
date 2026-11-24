"""Runner: the run log covers the verify steps, the optional action timeout (2026-10-02).

Found by the lab-day crazyswarm2 sim test and by E2. No ROS needed.
"""
from __future__ import annotations

import sys
from pathlib import Path



def test_the_verify_output_lands_in_the_run_log(tmp_path):
    """2026-10-02: the run log was closed before the verify steps ran."""
    import sys as _sys
    from robofacts import trial_runner as _tr
    from robofacts import campaign_spec as _cs
    from types import SimpleNamespace as _NS
    root = tmp_path
    (root / "common" / "config").mkdir(parents=True)
    (root / "common" / "config" / "factors.yaml").write_text(
        "version: 1\nfactors:\n  execution_host: {levels: [laptop], default: laptop, "
        "metadata_key: execution_host, maps_to: {kind: host}}\n")
    (root / "common" / "config" / "trial_metadata_core.yaml").write_text(
        "version: 1\nfields:\n  trial_id: {required: true, type: str}\n")
    (root / "expV" / "config").mkdir(parents=True)
    (root / "expV" / "config" / "runner.yaml").write_text(
        "launch: {package: p, file: f.launch.py}\nsettle_s: 0\nstop_grace_s: 0\n"
        "record_topics: [/x]\naction: {command: fly}\n"
        "verify_steps:\n  - {name: v, command: \"check {bag}\"}\nhosts: {laptop: {local: true}}\n")

    def start(cmd, target, *, cwd, label="", quiet_sink=None):
        if "record" in cmd:
            bag = Path(cmd[cmd.index("-o") + 1])
            bag.mkdir(parents=True)
            (bag / "metadata.yaml").write_text("x")
            (bag / "b_0.mcap").write_bytes(b"\x89MCAP0\r\n")
        return object()

    def fg(cmd, target, *, cwd, check=True):
        if cmd[0] == "check":
            print("AGENT 0 /px4_0/fmu: OK: verified")
        return _NS(returncode=0, stdout="")
    hooks = _tr.ProcessHooks(start_background=start, run_foreground=fg,
                             stop_process=lambda *a, **k: None)
    factors = _cs.load_factors("expV", root)
    config = _tr.RunnerConfig.load("expV", root)
    trial = _cs.TrialSpec("expV", {"execution_host": "laptop"}, [], 1)
    saved = _sys.stdout
    result = _tr.run_trial(trial, factors, config, root / "expV" / "bags", dry_run=False,
                           hooks=hooks, slim=False, pause_before_action=False)
    assert _sys.stdout is saved, "stdout must be restored after the trial"
    log = (root / "expV" / "runs" / f"{result.bag_dir.name}.log").read_text()
    assert "AGENT 0 /px4_0/fmu: OK" in log and "[verify] v" in log


def _timeout_root(tmp_path, extra):
    root = tmp_path
    (root / "common" / "config").mkdir(parents=True, exist_ok=True)
    (root / "common" / "config" / "factors.yaml").write_text(
        "version: 1\nfactors:\n  execution_host: {levels: [laptop], default: laptop, "
        "metadata_key: execution_host, maps_to: {kind: host}}\n")
    (root / "common" / "config" / "trial_metadata_core.yaml").write_text(
        "version: 1\nfields:\n  trial_id: {required: true, type: str}\n")
    (root / "expT" / "config").mkdir(parents=True, exist_ok=True)
    (root / "expT" / "config" / "runner.yaml").write_text(
        "launch: {package: p, file: f.launch.py}\nsettle_s: 0\nstop_grace_s: 0\n"
        "record_topics: [/x]\naction: {command: 'sleep 20'}\nhosts: {laptop: {local: true}}\n"
        f"project_root: {root}\n" + extra)
    return root


def test_an_action_past_its_timeout_is_terminated_and_aborted(tmp_path):
    """2026-10-02: an action blocked on a dead crazyswarm2 server ran forever."""
    import time as _time
    from robofacts import trial_runner as _tr
    from robofacts import campaign_spec as _cs
    from robofacts import remote_exec_utils as _rx
    import json as _json
    root = _timeout_root(tmp_path, "action_timeout_s: 1.5\n")

    def start(cmd, target, *, cwd, label="", quiet_sink=None):
        if "record" in cmd:
            bag = Path(cmd[cmd.index("-o") + 1])
            bag.mkdir(parents=True)
            (bag / "metadata.yaml").write_text("x")
            (bag / "b_0.mcap").write_bytes(b"\x89MCAP0\r\n")
        return object()
    hooks = _tr.ProcessHooks(start_background=start, run_foreground=_rx.run_foreground,
                             stop_process=lambda *a, **k: None)
    config = _tr.RunnerConfig.load("expT", root)
    assert config.action_timeout_s == 1.5
    trial = _cs.TrialSpec("expT", {"execution_host": "laptop"}, [], 1)
    t0 = _time.monotonic()
    result = _tr.run_trial(trial, _cs.load_factors("expT", root), config, root / "expT" / "bags",
                           dry_run=False, hooks=hooks, slim=False, pause_before_action=False)
    assert _time.monotonic() - t0 < 10, "the 20 s action must have been cut at 1.5 s"
    assert result.status == "failed" and "action_timeout_s=2 expired" in result.detail
    meta = _json.loads((result.bag_dir / "trial_metadata.json").read_text())
    assert meta["aborted"] is True


def test_no_action_timeout_by_default_and_bad_values_are_refused(tmp_path):
    from robofacts import trial_runner as _tr
    import pytest as _pytest
    assert _tr.RunnerConfig.load("expT", _timeout_root(tmp_path, "")).action_timeout_s is None
    with _pytest.raises(_tr.RunnerError, match="positive number"):
        _tr.RunnerConfig.load("expT", _timeout_root(tmp_path, "action_timeout_s: 0\n"))

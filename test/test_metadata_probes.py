#!/usr/bin/env python3
"""Tests for runner.yaml ``metadata_probes`` (SAC 2027 framework paper, 2026-10-01).

Provenance an experiment declares about its own apparatus (the PX4 checkout,
the Gazebo release) lands in trial_metadata.json under the declared key, is
never fatal, and leaves the key absent rather than blank when the probe fails.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


from declarative_robot_trials import trial_runner as tr
RUNNER = """\
launch: {package: p, file: f.launch.py}
metadata_probes:
  - {key: px4_version, command: "echo v1.17.0-alpha1-350-gabc"}
  - {key: gz_version, command: "printf 'Gazebo Sim, version 8.11.0\\\\nCopyright\\\\n'"}
"""


def _root(tmp_path: Path, runner: str) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(runner, encoding="utf-8")
    return tmp_path


def test_probes_load_from_runner_yaml(tmp_path):
    config = tr.RunnerConfig.load("expA", _root(tmp_path, RUNNER))
    assert [p["key"] for p in config.metadata_probes] == ["px4_version", "gz_version"]


def test_a_probe_needs_key_and_command(tmp_path):
    bad = "launch: {package: p, file: f}\nmetadata_probes:\n  - {key: x}\n"
    with pytest.raises(tr.RunnerError, match="needs a non-empty 'key' and 'command'"):
        tr.RunnerConfig.load("expA", _root(tmp_path, bad))


def test_underscore_keys_are_refused(tmp_path):
    """The contract validator ignores _keys, so a probe there would be invisible."""
    bad = "launch: {package: p, file: f}\nmetadata_probes:\n  - {key: _x, command: echo}\n"
    with pytest.raises(tr.RunnerError, match="may not start with '_'"):
        tr.RunnerConfig.load("expA", _root(tmp_path, bad))


def _local_hooks():
    def run_foreground(cmd, target, *, cwd, check=True):
        return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    return tr.ProcessHooks(start_background=None, run_foreground=run_foreground,
                           stop_process=None)


def test_first_stdout_line_lands_under_the_key(tmp_path):
    config = tr.RunnerConfig.load("expA", _root(tmp_path, RUNNER))
    out = tr.run_metadata_probes(config.metadata_probes, _local_hooks(), None, tmp_path)
    assert out == {"px4_version": "v1.17.0-alpha1-350-gabc",
                   "gz_version": "Gazebo Sim, version 8.11.0"}


def test_a_failing_or_silent_probe_leaves_its_key_absent(tmp_path):
    probes = [{"key": "a", "command": "exit 3"},
              {"key": "b", "command": "true"},
              {"key": "c", "command": "echo ok"}]
    out = tr.run_metadata_probes(probes, _local_hooks(), None, tmp_path)
    assert out == {"c": "ok"}


def test_a_raising_hook_is_not_fatal(tmp_path):
    def boom(cmd, target, *, cwd, check=True):
        raise OSError("ssh is down")
    hooks = tr.ProcessHooks(start_background=None, run_foreground=boom, stop_process=None)
    assert tr.run_metadata_probes([{"key": "a", "command": "echo"}], hooks, None, tmp_path) == {}


def test_a_json_probe_lands_as_a_list_and_bad_json_leaves_it_absent(tmp_path):
    """E2's `agents` list (2026-10-02): `json: true` keeps the structure."""
    import yaml
    runner = yaml.safe_dump({"launch": {"package": "p", "file": "f"}, "metadata_probes": [
        {"key": "agents", "command": "echo '[{\"instance\": 0}]'", "json": True},
        {"key": "broken", "command": "echo not-json", "json": True},
        {"key": "plain", "command": "echo 3"}]})
    config = tr.RunnerConfig.load("expA", _root(tmp_path, runner))
    out = tr.run_metadata_probes(config.metadata_probes, _local_hooks(), None, tmp_path)
    assert out == {"agents": [{"instance": 0}], "plain": "3"}


def test_json_must_be_a_boolean(tmp_path):
    bad = "launch: {package: p, file: f}\nmetadata_probes:\n  - {key: a, command: echo, json: 'yes'}\n"
    with pytest.raises(tr.RunnerError, match="json must be true or false"):
        tr.RunnerConfig.load("expA", _root(tmp_path, bad))

"""trial_metadata.json and recording.json are written whole or not at all (the review of
2026-11-25, round 2, B2-5).

On a 3 MB tmpfs a verify step filled the disk. The runner left a 0-byte trial_metadata.json
beside a finalized recording, ended the campaign with a traceback and wrote no ledger line,
so no tool showed the trial, and the empty file kept the runner from ever repairing it.
"""
from __future__ import annotations

import errno
import json
import os
import pathlib
import signal
import subprocess
import types

import pytest

from robofacts import remote_exec_utils as rx, trial_index as ti, trial_runner as tr
from robofacts.backends import process as pb, process_recorder

from test_process_backend import _run, use_project, write_project


def _disk_fills_half_way(monkeypatch, name: str) -> None:
    """Path.write_text of a file whose name starts with ``name`` writes half of its text,
    then fails as a full disk does."""
    real = pathlib.Path.write_text

    def write_text(self, data, *args, **kwargs):
        if self.name.startswith(name):
            real(self, data[: len(data) // 2], *args, **kwargs)
            raise OSError(errno.ENOSPC, "No space left on device", str(self))
        return real(self, data, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, "write_text", write_text)


@pytest.mark.real_sleep
def test_a_full_disk_at_the_metadata_fails_the_trial_on_record(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    _disk_fills_half_way(monkeypatch, "trial_metadata.json")
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert rc == 1 and "trial_metadata.json could not be written" in out, out
    (recording,) = [p for p in (experiments / "plant" / "recordings").iterdir() if p.is_dir()]
    assert not [p.name for p in recording.iterdir() if p.name.startswith("trial_metadata.json")]
    (row,) = [json.loads(line) for line in (experiments / "plant" / "runs" / tr.ATTEMPT_LEDGER_NAME)
              .read_text(encoding="utf-8").splitlines()]
    assert row["trial_label"] == recording.name and row["status"] == "failed"
    assert row["metadata_written"] is False and "No space left on device" in row["detail"]
    ti._recorder_at.cache_clear()


def test_a_write_on_the_rig_that_fails_half_way_leaves_no_file(tmp_path, monkeypatch):
    """The remote write goes through a shell: a base64 that dies after half its output."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "base64").write_text("#!/bin/sh\nprintf '{\"half\": '\nexit 1\n", encoding="utf-8")
    (stub / "base64").chmod(0o755)
    env = dict(os.environ, PATH=f"{stub}{os.pathsep}{os.environ['PATH']}")

    def rig(target, script, timeout=None, env=None):
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)

    target = types.SimpleNamespace(host="rig")
    path = tmp_path / "recording" / "trial_metadata.json"
    monkeypatch.setattr(rx, "_remote_sh", lambda t, script, timeout=None: rig(t, script, env=env))
    with pytest.raises(RuntimeError, match="could not write"):
        rx.write_trial_file(target, path, json.dumps({"trial_id": "t1"}))
    assert list(path.parent.iterdir()) == []
    monkeypatch.setattr(rx, "_remote_sh", lambda t, script, timeout=None: rig(t, script))
    rx.write_trial_file(target, path, json.dumps({"trial_id": "t1"}))
    assert json.loads(path.read_text(encoding="utf-8")) == {"trial_id": "t1"}
    assert [p.name for p in path.parent.iterdir()] == ["trial_metadata.json"]


def test_a_recorder_that_cannot_finish_its_manifest_leaves_the_recording_unfinalized(
        tmp_path, monkeypatch):
    source = tmp_path / "plant.log"
    source.write_text("", encoding="utf-8")
    out = tmp_path / "recording"
    handlers = {}

    def sleep(_seconds):
        # One tick of the plant, then the runner's SIGINT.
        with source.open("a", encoding="utf-8") as log:
            log.write("tick 0\n")
        handlers[signal.SIGINT](signal.SIGINT, None)
    monkeypatch.setattr(process_recorder, "signal", types.SimpleNamespace(
        signal=lambda signum, handler: handlers.__setitem__(signum, handler),
        SIGINT=signal.SIGINT, SIGTERM=signal.SIGTERM))
    monkeypatch.setattr(process_recorder, "time", types.SimpleNamespace(sleep=sleep))
    _disk_fills_half_way(monkeypatch, "recording.json")
    with pytest.raises(OSError):
        process_recorder.record(out, [str(source)])
    assert (out / "data" / "plant.log").read_text(encoding="utf-8") == "tick 0\n"
    assert not [p.name for p in out.iterdir() if p.name.startswith("recording.json")]
    assert pb.ProcessRecorder().unsettled_reason(out) is not None

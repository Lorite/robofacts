"""An unmounted disk must not turn covered cells into missing ones (the review of
2026-11-25, round 2, B2-8).

After a run with --bags-root the plan said "covered 1". With the directory moved away it
said "STILL MISSING 1", and --execute would have recorded the cell again, while the help
text said that coverage was unaffected.
"""
from __future__ import annotations

import os

import pytest

from robofacts import backend, cli, trial_index as ti

from test_process_backend import _run, use_project, write_project


@pytest.mark.real_sleep
def test_an_unmounted_recordings_root_stops_the_plan_and_the_run(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    disk = tmp_path / "disk"
    disk.mkdir()
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1", "--bags-root", str(disk))
    assert rc == 0, out
    campaign = str(experiments / "plant" / "campaigns" / "rates.yaml")
    plan = ["plan", campaign, "--experiments-root", str(experiments)]
    assert cli.main(plan) == 0 and "covered 1" in capsys.readouterr().out

    disk.rename(tmp_path / "unplugged")
    assert cli.main(plan) == 2
    assert f"the recordings root {disk} is not mounted" in capsys.readouterr().err
    run = ["run", campaign, "--experiments-root", str(experiments), "--no-pause"]
    for argv in (run + ["--execute"], run + ["--execute", "--bags-root", str(disk)], run):
        assert cli.main(argv) == 2, argv
        assert "is not mounted" in capsys.readouterr().err
    assert len(list((experiments / "plant" / "recordings").iterdir())) == 1, "nothing was recorded"

    (tmp_path / "unplugged").rename(disk)
    assert cli.main(plan) == 0 and "covered 1" in capsys.readouterr().out
    ti._recorder_at.cache_clear()


def test_a_slim_copy_or_a_deleted_recording_is_not_an_unmounted_disk(tmp_path):
    experiments = write_project(tmp_path / "proj")
    rd = backend.record_dirs(experiments)
    recordings = experiments / "plant" / rd["recordings"]
    gone = tmp_path / "disk" / "plant" / rd["recordings"]
    os.symlink(gone / "t1", recordings / "t1")
    assert list(ti.unmounted_recordings(experiments, "plant")) == [tmp_path / "disk"]
    # Its slim copy keeps the trial in the coverage.
    slim = experiments / "plant" / rd["slim"] / "t1_slim"
    slim.mkdir(parents=True)
    (slim / "trial_metadata.json").write_text("{}", encoding="utf-8")
    assert ti.unmounted_recordings(experiments, "plant") == {}
    # A recording deleted on a disk that is mounted is not a missing disk.
    gone.mkdir(parents=True)
    os.symlink(gone / "t2", recordings / "t2")
    assert ti.unmounted_recordings(experiments, "plant") == {}
    ti.require_recordings_mounted(experiments, "plant")


def test_the_web_interface_and_the_mcp_server_show_no_plan_while_the_disk_is_away(
        tmp_path, monkeypatch):
    """The campaign page, /api/plan, /run and the MCP server's plan computed coverage without
    the recordings of the missing disk (merge of round 2)."""
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    rd = backend.record_dirs(experiments)
    os.symlink(tmp_path / "disk" / "plant" / rd["recordings"] / "t1",
               experiments / "plant" / rd["recordings"] / "t1")
    from robofacts.mcp import queries
    with pytest.raises(queries.ToolInputError, match="is not mounted"):
        queries.plan_campaign("plant", "rates")
    try:
        import flask  # noqa: F401
    except ImportError:
        pytest.skip("the web interface needs Flask")
    from robofacts.ui.app import create_app
    client = create_app(experiments, None).test_client()
    page = client.get("/e/plant/c/rates")
    assert page.status_code == 409 and b"is not mounted" in page.data
    api = client.get("/api/plan/plant/rates.json")
    assert api.status_code == 409 and "is not mounted" in api.get_json()["error"]
    run = client.post("/run", data={"experiment": "plant", "campaign": "rates", "go": "dry"})
    assert run.status_code == 409 and b"is not mounted" in run.data

"""The web interface of the general package, on a project without ROS.

The interface is general: the plan, the coverage grid, the trials, the gate confirmations
and the journal need no middleware. A backend may add pages and controls through its
interface extension (the ROS 2 backend adds Foxglove replay, its layouts, the bag download,
the clean slate and Explore). These tests start the interface on the process backend's
example and check that the general pages work and that none of those additions appear.
"""
from __future__ import annotations

import base64
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from robofacts import cli

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"

def test_ui_help_needs_no_flask(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "flask", None)  # `import flask` now raises ImportError
    monkeypatch.delitem(sys.modules, "robofacts.ui.app", raising=False)
    import robofacts.ui as ui_package
    monkeypatch.delattr(ui_package, "app", raising=False)
    assert cli.main(["ui", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--no-auth" in out

def test_ui_without_flask_says_so_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "flask", None)
    monkeypatch.delitem(sys.modules, "robofacts.ui.app", raising=False)
    # The package keeps the submodule as an attribute once any test imported it, and
    # `from .ui import app` returns that attribute without importing. The test then
    # started the real server and the whole suite hung on port 8080 (2026-10-14).
    import robofacts.ui as ui_package
    monkeypatch.delattr(ui_package, "app", raising=False)
    assert cli.main(["ui", "--no-auth"]) == 2
    assert "needs Flask" in capsys.readouterr().err



@pytest.fixture
def client(tmp_path, monkeypatch):
    """The interface on a copy of the example, with one recorded trial in it."""
    pytest.importorskip("flask")
    from robofacts import trial_index as ti
    from robofacts import trial_runner as tr
    from robofacts import campaign_spec as cs
    from robofacts.ui import app as ui
    project = tmp_path / "example_project"
    shutil.copytree(EXAMPLE, project)
    experiments = project / "experiments"
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(experiments))
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(project))
    monkeypatch.setenv("PYTHONPATH", str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("TRIALS_VOICE", "off")
    monkeypatch.chdir(project)
    ti._recorder_at.cache_clear()
    campaign = cs.load_campaign(experiments / "demo_log_recorder" / "campaigns" / "grid.yaml", experiments, [])
    config = tr.RunnerConfig.load("demo_log_recorder", experiments)
    import robofacts.campaign_plan as cp
    plan = cp.build_plan(campaign, [], False)
    results = tr.run_plan(plan, config, experiments / "demo_log_recorder" / "recordings", dry_run=False,
                          limit=1, pause_before_action=False, extra_verify=campaign.verify_steps)
    assert [r.status for r in results] == ["ran"], [r.detail for r in results]
    app = ui.create_app(experiments, password="pw")
    app.config["TESTING"] = True
    yield app.test_client(), results[0]
    ti._recorder_at.cache_clear()


def auth():
    return {"Authorization": "Basic " + base64.b64encode(b"user:pw").decode()}


ROS_WORDS = ("Foxglove", "foxglove", "Kill ROS", "kill_ros", ".mcap", "Explore", "/viewers", "layout ⬇")


@pytest.mark.real_sleep
def test_the_general_pages_answer_and_name_nothing_of_a_backend_extension(client):
    c, result = client
    trial = result.recording_dir.name
    pages = ["/", "/e/demo_log_recorder", "/e/demo_log_recorder/c/grid",
             f"/e/demo_log_recorder/t/{trial}", "/e/demo_log_recorder/analyses",
             "/files/demo_log_recorder/", "/journal",
             "/api/plan/demo_log_recorder/grid.json", "/api/records/demo_log_recorder.json"]
    for page in pages:
        response = c.get(page, headers=auth())
        body = response.get_data(as_text=True)
        assert response.status_code == 200, (page, response.status_code, body[:500])
        for word in ROS_WORDS:
            assert word not in body, f"{page} shows {word!r}"
    assert c.get("/healthz").status_code == 200
    # The old address of a campaign page redirects to the new one.
    assert c.get("/campaign/demo_log_recorder/grid", headers=auth()).headers["Location"].endswith(
        "/e/demo_log_recorder/c/grid")


@pytest.mark.real_sleep
def test_the_pages_show_the_trial_and_the_coverage(client):
    c, result = client
    trial = result.recording_dir.name
    experiment = c.get("/e/demo_log_recorder", headers=auth()).get_data(as_text=True)
    assert trial in experiment and "grid" in experiment
    page = c.get(f"/e/demo_log_recorder/t/{trial}", headers=auth()).get_data(as_text=True)
    assert "The project's backend offers no viewer" in page
    plan = c.get("/api/plan/demo_log_recorder/grid.json", headers=auth()).get_json()
    assert plan is not None
    records = c.get("/api/records/demo_log_recorder.json", headers=auth()).get_json()
    assert any(r.get("name") == trial for r in (records if isinstance(records, list) else records.get("records", [])))


@pytest.mark.real_sleep
def test_the_routes_of_a_backend_extension_do_not_exist(client):
    c, _ = client
    for path in ("/viewers", "/layout/demo_log_recorder/x.json", "/bag/demo_log_recorder/x.mcap"):
        assert c.get(path, headers=auth()).status_code == 404, path
    assert c.post("/kill_ros", headers=auth()).status_code in (404, 405)
    response = c.post("/explore", data={"experiment": "demo_log_recorder", "campaign": "grid"}, headers=auth())
    assert response.status_code == 404
    assert "offers no Explore" in response.get_data(as_text=True)


@pytest.mark.real_sleep
def test_a_campaign_runs_from_the_interface(client):
    """The run button: a dry run of the campaign through the interface, on the process
    backend. The page of the run answers, and the run ends."""
    c, _ = client
    response = c.post("/run", data={"experiment": "demo_log_recorder", "campaign": "grid", "limit": "1"},
                      headers=auth())
    assert response.status_code in (200, 302, 303), response.get_data(as_text=True)[:800]
    if response.status_code in (302, 303):
        run_id = response.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
        for _ in range(100):
            state = c.get(f"/api/run/{run_id}.json", headers=auth()).get_json()
            if state and state.get("state") not in ("running", "aborting"):
                break
            time.sleep(0.1)
        assert state["state"] not in ("running", "aborting"), state
        page = c.get(f"/run/{run_id}", headers=auth()).get_data(as_text=True)
        assert "Foxglove" not in page

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


# --------------------------------------------------------------------------- #
# Review round 2 (2026-11-25): the rig's run slot, the stops, the gate, the forms
# --------------------------------------------------------------------------- #
@pytest.fixture
def bare(tmp_path, monkeypatch):
    """The interface on a copy of the example with no trial recorded, and its parts."""
    pytest.importorskip("flask")
    from types import SimpleNamespace
    from robofacts import trial_index as ti
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
    app = ui.create_app(experiments, password="pw")
    app.config["TESTING"] = True
    yield SimpleNamespace(app=app, c=app.test_client(), root=experiments, runs=app.config["RUNS"],
                          ui=ui, project=project)
    ti._recorder_at.cache_clear()


@pytest.fixture
def held_runner(monkeypatch):
    """run_plan replaced: an executed run holds the rig until the test releases it, and a dry
    run ends at once. Nothing is launched."""
    import threading
    from robofacts import trial_runner as tr
    release = threading.Event()

    def run_plan(plan, config, bags_dir, *, dry_run=True, **kwargs):
        if not dry_run:
            release.wait(timeout=30)
        return []

    monkeypatch.setattr(tr, "run_plan", run_plan)
    yield release
    release.set()


EXECUTE = {"experiment": "demo_log_recorder", "campaign": "grid", "limit": "1",
           "go": "execute", "confirm": "yes"}
DRY = {"experiment": "demo_log_recorder", "campaign": "grid", "limit": "1", "go": "dry"}


def run_id_of(response) -> str:
    assert response.status_code == 302, response.get_data(as_text=True)[:800]
    return response.headers["Location"].rstrip("/").rsplit("/", 1)[-1]


def wait_until_ended(run, timeout: float = 20.0):
    deadline = time.time() + timeout
    while run.state in ("running", "aborting") and time.time() < deadline:
        time.sleep(0.05)
    return run


def test_a_dry_run_during_an_executed_campaign_does_not_take_the_rig(bare, held_runner, monkeypatch):
    """C2-1: a dry run took the single run slot, so a second EXECUTE was accepted and the stop
    button aborted the dry run instead of the campaign that flew."""
    executed = bare.runs.get(run_id_of(bare.c.post("/run", data=EXECUTE, headers=auth())))
    dry = wait_until_ended(bare.runs.get(run_id_of(bare.c.post("/run", data=DRY, headers=auth()))))
    assert dry.state == "finished" and not dry.executed
    assert bare.runs.busy() and bare.runs.current() is executed
    second = bare.c.post("/run", data=EXECUTE, headers=auth())
    assert second.status_code == 409 and "already running" in second.get_data(as_text=True)
    assert sum(1 for r in bare.runs.all() if r.executed) == 1

    class Done:
        returncode, stdout, stderr = 0, "stopped", ""

    monkeypatch.setattr(bare.ui.subprocess, "run", lambda cmd, **kw: Done())
    page = bare.c.post("/stop", data={"action": "estop"}, headers=auth()).get_data(as_text=True)
    assert executed.state == "aborting", "the stop must reach the campaign that runs"
    assert "told to stop" in page


def test_the_run_slot_is_claimed_inside_start_under_its_lock(bare, held_runner):
    """C2-1: the busy check lives where the slot is taken, so two EXECUTE posts that both
    passed the route's early check cannot both start a stack."""
    from robofacts import campaign_plan as cp
    from robofacts import campaign_spec as cs
    from robofacts import trial_runner as tr
    campaign = cs.load_campaign(bare.root / "demo_log_recorder" / "campaigns" / "grid.yaml", bare.root)
    args = dict(experiment="demo_log_recorder", campaign_name="grid", campaign_file="grid",
                plan=cp.build_plan(campaign, [], False),
                config=tr.RunnerConfig.load("demo_log_recorder", bare.root),
                bags_dir=bare.root / "demo_log_recorder" / "recordings", limit=1,
                journal=bare.app.config["JOURNAL"])
    first = bare.runs.start(executed=True, **args)
    with pytest.raises(bare.ui.RunSlotBusy):
        bare.runs.start(executed=True, **args)
    wait_until_ended(bare.runs.start(executed=False, **args))
    assert bare.runs.current() is first and bare.runs.busy()


ROBOTS = """
version: 1
robots:
  demo_bot:
    host: local
    estop: "echo estop demo_bot"
    soft_stop: "echo soft demo_bot"
    actions:
      wave: {description: "wave", command: "echo wave"}
"""


def test_the_stop_and_action_buttons_name_the_robots_file_of_the_interface(bare, tmp_path, monkeypatch):
    """C2-2: estop.sh and robot_action.sh looked for robots.yaml from the server's working
    directory. With `trials ui --experiments-root X` started elsewhere, the stop bar listed X's
    robots and the stop failed with "Robot config not found". The scripts are stubbed here."""
    robots = bare.root / "common" / "config" / "robots.yaml"
    robots.unlink()
    robots.write_text(ROBOTS, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(bare.ui.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Done())[1])
    assert "demo_bot" in bare.c.get("/", headers=auth()).get_data(as_text=True)
    assert bare.c.post("/stop", data={"action": "estop", "robot": "demo_bot"}, headers=auth()).status_code == 200
    assert bare.c.post("/robot_action", data={"robot": "demo_bot", "action": "wave"},
                       headers=auth()).status_code == 200
    stop, act = calls
    assert stop[0].endswith("estop.sh") and stop[1:3] == ["--config", str(robots)]
    assert stop[3:] == ["--robot", "demo_bot"]
    assert act[0].endswith("robot_action.sh") and act[1:] == ["--config", str(robots), "demo_bot", "wave"]


@pytest.mark.real_sleep
def test_a_failing_checklist_blocks_an_execute_from_the_interface_as_on_the_cli(bare):
    """C2-3: an experiment without scripts/preflight.sh ran ungated from the interface, while
    the CLI built its gate from the checklist, and those trials counted. The paper says the
    runner runs the gate whether it starts from the CLI or the web interface."""
    import json
    from robofacts import trial_index as ti
    checklist = bare.root / "demo_log_recorder" / "config" / "preflight_checklist.json"
    data = json.loads(checklist.read_text(encoding="utf-8"))
    data["checklist"][0] = {"text": "[HARD] Enough disk for the recording.", "check": "disk_space",
                            "args": {"path": ".", "min_gb": "999999"}}
    checklist.unlink()
    checklist.write_text(json.dumps(data), encoding="utf-8")
    run = wait_until_ended(bare.runs.get(run_id_of(bare.c.post("/run", data=EXECUTE, headers=auth()))),
                           timeout=120)
    assert [r["status"] for r in run.results] == ["blocked"], (run.state, run.error, run.results)
    assert "disk_space" in run.results[0]["detail"]
    assert "UNGATED" not in run.campaign
    assert not [t for t in ti.load_trials(bare.root, "demo_log_recorder") if t.status == "usable"]
    gate_log = bare.root / "demo_log_recorder" / "runs" / "preflight_gate.jsonl"
    assert len(gate_log.read_text(encoding="utf-8").splitlines()) == 1


def test_an_execute_with_no_gate_at_all_says_so(bare, held_runner):
    """C2-3: with no preflight.sh and no checklist the run carries " ⚠UNGATED" in its shown name,
    like a skipped gate, and the campaign page no longer promises a T-0 gate."""
    (bare.root / "demo_log_recorder" / "config" / "preflight_checklist.json").unlink()
    page = bare.c.get("/e/demo_log_recorder/c/grid", headers=auth()).get_data(as_text=True)
    assert "run the T-0 gate" not in page and "runs UNGATED" in page
    run = bare.runs.get(run_id_of(bare.c.post("/run", data=EXECUTE, headers=auth())))
    assert run.campaign.endswith(" ⚠UNGATED"), run.campaign

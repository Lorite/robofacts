"""Tests for the experiments control surface (#96 Phase 5).

Flask's test client exercises every route without a browser, so the properties
that matter — auth, the two-step run, the stop allowlist, and the UI never being
able to do something the CLI cannot — are asserted offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import time

import pytest

HERE = Path(__file__).resolve().parent
UI_DIR = HERE.parent / "ui"

pytest.importorskip("flask", reason="the UI needs Flask (present in the dev container)")

from robofacts.ui import app as ui  # noqa: E402

PASSWORD = "hunter2"

FACTORS = """
version: 1
factors:
  execution_host:
    levels: [laptop, orin]
    default: orin
    metadata_key: execution_host
    maps_to: {kind: host}
  trajectory:
    levels: [hover, circle]
    default: hover
    metadata_key: trajectory
    maps_to: {kind: launch_arg, name: trajectory}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  trajectory: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

RUNNER = """
launch: {package: demo_nodes_cpp, file: talker}
action: {command: "echo fly --shape {trajectory}"}
hosts:
  laptop: {local: true}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 2
sweep:
  trajectory: [hover, circle]
fixed:
  execution_host: laptop
"""

ANALYSES = """
analyses:
  quick_look:
    describe: "a quick look"
    per: bag
    command: "echo {bag} {results}"
"""

# A campaign that asks the operator for things: manual prep before the trial
# (one step with a runnable command), and live steps while it flies (one prose,
# one runnable). Exercises the browser mirror of the CLI's operator waits.
CAMPAIGN_STEPS = """
name: steps
experiment: expA
repetitions: 1
sweep:
  trajectory: [hover]
fixed:
  execution_host: laptop
operator_steps:
  - text: "Power the drone and place it on the pad"
  - text: |-
      Switch the Motive body to the patched arm
      details on the second line
    run: "echo motive {execution_host}"
operator_live_steps:
  - "prose only: watch the estimator"
  - text: "hand over to vision"
    run: "echo handover"
"""

ROBOTS = """
version: 1
robots:
  crazyflie:
    host: local
    verified: true
    estop: {description: "cut motors", command: "echo ESTOP"}
    soft_stop: {description: "land", command: "echo SOFT"}
  spot:
    host: local
    verified: true
    estop: {description: "cut power", command: "echo ESTOP"}
    soft_stop: {description: "sit", command: "echo SOFT"}
    actions:
      ready: {description: "stand and pose the arm", command: "echo READY"}
      lease: {description: "who holds the lease", command: "echo LEASE"}
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(FACTORS, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "common" / "config" / "robots.yaml").write_text(ROBOTS, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER, encoding="utf-8")
    (tmp_path / "expA" / "config" / "analyses.yaml").write_text(ANALYSES, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    (tmp_path / "expA" / "campaigns" / "steps.yaml").write_text(CAMPAIGN_STEPS, encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(root: Path):
    app = ui.create_app(root, PASSWORD, repo_root=root.parent)
    app.config["TESTING"] = True
    return app.test_client()


def auth(password: str = PASSWORD, user: str = "x") -> dict:
    import base64
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --------------------------------------------------------------------------- #
# Authentication — this endpoint can start a flight
# --------------------------------------------------------------------------- #
def test_unauthenticated_is_refused(client):
    assert client.get("/").status_code == 401


def test_a_wrong_password_is_refused(client):
    assert client.get("/", headers=auth("nope")).status_code == 401


def test_the_right_password_gets_in(client):
    assert client.get("/", headers=auth()).status_code == 200


def test_actions_are_also_behind_auth(client):
    assert client.post("/run", data={"experiment": "expA", "campaign": "c"}).status_code == 401
    assert client.post("/stop", data={"action": "estop"}).status_code == 401


def test_healthz_needs_no_auth(client):
    """So a tunnel can be probed without handing over the password."""
    assert client.get("/healthz").status_code == 200


def test_a_configured_user_name_is_checked(root):
    """EXPERIMENTS_UI_USER (2026-10-20): the user name must match too."""
    app = ui.create_app(root, PASSWORD, repo_root=root.parent, user="lori")
    app.config["TESTING"] = True
    c = app.test_client()
    assert c.get("/", headers=auth(user="lori")).status_code == 200
    assert c.get("/", headers=auth(user="admin")).status_code == 401
    assert c.get("/", headers=auth("nope", user="lori")).status_code == 401


def test_no_user_configured_accepts_any_user_name(client):
    """Unset EXPERIMENTS_UI_USER keeps the old behavior: only the password counts."""
    assert client.get("/", headers=auth(user="anyone")).status_code == 200


def test_no_password_configured_means_open(root):
    app = ui.create_app(root, None, repo_root=root.parent)
    assert app.test_client().get("/").status_code == 200, "--no-auth mode"


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def test_index_lists_experiments_and_campaigns(client):
    body = client.get("/", headers=auth()).get_data(as_text=True)
    assert "expA" in body and "c" in body


def test_campaign_view_shows_the_coverage_and_the_shortfall(client):
    body = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert '<div class="k">wanted</div><div class="v">4</div>' in body
    assert '<div class="k">still missing</div><div class="v">4</div>' in body


def test_campaign_view_marks_non_defaults(client):
    """The UI requirement from the original ask: non-defaults clearly marked."""
    body = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert 'class="nd"' in body


def _write_bag(root: Path, name: str, metadata: dict) -> None:
    bag = root / "expA" / "bags" / name
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (bag / f"{name}_0.mcap").write_bytes(b"not really a bag")


def test_the_bag_table_shows_the_code_version_of_each_trial(client, root):
    """Both halves of the column, because a trial recorded before code stamping
    must render as cleanly as one recorded after it — the table is the operator's
    only view of the bags, so a missing key there breaks the whole page."""
    _write_bag(root, "t_stamped", {
        "trial_id": "t_stamped", "execution_host": "laptop", "aborted": False,
        "trajectory": "hover",
        "code_version": {"commit": "abcdef1234567890", "branch": "main",
                         "dirty": True, "dirty_files": ["a.py"],
                         "dirty_file_count": 1, "captured_on": "remote:orin"},
    })
    _write_bag(root, "t_legacy", {
        "trial_id": "t_legacy", "execution_host": "laptop", "aborted": False,
        "trajectory": "circle",
    })

    body = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert "abcdef12" in body, "the stamped trial should show its short commit"
    assert "remote:orin" in body, "the tooltip should name the host that answered"
    assert "recorded before code stamping" in body, "the legacy trial needs a reason, not a crash"


def test_code_version_is_not_offered_as_a_coverage_axis(client, root):
    """A dict in the metadata that leaked into the factor columns would put one
    column per commit in the grid and reopen every cell on every commit."""
    _write_bag(root, "t1", {
        "trial_id": "t1", "execution_host": "laptop", "aborted": False,
        "trajectory": "hover", "code_version": {"commit": "abc", "dirty": False},
    })
    body = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert "<th>code_version</th>" not in body


def test_unknown_campaign_is_a_clean_404(client):
    assert client.get("/e/expA/c/nope", headers=auth()).status_code == 404


def test_analyses_view_lists_declared_analyses(client):
    body = client.get("/e/expA/analyses", headers=auth()).get_data(as_text=True)
    assert "quick_look" in body


def test_api_plan_returns_the_same_numbers_as_the_cli(client, root):
    from robofacts import campaign_plan as cp
    from robofacts import campaign_spec as cs
    from robofacts import trial_index as ti
    payload = json.loads(client.get("/api/plan/expA/c.json", headers=auth()).get_data(as_text=True))
    campaign = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    expected = cp.build_plan(campaign, ti.load_trials(root, "expA"))
    assert payload["wanted"] == expected.wanted
    assert payload["needed"] == expected.needed, "the UI must not compute its own numbers"


# --------------------------------------------------------------------------- #
# Running is a two-step
# --------------------------------------------------------------------------- #
def _start_run(client, **extra) -> str:
    data = {"experiment": "expA", "campaign": "c", "limit": "1"}
    data.update(extra)
    # Executing takes BOTH the EXECUTE button and the arm checkbox; the
    # checkbox alone is a dry run (deliberate, since 2026-07-29).
    if data.get("confirm") == "yes":
        data.setdefault("go", "execute")
    response = client.post("/run", headers=auth(), data=data)
    assert response.status_code == 302, "a run should redirect to its live page"
    return response.headers["Location"].rstrip("/").split("/")[-1]


def _wait(client, run_id: str, timeout: float = 20.0) -> dict:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = json.loads(client.get(f"/api/run/{run_id}.json",
                                        headers=auth()).get_data(as_text=True))
        if payload["state"] in ("finished", "aborted", "error"):
            return payload
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} never finished: {payload}")


def test_run_without_confirm_is_a_dry_run(client, root):
    payload = _wait(client, _start_run(client))
    assert payload["state"] == "finished" and payload["executed"] is False
    assert not list((root / "expA" / "bags").glob("*")), "a dry run must create no bag"


def test_run_shows_the_commands_it_would_issue(client):
    payload = _wait(client, _start_run(client))
    commands = " ".join(c for t in payload["results"] for c in t["commands"])
    assert "ros2 launch" in commands and "ros2 bag record" in commands


def test_the_live_page_renders_and_polls(client):
    run_id = _start_run(client)
    body = client.get(f"/run/{run_id}", headers=auth()).get_data(as_text=True)
    assert "api/run/" in body, "the page must poll for progress"
    assert run_id in body


def test_progress_is_visible_before_the_run_ends(client):
    """The whole point of Phase 5 polish: a long campaign must not look frozen."""
    run_id = _start_run(client, limit="4")
    payload = json.loads(client.get(f"/api/run/{run_id}.json",
                                    headers=auth()).get_data(as_text=True))
    assert payload["wanted"] >= 1
    assert payload["state"] in ("running", "finished")
    final = _wait(client, run_id)
    assert final["done"] == final["wanted"]


def test_an_unknown_run_is_a_clean_404(client):
    assert client.get("/run/nope", headers=auth()).status_code == 404
    assert client.get("/api/run/nope.json", headers=auth()).status_code == 404


def test_abort_marks_the_run_and_never_kills_a_trial(client):
    """Abort is cooperative: it stops the NEXT trial, it does not interrupt one
    in flight — that is the e-stop's job, and killing a recorder loses the bag."""
    run_id = _start_run(client, limit="4")
    client.post(f"/run/{run_id}/abort", headers=auth())
    payload = _wait(client, run_id)
    assert payload["state"] in ("aborted", "finished")


def test_only_one_campaign_may_execute_at_a_time(client, root, monkeypatch):
    """Two campaigns on one rig would fight over radio, recorder and airspace."""
    import threading
    from robofacts import trial_runner as tr_mod
    release = threading.Event()

    # The first campaign holds the rig until the test releases it. It runs no real
    # trial: the earlier version called the real run_plan and returned before the
    # trial ended, so pytest left its `ros2 bag record` running after the session.
    def held(*args, **kwargs):
        release.wait(timeout=20)

    monkeypatch.setattr(tr_mod, "run_plan", held)
    first = _start_run(client, confirm="yes")
    try:
        second = client.post("/run", headers=auth(),
                             data={"experiment": "expA", "campaign": "c", "limit": "1",
                                   "go": "execute", "confirm": "yes"})
        assert second.status_code == 409
        assert "already running" in second.get_data(as_text=True)
    finally:
        release.set()
    assert _wait(client, first)["state"] == "finished"


def test_a_broken_runner_config_is_reported_not_a_500(client, root):
    (root / "expA" / "config" / "runner.yaml").write_text("launch: {package: p}\n", encoding="utf-8")
    response = client.post("/run", headers=auth(),
                           data={"experiment": "expA", "campaign": "c"})
    assert response.status_code == 400
    assert "launch.file is required" in response.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Stops are a narrow allowlist
# --------------------------------------------------------------------------- #
def test_stop_invokes_the_estop_script(client, root, monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = "All robots confirmed the estop."
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Done())[1])
    body = client.post("/stop", headers=auth(), data={"action": "estop"}).get_data(as_text=True)
    assert calls and calls[0][0].endswith("tools/estop.sh")
    assert "--soft" not in calls[0]
    assert "confirmed" in body


def test_soft_stop_passes_the_soft_flag(client, monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Done())[1])
    client.post("/stop", headers=auth(), data={"action": "soft", "robot": "crazyflie"})
    assert "--soft" in calls[0] and "--robot" in calls[0] and "crazyflie" in calls[0]


def test_a_failed_stop_says_use_the_physical_switch(client, monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "1 robot(s) DID NOT CONFIRM"

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: Failed())
    body = client.post("/stop", headers=auth(), data={"action": "estop"}).get_data(as_text=True)
    assert "physical kill switch" in body


# --------------------------------------------------------------------------- #
# Robot actions are the same narrow allowlist (2026-08-10)
# --------------------------------------------------------------------------- #
def test_declared_action_invokes_the_action_script(client, monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = "=== OK spot.ready"
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Done())[1])
    body = client.post("/robot_action", headers=auth(),
                       data={"robot": "spot", "action": "ready"}).get_data(as_text=True)
    assert calls and calls[0][0].endswith("tools/robot_action.sh")
    assert calls[0][1:] == ["spot", "ready"]
    assert "OK" in body


def test_an_undeclared_action_is_refused_before_any_process(client, monkeypatch):
    monkeypatch.setattr(ui.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(AssertionError("spawned!")))
    resp = client.post("/robot_action", headers=auth(),
                       data={"robot": "spot", "action": "power_off"})
    assert resp.status_code == 400
    resp = client.post("/robot_action", headers=auth(),
                       data={"robot": "nosuch", "action": "ready"})
    assert resp.status_code == 400


def _lease_echo(owner: str) -> str:
    """`ros2 topic echo .../status/leases --once` as robot_action.sh prints it, in the format
    of a LeaseArray recorded from the real Spot on 2026-08-17 (client name anonymized)."""
    blocks = []
    for resource in ("all-leases", "arm", "body", "fan", "full-arm", "gripper", "mobility"):
        blocks.append(f"""- resource: {resource}
  lease:
    resource: {resource}
    epoch: jloxOXQHyAqctmup
    sequence:
    - 4
  lease_owner:
    client_name: '{owner}'
    user_name: ''""")
    return "=== spot.lease ===\nresources:\n" + "\n".join(blocks) + "\n---\n=== OK spot.lease\n"


def test_the_lease_action_states_who_holds_the_body_lease(client, monkeypatch):
    class Done:
        returncode = 0
        stdout = _lease_echo("ros_rig:__main__.py-192339")
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: Done())
    body = client.post("/robot_action", headers=auth(),
                       data={"robot": "spot", "action": "lease"}).get_data(as_text=True)
    assert "Body lease: held by ros_rig:__main__.py-192339" in body
    assert all(f"<tr><td>{r}</td>" in body for r in ("all-leases", "arm", "body", "mobility"))


def test_a_free_body_lease_says_so(client, monkeypatch):
    class Done:
        returncode = 0
        stdout = _lease_echo("")
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: Done())
    body = client.post("/robot_action", headers=auth(),
                       data={"robot": "spot", "action": "lease"}).get_data(as_text=True)
    assert "Body lease: nobody holds it" in body


def test_other_actions_show_no_lease_table():
    assert ui.lease_owners("=== OK spot.ready") is None
    assert ui.lease_owners("resources: [unclosed") is None
    owners = ui.lease_owners(_lease_echo("tablet"))
    assert [o["resource"] for o in owners][:3] == ["all-leases", "arm", "body"]
    assert {o["client_name"] for o in owners} == {"tablet"}


def test_robot_action_requires_auth(client):
    assert client.post("/robot_action", data={"robot": "spot", "action": "ready"}).status_code == 401


def test_index_renders_the_declared_actions(client):
    body = client.get("/", headers=auth()).get_data(as_text=True)
    assert "Robot actions" in body and "ready" in body and "lease" in body


def test_the_stop_endpoint_cannot_run_arbitrary_commands(client, monkeypatch):
    """The robot name is passed as an argv element to estop.sh, which validates
    it against robots.yaml — it is never interpolated into a shell string."""
    calls = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Done())[1])
    client.post("/stop", headers=auth(), data={"action": "estop", "robot": "; rm -rf /"})
    assert isinstance(calls[0], list), "the command must be argv, never a shell string"
    assert calls[0][-1] == "; rm -rf /", "passed as one argv element, not interpreted"


# --------------------------------------------------------------------------- #
# Deep links and defaults
# --------------------------------------------------------------------------- #
def test_foxglove_link_is_a_remote_file_deep_link():
    link = ui.foxglove_link("http://localhost:8080", "expA", "trial1_slim")
    assert link.startswith("foxglove://open?ds=remote-file&ds.url=")
    assert link.endswith(".mcap"), "Foxglove needs the extension to pick a reader"


def test_viewers_page_offers_live_and_recorded_paths(client):
    body = client.get("/viewers", headers=auth()).get_data(as_text=True)
    assert "ws://localhost:8765" in body, "the live path is the foxglove bridge"
    assert "rviz2 -d" in body, "RViz gets a copy-paste command, having no URL scheme"


def test_viewers_page_links_slim_bags_via_foxglove(client, root):
    bag = root / "expA" / "bags_slim" / "t1_slim"
    bag.mkdir(parents=True)
    (bag / "d_0.mcap").write_bytes(b"x")
    (bag / "trial_metadata.json").write_text(json.dumps(
        {"trial_id": "t1", "execution_host": "laptop", "aborted": False}), encoding="utf-8")
    body = client.get("/viewers", headers=auth()).get_data(as_text=True)
    assert "foxglove://open?ds=remote-file" in body and "t1_slim" in body


def test_viewers_page_lists_slim_scratch_bags(client, root):
    """#130: a slim copy of a scratch flight (bags_scratch/*_slim) is listed like a campaign's."""
    fat = root / "expA" / "bags_scratch" / "t9_scratch_x"
    slim = root / "expA" / "bags_scratch" / "t9_scratch_x_slim"
    for b in (fat, slim):
        b.mkdir(parents=True)
        (b / "d_0.mcap").write_bytes(b"x")
    body = client.get("/viewers", headers=auth()).get_data(as_text=True)
    assert "t9_scratch_x_slim" in body
    assert body.count("t9_scratch_x<") == 0 and "t9_scratch_x.mcap" not in body, "the fat scratch bag is not offered"


def test_viewers_page_lists_per_experiment_foxglove_layouts(client, root):
    """#130: a layout an experiment ships as config/foxglove*.json is listed with a download
    link, and the download route serves it (and only it) behind auth."""
    layout = root / "expA" / "config" / "foxglove_demo.json"
    layout.write_text('{"configById": {}, "layout": null}', encoding="utf-8")
    (root / "expA" / "config" / "not_a_layout.json").write_text("{}", encoding="utf-8")
    body = client.get("/viewers", headers=auth()).get_data(as_text=True)
    assert "foxglove_demo.json" in body and "/layout/expA/foxglove_demo.json" in body
    assert "not_a_layout.json" not in body, "only names starting with foxglove are layouts"
    got = client.get("/layout/expA/foxglove_demo.json", headers=auth())
    assert got.status_code == 200 and b'"configById"' in got.data
    assert client.get("/layout/expA/not_a_layout.json", headers=auth()).status_code == 404
    assert client.get("/layout/expA/foxglove_demo.json").status_code == 401, "layouts stay behind auth"


def test_a_slim_bag_can_be_streamed(client, root):
    bag = root / "expA" / "bags_slim" / "t1_slim"
    bag.mkdir(parents=True)
    (bag / "d_0.mcap").write_bytes(b"MCAPDATA")
    response = client.get("/bag/expA/t1_slim.mcap", headers=auth())
    assert response.status_code == 200 and response.get_data() == b"MCAPDATA"


def test_bag_serving_rejects_path_traversal(client, root):
    """Names are resolved against bags that exist, never joined onto a path."""
    secret = root.parent / "secret.mcap"
    secret.write_bytes(b"TOPSECRET")
    for attempt in ("..%2f..%2fsecret", "....//secret", "nonexistent"):
        response = client.get(f"/bag/expA/{attempt}.mcap", headers=auth())
        assert response.status_code in (403, 404), attempt
        assert b"TOPSECRET" not in response.get_data()


def test_bag_serving_is_deliberately_public(client):
    """Slim-bag serving is intentionally unauthenticated, and must stay that way.

    Foxglove (desktop or app.foxglove.dev) fetches the bag URL itself and cannot
    present Basic credentials, so putting this route behind auth would silently
    break every "open in Foxglove" link. It serves read-only slim bags from a
    localhost/tunnel service, and it resolves names against bags that exist
    rather than joining user input onto a path (see the traversal test above).

    This test asserted 401 until 2026-08-12. The route was made public
    deliberately in e788cc64ef, which did not update the test, so the suite had
    been red ever since. It now pins the real contract instead.
    """
    response = client.get("/bag/expA/t1_slim.mcap")
    assert response.status_code != 401, "serving must not demand Basic auth"
    # No such slim bag in the fixture, so the traversal-safe lookup 404s.
    assert response.status_code == 404


def test_only_bag_serving_and_healthz_are_public(client):
    """Every other route stays behind auth, including every state-changing one.

    The real invariant worth guarding is not "this one route is authed" but
    "nothing accidentally stops being authed". requires_auth wraps its view with
    functools.wraps, so an authed view is exactly one carrying __wrapped__.

    Each name below is public on purpose: `serve_bag` because Foxglove fetches
    it itself, `healthz` so a tunnel can be probed without the password, and
    `static` is Flask's own endpoint for the UI's CSS and JS.
    """
    expected_public = {"serve_bag", "healthz", "static"}
    app = client.application
    public = {name for name, view in app.view_functions.items()
              if not hasattr(view, "__wrapped__")}
    assert public == expected_public, (
        f"unexpected public route(s): {public - expected_public}")

    # And the introspection above agrees with actual behaviour.
    assert client.get("/").status_code == 401
    assert client.post("/stop", data={"action": "estop"}).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_the_default_bind_is_localhost():
    # Checks the parsed default, not the source text: the parser moved to its own
    # module so that `trials ui --help` needs no Flask, and a source grep of
    # ui.main stopped seeing it.
    from robofacts.ui.arguments import build_parser

    assert build_parser().parse_args([]).host == "127.0.0.1", "must not default to 0.0.0.0"


def test_it_refuses_a_public_bind_without_a_password(monkeypatch, capsys):
    monkeypatch.delenv("EXPERIMENTS_UI_PASSWORD", raising=False)
    assert ui.main(["--host", "0.0.0.0", "--no-auth"]) == 2
    assert "without a password" in capsys.readouterr().err


def test_it_refuses_to_start_unauthenticated_by_accident(monkeypatch, capsys):
    monkeypatch.delenv("EXPERIMENTS_UI_PASSWORD", raising=False)
    assert ui.main([]) == 2
    assert "EXPERIMENTS_UI_PASSWORD" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Operator flows: the CLI's waits and tally, mirrored in the browser (2026-08-12)
# --------------------------------------------------------------------------- #
def _start_executed_stub_run(client, campaign: str = "steps", **extra) -> str:
    """EXECUTE with every trial phase disabled: the full executed code path —
    the operator waits, the gate hooks, the tally — with nothing launched,
    recorded or flown. hold_s=0 so the manual window costs no test time."""
    data = {"experiment": "expA", "campaign": campaign, "limit": "1",
            "go": "execute", "confirm": "yes", "steps_present": "1", "hold_s": "0"}
    data.update(extra)
    response = client.post("/run", headers=auth(), data=data)
    assert response.status_code == 302, response.get_data(as_text=True)
    return response.headers["Location"].rstrip("/").split("/")[-1]


def _wait_for(client, run_id: str, predicate, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = json.loads(client.get(f"/api/run/{run_id}.json",
                                        headers=auth()).get_data(as_text=True))
        if predicate(payload):
            return payload
        if payload["state"] in ("finished", "aborted", "error"):
            raise AssertionError(f"run {run_id} ended without matching: {payload}")
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never matched the predicate: {payload}")


def test_tally_is_the_clis_own_function():
    """One tally function for both surfaces — successes first (6d31fcca33)."""
    from robofacts import trial_runner as tr
    t = tr.tally(["ran", "ran", "blocked", "dry-run"])
    assert (t["total"], t["ok"], t["blocked"], t["failed"]) == (4, 2, 1, 0)
    assert t["line"] == "4 trial(s): 2 OK, 1 blocked, 0 failed"
    assert t["bad"] is True
    assert tr.tally(["ran"])["bad"] is False


def test_executed_run_waits_for_the_operator_steps(client):
    """The browser mirror of the CLI's Enter wait (8f1213cc6e): with
    operator_steps declared, an EXECUTE launches NOTHING until the operator
    confirms on the run page — otherwise the T-0 gate races the physical prep.
    Also the reason the UI must pass its own confirmation surface into
    run_plan: the tty default would park the worker on the SERVER's stdin."""
    run_id = _start_executed_stub_run(client)
    payload = _wait_for(client, run_id, lambda p: p["awaiting_kind"] == "steps")
    assert payload["state"] == "running" and payload["done"] == 0
    assert "hover" in payload["awaiting_what"]

    body = client.get(f"/run/{run_id}", headers=auth()).get_data(as_text=True)
    assert f"/run/{run_id}/proceed" in body, "the run page must carry the confirm button"

    client.post(f"/run/{run_id}/proceed", headers=auth())
    final = _wait(client, run_id)
    assert final["state"] == "finished"
    assert [r["status"] for r in final["results"]] == ["ran"]
    assert final["awaiting_kind"] == ""
    assert final["tally"]["ok"] == 1 and final["tally"]["bad"] is False


def test_abort_while_waiting_refuses_the_trial(client):
    """Abort must wake a parked confirmation AND the woken trial must refuse
    to launch — otherwise 'abort' either hangs forever or flies one more."""
    run_id = _start_executed_stub_run(client)
    _wait_for(client, run_id, lambda p: p["awaiting_kind"] == "steps")
    client.post(f"/run/{run_id}/abort", headers=auth())
    final = _wait(client, run_id)
    assert final["state"] == "aborted"
    assert [r["status"] for r in final["results"]] == ["blocked"]
    assert "not confirmed" in final["results"][0]["detail"]
    assert final["tally"]["blocked"] == 1


def test_the_pose_pause_waits_in_the_browser_too(client):
    """The second Enter wait (b0b42729ac), between the T-0 gate and the
    recorder. Same surface, kind 'pose', opted in by the form checkbox."""
    run_id = _start_executed_stub_run(client, pose_pause="1")
    _wait_for(client, run_id, lambda p: p["awaiting_kind"] == "steps")
    client.post(f"/run/{run_id}/proceed", headers=auth())
    payload = _wait_for(client, run_id, lambda p: p["awaiting_kind"] == "pose")
    assert payload["done"] == 0, "nothing may be recorded while the rig is being posed"
    client.post(f"/run/{run_id}/proceed", headers=auth())
    final = _wait(client, run_id)
    assert final["state"] == "finished"
    assert [r["status"] for r in final["results"]] == ["ran"]


def test_a_dry_run_never_asks_for_confirmation(client):
    """Dry runs preview; parking one on an operator wait would be absurd."""
    final = _wait(client, _start_run(client, campaign="steps"))
    assert final["state"] == "finished" and final["executed"] is False


def test_proceed_when_nothing_waits_is_harmless(client):
    run_id = _start_run(client)
    response = client.post(f"/run/{run_id}/proceed", headers=auth())
    assert response.status_code == 302
    assert _wait(client, run_id)["state"] == "finished"


def test_campaign_page_shows_live_steps_with_the_cockpit_command(client):
    """The UI's live panel and the tmux handover panes come from the same
    extractor (campaign_live_steps.annotate), text plus a copyable one-liner."""
    body = client.get("/e/expA/c/steps", headers=auth()).get_data(as_text=True)
    assert "hand over to vision" in body
    assert "echo handover" in body
    assert "copystep" in body, "the one-liner must be copyable"
    assert "prose only" in body, "prose-only steps stay visible in the browser"


def test_operator_step_runs_only_the_campaigns_declared_commands(client):
    """The campaign file is the allowlist: the browser posts an INDEX, and only
    indices that carry a declared run command execute anything."""
    def post(kind, index):
        return client.post("/operator_step", headers=auth(),
                           data={"experiment": "expA", "campaign": "steps",
                                 "kind": kind, "index": str(index)})

    assert post("live", 0).status_code == 400, "a prose step has nothing to run"
    assert post("live", 7).status_code == 400, "out of range is refused"
    ran = post("live", 1)
    assert ran.status_code == 200
    assert "handover" in ran.get_data(as_text=True)
    substituted = post("pre", 1).get_data(as_text=True)
    assert "motive laptop" in substituted, "fixed factor values are substituted"


def test_run_trial_refuses_an_unconfirmed_trial(root):
    """Runner-side contract behind the UI flow: a False from the confirmation
    surface blocks the trial before ANY process is spawned or path created."""
    from robofacts import campaign_spec as cs
    from robofacts import trial_runner as tr
    campaign = cs.load_campaign(root / "expA" / "campaigns" / "steps.yaml", root)
    config = tr.RunnerConfig.load("expA", root)
    trial = next(campaign.combinations())
    asked = []
    result = tr.run_trial(trial, campaign.factors, config, root / "expA" / "bags",
                          dry_run=False,
                          operator_steps=campaign.operator_steps,
                          confirm_steps=lambda t, s: (asked.append(s), False)[1])
    assert result.status == "blocked" and "not confirmed" in result.detail
    assert asked == [campaign.operator_steps], "the surface must see the campaign's steps"
    assert not (root / "expA" / "bags").exists(), "nothing may be created"


def test_the_tty_surface_passes_through_headless(monkeypatch):
    """confirm_operator_steps_tty keeps the pre-8f1213cc6e headless behaviour:
    no tty means no prompt, no block, proceed."""
    import types
    from robofacts import trial_runner as tr
    monkeypatch.setattr(tr.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert tr.confirm_operator_steps_tty(None, [{"text": "x"}]) is True


def test_campaign_verify_steps_reach_the_runner(monkeypatch, tmp_path):
    """A campaign's own verify steps must actually be executed.

    They were accepted by RunManager.start() and then dropped before
    tr.run_plan by a later edit, so `vision_was_in_the_loop` never ran: two
    hover trials that never had vision in the loop were written as usable
    coverage — precisely the wrong-result failure that check exists to
    prevent, and invisible because a missing check looks exactly like a
    passing one.
    """
    import sys
    from pathlib import Path
    pytest.importorskip("flask")
    from robofacts.ui import app as ui
    from robofacts import trial_runner as tr
    seen = {}

    def fake_run_plan(plan, config, bags_dir, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(tr, "run_plan", fake_run_plan)
    runs = ui.RunManager()
    journal = ui.RunJournal()
    steps = [{"name": "vision_was_in_the_loop", "command": "true {bag}"}]

    class _Plan:
        needed = 1
        cells = []

    run = runs.start(experiment="e", campaign_name="c", campaign_file="c", plan=_Plan(),
                     config=None, bags_dir=tmp_path, executed=True, limit=1,
                     journal=journal, extra_verify=steps)
    for _ in range(50):
        if run.state != "running":
            break
        time.sleep(0.05)
    assert seen.get("extra_verify") == steps, (
        "the campaign's verify steps never reached run_plan — a campaign can ask for a "
        "check that silently does not run")


# --------------------------------------------------------------------------- #
# The rework (#131): one row per trial, every trial, everything about it
# --------------------------------------------------------------------------- #
def _slim(root: Path, name: str, folder: str = "bags_slim") -> None:
    slim = root / "expA" / folder / f"{name}_slim"
    slim.mkdir(parents=True)
    (slim / f"{name}_slim_0.mcap").write_bytes(b"SLIM")


def _meta(name: str, **extra) -> dict:
    meta = {"trial_id": name, "execution_host": "laptop", "aborted": False, "trajectory": "hover"}
    meta.update(extra)
    return meta


def test_the_experiment_page_lists_every_trial_with_no_cap(client, root):
    """The Viewers page's cap of five per folder hid 20 of 24 trials (2026-10-15)."""
    names = [f"20261015T09{i:02d}00_rep{i:02d}" for i in range(12)]
    for i, name in enumerate(names):
        _write_bag(root, name, _meta(name, trajectory="hover" if i % 2 else "circle"))
    body = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    assert all(name in body for name in names)
    assert body.count('<td class="trial">') == 12, "one row per trial"
    assert "trials records expA" in body, "the page names the command that prints the same list"


def test_the_trial_row_and_page_carry_replay_layout_video_analyses_and_logs(client, root):
    name = "20261015T100000_rep01"
    _write_bag(root, name, _meta(name))
    _slim(root, name)
    (root / "expA" / "config" / "foxglove_demo.json").write_text("{}", encoding="utf-8")
    (root / "expA" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "expA" / "runs" / f"{name}.log").write_text(
        "[verify] flew\n[verify] flew rc=0 in 1.2 s\n", encoding="utf-8")
    res = root / "expA" / "results" / "20261015T110000_quick_look"
    (res / name).mkdir(parents=True)
    (res / name / "trial_video_h264.mp4").write_bytes(b"MP4")
    (res / "analysis_metadata.json").write_text(json.dumps(
        {"analysis": "quick_look", "bags": [name], "generated_at": "20261015T110000"}), encoding="utf-8")

    row = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    page = client.get(f"/e/expA/t/{name}", headers=auth()).get_data(as_text=True)
    for body in (row, page):
        # The & is escaped in the attribute, as it must be; the browser decodes it.
        assert f"foxglove://open?ds=remote-file&amp;ds.url=http://localhost/bag/expA/{name}_slim.mcap" in body
        assert "/layout/expA/foxglove_demo.json" in body, "the layout sits beside the bag"
        assert f"results/20261015T110000_quick_look/{name}/trial_video_h264.mp4" in body
        assert "quick_look" in body
        assert f"/files/expA/runs/{name}.log" in body
        assert f"/files/expA/bags/{name}/trial_metadata.json" in body
    assert "<video" in page and "<code>flew</code>" in page, "the video plays, the verify report lists the step"
    assert client.get(f"/bag/expA/{name}_slim.mcap").get_data() == b"SLIM"


def test_the_filters_are_the_cli_flags_and_the_page_prints_the_command(client, root):
    _write_bag(root, "t_ok", _meta("t_ok"))
    _write_bag(root, "t_circle", _meta("t_circle", trajectory="circle"))
    _write_bag(root, "t_abort", _meta("t_abort", aborted=True, abort_reason="rc=2"))
    body = client.get("/e/expA?status=aborted", headers=auth()).get_data(as_text=True)
    assert "t_abort" in body and "t_ok<" not in body and "t_circle<" not in body
    assert "trials records expA --status aborted" in body
    body = client.get("/e/expA?cell=trajectory%3Dcircle", headers=auth()).get_data(as_text=True)
    assert "t_circle" in body and "t_ok<" not in body
    assert "--where trajectory=circle" in body


def test_blocked_attempts_and_scratch_flights_are_listed(client, root):
    (root / "expA" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "expA" / "runs" / "attempts.jsonl").write_text(json.dumps(
        {"trial_label": "t_nogo", "campaign": "c", "status": "blocked", "values": {"trajectory": "hover"},
         "detail": "pre-flight gate FAIL: topic_rate", "gate": {"verdict": "NO-GO", "failed_checks": ["topic_rate"]}})
        + "\n", encoding="utf-8")
    scratch = root / "expA" / "bags_scratch" / "20261015T120000_scratch_x"
    scratch.mkdir(parents=True)
    (scratch / "x_0.mcap").write_bytes(b"x")
    _slim(root, "20261015T120000_scratch_x", folder="bags_scratch")
    body = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    assert "t_nogo" in body and "b-blocked" in body
    assert "20261015T120000_scratch_x" in body and "never coverage" in body
    camp = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert '<div class="k">blocked attempts</div><div class="v">1</div>' in camp


def test_the_campaign_page_shows_the_gate_outcomes(client, root):
    (root / "expA" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "expA" / "runs" / "preflight_gate.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": "2026-10-15T09:00:00", "campaign": "c", "trial_label": "t1", "verdict": "GO"},
        {"ts": "2026-10-15T09:05:00", "campaign": "c", "trial_label": "t2", "verdict": "NO-GO",
         "failed": [{"check": "disk_space", "detail": "1 GB free"}]},
    ]) + "\n", encoding="utf-8")
    body = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert "1 GO" in body and "1 NO-GO" in body
    assert "disk_space: 1 GB free" in body


def test_the_files_route_is_read_only_inside_the_experiment(client, root):
    res = root / "expA" / "results" / "r1"
    res.mkdir(parents=True)
    (res / "table.md").write_text("| a |", encoding="utf-8")
    _write_bag(root, "t1", _meta("t1"))
    assert client.get("/files/expA/results/r1/table.md", headers=auth()).get_data() == b"| a |"
    assert "table.md" in client.get("/files/expA/results/r1/", headers=auth()).get_data(as_text=True)
    assert client.get("/files/expA/results/r1/table.md").status_code == 401, "behind auth"
    assert client.get("/files/expA/bags/t1/t1_0.mcap", headers=auth()).status_code == 403, \
        "recordings go through /bag (slim only), never here"
    assert client.get("/files/expA/config/runner.yaml", headers=auth()).status_code == 200
    for attempt in ("/files/expA/..%2f..%2fsecret", "/files/expA/../common/config/robots.yaml",
                    "/files/expA/scripts/x.sh"):
        assert client.get(attempt, headers=auth()).status_code in (403, 404), attempt


def test_the_records_api_is_the_package_function(client, root):
    from robofacts import trial_records as trec
    _write_bag(root, "t1", _meta("t1"))
    _write_bag(root, "t2", _meta("t2", aborted=True))
    api = json.loads(client.get("/api/records/expA.json?status=aborted", headers=auth())
                     .get_data(as_text=True))
    expected = trec.filter_records(trec.load_records(root, "expA"), status="aborted")
    assert [r["name"] for r in api["records"]] == [r.name for r in expected] == ["t2"]


def test_the_old_addresses_redirect(client):
    response = client.get("/campaign/expA/c?include_unverified=1", headers=auth())
    assert response.status_code == 302 and response.headers["Location"].endswith(
        "/e/expA/c/c?include_unverified=1")
    assert client.get("/analyses/expA", headers=auth()).headers["Location"].endswith("/e/expA/analyses")


def test_every_page_renders_with_no_trial_at_all(client):
    for path in ("/", "/e/expA", "/e/expA/c/c", "/e/expA/c/steps", "/e/expA/analyses",
                 "/files/expA/", "/viewers", "/journal"):
        assert client.get(path, headers=auth()).status_code == 200, path
    assert client.get("/e/expA/t/nope", headers=auth()).status_code == 404
    assert client.get("/e/nope", headers=auth()).status_code == 404


def test_the_cohort_preview_is_a_dry_run(client, root):
    """`trials analyze` without --execute: the bags and commands, and no results folder."""
    _write_bag(root, "t1", _meta("t1"))
    _write_bag(root, "t2", _meta("t2", trajectory="circle"))
    body = client.get("/e/expA/analyses?analysis=quick_look&cell=trajectory%3Dcircle",
                      headers=auth()).get_data(as_text=True)
    assert "<b>1</b> bag(s) selected" in body and "echo" in body
    assert "trials analyze expA quick_look --where trajectory=circle --execute" in body
    assert not (root / "expA" / "results").exists(), "a preview writes nothing"


def test_quarantine_moves_the_slim_copy_with_its_bag(client, root):
    _write_bag(root, "t1", _meta("t1"))
    _slim(root, "t1")
    response = client.post("/quarantine/expA/t1", headers=auth(), data={"back": "/e/expA"})
    assert response.status_code == 302
    assert (root / "expA" / "bags_issues" / "t1").is_dir()
    assert (root / "expA" / "bags_issues_slim" / "t1_slim").is_dir()
    assert not (root / "expA" / "bags_slim" / "t1_slim").exists()
    body = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    assert "b-quarantined" in body, "the page shows it at once, not after the cache expires"


def test_a_dry_run_of_a_covered_campaign_says_so(client, root):
    _write_bag(root, "t1", _meta("t1"))   # the steps campaign wants one hover trial
    final = _wait(client, _start_run(client, campaign="steps"))
    assert final["wanted"] == 0
    assert any("Nothing to run" in line["text"] for line in final["log"]), \
        "the CLI's own line, so an empty run page says why"


def test_every_page_has_the_theme_switch_and_both_themes(client):
    """Dark mode (2026-10-16): Auto follows the OS, the switch stores dark or light, and
    the stored choice is applied before the first paint (no white flash)."""
    body = client.get("/", headers=auth()).get_data(as_text=True)
    assert 'id="themetoggle"' in body
    assert body.index("trialsTheme") < body.index("app.css"), "the theme is set before the CSS loads"
    css = client.get("/static/app.css").get_data(as_text=True)
    assert ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css
    assert "--solid-bad" in css, "buttons keep white text on a solid fill in both themes"


def test_the_ui_speaks_the_papers_words(client, root):
    """Class, not status; still missing, not still to fly; run, not fly (2026-10-16)."""
    _write_bag(root, "t_abort", _meta("t_abort", aborted=True))
    body = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    assert 'title="the paper\'s class' in body and "b-excluded" in body and "b-aborted" in body
    assert "still missing" in body and "still to fly" not in body
    campaign = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert "EXECUTE: run the trial(s)" in campaign and "fly the trial" not in campaign
    assert "run the action" in campaign


# --------------------------------------------------------------------------- #
# Explore (explore.py, 2026-10-16): a live stack, never coverage
# --------------------------------------------------------------------------- #
def _simulated(root: Path) -> None:
    path = root / "expA" / "config" / "runner.yaml"
    path.write_text("simulation: true\n" + RUNNER, encoding="utf-8")


def _wait_state(client, run_id, states, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = client.get(f"/api/run/{run_id}.json", headers=auth()).get_json()
        if d["state"] in states:
            return d
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached {states}: {d['state']}")


def test_explore_is_refused_on_hardware_without_a_block(client):
    r = client.post("/explore", data={"experiment": "expA", "campaign": "c"}, headers=auth())
    assert r.status_code == 400 and b"explore:" in r.data
    page = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert "Not available" in page


def test_explore_runs_until_stop_and_shows_the_changes(client, root, monkeypatch):
    from robofacts import explore as ex
    _simulated(root)
    seen = {}

    def fake_run_explore(campaign, config, experiments_root, **kw):
        seen.update(kw, campaign=campaign)
        out = experiments_root / "expA" / "runs" / "explore" / "x_explore_c"
        out.mkdir(parents=True)
        kw["on_ready"]({"url": "ws://localhost:8765", "deep_link": "foxglove://x", "web_link": "w",
                        "tunnel": "", "host": "this machine", "port": 8765, "writable": [".*"]})
        print("[explore] READY")
        kw["stop"].wait(5)
        spec = next(campaign.combinations())
        return ex.ExploreResult(spec, "explored", "", out, [], summary={
            "changed": [{"node": "/stack", "parameter": "kp", "from": 1, "to": 2, "times_changed": 1}],
            "reverted": []})

    monkeypatch.setattr(ex, "run_explore", fake_run_explore)
    page = client.get("/", headers=auth()).get_data(as_text=True)
    assert "🔭 Explore" in page
    r = client.post("/explore", data={"experiment": "expA", "campaign": "c",
                                      "pin": "trajectory=circle"}, headers=auth())
    assert r.status_code == 302, r.data
    run_id = r.headers["Location"].rsplit("/", 1)[1]
    d = _wait_state(client, run_id, ("running",))
    assert d["kind"] == "explore"
    deadline = time.monotonic() + 5
    while not client.get(f"/api/run/{run_id}.json", headers=auth()).get_json()["explore"].get("ready"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # The picked cell went through the --set path.
    assert next(seen["campaign"].combinations()).values["trajectory"] == "circle"
    assert seen["dry_run"] is False
    page = client.get(f"/run/{run_id}", headers=auth()).get_data(as_text=True)
    assert "Explore: c" in page and "Stop exploring" in page
    # One stack at a time: neither a second exploration nor an EXECUTE may start.
    assert client.post("/explore", data={"experiment": "expA", "campaign": "c"},
                       headers=auth()).status_code == 409
    assert client.post("/run", data={"experiment": "expA", "campaign": "c", "go": "execute",
                                     "confirm": "yes"}, headers=auth()).status_code == 409
    client.post(f"/run/{run_id}/abort", headers=auth())
    d = _wait_state(client, run_id, ("finished", "error"))
    assert d["state"] == "finished", d["error"]
    assert d["explore"]["changed"][0]["parameter"] == "kp"
    assert d["explore"]["out_dir"] == "runs/explore/x_explore_c"


def test_explore_on_hardware_needs_the_arm(client, root):
    path = root / "expA" / "config" / "runner.yaml"
    path.write_text("simulation: false\nexplore:\n  param_whitelist: ['^/cf1/kp$']\n" + RUNNER,
                    encoding="utf-8")
    r = client.post("/explore", data={"experiment": "expA", "campaign": "c"}, headers=auth())
    assert r.status_code == 400 and "arm".encode() in r.data
    page = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert 'name="explore_confirm"' in page


def test_the_explore_run_page_drives_actions_records_and_notes(client, root, monkeypatch):
    """#133: Foxglove opens by itself, action buttons, a live table, record and notes."""
    import json as _json
    from types import SimpleNamespace
    from robofacts import explore as ex
    path = root / "expA" / "config" / "runner.yaml"
    path.write_text("simulation: true\nexplore:\n  actions:\n"
                    "    - {name: take_off, command: 'python3 fly.py --mode take_off'}\n" + RUNNER,
                    encoding="utf-8")
    (root / "expA" / "config" / "foxglove_demo.json").write_text("{}", encoding="utf-8")
    started, foreground, signalled = [], [], []

    class Proc:
        rc = None
        pid = 0

        def poll(self):
            return self.rc

    def start(cmd, target, *, cwd, label="", quiet_sink=None):
        started.append((label, cmd))
        return Proc()

    def stop(proc, target, *, name, kind):
        proc.rc = 0

    hooks = SimpleNamespace(start_background=start, stop_process=stop,
                            run_foreground=lambda cmd, target, *, cwd, check=True: (
                                foreground.append(cmd), SimpleNamespace(returncode=0, stdout=""))[1])

    def fake_run_explore(campaign, config, experiments_root, **kw):
        spec = next(campaign.combinations())
        out = experiments_root / "expA" / "runs" / "explore" / "x_explore_c"
        out.mkdir(parents=True)
        (out / "parameter_events.jsonl").write_text(
            '[params] {"node": "/talker", "name": "rate", "kind": "initial", "value": 1.0}\n'
            '[params] {"node": "/talker", "name": "rate", "kind": "changed", "value": 4.0}\n',
            encoding="utf-8")
        controls = kw["controls"]
        controls._terminate = lambda proc, target, cmd: (signalled.append(cmd), setattr(proc, "rc", 2))
        controls._slim = lambda *a, **k: None
        controls.bind(hooks=hooks, target=None, cwd=experiments_root, out_dir=out, spec=spec,
                      factors=campaign.factors, config=config, settings=ex.settings(config),
                      clock=lambda: "20261017T120000", experiment="expA",
                      experiments_root=experiments_root, placeholders={})
        kw["on_ready"]({"url": "ws://localhost:8765", "deep_link": "foxglove://open?x", "web_link": "w",
                        "tunnel": "", "host": "this machine", "port": 8765, "writable": [".*"]})
        kw["stop"].wait(10)
        controls.shutdown()
        return ex.ExploreResult(spec, "explored", "", out, [])

    monkeypatch.setattr(ex, "run_explore", fake_run_explore)
    r = client.post("/explore", data={"experiment": "expA", "campaign": "c"}, headers=auth())
    run_id = r.headers["Location"].rsplit("/", 1)[1]
    deadline = time.monotonic() + 5
    while not (client.get(f"/api/run/{run_id}.json", headers=auth()).get_json()["explore"]
               .get("live", {}).get("live")):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    page = client.get(f"/run/{run_id}", headers=auth()).get_data(as_text=True)
    # Foxglove by itself (a checkbox remembered per browser), the layout beside it, the buttons.
    assert 'id="autoopen"' in page and "robofacts.explore.autoOpenFoxglove" in page
    assert "foxglove_demo.json" in page and 'data-name="take_off"' in page
    assert "● Record" in page and "Mark this moment" in page
    ctl = f"/run/{run_id}/explore/"
    assert client.post(ctl + "action", data={"name": "take_off"}).status_code == 401
    r = client.post(ctl + "action", data={"name": "take_off"}, headers=auth())
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    assert started[-1] == ("action:take_off", ["python3", "fly.py", "--mode", "take_off"])
    r = client.post(ctl + "action", data={"name": "take_off"}, headers=auth())
    assert r.status_code == 400 and "still running" in r.get_json()["message"]
    assert client.post(ctl + "action_stop", headers=auth()).get_json()["ok"]
    assert signalled == [["python3", "fly.py", "--mode", "take_off"]]
    # The live table, and its set back.
    live = client.get(f"/api/run/{run_id}.json", headers=auth()).get_json()["explore"]["live"]
    assert live["changes"]["changed"][0]["to"] == 4.0 and live["changes"]["changed"][0]["settable"]
    assert client.post(ctl + "set_back", data={"node": "/talker", "parameter": "rate"},
                       headers=auth()).get_json()["ok"]
    assert foreground[-1] == ["ros2", "param", "set", "/talker", "rate", "1.0"]
    # Record, note, mark, stop recording.
    assert client.post(ctl + "record", data={"label": "first"}, headers=auth()).get_json()["ok"]
    assert client.post(ctl + "note", data={"text": "looks right"}, headers=auth()).get_json()["ok"]
    assert client.post(ctl + "note", data={"mark": "yes"}, headers=auth()).get_json()["ok"]
    r = client.post(ctl + "record_stop", headers=auth())
    assert r.get_json()["ok"], r.get_json()
    bag = root / "expA" / "bags_scratch" / "20261017T120000_explore_first"
    meta = _json.loads((bag / "trial_metadata.json").read_text())
    assert meta["scratch"] and meta["explore"] and meta["explore_parameters_at_start"]["/talker.rate"] == 4.0
    notes = (root / "expA" / "runs" / "explore" / "x_explore_c" / "notes.md").read_text()
    assert "looks right" in notes and "● mark" in notes and "[recording 20261017T120000_explore_first" in notes
    assert client.post(ctl + "nope", headers=auth()).status_code == 404
    client.post(f"/run/{run_id}/abort", headers=auth())
    d = _wait_state(client, run_id, ("finished", "error"))
    assert d["state"] == "finished", d["error"]
    # Kept after Stop: the recording and the notes stay listed.
    assert d["explore"]["live"]["recordings"][0]["name"] == bag.name
    assert len(d["explore"]["live"]["notes"]) == 2
    assert client.post(ctl + "note", data={"text": "late"}, headers=auth()).status_code == 409
    # Keep it? works after Stop, from the last change table, and applies nothing.
    r = client.post(ctl + "propose", data={"node": "/talker", "parameter": "rate"}, headers=auth())
    assert r.status_code == 200 and r.get_json()["proposal"]["to"] == 4.0, r.get_json()
    assert (root / "expA" / "runs" / "explore" / "x_explore_c" / "proposed_changes.md").is_file()


def test_restart_with_another_condition_waits_for_the_old_stack(client, root, monkeypatch):
    """#133: the run page's picker stops this exploration, then explores the picked cell."""
    from robofacts import explore as ex
    _simulated(root)
    conditions, stopped = [], []

    def fake_run_explore(campaign, config, experiments_root, **kw):
        spec = next(campaign.combinations())
        conditions.append(spec.values["trajectory"])
        kw["on_ready"]({"url": "ws://localhost:8765", "deep_link": "foxglove://x", "web_link": "w",
                        "tunnel": "", "host": "this machine", "port": 8765, "writable": [".*"]})
        kw["stop"].wait(10)
        stopped.append(spec.values["trajectory"])
        return ex.ExploreResult(spec, "explored", "", None, [])

    monkeypatch.setattr(ex, "run_explore", fake_run_explore)
    r = client.post("/explore", data={"experiment": "expA", "campaign": "c"}, headers=auth())
    first = r.headers["Location"].rsplit("/", 1)[1]
    _wait_state(client, first, ("running",))
    page = client.get(f"/run/{first}", headers=auth()).get_data(as_text=True)
    assert 'name="f_trajectory"' in page and "hover (now)" in page and "Restart with this condition" in page
    # Only levels of the campaign are accepted.
    assert client.post(f"/run/{first}/explore_restart", data={"f_trajectory": "loop"},
                       headers=auth()).status_code == 400
    r = client.post(f"/run/{first}/explore_restart", data={"f_trajectory": "circle"}, headers=auth())
    assert r.status_code == 302, r.data
    second = r.headers["Location"].rsplit("/", 1)[1]
    assert second != first
    d = _wait_state(client, first, ("finished",))
    deadline = time.monotonic() + 5
    while len(conditions) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # The old stack was down before the new one started.
    assert conditions == ["hover", "circle"] and stopped[0] == "hover"
    d = client.get(f"/api/run/{second}.json", headers=auth()).get_json()
    assert d["explore"]["condition"]["trajectory"] == "circle" and d["explore"]["after"] == first
    assert "trajectory=circle" in d["campaign"]
    client.post(f"/run/{second}/abort", headers=auth())
    _wait_state(client, second, ("finished",))
    # From a stopped exploration, the same form starts a new one at once.
    r = client.post(f"/run/{second}/explore_restart", data={"f_trajectory": "hover"}, headers=auth())
    third = r.headers["Location"].rsplit("/", 1)[1]
    _wait_state(client, third, ("running",))
    assert client.get(f"/api/run/{third}.json", headers=auth()).get_json()["explore"]["after"] == ""
    client.post(f"/run/{third}/abort", headers=auth())
    _wait_state(client, third, ("finished",))


# --------------------------------------------------------------------------- #
# Table or cards (#137): the same records, one card each, with the video's still
# --------------------------------------------------------------------------- #
def _video(path: Path, seconds: float = 4.0) -> Path:
    import shutil
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg") is None:
        path.write_bytes(b"MP4")
        return path
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=320x180:rate=10:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _video_trial(root: Path, name: str) -> Path:
    _write_bag(root, name, _meta(name))
    res = root / "expA" / "results" / "20261015T110000_quick_look"
    video = _video(res / name / "trial_video_h264.mp4")
    meta = res / "analysis_metadata.json"
    bags = json.loads(meta.read_text())["bags"] if meta.is_file() else []
    meta.write_text(json.dumps({"analysis": "quick_look", "bags": bags + [name],
                                "generated_at": "20261015T110000"}), encoding="utf-8")
    return video


def test_the_trials_list_offers_a_card_per_row_and_the_table_stays_the_default(client, root):
    import re
    _video_trial(root, "20261015T100000_rep01")
    _write_bag(root, "20261015T090000_rep02", _meta("20261015T090000_rep02"))
    body = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    assert '<div class="viewbar" data-views="triallist" hidden>' in body, \
        "the switch appears only once app.js runs, so the page without it is the table"
    assert re.search(r'<div class="tcards" id="triallist-cards" hidden>', body), "cards start hidden"
    rows = re.findall(r'<tr data-trial="([^"]+)">', body)
    cards = re.findall(r'<article class="tcard" data-trial="([^"]+)">', body)
    assert rows == cards == ["20261015T100000_rep01", "20261015T090000_rep02"], \
        "one card per row, in the table's order (the script keeps it on sort)"
    poster = ("/files/expA/runs/posters/results/20261015T110000_quick_look/"
              "20261015T100000_rep01/trial_video_h264.mp4.jpg")
    assert f'data-src="{poster}"' in body and 'loading="lazy"' in body
    assert f'src="{poster}"' not in body.replace(f'data-src="{poster}"', ""), \
        "no still is fetched while the table is shown"
    assert 'data-video="/files/expA/results/20261015T110000_quick_look/20261015T100000_rep01/trial_video_h264.mp4"' in body
    assert '<div class="poster none"' in body, "a trial without video still has its card"
    card = body[body.index('<article class="tcard" data-trial="20261015T100000_rep01">'):]
    card = card[:card.index("</article>")]
    for piece in ("/e/expA/t/20261015T100000_rep01", "b-usable", "quick_look",
                  "/files/expA/bags/20261015T100000_rep01/trial_metadata.json"):
        assert piece in card, piece


def test_the_campaign_page_and_the_scratch_list_have_the_switch_too(client, root):
    _video_trial(root, "20261015T100000_rep01")
    scratch = root / "expA" / "bags_scratch" / "20261015T120000_scratch_x"
    scratch.mkdir(parents=True)
    (scratch / "x_0.mcap").write_bytes(b"x")
    camp = client.get("/e/expA/c/c", headers=auth()).get_data(as_text=True)
    assert 'data-views="triallist"' in camp and 'id="triallist-cards"' in camp
    exp = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    assert 'data-views="scratchlist"' in exp and 'id="scratchlist-cards"' in exp
    assert '<article class="tcard" data-trial="20261015T120000_scratch_x">' in exp


def test_the_switch_is_remembered_per_viewer_and_survives_a_private_window():
    js = (Path(ui.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "localStorage.getItem(VIEW_KEY)" in js and "localStorage.setItem(VIEW_KEY" in js
    view = js[js.index("table or cards (#137)"):]
    view = view[:view.index("Click a still")]
    assert view.count("try {") >= 2, "every storage access is guarded: a private window throws"
    assert "get('view')" in view, "?view=cards (or table) wins, so a link can open the cards"
    assert "trials:changed" in js, "sorting and the find box tell the cards to follow"


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_files_route_makes_the_poster_on_first_request_and_caches_it(client, root):
    name = "20261015T100000_rep01"
    _video_trial(root, name)
    url = (f"/files/expA/runs/posters/results/20261015T110000_quick_look/{name}/"
           "trial_video_h264.mp4.jpg")
    assert client.get(url).status_code == 401, "behind auth like every file"
    first = client.get(url, headers=auth())
    assert first.status_code == 200 and first.mimetype == "image/jpeg"
    assert first.get_data()[:2] == b"\xff\xd8"
    cached = (root / "expA" / url.removeprefix("/files/expA/"))
    assert cached.is_file(), "cached under runs/posters/, which git ignores"
    page = client.get(f"/e/expA/t/{name}", headers=auth()).get_data(as_text=True)
    assert f'poster="{url}"' in page, "the trial page's player shows the same still"


def test_a_poster_is_only_made_for_a_served_video(client, root):
    (root / "expA" / "scripts").mkdir(parents=True)
    (root / "expA" / "scripts" / "x.mp4").write_bytes(b"MP4")
    for rel in ("runs/posters/results/none.mp4.jpg",          # no such video
                "runs/posters/scripts/x.mp4.jpg",             # not a served folder
                "runs/posters/runs/posters/a.mp4.jpg",        # a poster of a poster
                "runs/posters/..%2f..%2fsecret.mp4.jpg"):
        assert client.get(f"/files/expA/{rel}", headers=auth()).status_code in (403, 404), rel


def test_no_id_is_used_twice_on_the_trials_pages(client, root):
    """Regression (2026-10-20): the heading <h2 id="trials"> and the table id="trials"
    shared one id, so getElementById found the heading: the find box matched nothing
    and the card switch could not hide the table."""
    import collections
    import re
    _video_trial(root, "20261015T100000_rep01")
    for url in ("/e/expA", "/e/expA/c/c"):
        body = client.get(url, headers=auth()).get_data(as_text=True)
        ids = collections.Counter(re.findall(r'\sid="([^"]+)"', body))
        assert [i for i, n in ids.items() if n > 1] == [], url


# --------------------------------------------------------------------------- #
# Stills, the camera video and the plot fallback (#138)
# --------------------------------------------------------------------------- #
def _camera_trial(root: Path, name: str, n_stills: int = 3, video: bool = True) -> Path:
    """A trial whose declared camera was already turned into stills (trial_media's cache)."""
    _write_bag(root, name, _meta(name))
    (root / "expA" / "config" / "trial_media.yaml").write_text(
        "camera: {topic: /drone/camera/image, label: drone camera, stills: 3}\n", encoding="utf-8")
    folder = root / "expA" / "runs" / "camera" / name
    folder.mkdir(parents=True)
    for i in range(1, n_stills + 1):
        (folder / f"still_{i:02d}.jpg").write_bytes(b"\xff\xd8 still")
    if video:
        (folder / "camera.mp4").write_bytes(b"MP4")
    (folder / "camera.json").write_text(json.dumps({"label": "drone camera"}), encoding="utf-8")
    return folder


def _card(body: str, name: str) -> str:
    card = body[body.index(f'<article class="tcard" data-trial="{name}">'):]
    return card[:card.index("</article>")]


def test_a_card_scrubs_the_stills_and_plays_the_labeled_camera_video(client, root):
    import html
    import re
    name = "20261015T100000_rep01"
    _camera_trial(root, name)
    card = _card(client.get("/e/expA", headers=auth()).get_data(as_text=True), name)
    base = f"/files/expA/runs/camera/{name}"
    assert f'data-video="{base}/camera.mp4"' in card, "no trial video: the camera video plays"
    assert "📷 drone camera" in card, "labeled, so nobody takes it for the trial video"
    stills = json.loads(html.unescape(re.search(r"data-stills='([^']+)'", card).group(1)))
    assert stills == [f"{base}/still_0{i}.jpg" for i in (1, 2, 3)]
    assert f'data-src="{base}/still_01.jpg"' in card and f'src="{base}' not in card.replace("data-src", ""), \
        "the first still waits in data-src like a poster"
    assert 'class="scrub"' in card and "· drone camera" in card, "the dialog title names the camera"


def test_a_trial_video_still_wins_over_the_camera_on_the_card(client, root):
    name = "20261015T100000_rep01"
    _video_trial(root, name)
    _camera_trial(root, "20261015T090000_rep02")
    folder = root / "expA" / "runs" / "camera" / name
    folder.mkdir(parents=True)
    (folder / "camera.mp4").write_bytes(b"MP4")
    (folder / "camera.json").write_text("{}", encoding="utf-8")
    card = _card(client.get("/e/expA", headers=auth()).get_data(as_text=True), name)
    assert "trial_video_h264.mp4" in card.split('data-video="')[1].split('"')[0]
    assert "data-stills" not in card and 'class="camtag"' not in card
    assert "📷 drone camera" in card, "the camera video is still one link away"


def test_stills_without_a_video_link_to_the_trial_page(client, root):
    name = "20261015T100000_rep01"
    _camera_trial(root, name, video=False)
    card = _card(client.get("/e/expA", headers=auth()).get_data(as_text=True), name)
    assert f'<a class="poster" data-layer="0" href="/e/expA/t/{name}"' in card and "data-video" not in card


def test_a_card_has_a_tag_per_picture_and_only_the_first_layer_shows(client, root):
    import re
    name, other = "20261015T100000_rep01", "20261015T090000_rep02"
    _camera_trial(root, name)
    _write_bag(root, other, _meta(other))
    folder = root / "expA" / "results" / "20261015T120000_vio_drift" / name
    folder.mkdir(parents=True)
    (folder / "vio_drift.png").write_bytes(b"\x89PNG")
    cohort = root / "expA" / "results" / "20261015T130000_cohort"
    cohort.mkdir(parents=True)
    (cohort / "analysis_metadata.json").write_text(json.dumps(
        {"analysis": "cohort", "bags": [name, other]}), encoding="utf-8")
    (cohort / "all.png").write_bytes(b"\x89PNG")
    body = client.get("/e/expA", headers=auth()).get_data(as_text=True)
    card = _card(body, name)
    tags = re.findall(r'<button type="button" class="ptab" data-layer="(\d)" aria-pressed="(\w+)"[^>]*>([^<]+)</button>', card)
    assert tags == [("0", "true", "📷 drone camera"), ("1", "false", "📈 vio_drift"),
                    ("2", "false", "📈 cohort · all")], "camera first, own plot, then the cohort's"
    layers = re.findall(r'class="poster[^"]*" data-layer="(\d)"( hidden)?', card)
    assert layers == [("0", ""), ("1", " hidden"), ("2", " hidden")]
    assert 'data-src="/files/expA/results/20261015T120000_vio_drift/' + name + '/vio_drift.png"' in card
    card = _card(body, other)
    assert 'class="ptab"' not in card and '<span class="camtag">📈 cohort · all</span>' in card, \
        "one picture, one plain tag"
    assert "cohort of 2 trials" in client.get(f"/e/expA/t/{other}", headers=auth()).get_data(as_text=True)
    served = client.get(f"/files/expA/results/20261015T120000_vio_drift/{name}/vio_drift.png", headers=auth())
    assert served.status_code == 200 and served.mimetype == "image/png"
    js = (Path(ui.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "closest('[data-layer][hidden]')" in js, "a hidden layer's image waits for its tag"


def test_the_trial_page_shows_the_camera_video_and_every_still(client, root):
    name = "20261015T100000_rep01"
    _camera_trial(root, name)
    page = client.get(f"/e/expA/t/{name}", headers=auth()).get_data(as_text=True)
    base = f"/files/expA/runs/camera/{name}"
    assert "📷 Drone camera" in page and "not the trial video" in page
    assert f'src="{base}/camera.mp4" poster="{base}/still_01.jpg"' in page
    assert page.count('class="stillstrip"') == 1 and all(
        f'src="{base}/still_0{i}.jpg"' in page for i in (1, 2, 3))
    assert client.get(f"{base}/camera.mp4", headers=auth()).mimetype == "video/mp4"
    assert client.get(f"{base}/still_02.jpg").status_code == 401, "behind auth like every file"


def test_the_script_scrubs_stills_on_pointer_move():
    js = (Path(ui.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    scrub = js[js.index("Stills of the declared camera (#138)"):]
    assert "pointermove" in scrub and "data-stills" in scrub and "pointerout" in scrub
    assert "img.hasAttribute('data-src')" in scrub, "no still is fetched before the cards are shown"


# --------------------------------------------------------------------------- #
# View-only mode: the home server's archive viewer (2026-10-20)
# --------------------------------------------------------------------------- #
@pytest.fixture
def poster_cache(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("poster_cache")


@pytest.fixture
def viewer(root: Path, poster_cache: Path):
    app = ui.create_app(root, PASSWORD, repo_root=root.parent, view_only=True,
                        cache_dir=poster_cache)
    app.config["TESTING"] = True
    return app.test_client()


ACTING_POSTS = ["/run", "/explore", "/run/r1/explore_restart", "/run/r1/explore/set",
                "/preflight/expA", "/operator_step", "/run/r1/abort", "/run/r1/proceed",
                "/stop", "/robot_action", "/kill_ros", "/quarantine/expA/t1"]


def test_view_only_registers_no_route_that_changes_anything(root):
    """The guarantee is the URL map itself, not hidden buttons: nothing but GET."""
    app = ui.create_app(root, PASSWORD, repo_root=root.parent, view_only=True)
    for rule in app.url_map.iter_rules():
        assert rule.methods <= {"GET", "HEAD", "OPTIONS"}, (rule.rule, rule.methods)
    full = ui.create_app(root, PASSWORD, repo_root=root.parent)
    posts = {r.rule for r in full.url_map.iter_rules() if "POST" in r.methods}
    assert len(posts) == len(ACTING_POSTS), "a new acting route must join ACTING_POSTS"


@pytest.mark.parametrize("url", ACTING_POSTS)
def test_view_only_answers_every_action_with_not_found(viewer, url):
    resp = viewer.post(url, headers=auth(), data={"action": "estop", "confirm": "yes",
                                                  "go": "execute", "experiment": "expA",
                                                  "campaign": "c.yaml"})
    assert resp.status_code in (404, 405), (url, resp.status_code)


def test_view_only_pages_render_without_any_control(viewer, root):
    name = "20261015T100000_rep01"
    _write_bag(root, name, _meta(name))
    _slim(root, name)
    pages = ["/", "/e/expA", "/e/expA/c/c", f"/e/expA/t/{name}", "/e/expA/analyses",
             "/viewers", "/journal", "/files/expA/"]
    for url in pages:
        resp = viewer.get(url, headers=auth())
        assert resp.status_code == 200, (url, resp.status_code)
        body = resp.get_data(as_text=True)
        assert 'method="post"' not in body.lower(), url
        for word in ("E-STOP ALL", "🧹 Kill ROS", "▶ EXECUTE", "🗑 Quarantine", "🔭 Explore"):
            assert word not in body, (url, word)
        assert "View only." in body, url


def test_view_only_still_needs_the_password(viewer):
    assert viewer.get("/").status_code == 401


def test_the_view_only_banner_shows_the_archive_status(viewer, root):
    (root / ui.ARCHIVE_STATUS_FILE).write_text(json.dumps({
        "merged": "2026-10-20T03:20:00", "collisions": 2,
        "hosts": {"laptop": {"ok": True, "finished": "2026-10-20T02:00:00"},
                  "orin": {"ok": False, "finished": "2026-10-18T03:15:00", "note": "unreachable"}}}),
        encoding="utf-8")
    body = viewer.get("/", headers=auth()).get_data(as_text=True)
    assert "Merged 2026-10-20 03:20 UTC" in body and "2 file(s) skipped" in body
    assert "laptop: 2026-10-20 02:00" in body and "orin: 2026-10-18 03:15 ⚠" in body


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_view_only_posters_go_to_the_cache_dir_not_the_tree(viewer, root, poster_cache):
    name = "20261015T100000_rep01"
    _video_trial(root, name)
    rel = f"runs/posters/results/20261015T110000_quick_look/{name}/trial_video_h264.mp4.jpg"
    resp = viewer.get(f"/files/expA/{rel}", headers=auth())
    assert resp.status_code == 200 and resp.get_data()[:2] == b"\xff\xd8"
    assert (poster_cache / "expA" / rel).is_file()
    assert not (root / "expA" / "runs" / "posters").exists(), "the archive tree stays untouched"

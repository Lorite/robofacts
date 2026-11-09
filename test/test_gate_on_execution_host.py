"""The gate runs where the trial runs, and a host's setup files apply wherever the runner is
(2026-10-14, the handheld VIO experiment of the lab's #122).

Two defects found the same day. A laptop-driven trial with ``execution_host=orin`` ran its T-0
gate on the laptop, so the laptop's processes, disk, network and stop latch (which the stop
commands write inside the rig's container) were checked instead of the rig's. And a runner
started inside the rig's container (the cockpit) ran every command directly, so the setup
files the host spec names, such as an overlay workspace, were never sourced.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from robofacts import campaign_spec as cs
from robofacts import preflight_gate as pg
from robofacts import remote_exec_utils as rx
from robofacts import trial_runner as tr

from test_trial_runner import CAMPAIGN, CONTRACT, CORE, RUNNER, FakeHooks  # noqa: F401

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
OVERLAY = "/home/vscode/ov_ws/install/local_setup.zsh"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "common" / "config" / "record_profiles.yaml").write_text(
        "profiles: {position_only: {topics: [/poses, /tf, /rosout]}}\n", encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    return tmp_path


@pytest.fixture
def pieces(root: Path):
    return cs.load_factors("expA", root), tr.RunnerConfig.load("expA", root)


def trial_on(host: str) -> cs.TrialSpec:
    return cs.TrialSpec("expA", {"execution_host": host, "record_profile": "position_only",
                                 "trajectory": "circle"}, ["execution_host", "trajectory"], 1)


def inside_the_rig(config, monkeypatch, **spec):
    config.hosts["orin"].update({"container_hostname": "jetson-dev", **spec})
    monkeypatch.setattr(tr.socket, "gethostname", lambda: "jetson-dev")


# --------------------------------------------------------------------------- #
# A host's setup files, also when the runner runs locally
# --------------------------------------------------------------------------- #
def test_a_host_that_names_no_file_keeps_its_commands_unchanged(pieces, monkeypatch):
    factors, config = pieces
    assert config.local_setup("laptop") is None
    inside_the_rig(config, monkeypatch)
    assert config.local_setup("orin") is None
    result = tr.run_trial(trial_on("orin"), factors, config, Path("/tmp/unused/bags"), dry_run=True)
    assert result.commands[-1][:2] == ["python3", "fly.py"]


def test_named_setup_files_are_sourced_inside_the_rig(pieces, monkeypatch, root):
    factors, config = pieces
    inside_the_rig(config, monkeypatch, ws_setup="/workspaces/ws/install/setup.zsh",
                   env_setup=[OVERLAY])
    assert config.remote_target("orin") is None
    assert config.local_setup("orin") == ("zsh", ["/workspaces/ws/install/setup.zsh", OVERLAY])

    preview = tr.run_trial(trial_on("orin"), factors, config, root / "bags", dry_run=True)
    action = preview.commands[-1]
    assert action[:2] == ["zsh", "-c"]
    assert action[2] == (f"source /workspaces/ws/install/setup.zsh && source {OVERLAY} && "
                         f"exec python3 fly.py --shape circle")

    fake = FakeHooks(records_bag=False)
    tr.run_trial(trial_on("orin"), factors, config, root / "bags", dry_run=False,
                 hooks=fake.hooks(), slim=False, pause_before_action=False)
    launched = [c for c in fake.started if c[:2] == ["zsh", "-c"] and "ros2 launch" in c[2]]
    assert launched and launched[0][2].startswith(f"source /workspaces/ws/install/setup.zsh && "
                                                  f"source {OVERLAY} && exec ros2 launch")
    assert any(c[:2] == ["zsh", "-c"] and "exec python3 fly.py" in c[2] for c in fake.foreground)


def test_env_setup_is_sourced_on_a_remote_host_too(pieces):
    _, config = pieces
    config.hosts["orin"]["env_setup"] = [OVERLAY]
    target = config.remote_target("orin")
    assert target.env_setup == (OVERLAY,)
    inner = rx.wrap_command(["ros2", "launch", "p", "f.launch.py"], target)[-1]
    assert f"source {target.ws_setup} && source {OVERLAY} && cd " in inner


def test_a_malformed_env_setup_is_refused_at_load(root):
    runner = root / "expA" / "config" / "runner.yaml"
    runner.write_text(RUNNER.replace("shell: zsh}", "shell: zsh, env_setup: /one/file.zsh}"))
    with pytest.raises(tr.RunnerError, match="env_setup must be a list"):
        tr.RunnerConfig.load("expA", root)


def test_local_setup_command_without_files_is_the_command_itself():
    assert rx.local_setup_command(["ros2", "topic", "list"], []) == ["ros2", "topic", "list"]
    assert rx.local_setup_command(["echo", "a b"], ["/x y.sh"], "bash") == [
        "bash", "-c", "source '/x y.sh' && exec echo 'a b'"]


# --------------------------------------------------------------------------- #
# The gate runs on the trial's host
# --------------------------------------------------------------------------- #
class RecordingGate:
    """A gate double that records where the runner sends it, then refuses the trial, so the
    test ends at the gate (a remote trial past it would reach for ssh)."""

    def __init__(self):
        self.moved_to = []

    def with_target(self, target, local_setup=None, project_root=None):
        self.moved_to.append((target, local_setup))
        return self

    def check(self, label, values=None):
        raise pg.PreflightBlocked("recorded where the gate was sent")


@pytest.mark.parametrize("host, expected_host", [("orin", "orin-host"), ("laptop", None)])
def test_the_runner_moves_the_gate_to_the_trial_host(pieces, root, host, expected_host):
    factors, config = pieces
    gate = RecordingGate()
    result = tr.run_trial(trial_on(host), factors, config, root / "bags", gate=gate, dry_run=False,
                          hooks=FakeHooks().hooks(), slim=False, pause_before_action=False)
    assert result.status == "blocked"
    (target, local_setup), = gate.moved_to
    assert (target.host if target is not None else None) == expected_host
    assert local_setup is None


def test_inside_the_rig_the_gate_stays_local_with_the_hosts_setup(pieces, root, monkeypatch):
    factors, config = pieces
    inside_the_rig(config, monkeypatch, env_setup=[OVERLAY])
    gate = RecordingGate()
    tr.run_trial(trial_on("orin"), factors, config, root / "bags", gate=gate, dry_run=False,
                 hooks=FakeHooks().hooks(), slim=False, pause_before_action=False)
    assert gate.moved_to == [(None, ("zsh", [OVERLAY]))]


def checklist(tmp_path: Path, path: str) -> Path:
    file = tmp_path / "preflight_checklist.json"
    file.write_text(json.dumps({"checklist": [
        {"text": "[HARD] The path exists", "check": "file_exists", "args": {"path": path}}]}))
    return file


def target_at(workspace: Path) -> rx.RemoteTarget:
    return rx.RemoteTarget(host="orin-host", container="ros2_jetson_dev", workspace=workspace,
                           ros_setup="/opt/ros/humble/setup.zsh", ws_setup="/ws/setup.zsh")


def test_a_remote_gate_command_carries_its_checklist(tmp_path):
    gate = pg.PreflightGate(checklist=checklist(tmp_path, "/")).with_target(
        target_at(Path("/workspaces/p")), project_root=PACKAGE_ROOT.parent)
    cmd = gate.command({"execution_host": "orin"})
    assert cmd[:2] == ["ssh", "-tt"] and "orin-host" in cmd
    assert "docker exec -i ros2_jetson_dev" in cmd[-1]
    assert "base64 -d" in cmd[-1] and "run_preflight" in cmd[-1]
    assert "execution_host=orin" in cmd[-1]
    # The package's place in this checkout, under the target's workspace.
    assert f"/workspaces/p/{PACKAGE_ROOT.name}" in cmd[-1]
    assert gate.ran_on() == "orin-host/ros2_jetson_dev"


@pytest.fixture
def ssh_is_local(monkeypatch, tmp_path):
    """The ssh+docker wrapper replaced by the inner command itself, so the remote script
    really runs, here. A stub `ros2` keeps the gate's daemon bounce off the real daemon."""
    monkeypatch.setattr(rx, "wrap_command", lambda cmd, target: cmd)
    monkeypatch.setenv("PYTHONPATH", str(PACKAGE_ROOT))
    stub = tmp_path / "stub_bin"
    stub.mkdir()
    (stub / "ros2").write_text("#!/bin/sh\nexit 0\n")
    (stub / "ros2").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.parametrize("path, passes", [("/", True), ("/no/such/path/at/all", False)])
def test_the_remote_gate_script_runs_the_checklist_it_carries(tmp_path, ssh_is_local, path, passes):
    script = pg.remote_gate_script(checklist(tmp_path, path).read_bytes(), ["--no-daemon-bounce"],
                                   str(PACKAGE_ROOT))
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=120,
                          cwd=tmp_path)
    assert (proc.returncode == 0) is passes, proc.stdout + proc.stderr
    assert ("PASS" if passes else "FAIL") in proc.stdout
    assert "file_exists" in proc.stdout


def test_a_remote_gate_verdict_says_where_it_ran(tmp_path, ssh_is_local, monkeypatch):
    log = tmp_path / "gate.jsonl"
    gate = pg.PreflightGate(checklist=checklist(tmp_path, "/"), log_path=log).with_target(
        target_at(PACKAGE_ROOT.parent), project_root=PACKAGE_ROOT.parent)
    result = gate.check("t1", {"execution_host": "orin"})
    assert result.status == "pass" and result.n_checks == 1
    assert result.as_metadata()["ran_on"] == "orin-host/ros2_jetson_dev"
    assert json.loads(log.read_text().splitlines()[-1])["ran_on"] == "orin-host/ros2_jetson_dev"

    failing = pg.PreflightGate(checklist=checklist(tmp_path, "/no/such/path")).with_target(
        target_at(PACKAGE_ROOT.parent), project_root=PACKAGE_ROOT.parent)
    with pytest.raises(pg.PreflightBlocked, match="file_exists"):
        failing.check("t2")


def test_an_unreachable_target_is_an_error_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(rx, "wrap_command", lambda cmd, target: ["sh", "-c", "echo 'ssh: no route\r'; exit 255"])
    gate = pg.PreflightGate(checklist=checklist(tmp_path, "/")).with_target(target_at(Path("/w")))
    with pytest.raises(pg.PreflightBlocked, match="ERROR"):
        gate.check("t3")
    assert gate.last_result.status == "error"
    assert "\r" not in "".join(gate.last_result.output_tail)


def test_a_local_gate_sources_the_hosts_setup(tmp_path):
    gate = pg.PreflightGate(checklist=checklist(tmp_path, "/")).with_target(None, ("bash", [OVERLAY]))
    cmd = gate.command()
    assert cmd[:2] == ["bash", "-c"] and cmd[2].startswith(f"source {OVERLAY} && exec ")
    assert gate.ran_on() == "local"
    assert pg.PreflightGate(checklist=checklist(tmp_path, "/")).command()[0] != "bash"


# --------------------------------------------------------------------------- #
# The web UI's gate (it ran on the trial host already, but could not import its helper)
# --------------------------------------------------------------------------- #
def test_every_relative_import_of_the_ui_resolves():
    """`from .remote_exec_utils import ...` inside ui/ named a module that only exists one
    level up, since the package split of 2026-10-13: every remote path of the web UI (the
    gate on the rig, the stops, the viewers) raised ImportError when it was used."""
    import ast
    ui_dir = PACKAGE_ROOT / "robofacts" / "ui"
    package_dir = ui_dir.parent
    missing = []
    for source in ui_dir.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                base = ui_dir if node.level == 1 else package_dir
                parts = node.module.split(".")
                if not (base.joinpath(*parts).with_suffix(".py").is_file()
                        or base.joinpath(*parts).is_dir()):
                    missing.append(f"{source.name}:{node.lineno} {'.' * node.level}{node.module}")
    assert not missing, missing


@pytest.mark.parametrize("remote", [True, False])
def test_the_ui_gate_runs_on_the_trial_host_or_with_its_setup(pieces, monkeypatch, remote):
    pytest.importorskip("flask")
    from robofacts.ui import app

    _, config = pieces
    if remote:
        host = "orin"
    else:
        inside_the_rig(config, monkeypatch, env_setup=[OVERLAY])
        host = "orin"
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="all clear", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    app.ShellPreflightGate("expA", config, host, Path("/repo")).check("t1")
    if remote:
        assert seen[0][:2] == ["ssh", "-tt"] and "orin-host" in seen[0]
    else:
        assert seen[0][:2] == ["zsh", "-c"] and seen[0][2].startswith(f"source {OVERLAY} && exec bash")

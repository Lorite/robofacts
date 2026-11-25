"""One run per project at a time, and the leftovers of a runner killed hard (the review of
2026-11-25, round 2).

B2-2: two runners started 1.5 s apart on one project recorded 8 trials for a campaign that
wanted 4, each recording held the other runner's lines, and the second runner deleted the
run marker while the first still ran. Started in the same second, two attempts shared one
trial directory.

B2-3: a runner killed with kill -9 during the action left its stack and recorder running, a
recording with data and no metadata, and no ledger line, and the next run said nothing.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from robofacts import campaign_plan as cp, campaign_spec as cs, run_marker
from robofacts import trial_index as ti, trial_runner as tr

from test_process_backend import PACKAGE, use_project, write_project


def _env() -> dict:
    return dict(os.environ, PYTHONPATH=str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                TRIALS_VOICE="off")


def _hold_marker(experiments: Path) -> subprocess.Popen:
    """Another process that holds the run marker of ``experiments`` until it is killed."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\n"
         "from robofacts import run_marker\n"
         "with run_marker.running(sys.argv[1], experiment='plant', campaign='rates'):\n"
         "    print('held', flush=True)\n"
         "    time.sleep(60)\n", str(experiments)],
        env=_env(), stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    return holder


def _plan(experiments: Path):
    campaign = cs.load_campaign(experiments / "plant" / "campaigns" / "rates.yaml", experiments, [])
    return cp.build_plan(campaign, ti.load_trials(experiments, "plant"), False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    yield experiments
    ti._recorder_at.cache_clear()


def test_a_run_is_refused_while_another_process_holds_the_marker(project):
    """run_plan is what the web interface calls: the refusal is there, not only in main()."""
    holder = _hold_marker(project)
    try:
        config = tr.RunnerConfig.load("plant", project)
        with pytest.raises(tr.RunnerError, match=rf"in progress on this machine: pid {holder.pid}"):
            tr.run_plan(_plan(project), config, project / "plant" / "recordings", dry_run=False)
        assert not [p for p in (project / "plant" / "recordings").iterdir() if p.name != ".gitkeep"]
        # A single trial (the scratch path) is refused the same way.
        spec = next(_plan(project).campaign.combinations())
        with pytest.raises(tr.RunnerError, match="in progress"):
            tr.run_trial(spec, _plan(project).campaign.factors, config,
                         project / "plant" / "bags_scratch", dry_run=False)
        assert run_marker.read(project)["pid"] == holder.pid, "the holder's marker stays"
    finally:
        holder.kill()
        holder.wait()


def test_trials_run_ends_a_refusal_with_its_message_not_a_traceback(project, capsys):
    """The refusal reaches `trials run` as a RunnerError: it ends with exit code 1 and the
    REFUSED line, once (merge of round 2: main did not catch it)."""
    holder = _hold_marker(project)
    try:
        rc = tr.main([str(project / "plant" / "campaigns" / "rates.yaml"),
                      "--experiments-root", str(project), "--no-pause", "--execute", "--limit", "1"])
    finally:
        holder.kill()
        holder.wait()
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("REFUSED") == 1 and "in progress" in out, out


def test_a_runner_removes_only_its_own_marker(tmp_path):
    path = run_marker.marker_path(tmp_path)
    with run_marker.running(tmp_path, experiment="expA"):
        # Another runner replaced the marker meanwhile (a stale one, or a race).
        path.write_text('{"host": "elsewhere", "pid": 4242, "experiment": "expB"}', encoding="utf-8")
    assert path.exists(), "the other run's marker is left in place"


def test_two_attempts_in_the_same_second_get_two_directories(project):
    plan = _plan(project)
    spec = next(plan.campaign.combinations())
    config = tr.RunnerConfig.load("plant", project)
    bags = project / "plant" / "recordings"
    first = tr.run_trial(spec, plan.campaign.factors, config, bags, dry_run=True,
                         clock=lambda: "20261125T120000")
    first.bag_dir.mkdir(parents=True)
    second = tr.run_trial(spec, plan.campaign.factors, config, bags, dry_run=True,
                          clock=lambda: "20261125T120000")
    assert second.bag_dir.name == first.bag_dir.name + "-2"
    # A run log of that name (a blocked attempt leaves no directory) takes the name too.
    (project / "plant" / "runs" / f"{second.bag_dir.name}.log").write_text("", encoding="utf-8")
    third = tr.run_trial(spec, plan.campaign.factors, config, bags, dry_run=True,
                         clock=lambda: "20261125T120000")
    assert third.bag_dir.name == first.bag_dir.name + "-3"


@pytest.mark.real_sleep
def test_two_runners_started_together_record_the_campaign_once(project):
    """The reviewer's reproduction: two `trials run --execute` of one campaign at once."""
    argv = [sys.executable, "-m", "robofacts.cli", "run",
            str(project / "plant" / "campaigns" / "rates.yaml"),
            "--experiments-root", str(project), "--execute", "--no-pause"]
    runners = [subprocess.Popen(argv, cwd=project.parent, env=_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
               for _ in range(2)]
    try:
        outs = [r.communicate(timeout=120)[0] for r in runners]
    finally:
        for r in runners:
            if r.poll() is None:
                os.killpg(r.pid, signal.SIGKILL)
    refused = [o for o in outs if "REFUSED - a run of plant" in o]
    assert len(refused) == 1, outs
    trials = ti.load_trials(project, "plant")
    assert sorted(t.metadata["rate"] for t in trials) == ["fast", "slow"], outs
    assert all(t.status == "usable" for t in trials)
    assert not run_marker.marker_path(project).exists()


# --------------------------------------------------------------------------- #
# B2-3: a runner killed hard (kill -9 during the action) left its stack and recorder running,
# a recording with data and no metadata, and no ledger line. The next run said nothing.
# --------------------------------------------------------------------------- #
def _dead_pid() -> int:
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


def _kill_processes_under(root: Path) -> None:
    """SIGKILL what this test started and left: every process whose working directory lies
    under ``root``, the test's own directory."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if os.readlink(entry / "cwd").startswith(str(root)):
                os.kill(int(entry.name), signal.SIGKILL)
        except OSError:
            continue


def _wait_until(predicate, timeout: float = 20.0, what: str = "") -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_killed_run_whose_processes_still_run_refuses_the_next_run(project):
    import socket
    leftover = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        run_marker.marker_path(project).write_text(json.dumps({
            "host": socket.gethostname(), "pid": _dead_pid(), "experiment": "plant",
            "started": "2026-11-25T10:00:00+00:00",
            "groups": [{"name": "recorder", "pgid": leftover.pid}]}), encoding="utf-8")
        config = tr.RunnerConfig.load("plant", project)
        with pytest.raises(tr.RunnerError, match=rf"(?s)recorder: process group {leftover.pid}.*"
                                                 rf"kill -INT -- -{leftover.pid}"):
            tr.run_plan(_plan(project), config, project / "plant" / "recordings", dry_run=False)
        assert leftover.poll() is None, "the runner never stops a leftover group itself"
    finally:
        leftover.kill()
        leftover.wait()
    # With the group gone, the next run replaces the stale marker and runs.
    assert tr.run_plan(_plan(project), config, project / "plant" / "recordings",
                       dry_run=False, limit=0) == []
    assert not run_marker.marker_path(project).exists()


@pytest.mark.real_sleep
def test_the_trial_of_a_runner_killed_during_the_action_is_put_on_record(project):
    (project / "plant" / "scripts" / "observe.py").write_text("import time\ntime.sleep(60)\n",
                                                             encoding="utf-8")
    runner = subprocess.Popen(
        [sys.executable, "-m", "robofacts.cli", "run",
         str(project / "plant" / "campaigns" / "rates.yaml"), "--experiments-root", str(project),
         "--execute", "--no-pause", "--limit", "1"],
        cwd=project.parent, env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        def action_noted():
            record = run_marker.read(project) or {}
            return "action" in [g.get("name") for g in record.get("groups") or []]
        _wait_until(action_noted, what="the action's process group in the run marker")
        record = run_marker.read(project)
        groups = [g["pgid"] for g in record["groups"]]
        assert sorted(g["name"] for g in record["groups"]) == ["action", "recorder", "stack"]
        runner.kill()   # kill -9: no teardown, no metadata, no ledger line
        runner.wait()
        config = tr.RunnerConfig.load("plant", project)
        bags = project / "plant" / "recordings"
        with pytest.raises(tr.RunnerError, match="kill -INT -- -") as refused:
            tr.run_plan(_plan(project), config, bags, dry_run=False)
        commands = [int(g) for g in re.findall(r"kill -INT -- -(\d+)", str(refused.value))]
        assert sorted(commands) == sorted(groups)
        for pgid in commands:   # what the operator does with the printed commands
            os.killpg(pgid, signal.SIGINT)
        _wait_until(lambda: not any(run_marker.group_alive(g) for g in groups),
                    what="the leftover groups to stop")
        assert tr.run_plan(_plan(project), config, bags, dry_run=False, limit=0) == []
    finally:
        if runner.poll() is None:
            runner.kill()
        _kill_processes_under(project.parent)
    (trial,) = ti.load_trials(project, "plant")
    assert trial.metadata["aborted"] is True and trial.status != "usable"
    assert trial.metadata["abort_reason"] == tr.KILLED_REASON
    rows = [json.loads(line) for line in (project / "plant" / "runs" / tr.ATTEMPT_LEDGER_NAME)
            .read_text(encoding="utf-8").splitlines()]
    assert [(r["trial_label"], r["status"], r["metadata_written"]) for r in rows] == \
        [(trial.bag_dir.name, "interrupted", True)]
    assert rows[0]["campaign"] == "rates" and rows[0]["abort_reason"] == tr.KILLED_REASON
    assert not run_marker.marker_path(project).exists()


# --------------------------------------------------------------------------- #
# A killed run whose stack ran on another machine (third review of 2026-11-25)
# --------------------------------------------------------------------------- #
REMOTE_HOSTS = """\
version: 1
hosts:
  here: {local: true}
  robot:
    host: robot-host
    container: robot_dev
    workspace: /ws/project
    shell: zsh
"""


def _remote_marker(project: Path, groups: list, trial: bool = True) -> None:
    """The marker of a killed run whose stack ran on the level ``robot``: its ssh clients
    died with this machine (no groups), or ``groups`` still run here."""
    import socket
    (project / "common" / "config").mkdir(parents=True, exist_ok=True)
    (project / "common" / "config" / "hosts.yaml").write_text(REMOTE_HOSTS, encoding="utf-8")
    record = {"host": socket.gethostname(), "pid": _dead_pid(), "experiment": "plant",
              "started": "2026-11-25T20:00:00+00:00", "execution_host": "robot",
              "simulation": False, "groups": groups, "trial_stage": "running"}
    if trial:
        record["trial"] = {"bag_dir": "/ws/project/experiments/plant/recordings/T1",
                           "remote": "robot-host", "backend": "process",
                           "runs_dir": str(project / "plant" / "runs"), "metadata": {},
                           "attempt": {"trial_label": "T1"}}
    run_marker.marker_path(project).write_text(json.dumps(record), encoding="utf-8")


def _with_clean_slate_tool(monkeypatch, tool: str = "clean_slate.sh") -> None:
    """The process backend, as a backend that ships a clean-slate tool."""
    import dataclasses
    from robofacts import backend
    load = backend.load
    monkeypatch.setattr(backend, "load",
                        lambda name: dataclasses.replace(load(name), clean_slate_tool=tool))


@pytest.mark.parametrize("trial", [True, False], ids=["during-a-trial", "between-trials"])
def test_a_killed_remote_run_says_that_its_stack_may_still_run_there(project, capsys, monkeypatch,
                                                                     trial):
    """Its local groups were only ssh clients, which died with the laptop: the new run still
    starts, as before, but says where to clean up and how."""
    _with_clean_slate_tool(monkeypatch)
    _remote_marker(project, [], trial=trial)
    with run_marker.running(project, on_dead=tr.recover_killed_trial, experiment="plant"):
        started = True
    assert started
    out = capsys.readouterr().out
    assert "ran its stack and its recorder on robot-host (execution host robot)" in out, out
    assert "may still run there" in out
    assert "  ssh robot-host\n  docker exec -it robot_dev zsh -l\n" \
           "  cd /ws/project && trials tool clean_slate.sh" in out, out


def test_without_a_clean_slate_tool_the_clean_up_is_said_in_words(project, capsys):
    _remote_marker(project, [])
    with run_marker.running(project, on_dead=tr.recover_killed_trial, experiment="plant"):
        pass
    out = capsys.readouterr().out
    assert "  ssh robot-host\n  docker exec -it robot_dev zsh -l\n" \
           "  then stop the stack and the recorder of that run there" in out, out


def test_the_refusal_for_a_remote_run_says_that_the_kill_stops_only_the_clients(project, monkeypatch):
    _with_clean_slate_tool(monkeypatch)
    leftover = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        _remote_marker(project, [{"name": "launch_stack", "pgid": leftover.pid}])
        with pytest.raises(run_marker.RunRefused) as refused:
            with run_marker.running(project, on_dead=tr.recover_killed_trial, experiment="plant"):
                pass
        text = str(refused.value)
        assert f"kill -INT -- -{leftover.pid}" in text, "the refusal and its commands stay"
        assert "only their ssh clients" in text and "trials tool clean_slate.sh" in text, text
    finally:
        leftover.kill()
        leftover.wait()


def test_a_killed_local_run_gets_no_remote_clean_up(project, capsys):
    _remote_marker(project, [], trial=False)
    record = json.loads(run_marker.marker_path(project).read_text(encoding="utf-8"))
    record["execution_host"] = "here"
    run_marker.marker_path(project).write_text(json.dumps(record), encoding="utf-8")
    with run_marker.running(project, on_dead=tr.recover_killed_trial, experiment="plant"):
        pass
    assert "ssh" not in capsys.readouterr().out


needs_proc = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")


@needs_proc
def test_a_marker_whose_pid_now_belongs_to_another_process_is_replaced(project, capsys):
    """A3-4 (third review of 2026-11-25): after a container restart or a reboot, the pid of a
    killed runner can belong to another process. os.kill(pid, 0) alone took that process for
    the runner, and every run was refused for 12 hours. The marker records when its process
    started, and a pid with another start time is another process."""
    other = subprocess.Popen(["sleep", "60"])
    try:
        record = {"host": socket.gethostname(), "pid": other.pid, "pid_start": "another-boot:1",
                  "experiment": "plant", "started": "2026-11-25T10:00:00+00:00"}
        run_marker.marker_path(project).write_text(json.dumps(record), encoding="utf-8")
        assert run_marker.read_live(project) is None
        with run_marker.running(project, experiment="plant"):
            mine = run_marker.read(project)
            assert mine["pid"] == os.getpid() and mine.get("pid_start"), mine
        assert "no longer runs" in capsys.readouterr().out
    finally:
        other.kill()
        other.wait()


@needs_proc
def test_a_marker_that_names_this_pid_from_another_start_is_not_taken_for_its_own(project):
    """The same reuse can hand the new runner the killed runner's pid: the marker then looked
    like its own, and the killed run's trial was never put on record."""
    record = {"host": socket.gethostname(), "pid": os.getpid(), "pid_start": "another-boot:1",
              "experiment": "plant", "started": "2026-11-25T10:00:00+00:00"}
    run_marker.marker_path(project).write_text(json.dumps(record), encoding="utf-8")
    dead = []
    with run_marker.running(project, on_dead=dead.append, experiment="plant"):
        pass
    assert [r.get("pid_start") for r in dead] == ["another-boot:1"]


@needs_proc
def test_a_live_holder_with_its_own_start_time_still_refuses(project):
    holder = _hold_marker(project)
    try:
        assert run_marker.read_live(project)["pid"] == holder.pid
        with pytest.raises(run_marker.RunRefused):
            with run_marker.running(project, experiment="plant"):
                pass
    finally:
        holder.kill()
        holder.wait()

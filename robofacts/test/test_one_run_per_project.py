"""One run per project at a time, and the leftovers of a runner killed hard (the review of
2026-11-25, round 2).

B2-2: two runners started 1.5 s apart on one project recorded 8 trials for a campaign that
wanted 4, each recording held the other runner's lines, and the second runner deleted the
run marker while the first still ran. Started in the same second, two attempts shared one
trial directory.
"""
from __future__ import annotations

import os
import signal
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

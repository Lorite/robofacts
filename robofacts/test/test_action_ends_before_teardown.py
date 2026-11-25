"""The trial's action on the process backend: it must END before the stack stops, a stop must
reach the whole action, and a trial whose action the runner lost never counts.

Found by the review of 2026-11-25 (round 1):
- an action that could not start was written with ``aborted: false`` and counted as coverage,
- one byte that is not UTF-8 in the action's output ended the reading and left the action
  running into the next trial,
- on Ctrl-C the runner stopped the stack while the action was still landing,
- the action timeout signalled only a wrapper script, and the flight it started flew on.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from robofacts import campaign_plan as cp, campaign_spec as cs, trial_index as ti

from test_process_backend import PACKAGE, _run, use_project, write_project

#: The action of these tests: it waits for a signal, then "lands" for 1.5 s and writes
#: whether the stack (the plant, which writes a log line every 50 ms) was still up then.
LANDER = '''\
import signal, sys, time
from pathlib import Path
mark, log = Path("experiments/plant/runs/lander.txt"), Path("experiments/plant/runs/plant.log")
def lines():
    return len(log.read_text().splitlines()) if log.is_file() else 0
def land(signum, frame):
    with mark.open("a") as f:
        f.write(f"signal {signal.Signals(signum).name}\\n")
    time.sleep(1.5)
    before = lines(); time.sleep(0.5)
    with mark.open("a") as f:
        f.write(f"landed stack_up={lines() > before}\\n")
    sys.exit(3)
signal.signal(signal.SIGINT, land)
signal.signal(signal.SIGTERM, land)
mark.write_text("started\\n")
time.sleep(30)
'''


def processes_in(root: Path, pattern: str = "") -> str:
    """Command lines of the live processes whose working directory lies under ``root`` (the
    stack, the recorder and the action run with relative paths, so a match on the command
    line alone would never find them)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            state = next((l for l in (entry / "status").read_text().splitlines() if l.startswith("State:")), "")
        except OSError:
            continue
        if cwd.startswith(str(root)) and pattern in command and "zombie" not in state:
            found.append(command.strip())
    return "\n".join(found)


def _action(experiments: Path, body: str, name: str = "observe.py") -> None:
    (experiments / "plant" / "scripts" / name).write_text(body, encoding="utf-8")


def _runner_yaml(experiments: Path, old: str, new: str) -> None:
    path = experiments / "plant" / "config" / "runner.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _wait_for(path: Path, text: str, timeout: float = 20.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if path.is_file() and text in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.1)
    raise AssertionError(f"{path} never said {text!r}")


@pytest.mark.real_sleep
def test_an_action_that_cannot_start_aborts_the_trial(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    script = experiments / "plant" / "scripts" / "fly.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)   # not executable: the action cannot start
    _runner_yaml(experiments, '"python3 experiments/plant/scripts/observe.py 0.6"',
                 '"experiments/plant/scripts/fly.sh"')
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert rc == 1 and "execution error" in out, out
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True and trial.status != "usable"
    assert trial.metadata["abort_reason"].startswith("execution error")
    campaign = cs.load_campaign(experiments / "plant" / "campaigns" / "rates.yaml", experiments, [])
    plan = cp.build_plan(campaign, ti.load_trials(experiments, "plant"), False)
    assert len(list(cp.execution_order(plan))) == 2, "the failed trial covers nothing"
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_a_byte_that_is_not_utf8_in_the_output_does_not_lose_the_action(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    _action(experiments, "import sys, time\nsys.stdout.buffer.write(b'caf\\xe9\\n'); "
                         "sys.stdout.flush(); time.sleep(0.6)\n")
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert rc == 0 and "1 trial(s): 1 OK" in out, out
    assert "execution error" not in out
    ti._recorder_at.cache_clear()


def _start_runner(root: Path, experiments: Path) -> subprocess.Popen:
    """The runner in its own session, as a terminal's foreground job: a signal to its group
    is what a Ctrl-C at that terminal sends."""
    env = dict(os.environ, PYTHONPATH=str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               TRIALS_VOICE="off")
    return subprocess.Popen(
        [sys.executable, "-m", "robofacts.cli", "run", str(experiments / "plant" / "campaigns" / "rates.yaml"),
         "--experiments-root", str(experiments), "--execute", "--no-pause", "--limit", "1"],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)


@pytest.mark.real_sleep
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_the_action_lands_before_the_stack_stops(tmp_path, monkeypatch, sig):
    """Ctrl-C at the terminal (SIGINT to the foreground group), a stop request (SIGTERM) or a
    closed terminal (SIGHUP): the action lands with its stack still up, then the teardown
    runs, and the trial is written as aborted. Before 2026-11-25 SIGTERM and SIGHUP ended the
    runner at once and left the stack and the recorder running."""
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    _action(experiments, LANDER)
    mark = experiments / "plant" / "runs" / "lander.txt"
    runner = _start_runner(root, experiments)
    try:
        _wait_for(mark, "started")
        time.sleep(0.5)
        os.killpg(runner.pid, sig)
        out, _ = runner.communicate(timeout=60)
    finally:
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
    text = mark.read_text(encoding="utf-8")
    assert "landed stack_up=True" in text, (text, out)
    assert processes_in(root) == "", "nothing of the trial is left running"
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True and trial.status != "usable"
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_the_action_timeout_reaches_the_flight_behind_a_wrapper(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    mark = experiments / "plant" / "runs" / "flight.txt"
    _action(experiments, "python3 experiments/plant/scripts/flight.py\necho wrapper done\n", "wrapper.sh")
    _action(experiments, '''\
import signal, sys, time
from pathlib import Path
mark = Path("experiments/plant/runs/flight.txt")
def land(signum, frame):
    mark.write_text("landed on SIGTERM\\n"); sys.exit(4)
signal.signal(signal.SIGTERM, land)
mark.write_text("flying\\n")
time.sleep(8)
mark.write_text("flew the whole 8 s, no signal\\n")
''', "flight.py")
    _runner_yaml(experiments, '"python3 experiments/plant/scripts/observe.py 0.6"',
                 '"sh experiments/plant/scripts/wrapper.sh"\naction_timeout_s: 1')
    t0 = time.monotonic()
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert "ACTION TIMEOUT" in out, out
    assert mark.read_text(encoding="utf-8") == "landed on SIGTERM\n"
    assert time.monotonic() - t0 < 7.5, "the runner did not wait for the whole flight"
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_a_second_interrupt_does_not_cut_the_teardown(tmp_path, monkeypatch):
    """Two Ctrl-C in a row: the second one waits until the recorder and the stack stopped."""
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    _action(experiments, LANDER)
    # A long pause after the recorder stops, so the next interrupts land inside the teardown.
    _runner_yaml(experiments, "stop_grace_s: 0.3", "stop_grace_s: 3")
    mark = experiments / "plant" / "runs" / "lander.txt"
    runner = _start_runner(root, experiments)
    try:
        _wait_for(mark, "started")
        time.sleep(0.5)
        os.killpg(runner.pid, signal.SIGINT)
        _wait_for(mark, "landed")
        time.sleep(0.8)
        for _ in range(10):   # keep interrupting through the teardown
            time.sleep(0.3)
            if runner.poll() is not None:
                break
            os.killpg(runner.pid, signal.SIGINT)
        out, _ = runner.communicate(timeout=60)
    finally:
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
    assert processes_in(root) == "", out
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True, out
    ti._recorder_at.cache_clear()

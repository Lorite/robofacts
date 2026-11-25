"""The trial's action on the process backend: it must END before the stack stops, a stop must
reach the whole action, and a trial whose action the runner lost never counts.

Found by the review of 2026-11-25 (round 1):
- an action that could not start was written with ``aborted: false`` and counted as coverage,
- one byte that is not UTF-8 in the action's output ended the reading and left the action
  running into the next trial,
- on Ctrl-C the runner stopped the stack while the action was still landing,
- the action timeout signalled only a wrapper script, and the flight it started flew on.

Found by the second review of 2026-11-25 (round 2), in the fixes of round 1:
- a closed terminal (a real SIGHUP, every later write to the terminal fails) left the stack
  and the recorder running and the trial without metadata,
- a flight that a wrapper started in the background was never signalled once the wrapper
  had exited, by Ctrl-C, by the action timeout or at the end of the action,
- SIGTERM 5 s after the forwarded Ctrl-C cut a landing short,
- a stop did not end the manual window (the action step disabled), and the trial counted,
- an error after the recording left a recording without metadata and no ledger line,
- the web interface's Ctrl-C did not reach the action in its worker thread.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from robofacts import backend as backends, campaign_plan as cp, campaign_spec as cs, trial_index as ti
from robofacts import remote_exec_utils as rex, trial_runner as tr

from test_process_backend import PACKAGE, _run, processes_in, use_project, write_project

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
    # The reason names the signal and the stage the trial had reached, not the teardown,
    # and the run ends with a message and the signal's exit code, not a traceback.
    reason = trial.metadata["abort_reason"]
    assert sig.name in reason and "after stage 'action_start'" in reason, reason
    runner_lines = [l for l in out.splitlines() if not l.startswith("[stack]")]
    assert f"INTERRUPTED ({sig.name})" in out, out
    assert not any(l.startswith("Traceback") for l in runner_lines), out
    assert runner.returncode == 128 + int(sig), runner.returncode
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


def _pty_runner(root: Path, experiments: Path):
    """The runner with a pseudo-terminal as its controlling terminal, as in a real shell.
    Closing the returned master end hangs the terminal up: the kernel sends SIGHUP, and every
    later write of the runner to the terminal fails with EIO."""
    import fcntl
    import termios
    master, slave = os.openpty()
    env = dict(os.environ, PYTHONPATH=str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               TRIALS_VOICE="off")
    proc = subprocess.Popen(
        [sys.executable, "-m", "robofacts.cli", "run", str(experiments / "plant" / "campaigns" / "rates.yaml"),
         "--experiments-root", str(experiments), "--execute", "--no-pause", "--limit", "1"],
        cwd=root, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    return proc, master


@pytest.mark.real_sleep
def test_a_closed_terminal_lands_the_action_and_stops_the_stack(tmp_path, monkeypatch):
    import select
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    _action(experiments, LANDER)
    mark = experiments / "plant" / "runs" / "lander.txt"
    runner, master = _pty_runner(root, experiments)
    stop = threading.Event()
    screen: list = []

    def read_terminal():   # the terminal's screen: read, so the runner never blocks on it
        while not stop.is_set():
            if select.select([master], [], [], 0.1)[0]:
                try:
                    screen.append(os.read(master, 65536).decode("utf-8", "replace"))
                except OSError:
                    return
    reader = threading.Thread(target=read_terminal, daemon=True)
    reader.start()
    try:
        try:
            _wait_for(mark, "started")
        except AssertionError:
            raise AssertionError("".join(screen)[-3000:])
        time.sleep(0.5)
        stop.set()
        reader.join(timeout=2)
        os.close(master)   # the terminal closes
        runner.wait(timeout=60)
    finally:
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
    assert "landed stack_up=True" in mark.read_text(encoding="utf-8")
    assert processes_in(root) == "", "the stack and the recorder stopped"
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True, trial.metadata
    assert "SIGHUP" in trial.metadata["abort_reason"]
    assert runner.returncode == 128 + int(signal.SIGHUP), runner.returncode
    ti._recorder_at.cache_clear()


#: A flight that a wrapper starts in the background. It lands on SIGINT or SIGTERM and writes
#: whether the stack (the plant's log) was still up while it landed.
FLIGHT = """\
import signal, sys, time
from pathlib import Path
mark, log = Path("experiments/plant/runs/flight.txt"), Path("experiments/plant/runs/plant.log")
def lines():
    return len(log.read_text().splitlines()) if log.is_file() else 0
def land(signum, frame):
    before = lines(); time.sleep(1.0)
    mark.write_text(f"landed on {signal.Signals(signum).name} stack_up={lines() > before}\\n")
    sys.exit(0)
signal.signal(signal.SIGINT, land)
signal.signal(signal.SIGTERM, land)
mark.write_text("flying\\n")
for i in range(60):
    print("flying", i, flush=True)
    time.sleep(0.25)
mark.write_text("flew the whole time, no signal reached it\\n")
"""


def _background_flight(experiments: Path, detached: bool, extra: str = "") -> Path:
    """The action: a wrapper that starts the flight in the background and exits."""
    _action(experiments, FLIGHT, "flight.py")
    out = " >/dev/null 2>&1" if detached else ""
    _action(experiments, f"python3 experiments/plant/scripts/flight.py{out} &\n"
                         "while ! grep -q flying experiments/plant/runs/flight.txt 2>/dev/null; "
                         "do sleep 0.1; done\necho wrapper exits\n", "wrapper.sh")
    _runner_yaml(experiments, '"python3 experiments/plant/scripts/observe.py 0.6"',
                 '"sh experiments/plant/scripts/wrapper.sh"' + extra)
    return experiments / "plant" / "runs" / "flight.txt"


@pytest.mark.real_sleep
def test_ctrl_c_lands_a_flight_the_wrapper_left_in_the_background(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    mark = _background_flight(experiments, detached=False)
    runner = _start_runner(root, experiments)
    try:
        _wait_for(mark, "flying")
        time.sleep(1.0)
        os.killpg(runner.pid, signal.SIGINT)
        out, _ = runner.communicate(timeout=60)
    finally:
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
    assert mark.read_text(encoding="utf-8") == "landed on SIGINT stack_up=True\n", out
    assert processes_in(root) == "", out
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_a_flight_left_running_after_the_action_ends_before_the_stack(tmp_path, monkeypatch, capsys):
    """The wrapper exits normally, the flight it detached flies on: it is stopped and lands
    while the stack is still up, and the trial does not count."""
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    mark = _background_flight(experiments, detached=True)
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert "left 1 process(es) of its group running" in out, out
    assert mark.read_text(encoding="utf-8") == "landed on SIGTERM stack_up=True\n", out
    assert processes_in(root) == ""
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True and trial.status != "usable"
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_the_action_timeout_reaches_a_flight_left_in_the_background(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    mark = _background_flight(experiments, detached=False, extra="\naction_timeout_s: 2")
    t0 = time.monotonic()
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert "ACTION TIMEOUT" in out, out
    assert time.monotonic() - t0 < 12, "the runner waited for the whole flight"
    assert mark.read_text(encoding="utf-8").startswith("landed on SIGTERM"), out
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_a_landing_in_the_ctrl_c_handler_gets_its_whole_time(tmp_path, monkeypatch):
    """An action that lands in its KeyboardInterrupt handler and keeps the default SIGTERM.
    Until round 2, SIGTERM 5 s after the forwarded Ctrl-C killed it in the middle of the
    landing, while the runner said it waited up to 90 s."""
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    _action(experiments, """\
import sys, time
from pathlib import Path
mark = Path("experiments/plant/runs/lander.txt")
mark.write_text("started\\n")
try:
    time.sleep(30)
except KeyboardInterrupt:
    t0 = time.monotonic()
    time.sleep(6.5)
    with mark.open("a") as f:
        f.write(f"landed after {time.monotonic() - t0:.1f} s\\n")
    sys.exit(3)
""")
    mark = experiments / "plant" / "runs" / "lander.txt"
    runner = _start_runner(root, experiments)
    try:
        _wait_for(mark, "started")
        time.sleep(0.5)
        os.killpg(runner.pid, signal.SIGINT)
        out, _ = runner.communicate(timeout=60)
    finally:
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
    assert "landed after" in mark.read_text(encoding="utf-8"), out
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_a_stop_ends_the_manual_window_and_the_trial_does_not_count(tmp_path, monkeypatch):
    """The action step disabled: the stack is held up for hold_s. The web interface's stop
    (terminate_current_foreground) ends the window, and the trial is written as aborted."""
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    campaign = cs.load_campaign(experiments / "plant" / "campaigns" / "rates.yaml", experiments, [])
    plan = cp.build_plan(campaign, ti.load_trials(experiments, "plant"), False)
    config = tr.RunnerConfig.load("plant", experiments)
    bags_dir = experiments / "plant" / backends.record_dirs(experiments)["recordings"]
    results: list = []
    monkeypatch.chdir(root)
    worker = threading.Thread(target=lambda: results.extend(tr.run_plan(
        plan, config, bags_dir, dry_run=False, limit=1, steps={"action": False}, hold_s=20,
        pause_before_action=False)), daemon=True)
    worker.start()
    t0 = time.monotonic()
    while not rex._HOLDS and time.monotonic() - t0 < 20:
        time.sleep(0.1)
    assert rex._HOLDS, "the manual window never started"
    t_stop = time.monotonic()
    assert rex.terminate_current_foreground() is True
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert time.monotonic() - t_stop < 12, "the window ran its whole 20 s"
    (result,) = results
    assert result.status == "failed" and result.detail.startswith(tr.MANUAL_WINDOW_STOPPED)
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True and trial.status != "usable"
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_an_error_after_the_recording_leaves_an_aborted_trial_and_a_ledger_line(tmp_path, monkeypatch):
    """A verify step that cannot run (here: the disk is full). Until round 2 the recording was
    left without trial_metadata.json and the attempt had no ledger line."""
    import errno
    import json
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    campaign = cs.load_campaign(experiments / "plant" / "campaigns" / "rates.yaml", experiments, [])
    plan = cp.build_plan(campaign, ti.load_trials(experiments, "plant"), False)
    config = tr.RunnerConfig.load("plant", experiments)
    bags_dir = experiments / "plant" / backends.record_dirs(experiments)["recordings"]
    base = tr.ProcessHooks.default()

    def run_foreground(cmd, target, **kw):
        if any("verify" in str(c) for c in cmd):
            raise OSError(errno.ENOSPC, "No space left on device")
        return base.run_foreground(cmd, target, **kw)
    hooks = tr.ProcessHooks(start_background=base.start_background, run_foreground=run_foreground,
                            stop_process=base.stop_process, run_action=base.run_action)
    monkeypatch.chdir(root)
    with pytest.raises(OSError):
        tr.run_plan(plan, config, bags_dir, dry_run=False, limit=1, hooks=hooks,
                    pause_before_action=False)
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True
    assert "No space left on device" in trial.metadata["abort_reason"]
    ledger = [json.loads(l) for l in (experiments / "plant" / "runs" / tr.ATTEMPT_LEDGER_NAME)
              .read_text(encoding="utf-8").splitlines()]
    assert [l["status"] for l in ledger] == ["failed"]
    assert processes_in(root) == ""
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_the_web_interface_shutdown_lands_the_action_of_its_worker_thread(tmp_path):
    """The web interface runs the trial in a worker thread, so a Ctrl-C at its server reaches
    neither that thread nor the action, which leads its own process group. The server calls
    end_current_action: the action gets the Ctrl-C and lands."""
    mark = tmp_path / "lander.txt"
    script = tmp_path / "lander.py"
    script.write_text(f"""\
import signal, sys, time
from pathlib import Path
mark = Path({str(mark)!r})
def land(signum, frame):
    time.sleep(1.0)
    mark.write_text(f"landed on {{signal.Signals(signum).name}}\\n")
    sys.exit(3)
signal.signal(signal.SIGINT, land)
mark.write_text("started\\n")
time.sleep(30)
""", encoding="utf-8")
    results: list = []
    worker = threading.Thread(target=lambda: results.append(
        rex.run_action([sys.executable, str(script)], None, cwd=tmp_path)), daemon=True)
    worker.start()
    _wait_for(mark, "started")
    assert rex.end_current_action(KeyboardInterrupt()) is True
    worker.join(timeout=20)
    assert mark.read_text(encoding="utf-8") == "landed on SIGINT\n"
    assert results[0].returncode == 3

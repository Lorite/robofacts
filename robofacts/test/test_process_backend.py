"""The two interfaces of robofacts.backend, exercised by the backend that has no ROS.

The process backend starts the stack with a shell command and records the lines that the
system writes to a log file. These tests run whole trials on it with real processes: the
gate, the launch, the recorder, the action, the stop order, the verify step, the metadata
and the coverage index. Nothing here needs ROS.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

from robofacts import backend, campaign_plan as cp, campaign_spec as cs
from robofacts import scaffold, trial_index as ti, trial_runner as tr
from robofacts.backends import process as pb
from robofacts.backends import process_recorder

PACKAGE = Path(__file__).resolve().parents[1]

PLANT = '''\
#!/usr/bin/env python3
"""The system under test: a process that writes one line to its log every 50 ms."""
import argparse, itertools, time
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("--rate"); ap.add_argument("--log", type=Path)
args = ap.parse_args()
args.log.parent.mkdir(parents=True, exist_ok=True)
with args.log.open("a") as log:
    for n in itertools.count():
        log.write(f"tick {n} rate={args.rate}\\n"); log.flush(); time.sleep(0.05)
'''

OBSERVE = '''\
#!/usr/bin/env python3
"""The action: let the plant run for a moment. Exits 0 whatever the plant did."""
import sys, time
time.sleep(float(sys.argv[1]))
'''

VERIFY = '''\
#!/usr/bin/env python3
"""A verify step: the recording must hold enough lines of this trial's rate."""
import sys
from pathlib import Path
recording, rate, minimum = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
log = recording / "data" / "plant.log"
lines = [l for l in (log.read_text().splitlines() if log.is_file() else []) if f"rate={rate}" in l]
print(f"{'PASS' if len(lines) >= minimum else 'FAIL'}: {len(lines)} lines of rate={rate}")
sys.exit(0 if len(lines) >= minimum else 1)
'''


def leftover_processes(pattern: str) -> str:
    """Command lines of the processes that match ``pattern`` (read from /proc, so the test
    needs no pgrep, which a plain Python image does not have)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if command and re.search(pattern, command) and "pytest" not in command:
            found.append(command)
    return "\n".join(found)


def write_project(root: Path, *, min_lines: int = 3, with_stack: bool = True) -> Path:
    """A project on the process backend with one experiment, ``plant``. Returns the
    experiments root."""
    assert scaffold.main_init([str(root)]) == 0
    (root / "trials.yaml").write_text("experiments_root: experiments\nproject_root: .\n"
                                      "backend: process\n", encoding="utf-8")
    exp = root / "experiments" / "plant"
    for sub in ("config", "campaigns", "scripts", "runs", "recordings"):
        (exp / sub).mkdir(parents=True)
    for name, body in (("plant.py", PLANT), ("observe.py", OBSERVE), ("verify_log.py", VERIFY)):
        (exp / "scripts" / name).write_text(body, encoding="utf-8")
    (exp / "config" / "factors.yaml").write_text(textwrap.dedent("""\
        version: 1
        factors:
          rate:
            describe: "How the plant is started. A stand-in for an independent variable."
            levels: [slow, fast]
            default: slow
            metadata_key: rate
            maps_to: {kind: internal}
        """), encoding="utf-8")
    (exp / "config" / "trial_metadata_schema.yaml").write_text(textwrap.dedent("""\
        version: 1
        fields:
          rate: {required: true, type: str, factor: true, enum: [slow, fast], describe: "The rate level."}
          record_profile: {required: false, type: str, factor: true, enum: [custom], describe: "Listed in runner.yaml."}
        """), encoding="utf-8")
    launch = ('launch:\n  command: "python3 experiments/plant/scripts/plant.py --rate {rate} '
              '--log experiments/plant/runs/plant.log"\n') if with_stack else ""
    (exp / "config" / "runner.yaml").write_text(
        "simulation: true\n" + launch + textwrap.dedent(f"""\
        action:
          command: "python3 experiments/plant/scripts/observe.py 0.6"
        record_sources: [experiments/plant/runs/plant.log]
        settle_s: 0.3
        stop_grace_s: 0.3
        verify_steps:
          - name: plant_log_recorded
            command: "python3 experiments/plant/scripts/verify_log.py {{recording}} {{rate}} {min_lines}"
        hosts:
          laptop: {{local: true}}
        """), encoding="utf-8")
    (exp / "config" / "preflight_checklist.json").write_text(json.dumps({"checklist": [
        {"text": "[HARD] The recording directory is writable.", "check": "recording_dir_writable",
         "args": {"path": "experiments/plant/recordings", "label": "recordings"}},
        {"text": "Enough disk for the recording.", "check": "disk_space",
         "args": {"path": ".", "min_gb": "0.01"}},
    ]}, indent=2), encoding="utf-8")
    (exp / "campaigns" / "rates.yaml").write_text(textwrap.dedent("""\
        name: rates
        experiment: plant
        describe: Both rates once.
        repetitions: 1
        sweep:
          rate: [slow, fast]
        """), encoding="utf-8")
    return root / "experiments"


def use_project(root: Path, monkeypatch) -> None:
    """Make ``root`` the project in force for this test, in this process and its children."""
    monkeypatch.chdir(root)
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(root))
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(root / "experiments"))
    monkeypatch.delenv(backend.ENV_BACKEND, raising=False)
    # The recorder and the gate start `python3 -m robofacts...`: they must find THIS package.
    monkeypatch.setenv("PYTHONPATH", str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("TRIALS_VOICE", "off")
    ti._recorder_at.cache_clear()


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    yield experiments
    ti._recorder_at.cache_clear()


# --------------------------------------------------------------------------- #
# The interfaces
# --------------------------------------------------------------------------- #
@pytest.fixture
def other_backend(monkeypatch):
    """A second backend, as another package would ship it: a module ``robofacts_other`` with
    a BACKEND object. It reuses the process launcher and recorder and counts the calls of
    its step before a gate."""
    module = types.ModuleType("robofacts_other")
    module.calls = []
    module.BACKEND = backend.Backend(name="other", launcher=pb.ProcessLauncher(), recorder=pb.ProcessRecorder(),
                                     gate_prepare=lambda: module.calls.append("prepare"))
    monkeypatch.setitem(sys.modules, "robofacts_other", module)
    monkeypatch.delitem(backend._LOADED, "other", raising=False)
    yield module
    backend._LOADED.pop("other", None)


def test_the_shipped_backend_implements_both_interfaces():
    found = backend.load("process")
    assert found.name == "process"
    assert isinstance(found.launcher, backend.Launcher)
    assert isinstance(found.recorder, backend.Recorder)
    assert found.recorder.noun == "recording"


def test_a_project_without_a_backend_key_is_refused_with_the_line_to_add(tmp_path, monkeypatch):
    """There is no default backend (Alejandro, 2026-11-25): a guess could read a project with
    the wrong recorder, and every recording would look empty."""
    (tmp_path / "trials.yaml").write_text("experiments_root: experiments\n", encoding="utf-8")
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(tmp_path))
    with pytest.raises(backend.BackendError, match="names no backend.*`backend: process`"):
        backend.configured_name(tmp_path)
    monkeypatch.setenv(backend.ENV_BACKEND, "process")
    assert backend.configured_name(tmp_path) == "process"


def test_trials_init_writes_the_backend_line(tmp_path, monkeypatch):
    from robofacts import scaffold
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRIALS_PROJECT_ROOT")
    monkeypatch.delenv("TRIALS_EXPERIMENTS_ROOT")
    assert scaffold.main_init([str(tmp_path / "p")]) == 0
    assert "\nbackend: process\n" in (tmp_path / "p" / "trials.yaml").read_text(encoding="utf-8")


def test_a_name_loads_the_package_of_that_backend_and_the_environment_overrides_the_file(
        project, monkeypatch, other_backend):
    assert backend.configured_name(project) == "process"
    assert backend.active(project).name == "process"
    monkeypatch.setenv(backend.ENV_BACKEND, "other")      # loads the module robofacts_other
    assert backend.active(project).name == "other"


def test_an_unknown_backend_is_refused_with_the_names_that_were_tried():
    with pytest.raises(backend.BackendError, match="robofacts_no_such_backend_anywhere"):
        backend.load("no_such_backend_anywhere")


def test_launch_keys_of_another_backend_are_refused_on_the_process_backend(project):
    """A project that forgot to name its backend must not run a trial without its stack."""
    runner = project / "plant" / "config" / "runner.yaml"
    text = runner.read_text(encoding="utf-8")
    start = text.index("launch:")
    runner.write_text(text[:start] + "launch: {package: demo_nodes_cpp, file: talker}\n"
                      + text[text.index("action:"):], encoding="utf-8")
    with pytest.raises(tr.RunnerError, match="belong to another backend.*`backend: <name>`"):
        tr.RunnerConfig.load("plant", project)


def test_the_process_launcher_templates_the_command_like_the_action():
    launcher = pb.ProcessLauncher()
    stack = launcher.parse({"launch": {"command": "plant --rate {rate} --port {arg_port}"}}, Path("r.yaml"))
    assert launcher.command(stack, {"port": 7}, {"rate": "fast"}) == ["plant", "--rate", "fast", "--port", "7"]
    assert launcher.command(launcher.parse({}, Path("r.yaml")), {}, {}) is None
    with pytest.raises(backend.BackendError, match="unknown factor"):
        launcher.command(stack, {}, {})


def test_a_backend_without_stop_patterns_makes_the_sweep_a_no_op(project, monkeypatch):
    from robofacts import remote_exec_utils as reu
    called = []
    monkeypatch.setattr(reu, "_pkill_local", lambda pattern, sig: called.append(pattern))
    reu.pkill_workspace_launches(None, project.parent, kind="launch")
    assert called == []
    with pytest.raises(ValueError, match="pkill kind"):
        reu.pkill_workspace_launches(None, project.parent, kind="not_a_kind")


# --------------------------------------------------------------------------- #
# The recorder process
# --------------------------------------------------------------------------- #
def test_the_recorder_keeps_only_what_was_written_while_it_ran(tmp_path):
    log, out = tmp_path / "sys.log", tmp_path / "rec"
    log.write_text("before the trial\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "robofacts.backends.process_recorder",
                             "--out", str(out), str(log), str(tmp_path / "never_written.log")],
                            env={**os.environ, "PYTHONPATH": str(PACKAGE)},
                            stdout=subprocess.PIPE, text=True)
    assert "recording 2 source(s)" in proc.stdout.readline()
    recorder = pb.ProcessRecorder()
    assert recorder.unsettled_reason(out) and not recorder.has_data(None, out)[0]
    with log.open("a", encoding="utf-8") as handle:
        handle.write("during 1\nduring 2\n")
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=10) == 0
    assert (out / "data" / "sys.log").read_text(encoding="utf-8") == "during 1\nduring 2\n"
    manifest = json.loads((out / pb.MANIFEST).read_text(encoding="utf-8"))
    assert [s["bytes"] for s in manifest["sources"]] == [18, 0]
    assert recorder.unsettled_reason(out) is None
    assert recorder.holds_data(out) and recorder.has_data(None, out) == (True, "")


def test_a_recording_without_data_is_not_a_recording(tmp_path):
    out = tmp_path / "rec"
    assert process_recorder.stored_names(["a/x.log", "b/x.log"]) == {"a/x.log": "x.log", "b/x.log": "2_x.log"}
    recorder = pb.ProcessRecorder()
    (out / "data").mkdir(parents=True)
    (out / pb.MANIFEST).write_text("{}", encoding="utf-8")
    ok, why = recorder.has_data(None, out)
    assert not ok and "no data" in why


# --------------------------------------------------------------------------- #
# Whole trials, without ROS
# --------------------------------------------------------------------------- #
def _run(project: Path, capsys, *extra: str):
    rc = tr.main([str(project / "plant" / "campaigns" / "rates.yaml"),
                  "--experiments-root", str(project), "--no-pause", *extra])
    return rc, capsys.readouterr().out


def test_a_dry_run_shows_the_launcher_and_the_recorder_commands(project, capsys):
    rc, out = _run(project, capsys, "--limit", "1")
    assert rc == 0
    assert re.search(r"\$ python3 experiments/plant/scripts/plant\.py --rate (slow|fast) --log", out), out
    assert "$ python3 -m robofacts.backends.process_recorder --out" in out
    assert "ros2" not in out
    assert not any((project / "plant" / "recordings").iterdir())


@pytest.mark.real_sleep
def test_a_campaign_runs_from_plan_to_coverage_index_without_ros(project, capsys):
    rc, out = _run(project, capsys, "--execute")
    assert rc == 0, out
    assert "2 trial(s): 2 OK" in out
    assert "[verify] plant_log_recorded rc=0" in out
    assert "ros2" not in out, "the process backend must not touch ROS, the daemon restart included"

    trials = ti.load_trials(project, "plant")
    assert sorted(t.metadata["rate"] for t in trials) == ["fast", "slow"]
    assert all(t.status == "usable" and t.has_bag_data for t in trials), [t.status for t in trials]
    for trial in trials:
        # Each recording holds its own trial's lines only: the recorder follows the log
        # from where it stood when the trial started.
        lines = (trial.bag_dir / "data" / "plant.log").read_text(encoding="utf-8").splitlines()
        assert lines and all(f"rate={trial.metadata['rate']}" in line for line in lines)
        assert trial.metadata["preflight"]["verdict"] == "GO"
        assert not (trial.bag_dir / "rosout.txt").exists()

    campaign = cs.load_campaign(project / "plant" / "campaigns" / "rates.yaml", project, [])
    plan = cp.build_plan(campaign, trials, False)
    assert not list(cp.execution_order(plan)), "the campaign is covered"
    # The stack was stopped: no plant process of this project is left.
    assert leftover_processes(str(project.parent) + ".*plant.py") == ""


@pytest.mark.real_sleep
def test_a_failing_verify_step_aborts_the_trial(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root, min_lines=100000)
    use_project(root, monkeypatch)
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert rc == 1 and "a verify step rejected the recorded trial" in out
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True and trial.status != "usable"
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
@pytest.mark.parametrize("signal_name, expected", [("SIGINT", "the action was interrupted"),
                                                   ("SIGTERM", "the action was stopped")])
def test_an_action_ended_by_a_signal_is_reported_as_interrupted(tmp_path, monkeypatch, capsys,
                                                               signal_name, expected):
    """An action that a signal ends (Ctrl-C reaches only the action, or a stop) did not fail on
    its own: the runner says so, names the signal, and still records the trial as aborted."""
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    (experiments / "plant" / "scripts" / "observe.py").write_text(
        "import os, signal\n"
        f"signal.signal(signal.{signal_name}, signal.SIG_DFL)\n"
        f"os.kill(os.getpid(), signal.{signal_name})\n", encoding="utf-8")
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert rc == 1, out
    assert f"{expected} ({signal_name})" in out, out
    assert "did not happen as planned" not in out
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.metadata["aborted"] is True and trial.status != "usable"
    assert trial.metadata["abort_reason"].startswith("action returned rc=-")
    assert signal_name in trial.metadata["abort_reason"]
    ti._recorder_at.cache_clear()


@pytest.mark.real_sleep
def test_a_stack_that_writes_nothing_leaves_no_trial(tmp_path, monkeypatch, capsys):
    """No stack, so nothing writes the log: the recorder finalizes an empty recording and
    the runner refuses to write metadata for it."""
    root = tmp_path / "proj"
    experiments = write_project(root, with_stack=False)
    use_project(root, monkeypatch)
    rc, out = _run(experiments, capsys, "--execute", "--limit", "1")
    assert rc == 1 and "holds no data" in out
    assert "no recording recorded" in out and "bag" not in out, out
    assert ti.load_trials(experiments, "plant") == []
    ti._recorder_at.cache_clear()


def test_the_lint_passes_on_a_project_of_the_process_backend(project):
    out = subprocess.run([sys.executable, "-m", "robofacts.cli", "lint", "--experiments-root", str(project)],
                         capture_output=True, text=True, cwd=project.parent)
    assert out.returncode == 0, out.stdout[-3000:]


def test_the_step_before_a_gate_is_the_backend_s(project, monkeypatch, other_backend):
    """The process backend has no step before the machine checks, another backend may."""
    from robofacts.preflight import run_preflight as rp
    rp.prepare_gate()
    assert other_backend.calls == []
    monkeypatch.setenv(backend.ENV_BACKEND, "other")
    rp.prepare_gate()
    assert other_backend.calls == ["prepare"]


def test_the_recording_directory_has_two_placeholder_names():
    assert tr.recording_placeholders("/r/t1") == {"bag": "/r/t1", "recording": "/r/t1"}
    # A project's own factor called `recording` keeps its placeholder.
    assert tr.recording_placeholders("/r/t1", {"recording": "x"}) == {"bag": "/r/t1"}


def test_the_index_and_the_planner_refuse_a_project_on_the_wrong_backend(project, capsys):
    """Without this, a project that forgot its `backend:` line would read as a project
    without trials: the recorder of the wrong backend finds no data in any recording."""
    from robofacts import cli
    runner = project / "plant" / "config" / "runner.yaml"
    text = runner.read_text(encoding="utf-8")
    runner.write_text(text[:text.index("launch:")] + "launch: {package: some_pkg, file: stack.launch.py}\n"
                      + text[text.index("action:"):], encoding="utf-8")
    with pytest.raises(backend.BackendError, match="belong to another backend"):
        ti.load_trials(project, "plant")
    for argv in (["index", "--experiments-root", str(project), "--summary"],
                 ["plan", str(project / "plant" / "campaigns" / "rates.yaml"), "--experiments-root", str(project)]):
        assert cli.main(argv) == 2
        assert "belong to another backend" in capsys.readouterr().err

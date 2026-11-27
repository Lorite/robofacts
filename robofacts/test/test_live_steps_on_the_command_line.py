"""`trials run` prints a campaign's operator_live_steps when each trial starts.

The web interface shows the live steps (what the operator does while the trial runs) on the
run page from the start of the run. The command line printed none of them, so an operator at
a terminal never read them. Now the runner prints them as each executed trial starts, before
the setup steps and the stack, with the trial's factor values filled in as in the operator
steps. They go into the run log, they are printed without a terminal too, and the runner never
waits for them.
"""
from __future__ import annotations

import builtins
import io
import re
import sys
from pathlib import Path

import pytest

from robofacts import campaign_spec as cs
from robofacts import trial_runner as tr

from test_process_backend import use_project, write_project

EXPERIMENT = "plant"
LIVE_STEPS = (
    "operator_steps:\n"
    "  - \"Put the plant on its mark.\"\n"
    "operator_live_steps:\n"
    "  - \"Watch the plant at the {rate} rate.\"\n"
    "  - text: \"Mark the {rate} trial in the notebook.\\nUse the {unknown} page.\"\n"
    "    run: \"echo  {rate}   mark\"\n"
)


class _Stdin(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__("")
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def experiments(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    experiments = write_project(root)
    use_project(root, monkeypatch)
    campaign = experiments / EXPERIMENT / "campaigns" / "rates.yaml"
    campaign.write_text(campaign.read_text(encoding="utf-8") + LIVE_STEPS, encoding="utf-8")
    return experiments


def _run(experiments: Path, capsys, *extra: str):
    rc = tr.main([str(experiments / EXPERIMENT / "campaigns" / "rates.yaml"),
                  "--experiments-root", str(experiments), "--limit", "1", *extra])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _trial(rate: str = "fast") -> cs.TrialSpec:
    return cs.TrialSpec(EXPERIMENT, {"rate": rate}, ["rate"])


def test_the_live_steps_are_printed_with_the_trials_levels_and_never_wait(monkeypatch, capsys):
    def no_input(*_args):
        raise AssertionError("the live steps must never wait for input")
    monkeypatch.setattr(builtins, "input", no_input)
    steps = [{"text": "Watch the plant at the {rate} rate.", "run": ""},
             {"text": "Mark the {rate} trial.\nUse the {unknown} page.", "run": "echo  {rate}   mark"},
             {"text": "Keep {robot.name} and {} as written.", "run": ""}]
    tr.print_live_steps(_trial(), steps)
    out = capsys.readouterr().out
    assert "LIVE STEPS for" in out and "does not wait for them" in out
    assert "1. Watch the plant at the fast rate." in out
    assert "2. Mark the fast trial." in out
    # A brace that names no factor stays as written, on the step's next line.
    assert "     Use the {unknown} page." in out
    # The one-liner of the web interface's button, with the trial's level.
    assert "     command: echo fast mark" in out
    # A text the filling cannot handle is printed as written, and the trial goes on.
    assert "3. Keep {robot.name} and {} as written." in out


def test_no_live_steps_print_nothing(capsys):
    tr.print_live_steps(_trial(), [])
    tr.print_live_steps(_trial(), None)
    assert capsys.readouterr().out == ""


@pytest.mark.real_sleep
def test_an_executed_run_without_a_terminal_prints_them_into_the_run_log(experiments, monkeypatch,
                                                                          capsys):
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    rc, out, err = _run(experiments, capsys, "--execute")
    assert rc == 0 and "1 OK" in out, (out, err)
    level = re.search(r"1\. Watch the plant at the (slow|fast) rate\.", out)
    assert level, out
    assert f"command: echo {level.group(1)} mark" in out
    # Printed as the trial starts: after its operator steps, before its stack and its gate.
    assert out.index("OPERATOR STEPS for") < out.index("LIVE STEPS for") < out.index("running gate")
    logs = sorted((experiments / EXPERIMENT / "runs").glob("*.log"))
    trial_logs = [p for p in logs if "LIVE STEPS for" in p.read_text(encoding="utf-8")]
    assert len(trial_logs) == 1, [p.name for p in logs]
    assert level.group(0) in trial_logs[0].read_text(encoding="utf-8")


@pytest.mark.real_sleep
def test_at_a_terminal_they_add_no_wait(experiments, monkeypatch, capsys):
    """The two waits of a simulation trial stay the only ones: the operator steps and the
    pose pause."""
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=True))
    prompts = []
    monkeypatch.setattr(builtins, "input", lambda *_a: prompts.append(1) or "")
    rc, out, err = _run(experiments, capsys, "--execute")
    assert rc == 0 and "1 OK" in out, (out, err)
    assert "LIVE STEPS for" in out
    assert len(prompts) == 2, out


@pytest.mark.real_sleep
def test_a_scratch_trial_prints_them_too(experiments, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _Stdin(tty=False))
    rc, out, err = _run(experiments, capsys, "--execute", "--scratch")
    assert rc == 0, (out, err)
    assert "LIVE STEPS for" in out
    assert "1. Watch the plant at the slow rate." in out   # the campaign's first level


def test_a_dry_run_lists_them(experiments, capsys):
    rc, out, err = _run(experiments, capsys)
    assert rc == 0, (out, err)
    assert "[dry-run] live steps of this trial" in out
    assert re.search(r"\[dry-run\]   1\. Watch the plant at the (slow|fast) rate\.", out), out
    assert "LIVE STEPS for" not in out   # nothing started, so nothing to do yet

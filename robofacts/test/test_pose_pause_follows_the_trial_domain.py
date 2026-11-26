"""The pose pause follows the trial's domain, not runner.yaml alone (2026-11-26).

The docs review found it: the pause asked for the word fly only when runner.yaml said
`simulation: false`, while the refusal of a hardware run without a terminal used the trial's
domain, which a factor level can set (`domain: {level: hardware}`). So such a level in a
`simulation: true` experiment was refused without a terminal and then started on a plain
Enter. The spoken GO, the voice defaults and the run marker for the stop tools followed the
same flag. Alejandro: "Yes, fix it".
"""
from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest

from robofacts import campaign_spec as cs
from robofacts import trial_runner as tr

from test_process_backend import use_project, write_project

EXPERIMENT = "plant"


class _Terminal(io.StringIO):
    """A stdin that is a terminal and answers with the given lines."""

    def isatty(self) -> bool:
        return True


def _factor(domain):
    return cs.Factor(name="rate", describe="", levels=["slow", "fast"], default="slow",
                     metadata_key="rate", maps_to={"kind": "internal"}, domain=domain)


@pytest.mark.parametrize("simulation, domain, value, hardware", [
    (True, {}, "fast", False),                       # a simulation
    (False, {}, "fast", True),                       # a hardware experiment
    (True, {"fast": "hardware"}, "fast", True),      # a hardware level in a simulation experiment
    (True, {"fast": "hardware"}, "slow", False),     # the other level stays a simulation
    (False, {"slow": "simulation"}, "slow", False),  # a simulated level on a hardware experiment
])
def test_the_trial_domain_decides(simulation, domain, value, hardware):
    trial = SimpleNamespace(values={"rate": value})
    config = SimpleNamespace(simulation=simulation)
    assert tr.trial_is_hardware(trial, {"rate": _factor(domain)}, config) is hardware


def _project(tmp_path, monkeypatch, *, hardware_level: bool):
    root = tmp_path / "proj"
    experiments = write_project(root)          # runner.yaml says simulation: true
    use_project(root, monkeypatch)
    if hardware_level:
        factors = experiments / EXPERIMENT / "config" / "factors.yaml"
        factors.write_text(factors.read_text(encoding="utf-8").replace(
            "    metadata_key: rate\n",
            "    metadata_key: rate\n    domain: {slow: hardware, fast: hardware}\n"), encoding="utf-8")
    return experiments


def _run(experiments, monkeypatch, capsys, answers: str):
    monkeypatch.setattr(sys, "stdin", _Terminal(answers))
    rc = tr.main([str(experiments / EXPERIMENT / "campaigns" / "rates.yaml"),
                  "--experiments-root", str(experiments), "--execute", "--limit", "1"])
    return rc, capsys.readouterr().out


@pytest.mark.real_sleep
def test_a_hardware_level_in_a_simulation_experiment_is_not_started_by_enter(tmp_path, monkeypatch, capsys):
    experiments = _project(tmp_path, monkeypatch, hardware_level=True)
    rc, out = _run(experiments, monkeypatch, capsys, "\n\n\n")
    assert "Type fly and Enter to record and act" in out, out[-3000:]
    assert "Recording starts on Enter" not in out
    assert "no 'fly' typed" in out and "0 OK" in out, out[-3000:]


@pytest.mark.real_sleep
def test_a_hardware_level_in_a_simulation_experiment_starts_on_fly(tmp_path, monkeypatch, capsys):
    experiments = _project(tmp_path, monkeypatch, hardware_level=True)
    rc, out = _run(experiments, monkeypatch, capsys, "fly\n")
    assert "Type fly and Enter to record and act" in out, out[-3000:]
    assert "1 OK" in out, out[-3000:]


@pytest.mark.real_sleep
def test_a_simulation_still_starts_on_enter(tmp_path, monkeypatch, capsys):
    experiments = _project(tmp_path, monkeypatch, hardware_level=False)
    rc, out = _run(experiments, monkeypatch, capsys, "\n")
    assert "Recording starts on Enter" in out and "Type fly" not in out, out[-3000:]
    assert "1 OK" in out, out[-3000:]

"""The domain field and the coverage view per condition and domain (2026-10-06).

SAC 2027 framework paper, Priority 2 C: every trial records `domain`
(simulation or hardware), older trials get a DERIVED domain from the same
sources, and ``trial_index.py --condition-space`` counts one condition's
levels across experiments, per domain, with a status per level. No ROS.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import campaign_spec as cs
from robofacts import trial_index as ti
from robofacts import trial_runner as tr
CORE = """
version: 1
factors:
  execution_host:
    levels: [laptop]
    default: laptop
    metadata_key: execution_host
    maps_to: {kind: host}
"""

SIM_FACTORS = """
version: 1
factors:
  lighting:
    levels: [lights_on, dark]
    default: lights_on
    metadata_key: lighting
    maps_to: {kind: internal}
"""

HW_FACTORS = """
version: 1
factors:
  lighting:
    levels: [lights_on, lights_dimmed]
    default: lights_on
    metadata_key: lighting
    maps_to: {kind: operator}
    unchecked: test
  backend:
    levels: [cpp, sim]
    default: cpp
    metadata_key: backend
    maps_to: {kind: internal}
    domain: {sim: simulation}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  aborted: {required: true, type: bool}
"""


def _runner(simulation):
    flag = "" if simulation is None else f"simulation: {str(simulation).lower()}\n"
    return (flag + "launch: {package: p, file: f.launch.py}\nsettle_s: 0\nstop_grace_s: 0\n"
            "record_topics: [/x, /rosout]\naction: {command: fly}\nhosts:\n  laptop: {local: true}\n")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE)
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT)
    for name, factors, sim in (("simexp", SIM_FACTORS, True), ("hwexp", HW_FACTORS, False),
                               ("oldexp", SIM_FACTORS, None)):
        cfg = tmp_path / name / "config"
        cfg.mkdir(parents=True)
        (cfg / "factors.yaml").write_text(factors)
        (cfg / "runner.yaml").write_text(_runner(sim))
    return tmp_path


def _bag(root, exp, name, meta):
    d = root / exp / "bags" / name
    d.mkdir(parents=True)
    (d / "metadata.yaml").write_text("x")
    (d / f"{name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")
    meta = {"trial_id": name, "aborted": False, **meta}
    (d / "trial_metadata.json").write_text(json.dumps(meta))


def _checked(level, result="PASS", overridden=False):
    return {"preflight": {"overridden": overridden, "factor_checks": [
        {"factor": "lighting", "level": level, "check": "image_brightness", "result": result}]}}


# --------------------------------------------------------------------------- #
# The runner writes the domain
# --------------------------------------------------------------------------- #
@pytest.mark.ros2
@pytest.mark.parametrize("exp,values,expected", [
    ("simexp", {"lighting": "dark"}, ("simulation", "runner.yaml simulation: true")),
    ("hwexp", {"lighting": "lights_on", "backend": "cpp"}, ("hardware", "runner.yaml simulation: false")),
    ("hwexp", {"lighting": "lights_on", "backend": "sim"}, ("simulation", "factor backend=sim")),
])
def test_the_trial_metadata_records_its_domain(root, exp, values, expected):
    factors = cs.load_factors(exp, root)
    config = tr.RunnerConfig.load(exp, root)
    trial = cs.TrialSpec(exp, {"execution_host": "laptop", **values}, [], 1)
    meta = tr.trial_metadata(trial, factors, config, trial_id="t")
    assert (meta["domain"], meta["domain_basis"]) == expected


def test_an_unknown_domain_level_is_refused(root):
    (root / "hwexp" / "config" / "factors.yaml").write_text(
        HW_FACTORS.replace("domain: {sim: simulation}", "domain: {sim: virtual}"))
    with pytest.raises(cs.SpecError, match="virtual"):
        cs.load_factors("hwexp", root)


@pytest.mark.ros2
def test_the_runner_knows_whether_simulation_was_declared(root):
    assert tr.RunnerConfig.load("simexp", root).simulation_declared
    assert not tr.RunnerConfig.load("oldexp", root).simulation_declared


# --------------------------------------------------------------------------- #
# Older trials: a derived domain, or unknown
# --------------------------------------------------------------------------- #
def test_recorded_then_derived_then_unknown(root):
    _bag(root, "simexp", "a", {"lighting": "dark", "domain": "simulation"})
    _bag(root, "hwexp", "b", {"lighting": "lights_on", "backend": "sim"})
    _bag(root, "hwexp", "c", {"lighting": "lights_on", "backend": "cpp"})
    _bag(root, "oldexp", "d", {"lighting": "dark"})
    resolver = ti.DomainResolver(root)
    got = {t.bag_dir.name: resolver.resolve(t) for t in ti.load_trials(root, validate=False)}
    assert got == {"a": ("simulation", "recorded"), "b": ("simulation", "derived"),
                   "c": ("hardware", "derived"), "d": ("unknown", "unknown")}


# --------------------------------------------------------------------------- #
# The view per condition and domain
# --------------------------------------------------------------------------- #
SPACE = {"name": "lighting", "levels": ["lights_on", "lights_dimmed", "dark", "backlit"],
         "require_check": True,
         "experiments": [{"experiment": "simexp", "factor": "lighting"},
                         {"experiment": "hwexp", "factor": "lighting"}]}


def _cov(root, space=SPACE):
    return {r["level"]: r for r in
            ti.condition_coverage(space, ti.load_trials(root, validate=False), root)["levels"]}


@pytest.mark.ros2
def test_levels_are_counted_per_domain_with_a_status(root):
    _bag(root, "simexp", "s1", {"lighting": "lights_on", "domain": "simulation", **_checked("lights_on")})
    _bag(root, "simexp", "s2", {"lighting": "dark", "domain": "simulation", **_checked("dark")})
    _bag(root, "hwexp", "h1", {"lighting": "lights_on", "domain": "hardware", **_checked("lights_on")})
    _bag(root, "hwexp", "h2", {"lighting": "lights_dimmed", "domain": "hardware",
                               **_checked("lights_dimmed")})
    cov = _cov(root)
    assert (cov["lights_on"]["simulation"], cov["lights_on"]["hardware"],
            cov["lights_on"]["status"]) == (1, 1, "both")
    assert cov["dark"]["status"] == "simulation only"
    assert cov["lights_dimmed"]["status"] == "hardware only"
    assert cov["backlit"]["status"] == "none"


@pytest.mark.ros2
def test_only_confirmed_usable_trials_count(root):
    _bag(root, "simexp", "fail", {"lighting": "dark", "domain": "simulation",
                                  **_checked("dark", result="FAIL")})
    _bag(root, "simexp", "override", {"lighting": "dark", "domain": "simulation",
                                      **_checked("dark", overridden=True)})
    _bag(root, "simexp", "nocheck", {"lighting": "dark", "domain": "simulation"})
    _bag(root, "simexp", "aborted", {"lighting": "dark", "domain": "simulation", "aborted": True,
                                     **_checked("dark")})
    cov = _cov(root)
    assert cov["dark"]["simulation"] == 0 and cov["dark"]["status"] == "none"
    assert cov["dark"]["unchecked"] == 3, "usable but unconfirmed is reported, never counted"
    relaxed = _cov(root, dict(SPACE, require_check=False))
    assert relaxed["dark"]["simulation"] == 3


@pytest.mark.ros2
def test_a_simulated_backend_counts_as_simulation_in_a_hardware_experiment(root):
    _bag(root, "hwexp", "old_sim_backend", {"lighting": "lights_on", "backend": "sim",
                                            **_checked("lights_on")})
    cov = _cov(root)
    assert cov["lights_on"]["simulation"] == 1 and cov["lights_on"]["hardware"] == 0
    assert cov["lights_on"]["derived_domain"] == 1


@pytest.mark.ros2
def test_refused_attempts_and_level_maps(root):
    runs = root / "hwexp" / "runs"
    runs.mkdir()
    runs.joinpath("attempts.jsonl").write_text(json.dumps({"status": "blocked", "gate": {
        "factor_checks": [{"factor": "lighting", "level": "lights_dimmed", "result": "FAIL"}]}}) + "\n")
    _bag(root, "hwexp", "h", {"lighting": "lights_dimmed", "domain": "hardware",
                              **_checked("lights_dimmed")})
    space = dict(SPACE, experiments=[{"experiment": "hwexp", "factor": "lighting",
                                      "map": {"lights_dimmed": "dark"}}])
    cov = _cov(root, space)
    assert cov["dark"]["hardware"] == 1 and cov["dark"]["refused_at_t0"] == 1
    assert cov["lights_dimmed"]["hardware"] == 0


def test_an_experiment_without_the_factor_is_a_note_not_an_error(root):
    space = dict(SPACE, experiments=[{"experiment": "simexp", "factor": "weather"}])
    cov = ti.condition_coverage(space, [], root)
    assert any("weather" in n for n in cov["notes"])


@pytest.mark.ros2
def test_the_condition_space_cli(root, tmp_path, capsys):
    _bag(root, "simexp", "s1", {"lighting": "dark", "domain": "simulation", **_checked("dark")})
    path = tmp_path / "space.yaml"
    import yaml
    path.write_text(yaml.safe_dump(SPACE))
    assert ti.main(["--experiments-root", str(root), "--condition-space", str(path),
                    "--no-validate", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {r["level"]: r["status"] for r in out["levels"]}["dark"] == "simulation only"

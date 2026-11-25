"""Tests for the declarative trial/campaign spec."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import campaign_spec as cs
#: The experiments of this package's example project (the "shipped" declarations).
EXPERIMENTS_ROOT = HERE.parent / "example_project" / "experiments"

CORE = """
version: 1
factors:
  execution_host:
    describe: "where it runs"
    levels: [laptop, orin]
    default: orin
    metadata_key: execution_host
    maps_to: {kind: host}
  record_profile:
    levels: [light, full]
    default: light
    metadata_key: record_profile
    maps_to: {kind: record_profile}
"""

EXPERIMENT = """
version: 1
factors:
  trajectory:
    levels: [hover, circle, figure8]
    default: hover
    metadata_key: trajectory
    maps_to: {kind: launch_arg, name: trajectory}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  record_profile: {required: false, type: str, factor: true}
  trajectory: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "factors.yaml").write_text(EXPERIMENT, encoding="utf-8")
    return tmp_path


def campaign_file(root: Path, body: str) -> Path:
    path = root / "expA" / "campaigns" / "c.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


BASIC = """
name: c
experiment: expA
sweep:
  trajectory: [hover, circle]
"""


# --------------------------------------------------------------------------- #
# The shipped definitions
# --------------------------------------------------------------------------- #
def test_shipped_core_factors_load():
    factors = cs.load_factors()
    assert "execution_host" in factors and "record_profile" in factors


def test_every_shipped_experiment_factor_file_loads():
    for path in EXPERIMENTS_ROOT.glob("*/config/factors.yaml"):
        assert cs.load_factors(path.parents[1].name)


def test_every_shipped_campaign_loads_and_is_measurable():
    """A shipped campaign must be valid AND its factors recordable, or its
    coverage could never be measured.

    One exception, and it is a feature rather than a hole: a campaign asking for
    a level the factor space declares `blocked:` is REFUSED on purpose, because
    the apparatus cannot deliver it (see BLOCKED_MARKER). Such a campaign is
    skipped here and policed by the config lint's L10 instead, which checks that
    a blocked level is the ONLY thing wrong with it.
    """
    for path in EXPERIMENTS_ROOT.glob("*/campaigns/*.yaml"):
        problems = cs.campaign_problems(path)
        if any(cs.BLOCKED_MARKER in p for p in problems):
            continue
        campaign = cs.load_campaign(path)
        assert campaign.size() > 0
        assert cs.validate_against_metadata_contract(campaign) == [], path


# --------------------------------------------------------------------------- #
# Factor definitions
# --------------------------------------------------------------------------- #
def test_experiment_factors_merge_over_core(root):
    factors = cs.load_factors("expA", root)
    assert set(factors) == {"execution_host", "record_profile", "trajectory"}


def test_an_experiment_can_narrow_a_core_factor(root):
    (root / "expA" / "config" / "factors.yaml").write_text(EXPERIMENT + """
  record_profile:
    levels: [light]
    default: light
    metadata_key: record_profile
    maps_to: {kind: record_profile}
""", encoding="utf-8")
    assert cs.load_factors("expA", root)["record_profile"].levels == ["light"]
    assert cs.load_factors(None, root)["record_profile"].levels == ["light", "full"]


def test_a_default_outside_its_levels_is_rejected(root):
    (root / "common" / "config" / "factors.yaml").write_text("""
version: 1
factors:
  x: {levels: [a, b], default: z, maps_to: {kind: internal}}
""", encoding="utf-8")
    with pytest.raises(cs.SpecError, match="not among its levels"):
        cs.load_factors(None, root)


def test_a_factor_without_a_default_is_rejected(root):
    """The default is what 'non-default' is measured against, and marking
    non-defaults is the whole readability of a plan."""
    (root / "common" / "config" / "factors.yaml").write_text("""
version: 1
factors:
  x: {levels: [a, b], maps_to: {kind: internal}}
""", encoding="utf-8")
    with pytest.raises(cs.SpecError, match="needs a 'default'"):
        cs.load_factors(None, root)


def test_a_launch_arg_without_a_name_is_rejected(root):
    (root / "common" / "config" / "factors.yaml").write_text("""
version: 1
factors:
  x: {levels: [a], default: a, maps_to: {kind: launch_arg}}
""", encoding="utf-8")
    with pytest.raises(cs.SpecError, match="names no argument"):
        cs.load_factors(None, root)


def test_an_unknown_maps_to_kind_is_rejected(root):
    (root / "common" / "config" / "factors.yaml").write_text("""
version: 1
factors:
  x: {levels: [a], default: a, maps_to: {kind: telepathy}}
""", encoding="utf-8")
    with pytest.raises(cs.SpecError, match="unknown maps_to.kind"):
        cs.load_factors(None, root)


# --------------------------------------------------------------------------- #
# Campaign expansion
# --------------------------------------------------------------------------- #
def test_cross_product_size_and_repetitions(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC + "repetitions: 3\n"), root)
    assert campaign.size() == 6
    assert len(list(campaign.combinations())) == 6


def test_unswept_factors_take_their_default(root):
    trial = next(cs.load_campaign(campaign_file(root, BASIC), root).combinations())
    assert trial.values["execution_host"] == "orin"
    assert trial.values["record_profile"] == "light"


def test_fixed_overrides_the_default(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC + """
fixed:
  execution_host: laptop
"""), root)
    assert all(t.values["execution_host"] == "laptop" for t in campaign.combinations())


def test_non_defaults_are_marked(root):
    """The UI marks non-default values, so the spec must know which they are."""
    campaign = cs.load_campaign(campaign_file(root, BASIC + """
fixed:
  execution_host: laptop
"""), root)
    trials = {t.values["trajectory"]: t for t in campaign.combinations()}
    assert "execution_host" in trials["hover"].non_defaults
    assert "trajectory" not in trials["hover"].non_defaults, "hover is the default"
    assert "trajectory" in trials["circle"].non_defaults


def test_launch_args_and_host_are_extracted(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC), root)
    trial = next(campaign.combinations())
    assert trial.launch_args(campaign.factors) == {"trajectory": "hover"}
    assert trial.host(campaign.factors) == "orin"
    assert "execution_host" not in trial.launch_args(campaign.factors)


def test_trial_key_ignores_repetition(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC + "repetitions: 2\n"), root)
    trials = list(campaign.combinations())
    same = [t for t in trials if t.values["trajectory"] == "hover"]
    assert len({t.key() for t in same}) == 1, "repetitions are the same combination"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_unknown_factor_is_rejected(root):
    with pytest.raises(cs.SpecError, match="unknown factor"):
        cs.load_campaign(campaign_file(root, """
experiment: expA
sweep: {nonsense: [a]}
"""), root)


def test_undeclared_level_is_rejected(root):
    with pytest.raises(cs.SpecError, match="not a declared level"):
        cs.load_campaign(campaign_file(root, """
experiment: expA
sweep: {trajectory: [hover, teleport]}
"""), root)


def test_a_factor_cannot_be_both_swept_and_fixed(root):
    with pytest.raises(cs.SpecError, match="both swept and fixed"):
        cs.load_campaign(campaign_file(root, """
experiment: expA
sweep: {trajectory: [hover]}
fixed: {trajectory: circle}
"""), root)


def test_missing_experiment_is_rejected(root):
    with pytest.raises(cs.SpecError, match="'experiment' is required"):
        cs.load_campaign(campaign_file(root, "sweep: {trajectory: [hover]}\n"), root)


# --------------------------------------------------------------------------- #
# Blocked levels — a level whose apparatus does not exist
# --------------------------------------------------------------------------- #
BLOCKING_EXPERIMENT = EXPERIMENT + """  airframe:
    levels: [bare, winged]
    default: bare
    metadata_key: airframe
    blocked:
      winged: "the wings were never built"
    maps_to: {kind: internal}
"""


def _with_blocking(root: Path) -> Path:
    (root / "expA" / "config" / "factors.yaml").write_text(BLOCKING_EXPERIMENT, encoding="utf-8")
    return root


def test_sweeping_a_blocked_level_is_refused(root):
    with pytest.raises(cs.SpecError, match=cs.BLOCKED_MARKER):
        cs.load_campaign(campaign_file(_with_blocking(root), """
experiment: expA
sweep: {airframe: [bare, winged]}
"""), root)


def test_fixing_a_blocked_level_is_refused(root):
    """The trap that motivated this: the level was FIXED, not swept, and an
    operator-applied factor has nothing at the rig that could refuse it."""
    with pytest.raises(cs.SpecError, match="the wings were never built"):
        cs.load_campaign(campaign_file(_with_blocking(root), """
experiment: expA
fixed: {airframe: winged}
"""), root)


def test_a_blocked_level_still_loads_for_campaigns_that_do_not_ask_for_it(root):
    """Blocking must not retire the level: bags already recorded on it have to
    keep matching, and every campaign that never names it is unaffected."""
    campaign = cs.load_campaign(campaign_file(_with_blocking(root), """
experiment: expA
sweep: {trajectory: [hover, circle]}
"""), root)
    assert campaign.size() == 2
    assert "winged" in campaign.factors["airframe"].levels


def test_campaign_problems_reports_without_raising(root):
    """What the config lint uses to tell a parked campaign from a broken one."""
    problems = cs.campaign_problems(campaign_file(_with_blocking(root), """
experiment: expA
fixed: {airframe: winged}
"""), root)
    assert problems and all(cs.BLOCKED_MARKER in p for p in problems)


def test_blocking_a_level_with_no_reason_is_rejected(root):
    (root / "expA" / "config" / "factors.yaml").write_text(EXPERIMENT + """  airframe:
    levels: [bare, winged]
    default: bare
    metadata_key: airframe
    blocked: {winged: "  "}
    maps_to: {kind: internal}
""", encoding="utf-8")
    with pytest.raises(cs.SpecError, match="with no reason"):
        cs.load_factors("expA", root)


def test_blocking_an_unknown_level_is_rejected(root):
    (root / "expA" / "config" / "factors.yaml").write_text(EXPERIMENT + """  airframe:
    levels: [bare, winged]
    default: bare
    metadata_key: airframe
    blocked: {jetpack: "no"}
    maps_to: {kind: internal}
""", encoding="utf-8")
    with pytest.raises(cs.SpecError, match="blocks unknown level"):
        cs.load_factors("expA", root)


def test_blocking_the_default_is_rejected(root):
    """A blocked default would refuse campaigns that never mentioned the factor,
    naming a level nobody chose."""
    (root / "expA" / "config" / "factors.yaml").write_text(EXPERIMENT + """  airframe:
    levels: [bare, winged]
    default: bare
    metadata_key: airframe
    blocked: {bare: "no"}
    maps_to: {kind: internal}
""", encoding="utf-8")
    with pytest.raises(cs.SpecError, match="blocks its own default"):
        cs.load_factors("expA", root)


# --------------------------------------------------------------------------- #
# Hydra-shaped overrides
# --------------------------------------------------------------------------- #
def test_override_with_one_value_pins(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC), root, ["trajectory=figure8"])
    assert campaign.size() == 1
    assert next(campaign.combinations()).values["trajectory"] == "figure8"


def test_override_with_several_values_sweeps(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC), root,
                                ["trajectory=hover,circle,figure8"])
    assert campaign.size() == 3


def test_override_can_add_a_factor_not_in_the_file(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC), root, ["execution_host=laptop,orin"])
    assert campaign.size() == 4  # 2 trajectories x 2 hosts


def test_override_rejects_an_illegal_level(root):
    with pytest.raises(cs.SpecError, match="not a declared level"):
        cs.load_campaign(campaign_file(root, BASIC), root, ["trajectory=teleport"])


def test_malformed_override_is_rejected():
    with pytest.raises(cs.SpecError, match="must be name=value"):
        cs.parse_overrides(["trajectory"])


# --------------------------------------------------------------------------- #
# The loop-closing cross-check
# --------------------------------------------------------------------------- #
def test_sweeping_an_unrecorded_factor_is_flagged(root):
    """A campaign whose factor never reaches trial_metadata produces bags whose
    grid silently collapses that axis — coverage that looks real and is not."""
    (root / "expA" / "config" / "factors.yaml").write_text(EXPERIMENT + """
  secret_knob:
    levels: [a, b]
    default: a
    maps_to: {kind: internal}
""", encoding="utf-8")
    campaign = cs.load_campaign(campaign_file(root, """
experiment: expA
sweep: {secret_knob: [a, b]}
"""), root)
    problems = cs.validate_against_metadata_contract(campaign, root)
    assert any("no metadata_key" in p for p in problems)


def test_factor_recorded_but_not_declared_as_an_axis_is_flagged(root):
    (root / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT.replace(
        "trajectory: {required: false, type: str, factor: true}",
        "trajectory: {required: false, type: str, factor: false}"), encoding="utf-8")
    campaign = cs.load_campaign(campaign_file(root, BASIC), root)
    problems = cs.validate_against_metadata_contract(campaign, root)
    assert any("not as a coverage axis (`factor: true`)" in p for p in problems)


def test_a_measurable_campaign_has_no_warnings(root):
    campaign = cs.load_campaign(campaign_file(root, BASIC), root)
    assert cs.validate_against_metadata_contract(campaign, root) == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_lists_the_factor_space(root, capsys):
    assert cs.main(["--experiments-root", str(root), "--factors", "expA"]) == 0
    assert "trajectory" in capsys.readouterr().out


def test_cli_expands_a_campaign(root, capsys):
    path = campaign_file(root, BASIC)
    assert cs.main([str(path), "--experiments-root", str(root)]) == 0
    assert "2 trial(s)" in capsys.readouterr().out


def test_cli_reports_a_bad_campaign_without_a_traceback(root, capsys):
    path = campaign_file(root, "experiment: expA\nsweep: {trajectory: [teleport]}\n")
    assert cs.main([str(path), "--experiments-root", str(root)]) == 2
    assert "ERROR" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The contract is a REFUSAL, not a warning (2026-10-02, found by the E3 study:
# the guide and the paper said "refused" while the CLIs exited 0)
# --------------------------------------------------------------------------- #
UNRECORDED = """
  secret_knob:
    levels: [a, b]
    default: a
    maps_to: {kind: internal}
"""


def _unrecorded_campaign(root):
    (root / "expA" / "config" / "factors.yaml").write_text(EXPERIMENT + UNRECORDED,
                                                          encoding="utf-8")
    return campaign_file(root, "experiment: expA\nsweep: {secret_knob: [a, b]}\n")


def test_a_swept_factor_without_metadata_key_is_refused_by_name(root, capsys):
    path = _unrecorded_campaign(root)
    refusal = cs.contract_refusal(cs.load_campaign(path, root), root)
    assert refusal and cs.CONTRACT_REFUSAL in refusal and "secret_knob" in refusal
    assert cs.main([str(path), "--experiments-root", str(root)]) == 2
    err = capsys.readouterr().err
    assert "secret_knob" in err and "no metadata_key" in err


def test_a_swept_factor_not_declared_as_an_axis_is_refused(root, capsys):
    (root / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT.replace(
        "trajectory: {required: false, type: str, factor: true}",
        "trajectory: {required: false, type: str, factor: false}"), encoding="utf-8")
    path = campaign_file(root, BASIC)
    assert cs.main([str(path), "--experiments-root", str(root)]) == 2
    assert "trajectory" in capsys.readouterr().err


def test_campaign_plan_refuses_it_too(root, capsys):
    from robofacts import campaign_plan as cp
    path = _unrecorded_campaign(root)
    assert cp.main([str(path), "--experiments-root", str(root)]) == 2
    assert "secret_knob" in capsys.readouterr().err


@pytest.mark.ros2
def test_the_runner_refuses_it_before_any_trial_starts(root, capsys):
    from robofacts import campaign_plan as cp
    from robofacts import trial_runner as tr
    (root / "expA" / "config" / "runner.yaml").write_text(
        "launch: {package: p, file: f.launch.py}\naction: {command: 'fly {secret_knob}'}\n",
        encoding="utf-8")
    path = _unrecorded_campaign(root)
    assert tr.main([str(path), "--experiments-root", str(root)]) == 2
    assert "secret_knob" in capsys.readouterr().err
    plan = cp.build_plan(cs.load_campaign(path, root), [])
    config = tr.RunnerConfig.load("expA", root)
    with pytest.raises(tr.RunnerError, match="secret_knob"):
        tr.run_plan(plan, config, root / "expA" / "bags", dry_run=True)


def test_a_measurable_campaign_still_plans_and_runs_dry(root):
    from robofacts import campaign_plan as cp
    path = campaign_file(root, BASIC)
    assert cs.main([str(path), "--experiments-root", str(root)]) == 0
    assert cp.main([str(path), "--experiments-root", str(root)]) == 0

"""Tests for the campaign expander (#96 Phase 3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import trial_index as ti
CORE = """
version: 1
factors:
  execution_host:
    levels: [laptop, orin]
    default: orin
    metadata_key: execution_host
    maps_to: {kind: host}
    aliases:
      laptop: [local, lori-ThinkPad-P15-Gen-2i]
  trajectory:
    levels: [hover, circle]
    default: hover
    metadata_key: trajectory
    maps_to: {kind: launch_arg, name: trajectory}
  knob:
    levels: [a, b]
    default: a
    maps_to: {kind: internal}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  trajectory: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 3
sweep:
  trajectory: [hover, circle]
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    return tmp_path


def add_trial(root: Path, experiment: str, name: str, metadata: dict, issues: bool = False):
    bag = root / experiment / ("bags_issues" if issues else "bags") / name
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    # A real bag also has rosbag2's own files; without them the dir is not a trial.
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n", encoding="utf-8")
    (bag / f"{name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")


def plan_for(root: Path, overrides=None, include_unverified=False) -> cp.Plan:
    campaign = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root, overrides or [])
    trials = ti.load_trials(root, "expA")
    return cp.build_plan(campaign, trials, include_unverified)


GOOD = {"trial_id": "t", "execution_host": "orin", "aborted": False}


# --------------------------------------------------------------------------- #
# The core arithmetic
# --------------------------------------------------------------------------- #
def test_nothing_recorded_means_everything_to_fly(root):
    plan = plan_for(root)
    assert (plan.wanted, plan.covered, plan.needed) == (6, 0, 6)


def test_usable_trials_reduce_what_is_needed(root):
    add_trial(root, "expA", "t1", dict(GOOD, trajectory="hover"))
    add_trial(root, "expA", "t2", dict(GOOD, trial_id="b", trajectory="hover"))
    plan = plan_for(root)
    hover = next(c for c in plan.cells if c.values["trajectory"] == "hover")
    assert hover.usable == 2 and hover.needed() == 1
    assert plan.needed == 4


def test_more_bags_than_wanted_never_goes_negative(root):
    for i in range(5):
        add_trial(root, "expA", f"t{i}", dict(GOOD, trial_id=str(i), trajectory="hover"))
    plan = plan_for(root)
    hover = next(c for c in plan.cells if c.values["trajectory"] == "hover")
    assert hover.needed() == 0
    assert plan.needed == 3, "the circle cell is still entirely missing"


# --------------------------------------------------------------------------- #
# Which trials may count — the Phase 1 three-state split doing its job
# --------------------------------------------------------------------------- #
def test_aborted_trials_never_count_as_coverage(root):
    add_trial(root, "expA", "t1", dict(GOOD, trajectory="hover", aborted=True,
                                       abort_reason="watchdog"))
    plan = plan_for(root, include_unverified=True)
    hover = next(c for c in plan.cells if c.values["trajectory"] == "hover")
    assert hover.excluded == 1 and hover.needed(True) == 3


def test_issue_bags_never_count(root):
    add_trial(root, "expA", "c1", dict(GOOD, trajectory="hover"), issues=True)
    plan = plan_for(root, include_unverified=True)
    hover = next(c for c in plan.cells if c.values["trajectory"] == "hover")
    assert hover.excluded == 1 and hover.needed(True) == 3


def test_unverified_trials_are_excluded_by_default(root):
    add_trial(root, "expA", "t1", {"trajectory": "hover"})  # violates the contract
    plan = plan_for(root)
    hover = next(c for c in plan.cells if c.values["trajectory"] == "hover")
    assert hover.unverified == 1 and hover.needed() == 3


def test_include_unverified_counts_them(root):
    add_trial(root, "expA", "t1", {"trajectory": "hover"})
    plan = plan_for(root, include_unverified=True)
    hover = next(c for c in plan.cells if c.values["trajectory"] == "hover")
    assert hover.needed(True) == 2


# --------------------------------------------------------------------------- #
# Matching semantics
# --------------------------------------------------------------------------- #
def test_a_factor_without_a_metadata_key_never_narrows_the_match(root):
    """A factor the trial cannot record (no metadata key) cannot narrow the match."""
    add_trial(root, "expA", "t1", dict(GOOD, trajectory="hover", knob="b"))
    hover = next(c for c in plan_for(root).cells if c.values["trajectory"] == "hover")
    assert hover.usable == 1


def test_an_unnamed_factor_at_another_level_does_not_match(root):
    """A campaign plans every factor it does not name at its default, so a trial
    recorded at another level of it is not the campaign's (2026-10-21: a campaign
    that named no ``world`` claimed the flights of another world)."""
    add_trial(root, "expA", "t1", dict(GOOD, execution_host="laptop", trajectory="hover"))
    assert plan_for(root).covered == 0


def test_an_unnamed_factor_the_trial_never_recorded_counts_as_its_default(root):
    """A trial recorded before a factor existed has no key for it: it ran at the default."""
    contract = root / "common" / "config" / "trial_metadata_core.yaml"
    contract.write_text(contract.read_text(encoding="utf-8").replace(
        "execution_host: {required: true", "execution_host: {required: false"), encoding="utf-8")
    meta = dict(GOOD, trajectory="hover")
    meta.pop("execution_host", None)
    add_trial(root, "expA", "t1", meta)
    hover = next(c for c in plan_for(root).cells if c.values["trajectory"] == "hover")
    assert hover.usable == 1


def test_a_none_alias_keeps_older_trials_apart_when_a_default_changes(root):
    """A factor whose default changed maps the missing key of older trials to the
    old level with a ``None`` alias, so they stop counting for the new default."""
    core = (root / "common" / "config" / "factors.yaml").read_text(encoding="utf-8")
    core = core.replace("""    maps_to: {kind: launch_arg, name: trajectory}
""", """    maps_to: {kind: launch_arg, name: trajectory}
  ceiling:
    levels: ["off", "on"]
    default: "on"
    metadata_key: ceiling
    aliases:
      "off": ["None"]
    maps_to: {kind: launch_arg, name: ceiling}
""")
    (root / "common" / "config" / "factors.yaml").write_text(core, encoding="utf-8")
    add_trial(root, "expA", "old", dict(GOOD, trajectory="hover"))
    add_trial(root, "expA", "new", dict(GOOD, trajectory="hover", ceiling="on"))
    hover = next(c for c in plan_for(root).cells if c.values["trajectory"] == "hover")
    assert hover.usable == 1 and hover.matched_bags == ["new"]


def test_a_fixed_factor_does_narrow_the_match(root):
    """A campaign pinned to orin must not be satisfied by laptop bags."""
    add_trial(root, "expA", "t1", dict(GOOD, execution_host="laptop", trajectory="hover"))
    plan = plan_for(root, ["execution_host=orin"])
    assert plan.covered == 0


def test_aliases_let_historical_values_match(root):
    """Host names were written ad hoc before the factor file existed; without
    aliases every historical bag is permanently unmatchable."""
    add_trial(root, "expA", "t1", dict(GOOD, execution_host="lori-ThinkPad-P15-Gen-2i",
                                       trajectory="hover"))
    plan = plan_for(root, ["execution_host=laptop"])
    assert plan.covered == 1


def test_values_compare_as_strings(root):
    """JSON round-trips lose the 720 vs '720' distinction; a count must not."""
    factor = cs.load_factors(None, root)["trajectory"]
    assert factor.canonical(720) == "720"


def test_trials_from_another_experiment_do_not_count(root):
    add_trial(root, "expB", "t1", dict(GOOD, trajectory="hover"))
    trials = ti.load_trials(root)  # both experiments
    campaign = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    assert cp.build_plan(campaign, trials).covered == 0


def test_a_factor_without_a_metadata_key_is_reported_as_unmatchable(root):
    plan = plan_for(root, ["knob=b"])
    assert "knob" in plan.unmatchable_factors


def test_matched_bags_are_named_for_traceability(root):
    add_trial(root, "expA", "2026-07-25_t1", dict(GOOD, trajectory="hover"))
    hover = next(c for c in plan_for(root).cells if c.values["trajectory"] == "hover")
    assert hover.matched_bags == ["2026-07-25_t1"]


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def test_render_separates_todo_from_covered(root):
    for i in range(3):
        add_trial(root, "expA", f"t{i}", dict(GOOD, trial_id=str(i), trajectory="hover"))
    text = cp.render(plan_for(root))
    assert "STILL MISSING (" in text and "COVERED (" in text
    assert "STILL MISSING 3" in text, "the paper's words: wanted, covered, still missing"


def test_render_warns_that_unverified_were_not_counted(root):
    add_trial(root, "expA", "t1", {"trajectory": "hover"})
    assert "not counted" in cp.render(plan_for(root)).lower()


def test_plan_dict_is_json_serialisable(root):
    add_trial(root, "expA", "t1", dict(GOOD, trajectory="hover"))
    payload = json.loads(json.dumps(plan_for(root).to_dict()))
    assert payload["wanted"] == 6 and payload["cells"]


def test_cli_json_output(root, capsys):
    rc = cp.main([str(root / "expA" / "campaigns" / "c.yaml"),
                  "--experiments-root", str(root), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["campaign"] == "c"


def test_cli_reports_a_bad_campaign_cleanly(root, capsys):
    bad = root / "expA" / "campaigns" / "bad.yaml"
    bad.write_text("experiment: expA\nsweep: {trajectory: [teleport]}\n", encoding="utf-8")
    assert cp.main([str(bad), "--experiments-root", str(root)]) == 2
    assert "ERROR" in capsys.readouterr().err

"""Tests for the cross-experiment trial index and coverage report (#96 Phase 1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import trial_index as ti
def make_trial(root: Path, experiment: str, name: str, metadata: dict,
               issues: bool = False) -> Path:
    bags = "bags_issues" if issues else "bags"
    bag = root / experiment / bags / name
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    # A real bag also has rosbag2's own files; without them the dir is not a trial.
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n", encoding="utf-8")
    (bag / f"{name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")
    return bag


BASE = {"trial_id": "t", "execution_host": "orin", "aborted": False}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(
        "version: 1\nfields:\n"
        "  trial_id:\n    required: true\n    type: str\n"
        "  execution_host:\n    required: true\n    type: str\n    factor: true\n"
        "  aborted:\n    required: true\n    type: bool\n",
        encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def test_loads_from_bags_and_bags_issues(root):
    make_trial(root, "expA", "t1", BASE)
    make_trial(root, "expA", "crash1", dict(BASE, trial_id="c"), issues=True)
    trials = ti.load_trials(root)
    assert len(trials) == 2
    assert sum(t.is_issue_bag for t in trials) == 1


def test_unreadable_metadata_is_skipped_not_fatal(root):
    make_trial(root, "expA", "t1", BASE)
    bad = root / "expA" / "bags" / "broken"
    bad.mkdir(parents=True)
    (bad / "trial_metadata.json").write_text("{oops", encoding="utf-8")
    (bad / "metadata.yaml").write_text("rosbag2_bagfile_information:\n", encoding="utf-8")
    (bad / "bad_0.mcap").write_bytes(b"\x89MCAP0\r\n")
    assert len(ti.load_trials(root)) == 1


def test_slim_copies_are_not_counted_as_trials(root):
    """Regression: the glob was `bags*`, which also matched bags_slim/. Slim bags
    carry a COPY of trial_metadata.json (#94), so every slimmed trial was counted
    twice and coverage was silently doubled — 179 'trials' where there were 108."""
    make_trial(root, "expA", "t1", BASE)
    slim = root / "expA" / "bags_slim" / "t1_slim"
    slim.mkdir(parents=True)
    (slim / "trial_metadata.json").write_text(json.dumps(BASE), encoding="utf-8")

    trials = ti.load_trials(root)
    assert len(trials) == 1, f"slim copy counted as a trial: {[t.bag_dir.name for t in trials]}"
    assert ti.load_trials(root, "expA")[0].bag_dir.name == "t1"


def test_slim_copies_sharing_a_generic_trial_id_are_distinct_trials(root):
    """Regression (2026-10-01): dedupe keyed on trial_id dropped every slim copy
    after the first one carrying the notebook-era generic id "trial_001", so 65
    distinct pre-framework recordings vanished from the index. A slim copy is a
    duplicate only of the full bag it is named after."""
    for name in ("2026-05-28T08-29-14_flight_slim", "2026-05-28T10-25-41_flight_slim"):
        slim = root / "expA" / "bags_slim" / name
        slim.mkdir(parents=True)
        (slim / "trial_metadata.json").write_text(json.dumps(dict(BASE, trial_id="trial_001")), encoding="utf-8")
    make_trial(root, "expA", "2026-06-01T09-00-00_other", dict(BASE, trial_id="trial_001"))
    names = sorted(t.bag_dir.name for t in ti.load_trials(root))
    assert len(names) == 3, names


def test_experiment_filter(root):
    make_trial(root, "expA", "t1", BASE)
    make_trial(root, "expB", "t2", BASE)
    assert {t.experiment for t in ti.load_trials(root, "expB")} == {"expB"}


# --------------------------------------------------------------------------- #
# The three-state model — the thing that makes the grid honest
# --------------------------------------------------------------------------- #
def test_a_clean_trial_is_usable(root):
    make_trial(root, "expA", "t1", BASE)
    assert ti.load_trials(root)[0].status == "usable"


def test_a_contract_violation_is_unverified_not_excluded(root):
    """A trial that predates the contract is 'we cannot prove it', NOT 'it is
    bad'. Collapsing the two made the first report claim 0 usable trials for 37
    perfectly good flights."""
    make_trial(root, "expA", "t1", {"trial_id": "t"})  # no execution_host / aborted
    trial = ti.load_trials(root)[0]
    assert trial.status == "unverified"
    assert trial.problems


def test_an_aborted_trial_is_excluded(root):
    make_trial(root, "expA", "t1", dict(BASE, aborted=True, abort_reason="watchdog"))
    assert ti.load_trials(root)[0].status == "excluded"


def test_an_issue_bag_is_excluded_even_if_clean(root):
    make_trial(root, "expA", "c1", BASE, issues=True)
    assert ti.load_trials(root)[0].status == "excluded"


def test_abort_outranks_a_contract_violation(root):
    """An aborted trial with broken metadata is excluded, not merely unverified."""
    make_trial(root, "expA", "t1", {"aborted": True})
    assert ti.load_trials(root)[0].status == "excluded"


# --------------------------------------------------------------------------- #
# Axes
# --------------------------------------------------------------------------- #
def test_declared_factors_come_first(root):
    make_trial(root, "expA", "t1", dict(BASE, execution_host="orin", mode="a"))
    make_trial(root, "expA", "t2", dict(BASE, execution_host="labpc", mode="b"))
    factors = ti.factors_for("expA", ti.load_trials(root), root)
    assert factors[0] == "execution_host", "the contract's declared axis should lead"
    assert "mode" in factors


def test_constant_fields_are_not_axes(root):
    make_trial(root, "expA", "t1", dict(BASE, mode="a"))
    make_trial(root, "expA", "t2", dict(BASE, mode="a"))
    assert "mode" not in ti.discovered_factors(ti.load_trials(root))


def test_high_cardinality_fields_are_not_axes(root):
    """A field with a distinct value per trial makes a list, not a grid."""
    for i in range(15):
        make_trial(root, "expA", f"t{i}", dict(BASE, stamp=f"2026-07-25T10:{i:02d}"))
    assert "stamp" not in ti.discovered_factors(ti.load_trials(root))


def test_identifier_and_prose_fields_are_never_axes(root):
    make_trial(root, "expA", "t1", dict(BASE, trial_id="a", notes="x", battery_start_v=4.1))
    make_trial(root, "expA", "t2", dict(BASE, trial_id="b", notes="y", battery_start_v=3.9))
    discovered = ti.discovered_factors(ti.load_trials(root))
    for excluded in ("trial_id", "notes", "battery_start_v"):
        assert excluded not in discovered


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
def test_cells_split_the_three_states(root):
    make_trial(root, "expA", "t1", dict(BASE, mode="a"))
    make_trial(root, "expA", "t2", dict(BASE, mode="a"))
    make_trial(root, "expA", "t3", dict(BASE, mode="a", aborted=True, abort_reason="r"))
    make_trial(root, "expA", "t4", {"mode": "a"})  # violates the contract
    cell = ti.coverage(ti.load_trials(root), ["mode"])[0]
    assert (cell.usable, cell.excluded, cell.unverified, cell.total) == (2, 1, 1, 4)


def test_missing_combinations_are_reported(root):
    make_trial(root, "expA", "t1", dict(BASE, backend="isaac", src="mocap"))
    make_trial(root, "expA", "t2", dict(BASE, backend="ros", src="mocap"))
    make_trial(root, "expA", "t3", dict(BASE, backend="ros", src="apriltag"))
    cells = ti.coverage(ti.load_trials(root), ["backend", "src"])
    missing = ti.missing_combinations(cells, ["backend", "src"])
    assert missing == [("isaac", "apriltag")]


def test_no_missing_when_the_grid_is_full(root):
    make_trial(root, "expA", "t1", dict(BASE, backend="a"))
    make_trial(root, "expA", "t2", dict(BASE, backend="b"))
    cells = ti.coverage(ti.load_trials(root), ["backend"])
    assert ti.missing_combinations(cells, ["backend"]) == []


def test_absent_factor_renders_as_a_dash(root):
    make_trial(root, "expA", "t1", BASE)
    assert ti.coverage(ti.load_trials(root), ["nope"])[0].values == ("—",)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def test_report_shows_all_three_buckets_and_the_gap(root):
    make_trial(root, "expA", "t1", dict(BASE, backend="isaac", src="mocap"))
    make_trial(root, "expA", "t2", dict(BASE, backend="ros", src="apriltag"))
    report = ti.render_report(ti.load_trials(root), root, ["backend", "src"])
    assert "usable" in report and "unverified" in report and "excluded" in report
    assert "have no bag at all" in report, "an empty combination must be called out"


def test_report_flags_contract_violations(root):
    make_trial(root, "expA", "t1", {"backend": "x"})
    report = ti.render_report(ti.load_trials(root), root, ["backend"])
    assert "violate the metadata contract" in report


def test_cli_summary_runs(root, capsys):
    make_trial(root, "expA", "t1", dict(BASE, mode="a"))
    make_trial(root, "expA", "t2", dict(BASE, mode="b"))
    assert ti.main(["--experiments-root", str(root), "--summary"]) == 0
    assert "expA" in capsys.readouterr().out


def test_cli_writes_a_report_file(root, tmp_path):
    make_trial(root, "expA", "t1", dict(BASE, mode="a"))
    out = tmp_path / "sub" / "coverage.md"
    assert ti.main(["--experiments-root", str(root), "--report", str(out)]) == 0
    assert out.is_file() and "Trial coverage" in out.read_text()


def test_cli_handles_an_empty_tree(tmp_path, capsys):
    assert ti.main(["--experiments-root", str(tmp_path)]) == 0
    assert "No recorded trials" in capsys.readouterr().out

"""Tests for named analyses as a headless API (#96 Phase 4b)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import analysis_runner as ar
EXPERIMENTS_ROOT = HERE.parents[1]

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

ANALYSES = """
analyses:
  per_bag:
    describe: "one run per bag"
    per: bag
    command: "echo analyse {bag} --output-dir {results}"
  whole_cohort:
    per: cohort
    command: "echo cohort {bags} --output-dir {results}"
"""

GOOD = {"trial_id": "t", "execution_host": "orin", "aborted": False}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "analyses.yaml").write_text(ANALYSES, encoding="utf-8")
    return tmp_path


def add_bag(root: Path, name: str, metadata: dict, issues: bool = False, slim: bool = False):
    folder = "bags_slim" if slim else ("bags_issues" if issues else "bags")
    bag = root / "expA" / folder / name
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    # A real bag also has rosbag2's own files; without them the dir is not a trial.
    (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n", encoding="utf-8")
    (bag / f"{name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")


class Recorder:
    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    def __call__(self, cmd):
        self.calls.append(cmd)
        return 1 if self.fail else 0


# --------------------------------------------------------------------------- #
# Declarations
# --------------------------------------------------------------------------- #
def test_analyses_load(root):
    analyses = ar.load_analyses("expA", root)
    assert set(analyses) == {"per_bag", "whole_cohort"}
    assert analyses["whole_cohort"].per == "cohort"


def test_a_missing_declaration_file_is_a_clear_error(tmp_path):
    with pytest.raises(ar.AnalysisError, match="declares no analyses"):
        ar.load_analyses("nope", tmp_path)


def test_an_invalid_per_value_is_rejected(root):
    (root / "expA" / "config" / "analyses.yaml").write_text(
        'analyses:\n  x: {per: sometimes, command: "echo"}\n', encoding="utf-8")
    with pytest.raises(ar.AnalysisError, match="per must be"):
        ar.load_analyses("expA", root)


def test_an_analysis_without_a_command_is_rejected(root):
    (root / "expA" / "config" / "analyses.yaml").write_text(
        'analyses:\n  x: {per: bag}\n', encoding="utf-8")
    with pytest.raises(ar.AnalysisError, match="command is required"):
        ar.load_analyses("expA", root)


def test_every_shipped_analyses_file_loads():
    for path in EXPERIMENTS_ROOT.glob(f"*/config/{ar.ANALYSES_CONFIG_NAME}"):
        assert ar.load_analyses(path.parents[1].name)


# --------------------------------------------------------------------------- #
# Cohort selection
# --------------------------------------------------------------------------- #
@pytest.mark.ros2
def test_only_usable_bags_are_selected_by_default(root):
    add_bag(root, "good", GOOD)
    add_bag(root, "aborted", dict(GOOD, aborted=True, abort_reason="r"))
    add_bag(root, "broken", {"trial_id": "x"})
    assert [t.bag_dir.name for t in ar.select_bags("expA", root)] == ["good"]


@pytest.mark.ros2
def test_unverified_can_be_included_explicitly(root):
    add_bag(root, "good", GOOD)
    add_bag(root, "broken", {"trial_id": "x"})
    names = [t.bag_dir.name for t in ar.select_bags("expA", root, include_unverified=True)]
    assert names == ["broken", "good"]


@pytest.mark.ros2
def test_slim_copies_are_never_selected(root):
    """A slim bag is a copy of a trial, not another trial — analysing both would
    double-count every slimmed run."""
    add_bag(root, "t1", GOOD)
    add_bag(root, "t1_slim", GOOD, slim=True)
    assert [t.bag_dir.name for t in ar.select_bags("expA", root)] == ["t1"]


@pytest.mark.ros2
def test_a_factor_filter_narrows_the_cohort(root):
    add_bag(root, "orin1", dict(GOOD, execution_host="orin"))
    add_bag(root, "laptop1", dict(GOOD, execution_host="laptop"))
    selected = ar.select_bags("expA", root, where={"execution_host": "orin"})
    assert [t.bag_dir.name for t in selected] == ["orin1"]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
@pytest.mark.ros2
def test_per_bag_analysis_builds_one_command_per_bag(root):
    add_bag(root, "a", GOOD)
    add_bag(root, "b", dict(GOOD, trial_id="b"))
    analyses = ar.load_analyses("expA", root)
    result = ar.run_analysis(analyses["per_bag"], ar.select_bags("expA", root), "expA", root)
    assert len(result.commands) == 2
    assert all(cmd[0] == "echo" for cmd in result.commands)


@pytest.mark.ros2
def test_cohort_analysis_builds_a_single_command(root):
    add_bag(root, "a", GOOD)
    add_bag(root, "b", dict(GOOD, trial_id="b"))
    analyses = ar.load_analyses("expA", root)
    result = ar.run_analysis(analyses["whole_cohort"], ar.select_bags("expA", root), "expA", root)
    assert len(result.commands) == 1


def test_an_empty_cohort_is_an_error_not_a_silent_noop(root):
    """Running an analysis over nothing and reporting success would be the
    quietest possible way to produce a wrong figure."""
    analyses = ar.load_analyses("expA", root)
    with pytest.raises(ar.AnalysisError, match="no bags selected"):
        ar.run_analysis(analyses["per_bag"], [], "expA", root)


@pytest.mark.ros2
def test_dry_run_writes_nothing(root):
    add_bag(root, "a", GOOD)
    analyses = ar.load_analyses("expA", root)
    result = ar.run_analysis(analyses["per_bag"], ar.select_bags("expA", root), "expA", root)
    assert not result.results_dir.exists()


@pytest.mark.ros2
def test_an_unknown_placeholder_is_reported(root):
    (root / "expA" / "config" / "analyses.yaml").write_text(
        'analyses:\n  x: {per: bag, command: "echo {nonsense}"}\n', encoding="utf-8")
    add_bag(root, "a", GOOD)
    analyses = ar.load_analyses("expA", root)
    with pytest.raises(ar.AnalysisError, match="unknown placeholder"):
        ar.run_analysis(analyses["x"], ar.select_bags("expA", root), "expA", root)


# --------------------------------------------------------------------------- #
# Provenance — the reason this exists
# --------------------------------------------------------------------------- #
@pytest.mark.ros2
def test_results_record_which_bags_they_came_from(root):
    add_bag(root, "a", GOOD)
    add_bag(root, "b", dict(GOOD, trial_id="b"))
    analyses = ar.load_analyses("expA", root)
    executor = Recorder()
    result = ar.run_analysis(analyses["per_bag"], ar.select_bags("expA", root), "expA", root,
                             where={"execution_host": "orin"}, dry_run=False, executor=executor)

    metadata = json.loads((result.results_dir / ar.ANALYSIS_METADATA_NAME).read_text())
    assert metadata["analysis"] == "per_bag"
    assert metadata["bags"] == ["a", "b"], "a result must name its inputs"
    assert metadata["selection"]["where"] == {"execution_host": "orin"}
    assert metadata["selection"]["n_bags"] == 2
    assert len(metadata["commands"]) == 2
    assert metadata["schema_version"] == 1


@pytest.mark.ros2
def test_failures_are_counted_and_reported(root):
    add_bag(root, "a", GOOD)
    analyses = ar.load_analyses("expA", root)
    result = ar.run_analysis(analyses["per_bag"], ar.select_bags("expA", root), "expA", root,
                             dry_run=False, executor=Recorder(fail=True))
    assert result.status == "failed" and "1 of 1" in result.detail
    metadata = json.loads((result.results_dir / ar.ANALYSIS_METADATA_NAME).read_text())
    assert metadata["failed_commands"] == 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_lists_analyses(root, capsys):
    assert ar.main(["expA", "--experiments-root", str(root), "--list"]) == 0
    assert "per_bag" in capsys.readouterr().out


def test_cli_rejects_an_unknown_analysis(root, capsys):
    assert ar.main(["expA", "nope", "--experiments-root", str(root)]) == 2
    assert "unknown analysis" in capsys.readouterr().err


@pytest.mark.ros2
def test_cli_dry_runs(root, capsys):
    add_bag(root, "a", GOOD)
    assert ar.main(["expA", "per_bag", "--experiments-root", str(root)]) == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_an_experiment_that_declares_no_analyses_yet_is_not_an_error(root):
    """`new_experiment.sh` scaffolds `analyses: {}` for the user to fill in.
    Treating that as malformed made --list crash on every fresh experiment."""
    (root / "expA" / "config" / "analyses.yaml").write_text("analyses: {}\n", encoding="utf-8")
    assert ar.load_analyses("expA", root) == {}


def test_a_malformed_analyses_value_is_still_rejected(root):
    (root / "expA" / "config" / "analyses.yaml").write_text("analyses: [a, b]\n", encoding="utf-8")
    with pytest.raises(ar.AnalysisError, match="must be a mapping"):
        ar.load_analyses("expA", root)

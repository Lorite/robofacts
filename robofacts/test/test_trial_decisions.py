"""Tests for trial_decisions: `trials exclude`, `trials restore` and `trials amend` (2026-10-22).

The rule they protect: a person's decision about a recorded trial is a file beside
the runner's, with a reason, and ``trial_metadata.json`` is never edited. The index,
the plan and the records must read the decision on top of what was recorded.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from record_dirs import rdir
from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import cli
from robofacts import run_marker
from robofacts import trial_decisions as td
from robofacts import trial_index as ti
from robofacts import trial_records as trec

FACTORS = """
version: 1
factors:
  execution_host:
    levels: [laptop, orin]
    default: orin
    metadata_key: execution_host
    maps_to: {kind: host}
  route:
    levels: [block_loop, building_loop]
    default: block_loop
    metadata_key: route
    maps_to: {kind: operator}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  route: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 1
sweep:
  route: [block_loop, building_loop]
fixed:
  execution_host: laptop
"""

#: Every test acts as if the trial ended an hour ago, so the quiet-time check passes.
LATER = time.time() + 3600


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(FACTORS, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    return tmp_path


def bag(root: Path, name: str, folder: str = "bags", slim: bool = True, **metadata) -> Path:
    meta = {"trial_id": name, "execution_host": "laptop", "aborted": False, "route": "block_loop",
            "non_default_factors": []}
    meta.update(metadata)
    d = root / "expA" / rdir(folder) / name
    d.mkdir(parents=True)
    (d / "trial_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / f"{name}_0.mcap").write_bytes(b"mcap")
    if slim:
        s = root / "expA" / rdir(f"{folder}_slim") / f"{name}_slim"
        s.mkdir(parents=True)
        (s / f"{name}_slim_0.mcap").write_bytes(b"slim")
        (s / "trial_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def recorded(path: Path) -> dict:
    return json.loads((path / "trial_metadata.json").read_text(encoding="utf-8"))


def trials(root: Path):
    return {t.bag_dir.name: t for t in ti.load_trials(root, "expA")}


# --------------------------------------------------------------------------- #
# exclude / restore
# --------------------------------------------------------------------------- #
def test_exclude_moves_the_bag_and_its_slim_copy_and_writes_the_reason_beside_each(root):
    full = bag(root, "t1")
    before = (full / "trial_metadata.json").read_bytes()
    (root / "expA" / "plot.png").write_bytes(b"png")
    record = td.exclude(root, "expA", "t1", reason="Wi-Fi re-association stalled DDS for 40 s",
                        category="infrastructure", by="Alejandro", evidence=["plot.png"], now=LATER)
    issues = root / "expA" / rdir("bags_issues") / "t1"
    issues_slim = root / "expA" / rdir("bags_issues_slim") / "t1_slim"
    assert issues.is_dir() and issues_slim.is_dir()
    assert not full.exists() and not (root / "expA" / rdir("bags_slim") / "t1_slim").exists()
    for d in (issues, issues_slim):
        written = json.loads((d / td.EXCLUSION_FILE).read_text(encoding="utf-8"))
        assert written["category"] == "infrastructure" and written["by"] == "Alejandro"
        assert written["reason"].startswith("Wi-Fi") and written["evidence"] == ["plot.png"]
        assert written["date"][:4].isdigit()
    assert (issues / "trial_metadata.json").read_bytes() == before, "never edited"
    assert record["moved"] == [f"{rdir('bags')}/t1 -> {rdir('bags_issues')}/t1",
                               f"{rdir('bags_slim')}/t1_slim -> {rdir('bags_issues_slim')}/t1_slim"]


def test_an_excluded_trial_shows_its_reason_and_category_in_the_records(root):
    bag(root, "t1")
    td.exclude(root, "expA", "t1", reason="walked the wrong route", category="operator",
               by="Alejandro", now=LATER)
    assert trials(root)["t1"].status == "excluded"
    r = {x.name: x for x in trec.load_records(root, "expA")}["t1"]
    assert r.status == "quarantined" and r.trial_class == "excluded"
    assert r.reason.startswith("operator: walked the wrong route (Alejandro, ")
    assert r.exclusion["category"] == "operator"
    assert r.to_dict(root)["exclusion"]["reason"] == "walked the wrong route"
    assert "why: operator: walked the wrong route" in trec.render([r])


def test_a_hand_quarantine_without_a_reason_says_so(root):
    bag(root, "t_old", folder="bags_issues", slim=False)
    r = trec.load_records(root, "expA")[0]
    assert r.status == "quarantined" and "no reason recorded" in r.reason


def test_exclude_of_a_hand_quarantine_adds_the_reason_in_place(root):
    """The 2026-10-22 conversion: a bag moved by hand gets its exclusion.json, no move."""
    old = bag(root, "t_old", folder="bags_issues")
    (old / "quarantine_reason.md").write_text("DDS stalled", encoding="utf-8")
    record = td.exclude(root, "expA", "t_old", reason="DDS stalled", category="infrastructure",
                        evidence=["quarantine_reason.md"], now=LATER)
    assert record["moved"] == []
    assert record["evidence"] == [f"{rdir('bags_issues')}/t_old/quarantine_reason.md"]
    assert (old / td.EXCLUSION_FILE).is_file()
    assert (root / "expA" / rdir("bags_issues_slim") / "t_old_slim" / td.EXCLUSION_FILE).is_file()


def test_exclude_refuses_without_a_reason_a_bad_category_or_twice(root):
    bag(root, "t1")
    with pytest.raises(td.DecisionError, match="reason"):
        td.exclude(root, "expA", "t1", reason="  ", category="operator", now=LATER)
    with pytest.raises(td.DecisionError, match="category"):
        td.exclude(root, "expA", "t1", reason="x", category="weather", now=LATER)
    td.exclude(root, "expA", "t1", reason="x", category="other", now=LATER)
    with pytest.raises(td.DecisionError, match="already excluded"):
        td.exclude(root, "expA", "t1", reason="y", category="other", now=LATER)


def test_exclude_refuses_a_trial_that_is_still_being_written(root):
    full = bag(root, "t1")
    with pytest.raises(td.DecisionError, match="still be writing"):
        td.exclude(root, "expA", "t1", reason="x", category="other")
    (full / "trial_metadata.json").unlink()
    with pytest.raises(td.DecisionError, match="no trial_metadata.json"):
        td.exclude(root, "expA", "t1", reason="x", category="other", now=LATER)
    assert full.is_dir(), "nothing moved"


def test_exclude_refuses_a_trial_of_the_run_in_progress(root):
    full = bag(root, "t1")
    old = time.time() - 600
    for p in [full, *full.rglob("*")] + [root / "expA" / rdir("bags_slim") / "t1_slim",
                                          *(root / "expA" / rdir("bags_slim")).rglob("*")]:
        os.utime(p, (old, old))
    with run_marker.running(root, experiment="expA", campaign="c", label="t1"):
        with pytest.raises(td.DecisionError, match="in progress"):
            td.exclude(root, "expA", "t1", reason="x", category="other")
    td.exclude(root, "expA", "t1", reason="x", category="other")


@pytest.mark.ros2
def test_restore_moves_it_back_and_keeps_the_old_reason_as_history(root):
    bag(root, "t1")
    td.exclude(root, "expA", "t1", reason="looked broken", category="estimator", now=LATER)
    result = td.restore(root, "expA", "t1", reason="the estimator was fine", by="Alejandro")
    full = root / "expA" / rdir("bags") / "t1"
    assert full.is_dir() and (root / "expA" / rdir("bags_slim") / "t1_slim").is_dir()
    assert not (full / td.EXCLUSION_FILE).exists()
    history = json.loads((full / result["history"]).read_text(encoding="utf-8"))
    assert history["reason"] == "looked broken"
    assert history["restored"]["reason"] == "the estimator was fine"
    assert trials(root)["t1"].status == "usable"
    with pytest.raises(td.DecisionError, match="not excluded"):
        td.restore(root, "expA", "t1")


def test_an_unknown_trial_names_where_it_looked(root):
    with pytest.raises(td.DecisionError, match=rdir("bags_issues_slim")):
        td.exclude(root, "expA", "nope", reason="x", category="other")
    with pytest.raises(td.DecisionError, match="not a trial name"):
        td.exclude(root, "expA", "../expA", reason="x", category="other")


# --------------------------------------------------------------------------- #
# amend
# --------------------------------------------------------------------------- #
def test_amend_writes_corrections_beside_both_and_never_edits_the_metadata(root):
    full = bag(root, "t1")
    before = (full / "trial_metadata.json").read_bytes()
    new = td.amend(root, "expA", "t1", ["route=building_loop"],
                   reason="operator levels are unchecked at T-0, the video shows the building loop",
                   by="Alejandro", now=LATER)
    assert new[0]["field"] == "route" and new[0]["recorded"] == "block_loop"
    assert new[0]["corrected"] == "building_loop" and new[0]["by"] == "Alejandro"
    assert (full / "trial_metadata.json").read_bytes() == before
    for d in (full, root / "expA" / rdir("bags_slim") / "t1_slim"):
        assert [e["corrected"] for e in td.read_corrections(d)] == ["building_loop"]


@pytest.mark.ros2
def test_the_index_the_plan_and_the_records_read_the_corrected_value(root):
    bag(root, "t1")
    td.amend(root, "expA", "t1", ["route=building_loop"], reason="video", now=LATER)
    t = trials(root)["t1"]
    assert t.metadata["route"] == "building_loop" and t.recorded["route"] == "block_loop"
    assert t.metadata["non_default_factors"] == ["route"] and t.recorded["non_default_factors"] == []
    assert t.status == "usable" and len(t.corrections) == 1

    campaign = cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)
    plan = cp.build_plan(campaign, ti.load_trials(root, "expA"))
    cells = {c.values["route"]: c for c in plan.cells}
    assert cells["building_loop"].usable == 1 and cells["block_loop"].usable == 0
    assert cells["building_loop"].corrected_bags == ["t1"]
    assert "corrections.json" in cp.render(plan)

    r = trec.load_records(root, "expA")[0]
    assert r.values["route"] == "building_loop" and r.corrections[0]["recorded"] == "block_loop"
    text = trec.render([r])
    assert "corrected=route" in text and "corrected: route: block_loop -> building_loop (video)" in text
    assert "trial(s) counted with a person's correction" in ti.render_report(
        ti.load_trials(root, "expA"), root)


def test_amend_validates_the_factor_and_the_level(root):
    bag(root, "t1")
    with pytest.raises(td.DecisionError, match="declares no factor"):
        td.amend(root, "expA", "t1", ["weather=rain"], reason="x", now=LATER)
    with pytest.raises(td.DecisionError, match="not a level of route"):
        td.amend(root, "expA", "t1", ["route=loop_3"], reason="x", now=LATER)
    with pytest.raises(td.DecisionError, match="already has route=block_loop"):
        td.amend(root, "expA", "t1", ["route=block_loop"], reason="x", now=LATER)
    with pytest.raises(td.DecisionError, match="reason"):
        td.amend(root, "expA", "t1", ["route=building_loop"], reason="", now=LATER)
    assert not (root / "expA" / rdir("bags") / "t1" / td.CORRECTIONS_FILE).exists()


def test_a_second_amend_extends_the_file_and_the_newest_wins(root):
    bag(root, "t1")
    td.amend(root, "expA", "t1", ["route=building_loop"], reason="first look", now=LATER)
    new = td.amend(root, "expA", "t1", ["route=block_loop"], reason="second look", now=LATER)
    assert new[0]["replaces"] == "building_loop"
    assert [e["reason"] for e in td.read_corrections(root / "expA" / rdir("bags") / "t1")] == \
        ["first look", "second look"]
    assert trials(root)["t1"].metadata["route"] == "block_loop"


def test_an_amended_trial_keeps_its_correction_when_excluded(root):
    bag(root, "t1")
    td.amend(root, "expA", "t1", ["route=building_loop"], reason="video", now=LATER)
    td.exclude(root, "expA", "t1", reason="x", category="other", now=LATER)
    t = trials(root)["t1"]
    assert t.status == "excluded" and t.metadata["route"] == "building_loop"


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #
def test_the_three_commands_are_trials_subcommands(root, capsys, monkeypatch):
    bag(root, "t1")
    monkeypatch.setattr(td, "WRITE_QUIET_S", 0)
    base = ["--experiments-root", str(root), "expA", "t1"]
    assert cli.main(["amend", *base, "route=building_loop", "--reason", "video", "--by", "A"]) == 0
    assert "corrected t1: route: block_loop -> building_loop (video)" in capsys.readouterr().out
    assert cli.main(["exclude", *base, "--reason", "stall", "--category", "infrastructure"]) == 0
    assert "excluded t1: infrastructure: stall" in capsys.readouterr().out
    assert cli.main(["exclude", *base, "--reason", "stall", "--category", "infrastructure"]) == 1
    assert "already excluded" in capsys.readouterr().err
    assert cli.main(["restore", *base]) == 0
    assert "kept the old reason as exclusion_restored_" in capsys.readouterr().out

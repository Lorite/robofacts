import base64
import os
import subprocess
import sys
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

CAMPAIGN = {
    "experiment": "fake",
    "operator_live_steps": [
        {"text": "FIRST check the estimator\nsecond line is dropped", "run": "ros2 topic hz /x"},
        {"text": "prose-only step, no run command"},
        {"text": "hand over", "run": 'ros2 service call /b std_srvs/srv/SetBool "{data: true}"'},
    ],
}


def run_script(path, *extra):
    return subprocess.run(
        [sys.executable, "-m", "robofacts.campaign_live_steps", str(path), *extra],
        capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)},
    ).stdout


def test_skips_prose_steps_and_numbers_sequentially(tmp_path):
    campaign = tmp_path / "c.yaml"
    campaign.write_text(yaml.safe_dump(CAMPAIGN))
    lines = run_script(campaign).splitlines()
    assert len(lines) == 2
    idx, title, run, text_b64 = lines[0].split("\t")
    assert (idx, title, run) == ("1", "FIRST check the estimator", "ros2 topic hz /x")
    assert base64.b64decode(text_b64).decode() == "FIRST check the estimator\nsecond line is dropped"
    idx2, _, run2, _ = lines[1].split("\t")
    assert idx2 == "2"
    assert run2 == 'ros2 service call /b std_srvs/srv/SetBool "{data: true}"'


def test_title_truncation(tmp_path):
    campaign = tmp_path / "c.yaml"
    campaign.write_text(yaml.safe_dump(
        {"operator_live_steps": [{"text": "x" * 200, "run": "true"}]}))
    _, title, _, _ = run_script(campaign, "--max-title", "10").splitlines()[0].split("\t")
    assert title == "x" * 10


def test_no_live_steps_prints_nothing(tmp_path):
    campaign = tmp_path / "c.yaml"
    campaign.write_text(yaml.safe_dump({"experiment": "fake"}))
    assert run_script(campaign) == ""


def test_annotate_keeps_the_original_campaign_indices():
    """annotate() is the shared extractor behind the cockpit panes AND the web
    UI's panels. The UI executes a step by its position in the campaign's FULL
    list (that position is the /operator_step allowlist key), so the rows must
    carry the original index even though prose-only steps are filtered out."""
    from robofacts import campaign_live_steps as cls_mod
    rows = cls_mod.annotate(CAMPAIGN["operator_live_steps"])
    assert [r["index"] for r in rows] == [0, 2], "prose step 1 skipped, positions kept"
    assert rows[0]["title"] == "FIRST check the estimator"
    assert rows[0]["cmd"] == "ros2 topic hz /x"
    assert rows[0]["text"].endswith("second line is dropped")


def test_live_steps_is_built_on_annotate(tmp_path):
    """The TSV the tmux window consumes and annotate() must be the same rows,
    or the cockpit and the web UI drift apart on what a step pre-types."""
    from robofacts import campaign_live_steps as cls_mod
    campaign = tmp_path / "c.yaml"
    campaign.write_text(yaml.safe_dump(CAMPAIGN))
    tsv = cls_mod.live_steps(campaign)
    rows = cls_mod.annotate(CAMPAIGN["operator_live_steps"])
    assert [(t[1], t[2]) for t in tsv] == [(r["title"], r["cmd"]) for r in rows]

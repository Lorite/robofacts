"""`trials run --execute` names a trial with contract problems as unverified (2026-11-25).

A trial whose metadata has contract problems (``_schema_problems``, for example a required
field that nothing writes) is recorded, but the index calls it unverified: it never counts,
and the next run records it again. The closing tally said "1 OK" for it. It now says
"recorded but unverified (contract problems)", with the first problem on the trial's line.
The exit code and what counts do not change.
"""
from __future__ import annotations

import pytest

from robofacts import trial_index as ti
from robofacts import trial_runner as tr
from test_process_backend import use_project, write_project

REQUIRED_BUT_UNWRITTEN = (
    "  operator_note: {required: true, type: str, describe: \"Written by nobody.\"}\n")


def test_the_tally_counts_unverified_trials_apart_from_the_ok_ones():
    counts = tr.tally(["ran", "ran", "blocked"], unverified=1)
    assert counts["line"] == ("3 trial(s): 1 OK, 1 recorded but unverified (contract problems), "
                              "1 blocked, 0 failed")
    assert (counts["ok"], counts["unverified"], counts["bad"]) == (1, 1, True)
    # Without unverified trials the line is the one the CI jobs and the web UI already read.
    assert tr.tally(["ran"])["line"] == "1 trial(s): 1 OK, 0 blocked, 0 failed"
    assert tr.tally(["ran"], unverified=1)["bad"] is False


@pytest.mark.real_sleep
def test_a_trial_with_contract_problems_is_not_called_ok(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    experiments = write_project(root)
    schema = experiments / "plant" / "config" / "trial_metadata_schema.yaml"
    schema.write_text(schema.read_text(encoding="utf-8") + REQUIRED_BUT_UNWRITTEN, encoding="utf-8")
    use_project(root, monkeypatch)
    rc = tr.main([str(experiments / "plant" / "campaigns" / "rates.yaml"),
                  "--experiments-root", str(experiments), "--no-pause", "--execute", "--limit", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "1 trial(s): 0 OK, 1 recorded but unverified (contract problems), 0 blocked, 0 failed" in out
    assert "[ran, unverified]" in out
    assert "the first: operator_note: required but missing" in out
    ti._recorder_at.cache_clear()
    (trial,) = ti.load_trials(experiments, "plant")
    assert trial.status == "unverified"
    ti._recorder_at.cache_clear()

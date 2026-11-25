"""`trials records` marks its times as UTC (2026-11-25).

The ledger's ``ts_start`` and the stamp in a trial's name are UTC, but the listing cut the
time to its first 19 characters, which dropped the zone: a UTC time read as local time, in
the table and in ``--json``. The table now says ``UTC`` and ``--json`` ends the ISO time with
``Z``.
"""
from __future__ import annotations

import json

from robofacts import trial_records as trc


def test_the_table_and_the_json_name_the_zone(tmp_path, capsys):
    exp = tmp_path / "experiments" / "demo"
    (exp / "runs").mkdir(parents=True)
    rows = [{"trial_label": "20261125T195800_rep01", "campaign": "c", "status": "blocked",
             "detail": "NO-GO", "ts_start": "2026-11-25T19:58:00.123+00:00"},
            {"trial_label": "20261125T200000_rep02", "campaign": "c", "status": "blocked",
             "detail": "NO-GO", "ts_start": "2026-11-25T21:00:00+01:00"},
            {"trial_label": "20261125T201500_rep03", "campaign": "c", "status": "failed",
             "detail": "no ts_start: the time comes from the name"}]
    (exp / "runs" / "attempts.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    root = ["--experiments-root", str(tmp_path / "experiments")]

    assert trc.main(["demo", *root]) == 0
    table = capsys.readouterr().out
    assert "2026-11-25 19:58:00 UTC" in table
    assert "2026-11-25 20:00:00 UTC" in table          # +01:00 converted to UTC
    assert "2026-11-25 20:15:00 UTC" in table          # from the trial's name

    assert trc.main(["demo", *root, "--json"]) == 0
    started = sorted(r["started"] for r in json.loads(capsys.readouterr().out))
    assert started == ["2026-11-25T19:58:00Z", "2026-11-25T20:00:00Z", "2026-11-25T20:15:00Z"]

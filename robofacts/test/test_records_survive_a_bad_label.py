"""`trials records` reports an entry whose name the file system cannot look up (2026-11-25).

A trial label longer than the file system's limit for a name (an attempt whose recorder
failed with "File name too long") stays in the ledger. Looking up its run log raised
OSError, and from then on `trials records <exp>` stopped with a traceback for the whole
experiment. The entry is now listed with its reason, and the other records with it.
"""
from __future__ import annotations

import json
from pathlib import Path

from robofacts import trial_records as trc


def _quiet(method):
    """``method`` as Python 3.14 has it: an OSError of the lookup answers False."""
    def answer(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except OSError:
            return False
    return answer


def test_a_ledger_label_too_long_for_the_file_system_is_listed_not_fatal(tmp_path, capsys):
    exp = tmp_path / "experiments" / "demo"
    (exp / "runs").mkdir(parents=True)
    long_label = "20261125T195710_site-" + "東京駅前の実験室" * 21 + "_rep01_h88fbfb61"
    assert len(long_label.encode("utf-8")) > 255
    rows = [{"trial_label": long_label, "campaign": "site", "status": "failed",
             "detail": "no recording recorded", "ts_start": "2026-11-25T19:57:10"},
            {"trial_label": "20261125T195800_rep01", "campaign": "site", "status": "blocked",
             "detail": "NO-GO", "ts_start": "2026-11-25T19:58:00"}]
    (exp / "runs" / "attempts.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert trc.main(["demo", "--experiments-root", str(tmp_path / "experiments")]) == 0
    out = capsys.readouterr().out
    assert long_label in out and "20261125T195800_rep01" in out
    assert "2 record(s)" in out
    (bad,) = [r for r in trc.load_records(tmp_path / "experiments", "demo") if r.name == long_label]
    assert bad.run_log is None and "run log cannot be looked up" in bad.reason


def test_the_reason_does_not_depend_on_the_python_version(tmp_path, capsys, monkeypatch):
    """On Python 3.14 ``Path.is_file`` and ``Path.is_dir`` return False for a name that is too
    long, where 3.10 to 3.13 raise OSError, and the reason was lost (CI job core-no-ros
    (3.14), 2026-11-25). The same answer on every version, here with the 3.14 behavior."""
    monkeypatch.setattr(Path, "is_file", _quiet(Path.is_file))
    monkeypatch.setattr(Path, "is_dir", _quiet(Path.is_dir))
    test_a_ledger_label_too_long_for_the_file_system_is_listed_not_fatal(tmp_path, capsys)

"""No override passes a failed [HARD] item or a factor check that did not pass (2026-11-26).

The checklist template says that a text starting with [HARD] cannot be overridden, and the
paper says that a trial that failed a check never counts. Until 2026-11-26 the operator's
override (`--gate-override`) passed both, and the trial counted. A factor check confirms the
trial's declared level (the room's lighting, for example), so an overridden one left a
counted trial whose level nobody had confirmed. The shadow mode of a `gate` factor, which
only a simulated experiment may use, still records every verdict and never blocks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from robofacts import preflight_gate as pg


def _checklist(tmp_path: Path, text: str, present: bool) -> Path:
    target = tmp_path / ("present" if present else "missing")
    if present:
        target.write_text("x", encoding="utf-8")
    path = tmp_path / "checklist.json"
    path.write_text(json.dumps({"checklist": [
        {"text": text, "check": "file_exists", "args": {"path": str(target)}}]}),
        encoding="utf-8")
    return path


def _factor_entry(tmp_path: Path, passes: bool) -> dict:
    target = tmp_path / ("lights_ok" if passes else "lights_wrong")
    if passes:
        target.write_text("x", encoding="utf-8")
    return {"text": "The room's lighting matches the declared level.", "check": "file_exists",
            "args": {"path": str(target)}, "factor": "lighting", "level": "dimmed"}


def _override(gate: pg.PreflightGate) -> pg.PreflightGate:
    gate.override = True
    gate.override_reason = "I am sure"
    return gate


def test_an_override_does_not_pass_a_failed_hard_item(tmp_path, capsys):
    gate = _override(pg.PreflightGate(checklist=_checklist(tmp_path, "[HARD] The file exists.", False)))
    with pytest.raises(pg.PreflightBlocked) as info:
        gate.check("t1")
    assert "The override does not pass [HARD] file_exists" in str(info.value)
    assert "override NOT applied" in capsys.readouterr().out
    assert gate.last_result.allowed is False and gate.last_result.overridden is False


def test_an_override_does_not_pass_a_failed_factor_check(tmp_path):
    """The failed item is soft: only the factor check's failure blocks the override."""
    gate = _override(pg.PreflightGate(checklist=_checklist(tmp_path, "The file exists.", True)))
    gate = gate.with_extra([_factor_entry(tmp_path, passes=False)])
    with pytest.raises(pg.PreflightBlocked) as info:
        gate.check("t1")
    message = str(info.value)
    assert "The override does not pass factor check lighting=dimmed (FAIL)" in message
    assert gate.last_result.allowed is False
    assert [f["result"] for f in gate.last_result.factor_checks] == ["FAIL"]


def test_without_the_override_a_failed_factor_check_does_not_suggest_it(tmp_path):
    gate = pg.PreflightGate(checklist=_checklist(tmp_path, "The file exists.", True))
    gate = gate.with_extra([_factor_entry(tmp_path, passes=False)])
    with pytest.raises(pg.PreflightBlocked) as info:
        gate.check("t1")
    message = str(info.value)
    assert message.endswith(". factor check lighting=dimmed (FAIL) failed: fix the cause, "
                            "then run again."), message
    assert "override flag" not in message


def test_an_override_still_passes_a_failed_soft_item(tmp_path, capsys):
    """Unchanged: a soft item may be overridden, with the reason stored, also when the trial's
    factor check passed."""
    gate = _override(pg.PreflightGate(checklist=_checklist(tmp_path, "The file exists.", False)))
    gate = gate.with_extra([_factor_entry(tmp_path, passes=True)])
    result = gate.check("t1")
    assert result.allowed is True and result.overridden is True
    assert result.override_reason == "I am sure"
    assert "PROCEEDING ANYWAY BY OPERATOR OVERRIDE" in capsys.readouterr().out


@pytest.mark.parametrize("hard_fails, factor_fails", [(True, False), (False, True)])
def test_the_shadow_mode_still_records_and_never_blocks(tmp_path, hard_fails, factor_fails):
    """The shadow gate of a simulated experiment's `gate` factor is a measurement: it runs every
    check, records the NO-GO and lets the trial run, a failed [HARD] item or factor check
    included (the fault study's gate=off arm)."""
    gate = pg.PreflightGate(checklist=_checklist(tmp_path, "[HARD] The file exists.", not hard_fails))
    gate = gate.with_extra([_factor_entry(tmp_path, passes=not factor_fails)]).with_mode("shadow")
    result = gate.check("t1")
    assert result.status == "fail" and result.allowed is True and result.overridden is True
    assert result.mode == "shadow" and result.override_reason == "shadow"

"""Concurrent T-0 gate machine checks (2026-10-13, SAC 2027 framework paper).

The gate's independent machine checks may run together. These tests hold the
promise that makes that safe: the verdict, the printed report, the gate log
line and the trial metadata's ``preflight`` block are the same in both modes,
with the checks reported in checklist order, including a failing check, a
check that times out, a skipped check and checks that must run alone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from robofacts import adapters

import robofacts.preflight  # noqa: F401  (the parent package of the module under test)
from robofacts import preflight_gate as pg

HERE = Path(__file__).resolve().parent
PREFLIGHT = HERE.parents[1] / "robofacts" / "robofacts" / "preflight"


def _load(name: str, path: Path):
    """A private copy of the module, loaded INSIDE its package so its relative imports work."""
    spec = importlib.util.spec_from_file_location(f"robofacts.preflight.{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rp = _load("run_preflight_concurrency_under_test", PREFLIGHT / "run_preflight.py")

#: name -> body of a fixture check script. Each sleeps so that overlap is visible.
FIXTURE_CHECKS = {
    "slow_pass": "sleep 0.5; echo 'PASS: slow probe ok'; exit 0",
    "slow_fail": "sleep 0.5; echo 'FAIL: slow probe saw nothing'; exit 1",
    "slow_skip": "sleep 0.5; echo 'SKIP: not used here'; exit 3",
    "hang": "exec sleep 30",
    "alone_pass": "sleep 0.5; echo 'PASS: ran alone'; exit 0",
    "unclassified": "echo 'PASS: an unknown check'; exit 0",
}

FIXTURE_CHECKLIST = {
    "checklist": [
        "Operator: the area is clear.",
        {"text": "[HARD] first probe", "check": "slow_pass", "parallel": True},
        {"text": "[HARD] a failing probe", "check": "slow_fail", "parallel": True},
        {"text": "a probe that hangs", "check": "hang", "parallel": True},
        {"text": "a skipped probe", "check": "slow_skip", "parallel": True},
        {"text": "[HARD] opted out, runs alone", "check": "alone_pass", "parallel": False},
        {"text": "[HARD] no field, unknown check, runs alone", "check": "unclassified"},
        {"text": "[HARD] second probe", "check": "slow_pass", "parallel": True},
        "Operator: e-stop in hand.",
    ]
}


@pytest.fixture
def fixture_runner(tmp_path: Path) -> Path:
    """A copy of run_preflight.py whose checks/ holds the fixture scripts."""
    root = tmp_path / "preflight"
    (root / "checks").mkdir(parents=True)
    source = (PREFLIGHT / "run_preflight.py").read_text(encoding="utf-8")
    # The gate does not pass --timeout, so the copy's default is lowered to 1 s
    # to make the hanging fixture time out on the gate path too.
    marker = 'parser.add_argument("--timeout", type=float, default=60.0'
    assert marker in source
    source = source.replace(marker, 'parser.add_argument("--timeout", type=float, default=1.0')
    # Never bounce the real ros2 daemon of the (shared) dev container from a test.
    bounce = "def prepare_gate() -> None:\n"
    assert bounce in source
    source = source.replace(bounce, bounce + "    return  # fixture copy\n")
    (root / "run_preflight.py").write_text(source, encoding="utf-8")
    for name, body in FIXTURE_CHECKS.items():
        script = root / "checks" / f"{name}.sh"
        script.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
    return root / "run_preflight.py"


@pytest.fixture
def checklist(tmp_path: Path) -> Path:
    path = tmp_path / "preflight_checklist.json"
    path.write_text(json.dumps(FIXTURE_CHECKLIST, indent=2), encoding="utf-8")
    return path


def _run_main(module, checklist: Path, mode: str, capsys) -> tuple[int, str, float]:
    t0 = time.monotonic()
    rc = module.main(["--checklist", str(checklist), "--no-daemon-bounce",
                      "--timeout", "1", "--concurrency", mode])
    return rc, capsys.readouterr().out, time.monotonic() - t0


def test_both_modes_print_the_same_report_in_checklist_order(fixture_runner, checklist, capsys,
                                                             monkeypatch):
    monkeypatch.delenv(rp.CONCURRENCY_ENV, raising=False)
    module = _load("run_preflight_fixture", fixture_runner)
    rc_seq, out_seq, t_seq = _run_main(module, checklist, "sequential", capsys)
    rc_con, out_con, t_con = _run_main(module, checklist, "concurrent", capsys)
    assert rc_seq == rc_con == module.RC_FAIL
    assert out_seq == out_con, "the report must not depend on the mode"
    lines = [l.split(":")[0].split() for l in out_con.splitlines() if l.startswith("  ")
             and l.split()[0] in ("PASS", "FAIL", "SKIP", "ERR")]
    assert lines == [["PASS", "slow_pass"], ["FAIL", "slow_fail"], ["FAIL", "hang"],
                     ["SKIP", "slow_skip"], ["PASS", "alone_pass"], ["PASS", "unclassified"],
                     ["PASS", "slow_pass"]]
    assert "check timed out after 1s (treated as FAIL)" in out_con
    # Operator confirmations still come after every machine check.
    assert out_con.index("OPERATOR CONFIRMATIONS") > out_con.index("PASS  unclassified")
    assert "2 CHECK(S) FAILED (4 passed, 1 skipped) - NO-GO" in out_con
    # Sequential: 0.5+0.5+1+0.5+0.5+0+0.5 = 3.5 s. Concurrent: max(0.5,0.5,1,0.5)+0.5+0+0.5 = 2 s.
    assert t_seq > 3.2 and t_con < 3.0, (t_seq, t_con)


def test_the_gate_result_and_log_line_do_not_depend_on_the_mode(fixture_runner, checklist,
                                                                tmp_path, monkeypatch):
    monkeypatch.delenv(rp.CONCURRENCY_ENV, raising=False)
    results = {}
    for mode in ("sequential", "concurrent"):
        log = tmp_path / f"gate_{mode}.jsonl"
        gate = pg.PreflightGate(checklist=checklist, runner=fixture_runner, log_path=log,
                                experiment="fixture", concurrency=mode, timeout_s=60)
        with pytest.raises(pg.PreflightBlocked) as blocked:
            gate.check("trial_1")
        meta = gate.last_result.as_metadata()
        line = pg.read_gate_log(log)[-1]
        for volatile in ("checked_at", "duration_s"):
            meta.pop(volatile)
        for volatile in ("ts", "duration_s"):
            line.pop(volatile)
        results[mode] = (str(blocked.value), meta, line)
    assert results["sequential"] == results["concurrent"]
    _, meta, line = results["concurrent"]
    assert meta["verdict"] == "NO-GO" and meta["n_checks"] == 7
    assert [f["check"] for f in line["failed"]] == ["slow_fail", "hang"]
    assert [f["hard"] for f in line["failed"]] == [True, False]
    assert line["skipped"] == ["slow_skip"]


def test_a_failed_soft_check_still_blocks_in_both_modes(fixture_runner, tmp_path, monkeypatch):
    """[HARD] and binding semantics are unchanged: a soft FAIL is a NO-GO too."""
    monkeypatch.delenv(rp.CONCURRENCY_ENV, raising=False)
    path = tmp_path / "soft.json"
    path.write_text(json.dumps({"checklist": [
        {"text": "soft probe", "check": "slow_fail", "parallel": True},
        {"text": "[HARD] hard probe", "check": "slow_pass", "parallel": True}]}),
        encoding="utf-8")
    for mode in ("sequential", "concurrent"):
        gate = pg.PreflightGate(checklist=path, runner=fixture_runner, concurrency=mode)
        with pytest.raises(pg.PreflightBlocked):
            gate.check()
        shadow = gate.with_mode("shadow", "shadow: test")
        result = shadow.check()
        assert result.allowed and result.overridden and result.verdict == "NO-GO"


def test_batches_put_every_opted_out_entry_alone_at_its_place(checklist):
    entries = rp.load_entries(checklist)
    machine = [e for e in entries if e["check"]]
    assert rp.plan_batches(machine, "concurrent") == [[0, 1, 2, 3], [4], [5], [6]]
    assert rp.plan_batches(machine, "sequential") == [[i] for i in range(7)]


def test_timing_log_records_overlap_without_changing_the_report(fixture_runner, checklist,
                                                                tmp_path, capsys, monkeypatch):
    log = tmp_path / "timing.jsonl"
    monkeypatch.setenv(rp.TIMING_LOG_ENV, str(log))
    module = _load("run_preflight_fixture_timing", fixture_runner)
    _run_main(module, checklist, "concurrent", capsys)
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["concurrency"] == "concurrent" and len(record["checks"]) == 7
    first_batch = [c for c in record["checks"] if c["batch"] == 0]
    assert max(c["start_s"] for c in first_batch) < 0.3, "the first batch starts together"
    alone = next(c for c in record["checks"] if c["check"] == "alone_pass")
    assert alone["start_s"] >= max(c["end_s"] for c in first_batch) - 1e-3


# --------------------------------------------------------------------------- #
# The per-check decision and the setting
# --------------------------------------------------------------------------- #
def test_every_check_script_is_classified_exactly_once():
    scripts = {p.stem for p in (PREFLIGHT / "checks").glob("*.sh")}
    safe, alone = set(rp.PARALLEL_SAFE_CHECKS), set(rp.SEQUENTIAL_CHECKS)
    assert not safe & alone
    assert scripts == safe | alone, (
        f"unclassified: {sorted(scripts - safe - alone)}, unknown: {sorted((safe | alone) - scripts)}. "
        f"Read the new check's code and add it to PARALLEL_SAFE_CHECKS or SEQUENTIAL_CHECKS")


@pytest.mark.parametrize("adapter", adapters.AVAILABLE)
def test_every_adapter_check_is_classified_exactly_once_by_its_adapter(adapter):
    module = adapters.module(adapter)
    scripts = {p.stem for p in (adapters.ADAPTERS_DIR / adapter / "checks").glob("*.sh")}
    safe, alone = set(module.PARALLEL_SAFE_CHECKS), set(module.SEQUENTIAL_CHECKS)
    assert not safe & alone
    assert scripts == safe | alone, (
        f"{adapter}: unclassified {sorted(scripts - safe - alone)}, unknown {sorted((safe | alone) - scripts)}")
    core = {p.stem for p in (PREFLIGHT / "checks").glob("*.sh")}
    assert not scripts & core, f"{adapter} shadows core checks {sorted(scripts & core)}"


@pytest.mark.parametrize("check", sorted(rp.SEQUENTIAL_CHECKS))
def test_a_check_that_must_run_alone_cannot_be_declared_parallel(check):
    entries = [{"text": "x", "check": check, "args": {}, "hard": True, "parallel": True}]
    problems = rp.validate(entries, {})
    assert any("cannot be declared parallel" in p for p in problems)


def test_parallel_must_be_a_boolean(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"checklist": [{"text": "x", "check": "disk_space",
                                               "parallel": "no"}]}), encoding="utf-8")
    with pytest.raises(rp.ChecklistError, match="non-boolean 'parallel'"):
        rp.load_entries(path)


def test_concurrency_precedence(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"concurrency": "sequential", "checklist": ["x"]}),
                    encoding="utf-8")
    monkeypatch.delenv(rp.CONCURRENCY_ENV, raising=False)
    assert rp.resolve_concurrency(None, path) == "sequential"
    monkeypatch.setenv(rp.CONCURRENCY_ENV, "concurrent")
    assert rp.resolve_concurrency(None, path) == "concurrent"
    assert rp.resolve_concurrency("sequential", path) == "sequential"
    monkeypatch.delenv(rp.CONCURRENCY_ENV)
    path.write_text(json.dumps({"checklist": ["x"]}), encoding="utf-8")
    assert rp.resolve_concurrency(None, path) == "concurrent" == rp.DEFAULT_CONCURRENCY
    monkeypatch.setenv(rp.CONCURRENCY_ENV, "fast")
    with pytest.raises(rp.ChecklistError, match="unknown concurrency"):
        rp.resolve_concurrency(None, path)


def test_an_unknown_mode_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rp.CONCURRENCY_ENV, "fast")
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"checklist": ["x"]}), encoding="utf-8")
    assert rp.main(["--checklist", str(path), "--no-daemon-bounce"]) == rp.RC_USAGE


def test_the_gate_passes_its_mode_to_the_runner(monkeypatch, tmp_path):
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    pg.PreflightGate(checklist=tmp_path / "c.json", concurrency="sequential")._run()
    assert seen["cmd"][-2:] == ["--concurrency", "sequential"]
    pg.PreflightGate(checklist=tmp_path / "c.json")._run()
    assert "--concurrency" not in seen["cmd"]



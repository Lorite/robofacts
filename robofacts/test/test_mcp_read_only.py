"""The MCP server only reads and plans. These tests hold that boundary.

An agent may call every tool of ``trials mcp`` on its own, so no tool may run a trial with
``--execute``, run a gate check against hardware, write the stop latch, or change a file.
Execution stays with the operator at the command line (the safety rule of the lab where
the framework was built: real hardware runs only on the operator's explicit go).

The tests call the functions behind the tools (``robofacts.mcp.queries``), which need no
MCP SDK, so they also run where the SDK is not installed. ``test_mcp_server.py`` checks the
server around them. The project is a copy of the example, with two trials recorded for real
on the process backend (a local script that writes a log, no hardware), one of them then
excluded by a person, and a third that breaks the metadata contract.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from robofacts import trial_decisions as td
from robofacts import trial_index as ti
from robofacts import trial_runner as tr
from robofacts.mcp import queries as q

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "example_project"
MCP_SOURCES = sorted((PACKAGE / "robofacts" / "mcp").glob("*.py"))
EXPERIMENT = "demo_log_recorder"
CAMPAIGN = "grid"


def _env(root: Path, latch: Path) -> dict:
    return {
        "TRIALS_PROJECT_ROOT": str(root),
        "TRIALS_EXPERIMENTS_ROOT": str(root / "experiments"),
        # The recorder and the gate start `python3 -m robofacts...`: they must find THIS package.
        "PYTHONPATH": str(PACKAGE) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "TRIALS_VOICE": "off",
        "TRIALS_STOP_LATCH": str(latch),
    }


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """A copy of the example with a usable, an excluded and an unverified trial."""
    root = tmp_path_factory.mktemp("mcp") / "example_project"
    shutil.copytree(EXAMPLE, root)                      # the links become files
    latch = root.parent / "stop_latch"
    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(root)
        for key, value in _env(root, latch).items():
            mp.setenv(key, value)
        mp.delenv("TRIALS_BACKEND", raising=False)
        ti._recorder_at.cache_clear()
        campaign = root / "experiments" / EXPERIMENT / "campaigns" / f"{CAMPAIGN}.yaml"
        rc = tr.main([str(campaign), "--experiments-root", str(root / "experiments"),
                      "--execute", "--no-pause", "--limit", "2"])
        assert rc == 0, "the example's trials did not run"
        recordings = root / "experiments" / EXPERIMENT / "recordings"
        names = sorted(d.name for d in recordings.iterdir() if d.is_dir())
        assert len(names) == 2
        usable, excluded = names
        mp.setattr(td, "WRITE_QUIET_S", 0)      # the trials just ended, and nothing writes them now
        assert td.main_exclude([EXPERIMENT, excluded, "--experiments-root", str(root / "experiments"),
                                "--reason", "the log rotated during the trial", "--category",
                                "infrastructure", "--by", "test"]) == 0
        # A third trial whose metadata breaks the contract: a required field is null.
        broken = recordings / f"{usable}_contract"
        shutil.copytree(recordings / usable, broken)
        meta = json.loads((broken / "trial_metadata.json").read_text(encoding="utf-8"))
        meta["trial_id"] = None
        (broken / "trial_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        ti._recorder_at.cache_clear()
    return {"root": root, "latch": latch, "usable": usable, "excluded": excluded,
            "broken": broken.name}


@pytest.fixture
def project(recorded, monkeypatch):
    root = recorded["root"]
    monkeypatch.chdir(root)
    for key, value in _env(root, recorded["latch"]).items():
        monkeypatch.setenv(key, value)
    ti._recorder_at.cache_clear()
    yield recorded
    ti._recorder_at.cache_clear()


def every_query():
    """One call of every query, with the arguments an agent would use."""
    return {
        "list_experiments": lambda r: q.list_experiments(),
        "list_campaigns": lambda r: q.list_campaigns(EXPERIMENT),
        "validate_experiment": lambda r: q.validate_experiment(EXPERIMENT),
        "lint": lambda r: q.lint(EXPERIMENT),
        "plan_campaign": lambda r: q.plan_campaign(EXPERIMENT, CAMPAIGN, {"rate": ["fast"]}),
        "dry_run_campaign": lambda r: q.dry_run_campaign(EXPERIMENT, CAMPAIGN),
        "coverage_index": lambda r: q.coverage_index(),
        "list_trials": lambda r: q.list_trials(EXPERIMENT),
        "explain_trial": lambda r: q.explain_trial(EXPERIMENT, r["excluded"]),
    }


def snapshot(root: Path) -> dict:
    """Every path below ``root`` with its content hash (or its link target), and every directory."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = "link:" + os.readlink(path)
        elif path.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #
def test_every_tool_has_a_query_and_the_list_is_the_read_only_one():
    assert set(every_query()) == set(q.READ_ONLY_TOOLS), (
        "a tool was added or removed: add it to every_query() and to this file's checks, "
        "after making sure it only reads")
    for name in q.READ_ONLY_TOOLS:
        assert callable(getattr(q, name))


def test_no_query_changes_a_file_starts_a_process_or_opens_a_socket(project, monkeypatch):
    """Every query on the recorded project: the tree is byte-identical afterwards, no process
    started except the lint's pytest, no socket opened (the voice cues are UDP), and the
    stop latch was not written."""
    before = snapshot(project["root"])
    started = []
    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):
        def __init__(self, args, *a, **kw):
            argv = [args] if isinstance(args, (str, bytes)) else [str(x) for x in args]
            started.append(argv)
            # The lint (trials lint and the lint stage of validate) runs pytest over the
            # framework's own rules, which read the declarations. Nothing else may start.
            if argv[:3] != [sys.executable, "-m", "pytest"]:
                raise AssertionError(f"a query started a process: {argv}")
            super().__init__(args, *a, **kw)

    def refuse(name):
        def guard(*a, **kw):
            raise AssertionError(f"a query called os.{name}{a[:2]}")
        return guard

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    for name in ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "execv",
                 "execve", "execvp", "execvpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse(name))

    def no_socket(*a, **kw):
        raise AssertionError("a query opened a socket")
    monkeypatch.setattr(socket, "socket", no_socket)

    results = {name: call(project) for name, call in every_query().items()}

    assert snapshot(project["root"]) == before, "a query changed the project"
    assert not project["latch"].exists(), "a query wrote the stop latch"
    assert all(argv[:3] == [sys.executable, "-m", "pytest"] for argv in started)
    assert started, "the lint did not run, so this test did not cover it"
    assert results["dry_run_campaign"]["executed"] is False
    assert results["dry_run_campaign"]["trials"], "the dry run planned no trial, so it showed nothing"


def test_the_dry_run_calls_the_runner_only_with_dry_run_and_without_a_gate(project, monkeypatch):
    seen = []
    real = tr.run_plan

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(tr, "run_plan", spy)
    q.dry_run_campaign(EXPERIMENT, CAMPAIGN, limit=1)
    assert seen and all(k.get("dry_run") is True and k.get("gate") is None for k in seen)


#: What the server's code may not name: the runner's execution entry points, a gate that runs,
#: the commands that move or write trial files, the stop and shell tools, file writes.
FORBIDDEN_NAMES = {
    "build_gate", "run_scratch", "run_trial", "_run_trial", "run_preflight", "prepare_gate",
    "main_exclude", "main_restore", "main_amend", "exec_tool", "make_poster", "make_stills",
    "write_text", "write_bytes", "mkdir", "makedirs", "unlink", "rmtree", "rename", "touch",
    "symlink_to", "chmod", "copytree", "copyfile", "move", "system", "Popen", "check_call",
    "check_output",
}
#: Modules of the package that act: deciding about trials, re-verifying, the stop latch, the
#: storage mover, the scaffolding, the shell tools (estop.sh, kill), the analyses.
FORBIDDEN_MODULES = {"trial_decisions", "trial_reverify", "stop_latch_clear", "bag_storage",
                     "scaffold", "tools", "analysis_runner", "robot_resets", "subprocess",
                     "shutil", "run_preflight"}


def test_the_server_code_names_no_execution_path():
    """A static check of robofacts/mcp/*.py, so that a later change cannot add an execution
    path without failing here first. Strings (the instructions) are not code, and are
    not checked."""
    problems = []
    for path in MCP_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                problems.append(f"{where}: .{node.attr}")
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                problems.append(f"{where}: {node.id}")
            elif isinstance(node, ast.Import):
                problems += [f"{where}: import {a.name}" for a in node.names
                             if a.name.split(".")[-1] in FORBIDDEN_MODULES]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # `from . import queries` is this package. `from .. import tools` would be the
                # framework's shell tools.
                if node.level != 1 and (module.split(".")[-1] in FORBIDDEN_MODULES
                                        or any(a.name in FORBIDDEN_MODULES for a in node.names)):
                    problems.append(f"{where}: from {'.' * node.level}{module} import "
                                    f"{', '.join(a.name for a in node.names)}")
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    value = kw.value
                    if kw.arg == "execute" or (kw.arg == "dry_run" and not (
                            isinstance(value, ast.Constant) and value.value is True)):
                        problems.append(f"{where}: {kw.arg}={ast.unparse(value)}")
                    if kw.arg in ("gate", "override_reason") and not (
                            isinstance(value, ast.Constant) and value.value in (None, "")):
                        problems.append(f"{where}: {kw.arg}={ast.unparse(value)}")
                if isinstance(node.func, ast.Name) and node.func.id == "open":
                    modes = [a for a in node.args[1:2]] + [k.value for k in node.keywords if k.arg == "mode"]
                    if any(not (isinstance(m, ast.Constant) and set(str(m.value)) <= set("rbt"))
                           for m in modes):
                        problems.append(f"{where}: open() for writing")
                for arg in node.args:
                    if isinstance(arg, (ast.List, ast.Tuple)) and any(
                            isinstance(e, ast.Constant) and e.value in ("--execute", "--gate-override")
                            for e in arg.elts):
                        problems.append(f"{where}: a command line with --execute")
    assert not problems, "the MCP server's code names an execution path:\n" + "\n".join(problems)


def test_validate_never_runs_the_experiments_own_tests(project):
    """An experiment's test_*.py files are project code: the server cannot promise they only
    read, so its validate leaves that stage to the command line, and says so."""
    tests_dir = project["root"] / "experiments" / EXPERIMENT / "scripts"
    marker = project["root"].parent / "own_test_ran"
    probe = tests_dir / "test_probe_mcp.py"
    probe.write_text(f"def test_x():\n    open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    try:
        result = q.validate_experiment(EXPERIMENT)
    finally:
        probe.unlink()
    assert not marker.exists(), "validate ran the experiment's own tests"
    stage = next(s for s in result["stages"] if s["stage"] == "tests")
    assert stage["status"] == "skip" and "project code" in stage["summary"]
    assert result["not_run_here"] == ["tests"]


# --------------------------------------------------------------------------- #
# What the queries answer
# --------------------------------------------------------------------------- #
def test_list_experiments_names_the_project_backend_and_counts(project):
    result = q.list_experiments()
    assert result["backend"] == "process"
    assert result["experiments_root"] == str(project["root"] / "experiments")
    (exp,) = result["experiments"]
    assert exp["name"] == EXPERIMENT and exp["campaigns"] == [CAMPAIGN]
    assert exp["trials"] == {"total": 3, "usable": 1, "unverified": 1, "excluded": 1}


def test_list_campaigns_says_what_is_wanted_and_still_needed(project):
    (campaign,) = q.list_campaigns(EXPERIMENT)["campaigns"]
    assert campaign["name"] == CAMPAIGN
    assert campaign["file"] == f"experiments/{EXPERIMENT}/campaigns/{CAMPAIGN}.yaml"
    assert sorted(campaign["sweep"]) == ["duration", "rate"]
    assert (campaign["wanted"], campaign["covered"], campaign["needed"]) == (4, 1, 3)


def test_plan_counts_only_usable_trials_and_takes_factor_levels(project):
    plan = q.plan_campaign(EXPERIMENT, CAMPAIGN)
    assert (plan["wanted"], plan["covered"], plan["needed"]) == (4, 1, 3)
    assert len(plan["schedule"]) == 3
    assert plan["command"] == f"trials plan experiments/{EXPERIMENT}/campaigns/{CAMPAIGN}.yaml"
    pinned = q.plan_campaign(EXPERIMENT, CAMPAIGN, {"rate": ["fast"]})
    assert pinned["wanted"] == 2 and {c["values"]["rate"] for c in pinned["cells"]} == {"fast"}
    assert "--set rate=fast" in pinned["command"]


def test_a_level_with_a_comma_is_refused_not_turned_into_a_sweep(project):
    # `--set rate=fast,slow` is two levels in the CLI, so a comma inside one level must not pass.
    for call in (q.plan_campaign, q.dry_run_campaign):
        with pytest.raises(q.ToolInputError, match="comma"):
            call(EXPERIMENT, CAMPAIGN, {"rate": ["fast,slow"]})


def test_the_dry_run_shows_the_commands_and_the_operator_command(project):
    result = q.dry_run_campaign(EXPERIMENT, CAMPAIGN, limit=2)
    assert result["executed"] is False and len(result["trials"]) == 2
    first = result["trials"][0]
    assert first["status"] == "dry-run"
    shells = [c["shell"] for c in first["commands"]]
    assert any("system.py" in s for s in shells) and any("run_trial.py" in s for s in shells)
    assert result["operator_command"] == (
        f"trials run experiments/{EXPERIMENT}/campaigns/{CAMPAIGN}.yaml --limit 2 --execute")


def test_validate_and_lint_report_structured_stages(project):
    result = q.validate_experiment(EXPERIMENT)
    assert result["ready"] is True, result
    assert [s["stage"] for s in result["stages"]] == ["check", "plan", "gate", "lint", "tests", "dry_run"]
    lint = q.lint(EXPERIMENT)
    assert lint["ok"] is True and lint["counts"]["passed"] > 0 and lint["problems"] == []


def test_lint_reports_a_broken_rule_by_name(project):
    runner = project["root"] / "experiments" / EXPERIMENT / "config" / "runner.yaml"
    text = runner.read_text(encoding="utf-8")
    start = text.index("verify_steps:")
    end = text.index("hosts:")
    runner.write_text(text[:start] + text[end:], encoding="utf-8")   # breaks rule L6
    try:
        result = q.lint(EXPERIMENT)
    finally:
        runner.write_text(text, encoding="utf-8")
    assert result["ok"] is False and result["counts"]["failed"] >= 1
    assert {"rule": "every experiment declares a verify step", "subject": EXPERIMENT}.items() <= next(
        p for p in result["problems"] if "verify step" in p["rule"]).items(), result


def test_coverage_index_splits_the_classes_and_names_the_reasons(project):
    (exp,) = q.coverage_index(EXPERIMENT)["experiments"]
    assert exp["trials"] == {"total": 3, "usable": 1, "unverified": 1, "excluded": 1}
    assert [v["trial"] for v in exp["contract_violations"]] == [project["broken"]]
    (excluded,) = exp["excluded_trials"]
    assert excluded["trial"] == project["excluded"] and excluded["status"] == "quarantined"
    assert "the log rotated" in excluded["reason"]
    assert [a["factor"] for a in exp["axes"]][:1] and all(a["source"] == "contract" for a in exp["axes"])


def test_list_and_explain_a_trial(project):
    listed = q.list_trials(EXPERIMENT)
    assert {t["name"] for t in listed["trials"]} >= {project["usable"], project["excluded"], project["broken"]}
    assert q.list_trials(EXPERIMENT, status="usable")["total"] == 1

    usable = q.explain_trial(EXPERIMENT, project["usable"])
    assert usable["counts"] is True and usable["class"] == "usable"
    assert usable["explanation"] == f"Usable: it counts as coverage, for {CAMPAIGN}."
    assert usable["gate"]["verdict"] == "GO"
    assert usable["verify_steps"] and all(s["ok"] for s in usable["verify_steps"])
    assert usable["attempts"] and usable["attempts"][0]["status"] == "ran"
    assert usable["gate_log"] and usable["gate_log"][0]["verdict"] == "GO"

    excluded = q.explain_trial(EXPERIMENT, project["excluded"])
    assert excluded["counts"] is False and excluded["class"] == "excluded"
    assert excluded["exclusion"]["category"] == "infrastructure"
    assert "does not count" in excluded["explanation"]

    broken = q.explain_trial(EXPERIMENT, project["broken"])
    assert broken["class"] == "unverified" and broken["contract_problems"]


@pytest.mark.parametrize("call", [
    lambda: q.explain_trial(EXPERIMENT, "../../etc"),
    lambda: q.explain_trial(EXPERIMENT, "no_such_trial"),
    lambda: q.plan_campaign("../experiments", CAMPAIGN),
    lambda: q.plan_campaign(EXPERIMENT, "no_such_campaign"),
    lambda: q.list_campaigns("no_such_experiment"),
    lambda: q.plan_campaign(EXPERIMENT, CAMPAIGN, {"rate": ["sideways"]}),
    lambda: q.validate_experiment(EXPERIMENT, skip=["everything"]),
    lambda: q.list_trials(EXPERIMENT, status="maybe"),
])
def test_a_bad_request_is_refused_with_a_reason(project, call):
    with pytest.raises(q.ToolInputError) as info:
        call()
    assert str(info.value)


def test_trials_mcp_says_how_to_install_the_sdk_when_it_is_missing(monkeypatch, capsys):
    import robofacts.mcp
    from robofacts import cli
    # As if the SDK were not installed, also after another test imported the server.
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)
    monkeypatch.delitem(sys.modules, "robofacts.mcp.server", raising=False)
    monkeypatch.delattr(robofacts.mcp, "server", raising=False)
    assert cli.main(["mcp", "--list-tools"]) == 2
    assert "pip install 'robofacts[mcp]'" in capsys.readouterr().err

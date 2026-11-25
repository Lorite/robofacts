"""The help, the index legend and the closing advice name what exists (2026-11-25).

On the process backend the help of `trials --help`, `trials exclude --help` and `trials run
--scratch` named only `bags_issues` and `bags_scratch`, and the coverage index legend
`bags_issues/`. `trials gate --help`, `trials index --help` and `trials analyze --help`
printed `usage: trials` without the command. `trials new --from` told the user to edit a
RUNBOOK.md that the copy did not have. A NO-GO on a [HARD] item recommended the override.
"""
from __future__ import annotations

import json

import pytest

from robofacts import cli, preflight_gate as pg, scaffold, trial_index as ti
from test_process_backend import use_project, write_project


@pytest.mark.parametrize("command", ["gate", "index", "analyze", "plan", "run", "check",
                                     "metadata", "storage", "version"])
def test_the_usage_of_a_command_names_the_command(command, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main([command, "--help"])
    assert info.value.code == 0
    assert capsys.readouterr().out.startswith(f"usage: trials {command} ")


def test_the_help_names_the_folders_of_both_backends(capsys):
    assert cli.main(["--help"]) == 0
    (line,) = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("exclude")]
    assert "bags_issues/" in line and "recordings_issues/" in line
    for command, folders in (("exclude", ("bags_issues/", "recordings_issues/")),
                             ("restore", ("bags_issues/", "recordings_issues/")),
                             ("run", ("bags_scratch/", "recordings_scratch/",
                                      "bags_scratch_slim/", "recordings_scratch_slim/"))):
        with pytest.raises(SystemExit):
            cli.main([command, "--help"])
        text = " ".join(capsys.readouterr().out.split())
        assert all(folder in text for folder in folders), (command, text)


def test_the_index_legend_names_the_issues_folder_of_the_backend(tmp_path, monkeypatch):
    experiments = write_project(tmp_path / "proj")
    use_project(tmp_path / "proj", monkeypatch)
    report = ti.render_report([], experiments)
    assert "`recordings_issues/`" in report and "bags_issues" not in report
    assert "22 recordings" in report


def test_new_from_names_a_runbook_only_when_the_copy_has_one(tmp_path, monkeypatch, capsys):
    experiments = write_project(tmp_path / "proj")
    use_project(tmp_path / "proj", monkeypatch)
    assert scaffold.main_new(["copy", "--from", "plant", "--experiments-root", str(experiments)]) == 0
    out = capsys.readouterr().out
    assert "RUNBOOK.md" not in out and "5. README.md (the design), scripts/" in out
    (experiments / "plant" / "RUNBOOK.md").write_text("Run campaigns/rates.yaml.\n", encoding="utf-8")
    assert scaffold.main_new(["copy2", "--from", "plant", "--experiments-root", str(experiments)]) == 0
    assert "5. README.md (the design), RUNBOOK.md, scripts/" in capsys.readouterr().out


@pytest.mark.parametrize("text, suggests_override", [("[HARD] The file exists.", False),
                                                     ("The file exists.", True)])
def test_a_no_go_on_a_hard_item_does_not_suggest_the_override(tmp_path, text, suggests_override):
    checklist = tmp_path / "checklist.json"
    checklist.write_text(json.dumps({"checklist": [
        {"text": text, "check": "file_exists", "args": {"path": str(tmp_path / "missing")}}]}),
        encoding="utf-8")
    with pytest.raises(pg.PreflightBlocked) as info:
        pg.PreflightGate(checklist=checklist).check("t1")
    message = str(info.value)
    assert ("override" in message) is suggests_override, message
    if not suggests_override:
        assert "fix the cause, then run again" in message

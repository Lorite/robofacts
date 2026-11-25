"""The fast path for a new experiment (2026-10-14): `trials new <name> --from <existing>` copies
and renames the declarations of a similar experiment, `trials validate` answers "is it ready"
in one command, and `trials lint --experiment` lints one experiment. Creating one experiment
took an agent 44 minutes that day, most of it spent on the template's demo files, on four
separate validation commands and on every test suite of the project."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from robofacts import cli, scaffold, validate

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE_ROOT / "example_project"
#: Both packages, for a child process that runs them from this source tree.
SOURCE_PATH = os.pathsep.join([str(PACKAGE_ROOT.parent / "robofacts"), str(PACKAGE_ROOT)])
SOURCE = "demo_talker_listener"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    shutil.copytree(EXAMPLE, root)
    # conftest points both roots at the shipped example; this test works on a copy of it.
    monkeypatch.setenv("TRIALS_EXPERIMENTS_ROOT", str(root / "experiments"))
    monkeypatch.setenv("TRIALS_PROJECT_ROOT", str(root))
    monkeypatch.delenv("TRIALS_VOICE", raising=False)
    source = root / "experiments" / SOURCE
    # What a lived-in experiment also holds: data, notebooks, a test, a link, a large asset.
    (source / "bags" / "20261014T120000_trial").mkdir(parents=True)
    (source / "bags" / "20261014T120000_trial" / "trial_metadata.json").write_text("{}")
    (source / "results" / "old").mkdir(parents=True)
    (source / "notebooks").mkdir()
    (source / "notebooks" / f"{SOURCE}_run.ipynb").write_text("{}")
    (source / "scripts" / "test_listen_for.py").write_text("def test_nothing():\n    pass\n")
    (source / "scripts" / "__pycache__").mkdir()
    (source / "scripts" / "__pycache__" / "listen_for.cpython-310.pyc").write_bytes(b"\0")
    (source / "assets").mkdir()
    (source / "assets" / "mesh.bin").write_bytes(b"\0" * (scaffold.MAX_COPY_BYTES + 1))
    (source / "assets" / "notes.txt").write_text(f"see {SOURCE} and {SOURCE}_v2\n")
    os.symlink("../../../shared_pkg", source / "ros2_pkg")
    return root


def experiments(project):
    return project / "experiments"


def test_new_from_copies_the_declarations_renamed_and_nothing_recorded(project):
    report = scaffold.new_experiment_from("demo_copy", SOURCE, experiments(project))
    target = experiments(project) / "demo_copy"

    runner = (target / "config" / "runner.yaml").read_text()
    assert "experiments/demo_copy/scripts/listen_for.py" in runner
    assert SOURCE not in runner
    assert (target / "scripts" / "listen_for.py").is_file()
    assert os.access(target / "scripts" / "listen_for.py", os.X_OK)
    assert (target / "campaigns" / "rates.yaml").is_file()
    # Whole names only: a sibling experiment's name is not rewritten.
    assert (target / "assets" / "notes.txt").read_text() == "see demo_copy and demo_talker_listener_v2\n"

    for data in ("bags", "runs", "results"):
        assert [p.name for p in (target / data).iterdir()] == [".gitkeep"]
    assert not (target / "notebooks").exists()
    assert not (target / "scripts" / "__pycache__").exists()
    assert not (target / "scripts" / "test_listen_for.py").exists()
    assert not (target / "assets" / "mesh.bin").exists()
    skipped = dict(report.skipped)
    assert "scripts/test_listen_for.py" in skipped and "assets/mesh.bin" in skipped

    assert os.readlink(target / "ros2_pkg") == "../../../shared_pkg"
    readme = (target / "README.md").read_text()
    assert readme.startswith("# demo_copy") and f"--from {SOURCE}" in readme
    # The source is untouched.
    assert SOURCE in (experiments(project) / SOURCE / "config" / "runner.yaml").read_text()


def test_new_from_copies_only_the_named_campaign(project):
    source_campaigns = experiments(project) / SOURCE / "campaigns"
    shutil.copy(source_campaigns / "rates.yaml", source_campaigns / "other.yaml")
    scaffold.new_experiment_from("demo_copy", SOURCE, experiments(project), ["rates"])
    assert sorted(p.name for p in (experiments(project) / "demo_copy" / "campaigns").iterdir()) == ["rates.yaml"]


def test_new_from_renames_a_campaign(project):
    source = experiments(project) / SOURCE
    (source / "RUNBOOK.md").write_text("Run campaigns/rates.yaml. The rates campaign sweeps the rate.\n")
    report = scaffold.new_experiment_from("demo_copy", SOURCE, experiments(project), ["rates:fast_rates"])
    target = experiments(project) / "demo_copy"
    assert sorted(p.name for p in (target / "campaigns").iterdir()) == ["fast_rates.yaml"]
    campaign = (target / "campaigns" / "fast_rates.yaml").read_text()
    assert "name: fast_rates" in campaign and "experiment: demo_copy" in campaign
    assert (target / "RUNBOOK.md").read_text().startswith("Run campaigns/fast_rates.yaml.")
    # The bare word may be prose ("two rates" in the campaign's comment), so it is reported.
    assert sorted(report.campaign_mentions) == ["RUNBOOK.md (names rates)",
                                                "campaigns/fast_rates.yaml (names rates)"]
    assert report.campaigns == ["fast_rates"]
    assert validate.validate("demo_copy", experiments(project)).ready


def test_new_from_leaves_out_what_no_declaration_names(project, capsys):
    """The adoption test of 2026-10-15 deleted the source's AGENTS.md, its gate-log design
    note, two study scripts and a CycloneDDS file by hand after `trials new --from`."""
    source = experiments(project) / SOURCE
    (source / "AGENTS.md").write_text(f"# AGENTS.md: {SOURCE}\n")
    (source / "GATE_LOG_TODO.md").write_text("design note\n")
    (source / ".gitignore").write_text("*.log\n")
    (source / "scripts" / "e2_analysis.py").write_text("print('one study')\n")
    (source / "config" / "cyclonedds_local_sim.xml").write_text("<!-- export cyclonedds_local_sim.xml -->\n")
    (source / "config" / "trial_media.yaml").write_text("camera: {topic: /camera/image}\n")
    # Named by the RUNBOOK, and that script imports a module: both reach the copy.
    (source / "RUNBOOK.md").write_text("Record with `scripts/record.sh`.\n")
    (source / "scripts" / "record.sh").write_text("python3 -c 'import rate_lib'\n")
    (source / "scripts" / "rate_lib.py").write_text("RATE = 1\n")

    rc = cli.main(["new", "demo_copy", "--from", SOURCE, "--experiments-root", str(experiments(project))])
    out = capsys.readouterr().out
    target = experiments(project) / "demo_copy"

    assert rc == 0
    for kept in ("RUNBOOK.md", ".gitignore", "scripts/record.sh", "scripts/rate_lib.py",
                 "scripts/listen_for.py", "ros2_pkg"):
        assert (target / kept).exists() or (target / kept).is_symlink(), kept
    for gone in ("AGENTS.md", "GATE_LOG_TODO.md", "scripts/e2_analysis.py"):
        assert not (target / gone).exists(), gone
        assert f"  {gone}\n" in out.split("Left out")[1]
    # A launch file outside the experiment may load a config file, so it is copied and listed.
    assert (target / "config" / "cyclonedds_local_sim.xml").is_file()
    assert "  config/cyclonedds_local_sim.xml" in out.split("Copied, but no declaration names them")[1]
    # The camera the trial cards show is a declaration: copied, not listed for review.
    assert (target / "config" / "trial_media.yaml").is_file()
    assert "trial_media.yaml" not in out.split("Copied, but no declaration names them")[1]
    assert validate.validate("demo_copy", experiments(project)).ready


@pytest.mark.parametrize("name, source, campaigns, message", [
    ("Bad-Name", SOURCE, None, "not a valid experiment name"),
    ("demo_copy", "no_such_experiment", None, "no experiment 'no_such_experiment'"),
    ("demo_copy", SOURCE, ["missing"], "has no campaign missing"),
    ("demo_copy", SOURCE, ["rates:Bad-Name"], "not a valid campaign name"),
    (SOURCE, SOURCE, None, "already exists"),
])
def test_new_from_refuses_clearly(project, name, source, campaigns, message):
    with pytest.raises(scaffold.ScaffoldError, match=message):
        scaffold.new_experiment_from(name, source, experiments(project), campaigns)


def test_cli_new_from_prints_the_review_order(project, capsys):
    rc = cli.main(["new", "demo_copy", "--from", SOURCE, "--experiments-root", str(experiments(project))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Not copied:" in out and "trials validate experiments/demo_copy" in out


def test_campaign_without_from_is_a_usage_error(project):
    with pytest.raises(SystemExit) as exc:
        cli.main(["new", "demo_two", "--campaign", "rates", "--experiments-root", str(experiments(project))])
    assert exc.value.code == 2


def test_a_copy_validates_ready(project):
    scaffold.new_experiment_from("demo_copy", SOURCE, experiments(project))
    report = validate.validate("demo_copy", experiments(project))
    text = validate.render(report)
    assert report.ready, text
    stages = {r.stage: r.status for r in report.results}
    assert stages == {"check": "ok", "plan": "ok", "gate": "ok", "lint": "ok",
                      "tests": "skip", "dry_run": "ok"}, text
    assert "READY: demo_copy" in text
    assert "wanted 4" in text  # nothing recorded was copied, so the plan starts from zero
    assert "1 trial(s) planned, every command resolved" in text
    assert "2 script(s) run untested: listen_for.py, verify_chatter.py" in text
    assert "TRIALS_VOICE" not in os.environ  # the dry run's --no-voice does not leak


def test_a_broken_copy_is_not_ready_and_says_why(project):
    scaffold.new_experiment_from("demo_copy", SOURCE, experiments(project))
    runner = experiments(project) / "demo_copy" / "config" / "runner.yaml"
    runner.write_text(runner.read_text().replace("listen_for.py", "listen_for_typo.py"))
    (experiments(project) / "demo_copy" / "scripts" / "test_fails.py").write_text(
        "def test_fails():\n    assert False\n")
    report = validate.validate("demo_copy", experiments(project))
    failed = {r.stage for r in report.results if r.status == "FAIL"}
    assert failed == {"lint", "tests"}, validate.render(report)
    text = validate.render(report)
    assert "NOT READY" in text and "listen_for_typo.py" in text


def test_validate_one_campaign_and_the_cli_exit_codes(project, capsys):
    campaign = experiments(project) / SOURCE / "campaigns" / "rates.yaml"
    assert cli.main(["validate", str(campaign), "--experiments-root", str(experiments(project)),
                     "--skip", "lint"]) == 0
    out = capsys.readouterr().out
    assert "skip  lint" in out and "READY" in out
    assert cli.main(["validate", "no_such_experiment", "--experiments-root", str(experiments(project))]) == 2


def test_experiment_tests_skip_data_and_links(project):
    source = experiments(project) / SOURCE
    (source / "bags" / "test_in_a_bag.py").write_text("")
    shared = project / "shared_pkg"
    shared.mkdir()
    (shared / "test_of_the_package.py").write_text("")
    found = [p.relative_to(source) for p in validate.experiment_tests(source)]
    assert found == [Path("scripts/test_listen_for.py")]


def test_lint_experiment_filters_the_parametrized_rules(project):
    scaffold.new_experiment_from("demo_copy", SOURCE, experiments(project))
    env = {**os.environ, "PYTHONPATH": SOURCE_PATH + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

    def collected(*extra):
        out = subprocess.run([sys.executable, "-m", "robofacts.cli", "lint",
                              "--experiments-root", str(experiments(project)), *extra,
                              "--collect-only"],
                             capture_output=True, text=True, env=env, cwd=project, timeout=300)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout

    everything = collected()
    assert f"[{SOURCE}]" in everything and "[demo_copy]" in everything
    only = collected("--experiment", "demo_copy")
    assert "[demo_copy]" in only and f"[{SOURCE}]" not in only
    out = subprocess.run([sys.executable, "-m", "robofacts.cli", "lint",
                          "--experiments-root", str(experiments(project)), "--experiment", "nope"],
                         capture_output=True, text=True, env=env, cwd=project, timeout=300)
    assert out.returncode == 2 and "no experiment 'nope'" in out.stderr

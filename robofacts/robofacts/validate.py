"""``trials validate <experiment | campaign.yaml>``: is this experiment ready for its first trial?

One command, one verdict. It runs, for one experiment and its campaigns (or one campaign):

check    every campaign loads and expands (``trials check``)
plan     the planner accepts it against the recorded trials (``trials plan``)
gate     every T-0 checklist it uses is coherent (``trials gate --validate``), the
         experiment's own and each campaign-scoped one
lint     the declaration rules, for THIS experiment only (``trials lint --experiment``)
tests    the experiment's own ``test_*.py`` files, if it has any
dry run  ``trials run --limit 1`` once per execution host the campaign uses, so the local
         and the remote command paths are both printed and checked

Each stage reports ok, FAIL or skip with its time, and a failed stage prints the output of
the command behind it. Nothing is launched, recorded or written: every stage is the dry
form of its command. Exit code 0 means READY, 1 means NOT READY, 2 a usage error.

It exists because the separate commands, plus the habit of running every test suite of
the project to be sure, made "is the new experiment ready" a 15-minute question
(2026-10-14). The stages here take seconds, and the full suites stay with CI.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import roots

#: The lint reads this to lint one experiment (lint.ENV_ONLY_EXPERIMENT, not imported here).
LINT_ENV = "TRIALS_LINT_EXPERIMENT"
STAGES = ("check", "plan", "gate", "lint", "tests", "dry_run")
DATA_DIRS = ("bags", "runs", "results", "notebooks")
_PYTEST_SUMMARY = re.compile(r"\d+ (passed|failed|error|errors|skipped|deselected)")
_GIT_HOOK_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX")


@dataclass
class StageResult:
    stage: str
    subject: str
    status: str  # ok | FAIL | skip
    summary: str = ""
    seconds: float = 0.0
    output: str = ""


@dataclass
class Report:
    experiment: str
    campaigns: List[Path]
    results: List[StageResult] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.results) and all(r.status != "FAIL" for r in self.results)


def _in_process(main: Callable[[List[str]], int], argv: List[str]) -> tuple:
    """Run a framework command's main() with its output captured. Returns (rc, output).
    The environment is restored afterwards (the runner's --no-voice sets TRIALS_VOICE)."""
    buffer = io.StringIO()
    saved_env = dict(os.environ)
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        try:
            rc = int(main(argv) or 0)
        except SystemExit as exc:  # argparse errors
            rc = int(exc.code or 0) if isinstance(exc.code, int) else 2
        except Exception as exc:  # noqa: BLE001 - a crash is a failed stage, not a traceback
            print(f"{type(exc).__name__}: {exc}")
            rc = 1
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
    return rc, buffer.getvalue()


def _pytest(paths: Sequence[str], experiments_root: Path, extra_env: Optional[dict] = None) -> tuple:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_HOOK_VARS}
    package_parent = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = package_parent + os.pathsep + env.get("PYTHONPATH", "")
    env[roots.ENV_EXPERIMENTS_ROOT] = str(experiments_root)
    # ROS installs pytest plugins that cost seconds to load and that no lint needs.
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    env.update(extra_env or {})
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths],
                          capture_output=True, text=True, env=env)
    output = proc.stdout + proc.stderr
    summary = next((line.strip("= ") for line in reversed(output.splitlines())
                    if _PYTEST_SUMMARY.search(line)), "")
    return proc.returncode, output, summary


def _timed(report: Report, stage: str, subject: str, run: Callable[[], tuple]) -> StageResult:
    start = time.monotonic()
    status, summary, output = run()
    result = StageResult(stage, subject, status, summary, time.monotonic() - start, output)
    report.results.append(result)
    return result


def experiment_tests(experiment_dir: Path) -> List[Path]:
    """The experiment's own test files. Data directories are not searched, and links
    (such as a ros2_pkg/ that points at a package tested on its own) are not followed."""
    found = []
    for root, dirs, files in os.walk(experiment_dir):
        here = Path(root)
        dirs[:] = [d for d in dirs
                   if d != "__pycache__" and not (here == experiment_dir and
                                                  (d in DATA_DIRS or d.startswith("bags_")))]
        found += [here / f for f in files if f.startswith("test_") and f.endswith(".py")]
    return sorted(found)


def _resolve(target: str, experiments_root: Path) -> tuple:
    """(experiment name, campaign files) for an experiment name, directory or campaign file."""
    path = Path(target)
    if path.suffix in (".yaml", ".yml"):
        if not path.is_file():
            raise ValueError(f"campaign not found: {target}")
        from . import campaign_spec as cs
        try:
            experiment = cs._build_campaign(path, experiments_root).experiment
        except cs.SpecError:
            experiment = path.resolve().parent.parent.name
        return experiment, [path]
    name = path.name if path.is_dir() else target
    experiment_dir = experiments_root / name
    if not (experiment_dir / "config").is_dir():
        raise ValueError(f"no experiment {name!r} under {experiments_root}")
    return name, sorted((experiment_dir / "campaigns").glob("*.yaml"))


def _hosts_of(campaign_path: Path, experiments_root: Path) -> List[str]:
    """The execution host levels the campaign's trials use, in order of first use."""
    from . import campaign_spec as cs
    try:
        campaign = cs.load_campaign(campaign_path, experiments_root)
    except cs.SpecError:
        return []
    hosts: List[str] = []
    for trial in campaign.combinations():
        host = trial.host(campaign.factors)
        if host is not None and str(host) not in hosts:
            hosts.append(str(host))
    return hosts


def _host_factor(campaign_path: Path, experiments_root: Path) -> Optional[str]:
    from . import campaign_spec as cs
    try:
        campaign = cs.load_campaign(campaign_path, experiments_root)
    except cs.SpecError:
        return None
    return next((n for n, f in campaign.factors.items() if f.maps_to.get("kind") == "host"), None)


def _last_line(output: str, pattern: str) -> str:
    regex = re.compile(pattern)
    return next((line.strip() for line in reversed(output.splitlines()) if regex.search(line)), "")


def validate(target: str, experiments_root: Path, skip: Sequence[str] = ()) -> Report:
    from . import campaign_plan, campaign_spec
    from . import trial_runner
    from .preflight import run_preflight

    experiment, campaigns = _resolve(target, experiments_root)
    experiment_dir = experiments_root / experiment
    report = Report(experiment, campaigns)
    root_args = ["--experiments-root", str(experiments_root)]

    def skipped(stage: str, why: str) -> None:
        report.results.append(StageResult(stage, "", "skip", why))

    # check and plan, per campaign
    for stage, main, extra in (("check", campaign_spec.main, ["--limit", "1"]),
                               ("plan", campaign_plan.main, [])):
        if stage in skip:
            skipped(stage, "--skip")
            continue
        if not campaigns:
            report.results.append(StageResult(stage, "", "FAIL", "the experiment has no campaigns/*.yaml"))
            continue
        for campaign in campaigns:
            def run(campaign=campaign, main=main, extra=extra, stage=stage):
                rc, out = _in_process(main, [str(campaign), *root_args, *extra])
                if rc != 0:
                    return "FAIL", _last_line(out, r"ERROR"), out
                if stage == "plan":
                    return "ok", _last_line(out, r"wanted \d+"), out
                size = campaign_spec.load_campaign(campaign, experiments_root).size()
                return "ok", f"loads, {size} trial(s)", out
            _timed(report, stage, campaign.name, run)

    # gate: the experiment's checklist and every campaign-scoped one
    if "gate" in skip:
        skipped("gate", "--skip")
    else:
        import yaml
        checklists = []
        default = experiment_dir / "config" / "preflight_checklist.json"
        if default.is_file():
            checklists.append(default)
        for campaign in campaigns:
            try:
                rel = str((yaml.safe_load(campaign.read_text(encoding="utf-8")) or {})
                          .get("preflight_checklist") or "")
            except yaml.YAMLError:
                rel = ""  # the check stage reports the broken file
            if rel and (experiment_dir / rel) not in checklists:
                checklists.append(experiment_dir / rel)
        if not checklists:
            skipped("gate", "no preflight_checklist.json (every trial runs ungated)")
        for checklist in checklists:
            def run(checklist=checklist):
                if not checklist.is_file():
                    return "FAIL", f"{checklist} does not exist", ""
                rc, out = _in_process(run_preflight.main, ["--checklist", str(checklist), "--validate"])
                summary = _last_line(out, r"items|coherent|ERROR|problem")
                # "✓ <path>: 17 items (...) - coherent." -> "17 items (...) - coherent."
                summary = re.sub(r"^\S*\s*\S+\.json:\s*", "", summary)
                return ("ok" if rc == 0 else "FAIL"), summary, out
            _timed(report, "gate", str(checklist.relative_to(experiment_dir)), run)

    # lint, scoped to this experiment
    if "lint" in skip:
        skipped("lint", "--skip")
    else:
        # By path, not imported here: the lint resolves its root when pytest imports it.
        lint_file = Path(__file__).with_name("lint.py")

        def run_lint():
            from . import backend
            lint_files = [str(lint_file), *[str(f) for f in backend.active(experiments_root).lint_files]]
            rc, out, summary = _pytest(lint_files, experiments_root,
                                       {LINT_ENV: experiment})
            return ("ok" if rc == 0 else "FAIL"), summary, out
        _timed(report, "lint", experiment, run_lint)

    # the experiment's own tests
    tests = experiment_tests(experiment_dir)
    if "tests" in skip:
        skipped("tests", "--skip")
    elif not tests:
        scripts = sorted(p.name for p in (experiment_dir / "scripts").glob("*.py"))
        skipped("tests", "no test_*.py in the experiment" +
                (f", so {len(scripts)} script(s) run untested: {', '.join(scripts)}" if scripts else ""))
    else:
        def run_tests():
            rc, out, summary = _pytest([str(t) for t in tests], experiments_root)
            return ("ok" if rc == 0 else "FAIL"), summary, out
        _timed(report, "tests", f"{len(tests)} file(s)", run_tests)

    # dry run, once per execution host the campaign uses
    if "dry_run" in skip:
        skipped("dry_run", "--skip")
    else:
        if not (experiment_dir / "config" / "runner.yaml").is_file():
            report.results.append(StageResult("dry_run", "", "FAIL",
                                              "no config/runner.yaml: the runner cannot run a trial"))
        for campaign in campaigns if (experiment_dir / "config" / "runner.yaml").is_file() else []:
            hosts = _hosts_of(campaign, experiments_root) or [None]
            host_factor = _host_factor(campaign, experiments_root)
            for host in hosts:
                overrides = ["--set", f"{host_factor}={host}"] if host and host_factor and len(hosts) > 1 else []

                def run(campaign=campaign, overrides=overrides):
                    rc, out = _in_process(trial_runner.main,
                                          [str(campaign), *root_args, "--limit", "1", "--no-voice",
                                           *overrides])
                    summary = _last_line(out, r"trial\(s\):|Nothing to run|ERROR")
                    planned = re.search(r"(\d+) trial\(s\):", summary)
                    if rc == 0 and planned:
                        # "1 trial(s): 0 OK, ..." counts outcomes, and a dry run has none.
                        summary = f"{planned.group(1)} trial(s) planned, every command resolved"
                    return ("ok" if rc == 0 else "FAIL"), summary, out
                subject = campaign.name + (f" @ {host}" if host else "")
                _timed(report, "dry_run", subject, run)
    return report


def render(report: Report, verbose: bool = False) -> str:
    names = ", ".join(c.stem for c in report.campaigns) or "none"
    lines = [f"trials validate {report.experiment}  (campaigns: {names})", ""]
    for r in report.results:
        mark = {"ok": "ok  ", "FAIL": "FAIL", "skip": "skip"}[r.status]
        timing = f"{r.seconds:5.1f} s" if r.status != "skip" else "       "
        lines.append(f"  {mark}  {r.stage:<8} {timing}  {r.subject}{': ' if r.subject and r.summary else ''}"
                     f"{r.summary}")
    failed = [r for r in report.results if r.status == "FAIL"]
    for r in [r for r in (report.results if verbose else failed) if r.output.strip()]:
        tail = r.output.strip().splitlines()[-40:]
        lines += ["", f"--- {r.stage} {r.subject} ---", *(f"    {line}" for line in tail)]
    total = sum(r.seconds for r in report.results)
    lines.append("")
    if report.ready:
        first = (os.path.relpath(report.campaigns[0]) if report.campaigns else "<campaign>")
        lines.append(f"READY: {report.experiment} passed every stage in {total:.1f} s. The next step "
                     f"is a real trial, which needs its own approval: trials run {first} --limit 1 --execute")
    else:
        lines.append(f"NOT READY: {len(failed)} stage(s) failed ({total:.1f} s). Fix them and run "
                     f"trials validate again.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="trials validate",
                                 description="is this experiment ready for its first trial? "
                                             "check, plan, gate, lint, its tests and a dry run, "
                                             "one verdict")
    ap.add_argument("target", help="an experiment (name or directory) or one campaign YAML")
    roots.add_root_arguments(ap)
    ap.add_argument("--skip", action="append", default=[], choices=STAGES,
                    help="leave out a stage (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the output of every stage, not only of the failed ones")
    args = ap.parse_args(argv)
    try:
        roots.resolve_root_arguments(args)
        report = validate(args.target, args.experiments_root, args.skip)
    except (ValueError, roots.RootNotFound) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(render(report, args.verbose))
    return 0 if report.ready else 1


if __name__ == "__main__":
    sys.exit(main())

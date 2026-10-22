"""The ``trials`` command: one entry point, one subcommand per framework tool.

    trials plan    experiments/<exp>/campaigns/<c>.yaml   what the campaign still needs
    trials run     experiments/<exp>/campaigns/<c>.yaml   dry run; --execute runs the trials
    trials explore experiments/<exp>/campaigns/<c>.yaml   one condition's stack, live in Foxglove, never coverage
    trials index   --experiment <exp> --summary            the coverage index
    trials records <exp> [--campaign <c>] [--status <s>]   every trial with its bag, gate, analyses, video
    trials exclude <exp> <trial> --reason ... --category c  move a trial out of coverage, with its reason
    trials restore <exp> <trial>                           move an excluded trial back
    trials amend   <exp> <trial> <factor>=<level> --reason  correct a recorded level, beside the metadata
    trials lint    [--experiment <exp>]                    the declarations against the rules
    trials validate <exp | campaign.yaml>                  is it ready: check, plan, gate, lint, tests, dry run
    trials gate    --checklist <path>                      the T-0 gate, by hand
    trials analyze <exp> <analysis> --execute              a named analysis over a cohort
    trials new     <name> [--from <exp>]                   a new experiment from the template or a similar one
    trials init    <dir>                                   a new project (experiments root)
    trials ui                                              the web interface

Every subcommand takes ``--experiments-root`` (see ``trials roots``). The same
commands work as ``ros2 run robofacts trials <subcommand>``.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__, roots

Handler = Callable[[List[str]], int]


def _plan(argv):
    from . import campaign_plan
    return campaign_plan.main(argv)


def _run(argv):
    from . import trial_runner
    return trial_runner.main(argv)


def _explore(argv):
    from . import explore
    return explore.main(argv)


def _index(argv):
    from . import trial_index
    return trial_index.main(argv)


def _records(argv):
    from . import trial_records
    return trial_records.main(argv)


def _exclude(argv):
    from . import trial_decisions
    return trial_decisions.main_exclude(argv)


def _restore(argv):
    from . import trial_decisions
    return trial_decisions.main_restore(argv)


def _amend(argv):
    from . import trial_decisions
    return trial_decisions.main_amend(argv)


def _check(argv):
    from . import campaign_spec
    return campaign_spec.main(argv)


def _metadata(argv):
    from . import trial_metadata_schema
    return trial_metadata_schema.main(argv)


def _analyze(argv):
    from . import analysis_runner
    return analysis_runner.main(argv)


def _gate(argv):
    from .preflight import run_preflight
    return run_preflight.main(argv)


def _storage(argv):
    from . import bag_storage
    return bag_storage.main(argv)


def _slim(argv):
    from . import slim_bag_archive
    return slim_bag_archive.main(argv)


def _version_probe(argv):
    from . import code_version
    return code_version.main(argv)


def _ui(argv):
    # The parser lives apart from the app, so the help needs no Flask.
    from .ui.arguments import build_parser
    if any(a in ("-h", "--help") for a in argv):
        build_parser().print_help()
        return 0
    try:
        from .ui import app
    except ImportError as exc:
        if "flask" in str(exc).lower():
            print("trials ui: the web interface needs Flask (`pip install flask`), which the dev "
                  "container has. The other commands do not need it.", file=sys.stderr)
            return 2
        raise
    return app.main(argv)


def _validate(argv):
    from . import validate
    return validate.main(argv)


def _new(argv):
    from . import scaffold
    return scaffold.main_new(argv)


def _init(argv):
    from . import scaffold
    return scaffold.main_init(argv)


def _tool(argv):
    from . import tools
    usage = "usage: trials tool <name> [args...]\n\npackaged tools:\n" + "".join(f"  {n}\n" for n in tools.available())
    if not argv:
        print(usage, file=sys.stderr)
        return 2
    if argv[0] in ("-h", "--help", "help"):
        print(usage)
        return 0
    try:
        tools.path(argv[0])
    except FileNotFoundError as exc:
        print(f"trials tool: {exc}", file=sys.stderr)
        return 2
    return tools.exec_tool(argv[0], argv[1:])


def _lint(argv):
    """Run the declaration lint with pytest over the resolved experiments root."""
    import argparse

    ap = argparse.ArgumentParser(prog="trials lint",
                                 description="check every experiment's declarations against the rules")
    roots.add_root_arguments(ap)
    ap.add_argument("--experiment", metavar="NAME",
                    help="lint only this experiment (the rules over shared files still run)")
    ap.add_argument("pytest_args", nargs="*",
                    help="extra pytest arguments (flags such as -k, -x or -v pass through as they are, "
                         "or after --)")
    # Flags that are not ours go to pytest, so `trials lint -k rule_L5 -x` works as written.
    args, passthrough = ap.parse_known_args(argv)
    roots.resolve_root_arguments(args)
    os.environ[roots.ENV_EXPERIMENTS_ROOT] = str(args.experiments_root)
    if args.experiment:
        if not (args.experiments_root / args.experiment / "config").is_dir():
            print(f"ERROR: no experiment {args.experiment!r} under {args.experiments_root}", file=sys.stderr)
            return 2
        os.environ["TRIALS_LINT_EXPERIMENT"] = args.experiment
    import pytest

    from . import lint
    return int(pytest.main(["-q", "-p", "no:cacheprovider", lint.__file__, *passthrough, *args.pytest_args]))


def _roots(argv):
    """Print where the two roots resolve to, and why."""
    import argparse

    ap = argparse.ArgumentParser(prog="trials roots", description=roots.__doc__.splitlines()[0])
    roots.add_root_arguments(ap, project=True)
    args = ap.parse_args(argv)
    try:
        roots.resolve_root_arguments(args)
    except roots.RootNotFound as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    project_file = roots.find_project_file()
    print(f"experiments root : {args.experiments_root}")
    print(f"project root     : {args.project_root}")
    print(f"{roots.PROJECT_FILE:<17}: {project_file or '(none above the working directory)'}")
    print(f"environment      : {roots.ENV_EXPERIMENTS_ROOT}={os.environ.get(roots.ENV_EXPERIMENTS_ROOT, '')!s} "
          f"{roots.ENV_PROJECT_ROOT}={os.environ.get(roots.ENV_PROJECT_ROOT, '')!s}")
    return 0


COMMANDS: Dict[str, Tuple[Handler, str]] = {
    "plan": (_plan, "what a campaign still needs (the planner)"),
    "run": (_run, "run the trials a campaign still needs (dry run without --execute)"),
    "explore": (_explore, "bring up one condition's stack with a Foxglove view that may change "
                          "parameters (dry run without --execute, never coverage)"),
    "index": (_index, "the coverage index over the recorded trials"),
    "records": (_records, "every trial of an experiment with its campaign, gate, slim bag, analyses, video"),
    "exclude": (_exclude, "move a trial out of coverage into bags_issues, with exclusion.json "
                          "(its reason)"),
    "restore": (_restore, "move an excluded trial back into coverage, keeping its old reason"),
    "amend": (_amend, "correct a recorded factor level in corrections.json; "
                      "trial_metadata.json is never edited"),
    "lint": (_lint, "check every declaration against the rules (pytest)"),
    "validate": (_validate, "is an experiment ready: check, plan, gate, lint, its tests, dry run"),
    "gate": (_gate, "run a T-0 gate checklist by hand"),
    "analyze": (_analyze, "run a named analysis over a cohort of bags"),
    "check": (_check, "validate one campaign file, or list an experiment's factor space"),
    "metadata": (_metadata, "audit the recorded trial metadata against the contract"),
    "storage": (_storage, "where this host writes heavy bags"),
    "slim": (_slim, "archive slim copies of recorded bags"),
    "version": (_version_probe, "the code version stamped into trials"),
    "ui": (_ui, "the web interface"),
    "new": (_new, "a new experiment from the template, or --from a similar one"),
    "init": (_init, "a new project: the experiments root with its shared config"),
    "tool": (_tool, "run a packaged shell tool (kill_ros.sh, estop.sh, robot_action.sh)"),
    "roots": (_roots, "show how the experiments root and the project root resolve"),
}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [f"trials {__version__}: declarative experiment design for ROS 2 trials", "",
             "usage: trials <command> [arguments]   (each command answers --help)", ""]
    lines += [f"  {name:<{width}}  {help_}" for name, (_, help_) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    if argv[0] in ("--version", "-V"):
        print(__version__)
        return 0
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"trials: unknown command {name!r}\n\n{_usage()}", file=sys.stderr)
        return 2
    try:
        return int(COMMANDS[name][0](rest) or 0)
    except roots.RootNotFound as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

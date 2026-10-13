"""``trials new <name>`` and ``trials init <dir>``: a new experiment from the
commented template, and a new project (an experiments root with its shared
configuration). The templates live in ``skeleton/`` next to this module."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import roots

SKELETON = Path(__file__).resolve().parent / "skeleton"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ScaffoldError(RuntimeError):
    pass


def _template_files(source: Path):
    """The template files below ``source``, skipping byte-code caches a build may leave."""
    return sorted(p for p in source.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")


def _render(template: Path, destination: Path, name: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = template.read_text(encoding="utf-8").replace("{{name}}", name)
    destination.write_text(text, encoding="utf-8")
    shutil.copymode(template, destination)


def new_experiment(name: str, experiments_root: Path) -> List[Path]:
    """Write experiments/<name>/ from the template. Returns the files written."""
    if not NAME_RE.match(name):
        raise ScaffoldError(f"{name!r} is not a valid experiment name "
                            f"(lowercase letters, digits and underscores, starting with a letter)")
    target = experiments_root / name
    if target.exists():
        raise ScaffoldError(f"{target} already exists")
    if not roots.is_experiments_root(experiments_root):
        raise ScaffoldError(f"{experiments_root} is not an experiments root (no "
                            f"{roots.CORE_SCHEMA_REL}); run 'trials init' first")
    written = []
    source = SKELETON / "experiment"
    for template in _template_files(source):
        destination = target / template.relative_to(source)
        _render(template, destination, name)
        written.append(destination)
    for empty in ("bags", "runs", "results"):
        (target / empty).mkdir(exist_ok=True)
        (target / empty / ".gitkeep").touch()
    return written


def init_project(directory: Path) -> List[Path]:
    """Write a project: trials.yaml and experiments/common/config/. Returns the files written."""
    source = SKELETON / "project"
    written = []
    for template in _template_files(source):
        destination = directory / template.relative_to(source)
        if destination.exists():
            raise ScaffoldError(f"{destination} already exists")
        _render(template, destination, directory.name)
        written.append(destination)
    (directory / "experiments" / "common" / "preflight" / "checks").mkdir(parents=True, exist_ok=True)
    if not roots.is_experiments_root(directory / "experiments"):
        raise ScaffoldError(f"the package's skeleton is incomplete ({SKELETON}): the installation "
                            f"lacks its template files, so {directory} is not a valid project")
    return written


def main_new(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="trials new",
                                 description="a new experiment from the commented template")
    ap.add_argument("name", help="the experiment directory name (lowercase, digits, underscores)")
    roots.add_root_arguments(ap)
    args = ap.parse_args(argv)
    try:
        roots.resolve_root_arguments(args)
        written = new_experiment(args.name, args.experiments_root)
    except (ScaffoldError, roots.RootNotFound) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    rel = args.experiments_root / args.name
    print(f"Created {rel} from the template:")
    for path in written:
        print(f"  {path.relative_to(args.experiments_root.parent)}")
    print(f"""
Edit, in this order:
  1. config/factors.yaml                 the factors, their levels, and how the runner realizes each level
  2. config/trial_metadata_schema.yaml   the metadata contract: the fields every trial records, the coverage axes
  3. config/preflight_checklist.json     the T-0 gate: machine checks and operator confirmations
  4. config/runner.yaml                  the stack, one trial, the verify steps, the computers
  5. campaigns/baseline.yaml             the first campaign: the sweep, the fixed levels, the repetitions

Then check, plan and dry-run:
  trials lint
  trials plan experiments/{args.name}/campaigns/baseline.yaml
  trials run  experiments/{args.name}/campaigns/baseline.yaml --limit 1""")
    return 0


def main_init(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="trials init",
                                 description="a new project: trials.yaml and the shared configuration")
    ap.add_argument("directory", type=Path, nargs="?", default=Path("."),
                    help="the project root (default: the current directory)")
    ap.add_argument("--experiment", metavar="NAME", help="also create a first experiment")
    args = ap.parse_args(argv)
    directory = args.directory.expanduser().resolve()
    try:
        written = init_project(directory)
        if args.experiment:
            written += new_experiment(args.experiment, directory / "experiments")
    except ScaffoldError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Initialized {directory}:")
    for path in written:
        print(f"  {path.relative_to(directory)}")
    if not args.experiment:
        print(f"\nNext: cd {directory} && trials new <experiment_name>")
    return 0

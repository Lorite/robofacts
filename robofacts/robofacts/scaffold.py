"""``trials new <name>`` and ``trials init <dir>``: a new experiment from the
commented template or from an existing experiment (``--from``), and a new
project (an experiments root with its shared configuration). The templates
live in ``skeleton/`` next to this module."""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

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


def experiment_template(experiments_root: Optional[Path] = None) -> Path:
    """The experiment template in force: the one of the project's backend, else the core's."""
    from . import backend
    try:
        custom = backend.active(experiments_root).skeleton_dir
    except backend.BackendError:
        custom = None
    if custom is not None and (custom / "experiment").is_dir():
        return custom / "experiment"
    return SKELETON / "experiment"


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
    source = experiment_template(experiments_root)
    for template in _template_files(source):
        destination = target / template.relative_to(source)
        _render(template, destination, name)
        written.append(destination)
    for empty in ("bags", "runs", "results"):
        (target / empty).mkdir(exist_ok=True)
        (target / empty / ".gitkeep").touch()
    return written


#: Top-level directories of a source experiment that hold its recorded data or
#: its own history, never its declarations. ``--from`` copies none of them.
DATA_DIRS = ("bags", "runs", "results", "notebooks")
#: Larger files are data or assets, not declarations, and are reported instead of copied.
MAX_COPY_BYTES = 1_000_000
#: The files the framework reads by name, and the per-experiment metadata templates. With the
#: copied campaigns they are where a copy starts (2026-10-15). A file at the top level or under
#: scripts/ is copied only when one of them, or a file copied because of them, names it. A file
#: under config/ that none names is copied but listed for review, because a launch file of a
#: package outside the experiment may load it. An experiment also holds files that belong to
#: its own history (an AGENTS.md, a design note such as GATE_LOG_TODO.md, the scripts and
#: predictions of one study, a network configuration file for one host), and a copy that keeps them
#: describes the source.
DECLARATIONS = ("config/factors.yaml", "config/trial_metadata_schema.yaml",
                "config/preflight_checklist.json", "config/runner.yaml",
                "config/analyses.yaml", "config/trial_media.yaml", "RUNBOOK.md",
                "trial_metadata.template.json", "analysis_metadata.template.json")

README_DERIVED = """# {name}

Derived from [`{source}`](../{source}/README.md) by `trials new {name} --from {source}` on {date}. Every declaration below `config/`, `campaigns/` and `scripts/` started as a copy of that experiment, with its name replaced. Rewrite what describes the source experiment, then run `trials validate experiments/{name}`.

## Research question

## Hypotheses

## Factors and levels

## Protocol

## Analysis plan
"""


class CopyReport:
    """What ``new_experiment_from`` wrote, and what it left behind on purpose."""

    def __init__(self) -> None:
        self.written: List[Path] = []
        self.renamed: List[Tuple[str, str]] = []
        self.skipped: List[Tuple[str, str]] = []
        self.campaigns: List[str] = []
        #: Copied files that still name a renamed campaign as a word, to review by hand.
        self.campaign_mentions: List[str] = []
        #: Top-level and scripts/ files of the source that no declaration names (DECLARATIONS).
        self.left_out: List[str] = []
        #: config/ files that no declaration names: copied, to review and delete when unused.
        self.unreferenced_config: List[str] = []


def _name_pattern(name: str) -> "re.Pattern[str]":
    # Whole identifiers only, so a source named `foo` leaves `foo_v2` alone.
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")


def _is_data_dir(rel: Path) -> bool:
    top = rel.parts[0]
    return top in DATA_DIRS or top.startswith("bags_")


def _source_entries(source: Path) -> Iterable[Path]:
    for root, dirs, files in os.walk(source):
        here = Path(root)
        # Data directories are left out even when they are links to external storage.
        dirs[:] = sorted(d for d in dirs
                         if d not in ("__pycache__", ".ipynb_checkpoints", ".pytest_cache")
                         and not _is_data_dir((here / d).relative_to(source)))
        # Symbolic links to directories are copied as links, never walked into.
        for d in list(dirs):
            if (here / d).is_symlink():
                files.append(d)
                dirs.remove(d)
        for f in sorted(files):
            yield here / f


def _needs_a_reference(path: Path, rel: Path) -> bool:
    """True for the files a declaration should name: the top level (but not a dotfile such as
    .gitignore, or a link to a shared package), config/ and scripts/. Other directories
    (launch files, worlds, models) are copied whole, because their files name each other in
    ways a file name search does not see (``model://`` URIs, package-relative paths)."""
    if path.is_symlink():
        return False
    if len(rel.parts) == 1:
        return not rel.name.startswith(".")
    return rel.parts[0] in ("config", "scripts")


def _names(text: str, rel: Path) -> bool:
    """Does ``text`` name the file ``rel``: its file name, or for a Python module its import
    name. Whole names only, so ``plot.py`` is not named by ``replot.py``."""
    if _name_pattern(rel.name).search(text):
        return True
    return rel.suffix == ".py" and _name_pattern(rel.stem).search(text) is not None


def _referenced(entries: List[Path], source: Path, roots_rel: Iterable[Path]) -> set:
    """The files of ``entries`` reachable from the declarations: the roots, every file outside
    the referenced-only directories, and transitively every file a kept file names."""
    def text_of(path: Path) -> str:
        if path.is_symlink() or path.stat().st_size > MAX_COPY_BYTES:
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return ""

    roots_rel = set(roots_rel)
    kept = {p for p in entries
            if p.relative_to(source) in roots_rel or not _needs_a_reference(p, p.relative_to(source))}
    texts = {p: text_of(p) for p in kept}
    candidates = [p for p in entries if p not in kept]
    changed = True
    while changed:
        changed = False
        for path in list(candidates):
            rel = path.relative_to(source)
            if any(_names(text, rel) for other, text in texts.items() if other != path):
                kept.add(path)
                texts[path] = text_of(path)
                candidates.remove(path)
                changed = True
    return kept


def new_experiment_from(name: str, source_name: str, experiments_root: Path,
                        campaigns: Optional[List[str]] = None) -> CopyReport:
    """Write experiments/<name>/ as a renamed copy of the declarations of an existing
    experiment: config/, the campaigns (all, or the named ones), scripts/ and the other
    text files. Recorded data, notebooks, caches, large files and tests are not copied, and
    neither is a top-level or scripts/ file that no declaration names (see DECLARATIONS):
    the source's AGENTS.md, notes and study scripts stay with it.
    The README becomes a short derived-from stub, because the design is the one part a
    copy cannot carry over."""
    if not NAME_RE.match(name):
        raise ScaffoldError(f"{name!r} is not a valid experiment name "
                            f"(lowercase letters, digits and underscores, starting with a letter)")
    source = experiments_root / source_name
    target = experiments_root / name
    if not (source / "config").is_dir():
        known = sorted(p.parent.name for p in experiments_root.glob("*/config"))
        raise ScaffoldError(f"no experiment {source_name!r} under {experiments_root} "
                            f"(experiments: {', '.join(known) or 'none'})")
    if target.exists():
        raise ScaffoldError(f"{target} already exists")
    available = sorted(p.stem for p in (source / "campaigns").glob("*.yaml"))
    # Each entry is <campaign> or <campaign>:<new name>.
    renames = {}
    for entry in campaigns or []:
        old_name, _, new_name = entry.partition(":")
        old_name = old_name[:-5] if old_name.endswith(".yaml") else old_name
        if new_name and not NAME_RE.match(new_name):
            raise ScaffoldError(f"{new_name!r} is not a valid campaign name "
                                f"(lowercase letters, digits and underscores, starting with a letter)")
        renames[old_name] = new_name or old_name
    wanted = list(renames) if renames else available
    unknown = sorted(set(wanted) - set(available))
    if unknown:
        raise ScaffoldError(f"{source_name} has no campaign {', '.join(unknown)} "
                            f"(campaigns: {', '.join(available) or 'none'})")
    renamed_campaigns = {o: n for o, n in renames.items() if o != n}

    pattern = _name_pattern(source_name)
    report = CopyReport()
    report.campaigns = [renames.get(c, c) for c in wanted]
    entries = []
    for path in _source_entries(source):
        rel = path.relative_to(source)
        if rel.parts[0] == "campaigns" and len(rel.parts) == 2 and path.suffix == ".yaml" \
                and path.stem not in wanted:
            continue
        if rel == Path("README.md"):
            continue
        if path.name.startswith("test_") and path.suffix == ".py":
            # Two test modules with one basename break pytest's collection of the whole
            # tree, and the source's tests test the source's scripts anyway.
            report.skipped.append((str(rel), "a test of the source experiment, write this one's own"))
            continue
        if path.suffix in (".pyc", ".FCBak") or path.name == ".DS_Store":
            continue
        entries.append(path)
    declarations = [Path(d) for d in DECLARATIONS] + [Path("campaigns") / f"{c}.yaml" for c in wanted]
    referenced = _referenced(entries, source, declarations)
    for path in entries:
        rel = path.relative_to(source)
        if path not in referenced:
            if rel.parts[0] != "config":
                report.left_out.append(str(rel))
                continue
            report.unreferenced_config.append(str(rel))
        new_rel = Path(*(pattern.sub(name, part) for part in rel.parts))
        is_campaign = rel.parts[0] == "campaigns" and len(rel.parts) == 2
        if is_campaign and path.stem in renamed_campaigns:
            new_rel = Path("campaigns") / f"{renamed_campaigns[path.stem]}.yaml"
        destination = target / new_rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            os.symlink(os.readlink(path), destination)
            report.written.append(destination)
            continue
        if path.stat().st_size > MAX_COPY_BYTES:
            report.skipped.append((str(rel), f"{path.stat().st_size // 1000} kB, data or an asset"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(path, destination)
        else:
            replaced = pattern.sub(name, text)
            if replaced != text:
                report.renamed.append((str(new_rel), str(len(pattern.findall(text)))))
            for old_c, new_c in renamed_campaigns.items():
                # The file name everywhere, the name: key in the campaign itself. A bare word
                # elsewhere may be prose (a campaign named `baseline`), so it is reported.
                replaced = replaced.replace(f"{old_c}.yaml", f"{new_c}.yaml")
                if is_campaign and path.stem == old_c:
                    replaced = re.sub(rf"(?m)^name:\s*{re.escape(old_c)}\s*$", f"name: {new_c}", replaced)
                if _name_pattern(old_c).search(replaced):
                    report.campaign_mentions.append(f"{new_rel} (names {old_c})")
            destination.write_text(replaced, encoding="utf-8")
            shutil.copymode(path, destination)
        report.written.append(destination)

    readme = target / "README.md"
    readme.write_text(README_DERIVED.format(name=name, source=source_name,
                                            date=datetime.date.today().isoformat()),
                      encoding="utf-8")
    report.written.append(readme)
    for empty in ("bags", "runs", "results"):
        (target / empty).mkdir(exist_ok=True)
        (target / empty / ".gitkeep").touch()
    return report


def init_project(directory: Path, backend_name: Optional[str] = None) -> List[Path]:
    """Write a project: trials.yaml and experiments/common/config/. Returns the files written.

    ``backend_name`` picks the backend the project runs on (default: the one in force, see
    robofacts.backend). A backend may replace template files with its own."""
    from . import backend
    chosen = backend.load(backend_name or backend.configured_name(directory))
    source = SKELETON / "project"
    overlay = (chosen.skeleton_dir / "project") if chosen.skeleton_dir else None
    templates = {t.relative_to(source): t for t in _template_files(source)}
    if overlay is not None and overlay.is_dir():
        templates.update({t.relative_to(overlay): t for t in _template_files(overlay)})
    written = []
    for relative, template in sorted(templates.items()):
        destination = directory / relative
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
    ap = argparse.ArgumentParser(
        prog="trials new",
        description="a new experiment from the commented template, or from an existing "
                    "experiment with --from (the fast path when a similar one exists)")
    ap.add_argument("name", help="the experiment directory name (lowercase, digits, underscores)")
    ap.add_argument("--from", dest="source", metavar="EXPERIMENT",
                    help="copy the declarations of this existing experiment, renamed, instead "
                         "of the template (no bags, runs, results, notebooks or tests)")
    ap.add_argument("--campaign", action="append", default=[], metavar="NAME[:NEW]",
                    help="with --from: copy only this campaign, renamed to NEW when given "
                         "(repeatable; default: all, names unchanged)")
    roots.add_root_arguments(ap)
    args = ap.parse_args(argv)
    if args.campaign and not args.source:
        ap.error("--campaign needs --from")
    try:
        roots.resolve_root_arguments(args)
        if args.source:
            report = new_experiment_from(args.name, args.source, args.experiments_root,
                                         args.campaign)
            _print_copy_report(args, report)
            return 0
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

Then one command checks, plans, lints and dry-runs it:
  trials validate experiments/{args.name}

A similar experiment already exists? Start from it instead:
  trials new <name> --from <existing_experiment>""")
    return 0


def _print_copy_report(args, report: CopyReport) -> None:
    rel = lambda p: p.relative_to(args.experiments_root.parent)  # noqa: E731
    target = args.experiments_root / args.name
    print(f"Created {rel(target)} from {args.source} ({len(report.written)} files):")
    for path in report.written:
        print(f"  {rel(path)}")
    if report.renamed:
        print(f"\n{args.source} -> {args.name} replaced in:")
        for path, count in report.renamed:
            print(f"  {path} ({count}x)")
    if report.campaign_mentions:
        print("\nStill name a renamed campaign, review by hand:")
        for line in report.campaign_mentions:
            print(f"  {line}")
    if report.skipped:
        print("\nNot copied:")
        for path, why in report.skipped:
            print(f"  {path}: {why}")
    if report.left_out:
        source_rel = rel(args.experiments_root / args.source)
        print(f"\nLeft out, no declaration names them (the source's own notes and study files). "
              f"Copy one from {source_rel}/ when this experiment needs it:")
        for path in report.left_out:
            print(f"  {path}")
    if report.unreferenced_config:
        print("\nCopied, but no declaration names them. Delete each one this experiment does not "
              "use (a launch file of a package may load one, so check before you delete):")
        for path in report.unreferenced_config:
            print(f"  {path}")
    first = f"campaigns/{report.campaigns[0]}.yaml" if report.campaigns else "campaigns/<c>.yaml"
    print(f"""
Everything still describes {args.source}. Change, in this order, only what differs:
  1. config/factors.yaml, config/trial_metadata_schema.yaml   the factors and the contract
  2. config/runner.yaml                                       the launch, the action, the verify steps, the hosts
  3. config/preflight_checklist.json                          the T-0 gate
  4. {first:<52}  the claim, the sweep, the operator steps
  5. README.md (the design), RUNBOOK.md, scripts/             the prose and the trial scripts

Then one command answers "is it ready":
  trials validate experiments/{args.name}""")


def main_init(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="trials init",
                                 description="a new project: trials.yaml and the shared configuration")
    ap.add_argument("directory", type=Path, nargs="?", default=Path("."),
                    help="the project root (default: the current directory)")
    ap.add_argument("--experiment", metavar="NAME", help="also create a first experiment")
    ap.add_argument("--backend", metavar="NAME", default=None,
                    help="the backend the project runs on: process (built in), a name (the package "
                         "robofacts_<name>), or a module (default: $TRIALS_BACKEND, else the one of "
                         "the project above this directory, else process)")
    args = ap.parse_args(argv)
    directory = args.directory.expanduser().resolve()
    try:
        written = init_project(directory, args.backend)
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

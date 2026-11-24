"""Where a parameter's value is declared, and the edit that would keep a new one.

robofacts Explore never applies anything: a value worth keeping goes back into the
declarations as a reviewed change. This module finds the candidates for that change,
read-only, so the run page can say *where* instead of leaving the search to the
scientist (2026-10-17):

* the stack's **launch file**, a line that names the parameter. When the line takes
  a ``LaunchConfiguration('<arg>')``, the value comes from that launch argument: its
  ``DeclareLaunchArgument`` default, and every **factor** whose ``maps_to`` drives
  ``<arg>`` (with the level that already gives the new value, if one does). When the
  line holds the starting value as a literal, the candidate is that line with the
  new value.
* the **parameter files** (``config/*.yaml``) of the launch file's package, a
  ``<parameter>: <value>`` line in a section of the node (or ``/**``). When the
  launch file overrides the parameter, the YAML line is listed as overridden.

The result is a list of candidates, each ``{kind, file, line, text, suggestion,
note}``, and a Markdown block for ``proposed_changes.md``. A heuristic over source
text: it may miss a value computed in Python, and it says so when it finds nothing.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SKIP_DIRS = {"build", "install", "log", ".git", "node_modules", "__pycache__"}


def _same(a: Any, b: Any) -> bool:
    """Equal as values: 4 and 4.0 and "4.0" are the same number."""
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


def _text(value: Any) -> str:
    return json.dumps(value) if not isinstance(value, str) else value


@functools.lru_cache(maxsize=32)
def _package_dirs(project_root: str, package: str) -> tuple:
    """Source directories of the ROS package ``package`` in the project's colcon workspaces."""
    from robofacts.code_version import workspaces_to_probe
    found = []
    for ws in workspaces_to_probe():
        src = Path(project_root) / ws / "src"
        if not src.is_dir():
            continue
        for xml in src.rglob("package.xml"):
            if SKIP_DIRS & set(xml.relative_to(src).parts):
                continue
            try:
                text = xml.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"<name>\s*%s\s*</name>" % re.escape(package), text):
                found.append(str(xml.parent))
    return tuple(found)


def find_launch_file(project_root: Path, package: str, file_name: str) -> Optional[Path]:
    """The SOURCE of ``<package>/launch/<file_name>``, never an installed copy."""
    for d in _package_dirs(str(project_root), package):
        path = Path(d) / "launch" / file_name
        if path.is_file():
            return path
    return None


def _quoted(name: str) -> re.Pattern:
    return re.compile(r"""['"]%s['"]""" % re.escape(name))


def _declared_default(lines: List[str], arg: str) -> Optional[tuple]:
    """(line number, default text) of ``DeclareLaunchArgument('<arg>', default_value=...)``."""
    pattern = _quoted(arg)
    for i, line in enumerate(lines):
        if "DeclareLaunchArgument" in line or (i and "DeclareLaunchArgument" in lines[i - 1]):
            if not pattern.search(line):
                continue
            for j in range(i, min(i + 4, len(lines))):
                m = re.search(r"default_value\s*=\s*['\"]([^'\"]*)['\"]", lines[j])
                if m:
                    return j + 1, m.group(1)
            return i + 1, None
    return None


def _factors_driving(arg: str, factors: Dict[str, Any], new: Any) -> List[Dict[str, Any]]:
    out = []
    for name, f in factors.items():
        maps = getattr(f, "maps_to", {}) or {}
        if maps.get("kind") != "launch_arg":
            continue
        names = maps.get("names") or [maps.get("name")]
        level_values = maps.get("level_values") or {}
        drives = arg in names or any(isinstance(v, dict) and arg in v for v in level_values.values())
        if not drives:
            continue
        giving = []
        for level in f.levels:
            resolved = level_values.get(level, level)
            value = resolved.get(arg) if isinstance(resolved, dict) else resolved
            if value is not None and _same(value, new):
                giving.append(level)
        out.append({"factor": name, "levels": list(f.levels), "default": f.default,
                    "giving": giving, "mapped": bool(level_values)})
    return out


def _yaml_candidates(config_dir: Path, node: str, parameter: str, new: Any,
                     root: Path) -> List[Dict[str, Any]]:
    out = []
    base = node.rsplit("/", 1)[-1]
    line_re = re.compile(r"^(\s*)%s\s*:\s*(.+?)\s*$" % re.escape(parameter))
    for path in sorted(config_dir.glob("*.yaml")) if config_dir.is_dir() else []:
        section = ""
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            top = re.match(r"^([^\s#][^:]*):\s*$", line)
            if top:
                section = top.group(1).strip().strip("'\"")
                continue
            m = line_re.match(line)
            if not m:
                continue
            if section not in (base, "/" + base, node, "/**", "**"):
                continue
            old = m.group(2)
            out.append({"kind": "yaml", "file": str(path.relative_to(root)), "line": i + 1,
                        "text": line.strip(),
                        "suggestion": f"{m.group(1)}{parameter}: {_text(new)}".strip(),
                        "note": f"the parameter file of {base} (section {section})",
                        "old": old})
    return out


def propose(node: str, parameter: str, start: Any, new: Any, *, project_root: Path,
            launch_package: str, launch_file: str, factors: Dict[str, Any],
            condition: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The candidates for keeping ``new`` as the value of ``<node>.<parameter>``."""
    condition = condition or {}
    candidates: List[Dict[str, Any]] = []
    launch = find_launch_file(project_root, launch_package, launch_file)
    overridden = False
    if launch is not None:
        lines = launch.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = str(launch.relative_to(project_root))
        key = _quoted(parameter)
        for i, line in enumerate(lines):
            if not key.search(line):
                continue
            arg = re.search(r"LaunchConfiguration\(\s*['\"]([^'\"]+)['\"]", line)
            if arg:
                overridden = True
                arg_name = arg.group(1)
                declared = _declared_default(lines, arg_name)
                drivers = _factors_driving(arg_name, factors, new)
                note = f"the launch argument {arg_name} sets it"
                cand = {"kind": "launch_arg", "file": rel, "line": i + 1, "text": line.strip(),
                        "arg": arg_name, "note": note, "suggestion": "", "factors": drivers}
                candidates.append(cand)
                if declared is not None:
                    at, default = declared
                    candidates.append({
                        "kind": "launch_default", "file": rel, "line": at,
                        "text": lines[at - 1].strip(), "arg": arg_name,
                        "suggestion": (lines[at - 1].strip().replace(f"'{default}'", f"'{_text(new)}'", 1)
                                       if default is not None else ""),
                        "note": (f"the default of {arg_name}, used when no factor sets it"
                                 + (": a factor drives it, so a trial passes its level instead"
                                    if drivers else ""))})
                for d in drivers:
                    now = condition.get(d["factor"])
                    if d["giving"]:
                        hint = (f"level {d['giving'][0]!r} of factor {d['factor']} gives {_text(new)}"
                                f" (this exploration ran {d['factor']}={now}). Restart with that level "
                                f"(🔁 on the run page), or set it in the campaign")
                    else:
                        hint = (f"no level of factor {d['factor']} ({', '.join(map(str, d['levels']))}) "
                                f"gives {_text(new)}: add a level to factors.yaml"
                                + (" with its level_values" if d["mapped"] else "")
                                + ", and to a campaign's sweep or fixed block")
                    candidates.append({"kind": "factor", "file": "factors.yaml", "line": 0,
                                       "text": f"{d['factor']}: levels {d['levels']}, default {d['default']}",
                                       "factor": d["factor"], "suggestion": "", "note": hint})
                continue
            if start is not None:
                literal = _text(start)
                if re.search(r"(?<![\w.])%s(?![\w.])" % re.escape(literal), line):
                    candidates.append({
                        "kind": "literal", "file": rel, "line": i + 1, "text": line.strip(),
                        "suggestion": re.sub(r"(?<![\w.])%s(?![\w.])" % re.escape(literal),
                                             _text(new), line.strip(), count=1),
                        "note": "a literal in the launch file"})
        for c in _yaml_candidates(launch.parent.parent / "config", node, parameter, new,
                                  project_root):
            if overridden:
                c["note"] += ", overridden by the launch file above (changing it alone has no effect)"
            candidates.append(c)
    summary = (f"{len(candidates)} place(s) declare {node}.{parameter}" if candidates else
               f"no declaration of {node}.{parameter} found in "
               f"{launch_package}/{launch_file} or its config/*.yaml. The node may compute it, or "
               f"take its own default: look for declare_parameter('{parameter}', ...) in its source")
    return {"node": node, "parameter": parameter, "from": start, "to": new,
            "launch_file": str(launch.relative_to(project_root)) if launch else "",
            "candidates": candidates, "summary": summary}


def markdown(p: Dict[str, Any], stamp: str) -> str:
    """One block of proposed_changes.md."""
    out = [f"## {p['node']}.{p['parameter']}: {_text(p['from'])} -> {_text(p['to'])} ({stamp})", "",
           f"{p['summary']}. Nothing was applied: review the edit like any other change.", ""]
    for c in p["candidates"]:
        where = f"`{c['file']}:{c['line']}`" if c["line"] else f"`{c['file']}`"
        out.append(f"- **{c['kind']}** {where}: {c['note']}")
        out.append(f"  - now: `{c['text']}`")
        if c.get("suggestion") and c["suggestion"] != c["text"]:
            out.append(f"  - proposed: `{c['suggestion']}`")
    return "\n".join(out) + "\n\n"

"""The core package imports no middleware and names none. What belongs to ROS 2 lives in the
package robofacts_ros2, which depends on this one and never the reverse.

Before 2026-11-24 the framework was one ROS 2 package. Its design layer had no ROS in it,
but the runner, the gate and the tools named ROS in 130 lines, so "the framework is general"
was a claim that nothing checked. These tests are the check. When one fails, the fix is to
move the code or the sentence into the backend's package and to reach it through
robofacts.backend, not to extend the allowlist, unless the hit is the core saying how
another backend is named.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
CORE = PROJECT / "robofacts"
SUFFIXES = (".py", ".sh", ".md", ".html", ".js", ".css", ".yaml", ".json", ".toml", ".cfg", ".xml")

#: Python modules of ROS 2, of its message packages, and of the ROS 2 package of this
#: repository: the core imports none of them.
ROS_MODULES = re.compile(
    r"^(rclpy|rosbag2_py|rosidl_\w+|rcl_interfaces|rclcpp|launch|launch_ros|ament_\w+|"
    r"\w+_msgs|\w+_srvs|\w+_interfaces|tf2_\w+|tf2|ros2\w*|rosgraph_\w+|message_filters|"
    r"cv_bridge|robofacts_ros2)$")

#: Words that name ROS 2, its tools, its formats or its middleware.
ROS_WORDS = re.compile(
    r"\bros2\b|\bROS\b|ROS 2|rclpy|rosbag|rosidl|rosout|ROS_DOMAIN|\bament\b|colcon|/opt/ros|"
    r"\bmcap\b|\.mcap\b|\.db3\b|foxglove|\brmw\b|cyclone|\bDDS\b|robofacts_ros2|launch\.py|ros2_ws|"
    r"demo_nodes|package\.xml", re.IGNORECASE)
#: The same words must not hide in a different case, except these three, which are also
#: ordinary words in lower case.
CASE_SENSITIVE = re.compile(r"\bROS\b|\bDDS\b|ROS 2")

#: Core files allowed to name ROS, and why. Keep this list short.
ALLOWED: dict = {}


def core_files():
    for path in sorted(PROJECT.rglob("*")):
        if (path.is_file() and path.suffix in SUFFIXES and "__pycache__" not in path.parts
                and PROJECT / "test" not in path.parents
                and not any(part.endswith(".egg-info") or part in ("build", "dist") for part in path.parts)):
            yield path


def ros_hits(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for match in ROS_WORDS.finditer(line):
            word = match.group(0)
            if word.lower() in ("ros", "dds", "ros 2") and not CASE_SENSITIVE.fullmatch(word):
                continue
            yield f"{path.relative_to(PROJECT)}:{number}: {line.strip()[:110]}"
            break


def test_the_core_imports_no_ros_module():
    hits = []
    for path in core_files():
        if path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                     else [])
            for name in names:
                if ROS_MODULES.match(name.split(".")[0]):
                    hits.append(f"{path.relative_to(PROJECT)}:{node.lineno}: imports {name}")
    assert not hits, "the core imports a ROS module:\n" + "\n".join(hits)


def test_the_core_names_no_ros():
    hits = [hit for path in core_files()
            if str(path.relative_to(PROJECT)) not in ALLOWED
            for hit in ros_hits(path)]
    assert not hits, ("ROS words in the core (move the code or the sentence into the backend's "
                      "package):\n" + "\n".join(hits))


@pytest.mark.parametrize("allowed", sorted(ALLOWED) or ["<none>"])
def test_every_allowlisted_file_still_needs_it(allowed):
    """An allowlist entry whose file no longer names ROS is stale: remove it."""
    if allowed == "<none>":
        pytest.skip("the allowlist is empty")
    path = PROJECT / allowed
    assert path.is_file(), f"{allowed} does not exist any more"
    assert any(True for _ in ros_hits(path)), f"{allowed} names no ROS now: drop it"


def test_the_core_is_a_plain_python_project():
    """No ROS package manifest, no ament resource, and only Python dependencies."""
    assert not (PROJECT / "package.xml").exists(), "a package.xml makes the core a ROS package"
    assert not (PROJECT / "resource").exists()
    setup = (PROJECT / "setup.py").read_text(encoding="utf-8")
    assert "share/ament_index" not in setup and "data_files" not in setup


def test_every_module_of_the_core_imports_in_a_process_without_ros():
    """Import every module in a fresh interpreter whose path holds this package and the
    standard library's site-packages only, and report any ROS module that got loaded."""
    modules = sorted(
        ".".join(path.relative_to(PROJECT).with_suffix("").parts)
        for path in CORE.rglob("*.py")
        if "skeleton" not in path.parts and path.name != "__main__.py")
    modules = [m[:-len(".__init__")] if m.endswith(".__init__") else m for m in modules]
    code = (
        "import importlib, sys, re\n"
        f"sys.path.insert(0, {str(PROJECT)!r})\n"
        f"pattern = re.compile({ROS_MODULES.pattern!r})\n"
        f"for name in {modules!r}:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except ModuleNotFoundError as exc:\n"
        "        if exc.name != 'flask':   # the web interface's optional dependency\n"
        "            raise\n"
        "loaded = sorted(m for m in sys.modules if pattern.match(m.split('.')[0]))\n"
        "print('LOADED:' + ','.join(loaded))\n")
    # The lint module resolves an experiments root when it is imported: the example's.
    env = {**os.environ, "TRIALS_EXPERIMENTS_ROOT": str(PROJECT / "example_project" / "experiments"),
           "TRIALS_PROJECT_ROOT": str(PROJECT / "example_project")}
    env.pop("PYTHONPATH", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = out.stdout.strip().rsplit("LOADED:", 1)[-1]
    assert loaded == "", f"importing the core loaded ROS modules: {loaded}"

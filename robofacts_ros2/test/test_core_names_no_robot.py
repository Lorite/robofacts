"""The core of the package names no robot and no lab. Robot-specific code lives in
robofacts/adapters/, a lab's names live in its own project.

The extraction of 2026-10-14 moved the lab's names out of the code, but nothing stopped new
robot code from coming back into the core: within four weeks the package carried 18 topic
sets of one lab, its Spot and Crazyflie checks, and its process names (found 2026-11-09).
These tests are that missing guard. When one fails, the fix is to move the code into an
adapter or into the project, not to extend the allowlist, unless the hit is a fact about a
third-party tool that a generic word cannot say.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
CORE = PACKAGE / "robofacts"
ADAPTERS = CORE / "adapters"
SUFFIXES = (".py", ".sh", ".md", ".html", ".js", ".yaml", ".json")

#: Names of one lab, its people, its robots and its issue tracker: nowhere in the package.
LAB = re.compile(r"lorite(?!/robofacts)|crazylori|spot_BD_|helix|LORITE_|lab'?s #|issue #|\(#\d+|(?<![&\w])#\d{2,3}\b",
                 re.IGNORECASE)

#: Robot families and lab hardware that have (or would need) an adapter: not in the core.
ROBOT = re.compile(r"\bspot\b|spot_|crazyflie|crazyswarm|\bcf1\b|px4|livox|graph_?nav|aideck|"
                   r"ai deck|markerless|apriltag", re.IGNORECASE)

#: Core files allowed to name a robot, and why. Keep this list short.
ROBOT_ALLOWED = {
    # Vendor binaries that live outside /opt/ros and the workspaces: the clean slate must
    # kill them on any machine, and a pattern that matches nothing costs nothing.
    "tools/kill_ros.sh",
    # The lesson behind the check is a fact about PX4's own simulation start script.
    "preflight/checks/gz_world_matches.sh",
    # The check table names the adapters' checks, tagged with their adapter.
    "preflight/README.md",
    # The commented template of trials.yaml shows how to enable an adapter.
    "skeleton/project/trials.yaml",
}


def _core_files():
    for path in sorted(CORE.rglob("*")):
        if path.suffix in SUFFIXES and "__pycache__" not in path.parts and ADAPTERS not in path.parents:
            yield path


def _hits(path: Path, pattern: re.Pattern):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if pattern.search(line):
            yield f"{path.relative_to(PACKAGE)}:{number}: {line.strip()[:100]}"


def test_no_lab_name_anywhere_in_the_package():
    files = [*_core_files(), *sorted(ADAPTERS.rglob("*.py")), *sorted(ADAPTERS.rglob("*.sh")),
             PACKAGE.parent / "README.md", PACKAGE.parent / "CHANGELOG.md",
             *sorted((PACKAGE.parent / "robofacts_ros2" / "example_project").rglob("*.yaml")),
             *sorted((PACKAGE.parent / "robofacts_ros2" / "example_project").rglob("*.json"))]
    hits = [h for path in files if path.is_file() for h in _hits(path, LAB)]
    assert not hits, "lab names or internal issue numbers in the package:\n" + "\n".join(hits)


def test_the_core_names_no_robot():
    hits = [h for path in _core_files()
            if str(path.relative_to(CORE)) not in ROBOT_ALLOWED
            for h in _hits(path, ROBOT)]
    assert not hits, ("robot names in the core (move the code into robofacts/adapters/ or "
                      "the project):\n" + "\n".join(hits))


@pytest.mark.parametrize("allowed", sorted(ROBOT_ALLOWED))
def test_every_allowlisted_file_still_needs_it(allowed):
    """An allowlist entry whose file no longer names a robot is stale: remove it."""
    path = CORE / allowed
    assert path.is_file(), f"{allowed} does not exist any more"
    assert any(True for _ in _hits(path, ROBOT)), f"{allowed} names no robot now: drop it"

"""The two packages name no robot and no lab outside the robot adapters. Robot-specific code
lives in robofacts_ros2/adapters/, a lab's names live in its own project.

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

REPO = Path(__file__).resolve().parents[2]
#: The general package and the ROS 2 package. A robot belongs in neither, outside the adapters.
CORE = REPO / "robofacts" / "robofacts"
ROS2 = REPO / "robofacts_ros2" / "robofacts_ros2"
ADAPTERS = ROS2 / "adapters"
SUFFIXES = (".py", ".sh", ".md", ".html", ".js", ".yaml", ".json")

#: Names of one lab, its people, its robots and its issue tracker: nowhere in the package.
#: The author's name in its paper form (Lorite-Mora) is allowed: the README cites the paper.
LAB = re.compile(r"lorite(?!/robofacts|-mora\b)|crazylori|spot_BD_|helix|LORITE_|lab'?s #|issue #|\(#\d+|(?<![&\w])#\d{2,3}\b",
                 re.IGNORECASE)

#: Robot families and lab hardware that have (or would need) an adapter: not in the core.
ROBOT = re.compile(r"\bspot\b|spot_|crazyflie|crazyswarm|\bcf1\b|px4|livox|graph_?nav|aideck|"
                   r"ai deck|markerless|apriltag", re.IGNORECASE)

#: Core files allowed to name a robot, and why. Keep this list short.
ROBOT_ALLOWED = {
    # Vendor binaries that live outside /opt/ros and the workspaces: the clean slate must
    # kill them on any machine, and a pattern that matches nothing costs nothing.
    "robofacts_ros2/tools/kill_ros.sh",
    # The lesson behind the check is a fact about PX4's own simulation start script.
    "robofacts_ros2/checks/gz_world_matches.sh",
    # The check table names the adapters' checks, tagged with their adapter.
    "robofacts_ros2/checks/README.md",
    # The commented template of trials.yaml shows how to enable an adapter.
    "robofacts_ros2/skeleton/project/trials.yaml",
}


def _core_files():
    """Every file of the two packages outside the robot adapters."""
    for root in (CORE, ROS2):
        for path in sorted(root.rglob("*")):
            if path.suffix in SUFFIXES and "__pycache__" not in path.parts and ADAPTERS not in path.parents:
                yield path


def _key(path: Path) -> str:
    """``robofacts/<file>`` or ``robofacts_ros2/<file>``, the allowlist's spelling."""
    root = CORE if CORE in path.parents else ROS2
    return f"{root.name}/{path.relative_to(root)}"


def _hits(path: Path, pattern: re.Pattern):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if pattern.search(line):
            yield f"{path.relative_to(REPO)}:{number}: {line.strip()[:100]}"


def test_the_lab_pattern_allows_the_cited_author_and_still_catches_the_lab():
    assert not LAB.search("Alejandro Lorite-Mora. RoboFACTS: A Declarative Experiment Design Framework")
    assert not LAB.search("git clone https://github.com/Lorite/robofacts.git")
    for lab in ("lorite_ros2_humble_phd", "lorite_experiments", "LORITE_STOP", "a.lorite.mora", "Lorite-Moral"):
        assert LAB.search(lab), lab


def test_no_lab_name_anywhere_in_the_package():
    files = [*_core_files(), *sorted(ADAPTERS.rglob("*.py")), *sorted(ADAPTERS.rglob("*.sh")),
             REPO / "README.md", REPO / "CHANGELOG.md",
             *sorted((REPO / "robofacts_ros2" / "example_project").rglob("*.yaml")),
             *sorted((REPO / "robofacts_ros2" / "example_project").rglob("*.json"))]
    hits = [h for path in files if path.is_file() for h in _hits(path, LAB)]
    assert not hits, "lab names or internal issue numbers in the package:\n" + "\n".join(hits)


def test_the_core_names_no_robot():
    hits = [h for path in _core_files()
            if _key(path) not in ROBOT_ALLOWED
            for h in _hits(path, ROBOT)]
    assert not hits, ("robot names outside the adapters (move the code into robofacts_ros2/adapters/ or "
                      "the project):\n" + "\n".join(hits))


@pytest.mark.parametrize("allowed", sorted(ROBOT_ALLOWED))
def test_every_allowlisted_file_still_needs_it(allowed):
    """An allowlist entry whose file no longer names a robot is stale: remove it."""
    path = REPO / allowed.split("/", 1)[0] / allowed
    assert path.is_file(), f"{allowed} does not exist any more"
    assert any(True for _ in _hits(path, ROBOT)), f"{allowed} names no robot now: drop it"

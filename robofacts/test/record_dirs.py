"""The folder names of the project in force, for tests that build recordings by hand.

Many tests were written when the folders were always called ``bags``, ``bags_issues`` and so
on. The backend names them now (robofacts.backend.record_dirs): the process backend says
``recordings``, the ROS 2 backend keeps ``bags``. ``rdir("bags_issues")`` gives the name of
that folder for the backend in force when the test runs, so one helper serves the tests
on both backends.
"""
from __future__ import annotations

from robofacts import backend

ROLE = {"bags": "recordings", "bags_issues": "issues", "bags_slim": "slim",
        "bags_issues_slim": "issues_slim", "bags_scratch": "scratch",
        "bags_scratch_slim": "scratch_slim"}


def rdir(old_name: str, root=None) -> str:
    """The folder that was called ``old_name``, as the backend in force names it."""
    return backend.record_dirs(root)[ROLE[old_name]]

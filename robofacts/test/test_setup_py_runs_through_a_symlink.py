"""setup.py runs through a symlink, as `colcon build --symlink-install` runs it (2026-11-26).

colcon's symlink install runs ``build/robofacts/setup.py``, a symlink to this package's
setup.py, with ``build/robofacts`` as the working directory. setup.py read its README.md
next to ``os.path.abspath(__file__)``, which is the symlink's folder, so the lab's build
stopped with ``FileNotFoundError: .../build/robofacts/README.md``. The CI's colcon job builds
without --symlink-install and did not see it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[1] / "setup.py"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_setup_py_runs_through_a_symlink_from_another_folder(tmp_path):
    pytest.importorskip("setuptools")
    build = tmp_path / "build" / "robofacts"
    build.mkdir(parents=True)
    (build / "setup.py").symlink_to(SETUP)
    done = subprocess.run([sys.executable, "setup.py", "--name", "--version"], cwd=build,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.split()[:1] == ["robofacts"], done.stdout

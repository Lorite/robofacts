"""Test setup: import the package from this source tree when it is not installed,
and point the framework at the shipped example project unless the caller set a root."""
import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROJECT = PACKAGE_ROOT / "example_project"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
os.environ.setdefault("TRIALS_EXPERIMENTS_ROOT", str(EXAMPLE_PROJECT / "experiments"))
os.environ.setdefault("TRIALS_PROJECT_ROOT", str(EXAMPLE_PROJECT))

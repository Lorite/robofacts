"""The reference pages that tools/gen_docs_reference.py writes from the code are current.

docs/reference/commands.md, lint-rules.md and gate-checks.md list the commands with their
help texts, the lint rules and the gate checks of both packages. A change to a help text, a
rule or a check fails here until someone writes the pages again with the command that the
failure names. The test lives in the ROS 2 package because the pages also list what the ROS 2
backend adds, and CI runs this package's tests on ROS 2 Humble, whose Python writes the pages.
An installed package has no docs/ and no tools/, so the test skips there.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "tools" / "gen_docs_reference.py"
PAGES = REPO / "docs" / "reference"


def test_the_generated_reference_pages_match_the_code():
    if not GENERATOR.is_file() or not PAGES.is_dir():
        pytest.skip(f"no {GENERATOR.relative_to(REPO)} or no {PAGES.relative_to(REPO)}/ here "
                    f"(an installed package has neither)")
    result = subprocess.run([sys.executable, str(GENERATOR), "--check"], cwd=REPO,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, (
        f"the reference pages do not match the code. Write them again with\n"
        f"  python3 tools/gen_docs_reference.py\n"
        f"and commit the result.\n{result.stdout}{result.stderr}")

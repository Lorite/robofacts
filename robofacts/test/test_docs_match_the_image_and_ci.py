"""The commands and versions that the Dockerfile and the README state are the real ones
(2026-11-25).

``docker/Dockerfile`` told the reader to run ``pytest -q test`` in the quickstart image, but
its working directory ``/opt/robofacts`` is a copy of the repository and has no ``test/``
folder (and ``pytest`` is not on the image's PATH: Ubuntu's python3-pytest installs
``pytest-3``). The README said that ``core-no-ros`` runs on ``python:3.10-slim`` after
ci.yaml had grown a matrix of 3.10 to 3.14.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "docker" / "Dockerfile"
CI = REPO / ".github" / "workflows" / "ci.yaml"

pytestmark = pytest.mark.skipif(not (DOCKERFILE.is_file() and CI.is_file()),
                                reason="needs the repository checkout")


def test_the_test_command_of_the_image_names_folders_the_image_holds():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # The image's working directory is a copy of the whole repository.
    assert re.search(r"^WORKDIR /opt/robofacts$", text, re.M) and re.search(r"^COPY \. \.$", text, re.M)
    commands = re.findall(r"^#\s+docker run --rm trials-quickstart (\S.*?)\s*(?:#.*)?$", text, re.M)
    tests = [shlex.split(c) for c in commands if "pytest" in c]
    assert tests, commands
    for args in tests:
        assert args[:3] == ["python3", "-m", "pytest"], args
        folders = [a for a in args[3:] if not a.startswith("-")]
        assert folders and all((REPO / f).is_dir() for f in folders), folders


def test_the_readme_names_the_python_versions_of_the_core_job():
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    versions = [str(v) for v in ci["jobs"]["core-no-ros"]["strategy"]["matrix"]["python"]]
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    stated = re.search(r"`core-no-ros` installs only `robofacts` on [^(]*\(([^)]*)\)", readme)
    assert stated, "the README no longer describes the core-no-ros job"
    named = re.findall(r"python:(\d+\.\d+)-slim", stated.group(1))
    assert named and named[0] == versions[0] and named[-1] == versions[-1], (named, versions)

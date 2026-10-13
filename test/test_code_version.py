"""Tests for the code-version stamp (#96, 2026-08-04).

The parser is shared by the local and remote probes on purpose (see
``code_version._script``), so exercising it against real ``git status
--porcelain`` output covers the rig path too, which no test can reach.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from declarative_robot_trials import code_version as cv

# git exports GIT_DIR and friends to hooks (the pre-push hook runs this file). With them set,
# every git call below would act on the hook's repository instead of the temporary one.
_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
@pytest.fixture(autouse=True)
def _clean_cache():
    cv.reset_cache()
    yield
    cv.reset_cache()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo — the probe shells out to git, so faking it proves nothing."""
    def git(*args):
        subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=tmp_path, env=_ENV,
                       check=True, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "tracked.py")
    git("commit", "-q", "-m", "initial")
    return tmp_path


# --------------------------------------------------------------------------- #
# The probe against a real repo
# --------------------------------------------------------------------------- #
def test_a_clean_repo_reports_its_commit(repo):
    version = cv.probe(None, repo)
    assert len(version["commit"]) == 40
    assert version["branch"] == "main"
    assert version["dirty"] is False
    assert version["dirty_file_count"] == 0
    assert version["captured_on"] == "local"
    assert "error" not in version


def test_a_modified_tracked_file_makes_it_dirty(repo):
    (repo / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    version = cv.probe(None, repo)
    assert version["dirty"] is True
    assert version["dirty_files"] == ["tracked.py"]
    assert version["dirty_file_count"] == 1


def test_untracked_files_do_not_make_it_dirty(repo):
    """The whole point of the flag. This repo permanently carries untracked
    directories (lib/ SDK checkouts, results/), so counting them would pin
    `dirty` to True forever and the flag would carry no information at all."""
    (repo / "scratch_results").mkdir()
    (repo / "scratch_results" / "notes.txt").write_text("hi", encoding="utf-8")
    (repo / "untracked.py").write_text("y = 1\n", encoding="utf-8")
    version = cv.probe(None, repo)
    assert version["dirty"] is False, "untracked files must not count as dirty"
    assert version["dirty_files"] == []


def test_dirty_file_list_is_capped_but_the_count_is_exact(repo):
    for i in range(cv.MAX_DIRTY_FILES + 5):
        (repo / f"f{i:02d}.py").write_text("a = 0\n", encoding="utf-8")
    subprocess.run(["git", "-c", "safe.directory=*", "add", "."], cwd=repo, env=_ENV,
                   check=True, capture_output=True)
    subprocess.run(["git", "-c", "safe.directory=*", "-c", "user.email=t@e.com",
                    "-c", "user.name=t", "commit", "-q", "-m", "many"], cwd=repo, env=_ENV,
                   check=True, capture_output=True)
    for i in range(cv.MAX_DIRTY_FILES + 5):
        (repo / f"f{i:02d}.py").write_text("a = 1\n", encoding="utf-8")

    version = cv.probe(None, repo)
    assert len(version["dirty_files"]) == cv.MAX_DIRTY_FILES
    assert version["dirty_file_count"] == cv.MAX_DIRTY_FILES + 5


# --------------------------------------------------------------------------- #
# Failure paths — never fatal, and never a false claim of cleanliness
# --------------------------------------------------------------------------- #
def test_a_directory_that_is_not_a_repo_is_unknown_not_clean(tmp_path):
    version = cv.probe(None, tmp_path)
    assert version["commit"] is None
    assert version["dirty"] is None, "unknown must not read as verified-clean"
    assert "not a git repository" in version["error"]


def test_a_missing_workspace_is_reported(tmp_path):
    version = cv.probe(None, tmp_path / "nope")
    assert version["commit"] is None
    assert "not found" in version["error"]


def test_unknown_keeps_the_same_keys_as_a_success(repo, tmp_path):
    """A consumer reading version["dirty"] must not have to branch on whether
    the probe worked."""
    assert set(cv.probe(None, repo)) - {"error"} == set(cv.probe(None, tmp_path / "x")) - {"error"}


def test_a_timeout_is_survivable(monkeypatch, repo):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=20)
    monkeypatch.setattr(cv.subprocess, "run", boom)
    version = cv.probe(None, repo)
    assert version["commit"] is None and "timed out" in version["error"]


def test_an_exploding_probe_never_raises(monkeypatch, repo):
    monkeypatch.setattr(cv.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no sh")))
    version = cv.probe(None, repo)
    assert version["commit"] is None and "no sh" in version["error"]


# --------------------------------------------------------------------------- #
# Caching — one ssh round-trip per host, not one per trial
# --------------------------------------------------------------------------- #
def test_the_probe_runs_once_per_host_and_workspace(monkeypatch, repo):
    calls = []
    real = cv.probe

    def counting(target=None, workspace=None, timeout=cv.DEFAULT_TIMEOUT_S):
        calls.append(workspace)
        return real(target, workspace, timeout)

    monkeypatch.setattr(cv, "probe", counting)
    first = cv.cached(None, repo)
    second = cv.cached(None, repo)
    assert len(calls) == 1, "a 40-trial campaign must not pay 40 ssh round-trips"
    assert first == second


def test_the_cached_dict_cannot_be_mutated_through_a_caller(repo):
    """The runner writes this straight into trial metadata; a caller that
    mutated it would corrupt every later trial's stamp."""
    cv.cached(None, repo)["commit"] = "tampered"
    assert cv.cached(None, repo)["commit"] != "tampered"


# --------------------------------------------------------------------------- #
# Parsing (the remote path's only testable half)
# --------------------------------------------------------------------------- #
def test_parse_handles_porcelain_status_columns():
    out = ("commit=abc123\nbranch=main\n"
           "dirty= M experiments/common/scripts/trial_runner.py\n"
           "dirty=?? ignored-by-flag\n"
           "dirty= m ros2_ws/src/mobile-sensor\n")
    version = cv.parse(out, "remote:orin", "/workspaces/x")
    assert version["dirty"] is True
    assert "experiments/common/scripts/trial_runner.py" in version["dirty_files"]
    assert "ros2_ws/src/mobile-sensor" in version["dirty_files"], \
        "a submodule whose content moved is a real code difference"
    assert version["captured_on"] == "remote:orin"


def test_parse_reports_a_detached_head():
    version = cv.parse("commit=abc123\nbranch=HEAD\n", "local", "/x")
    assert version["commit"] == "abc123"
    assert version["dirty"] is False


def test_parse_without_a_commit_is_unknown():
    assert cv.parse("branch=main\n", "local", "/x")["commit"] is None


def test_summary_leads_with_the_dirty_warning():
    dirty = cv.parse("commit=abcdef1234\nbranch=main\ndirty= M a.py\n", "local", "/x")
    assert "uncommitted" in cv.summary(dirty)
    assert "abcdef12" in cv.summary(dirty)
    assert "unknown" in cv.summary(cv.unknown("nope", "local", "/x"))


def test_the_probe_answers_for_the_given_path_even_inside_a_git_hook(repo, tmp_path, monkeypatch):
    """git exports GIT_DIR to hooks. On 2026-10-14 the pre-push hook ran this suite from a
    worktree, every git call answered for that worktree, and the fixture committed into it."""
    other = tmp_path / "other"
    other.mkdir()
    for args in (["init", "-q", "-b", "other"], ["config", "user.email", "t@e.com"],
                 ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "other"]):
        subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=other, check=True,
                       capture_output=True, text=True, env=_ENV)
    other_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=other, capture_output=True,
                                text=True, env=_ENV).stdout.strip()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    version = cv.probe(None, repo)
    assert version["branch"] == "main"
    assert version["commit"] != other_head

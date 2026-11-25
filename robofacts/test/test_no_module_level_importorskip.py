"""No test module of the repository skips itself at module level.

A module-level ``pytest.importorskip`` (or ``pytest.skip(allow_module_level=True)``) can skip
far more than its own module: under the lab's pytest.ini and the launch_testing plugins of
the backend's container, one such call made a whole suite collect 0 tests with exit code 0
(the SDK of the MCP server, 2026-11-25, and the web interface's Flask, 2026-11-25). Every
test module guards an optional import and marks its tests with ``skipif`` instead. A call
inside a fixture or a test function is fine: it skips that test only.
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
REPOSITORY = PACKAGE.parent


def _test_modules():
    """The core's test modules, and those of the sibling packages and examples when the
    whole repository is checked out (an sdist holds only the core)."""
    dirs = [PACKAGE / "test", *sorted(REPOSITORY.glob("*/test")), *sorted(REPOSITORY.glob("examples/*/test"))]
    seen = set()
    for directory in dirs:
        for path in sorted(directory.glob("*.py")):
            if path.resolve() not in seen:
                seen.add(path.resolve())
                yield path


def _module_level_skips(tree: ast.Module):
    """Calls of pytest.importorskip or pytest.skip in statements that run at import time
    (not inside a def or a class body's methods)."""
    def walk(node, top):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if top and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest" \
                and node.func.attr in ("importorskip", "skip"):
            yield node
        for child in ast.iter_child_nodes(node):
            yield from walk(child, top)
    for statement in tree.body:
        yield from walk(statement, True)


def test_no_test_module_skips_itself_at_module_level():
    offenders = []
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _module_level_skips(tree):
            offenders.append(f"{path.relative_to(REPOSITORY)}:{call.lineno} pytest.{call.func.attr}")
    assert not offenders, (
        "a module-level skip can skip a whole suite with exit code 0; guard the import and "
        "use a skipif mark instead:\n" + "\n".join(offenders))


def test_the_check_finds_a_module_level_importorskip():
    tree = ast.parse("import pytest\npytest.importorskip('x')\n"
                     "def test_a():\n    pytest.importorskip('y')\n")
    assert [c.lineno for c in _module_level_skips(tree)] == [2]

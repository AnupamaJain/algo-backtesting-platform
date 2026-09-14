"""The architecture's central claim, enforced mechanically.

An engine that can reach live state -- a database, an HTTP client, the wall
clock -- behaves differently in a backtest than it did live. If that ever
happens, every research number the system has produced becomes suspect, and
nothing else in the test suite would notice.

So it is checked by walking the AST rather than by convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINES_DIR = Path(__file__).resolve().parent.parent / "src" / "vriddhix" / "engines"

#: Top-level packages an engine may never import.
FORBIDDEN_ROOTS = {
    "sqlalchemy", "alembic", "redis", "celery",
    "requests", "httpx", "urllib", "socket",
    "fastapi", "starlette", "flask",
    "yfinance",
}

#: Sibling packages that would let an engine reach stateful code.
FORBIDDEN_INTERNAL = {"db", "api", "jobs", "services", "data"}

#: Calls that make an engine depend on when it is run rather than on its input.
FORBIDDEN_CALLS = {"now", "today", "utcnow", "time"}


def engine_modules() -> list[Path]:
    return sorted(p for p in ENGINES_DIR.rglob("*.py"))


def test_there_are_engine_modules_to_check():
    """A purity test that silently checks nothing is worse than none."""
    assert engine_modules(), "no engine modules found -- has the path moved?"


@pytest.mark.parametrize("path", engine_modules(), ids=lambda p: p.name)
def test_engine_imports_nothing_stateful(path: Path):
    tree = ast.parse(path.read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in FORBIDDEN_ROOTS, (
                    f"{path.name} imports {alias.name}: engines must be pure"
                )

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            assert root not in FORBIDDEN_ROOTS, (
                f"{path.name} imports from {module}: engines must be pure"
            )

            # Relative imports: level 1 is inside engines/, level 2 is the
            # vriddhix package. Only domain, features and versioning are
            # reachable from there.
            if node.level and node.level >= 2:
                first = module.split(".")[0] if module else ""
                assert first not in FORBIDDEN_INTERNAL, (
                    f"{path.name} imports vriddhix.{module}: "
                    "engines may not reach stateful packages"
                )


@pytest.mark.parametrize("path", engine_modules(), ids=lambda p: p.name)
def test_engine_does_not_read_the_clock(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN_CALLS, (
                f"{path.name} calls {node.func.attr}(): an engine that knows the "
                "wall clock cannot be replayed faithfully in a backtest"
            )


@pytest.mark.parametrize("path", engine_modules(), ids=lambda p: p.name)
def test_engine_has_no_forward_shift(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "shift"
        ):
            for arg in node.args:
                if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                    pytest.fail(f"{path.name}: negative shift() is look-ahead")


@pytest.mark.parametrize("path", engine_modules(), ids=lambda p: p.name)
def test_engine_does_no_io(path: Path):
    tree = ast.parse(path.read_text())
    banned = {"open", "print", "input"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned, (
                f"{path.name} calls {node.func.id}(): engines perform no I/O"
            )

"""Condition-tree evaluation.

ONE evaluator, used by the custom scanner AND the backtester's entry/exit
rules. This is the architecture rule applied to user-defined logic: if the
screen builder and the backtester each had their own interpreter, a saved
strategy would mean two different things depending on which one ran it, and
the backtest would stop describing the screen.

Trees are JSON-serialisable so they round-trip through the database and the
API unchanged:

    {"op": "AND", "children": [
      {"field": "vcp_score", "cmp": "gt", "value": 80},
      {"op": "OR", "children": [...]}
    ]}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "between": lambda a, b: b[0] <= a <= b[1],
}

LOGICAL_OPS = {"AND", "OR", "NOT"}


class ConditionError(ValueError):
    """A malformed condition tree. Raised at parse time, not mid-scan."""


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The verdict plus why, so a screen can explain a row it returned."""

    passed: bool
    matched: tuple[str, ...]
    failed: tuple[str, ...]

    def describe(self) -> str:
        if self.passed:
            return f"matched: {', '.join(self.matched) or 'no conditions'}"
        return f"failed: {', '.join(self.failed)}"


def known_fields() -> frozenset[str]:
    """Every field a condition may reference.

    Taken from ``SetupRow`` rather than listed here, so the screen builder
    cannot drift from the row it filters. Imported lazily because the scanner
    imports this module.
    """
    from .scanner import SetupRow

    return frozenset(
        name for name in SetupRow.__dataclass_fields__ if name != "score_components"
    )


def validate(
    node: Mapping[str, Any], *, path: str = "root", check_fields: bool = True
) -> None:
    """Check a tree before it is stored or run.

    A screen that fails halfway through a 500-symbol scan has already written
    partial results; failing at save time costs nothing.

    ``check_fields`` also rejects field names that do not exist on a scanner
    row. Without it a typo saves cleanly and then matches nothing forever,
    which looks exactly like a screen that is working and finding nothing --
    the worst kind of silent failure this project exists to prevent.
    """
    if not isinstance(node, Mapping):
        raise ConditionError(f"{path}: condition must be an object")

    if "op" in node:
        op = node["op"]
        if op not in LOGICAL_OPS:
            raise ConditionError(f"{path}: unknown operator {op!r}")
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise ConditionError(f"{path}: {op} requires a non-empty children list")
        if op == "NOT" and len(children) != 1:
            raise ConditionError(f"{path}: NOT takes exactly one child")
        for i, child in enumerate(children):
            validate(child, path=f"{path}.{op}[{i}]", check_fields=check_fields)
        return

    if "field" not in node:
        raise ConditionError(f"{path}: leaf requires a 'field'")
    cmp = node.get("cmp")
    if cmp not in COMPARATORS:
        raise ConditionError(f"{path}: unknown comparator {cmp!r}")
    if "value" not in node:
        raise ConditionError(f"{path}: leaf requires a 'value'")
    if cmp == "between":
        value = node["value"]
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ConditionError(f"{path}: 'between' needs a two-element value")

    # Checked last: a leaf that is structurally broken should report that
    # first, since it is the more actionable message.
    if check_fields:
        allowed = known_fields()
        if node["field"] not in allowed:
            raise ConditionError(
                f"{path}: unknown field {node['field']!r}. "
                f"Known fields: {', '.join(sorted(allowed))}"
            )


def _leaf(node: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[bool, str]:
    field = node["field"]
    cmp = node["cmp"]
    expected = node["value"]
    actual = row.get(field)

    label = f"{field} {cmp} {expected}"

    # A missing field fails rather than raising: a symbol without an RS score
    # should drop out of an RS screen, not abort the whole scan.
    if actual is None:
        return False, f"{label} (no value)"

    try:
        return bool(COMPARATORS[cmp](actual, expected)), label
    except TypeError:
        # Comparing a string to a number, say. Treated as not-matching and
        # labelled, because silently excluding it would hide a broken screen.
        return False, f"{label} (incomparable: {actual!r})"


def evaluate(node: Mapping[str, Any], row: Mapping[str, Any]) -> Evaluation:
    """Evaluate a tree against one row of scanner/backtest fields."""
    matched: list[str] = []
    failed: list[str] = []

    def walk(current: Mapping[str, Any]) -> bool:
        if "op" in current:
            op = current["op"]
            children = current["children"]
            if op == "AND":
                # Deliberately not short-circuiting: the caller wants to know
                # every condition that failed, not just the first.
                return all([walk(c) for c in children])
            if op == "OR":
                return any([walk(c) for c in children])
            return not walk(children[0])

        ok, label = _leaf(current, row)
        (matched if ok else failed).append(label)
        return ok

    passed = walk(node)
    return Evaluation(passed=passed, matched=tuple(matched), failed=tuple(failed))


def fields_used(node: Mapping[str, Any]) -> set[str]:
    """Every field a tree references.

    Lets the scanner load only what a screen needs, and lets the API reject a
    screen that references a field the system does not produce.
    """
    if "op" in node:
        out: set[str] = set()
        for child in node["children"]:
            out |= fields_used(child)
        return out
    return {node["field"]}

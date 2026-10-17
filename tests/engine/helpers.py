"""Small helpers shared by the engine tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sheet.engine.sheet import Sheet

if TYPE_CHECKING:
    from sheet.engine.values import Value


def c(name: str) -> tuple[int, int]:
    """A1-style name -> (row, col)."""
    return int(name[1:]) - 1, ord(name[0]) - ord("A")


def put(s: Sheet, name: str, text: str) -> frozenset[tuple[int, int]]:
    return s.set_entry(*c(name), text)


def val(s: Sheet, name: str) -> Value:
    return s.get(*c(name))


def text(s: Sheet, name: str) -> str | None:
    cell = s.store.get_cell(*c(name))
    return None if cell is None else cell.text


def calc(formula: str, **cells: str) -> Value:
    """Value of ``formula`` (entered in A31) on a fresh one-thread sheet holding ``cells``."""
    s = Sheet(workers=1)
    for name, entry in cells.items():
        s.set_entry(*c(name), entry)
    s.set_entry(30, 0, formula)
    return s.get(30, 0)


def cell_state(s: Sheet) -> dict[tuple[int, int], tuple[str, Value, str]]:
    """Text, value and format of every stored cell, for before/after comparisons."""
    return {pos: (cell.text, cell.value, cell.format) for pos, cell in s.store.items()}


def full_state(s: Sheet):
    """``cell_state`` plus each formula's dependencies in the graph."""
    graph = {pos: s.graph.dependencies(pos) for pos, _ in s.store.items()}
    return cell_state(s), graph


def exact_state(s: Sheet):
    """Everything stored for each cell, including its parsed formula."""
    return {pos: (cell.text, cell.ast, cell.value, cell.format) for pos, cell in s.store.items()}

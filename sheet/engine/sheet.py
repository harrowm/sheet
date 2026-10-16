"""The engine facade: entry parsing, cell store, dependency graph, recalculation, history.

Every user action is one transaction. Cells are immutable once stored, so a transaction
is just the before and after cell for each position it changed. The dependency graph is
derived from the cells' ASTs, so restoring cells restores the graph, and rollback, undo
and redo are all the same operation: put these cells back.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from sheet.engine.cells import Cell, CellStore
from sheet.engine.entry import parse_entry
from sheet.engine.evaluator import evaluate
from sheet.engine.formats import GENERAL
from sheet.engine.graph import DependencyGraph, RecalcPlan, extract_references
from sheet.engine.values import Value

Pos = tuple[int, int]
DEFAULT_MAX_HISTORY = 100


@dataclass(frozen=True, slots=True)
class _Transaction:
    before: dict[Pos, Cell | None]
    after: dict[Pos, Cell | None]


def _same_value(a: Value, b: Value) -> bool:
    return type(a) is type(b) and a == b


def _same_cell(a: Cell | None, b: Cell | None) -> bool:
    if a is None or b is None:
        return a is b
    return (
        a.text == b.text
        and a.ast == b.ast
        and a.format == b.format
        and _same_value(a.value, b.value)
    )


class Sheet:
    def __init__(self, max_history: int = DEFAULT_MAX_HISTORY) -> None:
        self.store = CellStore()
        self.graph = DependencyGraph()
        self._undo: deque[_Transaction] = deque(maxlen=max_history)
        self._redo: list[_Transaction] = []

    # --- reading -----------------------------------------------------------------

    def get(self, row: int, col: int) -> Value:
        return self.store.get(row, col)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    # --- editing -----------------------------------------------------------------

    def set_entry(self, row: int, col: int, text: str) -> frozenset[Pos]:
        """Enter ``text`` in a cell. Returns every position whose cell changed.

        Raises FormulaSyntaxError, CycleError or IndexError, leaving the sheet unchanged.
        """
        return self.set_entries([(row, col, text)])

    def set_entries(self, edits: Iterable[tuple[int, int, str]]) -> frozenset[Pos]:
        """Enter several cells as one transaction (paste, fill, import).

        All-or-nothing: if any entry is invalid or the batch creates a cycle, nothing
        changes. For repeated positions the last entry wins.
        """
        changes: dict[Pos, Cell | None] = {}
        for row, col, text in edits:
            entry = parse_entry(text)  # raises before anything is touched
            pos = (row, col)
            old = changes[pos] if pos in changes else self.store.get_cell(row, col)
            fmt = old.format if old is not None else GENERAL
            if not entry.text.strip():
                changes[pos] = None if fmt == GENERAL else Cell("", None, None, fmt)
            else:
                changes[pos] = Cell.from_entry(entry, fmt)
        return self._commit(changes, recalculate=True)

    def set_format_range(self, r1: int, c1: int, r2: int, c2: int, fmt: str) -> frozenset[Pos]:
        """Apply a display format to a rectangle as one transaction."""
        return self._commit(self.store.formatted_cells(r1, c1, r2, c2, fmt), recalculate=False)

    # --- history -----------------------------------------------------------------

    def undo(self) -> frozenset[Pos]:
        if not self._undo:
            return frozenset()
        tx = self._undo.pop()
        self._put_all(tx.before)
        self._redo.append(tx)
        return frozenset(tx.before)

    def redo(self) -> frozenset[Pos]:
        if not self._redo:
            return frozenset()
        tx = self._redo.pop()
        self._put_all(tx.after)
        self._undo.append(tx)
        return frozenset(tx.after)

    # --- internals ---------------------------------------------------------------

    def _commit(self, changes: Mapping[Pos, Cell | None], recalculate: bool) -> frozenset[Pos]:
        before: dict[Pos, Cell | None] = {}

        def touch(pos: Pos) -> None:
            if pos not in before:
                before[pos] = self.store.get_cell(*pos)

        try:
            for pos in changes:
                touch(pos)  # also bounds-checks every position before the first write
            for pos, cell in changes.items():
                self._put(pos, cell)
            if recalculate:
                self._recalculate(self.graph.plan(list(changes)), touch)  # CycleError here
        except BaseException:
            self._put_all(before)
            raise

        after = {pos: self.store.get_cell(*pos) for pos in before}
        changed = {pos for pos in before if not _same_cell(before[pos], after[pos])}
        if not changed:
            return frozenset()
        self._undo.append(
            _Transaction({p: before[p] for p in changed}, {p: after[p] for p in changed})
        )
        self._redo.clear()
        return frozenset(changed)

    def _recalculate(self, plan: RecalcPlan, touch: Callable[[Pos], None]) -> None:
        for pos in plan.order:
            cell = self.store.get_cell(*pos)
            if cell is None or cell.ast is None:
                continue
            value = evaluate(cell.ast, self.store)
            if value is None:  # a formula that returns an empty cell displays as 0
                value = 0.0
            if not _same_value(value, cell.value):
                touch(pos)
                self.store.set_cell(*pos, replace(cell, value=value))

    def _put_all(self, states: dict[Pos, Cell | None]) -> None:
        for pos, cell in states.items():
            self._put(pos, cell)

    def _put(self, pos: Pos, cell: Cell | None) -> None:
        """Store a cell (or remove it) and bring its graph edges in line with its AST."""
        if cell is None:
            self.store.remove(*pos)
            self.graph.remove_cell(pos)
        elif cell.ast is None:
            self.store.set_cell(*pos, cell)
            self.graph.remove_cell(pos)
        else:
            self.store.set_cell(*pos, cell)
            cells, ranges = extract_references(cell.ast)
            self.graph.restore_dependencies(pos, cells, ranges)

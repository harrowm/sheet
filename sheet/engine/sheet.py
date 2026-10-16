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

from sheet.engine.ast import Node
from sheet.engine.cells import Cell, CellStore
from sheet.engine.entry import parse_entry
from sheet.engine.evaluator import evaluate
from sheet.engine.formats import GENERAL
from sheet.engine.graph import DependencyGraph, RecalcPlan, extract_references
from sheet.engine.parser import MAX_COLS, MAX_ROWS, parse
from sheet.engine.refs import Axis, delete_refs, insert_refs, render, shift_refs
from sheet.engine.values import ErrorValue, Value

Pos = tuple[int, int]
DEFAULT_MAX_HISTORY = 100


@dataclass(frozen=True, slots=True)
class Clipboard:
    """A snapshot of copied cells. Keys of ``cells`` are offsets from the top-left corner."""

    rows: int
    cols: int
    origin: Pos
    cells: dict[Pos, Cell]


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


def _rewritten(cell: Cell, rewrite: Callable[[Node], Node]) -> Cell:
    """The cell with its formula's references rewritten, or the same cell if none change.

    The text is regenerated from the *unfolded* parse (the stored AST has constants
    folded away), then compiled again, so the stored AST stays folded.
    """
    if cell.ast is None or rewrite(cell.ast) == cell.ast:
        return cell
    entry = parse_entry("=" + render(rewrite(parse(cell.text.strip()))))
    return Cell(entry.text, entry.ast, entry.value, cell.format)


def _cleared(old: Cell | None) -> Cell | None:
    """What remains of a cell after its contents are cleared: just its format."""
    return None if old is None or old.format == GENERAL else Cell("", None, None, old.format)


def _value_text(value: Value) -> str:
    """Entry text that reproduces ``value`` as a constant."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, ErrorValue):
        return f"={value}"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    entry = parse_entry(value)
    return value if entry.ast is None and entry.value == value else "'" + value


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

    # --- structural edits --------------------------------------------------------

    def insert_rows(self, at: int, count: int = 1) -> frozenset[Pos]:
        """Insert ``count`` empty rows before row index ``at``."""
        return self._structural(Axis.ROW, at, count, insert=True)

    def delete_rows(self, at: int, count: int = 1) -> frozenset[Pos]:
        return self._structural(Axis.ROW, at, count, insert=False)

    def insert_cols(self, at: int, count: int = 1) -> frozenset[Pos]:
        return self._structural(Axis.COL, at, count, insert=True)

    def delete_cols(self, at: int, count: int = 1) -> frozenset[Pos]:
        return self._structural(Axis.COL, at, count, insert=False)

    def _structural(self, axis: Axis, at: int, count: int, insert: bool) -> frozenset[Pos]:
        if count < 1:
            raise ValueError("count must be at least 1")
        if not 0 <= at < axis.limit:
            raise IndexError(f"{at} is outside the grid")
        band_end = at + count
        rewrite = (
            (lambda node: insert_refs(node, axis, at, count))
            if insert
            else (lambda node: delete_refs(node, axis, at, count))
        )
        final: dict[Pos, Cell | None] = {}
        vacated: set[Pos] = set()
        for pos, cell in self.store.items():
            i = pos[axis.value]
            if insert:
                new_i: int | None = i + count if i >= at else i
                if new_i is not None and new_i >= axis.limit:
                    raise ValueError("Insert would push cells off the edge of the grid")
            else:
                new_i = None if at <= i < band_end else i - count if i >= band_end else i
            if new_i is None:
                vacated.add(pos)
                continue
            new_pos = (new_i, pos[1]) if axis is Axis.ROW else (pos[0], new_i)
            new_cell = _rewritten(cell, rewrite)
            if new_pos != pos or new_cell is not cell:
                if new_pos != pos:
                    vacated.add(pos)
                final[new_pos] = new_cell
        changes: dict[Pos, Cell | None] = {p: None for p in vacated if p not in final}
        changes.update(final)
        return self._commit(changes, recalculate=True)

    # --- copy, paste, fill -------------------------------------------------------

    def copy(self, r1: int, c1: int, r2: int, c2: int) -> Clipboard:
        """Snapshot the rectangle (corners in any order)."""
        r1, r2, c1, c2 = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
        self.store.get_cell(r1, c1)  # bounds checks
        self.store.get_cell(r2, c2)
        cells = {
            (r - r1, c - c1): cell for (r, c), cell in self.store.cells_in(r1, c1, r2, c2).items()
        }
        return Clipboard(r2 - r1 + 1, c2 - c1 + 1, (r1, c1), cells)

    def paste(
        self, clip: Clipboard, row: int, col: int, values_only: bool = False
    ) -> frozenset[Pos]:
        """Paste at ``(row, col)``. Relative references shift by the paste offset.

        Empty cells in the copied area clear their targets. With ``values_only`` the
        calculated values are pasted as constants and target formats are kept.
        """
        return self._commit(self._paste_changes(clip, row, col, values_only), recalculate=True)

    def fill_down(self, r1: int, c1: int, r2: int, c2: int) -> frozenset[Pos]:
        """Copy the top row of the rectangle into the rows below it."""
        r1, r2, c1, c2 = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
        clip = self.copy(r1, c1, r1, c2)
        changes: dict[Pos, Cell | None] = {}
        for r in range(r1 + 1, r2 + 1):
            changes.update(self._paste_changes(clip, r, c1, False))
        return self._commit(changes, recalculate=True)

    def fill_right(self, r1: int, c1: int, r2: int, c2: int) -> frozenset[Pos]:
        """Copy the left column of the rectangle into the columns to its right."""
        r1, r2, c1, c2 = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
        clip = self.copy(r1, c1, r2, c1)
        changes: dict[Pos, Cell | None] = {}
        for c in range(c1 + 1, c2 + 1):
            changes.update(self._paste_changes(clip, r1, c, False))
        return self._commit(changes, recalculate=True)

    def _paste_changes(
        self, clip: Clipboard, row: int, col: int, values_only: bool
    ) -> dict[Pos, Cell | None]:
        if row < 0 or col < 0 or row + clip.rows > MAX_ROWS or col + clip.cols > MAX_COLS:
            raise ValueError("Paste would extend beyond the edge of the grid")
        d_row, d_col = row - clip.origin[0], col - clip.origin[1]
        changes: dict[Pos, Cell | None] = {}
        target = self.store.cells_in(row, col, row + clip.rows - 1, col + clip.cols - 1)
        for pos, existing in target.items():
            changes[pos] = _cleared(existing) if values_only else None
        for (dr, dc), src in clip.cells.items():
            pos = (row + dr, col + dc)
            if values_only:
                old = target.get(pos)
                fmt = old.format if old is not None else GENERAL
                entry = parse_entry(_value_text(src.value))
                changes[pos] = Cell.from_entry(entry, fmt) if entry.text.strip() else _cleared(old)
            else:
                changes[pos] = _rewritten(src, lambda node: shift_refs(node, d_row, d_col))
        return changes

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

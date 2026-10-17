"""The engine facade: entry parsing, cell store, dependency graph, recalculation, history.

Every user action is one transaction. Cells are immutable once stored, so a transaction
is just the before and after cell for each position it changed. The dependency graph is
derived from the cells' ASTs, so restoring cells restores the graph, and rollback, undo
and redo are all the same operation: put these cells back.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Self

from sheet.engine.ast import Literal, Node
from sheet.engine.cells import Cell, CellStore
from sheet.engine.compiler import fold_constants
from sheet.engine.entry import Entry, parse_entry
from sheet.engine.evaluator import evaluate
from sheet.engine.formats import GENERAL
from sheet.engine.graph import DependencyGraph, RecalcPlan, Rect, extract_references
from sheet.engine.limits import MAX_WRITE_CELLS
from sheet.engine.parser import MAX_COLS, MAX_ROWS, parse
from sheet.engine.recalc import DEFAULT_THRESHOLD, ParallelRecalculator
from sheet.engine.refs import Axis, Effects, delete_refs, insert_refs, render, shift_refs
from sheet.engine.values import ErrorValue, Value

Pos = tuple[int, int]
DEFAULT_MAX_HISTORY = 100
DEFAULT_MAX_HISTORY_CELLS = 1_000_000  # total changed cells the undo/redo history may hold
SNAPSHOT_MIN_READERS = 4  # a range read by fewer formulas is not worth packing


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


def _rewritten(
    cell: Cell, rewrite: Callable[[Node, Effects], Node], carry_value: bool = False
) -> tuple[Cell, bool]:
    """The cell with its formula's references rewritten, and whether its value must be redone.

    Returns the same cell if no reference changes. Otherwise the new text is regenerated from
    the *unfolded* parse of the old text (the stored AST has constants folded away) and the
    new stored AST is that tree folded again; each changed formula is parsed once. If the
    rewrite only shifted references (``Effects.value_may_change`` is false) and
    ``carry_value`` is set, the formula keeps its old value, so a move does not recalculate it.
    Pasting must not carry values: the pasted references point at different cells.
    """
    if cell.ast is None:
        return cell, False
    effects = Effects()
    if rewrite(cell.ast, effects) == cell.ast:
        return cell, False
    tree = rewrite(parse(cell.text.strip()), Effects())
    ast = fold_constants(tree)
    text = "=" + render(tree)
    if effects.value_may_change or cell.value is None or not carry_value:
        value = (
            ast.value if isinstance(ast, Literal) else None
        )  # a constant formula knows its value
        # Its value may differ from before even when already known (a folded #REF!), so whatever
        # reads this cell must be recalculated: it seeds the recalculation either way.
        return Cell(text, ast, value, cell.format), True
    return Cell(text, ast, cell.value, cell.format), False


def _check_write_size(cells: int) -> None:
    if cells > MAX_WRITE_CELLS:
        raise ValueError(
            f"Too many cells: this would write {cells:,}, and the limit is "
            f"{MAX_WRITE_CELLS:,} cells. Select a smaller area."
        )


def _contains_any(rect: Rect, positions: set[Pos]) -> bool:
    """Whether any position lies in the rectangle, checking whichever side is smaller."""
    area = (rect.r2 - rect.r1 + 1) * (rect.c2 - rect.c1 + 1)
    if area <= len(positions):
        return any(
            (r, c) in positions
            for r in range(rect.r1, rect.r2 + 1)
            for c in range(rect.c1, rect.c2 + 1)
        )
    return any(rect.r1 <= r <= rect.r2 and rect.c1 <= c <= rect.c2 for r, c in positions)


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
    def __init__(
        self,
        max_history: int = DEFAULT_MAX_HISTORY,
        workers: int | None = None,
        parallel_threshold: int = DEFAULT_THRESHOLD,
        max_history_cells: int = DEFAULT_MAX_HISTORY_CELLS,
    ) -> None:
        self.store = CellStore()
        self.graph = DependencyGraph()
        self._recalc = ParallelRecalculator(workers, parallel_threshold)
        self._undo: deque[_Transaction] = deque()
        self._max_history = max_history
        self._max_history_cells = max_history_cells
        self.last_undo_dropped = False  # the latest change was too big to keep in the history
        self._redo: list[_Transaction] = []

    def close(self) -> None:
        """Release the recalculation thread pool. The sheet stays usable (sequentially)."""
        self._recalc.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- reading -----------------------------------------------------------------

    def get(self, row: int, col: int) -> Value:
        return self.store.get(row, col)

    @property
    def history_cells(self) -> int:
        """Changed cells currently held by the undo and redo history."""
        return sum(len(tx.before) for tx in (*self._undo, *self._redo))

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

    def load_cells(self, items: Iterable[tuple[int, int, Entry, str]]) -> None:
        """Fill the sheet from saved cells ``(row, col, entry, format)`` in one transaction.

        Unlike typing, the saved format is used as is (a date is not re-detected), and the
        recalculation covers every cell. Callers normally follow with ``clear_history``.
        """
        changes: dict[Pos, Cell | None] = {}
        for row, col, entry, fmt in items:
            if entry.text.strip():
                changes[(row, col)] = Cell(entry.text, entry.ast, entry.value, fmt)
            else:
                changes[(row, col)] = None if fmt == GENERAL else Cell("", None, None, fmt)
        self._commit(changes, recalculate=True)

    def clear_history(self) -> None:
        self._undo.clear()
        self._redo.clear()

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

        def rewrite(node: Node, fx: Effects) -> Node:
            if insert:
                return insert_refs(node, axis, at, count, fx)
            return delete_refs(node, axis, at, count, fx)

        seeds: list[Pos] = []  # moved formulas whose value may now differ
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
            new_cell, redo = _rewritten(cell, rewrite, carry_value=True)
            if new_pos != pos or new_cell is not cell:
                if new_pos != pos:
                    vacated.add(pos)
                final[new_pos] = new_cell
            if redo:
                seeds.append(new_pos)
        changes: dict[Pos, Cell | None] = {p: None for p in vacated if p not in final}
        changes.update(final)
        # Moving cells and shifting references changes no value, so only the formulas that
        # lost a reference or had a range cut (and what depends on them) are recalculated.
        return self._commit(changes, recalculate=True, seeds=seeds)

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
        _check_write_size(len(clip.cells))
        return self._commit(self._paste_changes(clip, row, col, values_only), recalculate=True)

    def fill_down(self, r1: int, c1: int, r2: int, c2: int) -> frozenset[Pos]:
        """Copy the top row of the rectangle into the rows below it."""
        r1, r2, c1, c2 = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
        clip = self.copy(r1, c1, r1, c2)
        _check_write_size(max(len(clip.cells), 1) * (r2 - r1))
        changes: dict[Pos, Cell | None] = {}
        for r in range(r1 + 1, r2 + 1):
            changes.update(self._paste_changes(clip, r, c1, False))
        return self._commit(changes, recalculate=True)

    def fill_right(self, r1: int, c1: int, r2: int, c2: int) -> frozenset[Pos]:
        """Copy the left column of the rectangle into the columns to its right."""
        r1, r2, c1, c2 = min(r1, r2), max(r1, r2), min(c1, c2), max(c1, c2)
        clip = self.copy(r1, c1, r2, c1)
        _check_write_size(max(len(clip.cells), 1) * (c2 - c1))
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

        def shifted(node: Node, fx: Effects) -> Node:
            return shift_refs(node, d_row, d_col)

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
                changes[pos] = _rewritten(src, shifted)[0]
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

    def _commit(
        self,
        changes: Mapping[Pos, Cell | None],
        recalculate: bool,
        seeds: list[Pos] | None = None,
    ) -> frozenset[Pos]:
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
                plan = self.graph.plan(list(changes) if seeds is None else seeds)  # CycleError here
                self._recalculate(plan, touch)
        except BaseException:
            self._put_all(before)
            raise

        after = {pos: self.store.get_cell(*pos) for pos in before}
        changed = {pos for pos in before if not _same_cell(before[pos], after[pos])}
        self.last_undo_dropped = False
        if not changed:
            return frozenset()
        if len(changed) > self._max_history_cells:
            # Too big to remember. Older steps would be invalid across it, so drop them all.
            self.clear_history()
            self.last_undo_dropped = True
            return frozenset(changed)
        self._undo.append(
            _Transaction({p: before[p] for p in changed}, {p: after[p] for p in changed})
        )
        self._redo.clear()
        while len(self._undo) > self._max_history or (
            len(self._undo) > 1 and self.history_cells > self._max_history_cells
        ):
            self._undo.popleft()
        return frozenset(changed)

    def _recalculate(self, plan: RecalcPlan, touch: Callable[[Pos], None]) -> None:
        """Evaluate the planned cells, in parallel where the graph allows.

        Each task reads only cells that finished earlier in the plan and writes only its
        own cell, so no two tasks touch the same data.
        """

        def compute(pos: Pos) -> None:
            cell = self.store.get_cell(*pos)
            if cell is None or cell.ast is None:
                return
            value = evaluate(cell.ast, self.store)
            if value is None:  # a formula that returns an empty cell displays as 0
                value = 0.0
            if not _same_value(value, cell.value):
                touch(pos)
                self.store.set_cell(*pos, replace(cell, value=value))

        rects = self._shared_clean_ranges(plan) if len(plan.order) >= SNAPSHOT_MIN_READERS else []
        if rects:
            self.store.begin_snapshots(rects)
        try:
            self._recalc.run(plan, compute)
        finally:
            if rects:
                self.store.end_snapshots()

    def _shared_clean_ranges(self, plan: RecalcPlan) -> list[tuple[int, int, int, int]]:
        """Ranges worth snapshotting before a parallel run.

        A range qualifies if several planned formulas read it and none of the formulas
        about to be recalculated sits inside it, so its numbers cannot change mid-run.
        """
        readers: Counter[Rect] = Counter()
        formulas: set[Pos] = set()
        for pos in plan.order:
            cell = self.store.get_cell(*pos)
            if cell is not None and cell.ast is not None:
                formulas.add(pos)
                readers.update(self.graph.range_dependencies(pos))
        return [
            tuple(rect)  # type: ignore[misc]
            for rect, n in readers.items()
            if n >= SNAPSHOT_MIN_READERS and not _contains_any(rect, formulas)
        ]

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

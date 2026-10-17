"""Cells and the sparse cell store.

The store is a plain ``dict[(row, col)] -> Cell``: only non-empty cells exist, and reads
and writes are O(1). Cells are treated as immutable once stored (updates replace the
object), so readers never see a half-written cell.
"""

from __future__ import annotations

import sys
from array import array
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from sheet.engine.formats import GENERAL, validate_format
from sheet.engine.parser import MAX_COLS, MAX_ROWS
from sheet.engine.values import ErrorValue, Value

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sheet.engine.ast import Node
    from sheet.engine.entry import Entry

MAX_FORMAT_CELLS = 1_000_000  # largest area a format change may materialise


@dataclass(slots=True)
class Cell:
    text: str
    ast: Node | None
    value: Value
    format: str = GENERAL
    # How far the formula reaches, packed by ``refs.reach``; -1 until first needed. Not an
    # init argument, so ``replace`` and every constructor start it afresh for the new AST.
    reach_cache: int = field(default=-1, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.format = sys.intern(self.format)

    @property
    def error(self) -> ErrorValue | None:
        return self.value if isinstance(self.value, ErrorValue) else None

    @classmethod
    def from_entry(cls, entry: Entry, current_format: str = GENERAL) -> Cell:
        """Build a cell from a parsed entry, keeping a non-General format already set."""
        fmt = entry.format if entry.format and current_format == GENERAL else current_format
        return cls(entry.text, entry.ast, entry.value, fmt)


@dataclass(frozen=True, slots=True)
class RangeSnapshot:
    """The numbers in a rectangle, packed into a compact array, plus its first error.

    Reading a range cell by cell touches shared ``Cell`` and float objects, and under
    free-threaded Python every thread that does so updates the same reference counts, so
    such reads stop scaling with the number of threads. A snapshot is built once on one
    thread; each reader then copies from a flat array and creates its own float objects.
    """

    numbers: array[float]
    error: ErrorValue | None


class CellStore:
    """Sparse cell storage. Implements the evaluator's ``CellReader`` protocol."""

    def __init__(self) -> None:
        self._cells: dict[tuple[int, int], Cell] = {}
        self._snapshots: dict[tuple[int, int, int, int], RangeSnapshot] = {}

    def __len__(self) -> int:
        return len(self._cells)

    def items(self) -> list[tuple[tuple[int, int], Cell]]:
        return list(self._cells.items())

    @staticmethod
    def _check(row: int, col: int) -> None:
        if not (0 <= row < MAX_ROWS and 0 <= col < MAX_COLS):
            raise IndexError(f"Cell ({row}, {col}) is outside the grid")

    def get_cell(self, row: int, col: int) -> Cell | None:
        self._check(row, col)
        return self._cells.get((row, col))

    def get(self, row: int, col: int) -> Value:
        cell = self.get_cell(row, col)
        return None if cell is None else cell.value

    def set_cell(self, row: int, col: int, cell: Cell) -> None:
        self._check(row, col)
        self._cells[(row, col)] = cell

    def remove(self, row: int, col: int) -> Cell | None:
        self._check(row, col)
        return self._cells.pop((row, col), None)

    def _cells_in(self, r1: int, c1: int, r2: int, c2: int) -> list[tuple[tuple[int, int], Cell]]:
        """Stored cells in the inclusive rectangle, walking the cheaper of the two ways.

        Small rectangles are walked by coordinate and huge ones (``A1:XFD1048576``) by
        scanning the stored cells, so the cost is bounded by the number of cells stored.
        """
        area = (r2 - r1 + 1) * (c2 - c1 + 1)
        cells = self._cells
        if area <= len(cells):
            return [
                ((r, c), cell)
                for r in range(r1, r2 + 1)
                for c in range(c1, c2 + 1)
                if (cell := cells.get((r, c))) is not None
            ]
        return [
            ((r, c), cell)
            for (r, c), cell in list(cells.items())
            if r1 <= r <= r2 and c1 <= c <= c2
        ]

    def cells_in(self, r1: int, c1: int, r2: int, c2: int) -> dict[tuple[int, int], Cell]:
        return dict(self._cells_in(r1, c1, r2, c2))

    def range_values(self, r1: int, c1: int, r2: int, c2: int) -> list[Value]:
        """Values of populated cells in the inclusive rectangle (empty values skipped)."""
        return [c.value for _, c in self._cells_in(r1, c1, r2, c2) if c.value is not None]

    def begin_snapshots(self, rects: Iterable[tuple[int, int, int, int]]) -> None:
        """Snapshot the numbers in each ``(r1, c1, r2, c2)`` rectangle for fast shared reads.

        Only valid while no cell in those rectangles changes; call ``end_snapshots`` when
        the phase that needed them is over.
        """
        built: dict[tuple[int, int, int, int], RangeSnapshot] = {}
        for rect in rects:
            numbers = array("d")
            error: ErrorValue | None = None
            for _, cell in self._cells_in(*rect):
                v = cell.value
                if isinstance(v, float):
                    numbers.append(v)
                elif error is None and isinstance(v, ErrorValue):
                    error = v
            built[rect] = RangeSnapshot(numbers, error)
        self._snapshots = built

    def end_snapshots(self) -> None:
        self._snapshots = {}

    def range_snapshot(self, r1: int, c1: int, r2: int, c2: int) -> RangeSnapshot | None:
        snapshots = self._snapshots
        return snapshots.get((r1, c1, r2, c2)) if snapshots else None

    def used_range(self) -> tuple[int, int, int, int] | None:
        """(min_row, min_col, max_row, max_col) of stored cells, or None if empty."""
        keys = list(self._cells)
        if not keys:
            return None
        rows = [r for r, _ in keys]
        cols = [c for _, c in keys]
        return min(rows), min(cols), max(rows), max(cols)

    def formatted_cells(
        self, r1: int, c1: int, r2: int, c2: int, fmt: str
    ) -> dict[tuple[int, int], Cell | None]:
        """The cells that would result from applying ``fmt`` to the rectangle.

        Creates empty cells where none exist. An empty cell left with the General format
        is the same as no cell, so it comes back as ``None`` (remove). Nothing is stored;
        raises FormatError for an invalid format and ValueError if the rectangle is larger
        than ``MAX_FORMAT_CELLS``.
        """
        self._check(r1, c1)
        self._check(r2, c2)
        validate_format(fmt)
        if (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_FORMAT_CELLS:
            raise ValueError(f"Range too large to format (limit {MAX_FORMAT_CELLS} cells)")
        out: dict[tuple[int, int], Cell | None] = {}
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                old = self._cells.get((r, c))
                new = Cell("", None, None, fmt) if old is None else replace(old, format=fmt)
                blank = not new.text.strip() and new.value is None
                out[(r, c)] = None if blank and fmt == GENERAL else new
        return out

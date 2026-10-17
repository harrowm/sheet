"""Cell dependency graph (a DAG) with localised Kahn ordering and cycle detection.

Both directions are kept as adjacency lists:
  * dependencies: the cells/ranges a formula reads (what to pull when recalculating);
  * dependents: the formula cells that read a cell (what to recalculate when it changes).

A range such as ``A1:A1000000`` is one node (a ``Rect``), not a million edges. Finding
the ranges that cover a changed cell uses a per-column bucket index for narrow ranges
and a short list for very wide ones.

Kahn's algorithm is run only on the subgraph downstream of the changed cells. It yields
the evaluation order, the in-degree map and the successor lists that the parallel
recalculation needs, and it detects cycles: any node left unprocessed is part of, or
downstream of, a cycle.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from sheet.engine.ast import BinaryOp, CellRef, FuncCall, Node, RangeRef, UnaryOp

Cell = tuple[int, int]  # (row, col), 0-based
WIDE_RANGE_COLUMNS = 64  # ranges wider than this are not bucketed per column


class Rect(NamedTuple):
    """Inclusive rectangle of cells: rows r1..r2, columns c1..c2 (0-based)."""

    r1: int
    c1: int
    r2: int
    c2: int


class CycleError(ValueError):
    """An edit would create a circular reference."""

    def __init__(self, cells: Iterable[Cell]) -> None:
        self.cells = frozenset(cells)
        super().__init__(f"Circular reference involving {len(self.cells)} cell(s)")


@dataclass(frozen=True, slots=True)
class RecalcPlan:
    order: list[Cell]  # topological evaluation order
    in_degree: dict[Cell, int]  # in-edges from inside the subgraph, for dispatch counters
    successors: dict[Cell, list[Cell]]  # edges inside the subgraph


class _RowIntervals:
    """The row spans of the ranges covering one column: "which of them contain row r?".

    Scanning every range for every changed cell is quadratic in a sheet full of running-total
    formulas (a bulk edit of 20,000 inputs with 8,000 ranges took 5.7 s). Instead the ranges
    are grouped by height class (class ``k`` holds heights in ``(2**(k-1), 2**k]``) and kept
    sorted by first row. A range of class ``k`` that contains row ``r`` must start within
    ``2**k - 1`` rows above it, so each class needs one binary search and then looks only at
    the ranges starting in that small window.
    """

    __slots__ = ("_classes",)

    def __init__(self) -> None:
        self._classes: dict[int, tuple[list[int], list[Rect]]] = {}

    def __bool__(self) -> bool:
        return bool(self._classes)

    @staticmethod
    def _class_of(rect: Rect) -> int:
        return (rect.r2 - rect.r1).bit_length()  # height 1 -> 0, 2 -> 1, 3-4 -> 2, 5-8 -> 3, ...

    def add(self, rect: Rect) -> None:
        starts, rects = self._classes.setdefault(self._class_of(rect), ([], []))
        i = bisect_right(starts, rect.r1)
        starts.insert(i, rect.r1)
        rects.insert(i, rect)

    def remove(self, rect: Rect) -> None:
        k = self._class_of(rect)
        starts, rects = self._classes[k]
        i = rects.index(rect, bisect_left(starts, rect.r1), bisect_right(starts, rect.r1))
        del starts[i]
        del rects[i]
        if not starts:
            del self._classes[k]

    def stab(self, row: int) -> list[Rect]:
        """The ranges whose rows include ``row``."""
        found: list[Rect] = []
        for k, (starts, rects) in self._classes.items():
            lo = bisect_left(starts, row - (1 << k) + 1)
            for i in range(lo, bisect_right(starts, row)):
                if rects[i].r2 >= row:
                    found.append(rects[i])
        return found


def extract_references(node: Node) -> tuple[set[Cell], set[Rect]]:
    """Cells and ranges read by an AST."""
    cells: set[Cell] = set()
    ranges: set[Rect] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, CellRef):
            cells.add((n.row, n.col))
        elif isinstance(n, RangeRef):
            ranges.add(Rect(n.start.row, n.start.col, n.end.row, n.end.col))
        elif isinstance(n, UnaryOp):
            stack.append(n.operand)
        elif isinstance(n, BinaryOp):
            stack.extend((n.left, n.right))
        elif isinstance(n, FuncCall):
            stack.extend(n.args)
    return cells, ranges


class DependencyGraph:
    def __init__(self) -> None:
        self._cell_deps: dict[Cell, frozenset[Cell]] = {}
        self._range_deps: dict[Cell, frozenset[Rect]] = {}
        self._cell_dependents: dict[Cell, set[Cell]] = {}
        self._range_dependents: dict[Rect, set[Cell]] = {}
        self._range_cols: dict[int, _RowIntervals] = {}  # column -> its narrow ranges
        self._wide_ranges: set[Rect] = set()

    # --- queries ---------------------------------------------------------------

    def dependencies(self, cell: Cell) -> tuple[set[Cell], set[Rect]]:
        return set(self._cell_deps.get(cell, ())), set(self._range_deps.get(cell, ()))

    def range_dependencies(self, cell: Cell) -> frozenset[Rect]:
        """The ranges ``cell`` reads (no copy)."""
        return self._range_deps.get(cell, frozenset())

    def dependents(self, cell: Cell) -> set[Cell]:
        """Formula cells that read ``cell``, directly or through a range."""
        out = set(self._cell_dependents.get(cell, ()))
        row, col = cell
        column = self._range_cols.get(col)
        if column is not None:
            for rect in column.stab(row):
                out |= self._range_dependents[rect]
        for rect in self._wide_ranges:
            if rect.r1 <= row <= rect.r2 and rect.c1 <= col <= rect.c2:
                out |= self._range_dependents[rect]
        return out

    def restore_dependencies(
        self, cell: Cell, cells: Iterable[Cell], ranges: Iterable[Rect]
    ) -> None:
        """Replace what ``cell`` reads. There is no cycle check here.

        ``Sheet`` applies a whole batch of these, runs ``plan`` once (which finds any cycle),
        and puts the old edges back if it fails, so the check-and-restore lives there. The
        same call restores a state already known to be acyclic (undo, redo, rollback).
        """
        self._uninstall(cell)
        self._install(cell, frozenset(cells), frozenset(ranges))

    def remove_cell(self, cell: Cell) -> None:
        """Drop what ``cell`` reads. Edges from cells that read it are kept."""
        self._uninstall(cell)

    def _install(self, cell: Cell, cells: frozenset[Cell], ranges: frozenset[Rect]) -> None:
        if cells:
            self._cell_deps[cell] = cells
            for dep in cells:
                self._cell_dependents.setdefault(dep, set()).add(cell)
        if ranges:
            self._range_deps[cell] = ranges
            for rect in ranges:
                if rect not in self._range_dependents:
                    self._range_dependents[rect] = set()
                    self._index_range(rect)
                self._range_dependents[rect].add(cell)

    def _uninstall(self, cell: Cell) -> None:
        for dep in self._cell_deps.pop(cell, ()):
            readers = self._cell_dependents[dep]
            readers.discard(cell)
            if not readers:
                del self._cell_dependents[dep]
        for rect in self._range_deps.pop(cell, ()):
            readers = self._range_dependents[rect]
            readers.discard(cell)
            if not readers:
                del self._range_dependents[rect]
                self._unindex_range(rect)

    def _index_range(self, rect: Rect) -> None:
        if rect.c2 - rect.c1 + 1 > WIDE_RANGE_COLUMNS:
            self._wide_ranges.add(rect)
        else:
            for col in range(rect.c1, rect.c2 + 1):
                self._range_cols.setdefault(col, _RowIntervals()).add(rect)

    def _unindex_range(self, rect: Rect) -> None:
        if rect in self._wide_ranges:
            self._wide_ranges.discard(rect)
            return
        for col in range(rect.c1, rect.c2 + 1):
            bucket = self._range_cols[col]
            bucket.remove(rect)
            if not bucket:
                del self._range_cols[col]

    # --- localised Kahn ----------------------------------------------------------

    def plan(self, changed: Iterable[Cell]) -> RecalcPlan:
        """Order the cells downstream of ``changed`` (inclusive) for recalculation.

        Raises CycleError if the subgraph contains a cycle. Successor lists are sorted,
        so the plan is deterministic.
        """
        seeds = list(dict.fromkeys(changed))
        successors: dict[Cell, list[Cell]] = {}
        stack = list(seeds)
        seen = set(seeds)
        while stack:  # iterative walk: chains can be far deeper than the recursion limit
            node = stack.pop()
            succ = sorted(self.dependents(node))
            successors[node] = succ
            for nxt in succ:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)

        in_degree = dict.fromkeys(successors, 0)
        for succ in successors.values():
            for nxt in succ:
                in_degree[nxt] += 1

        remaining = dict(in_degree)
        ready = deque(n for n in seeds if remaining[n] == 0)
        order: list[Cell] = []
        while ready:
            node = ready.popleft()
            order.append(node)
            for nxt in successors[node]:
                remaining[nxt] -= 1
                if remaining[nxt] == 0:
                    ready.append(nxt)
        if len(order) != len(in_degree):
            raise CycleError(n for n, d in remaining.items() if d > 0)
        return RecalcPlan(order, in_degree, successors)

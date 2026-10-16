"""Rendering ASTs back to text and rewriting cell references.

Used by structural edits: copy/paste/fill shift relative references, and inserting or
deleting rows and columns re-point every reference. A reference that no longer has a
target becomes the ``#REF!`` error literal.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from enum import Enum

from sheet.engine.ast import (
    BinaryOp,
    CellRef,
    FuncCall,
    Literal,
    Node,
    RangeRef,
    UnaryOp,
    normalised_range,
)
from sheet.engine.parser import MAX_COLS, MAX_ROWS
from sheet.engine.values import ErrorKind, ErrorValue

_REF_ERROR = Literal(ErrorValue(ErrorKind.REF))


class Axis(Enum):
    ROW = 0
    COL = 1

    @property
    def limit(self) -> int:
        return MAX_ROWS if self is Axis.ROW else MAX_COLS


# --- rendering ---------------------------------------------------------------------------

_BINARY_LEVEL = {"=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1, "&": 2, "+": 3, "-": 3}
_BINARY_LEVEL |= {"*": 4, "/": 4, "^": 5}
_PREFIX_LEVEL, _POSTFIX_LEVEL, _PRIMARY_LEVEL = 6, 7, 8


def render(node: Node) -> str:
    """Formula text for an AST (without the leading ``=``) using minimal parentheses.

    ``parse("=" + render(node)) == node`` for any tree the parser can produce.
    """
    return _render(node)[0]


def _wrap(node: Node, min_level: int) -> str:
    text, level = _render(node)
    return text if level >= min_level else f"({text})"


def _render(node: Node) -> tuple[str, int]:
    match node:
        case Literal(value):
            return _literal(value)
        case CellRef():
            return _cell(node), _PRIMARY_LEVEL
        case RangeRef(start, end):
            return f"{_cell(start)}:{_cell(end)}", _PRIMARY_LEVEL
        case UnaryOp("%", operand):
            return _wrap(operand, _POSTFIX_LEVEL) + "%", _POSTFIX_LEVEL
        case UnaryOp(op, operand):
            return op + _wrap(operand, _PREFIX_LEVEL), _PREFIX_LEVEL
        case BinaryOp("^", left, right):  # right-associative; unary binds tighter on the left
            return f"{_wrap(left, _PREFIX_LEVEL)}^{_wrap(right, 5)}", 5
        case BinaryOp(op, left, right):  # the rest are left-associative
            level = _BINARY_LEVEL[op]
            return f"{_wrap(left, level)}{op}{_wrap(right, level + 1)}", level
        case FuncCall(name, args):
            return f"{name}({','.join(render(a) for a in args)})", _PRIMARY_LEVEL
    raise TypeError(f"Unknown AST node: {node!r}")


def _literal(value: float | str | bool | ErrorValue) -> tuple[str, int]:
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), _PRIMARY_LEVEL
    if isinstance(value, ErrorValue):
        return str(value), _PRIMARY_LEVEL
    if isinstance(value, str):
        return '"' + value.replace('"', '""') + '"', _PRIMARY_LEVEL
    if not math.isfinite(value):
        raise ValueError(f"Cannot render non-finite number {value!r}")
    text = str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    return text, _PREFIX_LEVEL if text.startswith("-") else _PRIMARY_LEVEL


def _cell(ref: CellRef) -> str:
    letters = ""
    n = ref.col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"{'$' if ref.abs_col else ''}{letters}{'$' if ref.abs_row else ''}{ref.row + 1}"


# --- rewriting ---------------------------------------------------------------------------


def map_refs(
    node: Node,
    on_ref: Callable[[CellRef], CellRef | None],
    on_range: Callable[[RangeRef], RangeRef | None],
) -> Node:
    """Rebuild ``node`` with every reference rewritten; ``None`` becomes ``#REF!``."""
    match node:
        case CellRef():
            return on_ref(node) or _REF_ERROR
        case RangeRef():
            return on_range(node) or _REF_ERROR
        case UnaryOp(op, operand):
            return UnaryOp(op, map_refs(operand, on_ref, on_range))
        case BinaryOp(op, left, right):
            return BinaryOp(op, map_refs(left, on_ref, on_range), map_refs(right, on_ref, on_range))
        case FuncCall(name, args):
            return FuncCall(name, tuple(map_refs(a, on_ref, on_range) for a in args))
    return node  # Literal


def _in_grid(row: int, col: int) -> bool:
    return 0 <= row < MAX_ROWS and 0 <= col < MAX_COLS


def shift_refs(node: Node, d_row: int, d_col: int) -> Node:
    """Move relative references (not ``$`` parts) by an offset, as copy/paste does."""

    def ref(r: CellRef) -> CellRef | None:
        row = r.row if r.abs_row else r.row + d_row
        col = r.col if r.abs_col else r.col + d_col
        return replace(r, row=row, col=col) if _in_grid(row, col) else None

    def rng(r: RangeRef) -> RangeRef | None:
        start, end = ref(r.start), ref(r.end)
        return None if start is None or end is None else normalised_range(start, end)

    return map_refs(node, ref, rng)


def _on_axis(ref: CellRef, axis: Axis, index: int) -> CellRef:
    return replace(ref, row=index) if axis is Axis.ROW else replace(ref, col=index)


def _index(ref: CellRef, axis: Axis) -> int:
    return ref.row if axis is Axis.ROW else ref.col


def insert_refs(node: Node, axis: Axis, at: int, count: int) -> Node:
    """Re-point references after ``count`` rows/columns are inserted before index ``at``.

    A range spanning the insertion point grows; one that ends just before it does not.
    """

    def move(i: int) -> int:
        return i + count if i >= at else i

    def ref(r: CellRef) -> CellRef | None:
        i = move(_index(r, axis))
        return _on_axis(r, axis, i) if i < axis.limit else None

    def rng(r: RangeRef) -> RangeRef | None:
        start, end = ref(r.start), ref(r.end)
        return None if start is None or end is None else RangeRef(start, end)

    return map_refs(node, ref, rng)


def delete_refs(node: Node, axis: Axis, at: int, count: int) -> Node:
    """Re-point references after rows/columns ``at .. at+count-1`` are deleted.

    References into the deleted band become ``#REF!``; ranges shrink, and one that is
    entirely inside the band becomes ``#REF!``.
    """
    band_end = at + count

    def ref(r: CellRef) -> CellRef | None:
        i = _index(r, axis)
        if i < at:
            return r
        return None if i < band_end else _on_axis(r, axis, i - count)

    def rng(r: RangeRef) -> RangeRef | None:
        s, e = _index(r.start, axis), _index(r.end, axis)
        new_s = s if s < at else at if s < band_end else s - count
        new_e = e if e < at else at - 1 if e < band_end else e - count
        if new_s > new_e:
            return None
        return RangeRef(_on_axis(r.start, axis, new_s), _on_axis(r.end, axis, new_e))

    return map_refs(node, ref, rng)

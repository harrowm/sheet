"""Immutable AST nodes produced by the parser."""

from __future__ import annotations

from dataclasses import dataclass

from sheet.engine.values import ErrorValue


@dataclass(frozen=True, slots=True)
class Literal:
    value: float | str | bool | ErrorValue


@dataclass(frozen=True, slots=True)
class CellRef:
    row: int  # 0-based
    col: int  # 0-based
    abs_row: bool
    abs_col: bool


@dataclass(frozen=True, slots=True)
class RangeRef:
    start: CellRef  # top-left
    end: CellRef  # bottom-right


def normalised_range(a: CellRef, b: CellRef) -> RangeRef:
    """Range between two corners, with ``start`` top-left and ``end`` bottom-right.

    Each ``$`` flag stays with its coordinate when the corners are reordered.
    """
    top, bottom = sorted((a, b), key=lambda c: c.row)
    left, right = sorted((a, b), key=lambda c: c.col)
    return RangeRef(
        CellRef(top.row, left.col, top.abs_row, left.abs_col),
        CellRef(bottom.row, right.col, bottom.abs_row, right.abs_col),
    )


@dataclass(frozen=True, slots=True)
class UnaryOp:
    op: str  # "-", "+" or "%" (postfix)
    operand: Node


@dataclass(frozen=True, slots=True)
class BinaryOp:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class FuncCall:
    name: str  # upper case
    args: tuple[Node, ...]


Node = Literal | CellRef | RangeRef | UnaryOp | BinaryOp | FuncCall

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

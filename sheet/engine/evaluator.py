"""Evaluate a parsed AST against a cell reader. Never re-parses text."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from sheet.engine.ast import BinaryOp, CellRef, FuncCall, Literal, Node, RangeRef, UnaryOp
from sheet.engine.values import ErrorKind, ErrorValue, Value


class RangeSnapshotLike(Protocol):
    @property
    def numbers(self) -> Sequence[float]: ...

    @property
    def error(self) -> ErrorValue | None: ...


class CellReader(Protocol):
    def get(self, row: int, col: int) -> Value: ...

    def range_snapshot(self, r1: int, c1: int, r2: int, c2: int) -> RangeSnapshotLike | None:
        """Pre-packed numbers for a rectangle, if the reader has them (else None)."""
        ...

    def range_values(self, r1: int, c1: int, r2: int, c2: int) -> Iterable[Value]:
        """Values of the *populated* cells in the inclusive rectangle."""
        ...


class _NoCells:
    """Reader with no cells, used when folding constants."""

    def get(self, row: int, col: int) -> Value:
        return None

    def range_values(self, r1: int, c1: int, r2: int, c2: int) -> Iterable[Value]:
        return ()

    def range_snapshot(self, r1: int, c1: int, r2: int, c2: int) -> RangeSnapshotLike | None:
        return None


NO_CELLS: CellReader = _NoCells()

_NUMERIC_TEXT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)
_VALUE = ErrorValue(ErrorKind.VALUE)
_DIV0 = ErrorValue(ErrorKind.DIV0)
_NUM = ErrorValue(ErrorKind.NUM)
_NAME = ErrorValue(ErrorKind.NAME)


def evaluate(node: Node, reader: CellReader) -> Value:
    match node:
        case Literal(value):
            return value
        case CellRef(row, col):
            return reader.get(row, col)
        case RangeRef():
            return _VALUE  # a bare range is only valid as a function argument
        case UnaryOp(op, operand):
            return _unary(op, evaluate(operand, reader))
        case BinaryOp(op, left, right):
            return _binary(op, evaluate(left, reader), evaluate(right, reader))
        case FuncCall(name, args):
            return _call(name, args, reader)
    raise TypeError(f"Unknown AST node: {node!r}")


# --- coercion ---------------------------------------------------------------


def to_number(v: Value) -> float | ErrorValue:
    if isinstance(v, ErrorValue):
        return v
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, float):
        return v
    text = v.strip()
    if _NUMERIC_TEXT.fullmatch(text) and math.isfinite(number := float(text)):
        return number
    return _VALUE


def to_text(v: Value) -> str | ErrorValue:
    if isinstance(v, ErrorValue):
        return v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return f"{v:.15g}"
    return v


# --- operators --------------------------------------------------------------


def _unary(op: str, v: Value) -> Value:
    n = to_number(v)
    if isinstance(n, ErrorValue):
        return n
    if op == "-":
        return 0.0 - n  # not -n: that would turn 0 into -0
    if op == "%":
        return n / 100.0 + 0.0
    return n  # unary plus


def _binary(op: str, a: Value, b: Value) -> Value:
    if isinstance(a, ErrorValue):
        return a
    if isinstance(b, ErrorValue):
        return b
    if op == "&":
        return to_text(a) + to_text(b)  # type: ignore[operator]
    if op in _COMPARE:
        return _COMPARE[op](_compare(a, b))
    x, y = to_number(a), to_number(b)
    if isinstance(x, ErrorValue):
        return x
    if isinstance(y, ErrorValue):
        return y
    return _arith(op, x, y)


def _arith(op: str, x: float, y: float) -> Value:
    try:
        match op:
            case "+":
                return _finite(x + y)
            case "-":
                return _finite(x - y)
            case "*":
                return _finite(x * y)
            case "/":
                return _DIV0 if y == 0 else _finite(x / y)
            case "^":
                if x == 0 and y < 0:
                    return _DIV0
                result = x**y
                return _NUM if isinstance(result, complex) else _finite(result)
    except OverflowError:
        return _NUM
    raise ValueError(f"Unknown operator {op!r}")


def _finite(x: float) -> Value:
    return x + 0.0 if math.isfinite(x) else _NUM  # + 0.0 turns -0.0 into 0.0


def _rank(v: Value) -> int:
    return 2 if isinstance(v, bool) else 1 if isinstance(v, str) else 0


def _compare(a: Value, b: Value) -> int:
    """Three-way compare using Excel ordering: numbers < text < booleans."""
    if a is None and b is None:
        return 0
    if a is None:
        a = _blank_like(b)
    elif b is None:
        b = _blank_like(a)
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.casefold(), b.casefold()
    return (a > b) - (a < b)  # type: ignore[operator]


def _blank_like(other: Value) -> Value:
    return "" if isinstance(other, str) else False if isinstance(other, bool) else 0.0


_COMPARE: dict[str, Callable[[int], bool]] = {
    "=": lambda c: c == 0,
    "<>": lambda c: c != 0,
    "<": lambda c: c < 0,
    ">": lambda c: c > 0,
    "<=": lambda c: c <= 0,
    ">=": lambda c: c >= 0,
}

# --- functions --------------------------------------------------------------


def _numbers(args: tuple[Node, ...], reader: CellReader) -> list[float] | ErrorValue:
    """Collect numeric arguments for the aggregate functions.

    Range and single-cell arguments ignore empty, text and boolean cells; literal
    arguments are coerced strictly. The first error encountered is returned.
    """
    out: list[float] = []
    for arg in args:
        if isinstance(arg, RangeRef):
            rect = (arg.start.row, arg.start.col, arg.end.row, arg.end.col)
            snap = reader.range_snapshot(*rect)
            if snap is not None:
                if snap.error is not None:
                    return snap.error
                out.extend(snap.numbers)
                continue
            values: Iterable[Value] = reader.range_values(*rect)
        elif isinstance(arg, CellRef):
            values = (reader.get(arg.row, arg.col),)
        else:
            n = to_number(evaluate(arg, reader))
            if isinstance(n, ErrorValue):
                return n
            out.append(n)
            continue
        for v in values:
            if isinstance(v, ErrorValue):
                return v
            if isinstance(v, float):
                out.append(v)
    return out


def _count(args: tuple[Node, ...], reader: CellReader) -> Value:
    total = 0
    for arg in args:
        if isinstance(arg, RangeRef):
            rect = (arg.start.row, arg.start.col, arg.end.row, arg.end.col)
            snap = reader.range_snapshot(*rect)
            if snap is not None:
                total += len(snap.numbers)
                continue
            values: Iterable[Value] = reader.range_values(*rect)
        else:
            values = (evaluate(arg, reader),)
        total += sum(1 for v in values if isinstance(v, float))
    return float(total)


def _aggregate(
    fn: Callable[[list[float]], Value],
) -> Callable[[tuple[Node, ...], CellReader], Value]:
    def run(args: tuple[Node, ...], reader: CellReader) -> Value:
        nums = _numbers(args, reader)
        if isinstance(nums, ErrorValue):
            return nums
        try:
            return fn(nums)
        except OverflowError, ValueError:  # math.fsum: the exact total does not fit a float
            return _NUM

    return run


_FUNCTIONS: dict[str, Callable[[tuple[Node, ...], CellReader], Value]] = {
    "SUM": _aggregate(lambda n: _finite(math.fsum(n))),
    "AVERAGE": _aggregate(lambda n: _finite(math.fsum(n) / len(n)) if n else _DIV0),
    "MAX": _aggregate(lambda n: max(n) if n else 0.0),
    "MIN": _aggregate(lambda n: min(n) if n else 0.0),
    "COUNT": _count,
}


def _call(name: str, args: tuple[Node, ...], reader: CellReader) -> Value:
    fn = _FUNCTIONS.get(name)
    return _NAME if fn is None else fn(args, reader)

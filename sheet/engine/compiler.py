"""Entry-time compilation: parse once, then pre-calculate constant sub-expressions."""

from __future__ import annotations

from sheet.engine.ast import BinaryOp, FuncCall, Literal, Node, UnaryOp
from sheet.engine.evaluator import NO_CELLS, evaluate
from sheet.engine.parser import parse


def fold_constants(node: Node) -> Node:
    """Replace every sub-tree made only of literals with a ``Literal`` of its value."""
    match node:
        case UnaryOp(op, operand):
            node = UnaryOp(op, fold_constants(operand))
            foldable = isinstance(node.operand, Literal)
        case BinaryOp(op, left, right):
            node = BinaryOp(op, fold_constants(left), fold_constants(right))
            foldable = isinstance(node.left, Literal) and isinstance(node.right, Literal)
        case FuncCall(name, args):
            node = FuncCall(name, tuple(fold_constants(a) for a in args))
            foldable = all(isinstance(a, Literal) for a in node.args)
        case _:
            return node  # Literal, CellRef, RangeRef
    if not foldable:
        return node
    value = evaluate(node, NO_CELLS)
    if value is None:  # constant expressions never produce an empty value
        raise AssertionError("a constant expression evaluated to an empty value")
    return Literal(value)


def compile_formula(text: str) -> Node:
    """Parse formula text (starting with ``=``) once and fold its constants."""
    return fold_constants(parse(text))

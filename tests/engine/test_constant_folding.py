from sheet.engine.ast import BinaryOp, CellRef, Literal
from sheet.engine.compiler import compile_formula
from sheet.engine.values import ErrorKind, ErrorValue


def test_constant_expression_folds_to_literal():
    assert compile_formula("=1/2") == Literal(0.5)


def test_nested_constants_fold():
    assert compile_formula("=(1+2)*3-1") == Literal(8.0)
    assert compile_formula('="a"&"b"') == Literal("ab")


def test_partial_fold_keeps_references():
    node = compile_formula("=A1+(2*3)")
    assert node == BinaryOp("+", CellRef(0, 0, False, False), Literal(6.0))


def test_constant_error_folds_to_error_literal():
    assert compile_formula("=1/0") == Literal(ErrorValue(ErrorKind.DIV0))


def test_folding_does_not_touch_function_of_references():
    node = compile_formula("=SUM(A1:A3)")
    assert not isinstance(node, Literal)

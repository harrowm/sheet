import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sheet.engine.ast import (
    BinaryOp,
    CellRef,
    FuncCall,
    Literal,
    UnaryOp,
    normalised_range,
)
from sheet.engine.compiler import compile_formula
from sheet.engine.evaluator import evaluate
from sheet.engine.lexer import FormulaSyntaxError, TokenKind, tokenize
from sheet.engine.parser import MAX_COLS, MAX_ROWS, parse
from sheet.engine.refs import Axis, delete_refs, insert_refs, render, shift_refs
from sheet.engine.values import ErrorKind, ErrorValue

REF_ERR = Literal(ErrorValue(ErrorKind.REF))


def rt(formula: str) -> str:
    """Render the parse of a formula, without the leading '='."""
    return render(parse(formula))


def shifted(formula: str, dr: int, dc: int) -> str:
    return render(shift_refs(parse(formula), dr, dc))


def inserted(formula: str, axis: Axis, at: int, count: int = 1) -> str:
    return render(insert_refs(parse(formula), axis, at, count))


def deleted(formula: str, axis: Axis, at: int, count: int = 1) -> str:
    return render(delete_refs(parse(formula), axis, at, count))


# --- error literals in the formula language ---------------------------------------------


def test_error_literals_lex_and_parse():
    tok = tokenize("#REF!")[0]
    assert tok.kind == TokenKind.ERROR
    assert tok.value == "#REF!"
    assert parse("=#REF!") == REF_ERR
    assert parse("=A1+#DIV/0!") == BinaryOp(
        "+", CellRef(0, 0, False, False), Literal(ErrorValue(ErrorKind.DIV0))
    )


@pytest.mark.parametrize("bad", ["=#FOO!", "=#", "=#ref"])
def test_unknown_error_literals_are_syntax_errors(bad):
    with pytest.raises(FormulaSyntaxError):
        parse(bad)


def test_error_literals_evaluate_and_propagate():
    class Empty:
        def get(self, r, c):
            return None

        def range_values(self, *a):
            return ()

    assert evaluate(compile_formula("=1+#REF!"), Empty()) == ErrorValue(ErrorKind.REF)
    assert compile_formula("=#NAME?") == Literal(ErrorValue(ErrorKind.NAME))


# --- rendering -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "formula",
    [
        "=1+2*3",
        "=(1+2)*3",
        "=1-2-3",
        "=1-(2-3)",
        "=2^3^2",
        "=(2^3)^2",
        "=-2^2",
        "=-(2^2)",
        "=(-2)%",
        "=50%%",
        "=-50%",
        '="a"&"b"',
        '="say ""hi"""',
        "=1&2=3",
        "=1<2=(3<4)",
        "=TRUE",
        "=A1:B2",
        "=SUM(A1:A3,$B$1,2.5)",
        "=$A1+A$1+$A$1",
        "=SUM()",
        "=-A1",
        "=+A1",
        "=#REF!",
        "=MAX(1,(2+3)*4)",
        "=1.5E-7",
        "=1000000",
        "=0.1",
    ],
)
def test_render_round_trips_through_the_parser(formula):
    node = parse(formula)
    assert parse("=" + render(node)) == node


def test_render_uses_minimal_parentheses():
    assert rt("=(1+2)+3") == "1+2+3"
    assert rt("=1+(2*3)") == "1+2*3"
    assert rt("=(1+2)*3") == "(1+2)*3"
    assert rt("=(A1)") == "A1"
    assert rt("=  a1 + sum( b2:a1 )") == "A1+SUM(A1:B2)"


def test_render_formats_numbers():
    assert rt("=1.0") == "1"
    assert rt("=0.5") == "0.5"
    assert rt("=1e3") == "1000"


@st.composite
def exprs(draw, depth=3):
    leaf = st.one_of(
        st.integers(0, 1000).map(lambda n: Literal(float(n))),
        st.floats(0, 1e6, allow_nan=False, allow_infinity=False).map(lambda f: Literal(abs(f))),
        st.text(alphabet='ab "', max_size=4).map(Literal),
        st.booleans().map(Literal),
        st.builds(CellRef, st.integers(0, 20), st.integers(0, 20), st.booleans(), st.booleans()),
        st.builds(
            normalised_range,
            st.builds(
                CellRef, st.integers(0, 20), st.integers(0, 20), st.booleans(), st.booleans()
            ),
            st.builds(
                CellRef, st.integers(0, 20), st.integers(0, 20), st.booleans(), st.booleans()
            ),
        ),
    )
    if depth == 0:
        return draw(leaf)
    sub = exprs(depth=depth - 1)
    return draw(
        st.one_of(
            leaf,
            st.builds(UnaryOp, st.sampled_from(["-", "+", "%"]), sub),
            st.builds(
                BinaryOp,
                st.sampled_from(["+", "-", "*", "/", "^", "&", "=", "<>", "<", ">", "<=", ">="]),
                sub,
                sub,
            ),
            st.builds(
                FuncCall,
                st.sampled_from(["SUM", "MAX", "FOO"]),
                st.lists(sub, max_size=3).map(tuple),
            ),
        )
    )


@settings(max_examples=300, deadline=None)
@given(exprs())
def test_render_then_parse_returns_the_same_tree(node):
    assert parse("=" + render(node)) == node


# --- relative shift (copy / paste / fill) -------------------------------------------------


def test_shift_moves_relative_parts_only():
    assert shifted("=A1+$B$2", 1, 1) == "B2+$B$2"
    assert shifted("=$A1", 2, 5) == "$A3"
    assert shifted("=A$1", 2, 5) == "F$1"
    assert shifted("=SUM(A1:B2)", 2, 1) == "SUM(B3:C4)"
    assert shifted("=SUM($A$1:B2)", 1, 1) == "SUM($A$1:C3)"


def test_shift_off_the_grid_gives_ref_error():
    assert shifted("=A1", -1, 0) == "#REF!"
    assert shifted("=A1", 0, -1) == "#REF!"
    assert shifted("=A1+1", 0, -1) == "#REF!+1"
    assert shift_refs(parse("=A1"), MAX_ROWS, 0) == REF_ERR
    assert shift_refs(parse("=A1"), 0, MAX_COLS) == REF_ERR
    assert shifted("=SUM(A1:B2)", -1, 0) == "SUM(#REF!)"


def test_shift_with_mixed_absolute_range_is_renormalised():
    assert shifted("=SUM($A$1:A1)", 4, 0) == "SUM($A$1:A5)"
    assert shifted("=SUM(A5:$A$5)", -2, 0) == "SUM(A3:$A$5)"
    # $ flags stay with their coordinate, not their corner, when the ends swap
    assert shifted("=SUM(A1:$A$2)", 5, 0) == "SUM(A$2:$A6)"


# --- insert rows / columns --------------------------------------------------------------


def test_insert_rows_shifts_references_at_or_after_the_insertion_point():
    assert inserted("=A5", Axis.ROW, 2) == "A6"
    assert inserted("=A3", Axis.ROW, 2) == "A4"  # row index 2 is row 3
    assert inserted("=A2", Axis.ROW, 2) == "A2"
    assert inserted("=$A$5", Axis.ROW, 0, 3) == "$A$8"


def test_insert_rows_into_a_range_expands_it_but_after_it_does_not():
    assert inserted("=SUM(A1:A3)", Axis.ROW, 1) == "SUM(A1:A4)"
    assert inserted("=SUM(A1:A3)", Axis.ROW, 0) == "SUM(A2:A4)"
    assert inserted("=SUM(A1:A3)", Axis.ROW, 3) == "SUM(A1:A3)"
    assert inserted("=SUM(A1:A3)", Axis.ROW, 2, 2) == "SUM(A1:A5)"


def test_insert_columns():
    assert inserted("=B1", Axis.COL, 0) == "C1"
    assert inserted("=B1", Axis.COL, 2) == "B1"
    assert inserted("=SUM(A1:C1)", Axis.COL, 1, 2) == "SUM(A1:E1)"


def test_insert_pushing_a_reference_off_the_grid_gives_ref_error():
    assert insert_refs(parse(f"=A{MAX_ROWS}"), Axis.ROW, 0, 1) == REF_ERR


# --- delete rows / columns --------------------------------------------------------------


def test_delete_rows_references():
    assert deleted("=A2", Axis.ROW, 1) == "#REF!"
    assert deleted("=A3", Axis.ROW, 1) == "A2"
    assert deleted("=A1", Axis.ROW, 1) == "A1"
    assert deleted("=A4", Axis.ROW, 1, 2) == "A2"
    assert deleted("=A3", Axis.ROW, 1, 2) == "#REF!"


def test_delete_rows_shrinks_ranges():
    assert deleted("=SUM(A1:A3)", Axis.ROW, 1) == "SUM(A1:A2)"
    assert deleted("=SUM(A1:A3)", Axis.ROW, 0) == "SUM(A1:A2)"
    assert deleted("=SUM(A1:A3)", Axis.ROW, 2) == "SUM(A1:A2)"
    assert deleted("=SUM(A2:A5)", Axis.ROW, 1, 2) == "SUM(A2:A3)"
    assert deleted("=SUM(A1:A3)", Axis.ROW, 3) == "SUM(A1:A3)"
    assert deleted("=SUM(A5:A9)", Axis.ROW, 0, 2) == "SUM(A3:A7)"


def test_delete_everything_in_a_range_gives_ref_error():
    assert deleted("=SUM(A1:A3)", Axis.ROW, 0, 3) == "SUM(#REF!)"
    assert deleted("=SUM(A2:A3)", Axis.ROW, 0, 5) == "SUM(#REF!)"


def test_delete_columns():
    assert deleted("=C1", Axis.COL, 0) == "B1"
    assert deleted("=B1", Axis.COL, 1) == "#REF!"
    assert deleted("=SUM(A1:D1)", Axis.COL, 1, 2) == "SUM(A1:B1)"


def test_adjusting_leaves_unaffected_nodes_equal():
    node = parse("=SUM(A1:A3)+B1")
    assert insert_refs(node, Axis.ROW, 100, 1) == node
    assert delete_refs(node, Axis.ROW, 100, 1) == node

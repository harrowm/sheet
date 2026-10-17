import pytest

from sheet.engine.ast import BinaryOp, CellRef, FuncCall, Literal, RangeRef, UnaryOp
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.parser import parse


def test_requires_leading_equals():
    with pytest.raises(FormulaSyntaxError):
        parse("1+2")


def test_number_literal():
    assert parse("=5") == Literal(5.0)


def test_precedence_mul_over_add():
    assert parse("=1+2*3") == BinaryOp("+", Literal(1.0), BinaryOp("*", Literal(2.0), Literal(3.0)))


def test_left_associative_subtraction():
    assert parse("=10-4-3") == BinaryOp(
        "-", BinaryOp("-", Literal(10.0), Literal(4.0)), Literal(3.0)
    )


def test_power_is_left_associative_as_in_excel():
    assert parse("=2^3^2") == BinaryOp("^", BinaryOp("^", Literal(2.0), Literal(3.0)), Literal(2.0))
    assert parse("=2^(3^2)") == BinaryOp(
        "^", Literal(2.0), BinaryOp("^", Literal(3.0), Literal(2.0))
    )


def test_power_binds_tighter_than_multiplication_and_takes_a_signed_exponent():
    assert parse("=2^3*4") == BinaryOp("*", BinaryOp("^", Literal(2.0), Literal(3.0)), Literal(4.0))
    assert parse("=2^-3") == BinaryOp("^", Literal(2.0), UnaryOp("-", Literal(3.0)))
    assert parse("=2^3%") == BinaryOp("^", Literal(2.0), UnaryOp("%", Literal(3.0)))


def test_unary_minus_binds_tighter_than_power_as_in_excel():
    # Excel: =-2^2 is 4, i.e. (-2)^2
    assert parse("=-2^2") == BinaryOp("^", UnaryOp("-", Literal(2.0)), Literal(2.0))


def test_percent_postfix():
    assert parse("=50%") == UnaryOp("%", Literal(50.0))


def test_parentheses_override_precedence():
    assert parse("=(1+2)*3") == BinaryOp(
        "*", BinaryOp("+", Literal(1.0), Literal(2.0)), Literal(3.0)
    )


def test_concat_lower_than_add_higher_than_comparison():
    assert parse('="a"&1+2') == BinaryOp(
        "&", Literal("a"), BinaryOp("+", Literal(1.0), Literal(2.0))
    )
    assert parse("=1&2=3") == BinaryOp("=", BinaryOp("&", Literal(1.0), Literal(2.0)), Literal(3.0))


def test_cell_reference_relative_and_absolute():
    assert parse("=B3") == CellRef(row=2, col=1, abs_row=False, abs_col=False)
    assert parse("=$B$3") == CellRef(row=2, col=1, abs_row=True, abs_col=True)
    assert parse("=$B3") == CellRef(row=2, col=1, abs_row=False, abs_col=True)


def test_reference_is_case_insensitive_and_bounds_checked():
    assert parse("=xfd1048576") == CellRef(row=1048575, col=16383, abs_row=False, abs_col=False)
    with pytest.raises(FormulaSyntaxError):
        parse("=XFE1")
    with pytest.raises(FormulaSyntaxError):
        parse("=A1048577")


def test_range():
    assert parse("=A1:A45") == RangeRef(CellRef(0, 0, False, False), CellRef(44, 0, False, False))


def test_range_is_normalised_so_start_is_top_left():
    assert parse("=B2:A1") == RangeRef(CellRef(0, 0, False, False), CellRef(1, 1, False, False))


def test_function_call_with_range_and_args():
    node = parse("=SUM(A1:A3, 4)")
    assert node == FuncCall(
        "SUM", (RangeRef(CellRef(0, 0, False, False), CellRef(2, 0, False, False)), Literal(4.0))
    )


def test_function_name_is_uppercased_and_no_args_allowed():
    assert parse("=sum()") == FuncCall("SUM", ())


@pytest.mark.parametrize("bad", ["=", "=1+", "=(1", "=1)", "=SUM(1,", "=1 2", "=*3", "=A1:"])
def test_syntax_errors(bad):
    with pytest.raises(FormulaSyntaxError):
        parse(bad)


@pytest.mark.parametrize(
    ("text", "position"),
    [
        ('="abc""x', 1),  # "" inside a string is an escaped quote, so this string never ends
        ('=1+"a""', 3),
        ('=A1&"x"&"y', 8),
        ('="a" "b', 5),
    ],
)
def test_unterminated_string_error_points_at_its_opening_quote(text, position):
    with pytest.raises(FormulaSyntaxError) as info:
        parse(text)
    assert info.value.pos == position


def test_binary_operator_precedence_and_associativity():
    assert parse("=1+2*3") == parse("=1+(2*3)")
    assert parse("=1&2+3") == parse("=1&(2+3)")
    assert parse("=1=2&3") == parse("=1=(2&3)")
    assert parse("=2^3^2") == parse("=(2^3)^2")  # left-associative, as in Excel
    assert parse("=-2^2") == parse("=(-2)^2")  # unary binds tighter than ^
    assert parse("=1-2-3") == parse("=(1-2)-3")
    assert parse("=1<2<3") == parse("=(1<2)<3")
    assert parse("=2*3%") == parse("=2*(3%)")

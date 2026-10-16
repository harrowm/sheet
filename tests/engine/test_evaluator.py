import pytest

from sheet.engine.compiler import compile_formula
from sheet.engine.evaluator import evaluate
from sheet.engine.values import ErrorKind, ErrorValue


class FakeSheet:
    """Minimal CellReader backed by a dict keyed by A1-style names."""

    def __init__(self, **cells):
        self.cells = {}
        for name, value in cells.items():
            col = ord(name[0]) - ord("A")
            row = int(name[1:]) - 1
            self.cells[(row, col)] = value

    def get(self, row, col):
        return self.cells.get((row, col))

    def range_snapshot(self, *rect):
        return None

    def range_values(self, r1, c1, r2, c2):
        return [v for (r, c), v in self.cells.items() if r1 <= r <= r2 and c1 <= c <= c2]


def calc(formula, **cells):
    return evaluate(compile_formula(formula), FakeSheet(**cells))


def err(kind):
    return ErrorValue(kind)


@pytest.mark.parametrize(
    "formula,expected",
    [
        ("=1+2*3", 7.0),
        ("=10-4-3", 3.0),
        ("=2^3^2", 512.0),
        ("=-2^2", 4.0),
        ("=50%", 0.5),
        ("=(1+2)*3", 9.0),
        ("=7/2", 3.5),
        ("=-(-3)", 3.0),
    ],
)
def test_arithmetic(formula, expected):
    assert calc(formula) == pytest.approx(expected)


def test_division_by_zero():
    assert calc("=1/A1", A1=0.0) == err(ErrorKind.DIV0)


def test_references():
    assert calc("=A1+B1", A1=2.0, B1=3.0) == 5.0


def test_empty_reference_is_zero_in_arithmetic_and_blank_in_concat():
    assert calc("=A1+1") == 1.0
    assert calc('=A1&"x"') == "x"


def test_booleans_coerce_in_arithmetic():
    assert calc("=TRUE+1") == 2.0


def test_numeric_text_converts_and_other_text_is_value_error():
    assert calc("=A1+1", A1="5") == 6.0
    assert calc("=A1+1", A1="abc") == err(ErrorKind.VALUE)


def test_concat_formats_numbers_without_trailing_zero():
    assert calc('="n="&5') == "n=5"
    assert calc('="n="&2.5') == "n=2.5"
    assert calc('=TRUE&""') == "TRUE"


def test_comparisons():
    assert calc("=1<2") is True
    assert calc("=2<=1") is False
    assert calc("=1<>2") is True
    assert calc('="a"="A"') is True  # text comparison is case-insensitive
    assert calc('=1<"a"') is True  # numbers sort before text


def test_error_propagates_through_operators():
    e = ErrorValue(ErrorKind.REF)
    assert calc("=A1+1", A1=e) == e
    assert calc('=A1&"x"', A1=e) == e
    assert calc("=-A1", A1=e) == e


def test_bare_range_outside_function_is_value_error():
    assert calc("=A1:A3", A1=1.0) == err(ErrorKind.VALUE)


def test_sum_average_max_min_count_over_range():
    cells = dict(A1=1.0, A2=2.0, A3=6.0)
    assert calc("=SUM(A1:A3)", **cells) == 9.0
    assert calc("=AVERAGE(A1:A3)", **cells) == 3.0
    assert calc("=MAX(A1:A3)", **cells) == 6.0
    assert calc("=MIN(A1:A3)", **cells) == 1.0
    assert calc("=COUNT(A1:A3)", **cells) == 3.0


def test_range_functions_ignore_text_bool_and_empty_in_ranges():
    cells = dict(A1=1.0, A2="x", A3=True, A5=4.0)
    assert calc("=SUM(A1:A5)", **cells) == 5.0
    assert calc("=COUNT(A1:A5)", **cells) == 2.0
    assert calc("=AVERAGE(A1:A5)", **cells) == 2.5


def test_range_functions_propagate_errors_but_count_ignores_them():
    e = ErrorValue(ErrorKind.NUM)
    assert calc("=SUM(A1:A2)", A1=1.0, A2=e) == e
    assert calc("=COUNT(A1:A2)", A1=1.0, A2=e) == 1.0


def test_function_with_scalar_arguments():
    assert calc("=SUM(1,2,3)") == 6.0
    assert calc("=MAX(A1,10)", A1=4.0) == 10.0
    assert calc('=SUM("x")') == err(ErrorKind.VALUE)


def test_average_of_nothing_is_div0_and_max_min_of_nothing_are_zero():
    assert calc("=AVERAGE(A1:A3)") == err(ErrorKind.DIV0)
    assert calc("=MAX(A1:A3)") == 0.0
    assert calc("=MIN(A1:A3)") == 0.0


def test_unknown_function_is_name_error():
    assert calc("=FROBNICATE(1)") == err(ErrorKind.NAME)


def test_power_overflow_is_num_error_and_zero_to_negative_is_div0():
    assert calc("=10^400") == err(ErrorKind.NUM)
    assert calc("=0^-1") == err(ErrorKind.DIV0)

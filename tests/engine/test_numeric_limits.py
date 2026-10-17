"""Numbers outside the float range, overflow, and number-like text."""

import pytest

from sheet.engine.compiler import compile_formula
from sheet.engine.entry import parse_entry
from sheet.engine.evaluator import evaluate
from sheet.engine.formats import format_value
from sheet.engine.lexer import FormulaSyntaxError, tokenize
from sheet.engine.sheet import Sheet
from sheet.engine.values import ErrorKind, ErrorValue
from tests.engine.helpers import calc

NUM = ErrorValue(ErrorKind.NUM)
VALUE = ErrorValue(ErrorKind.VALUE)


# --- numbers too large to store ----------------------------------------------------------------


@pytest.mark.parametrize("text", ["1e999", "-1e999", "9" * 400, "1.5E+4000"])
def test_an_out_of_range_number_typed_into_a_cell_is_kept_as_text(text):
    e = parse_entry(text)
    assert e.value == text and isinstance(e.value, str)


@pytest.mark.parametrize(
    "text,value",
    [("1e308", 1e308), ("-1.7976931348623157e308", -1.7976931348623157e308), ("1e-999", 0.0)],
)
def test_the_largest_and_tiniest_numbers_are_still_numbers(text, value):
    assert parse_entry(text).value == value


@pytest.mark.parametrize("text", ["1e999", "9" * 400, "2.5e5000"])
def test_an_out_of_range_literal_in_a_formula_is_a_syntax_error(text):
    with pytest.raises(FormulaSyntaxError, match="too large"):
        tokenize(text)
    with pytest.raises(FormulaSyntaxError, match="too large"):
        parse_entry(f"={text}+1")


def test_such_a_cell_never_breaks_the_display_or_a_numeric_format():
    s = Sheet(workers=1)
    s.set_entry(0, 0, "1e999")
    s.set_format_range(0, 0, 0, 0, "0.00")
    assert format_value(s.get(0, 0), "0.00") == "1e999"
    assert s.insert_rows(0) is not None  # structural edits cope too


@pytest.mark.parametrize("fmt", ["General", "0.00", "#,##0", "0.0%", "DD/MM/YYYY"])
@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_formatting_a_non_finite_value_is_never_an_exception(fmt, bad):
    # Defence in depth: such a value should not exist, but it must not crash a view.
    assert format_value(bad, fmt) == "#NUM!"


# --- overflow ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "formula", ["=SUM(1e308,1e308)", "=AVERAGE(1e308,1e308)", "=SUM(A1:A2)", "=AVERAGE(A1:A2)"]
)
def test_aggregate_overflow_is_a_num_error_not_an_exception(formula):
    assert calc(formula, A1="1e308", A2="1e308") == NUM


def test_overflow_in_one_formula_does_not_reject_an_unrelated_edit():
    s = Sheet(workers=1)
    s.set_entry(5, 0, "1")
    s.set_entry(6, 0, "=SUM(A6,1e308)")
    s.set_entry(5, 0, "1e308")  # used to be rejected with an internal error
    assert s.get(5, 0) == 1e308 and s.get(6, 0) == NUM
    s.set_entry(5, 0, "1")
    assert s.get(6, 0) == 1e308 + 1


@pytest.mark.parametrize(
    "formula", ["=1e308*10", "=2^1024", "=-1e308*10", "=1e308+1e308", "=10^400"]
)
def test_arithmetic_overflow_is_a_num_error(formula):
    assert calc(formula) == NUM


def test_a_huge_sum_that_fits_is_exact_and_max_min_are_unaffected():
    assert calc("=SUM(A1:A3)", A1="1e308", A2="-1e308", A3="5") == 5.0
    assert calc("=MAX(1e308,1e308)") == 1e308


# --- number-like text ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('="5"+1', 6.0),
        ('=" 5 "+1', 6.0),
        ('="1e2"+1', 101.0),
        ('="+5"+1', 6.0),
        ('="-.5"+1', 0.5),
        ('="5."+1', 6.0),
    ],
)
def test_text_that_looks_like_a_number_converts(text, expected):
    assert calc(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        '="inf"+1',
        '="nan"+1',
        '="Infinity"+1',
        '="-inf"+1',
        '="1_0"+1',
        '="0x10"+1',
        '="1e999"+1',
        '="٣"+1',
        '="１２"+1',
        '="5 5"+1',
        '=""+1',
        '="--5"+1',
    ],
)
def test_other_text_is_a_value_error(text):
    assert calc(text) == VALUE


@pytest.mark.parametrize("text", ["٣", "１２", "٣.٥", "1_000", "inf", "nan", "Infinity", "0x1F"])
def test_unicode_digits_and_python_only_spellings_are_text_not_numbers(text):
    e = parse_entry(text)
    assert isinstance(e.value, str) and e.value == text


def test_unicode_digits_are_not_cell_references():
    for text in ["=A١", "=٣+1"]:
        with pytest.raises(FormulaSyntaxError):
            parse_entry(text)
    assert isinstance(parse_entry("A١").value, str)


def test_aggregate_over_text_and_literals_still_works():
    assert calc('=SUM("3",4)') == 7.0
    assert calc('=SUM("inf")') == VALUE


def test_evaluate_of_constant_folding_overflow_is_an_error_literal():
    from sheet.engine.ast import Literal

    assert compile_formula("=SUM(1e308,1e308)") == Literal(NUM)
    assert evaluate(compile_formula("=1+1"), None) == 2.0  # type: ignore[arg-type]

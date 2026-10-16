import pytest

from sheet.engine.ast import Literal
from sheet.engine.entry import parse_entry
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.values import ErrorKind, ErrorValue


def test_empty_entry():
    e = parse_entry("")
    assert (e.text, e.ast, e.value, e.format) == ("", None, None, None)
    assert parse_entry("   ").value is None


@pytest.mark.parametrize(
    "text,value",
    [
        ("42", 42.0),
        ("-3.5", -3.5),
        ("+7", 7.0),
        (".5", 0.5),
        ("1e3", 1000.0),
        (" 42 ", 42.0),
    ],
)
def test_numbers(text, value):
    e = parse_entry(text)
    assert e.value == value
    assert e.text == text
    assert e.ast is None


@pytest.mark.parametrize(
    "text", ["inf", "nan", "infinity", "1_000", "1.2.3", "0x10", "1,000", "--1"]
)
def test_number_lookalikes_are_text(text):
    assert parse_entry(text).value == text


@pytest.mark.parametrize("text,value", [("TRUE", True), ("true", True), ("False", False)])
def test_booleans(text, value):
    assert parse_entry(text).value is value


def test_text():
    assert parse_entry("hello world").value == "hello world"


def test_leading_apostrophe_forces_text():
    e = parse_entry("'123")
    assert e.value == "123"
    assert e.text == "'123"


def test_date_entry_sets_value_and_format():
    e = parse_entry("25/12/2024")
    assert e.value == 45651.0
    assert e.format == "DD/MM/YYYY"
    assert parse_entry("2024-12-25").format == "YYYY-MM-DD"


def test_constant_formula_is_precalculated():
    e = parse_entry("=1/2")
    assert e.text == "=1/2"
    assert e.ast == Literal(0.5)
    assert e.value == 0.5


def test_constant_formula_error_is_precalculated():
    e = parse_entry("=1/0")
    assert e.value == ErrorValue(ErrorKind.DIV0)


def test_formula_with_references_is_not_yet_calculated():
    e = parse_entry("=A1+1")
    assert e.ast is not None
    assert not isinstance(e.ast, Literal)
    assert e.value is None


@pytest.mark.parametrize("bad", ["=", "=1+", "=(1"])
def test_bad_formula_raises(bad):
    with pytest.raises(FormulaSyntaxError):
        parse_entry(bad)

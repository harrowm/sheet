import pytest

from sheet.engine.formats import FormatError, format_value, is_date_format, validate_format
from sheet.engine.values import ErrorKind, ErrorValue


@pytest.mark.parametrize(
    "value,expected",
    [
        (5.0, "5"),
        (2.5, "2.5"),
        (1 / 3, "0.3333333333"),
        (-7.0, "-7"),
        (True, "TRUE"),
        (False, "FALSE"),
        (None, ""),
        ("text", "text"),
        (ErrorValue(ErrorKind.DIV0), "#DIV/0!"),
    ],
)
def test_general(value, expected):
    assert format_value(value, "General") == expected


@pytest.mark.parametrize(
    "fmt,value,expected",
    [
        ("0", 2.5, "3"),  # rounds half away from zero
        ("0", 3.5, "4"),
        ("0", -2.5, "-3"),
        ("0.00", 2.0, "2.00"),
        ("0.00", 2.675, "2.68"),
        ("#,##0.00", 1234567.891, "1,234,567.89"),
        ("#,##0", 999.5, "1,000"),
        ("#,##0", 12.0, "12"),
        ("$#,##0.00", 1234.5, "$1,234.50"),
        ("$#,##0.00", -1234.5, "-$1,234.50"),
        ("0.0%", 0.1234, "12.3%"),
        ("0%", 0.5, "50%"),
        ("00000", 42.0, "00042"),
        ("#.##", 0.5, ".5"),
        ("0.0#", 1.0, "1.0"),
        ("0.0#", 1.25, "1.25"),
        ("#", 0.0, ""),
        ('0 "units"', 5.0, "5 units"),
        ("0.00", -0.001, "0.00"),  # a value that rounds to zero shows no sign
    ],
)
def test_numeric_formats(fmt, value, expected):
    assert format_value(value, fmt) == expected


def test_numeric_format_on_non_numbers_falls_back_to_general():
    assert format_value("abc", "0.00") == "abc"
    assert format_value(True, "0.00") == "TRUE"
    assert format_value(None, "0.00") == ""
    assert format_value(ErrorValue(ErrorKind.REF), "0.00") == "#REF!"


@pytest.mark.parametrize(
    "fmt,value,expected",
    [
        ("DD/MM/YYYY", 45651.0, "25/12/2024"),
        ("YYYY-MM-DD", 45651.0, "2024-12-25"),
        ("DD MMM YY", 45651.0, "25 Dec 24"),
        ("MMM", 45292.0, "Jan"),
        ("HH:mm:SS", 45292.75, "18:00:00"),
        ("DD/MM/YYYY HH:mm", 45651.5, "25/12/2024 12:00"),
        ("DD/MM/YYYY", 60.0, "29/02/1900"),
        ("DD/MM/YYYY HH:mm:SS", 45292.999999999, "02/01/2024 00:00:00"),
        ("DD/MM/YYYY", -1.0, "########"),
        ("DD/MM/YYYY", 3_000_000.0, "########"),
    ],
)
def test_date_formats(fmt, value, expected):
    assert format_value(value, fmt) == expected


def test_date_format_on_non_numbers_falls_back_to_general():
    assert format_value("hi", "DD/MM/YYYY") == "hi"
    assert format_value(None, "DD/MM/YYYY") == ""


def test_is_date_format():
    assert is_date_format("DD/MM/YYYY")
    assert is_date_format("HH:mm")
    assert not is_date_format("General")
    assert not is_date_format("0.00")
    assert not is_date_format("$#,##0.00")


@pytest.mark.parametrize(
    "fmt", ["General", "0", "0.00", "#,##0.00", "$#,##0.00", "0%", "DD/MM/YYYY", '0 "kg"']
)
def test_valid_formats(fmt):
    validate_format(fmt)


@pytest.mark.parametrize("fmt", ["", "0.0.0", "QQ", "DD0", "0 DD", '0 "unterminated', "General0"])
def test_invalid_formats(fmt):
    with pytest.raises(FormatError):
        validate_format(fmt)
    with pytest.raises(FormatError):
        format_value(1.0, fmt)


# --- General display of large integers, negative zero, and huge numbers ------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (12345678901.0, "12345678901"),  # 11 digits: shown in full, as Excel does
        (99999999999.0, "99999999999"),
        (-12345678901.0, "-12345678901"),
        (4155551234.0, "4155551234"),
        (100000000000.0, "1e+11"),  # 12 digits: scientific
        (4155551234567.0, "4.155551235e+12"),
        (0.1 + 0.2, "0.3"),
        (1234567.891234567, "1234567.891"),
        (0.0, "0"),
        (-0.0, "0"),
    ],
)
def test_general_shows_integers_up_to_eleven_digits_in_full_and_never_negative_zero(
    value, expected
):
    assert format_value(value, "General") == expected


def test_numeric_formats_never_show_negative_zero():
    for fmt in ("0.00", "#,##0", "0%", "$#,##0.00"):
        assert not format_value(-0.0, fmt).startswith("-"), fmt


@pytest.mark.parametrize("fmt", ["0.00", "#,##0.00", "0.0%", "$#,##0", "0"])
def test_enormous_numbers_do_not_produce_enormous_strings(fmt):
    for value in (1e21, 1e100, 1e308, -1e308):
        shown = format_value(value, fmt)
        assert len(shown) <= 16 and "E+" in shown, (fmt, value, shown)


def test_numbers_below_the_cutoff_keep_their_full_numeric_format():
    assert format_value(1e20, "0.00") == "100000000000000000000.00"
    assert format_value(123456789012345.0, "#,##0") == "123,456,789,012,345"

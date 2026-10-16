import pytest

from sheet.ui.names import cell_name, column_name, parse_cell_name


@pytest.mark.parametrize(
    "col,name",
    [(0, "A"), (25, "Z"), (26, "AA"), (27, "AB"), (701, "ZZ"), (702, "AAA"), (16383, "XFD")],
)
def test_column_names(col, name):
    assert column_name(col) == name


def test_cell_names():
    assert cell_name(0, 0) == "A1"
    assert cell_name(1_048_575, 16_383) == "XFD1048576"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("A1", (0, 0)),
        ("b7", (6, 1)),
        (" XFD1048576 ", (1_048_575, 16_383)),
        ("$C$3", (2, 2)),
        ("AA10", (9, 26)),
    ],
)
def test_parse_cell_names(text, expected):
    assert parse_cell_name(text) == expected


@pytest.mark.parametrize("text", ["", "A", "1", "A0", "XFE1", "A1048577", "A1:B2", "hello", "A-1"])
def test_parse_rejects_bad_names(text):
    assert parse_cell_name(text) is None

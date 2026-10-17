import pytest

from sheet.engine.sheet import _moved_index
from sheet.engine.values import parse_number


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12", 12.0),
        ("-3.5", -3.5),
        ("+.5", 0.5),
        ("1e3", 1000.0),
        ("1E-2", 0.01),
        ("5.", 5.0),
        ("-0", -0.0),
    ],
)
def test_parse_number_accepts_plain_decimals(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        " 1",
        "1 ",
        "1,000",
        "0x10",
        "1e",
        "e5",
        ".",
        "-",
        "--1",
        "1.2.3",
        "١٢",
        "inf",
        "nan",
        "1e999",
        "-1e999",
    ],
)
def test_parse_number_rejects_everything_else(text):
    assert parse_number(text) is None


@pytest.mark.parametrize(
    ("i", "insert", "expected"),
    [
        (0, True, 0),  # inserting 2 at index 3: earlier rows stay
        (3, True, 5),  # rows at and after it move down by 2
        (9, True, 11),
        (0, False, 0),  # deleting 2 at index 3: earlier rows stay
        (3, False, None),  # rows inside the deleted band are gone
        (4, False, None),
        (5, False, 3),  # rows after it move up by 2
    ],
)
def test_moved_index(i, insert, expected):
    assert _moved_index(i, 3, 2, insert, 100) == expected


def test_an_insert_that_would_push_a_row_off_the_grid_raises():
    with pytest.raises(ValueError, match="off the edge"):
        _moved_index(98, 3, 2, True, 100)
    assert _moved_index(97, 3, 2, True, 100) == 99

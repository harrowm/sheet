import pytest
from hypothesis import given
from hypothesis import strategies as st

from sheet.engine.dates import (
    MAX_SERIAL,
    make_serial,
    parse_date_text,
    split_serial,
)


@pytest.mark.parametrize(
    "ymd,serial",
    [
        ((1900, 1, 1), 1),
        ((1900, 2, 28), 59),
        ((1900, 2, 29), 60),  # Excel's fictitious leap day
        ((1900, 3, 1), 61),
        ((1970, 1, 1), 25569),
        ((2000, 1, 1), 36526),
        ((2024, 1, 1), 45292),
        ((9999, 12, 31), 2958465),
    ],
)
def test_known_serials_match_excel(ymd, serial):
    assert make_serial(*ymd) == serial
    assert split_serial(float(serial))[:3] == ymd


def test_max_serial_constant():
    assert MAX_SERIAL == 2958465


@pytest.mark.parametrize(
    "ymd", [(1899, 12, 31), (2023, 2, 29), (2024, 13, 1), (2024, 4, 31), (10000, 1, 1)]
)
def test_invalid_dates_rejected(ymd):
    with pytest.raises(ValueError):
        make_serial(*ymd)


@pytest.mark.parametrize("serial", [0.0, -1.0, MAX_SERIAL + 1.0])
def test_serial_out_of_range_rejected(serial):
    with pytest.raises(ValueError):
        split_serial(serial)


def test_time_of_day_fraction():
    assert make_serial(2024, 1, 1, 12, 0, 0) == pytest.approx(45292.5)
    assert split_serial(45292.75) == (2024, 1, 1, 18, 0, 0)


def test_split_rounds_to_nearest_second_and_carries_into_the_day():
    assert split_serial(45292.999999999) == (2024, 1, 2, 0, 0, 0)


@given(st.integers(min_value=1, max_value=MAX_SERIAL))
def test_round_trip_every_serial(serial):
    y, m, d, *_ = split_serial(float(serial))
    assert make_serial(y, m, d) == serial


@pytest.mark.parametrize(
    "text,expected",
    [
        ("25/12/2024", (45651, "DD/MM/YYYY")),
        ("1/2/2024", (45323, "DD/MM/YYYY")),
        ("2024-12-25", (45651, "YYYY-MM-DD")),
        ("  25/12/2024 ", (45651, "DD/MM/YYYY")),
        ("29/02/1900", (60, "DD/MM/YYYY")),
    ],
)
def test_parse_date_text(text, expected):
    assert parse_date_text(text) == expected


@pytest.mark.parametrize(
    "text", ["31/02/2024", "hello", "", "25/12", "2024-13-01", "25-12-2024", "1/1/1899", "12.5"]
)
def test_parse_date_text_rejects(text):
    assert parse_date_text(text) is None

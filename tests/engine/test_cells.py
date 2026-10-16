import threading
import time

import pytest

from sheet.engine.cells import MAX_COLS, MAX_ROWS, Cell, CellStore
from sheet.engine.compiler import compile_formula
from sheet.engine.entry import parse_entry
from sheet.engine.evaluator import evaluate
from sheet.engine.formats import FormatError
from sheet.engine.values import ErrorKind, ErrorValue


def num(v):
    return Cell(text=str(v), ast=None, value=float(v))


def test_cell_defaults_and_error_property():
    c = Cell(text="x", ast=None, value="x")
    assert c.format == "General"
    assert c.error is None
    e = ErrorValue(ErrorKind.DIV0, "divided by zero")
    assert Cell(text="=1/0", ast=None, value=e).error == e


def test_equal_formats_are_interned():
    a = Cell("", None, None, "".join(["DD/", "MM/", "YYYY"]))  # noqa: FLY002 - must not be a compile-time constant
    b = Cell("", None, None, "".join(["DD/", "MM/", "YYYY"]))  # noqa: FLY002 - must not be a compile-time constant
    assert a.format is b.format


def test_from_entry_keeps_existing_non_general_format():
    date_entry = parse_entry("25/12/2024")
    assert Cell.from_entry(date_entry).format == "DD/MM/YYYY"
    assert Cell.from_entry(date_entry, current_format="0.00").format == "0.00"
    assert Cell.from_entry(parse_entry("5"), current_format="0.00").format == "0.00"
    assert Cell.from_entry(parse_entry("5")).format == "General"


def test_set_get_remove():
    s = CellStore()
    assert s.get_cell(0, 0) is None
    assert s.get(0, 0) is None
    s.set_cell(2, 3, num(5))
    assert s.get(2, 3) == 5.0
    assert s.get_cell(2, 3).text == "5"
    assert len(s) == 1
    assert s.remove(2, 3).value == 5.0
    assert s.remove(2, 3) is None
    assert len(s) == 0


@pytest.mark.parametrize("row,col", [(-1, 0), (0, -1), (MAX_ROWS, 0), (0, MAX_COLS)])
def test_out_of_bounds_rejected(row, col):
    s = CellStore()
    with pytest.raises(IndexError):
        s.set_cell(row, col, num(1))
    with pytest.raises(IndexError):
        s.get(row, col)


def test_grid_corners_are_valid():
    s = CellStore()
    s.set_cell(MAX_ROWS - 1, MAX_COLS - 1, num(1))
    assert s.get(MAX_ROWS - 1, MAX_COLS - 1) == 1.0


def test_range_values_small_rectangle_and_filtering():
    s = CellStore()
    s.set_cell(0, 0, num(1))
    s.set_cell(1, 0, Cell("x", None, "x"))
    s.set_cell(2, 0, Cell("=A1", None, None))  # formula not yet calculated: skipped
    s.set_cell(3, 0, Cell("t", None, True))
    s.set_cell(0, 5, num(99))  # outside the rectangle
    assert sorted(map(str, s.range_values(0, 0, 3, 0))) == sorted(["1.0", "x", "True"])


def test_range_values_huge_rectangle_only_visits_populated_cells():
    s = CellStore()
    for i in range(3):
        s.set_cell(i * 1000, i, num(i))
    start = time.perf_counter()
    values = list(s.range_values(0, 0, MAX_ROWS - 1, MAX_COLS - 1))
    assert time.perf_counter() - start < 0.5
    assert sorted(values) == [0.0, 1.0, 2.0]


def test_range_values_dense_and_sparse_paths_agree():
    s = CellStore()
    for r in range(20):
        s.set_cell(r, 0, num(r))
    dense = sorted(s.range_values(0, 0, 9, 0))  # area 10 <= len: coordinate walk
    sparse = sorted(s.range_values(0, 0, 9, 1_000))  # area >> len: scan cells
    assert dense == sparse == [float(i) for i in range(10)]


def test_used_range():
    s = CellStore()
    assert s.used_range() is None
    s.set_cell(4, 2, num(1))
    s.set_cell(1, 7, num(1))
    assert s.used_range() == (1, 2, 4, 7)


def test_set_format_on_empty_and_existing_cells():
    s = CellStore()
    s.set_format(0, 0, "0.00")
    assert s.get_cell(0, 0).format == "0.00"
    assert s.get(0, 0) is None
    s.set_cell(1, 0, num(5))
    s.set_format(1, 0, "$#,##0.00")
    assert s.get_cell(1, 0).value == 5.0
    assert s.get_cell(1, 0).format == "$#,##0.00"


def test_invalid_format_rejected_and_store_unchanged():
    s = CellStore()
    with pytest.raises(FormatError):
        s.set_format(0, 0, "QQ")
    assert len(s) == 0


def test_set_format_range():
    s = CellStore()
    s.set_cell(0, 0, num(1))
    s.set_format_range(0, 0, 1, 1, "0.0")
    assert len(s) == 4
    assert {s.get_cell(r, c).format for r in (0, 1) for c in (0, 1)} == {"0.0"}
    assert s.get(0, 0) == 1.0


def test_set_format_range_too_large_rejected_atomically():
    s = CellStore()
    with pytest.raises(ValueError):
        s.set_format_range(0, 0, MAX_ROWS - 1, MAX_COLS - 1, "0.0")
    assert len(s) == 0


def test_concurrent_writers_to_distinct_cells():
    s = CellStore()

    def work(base):
        for i in range(500):
            s.set_cell(base, i, num(i))

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s) == 8 * 500


def test_store_is_a_cell_reader_for_the_evaluator():
    s = CellStore()
    for r, v in enumerate([1, 2, 3]):
        s.set_cell(r, 0, num(v))
    assert evaluate(compile_formula("=SUM(A1:A3)*2"), s) == 12.0
    assert evaluate(compile_formula("=A1+B1"), s) == 1.0


def test_formatted_cells_builds_changes_without_touching_the_store():
    s = CellStore()
    s.set_cell(0, 0, num(1))
    changes = s.formatted_cells(0, 0, 1, 1, "0.0")
    assert len(changes) == 4
    assert changes[(0, 0)].value == 1.0 and changes[(0, 0)].format == "0.0"
    assert s.get_cell(0, 0).format == "General" and len(s) == 1
    with pytest.raises(FormatError):
        s.formatted_cells(0, 0, 1, 1, "QQ")
    with pytest.raises(ValueError):
        s.formatted_cells(0, 0, MAX_ROWS - 1, MAX_COLS - 1, "0.0")

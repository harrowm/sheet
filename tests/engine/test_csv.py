import contextlib
import csv
import io

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sheet.engine.cells import MAX_COLS, MAX_ROWS
from sheet.engine.csv_io import CsvError, export_csv, import_csv
from sheet.engine.graph import CycleError
from sheet.engine.sheet import Sheet
from tests.engine.helpers import c, cell_state, put


def exported(s: Sheet, **kw) -> list[list[str]]:
    out = io.StringIO()
    export_csv(s, out, **kw)
    return list(csv.reader(io.StringIO(out.getvalue())))


def imported(text: str, **kw) -> Sheet:
    s = Sheet()
    import_csv(s, io.StringIO(text), **kw)
    return s


# --- export ------------------------------------------------------------------------------


def test_export_display_values_use_formats():
    s = Sheet()
    put(s, "A1", "1234.5")
    s.set_format_range(*c("A1"), *c("A1"), "$#,##0.00")
    put(s, "B1", "25/12/2024")
    put(s, "C1", "TRUE")
    put(s, "D1", "=1/0")
    put(s, "E1", "hello")
    put(s, "A2", "=A1*2")
    assert exported(s) == [
        ["$1,234.50", "25/12/2024", "TRUE", "#DIV/0!", "hello"],
        ["2469", "", "", "", ""],
    ]


def test_export_raw_gives_the_text_as_typed():
    s = Sheet()
    put(s, "A1", "5")
    put(s, "B1", "=A1+1")
    put(s, "C1", "'007")
    s.set_format_range(*c("A1"), *c("A1"), "0.00")
    assert exported(s, mode="raw") == [["5", "=A1+1", "'007"]]


def test_export_always_starts_at_a1_and_pads_gaps():
    s = Sheet()
    put(s, "C2", "x")
    assert exported(s) == [["", "", ""], ["", "", "x"]]


def test_export_of_an_empty_sheet_is_empty():
    out = io.StringIO()
    assert export_csv(Sheet(), out) == 0
    assert out.getvalue() == ""


def test_export_returns_the_number_of_rows_and_can_export_a_rectangle():
    s = Sheet()
    for i, name in enumerate(["A1", "B1", "A2", "B2", "A3", "B3"]):
        put(s, name, str(i))
    out = io.StringIO()
    assert export_csv(s, out, rect=(1, 0, 2, 0)) == 2
    assert list(csv.reader(io.StringIO(out.getvalue()))) == [["2"], ["4"]]


def test_export_quotes_commas_quotes_and_newlines():
    s = Sheet()
    put(s, "A1", "a,b")
    put(s, "B1", 'say "hi"')
    put(s, "C1", "line1\nline2")
    assert exported(s) == [["a,b", 'say "hi"', "line1\nline2"]]


def test_export_other_delimiter():
    s = Sheet()
    put(s, "A1", "a;b")
    put(s, "B1", "2")
    out = io.StringIO()
    export_csv(s, out, delimiter="\t")
    assert out.getvalue() == "a;b\t2\r\n"


def test_export_invalid_mode():
    with pytest.raises(ValueError):
        export_csv(Sheet(), io.StringIO(), mode="pretty")


def test_file_round_trip_is_utf8(tmp_path):
    s = Sheet()
    put(s, "A1", "héllo €")
    path = tmp_path / "out.csv"
    export_csv(s, path)
    assert path.read_bytes() == "héllo €\r\n".encode()
    s2 = Sheet()
    import_csv(s2, path)
    assert s2.get(0, 0) == "héllo €"


def test_import_accepts_a_utf8_bom(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfname,qty\r\nwidget,3\r\n")
    s = Sheet()
    import_csv(s, path)
    assert s.get(0, 0) == "name"
    assert s.get(1, 1) == 3.0


# --- import ------------------------------------------------------------------------------


def test_import_goes_through_the_normal_entry_path():
    s = imported("1,2,=A1+B1\r\nhello,TRUE,25/12/2024\r\n")
    assert s.get(0, 2) == 3.0
    assert s.get(1, 0) == "hello"
    assert s.get(1, 1) is True
    assert s.get(1, 2) == 45651.0
    assert s.store.get_cell(1, 2).format == "DD/MM/YYYY"
    assert s.store.get_cell(0, 2).ast is not None


def test_import_dependent_formulas_in_any_order():
    s = imported("=B1+1,=C1+1,5\n")
    assert s.get(0, 0) == 7.0


def test_import_at_an_offset_returns_changed_positions():
    s = Sheet()
    changed = import_csv(s, io.StringIO("1,2\n3,4\n"), row=2, col=1)
    assert changed == {(2, 1), (2, 2), (3, 1), (3, 2)}
    assert s.get(3, 2) == 4.0
    assert s.get(0, 0) is None


def test_import_handles_quotes_commas_and_newlines():
    s = imported('"a,b","say ""hi""","line1\nline2"\r\n')
    assert [s.get(0, i) for i in range(3)] == ["a,b", 'say "hi"', "line1\nline2"]


def test_quoted_field_starting_with_equals_is_still_a_formula():
    assert imported('"=1+2"\n').get(0, 0) == 3.0


def test_import_ragged_rows_and_blank_lines():
    s = imported("1,2,3\n4\n\n5,6\n")
    assert s.get(1, 0) == 4.0
    assert s.get(1, 1) is None
    assert s.get(3, 1) == 6.0


def test_empty_fields_clear_existing_cells_but_nothing_outside_the_data():
    s = Sheet()
    put(s, "A1", "old")
    put(s, "B1", "old")
    put(s, "C1", "keep")
    import_csv(s, io.StringIO("new,\n"))
    assert s.get(0, 0) == "new"
    assert s.get(0, 1) is None
    assert s.get(0, 2) == "keep"


def test_import_is_one_undo_step():
    s = Sheet()
    put(s, "A1", "old")
    before = cell_state(s)
    import_csv(s, io.StringIO("1,2\n3,=A1+B1\n"))
    s.undo()
    assert cell_state(s) == before
    s.redo()
    assert s.get(1, 0) == 3.0


def test_malformed_formula_is_reported_with_its_position_and_changes_nothing():
    s = Sheet()
    put(s, "A1", "keep")
    before = cell_state(s)
    with pytest.raises(CsvError) as info:
        import_csv(s, io.StringIO("1,2\n3,=1+\n"))
    assert (info.value.row, info.value.col) == (1, 1)
    assert "B2" in str(info.value)
    assert cell_state(s) == before


def test_malformed_formula_can_be_kept_as_text():
    s = imported("=1+,ok\n", bad_formulas="text")
    assert s.get(0, 0) == "=1+"
    assert s.get(0, 1) == "ok"


def test_circular_references_in_the_file_are_rejected_atomically():
    s = Sheet()
    put(s, "Z1", "keep")
    before = cell_state(s)
    with pytest.raises(CycleError):
        import_csv(s, io.StringIO("=B1,=A1\n"))
    assert cell_state(s) == before


def test_import_beyond_the_grid_is_rejected():
    s = Sheet()
    with pytest.raises(CsvError):
        import_csv(s, io.StringIO("1,2\n"), col=MAX_COLS - 1)
    with pytest.raises(CsvError):
        import_csv(s, io.StringIO("1\n2\n"), row=MAX_ROWS - 1)
    assert len(s.store) == 0


def test_oversized_field_is_a_csv_error():
    with pytest.raises(CsvError):
        import_csv(Sheet(), io.StringIO("x" * 200_000 + "\n"))


def test_import_other_delimiters():
    assert imported("1;2\n", delimiter=";").get(0, 1) == 2.0
    assert imported("1\t2\n", delimiter="\t").get(0, 1) == 2.0


def test_import_empty_input():
    s = Sheet()
    assert import_csv(s, io.StringIO("")) == frozenset()
    assert not s.can_undo


def test_large_import_is_fast_enough():
    rows = "\n".join(f"{i},{i * 2},=A{i + 1}+B{i + 1}" for i in range(5000))
    s = imported(rows)
    assert s.get(4999, 2) == 4999 * 3.0


# --- round trip --------------------------------------------------------------------------

TEXTS = [
    "1",
    "2.5",
    "hello",
    "a,b",
    'q"uote',
    "line1\nline2",
    "",
    "=A1+1",
    "=SUM(A1:A3)",
    "=B2*2",
    "TRUE",
    "25/12/2024",
    " padded ",
    "'123",
    "=1/0",
    '=A1&"x"',
    "é€",
]


@settings(max_examples=100, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 3), st.integers(0, 3), st.sampled_from(TEXTS)), max_size=20)
)
def test_raw_export_then_import_reproduces_the_sheet(edits):
    src = Sheet()
    for r, k, text in edits:
        with contextlib.suppress(CycleError):
            src.set_entry(r, k, text)
    out = io.StringIO()
    export_csv(src, out, mode="raw")
    dst = Sheet()
    import_csv(dst, io.StringIO(out.getvalue()))
    for pos, cell in src.store.items():
        if cell.text.strip():
            other = dst.store.get_cell(*pos)
            assert other is not None and other.text == cell.text, pos
            assert type(other.value) is type(cell.value) and other.value == cell.value, pos
    assert {p for p, x in dst.store.items() if x.text.strip()} == {
        p for p, x in src.store.items() if x.text.strip()
    }


def test_display_export_then_import_keeps_numbers_dates_and_text():
    s = Sheet()
    put(s, "A1", "25/12/2024")
    put(s, "B1", "3.25")
    put(s, "C1", "text")
    out = io.StringIO()
    export_csv(s, out)  # display mode: the date is written as "25/12/2024"
    s2 = imported(out.getvalue())
    assert [s2.get(0, i) for i in range(3)] == [45651.0, 3.25, "text"]
    assert s2.store.get_cell(0, 0).format == "DD/MM/YYYY"

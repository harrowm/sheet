import json
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sheet.engine.cells import MAX_COLS, MAX_ROWS
from sheet.engine.files import FILE_VERSION, FileFormatError, load_sheet, save_sheet
from sheet.engine.graph import CycleError
from sheet.engine.sheet import Sheet


def c(name: str) -> tuple[int, int]:
    return int(name[1:]) - 1, ord(name[0]) - ord("A")


def put(s: Sheet, name: str, text: str):
    return s.set_entry(*c(name), text)


def state(s: Sheet):
    return {pos: (cell.text, cell.value, cell.format) for pos, cell in s.store.items()}


def write(tmp_path, content, name="x.sheet"):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def doc(**overrides):
    base = {"format": "sheet", "version": FILE_VERSION, "formats": ["General"], "cells": []}
    base.update(overrides)
    return base


# --- the file ---------------------------------------------------------------------------------


def test_file_is_versioned_json_with_one_cell_per_line_sorted_by_position(tmp_path):
    s = Sheet()
    put(s, "B2", "=A1*2")  # entered first, but sorts after A1
    put(s, "A1", "5")
    s.set_format_range(*c("B2"), *c("B2"), "0.00")
    path = tmp_path / "a.sheet"
    assert save_sheet(s, path) == 2
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data == {
        "format": "sheet",
        "version": FILE_VERSION,
        "formats": ["General", "0.00"],
        "cells": [[0, 0, "5"], [1, 1, "=A1*2", 1]],
    }
    assert (
        len(text.splitlines()) == 4
    )  # header line, one line per cell, closing line (diff-friendly)
    assert text.endswith("\n")


def test_saving_is_deterministic(tmp_path):
    a, b = Sheet(), Sheet()
    for name, t in [("A1", "1"), ("C3", "x"), ("B2", "=A1")]:
        put(a, name, t)
    for name, t in [("B2", "=A1"), ("C3", "x"), ("A1", "1")]:
        put(b, name, t)
    save_sheet(a, tmp_path / "a.sheet")
    save_sheet(b, tmp_path / "b.sheet")
    assert (tmp_path / "a.sheet").read_bytes() == (tmp_path / "b.sheet").read_bytes()


def test_formats_are_stored_once_in_a_table(tmp_path):
    s = Sheet()
    for i in range(5):
        put(s, f"A{i + 1}", str(i))
    s.set_format_range(0, 0, 4, 0, "$#,##0.00")
    save_sheet(s, tmp_path / "a.sheet")
    data = json.loads((tmp_path / "a.sheet").read_text())
    assert data["formats"] == ["General", "$#,##0.00"]
    assert all(cell[3] == 1 for cell in data["cells"])


def test_empty_sheet_round_trips(tmp_path):
    assert save_sheet(Sheet(), tmp_path / "e.sheet") == 0
    assert len(load_sheet(tmp_path / "e.sheet").store) == 0


# --- round trips -------------------------------------------------------------------------------


def test_round_trip_keeps_text_values_formats_and_recalculates_formulas(tmp_path):
    s = Sheet()
    put(s, "A1", "5")
    put(s, "A2", "hello")
    put(s, "A3", "TRUE")
    put(s, "A4", "25/12/2024")
    put(s, "B1", "=A1*2")
    put(s, "B2", "=SUM(A1:B1)+(2*3)")
    put(s, "B3", "=1/0")
    put(s, "B4", "'007")
    s.set_format_range(*c("B1"), *c("B1"), "$#,##0.00")
    save_sheet(s, tmp_path / "a.sheet")
    loaded = load_sheet(tmp_path / "a.sheet")
    assert state(loaded) == state(s)
    put(loaded, "A1", "10")  # the dependency graph was rebuilt, not just the values
    assert loaded.get(*c("B1")) == 20.0 and loaded.get(*c("B2")) == 36.0


def test_formatted_empty_cells_and_an_explicit_general_format_survive(tmp_path):
    s = Sheet()
    s.set_format_range(*c("C3"), *c("C3"), "0.0%")  # formatted but empty
    put(s, "A1", "25/12/2024")  # gets a date format automatically...
    s.set_format_range(*c("A1"), *c("A1"), "General")  # ...which the user then removes
    save_sheet(s, tmp_path / "a.sheet")
    loaded = load_sheet(tmp_path / "a.sheet")
    assert loaded.store.get_cell(*c("C3")).format == "0.0%"
    assert loaded.store.get_cell(*c("A1")).format == "General"  # not re-detected as a date
    assert loaded.get(*c("A1")) == 45651.0


def test_unicode_quotes_and_newlines_in_text(tmp_path):
    s = Sheet()
    put(s, "A1", 'héllo "wörld" €\nline two\ttab')
    save_sheet(s, tmp_path / "a.sheet")
    assert load_sheet(tmp_path / "a.sheet").get(0, 0) == 'héllo "wörld" €\nline two\ttab'
    assert "héllo" in (tmp_path / "a.sheet").read_text(encoding="utf-8")  # not \u-escaped


def test_cells_at_the_far_corner(tmp_path):
    s = Sheet()
    s.set_entry(MAX_ROWS - 1, MAX_COLS - 1, "corner")
    save_sheet(s, tmp_path / "a.sheet")
    assert load_sheet(tmp_path / "a.sheet").get(MAX_ROWS - 1, MAX_COLS - 1) == "corner"


def test_a_loaded_sheet_has_no_undo_history(tmp_path):
    s = Sheet()
    put(s, "A1", "1")
    save_sheet(s, tmp_path / "a.sheet")
    loaded = load_sheet(tmp_path / "a.sheet")
    assert not loaded.can_undo and not loaded.can_redo
    put(loaded, "A1", "2")
    loaded.undo()
    assert loaded.get(0, 0) == 1.0  # edits after loading undo back to the loaded state


def test_large_sheet_loads_quickly(tmp_path):
    s = Sheet()
    s.set_entries([(r, c, str(r * c)) for r in range(200) for c in range(50)])
    s.set_entries([(r, 50, f"=SUM(A{r + 1}:AX{r + 1})") for r in range(200)])
    save_sheet(s, tmp_path / "big.sheet")
    import time

    start = time.perf_counter()
    loaded = load_sheet(tmp_path / "big.sheet")
    assert time.perf_counter() - start < 5
    assert loaded.get(199, 50) == s.get(199, 50)


# --- saving safely -----------------------------------------------------------------------------


def test_a_failed_save_leaves_the_existing_file_untouched(tmp_path, monkeypatch):
    s = Sheet()
    put(s, "A1", "new")
    path = tmp_path / "keep.sheet"
    path.write_text("ORIGINAL")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_sheet(s, path)
    assert path.read_text() == "ORIGINAL"
    assert [p.name for p in tmp_path.iterdir()] == ["keep.sheet"]  # temp file cleaned up


def test_saving_into_a_missing_folder_is_an_oserror(tmp_path):
    with pytest.raises(OSError):
        save_sheet(Sheet(), tmp_path / "no" / "such" / "dir.sheet")


# --- loading bad files -----------------------------------------------------------------------


def test_missing_file_is_an_oserror(tmp_path):
    with pytest.raises(OSError):
        load_sheet(tmp_path / "nope.sheet")


@pytest.mark.parametrize(
    "content,fragment",
    [
        ("", "not a sheet file"),
        ("not json at all", "not a sheet file"),
        ("[1, 2]", "not a sheet file"),
        (doc(format="spreadsheet"), "not a sheet file"),
        (doc(version=FILE_VERSION + 1), "newer"),
        (doc(version=0), "version"),
        (doc(version="1"), "version"),
        (doc(cells="x"), "cells"),
        (doc(cells=[[0, 0]]), "cell 1"),
        (doc(cells=[[0, 0, "a", 0, 9]]), "cell 1"),
        (doc(cells=[["0", 0, "a"]]), "cell 1"),
        (doc(cells=[[0, 0, 5]]), "cell 1"),
        (doc(cells=[[-1, 0, "a"]]), "outside the grid"),
        (doc(cells=[[0, MAX_COLS, "a"]]), "outside the grid"),
        (doc(cells=[[0, 0, "a", 3]]), "format"),
        (doc(formats=["General", "QQ"]), "format"),
        (doc(formats=["0.00"]), "General"),
        (doc(formats="x"), "formats"),
        (doc(cells=[[0, 0, "a"], [0, 0, "b"]]), "twice"),
        (doc(cells=[[0, 1, "=1+"]]), "B1"),
    ],
)
def test_malformed_files_are_rejected_with_a_clear_message(tmp_path, content, fragment):
    with pytest.raises(FileFormatError) as info:
        load_sheet(write(tmp_path, content))
    assert fragment in str(info.value)


def test_circular_references_in_a_file_are_rejected(tmp_path):
    with pytest.raises(CycleError):
        load_sheet(write(tmp_path, doc(cells=[[0, 0, "=B1"], [0, 1, "=A1"]])))


def test_unknown_extra_keys_are_ignored(tmp_path):
    path = write(tmp_path, doc(cells=[[0, 0, "1"]], meta={"app": "future"}, extra=[1, 2]))
    assert load_sheet(path).get(0, 0) == 1.0


# --- randomised round trip ----------------------------------------------------------------------

TEXTS = [
    "1",
    "2.5",
    "hello",
    "",
    "=A1+1",
    "=SUM(A1:A4)",
    "=B2*2",
    "TRUE",
    "25/12/2024",
    "'123",
    "=1/0",
    "é€",
    "a\nb",
]
FORMATS = ["General", "0.00", "$#,##0.00", "DD/MM/YYYY", "0%"]


@settings(max_examples=80, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 4), st.integers(0, 4), st.sampled_from(TEXTS)), max_size=25),
    st.lists(st.tuples(st.integers(0, 4), st.integers(0, 4), st.sampled_from(FORMATS)), max_size=6),
)
def test_random_sheets_round_trip_exactly(tmp_path_factory, edits, formats):
    s = Sheet()
    for r, k, text in edits:
        try:
            s.set_entry(r, k, text)
        except CycleError:
            pass
    for r, k, fmt in formats:
        s.set_format_range(r, k, r, k, fmt)
    path = tmp_path_factory.mktemp("rt") / "r.sheet"
    save_sheet(s, path)
    loaded = load_sheet(path)
    assert state(loaded) == state(s)
    assert {p: x.ast for p, x in loaded.store.items()} == {p: x.ast for p, x in s.store.items()}

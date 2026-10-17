"""Selection-based operations are bounded, so one accidental keystroke cannot freeze the engine."""

import time

import pytest

import sheet.engine.sheet as sheet_module
import sheet.server.protocol as protocol_module
from sheet.engine.limits import MAX_WRITE_CELLS
from sheet.engine.sheet import Sheet
from sheet.server.protocol import Session


def state(s: Sheet):
    return {pos: (c.text, c.value, c.format) for pos, c in s.store.items()}


def test_the_limit_is_250000_cells():
    assert MAX_WRITE_CELLS == 250_000


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setattr(sheet_module, "MAX_WRITE_CELLS", 100)
    monkeypatch.setattr(protocol_module, "MAX_WRITE_CELLS", 100)


# --- fill ----------------------------------------------------------------------------------------------


def test_fill_down_at_the_limit_works_and_one_over_is_rejected_without_changes(small_limit):
    s = Sheet(workers=1)
    s.set_entry(0, 0, "=B1")
    s.fill_down(0, 0, 100, 0)  # 100 rows written: allowed
    assert s.store.get_cell(100, 0).text == "=B101"
    s2 = Sheet(workers=1)
    s2.set_entry(0, 0, "=B1")
    before = state(s2)
    with pytest.raises(ValueError, match="100 cells"):
        s2.fill_down(0, 0, 101, 0)
    assert state(s2) == before


def test_fill_right_counts_the_populated_cells_it_would_write(small_limit):
    s = Sheet(workers=1)
    for r in range(10):
        s.set_entry(r, 0, str(r))  # a populated column of 10 cells
    s.fill_right(0, 0, 9, 10)  # 10 cells x 10 columns = 100: allowed
    with pytest.raises(ValueError, match="cells"):
        s.fill_right(0, 0, 9, 11)  # 110


def test_filling_an_empty_source_is_bounded_by_the_number_of_rows_too(small_limit):
    s = Sheet(workers=1)
    with pytest.raises(ValueError, match="cells"):
        s.fill_down(0, 0, 500, 0)  # nothing to copy, but 500 rows to clear


def test_select_all_fill_is_rejected_immediately_with_the_real_limit():
    s = Sheet(workers=1)
    s.set_entry(0, 0, "=B1+1")
    start = time.perf_counter()
    with pytest.raises(ValueError, match="250,000 cells"):
        s.fill_down(0, 0, 1_048_575, 0)  # this used to run for ~24 seconds
    assert time.perf_counter() - start < 1.0
    assert len(s.store) == 1


# --- paste -----------------------------------------------------------------------------------------------


def test_paste_of_a_large_copy_is_rejected_and_small_ones_work(small_limit):
    s = Sheet(workers=1)
    s.set_entries([(r, 0, str(r)) for r in range(100)])
    clip = s.copy(0, 0, 99, 0)
    s.paste(clip, 0, 1)  # 100 cells: allowed
    s.set_entry(100, 0, "one more")
    big = s.copy(0, 0, 100, 0)  # 101 cells
    before = state(s)
    with pytest.raises(ValueError, match="100 cells"):
        s.paste(big, 0, 3)
    assert state(s) == before


def test_copying_is_not_limited_only_pasting():
    s = Sheet(workers=1)
    s.set_entries([(r, 0, str(r)) for r in range(2000)])
    clip = s.copy(0, 0, 1_048_575, 16_383)
    assert len(clip.cells) == 2000


# --- through the protocol -------------------------------------------------------------------------------------


def test_the_protocol_reports_these_as_invalid_requests(small_limit):
    session = Session(workers=1)
    session.handle({"op": "edit", "entries": [[0, 0, "1"]]})
    resp = session.handle({"op": "fill", "rect": [0, 0, 500, 0], "direction": "down"})
    assert (
        resp["ok"] is False
        and resp["error"]["kind"] == "invalid"
        and "100 cells" in resp["error"]["message"]
    )
    text = "\n".join("\t".join(["x"] * 5) for _ in range(30))  # 150 cells
    resp = session.handle({"op": "paste_text", "at": [0, 0], "text": text})
    assert resp["ok"] is False and "100 cells" in resp["error"]["message"]
    assert session.handle({"op": "paste_text", "at": [0, 0], "text": "a\tb"})["ok"] is True
    session.close()


def test_a_huge_paste_text_is_rejected_before_it_is_split():
    session = Session(workers=1)
    start = time.perf_counter()
    resp = session.handle({"op": "paste_text", "at": [0, 0], "text": "\t" * 3_000_000})
    assert resp["ok"] is False and time.perf_counter() - start < 2.0
    session.close()

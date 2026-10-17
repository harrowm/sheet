"""Undo history is bounded by the number of cells it holds, not only by the number of steps."""

from sheet.engine.sheet import DEFAULT_MAX_HISTORY_CELLS, Sheet
from sheet.server.protocol import Session


def fan_out_sheet(dependents: int, **kw) -> Sheet:
    """A seed in A1 feeding `dependents` formulas in column B (every seed edit changes them all)."""
    s = Sheet(workers=1, **kw)
    s.set_entry(0, 0, "1")
    s.set_entries([(r, 1, f"=$A$1+{r}") for r in range(1, dependents + 1)])
    s.clear_history()
    return s


def test_default_limit_is_a_million_cells():
    assert DEFAULT_MAX_HISTORY_CELLS == 1_000_000


def test_history_never_holds_more_cells_than_the_cap():
    s = fan_out_sheet(29, max_history_cells=100)  # each seed edit changes 30 cells
    for i in range(10):
        s.set_entry(0, 0, str(i + 2))
        assert s.history_cells <= 100
    undone = 0
    while s.can_undo:
        s.undo()
        undone += 1
    assert undone == 3  # 100 // 30: the oldest steps were dropped, the newest kept


def test_the_newest_steps_are_the_ones_kept_and_they_undo_correctly():
    s = fan_out_sheet(29, max_history_cells=100)
    for i in range(6):
        s.set_entry(0, 0, str(10 + i))  # A1 = 10..15
    s.undo()
    assert s.get(0, 0) == 14.0 and s.get(5, 1) == 14.0 + 5
    s.undo()
    s.undo()
    assert s.get(0, 0) == 12.0 and not s.can_undo
    s.redo()
    assert s.get(0, 0) == 13.0


def test_a_single_change_bigger_than_the_cap_cannot_be_undone_and_clears_older_history():
    s = Sheet(workers=1, max_history_cells=100)
    s.set_entry(0, 0, "old")
    assert s.can_undo
    s.set_entries([(r, 3, "x") for r in range(150)])  # 150 cells > cap
    assert s.last_undo_dropped is True
    assert not s.can_undo and not s.can_redo  # the earlier step is gone too: state moved past it
    assert s.get(149, 3) == "x"  # but the change itself was applied


def test_history_resumes_normally_after_a_dropped_change():
    s = Sheet(workers=1, max_history_cells=100)
    s.set_entries([(r, 3, "x") for r in range(150)])
    s.set_entry(0, 0, "a")
    assert s.last_undo_dropped is False and s.can_undo
    s.undo()
    assert s.get(0, 0) is None and s.get(10, 3) == "x"


def test_the_flag_is_per_change():
    s = Sheet(workers=1, max_history_cells=100)
    s.set_entries([(r, 3, "x") for r in range(150)])
    assert s.last_undo_dropped
    s.set_entry(0, 0, "1")
    assert not s.last_undo_dropped


def test_the_step_count_limit_still_applies():
    s = Sheet(workers=1, max_history=3, max_history_cells=10_000)
    for i in range(10):
        s.set_entry(0, 0, str(i))
    undone = 0
    while s.can_undo:
        s.undo()
        undone += 1
    assert undone == 3


def test_redo_survives_within_the_cap_and_is_cleared_by_a_new_edit():
    s = fan_out_sheet(9, max_history_cells=100)
    s.set_entry(0, 0, "5")
    s.undo()
    assert s.can_redo and s.history_cells == 10
    s.set_entry(0, 0, "6")
    assert not s.can_redo


def test_formats_and_structural_edits_count_too():
    s = Sheet(workers=1, max_history_cells=100)
    s.set_format_range(0, 0, 14, 9, "0.00")  # 150 cells
    assert s.last_undo_dropped
    s2 = Sheet(workers=1, max_history_cells=100)
    s2.set_entries([(r, 0, str(r)) for r in range(60)])
    s2.insert_rows(0)  # moves 60 cells: 61 positions change
    assert s2.can_undo and s2.history_cells <= 100


def test_memory_stays_bounded_in_the_scenario_that_used_to_reach_760_mb():
    s = fan_out_sheet(3000, max_history_cells=20_000)
    for i in range(40):
        s.set_entry(0, 0, str(i + 2))
        assert s.history_cells <= 20_000
    assert s.history_cells >= 3001  # at least the latest step is always kept


# --- protocol ----------------------------------------------------------------------------------


def test_the_protocol_reports_when_a_change_cannot_be_undone():
    session = Session(workers=1, max_history_cells=50)
    small = session.handle({"op": "edit", "entries": [[0, 0, "1"]]})
    assert small["ok"] and not small.get("undo_dropped") and small["can_undo"]
    big = session.handle({"op": "edit", "entries": [[r, 5, "x"] for r in range(100)]})
    assert big["ok"] and big["undo_dropped"] is True and big["can_undo"] is False
    again = session.handle({"op": "edit", "entries": [[0, 0, "2"]]})
    assert not again.get("undo_dropped") and again["can_undo"]
    session.close()

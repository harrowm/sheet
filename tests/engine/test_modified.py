"""``Sheet.modified``: does the content differ from the last save or load? Editing and then
undoing back to the saved point must read as unmodified."""

from sheet.engine.files import load_sheet, save_sheet
from sheet.engine.sheet import Sheet
from sheet.server.protocol import Session


def test_a_new_sheet_is_unmodified_and_an_edit_modifies_it():
    s = Sheet()
    assert not s.modified
    s.set_entry(0, 0, "1")
    assert s.modified


def test_undoing_back_to_the_saved_point_clears_the_flag_and_redo_sets_it_again():
    s = Sheet()
    s.set_entry(0, 0, "1")
    s.mark_saved()
    assert not s.modified
    s.set_entry(0, 0, "2")
    s.set_entry(0, 1, "3")
    assert s.modified
    s.undo()
    assert s.modified
    s.undo()
    assert not s.modified  # back to what was saved
    s.redo()
    assert s.modified
    s.undo()
    assert not s.modified


def test_undoing_past_the_saved_point_is_a_modification():
    s = Sheet()
    s.set_entry(0, 0, "1")
    s.mark_saved()
    s.undo()
    assert s.modified
    s.redo()
    assert not s.modified


def test_saving_with_everything_undone_then_redoing_modifies():
    s = Sheet()
    s.set_entry(0, 0, "1")
    s.undo()
    s.mark_saved()
    assert not s.modified
    s.redo()
    assert s.modified
    s.undo()
    assert not s.modified


def test_a_new_edit_after_undo_is_a_modification_even_though_history_depth_matches():
    s = Sheet()
    s.set_entry(0, 0, "1")
    s.mark_saved()
    s.undo()
    s.set_entry(0, 0, "2")  # a different transaction now sits where the saved one did
    assert s.modified
    s.undo()
    assert s.modified  # undone past the saved point, not back to it


def test_an_edit_that_changes_nothing_does_not_modify():
    s = Sheet()
    s.set_entry(0, 0, "1")
    s.mark_saved()
    s.set_entry(0, 0, "1")
    assert not s.modified


def test_a_change_too_big_for_the_history_is_a_modification_that_undo_cannot_clear():
    s = Sheet(max_history_cells=10)
    s.set_entry(0, 0, "1")
    s.mark_saved()
    s.set_entries([(r, 1, str(r)) for r in range(50)])  # more than the history keeps
    assert s.last_undo_dropped and s.modified
    s.mark_saved()
    assert not s.modified


def test_trimming_old_history_does_not_change_the_flag():
    s = Sheet(max_history=3)
    for i in range(3):
        s.set_entry(i, 0, str(i))
    s.mark_saved()
    for i in range(3, 8):
        s.set_entry(i, 0, str(i))  # older steps fall off the history
    assert s.modified
    s.mark_saved()
    s.set_entry(9, 0, "x")
    s.undo()
    assert not s.modified


def test_clear_history_is_a_modification_until_saved_again():
    s = Sheet()
    s.set_entry(0, 0, "1")
    s.mark_saved()
    s.clear_history()
    assert s.modified


def test_a_loaded_sheet_is_unmodified(tmp_path):
    s = Sheet()
    s.set_entry(0, 0, "1")
    save_sheet(s, tmp_path / "a.sheet")
    loaded = load_sheet(tmp_path / "a.sheet")
    assert not loaded.modified
    loaded.set_entry(0, 0, "2")
    assert loaded.modified
    loaded.undo()
    assert not loaded.modified


def test_the_protocol_reports_modified_and_saving_clears_it(tmp_path):
    session = Session()
    assert session.handle({"op": "hello"})["modified"] is False
    assert session.handle({"op": "edit", "entries": [[0, 0, "1"]]})["modified"] is True
    path = str(tmp_path / "p.sheet")
    assert session.handle({"op": "save", "path": path})["modified"] is False
    assert session.handle({"op": "edit", "entries": [[0, 0, "2"]]})["modified"] is True
    assert session.handle({"op": "undo"})["modified"] is False
    assert session.handle({"op": "redo"})["modified"] is True
    assert session.handle({"op": "load", "path": path})["modified"] is False
    assert session.handle({"op": "edit", "entries": [[0, 0, "3"]]})["modified"] is True
    assert session.handle({"op": "new"})["modified"] is False
    session.sheet.close()

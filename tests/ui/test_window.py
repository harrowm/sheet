import pytest
from PyQt6.QtCore import QItemSelection, QItemSelectionModel, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit, QStyleOptionViewItem

from sheet.ui.window import MainWindow

DISPLAY = Qt.ItemDataRole.DisplayRole


@pytest.fixture
def win(engine, qapp, wait_until):
    w = MainWindow(engine, interactive=False)
    w.show()
    wait_until(
        lambda: "free-threaded" in w.engine_label.text(),
        message="window never saw the engine hello",
    )
    yield w
    w.close()


@pytest.fixture
def put(win, wait_until):
    """Enter cells through the window's model (as a user edit would): put(("A1", "5"), ...)."""
    from sheet.ui.names import parse_cell_name

    def run(*pairs):
        entries = [[*parse_cell_name(name), text] for name, text in pairs]
        done = []
        win.model.send("edit", entries=entries, on_done=done.append)
        wait_until(lambda: done, message="edit not answered")
        assert done[0]["ok"], done[0]

    return run


def goto(win, name):
    from sheet.ui.names import parse_cell_name

    row, col = parse_cell_name(name)
    win.goto(row, col)


def shown(win, name):
    from sheet.ui.names import parse_cell_name

    row, col = parse_cell_name(name)
    return win.model.data(win.model.index(row, col), DISPLAY)


def wait_shown(win, wait_until, name, expected):
    wait_until(
        lambda: shown(win, name) == expected,
        message=f"{name} never showed {expected!r}, has {shown(win, name)!r}",
    )


def select(win, first, last):
    from sheet.ui.names import parse_cell_name

    (r1, c1), (r2, c2) = parse_cell_name(first), parse_cell_name(last)
    sm = win.view.selectionModel()
    sm.setCurrentIndex(win.model.index(r1, c1), QItemSelectionModel.SelectionFlag.NoUpdate)
    sm.select(
        QItemSelection(win.model.index(r1, c1), win.model.index(r2, c2)),
        QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )


# --- layout and navigation ------------------------------------------------------------------


def test_window_shows_the_engine_and_a_full_size_grid(win):
    assert win.model.rowCount() == 1_048_576 and win.model.columnCount() == 16_384
    assert "GIL off" in win.engine_label.text()
    assert "Sheet" in win.windowTitle() and "Untitled" in win.windowTitle()
    assert win.name_box.text() == "A1"


def test_moving_the_cursor_updates_the_name_box_and_formula_bar(win, put, wait_until):
    put(("B3", "=A1+1"))
    goto(win, "B3")
    assert win.name_box.text() == "B3"
    wait_until(lambda: win.formula_bar.text() == "=A1+1")
    goto(win, "C9")
    wait_until(lambda: win.formula_bar.text() == "" and win.name_box.text() == "C9")


def test_name_box_jumps_to_a_cell_and_rejects_nonsense(win):
    win.name_box.setText("D12")
    QTest.keyClick(win.name_box, Qt.Key.Key_Return)
    assert (win.view.currentIndex().row(), win.view.currentIndex().column()) == (11, 3)
    win.name_box.setText("nonsense")
    QTest.keyClick(win.name_box, Qt.Key.Key_Return)
    assert "nonsense" in win.status_text()
    assert win.name_box.text() == "D12"  # reverts to the current cell


def test_goto_far_cells(win):
    win.goto(1_048_575, 16_383)
    assert win.name_box.text() == "XFD1048576"


# --- editing ----------------------------------------------------------------------------------


def test_formula_bar_commits_to_the_current_cell(win, wait_until):
    goto(win, "B2")
    win.formula_bar.setText("=3*4")
    QTest.keyClick(win.formula_bar, Qt.Key.Key_Return)
    wait_shown(win, wait_until, "B2", "12")


def test_escape_in_the_formula_bar_restores_the_cell_text(win, put, wait_until):
    put(("A1", "keep"))
    goto(win, "A1")
    wait_until(lambda: win.formula_bar.text() == "keep")
    QTest.keyClicks(win.formula_bar, "xyz")
    QTest.keyClick(win.formula_bar, Qt.Key.Key_Escape)
    assert win.formula_bar.text() == "keep"


def test_the_formula_bar_is_not_overwritten_while_being_edited(win, put, call, wait_until):
    goto(win, "A1")
    QTest.keyClicks(win.formula_bar, "=1+")
    put(("A1", "99"))  # the engine changes the cell underneath
    call("hello")
    QTest.qWait(50)
    assert win.formula_bar.text() == "=1+"


def test_a_rejected_edit_is_reported_in_the_status_bar(win, put, wait_until):
    put(("A1", "1"), ("B1", "=A1"))
    goto(win, "B1")
    win.formula_bar.setText("=1+")
    QTest.keyClick(win.formula_bar, Qt.Key.Key_Return)
    wait_until(lambda: "=1+" in win.status_text() or "syntax" in win.status_text().lower())
    assert win.status_is_error()
    goto(win, "A1")
    win.formula_bar.setText("=B1")
    QTest.keyClick(win.formula_bar, Qt.Key.Key_Return)
    wait_until(lambda: "ircular" in win.status_text())


def test_enter_in_the_cell_editor_commits_and_moves_down(win, wait_until):
    index = win.model.index(2, 2)
    win.view.setCurrentIndex(index)
    win.view.edit(index)
    editor = win.view.indexWidget(index)
    assert isinstance(editor, QLineEdit)
    editor.setText("=2+3")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    wait_shown(win, wait_until, "C3", "5")
    wait_until(lambda: win.view.currentIndex().row() == 3 and win.view.currentIndex().column() == 2)


def test_editing_through_the_delegate_updates_the_engine(win, wait_until):
    index = win.model.index(0, 0)
    editor = win.view.itemDelegate().createEditor(
        win.view.viewport(), QStyleOptionViewItem(), index
    )
    editor.setText("=7*6")
    win.view.itemDelegate().setModelData(editor, win.model, index)
    wait_shown(win, wait_until, "A1", "42")


# --- undo / redo -----------------------------------------------------------------------------


def test_undo_and_redo_actions_follow_availability_and_work(win, wait_until):
    assert not win.actions_by_name["undo"].isEnabled()
    goto(win, "A1")
    win.formula_bar.setText("5")
    QTest.keyClick(win.formula_bar, Qt.Key.Key_Return)
    wait_until(lambda: win.actions_by_name["undo"].isEnabled())
    win.actions_by_name["undo"].trigger()
    wait_until(
        lambda: (
            not win.actions_by_name["undo"].isEnabled() and win.actions_by_name["redo"].isEnabled()
        )
    )
    wait_until(lambda: shown(win, "A1") in ("", None), message="A1 still shows the undone value")
    win.actions_by_name["redo"].trigger()
    wait_shown(win, wait_until, "A1", "5")


# --- clipboard, clear, fill --------------------------------------------------------------------


def test_copy_and_paste_shift_formula_references(win, put, call, wait_until):
    put(("A1", "1"), ("B1", "=A1+1"), ("A3", "10"))
    select(win, "B1", "B1")
    win.copy()
    wait_until(lambda: QApplication.clipboard().text() == "2")
    select(win, "B3", "B3")
    win.paste()
    wait_until(lambda: call("cell", at=[2, 1])["text"] == "=A3+1")
    wait_shown(win, wait_until, "B3", "11")


def test_paste_text_from_outside_the_app(win, wait_until):
    QApplication.clipboard().setText("x\ty\n1\t=B2*2")
    select(win, "D1", "D1")
    win.paste()
    wait_shown(win, wait_until, "D1", "x")
    wait_shown(win, wait_until, "E1", "y")
    wait_shown(win, wait_until, "D2", "1")
    wait_shown(win, wait_until, "E2", "0")  # "=B2*2" with B2 empty


def test_paste_values_pastes_results_not_formulas(win, put, call, wait_until):
    put(("A1", "3"), ("B1", "=A1*2"))
    select(win, "B1", "B1")
    win.copy()
    wait_until(lambda: QApplication.clipboard().text() == "6")
    select(win, "C1", "C1")
    win.paste_values()
    wait_until(lambda: call("cell", at=[0, 2])["text"] == "6")


def test_paste_with_an_empty_clipboard_says_so(win):
    QApplication.clipboard().clear()
    win.paste()
    assert "clipboard" in win.status_text().lower()


def test_delete_clears_the_selection_and_is_undoable(win, put, wait_until):
    put(("A1", "1"), ("A2", "2"), ("B1", "keep"))
    select(win, "A1", "A2")
    wait_until(lambda: shown(win, "A2") == "2")
    QTest.keyClick(win.view, Qt.Key.Key_Delete)
    wait_until(lambda: shown(win, "A1") in ("", None) and shown(win, "A2") in ("", None))
    assert shown(win, "B1") == "keep"
    win.actions_by_name["undo"].trigger()
    wait_shown(win, wait_until, "A2", "2")


def test_select_all_and_clear_does_not_hang(win, put, wait_until):
    put(("C3", "x"))
    win.view.selectAll()
    win.clear_selection()
    wait_until(lambda: shown(win, "C3") in ("", None))


def test_copying_a_whole_column_is_cheap(win, put, wait_until):
    put(("A1", "1"), ("A2", "2"))
    win.view.selectColumn(0)
    win.copy()
    wait_until(lambda: QApplication.clipboard().text() == "1\n2")


def test_fill_down_and_right(win, put, call, wait_until):
    put(("A1", "=B1*2"), ("B1", "1"), ("B2", "2"), ("B3", "3"))
    select(win, "A1", "A3")
    win.fill_down()
    wait_until(lambda: call("cell", at=[2, 0])["text"] == "=B3*2")
    put(("E1", "=D1+1"))
    select(win, "E1", "G1")
    win.fill_right()
    wait_until(lambda: call("cell", at=[0, 6])["text"] == "=F1+1")


def test_fill_needs_more_than_one_row(win):
    select(win, "A1", "A1")
    win.fill_down()
    assert "more than one" in win.status_text().lower()


# --- formats ------------------------------------------------------------------------------------


def test_format_actions_apply_to_the_selection(win, put, wait_until):
    put(("A1", "1234.5"), ("A2", "0.25"))
    select(win, "A1", "A2")
    win.actions_by_name["format:$#,##0.00"].trigger()
    wait_shown(win, wait_until, "A1", "$1,234.50")
    wait_shown(win, wait_until, "A2", "$0.25")
    win.actions_by_name["format:0.0%"].trigger()
    wait_shown(win, wait_until, "A2", "25.0%")


def test_custom_format_errors_are_reported(win, wait_until):
    select(win, "A1", "A1")
    win.apply_format("QQ")
    wait_until(lambda: win.status_is_error())


# --- structure ----------------------------------------------------------------------------------


def test_insert_and_delete_rows_use_the_selected_rows(win, put, wait_until):
    put(("A1", "top"), ("A2", "mid"), ("A3", "bottom"))
    win.view.selectRow(1)
    win.actions_by_name["insert_rows"].trigger()
    wait_shown(win, wait_until, "A3", "mid")
    wait_shown(win, wait_until, "A4", "bottom")
    win.view.selectRow(1)
    win.actions_by_name["delete_rows"].trigger()
    wait_shown(win, wait_until, "A2", "mid")
    assert shown(win, "A1") == "top"


def test_insert_several_columns_at_once(win, put, wait_until):
    put(("A1", "a"), ("B1", "b"))
    select(win, "A1", "B1")  # two columns selected: two are inserted
    win.actions_by_name["insert_cols"].trigger()
    wait_shown(win, wait_until, "C1", "a")
    wait_shown(win, wait_until, "D1", "b")


def test_context_menu_offers_the_editing_commands(win):
    texts = [a.text().replace("&", "") for a in win.build_context_menu().actions() if a.text()]
    for expected in (
        "Copy",
        "Paste",
        "Clear",
        "Insert Row",
        "Delete Row",
        "Insert Column",
        "Delete Column",
    ):
        assert any(expected in t for t in texts), (expected, texts)


# --- files --------------------------------------------------------------------------------------


def test_save_and_open_csv(win, put, call, wait_until, tmp_path):
    put(("A1", "1"), ("B1", "=A1+1"))
    path = str(tmp_path / "sheet.csv")
    win.save_csv(path, "raw")
    wait_until(lambda: (tmp_path / "sheet.csv").exists() and "Exported" in win.status_text())
    assert (tmp_path / "sheet.csv").read_bytes() == b"1,=A1+1\r\n"
    goto(win, "D5")
    win.open_csv(path)
    wait_until(lambda: call("cell", at=[4, 4])["text"] == "=A1+1")


def test_a_bad_csv_is_reported_not_loaded(win, tmp_path, wait_until):
    bad = tmp_path / "bad.csv"
    bad.write_text("1,=1+\n")
    win.open_csv(str(bad))
    wait_until(lambda: win.status_is_error())
    assert "B1" in win.status_text()
    win.open_csv(str(tmp_path / "missing.csv"))
    wait_until(lambda: "missing.csv" in win.status_text())


# --- engine lifecycle -----------------------------------------------------------------------------


def test_busy_indicator_appears_only_when_work_takes_a_moment(win, engine, wait_until):
    wait_until(lambda: not engine.busy)  # let the window's own startup requests finish
    win._on_busy(True)
    assert win.busy_label.isHidden()  # not immediately: quick requests never flash it
    wait_until(
        lambda: not win.busy_label.isHidden(), message="calculating indicator never appeared"
    )
    win._on_busy(False)
    assert win.busy_label.isHidden()
    win._on_busy(True)
    win._on_busy(False)  # finishes before the delay: it never shows
    QTest.qWait(250)
    assert win.busy_label.isHidden()


def test_if_the_engine_dies_the_window_says_so_and_locks_up(win, engine, wait_until):
    engine._process.kill()
    wait_until(lambda: not win.view.isEnabled(), message="window did not react to the crash")
    assert win.status_is_error() and "engine" in win.status_text().lower()


def test_closing_the_window_shuts_the_engine_down(engine, qapp, wait_until):
    w = MainWindow(engine, interactive=False)
    w.show()
    w.close()
    assert not engine.alive


# --- native files: new, open, save -------------------------------------------------------------------


def make_doc(win, put, wait_until):
    put(("A1", "5"), ("B1", "=A1*2"))
    select(win, "B1", "B1")
    win.apply_format("$#,##0.00")
    wait_until(lambda: shown(win, "B1") == "$10.00")


def test_file_actions_and_shortcuts_exist(win):
    names = win.actions_by_name
    for n in ("new", "open_file", "save", "save_as", "import_csv", "export_values", "export_raw"):
        assert n in names, n
    assert names["new"].shortcut().toString() == "Ctrl+N"
    assert names["open_file"].shortcut().toString() == "Ctrl+O"
    assert names["save"].shortcut().toString() == "Ctrl+S"
    assert "Shift" in names["save_as"].shortcut().toString()


def test_title_shows_the_file_name_and_a_modified_marker(win, put, wait_until, tmp_path):
    assert "*" not in win.windowTitle()
    put(("A1", "1"))
    wait_until(lambda: win.is_dirty() and "*" in win.windowTitle())
    path = str(tmp_path / "budget.sheet")
    win.save_file(path)
    wait_until(lambda: not win.is_dirty() and "budget.sheet" in win.windowTitle())
    assert "*" not in win.windowTitle() and win.current_path == path
    put(("A2", "2"))
    wait_until(lambda: "*" in win.windowTitle() and "budget.sheet" in win.windowTitle())


def test_save_then_open_restores_everything(win, put, call, wait_until, tmp_path):
    make_doc(win, put, wait_until)
    path = str(tmp_path / "doc.sheet")
    win.save_file(path)
    wait_until(lambda: (tmp_path / "doc.sheet").exists() and "Saved" in win.status_text())
    assert "2 cells" in win.status_text()
    win.new_file()
    wait_until(lambda: shown(win, "A1") in ("", None) and shown(win, "B1") in ("", None))
    assert "Untitled" in win.windowTitle() and win.current_path is None
    win.open_file(path)
    wait_shown(win, wait_until, "B1", "$10.00")
    wait_shown(win, wait_until, "A1", "5")
    assert win.current_path == path and not win.is_dirty()
    assert "doc.sheet" in win.windowTitle()
    assert not win.actions_by_name["undo"].isEnabled()  # a loaded document starts with no history
    put(("A1", "7"))  # formulas were rebuilt on load
    wait_shown(win, wait_until, "B1", "$14.00")


def test_opening_replaces_what_was_on_screen(win, put, wait_until, tmp_path):
    put(("A1", "old"), ("Z9", "stale"))
    wait_shown(win, wait_until, "Z9", "stale")
    other = tmp_path / "other.sheet"
    other.write_text(
        '{"format":"sheet","version":1,"formats":["General"],"cells":[[0,0,"fresh"]]}\n'
    )
    win.open_file(str(other))
    wait_shown(win, wait_until, "A1", "fresh")
    wait_until(lambda: shown(win, "Z9") in ("", None))
    assert (win.view.currentIndex().row(), win.view.currentIndex().column()) == (0, 0)


def test_a_bad_file_is_reported_and_the_document_is_kept(win, put, wait_until, tmp_path):
    put(("A1", "mine"))
    wait_shown(win, wait_until, "A1", "mine")
    bad = tmp_path / "bad.sheet"
    bad.write_text("this is not a sheet file")
    win.open_file(str(bad))
    wait_until(lambda: win.status_is_error())
    assert "sheet file" in win.status_text().lower()
    assert shown(win, "A1") == "mine" and win.current_path is None
    win.open_file(str(tmp_path / "missing.sheet"))
    wait_until(lambda: "missing.sheet" in win.status_text())


def test_save_without_a_path_asks_for_one_instead_of_saving(win, put, wait_until):
    put(("A1", "1"))
    wait_until(win.is_dirty)
    win.save_file()  # non-interactive: no dialog, and nothing is written
    QTest.qWait(50)
    assert win.current_path is None and win.is_dirty()


def test_save_uses_the_current_path_and_a_failed_save_stays_dirty(win, put, wait_until, tmp_path):
    put(("A1", "1"))
    win.save_file(str(tmp_path / "a.sheet"))
    wait_until(lambda: not win.is_dirty())
    put(("A1", "2"))
    wait_until(win.is_dirty)
    win.save_file()  # no argument: the same file again
    wait_until(lambda: not win.is_dirty())
    win.current_path = str(tmp_path / "no" / "such" / "dir.sheet")
    put(("A1", "3"))
    wait_until(win.is_dirty)
    win.save_file()
    wait_until(lambda: win.status_is_error())
    assert win.is_dirty()


def test_save_as_adds_the_extension():
    from sheet.ui.window import with_extension

    assert with_extension("/tmp/budget") == "/tmp/budget.sheet"
    assert with_extension("/tmp/budget.sheet") == "/tmp/budget.sheet"
    assert with_extension("/tmp/data.csv") == "/tmp/data.csv"  # an explicit extension is respected


def test_discarding_unsaved_changes_needs_confirmation(win, put, wait_until, tmp_path):
    put(("A1", "unsaved"))
    wait_until(win.is_dirty)
    answers = []
    win._confirm_discard = lambda: answers.append(1) or False  # the user says "cancel"
    win.new_file()
    other = tmp_path / "o.sheet"
    other.write_text('{"format":"sheet","version":1,"formats":["General"],"cells":[]}\n')
    win.open_file(str(other))
    QTest.qWait(100)
    assert len(answers) == 2 and shown(win, "A1") == "unsaved"  # both refused
    win._confirm_discard = lambda: True
    win.new_file()
    wait_until(lambda: shown(win, "A1") in ("", None))


def test_closing_with_unsaved_changes_can_be_cancelled(engine, qapp, wait_until, call):
    w = MainWindow(engine, interactive=False)
    w.show()
    call("edit", entries=[[0, 0, "x"]])
    wait_until(w.is_dirty)
    w._confirm_discard = lambda: False
    assert w.close() is False
    assert engine.alive
    w._confirm_discard = lambda: True
    assert w.close() is True
    assert not engine.alive


def test_csv_actions_are_separate_from_native_files(win, put, wait_until, tmp_path):
    put(("A1", "1"), ("B1", "=A1+1"))
    path = str(tmp_path / "x.csv")
    win.save_csv(path, "raw")
    wait_until(lambda: (tmp_path / "x.csv").exists())
    assert win.current_path is None  # exporting CSV does not make it the document
    assert "*" in win.windowTitle()  # and does not count as saving it


def test_the_window_tells_the_user_when_a_change_cannot_be_undone(win, wait_until):
    win.model.notice.emit(
        "That change was too large to keep in the undo history, so it cannot be undone"
    )
    assert "cannot be undone" in win.status_text() and not win.status_is_error()


def test_select_all_then_fill_down_is_refused_with_a_clear_message_at_once(win, put, wait_until):
    put(("A1", "=B1+1"))
    win.view.selectAll()
    import time

    start = time.monotonic()
    win.fill_down()
    wait_until(lambda: win.status_is_error() and "limit" in win.status_text())
    assert "250,000" in win.status_text()
    assert time.monotonic() - start < 5  # it used to take ~24 seconds and create a million cells
    assert win.client.alive


def test_selecting_everything_or_a_whole_column_does_not_freeze_the_window(win):
    import time

    # Qt's headers walked every row of a selection to highlight it: ~15 s for select-all on a
    # million-row grid and ~1.5 s for one column, until they were given their own selection.
    for select in (
        win.view.selectAll,
        lambda: win.view.selectColumn(3),
        lambda: win.view.selectRow(5),
    ):
        select()
        start = time.monotonic()
        QTest.qWait(1)
        assert time.monotonic() - start < 1.0, select


def test_clicking_a_header_still_selects_the_whole_column_or_row(win):
    from PyQt6.QtCore import QPoint

    hh, vh = win.view.horizontalHeader(), win.view.verticalHeader()
    QTest.mouseClick(
        hh.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(hh.sectionViewportPosition(2) + 10, 5)
    )
    QTest.qWait(50)
    assert win.selection_rect() == (0, 2, 1_048_575, 2)
    QTest.mouseClick(
        vh.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(5, vh.sectionViewportPosition(4) + 5)
    )
    QTest.qWait(50)
    assert win.selection_rect() == (4, 0, 4, 16_383)


def test_selection_still_paints_for_the_visible_cells(win, put, wait_until):
    put(("A1", "1"), ("B2", "2"))
    select(win, "A1", "B2")
    QTest.qWait(50)
    assert win.selection_rect() == (0, 0, 1, 1)
    assert not win.grab().isNull()  # painting a selection does not fail

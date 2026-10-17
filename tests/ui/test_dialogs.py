"""The modal dialogs, driven with scripted answers (a real dialog would block the test)."""

import time

import pytest
from PyQt6.QtWidgets import QMessageBox

from sheet.ui.names import parse_cell_name
from sheet.ui.window import MainWindow

SAVE, DISCARD, CANCEL = (
    QMessageBox.StandardButton.Save,
    QMessageBox.StandardButton.Discard,
    QMessageBox.StandardButton.Cancel,
)


class Script:
    """Answers for the prompts, and a log of what was asked."""

    def __init__(self):
        self.unsaved = CANCEL
        self.save_path = ""
        self.open_path = ""
        self.text = ("", False)
        self.log = []


@pytest.fixture
def iwin(engine, qapp, wait_until):
    """An interactive window whose prompts are scripted."""
    w = MainWindow(engine, interactive=True)
    script = Script()
    w.script = script

    def ask_unsaved():
        script.log.append("unsaved?")
        return script.unsaved

    def choose_save(title, filter, start=""):
        script.log.append(("save-chooser", title))
        return script.save_path

    def choose_open(title, filter):
        script.log.append(("open-chooser", title))
        return script.open_path

    def ask_text(title, label):
        script.log.append(("text", title))
        return script.text

    w._ask_unsaved = ask_unsaved
    w._choose_save_path = choose_save
    w._choose_open_path = choose_open
    w._ask_text = ask_text
    w._show_warning = lambda text: script.log.append(("warning", text))
    w._show_critical = lambda text: script.log.append(("critical", text))
    w.show()
    wait_until(lambda: "free-threaded" in w.engine_label.text())
    yield w
    w.script.unsaved = DISCARD
    w.close()


def dirty(win, wait_until, name="A1", text="x"):
    row, col = parse_cell_name(name)
    done = []
    win.model.send("edit", entries=[[row, col, text]], on_done=done.append)
    wait_until(lambda: done and win.is_dirty())


def cell_text(win, call, name):
    row, col = parse_cell_name(name)
    return call("cell", at=[row, col])["text"]


# --- the unsaved-changes question ---------------------------------------------------------------


def test_a_clean_document_is_never_questioned(iwin):
    iwin.new_file()
    assert "unsaved?" not in iwin.script.log


def test_discard_lets_new_proceed(iwin, wait_until, call):
    dirty(iwin, wait_until)
    iwin.script.unsaved = DISCARD
    iwin.new_file()
    wait_until(lambda: not iwin.is_dirty() and cell_text(iwin, call, "A1") == "")
    assert iwin.script.log == ["unsaved?"]


def test_cancel_stops_new_open_and_close(iwin, wait_until, call, tmp_path):
    dirty(iwin, wait_until)
    other = tmp_path / "o.sheet"
    other.write_text('{"format":"sheet","version":1,"formats":["General"],"cells":[]}\n')
    iwin.script.unsaved = CANCEL
    iwin.new_file()
    iwin.open_file(str(other))
    assert iwin.close() is False
    assert iwin.script.log.count("unsaved?") == 3
    assert cell_text(iwin, call, "A1") == "x" and iwin.is_dirty()
    assert iwin.client.alive


def test_save_with_a_known_path_saves_then_proceeds(iwin, wait_until, call, tmp_path):
    path = str(tmp_path / "doc.sheet")
    iwin.save_file(path)  # gives the document a path (still unmodified)
    dirty(iwin, wait_until, text="keep me")
    iwin.script.unsaved = SAVE
    iwin.new_file()
    wait_until(lambda: not iwin.is_dirty() and cell_text(iwin, call, "A1") == "")
    assert "keep me" in (tmp_path / "doc.sheet").read_text()
    assert not any(
        entry[0] == "save-chooser" for entry in iwin.script.log if isinstance(entry, tuple)
    )


def test_save_without_a_path_asks_where_and_adds_the_extension(iwin, wait_until, call, tmp_path):
    dirty(iwin, wait_until, text="fresh")
    iwin.script.unsaved = SAVE
    iwin.script.save_path = str(tmp_path / "typed-without-extension")
    iwin.new_file()
    wait_until(lambda: not iwin.is_dirty() and cell_text(iwin, call, "A1") == "")
    assert "fresh" in (tmp_path / "typed-without-extension.sheet").read_text()


def test_cancelling_the_save_chooser_cancels_the_whole_action(iwin, wait_until, call):
    dirty(iwin, wait_until)
    iwin.script.unsaved = SAVE
    iwin.script.save_path = ""  # the user backs out of "Save As"
    iwin.new_file()
    assert cell_text(iwin, call, "A1") == "x" and iwin.is_dirty()


def test_a_failed_save_cancels_the_action_and_the_user_is_told(iwin, wait_until, call, tmp_path):
    dirty(iwin, wait_until)
    iwin.script.unsaved = SAVE
    iwin.script.save_path = str(tmp_path / "no" / "such" / "dir.sheet")
    iwin.new_file()
    wait_until(lambda: any(e[0] == "warning" for e in iwin.script.log if isinstance(e, tuple)))
    assert cell_text(iwin, call, "A1") == "x" and iwin.is_dirty()
    assert iwin.status_is_error()


def test_the_engine_dying_during_a_save_does_not_hang_the_window(iwin, wait_until, tmp_path):
    dirty(iwin, wait_until)
    iwin.script.unsaved = SAVE
    iwin.script.save_path = str(tmp_path / "x.sheet")
    iwin.client._process.kill()
    start = time.monotonic()
    iwin.new_file()
    assert time.monotonic() - start < 15
    assert iwin.is_dirty()  # nothing was saved or discarded


# --- closing --------------------------------------------------------------------------------------


def test_closing_with_discard_closes_and_stops_the_engine(engine, qapp, wait_until):
    w = MainWindow(engine, interactive=True)
    w._ask_unsaved = lambda: DISCARD
    w.show()
    dirty(w, wait_until)
    assert w.close() is True
    assert not engine.alive


def test_closing_with_save_saves_first(engine, qapp, wait_until, tmp_path):
    w = MainWindow(engine, interactive=True)
    w._ask_unsaved = lambda: SAVE
    w._choose_save_path = lambda title, filter, start="": str(tmp_path / "closing.sheet")
    w.show()
    dirty(w, wait_until, text="saved on close")
    assert w.close() is True
    assert "saved on close" in (tmp_path / "closing.sheet").read_text()


# --- the file dialogs -------------------------------------------------------------------------------


def test_open_dialog_opens_the_chosen_file_or_does_nothing_if_cancelled(
    iwin, wait_until, call, tmp_path
):
    f = tmp_path / "o.sheet"
    f.write_text('{"format":"sheet","version":1,"formats":["General"],"cells":[[0,0,"hello"]]}\n')
    iwin.script.open_path = ""
    iwin.open_dialog()
    assert iwin.current_path is None
    iwin.script.open_path = str(f)
    iwin.open_dialog()
    wait_until(lambda: cell_text(iwin, call, "A1") == "hello" and iwin.current_path == str(f))


def test_save_as_dialog_saves_under_the_chosen_name(iwin, wait_until, tmp_path):
    iwin.script.save_path = ""
    iwin.save_as_dialog()
    assert iwin.current_path is None
    iwin.script.save_path = str(tmp_path / "named")
    iwin.save_as_dialog()
    wait_until(lambda: iwin.current_path == str(tmp_path / "named.sheet"))
    assert (tmp_path / "named.sheet").exists()


def test_csv_dialogs_import_and_export(iwin, wait_until, call, tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("1,2\r\n3,=A1+B1\r\n")
    iwin.script.open_path = str(src)
    iwin.import_csv_dialog()
    wait_until(lambda: cell_text(iwin, call, "B2") == "=A1+B1")
    out = tmp_path / "out.csv"
    iwin.script.save_path = str(out)
    iwin.export_csv_dialog("display")
    wait_until(lambda: out.exists())
    assert out.read_bytes() == b"1,2\r\n3,3\r\n"  # B2 = A1+B1 = 1+2
    iwin.script.open_path = iwin.script.save_path = ""  # cancelled: nothing more happens
    iwin.import_csv_dialog()
    iwin.export_csv_dialog("raw")


# --- custom format and warnings -----------------------------------------------------------------------


def test_custom_format_applies_cancels_and_reports_bad_formats(iwin, wait_until, call):
    dirty(iwin, wait_until, text="1234.5")
    iwin.script.text = ("", False)
    iwin.custom_format()
    iwin.script.text = ("0.0", False)  # the user pressed Cancel
    iwin.custom_format()
    assert call("cell", at=[0, 0])["format"] == "General"
    iwin.script.text = ("#,##0.0", True)
    iwin.custom_format()
    wait_until(lambda: call("cell", at=[0, 0])["format"] == "#,##0.0")
    iwin.script.text = ("QQ", True)
    iwin.custom_format()
    wait_until(lambda: iwin.status_is_error() and "format" in iwin.status_text().lower())


def test_file_and_csv_errors_show_a_warning_dialog_but_syntax_errors_do_not(iwin):
    for kind in ("csv", "io", "file"):
        iwin._report_error(kind, f"problem {kind}")
    iwin._report_error("syntax", "bad formula")
    warnings = [e[1] for e in iwin.script.log if isinstance(e, tuple) and e[0] == "warning"]
    assert warnings == ["problem csv", "problem io", "problem file"]


def test_an_engine_failure_shows_one_critical_dialog_and_locks_the_window(iwin, wait_until):
    iwin.client._process.kill()
    wait_until(lambda: not iwin.view.isEnabled())
    assert [e for e in iwin.script.log if isinstance(e, tuple) and e[0] == "critical"]


def test_prompts_are_inert_in_a_non_interactive_window(engine, qapp):
    win = MainWindow(engine, interactive=False)
    assert win._choose_save_path("t", "f") == "" and win._choose_open_path("t", "f") == ""
    assert win._ask_text("t", "l") == ("", False)
    win._show_warning("x")
    win._show_critical("y")
    win.close()

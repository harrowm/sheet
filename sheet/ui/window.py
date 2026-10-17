"""The main window: grid, formula bar, name box, menus and status bar.

The window owns no spreadsheet data. Every command is a request to the engine process, and
the grid shows whatever the model has cached from it.
"""

from __future__ import annotations

import os
from typing import Any

from PyQt6.QtCore import QEvent, QEventLoop, QModelIndex, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QKeyEvent, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from sheet.ui.client import EngineClient
from sheet.ui.model import SheetModel
from sheet.ui.names import cell_name, parse_cell_name

BUSY_DELAY_MS = 150  # show "Calculating..." only if the engine takes longer than this
MESSAGE_MS = 8000
SAVE_WAIT_CEILING_MS = 60_000  # a save that has not been answered after this long is abandoned

FORMATS = [
    ("General", "General"),
    ("Number (0.00)", "0.00"),
    ("Thousands (#,##0.00)", "#,##0.00"),
    ("Currency ($#,##0.00)", "$#,##0.00"),
    ("Percent (0%)", "0%"),
    ("Percent (0.0%)", "0.0%"),
    ("Date (DD/MM/YYYY)", "DD/MM/YYYY"),
    ("Date (YYYY-MM-DD)", "YYYY-MM-DD"),
    ("Time (HH:mm:SS)", "HH:mm:SS"),
]

Rect = tuple[int, int, int, int]

FILE_FILTER = "Sheet files (*.sheet);;All files (*)"
FILE_SUFFIX = ".sheet"


def with_extension(path: str) -> str:
    """Add ``.sheet`` to a name typed without any extension; keep an explicit one."""
    return path if os.path.splitext(path)[1] else path + FILE_SUFFIX


class CellDelegate(QStyledItemDelegate):
    """Default cell editing, plus a signal when Enter commits an edit (to move down)."""

    enter_pressed = pyqtSignal(int, int)

    def createEditor(
        self, parent: QWidget | None, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget | None:
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            editor.setProperty("cell", [index.row(), index.column()])
        return editor

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        is_enter = (
            isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        )
        handled = super().eventFilter(obj, event)
        if is_enter and obj is not None and (cell := obj.property("cell")):
            self.enter_pressed.emit(cell[0], cell[1])
        return handled


class FormulaBar(QLineEdit):
    """The formula bar; Escape abandons the edit (a plain QLineEdit ignores that key)."""

    escaped = pyqtSignal()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, client: EngineClient, interactive: bool = True) -> None:
        super().__init__()
        self.client = client
        self.interactive = interactive  # False in tests: no modal dialogs
        self.model = SheetModel(client, parent=self)
        self._copied: str | None = None  # text last copied inside the app (see paste)
        self._formula_dirty = False
        self.current_path: str | None = None  # the document's file; None until saved or opened
        self._saved_version = client.version  # engine version when last saved or loaded
        self._busy = False
        self._status_error = False
        self.actions_by_name: dict[str, QAction] = {}

        self.resize(1100, 700)
        self._build_widgets()
        self._build_actions()
        self._build_menus()
        self._connect()
        self._update_undo_actions()
        self._update_title()
        if client.hello:
            self._on_ready(client.hello)
        self._refresh_bars()

    # --- construction ------------------------------------------------------------------

    def _build_widgets(self) -> None:
        self.name_box = QLineEdit()
        self.name_box.setFixedWidth(90)
        self.formula_bar = FormulaBar()
        self.formula_bar.setPlaceholderText("Enter a value or a formula (=SUM(A1:A10))")
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 4, 4, 4)
        bar.addWidget(self.name_box)
        bar.addWidget(QLabel("fx"))
        bar.addWidget(self.formula_bar, 1)

        self.view = QTableView()
        self.view.setModel(self.model)
        self.delegate = CellDelegate(self.view)
        self.view.setItemDelegate(self.delegate)
        self.view.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.view.setHorizontalScrollMode(QTableView.ScrollMode.ScrollPerPixel)
        self.view.setVerticalScrollMode(QTableView.ScrollMode.ScrollPerPixel)
        self.view.horizontalHeader().setDefaultSectionSize(90)
        self.view.verticalHeader().setDefaultSectionSize(22)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        for header in (self.view.horizontalHeader(), self.view.verticalHeader()):
            header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            header.customContextMenuRequested.connect(self._show_context_menu_at_header)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)
        self.setCentralWidget(central)

        self.status_label = QLabel()
        self.busy_label = QLabel("Calculating…")
        self.busy_label.hide()
        self.engine_label = QLabel("Engine: starting…")
        status = self.statusBar()
        status.addWidget(self.status_label, 1)
        status.addPermanentWidget(self.busy_label)
        status.addPermanentWidget(self.engine_label)
        self._message_timer = self._single_shot_timer(MESSAGE_MS, lambda: self.show_message(""))
        self._busy_timer = self._single_shot_timer(BUSY_DELAY_MS, self._show_busy)

    def _single_shot_timer(self, interval_ms: int, slot: Any) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval_ms)
        timer.timeout.connect(slot)
        return timer

    def _action(self, name: str, text: str, slot: Any, shortcut: Any = None) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(lambda checked=False: slot())
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        self.addAction(action)
        self.actions_by_name[name] = action
        return action

    def _build_actions(self) -> None:
        a = self._action
        a("new", "&New", self.new_file, QKeySequence.StandardKey.New)
        a("open_file", "&Open…", self.open_dialog, QKeySequence.StandardKey.Open)
        a("save", "&Save", self.save_file, QKeySequence.StandardKey.Save)
        a("save_as", "Save &As…", self.save_as_dialog, QKeySequence.StandardKey.SaveAs)
        a("import_csv", "&Import CSV…", self.import_csv_dialog)
        a("export_values", "Export CSV (&values)…", lambda: self.export_csv_dialog("display"))
        a("export_raw", "Export CSV (&formulas)…", lambda: self.export_csv_dialog("raw"))
        a("quit", "&Quit", self.close, QKeySequence.StandardKey.Quit)
        a("undo", "&Undo", self.undo, QKeySequence.StandardKey.Undo)
        a("redo", "&Redo", self.redo, QKeySequence.StandardKey.Redo)
        a("copy", "&Copy", self.copy, QKeySequence.StandardKey.Copy)
        a("paste", "&Paste", self.paste, QKeySequence.StandardKey.Paste)
        a("paste_values", "Paste &Values", self.paste_values, "Ctrl+Shift+V")
        a("clear", "C&lear", self.clear_selection, Qt.Key.Key_Delete)
        a("fill_down", "Fill &Down", self.fill_down, "Ctrl+D")
        a("fill_right", "Fill &Right", self.fill_right, "Ctrl+R")
        a("insert_rows", "Insert &Row", lambda: self.structure("insert_rows"))
        a("delete_rows", "Delete R&ow", lambda: self.structure("delete_rows"))
        a("insert_cols", "Insert &Column", lambda: self.structure("insert_cols"))
        a("delete_cols", "Delete C&olumn", lambda: self.structure("delete_cols"))
        for label, fmt in FORMATS:
            a(f"format:{fmt}", label, lambda fmt=fmt: self.apply_format(fmt))
        a("format_custom", "Custom format…", self.custom_format)

    def _build_menus(self) -> None:
        names = self.actions_by_name
        bar = self.menuBar()
        assert bar is not None
        file_menu, edit, fmt = bar.addMenu("&File"), bar.addMenu("&Edit"), bar.addMenu("F&ormat")
        assert file_menu is not None and edit is not None and fmt is not None
        for n in ("new", "open_file", "save", "save_as"):
            file_menu.addAction(names[n])
        file_menu.addSeparator()
        for n in ("import_csv", "export_values", "export_raw"):
            file_menu.addAction(names[n])
        file_menu.addSeparator()
        file_menu.addAction(names["quit"])
        for n in ("undo", "redo"):
            edit.addAction(names[n])
        edit.addSeparator()
        for n in ("copy", "paste", "paste_values", "clear", "fill_down", "fill_right"):
            edit.addAction(names[n])
        edit.addSeparator()
        for n in ("insert_rows", "delete_rows", "insert_cols", "delete_cols"):
            edit.addAction(names[n])
        self._add_format_menu(fmt)

    def _add_format_menu(self, menu: QMenu) -> None:
        for _, fmt in FORMATS:
            menu.addAction(self.actions_by_name[f"format:{fmt}"])
        menu.addSeparator()
        menu.addAction(self.actions_by_name["format_custom"])

    def build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        names = self.actions_by_name
        for n in ("copy", "paste", "paste_values", "clear"):
            menu.addAction(names[n])
        menu.addSeparator()
        for n in ("insert_rows", "delete_rows", "insert_cols", "delete_cols"):
            menu.addAction(names[n])
        menu.addSeparator()
        sub = menu.addMenu("Format")
        assert sub is not None
        self._add_format_menu(sub)
        return menu

    def _show_context_menu_at_header(self, pos: QPoint) -> None:
        header = self.sender()
        self.build_context_menu().exec(header.mapToGlobal(pos))

    def _connect(self) -> None:
        self.view.selectionModel().currentChanged.connect(lambda *_: self._on_current_changed())
        self.view.customContextMenuRequested.connect(
            lambda pos: self.build_context_menu().exec(self.view.viewport().mapToGlobal(pos))
        )
        self.model.dataChanged.connect(lambda *_: self._load_formula_bar())
        self.model.error.connect(self._on_model_error)
        self.model.notice.connect(self.show_message)
        self.name_box.returnPressed.connect(self._name_box_entered)
        self.formula_bar.returnPressed.connect(self.commit_formula_bar)
        self.formula_bar.escaped.connect(self._abandon_formula_edit)
        self.formula_bar.textEdited.connect(lambda _: setattr(self, "_formula_dirty", True))
        self.delegate.enter_pressed.connect(self._move_down)
        self.client.ready.connect(self._on_ready)
        self.client.failed.connect(self._on_engine_failed)
        self.client.state_changed.connect(self._update_undo_actions)
        self.client.state_changed.connect(self._update_title)
        self.model.modelReset.connect(self._on_model_reset)
        self.client.busy_changed.connect(self._on_busy)

    # --- status ------------------------------------------------------------------------

    def show_message(self, text: str, error: bool = False) -> None:
        self._status_error = error and bool(text)
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #c62828;" if self._status_error else "")
        self._message_timer.start() if text else self._message_timer.stop()

    def status_text(self) -> str:
        return self.status_label.text()

    def status_is_error(self) -> bool:
        return self._status_error

    def _on_ready(self, hello: dict[str, Any]) -> None:
        if hello.get("gil"):
            self.engine_label.setText("Engine: GIL on (no parallel speed-up)")
        else:
            self.engine_label.setText("Engine: free-threaded Python (GIL off)")

    def _on_engine_failed(self, message: str) -> None:
        self.show_message(f"Engine error: {message}", error=True)
        self.engine_label.setText("Engine: stopped")
        self.view.setEnabled(False)
        self.formula_bar.setEnabled(False)
        self.name_box.setEnabled(False)
        for action in self.actions_by_name.values():
            action.setEnabled(False)
        self._show_critical(f"The calculation engine stopped.\n\n{message}")

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
            self.busy_label.hide()

    def _show_busy(self) -> None:
        if self._busy:
            self.busy_label.show()

    def _update_undo_actions(self) -> None:
        self.actions_by_name["undo"].setEnabled(self.client.can_undo)
        self.actions_by_name["redo"].setEnabled(self.client.can_redo)

    def _on_model_error(self, kind: str, message: str) -> None:
        self._report_error(kind, message)

    def _report_error(self, kind: str, message: str) -> None:
        text = {
            "syntax": f"Syntax error in formula: {message}",
            "circular": "Circular reference: that edit would make a cell depend on itself",
            "format": f"Invalid format: {message}",
            "file": f"Not a valid sheet file: {message}",
            "engine": f"Engine error: {message}",
        }.get(kind, message)
        self.show_message(text, error=True)
        if kind in ("csv", "io", "file"):
            self._show_warning(message)

    # --- cursor, name box and formula bar ----------------------------------------------

    def _current(self) -> tuple[int, int]:
        index = self.view.currentIndex()
        return (index.row(), index.column()) if index.isValid() else (0, 0)

    def goto(self, row: int, col: int) -> None:
        index = self.model.index(row, col)
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index)
        self._refresh_bars()

    def _on_current_changed(self) -> None:
        self._formula_dirty = False
        self._refresh_bars()

    def _refresh_bars(self) -> None:
        self.name_box.setText(cell_name(*self._current()))
        self._load_formula_bar()

    def _load_formula_bar(self) -> None:
        if self._formula_dirty:
            return
        text = self.model.data(
            self.view.currentIndex()
            if self.view.currentIndex().isValid()
            else self.model.index(0, 0),
            Qt.ItemDataRole.EditRole,
        )
        if text != self.formula_bar.text():
            self.formula_bar.setText(text)

    def _name_box_entered(self) -> None:
        target = parse_cell_name(self.name_box.text())
        if target is None:
            self.show_message(f"“{self.name_box.text()}” is not a cell name (try B7)", error=True)
            self.name_box.setText(cell_name(*self._current()))
            return
        self.goto(*target)
        self.view.setFocus()

    def commit_formula_bar(self) -> None:
        row, col = self._current()
        self.model.setData(self.model.index(row, col), self.formula_bar.text())
        self._formula_dirty = False
        self.view.setFocus()

    def _abandon_formula_edit(self) -> None:
        self._formula_dirty = False
        self._load_formula_bar()
        self.view.setFocus()

    def _move_down(self, row: int, col: int) -> None:
        QTimer.singleShot(0, lambda: self.goto(min(row + 1, self.model.rowCount() - 1), col))

    # --- selection ---------------------------------------------------------------------

    def selection_ranges(self) -> list[Rect]:
        ranges = [
            (r.top(), r.left(), r.bottom(), r.right())
            for r in self.view.selectionModel().selection()
        ]
        if ranges:
            return ranges
        row, col = self._current()
        return [(row, col, row, col)]

    def selection_rect(self) -> Rect:
        return self.selection_ranges()[-1]

    # --- commands ----------------------------------------------------------------------

    def undo(self) -> None:
        self.model.send("undo")

    def redo(self) -> None:
        self.model.send("redo")

    def copy(self) -> None:
        def done(response: dict[str, Any]) -> None:
            if response.get("ok"):
                QApplication.clipboard().setText(response["text"])
                self._copied = response["text"]
                self.show_message("Copied")
            else:
                self._report_error(response["error"]["kind"], response["error"]["message"])

        self.client.request("copy", done, rect=list(self.selection_rect()))

    def _paste(self, values_only: bool) -> None:
        row, col, _, _ = self.selection_rect()
        text = QApplication.clipboard().text()
        if self._copied is not None and text == self._copied:
            self.model.send("paste", at=[row, col], values_only=values_only)
        elif text:
            self.model.send("paste_text", at=[row, col], text=text)
        else:
            self.show_message("The clipboard is empty")

    def paste(self) -> None:
        self._paste(False)

    def paste_values(self) -> None:
        self._paste(True)

    def clear_selection(self) -> None:
        for rect in self.selection_ranges():
            self.model.send("clear", rect=list(rect))

    def fill_down(self) -> None:
        r1, c1, r2, c2 = self.selection_rect()
        if r1 == r2:
            self.show_message("Select more than one row to fill down")
            return
        self.model.send("fill", rect=[r1, c1, r2, c2], direction="down")

    def fill_right(self) -> None:
        r1, c1, r2, c2 = self.selection_rect()
        if c1 == c2:
            self.show_message("Select more than one column to fill right")
            return
        self.model.send("fill", rect=[r1, c1, r2, c2], direction="right")

    def apply_format(self, fmt: str) -> None:
        self.model.send("format", rect=list(self.selection_rect()), fmt=fmt)

    def custom_format(self) -> None:
        text, ok = self._ask_text("Custom format", "Format (for example #,##0.00 or DD/MM/YYYY):")
        if ok and text:
            self.apply_format(text)

    def structure(self, op: str) -> None:
        r1, c1, r2, c2 = self.selection_rect()
        if op.endswith("rows"):
            self.model.send(op, at=r1, count=r2 - r1 + 1)
        else:
            self.model.send(op, at=c1, count=c2 - c1 + 1)

    # --- prompts -----------------------------------------------------------------------
    # Every modal dialog lives in one of these small methods so tests can script the answers.
    # A window created with interactive=False never opens a dialog: it behaves as if cancelled.

    def _ask_unsaved(self) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("This document has unsaved changes.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        return QMessageBox.StandardButton(box.exec())

    def _choose_open_path(self, title: str, file_filter: str) -> str:
        if not self.interactive:
            return ""
        return QFileDialog.getOpenFileName(self, title, "", file_filter)[0]

    def _choose_save_path(self, title: str, file_filter: str, start: str = "") -> str:
        if not self.interactive:
            return ""
        return QFileDialog.getSaveFileName(self, title, start, file_filter)[0]

    def _ask_text(self, title: str, label: str) -> tuple[str, bool]:
        if not self.interactive:
            return "", False
        return QInputDialog.getText(self, title, label)

    def _show_warning(self, text: str) -> None:
        if self.interactive:
            QMessageBox.warning(self, "Sheet", text)

    def _show_critical(self, text: str) -> None:
        if self.interactive:
            QMessageBox.critical(self, "Sheet", text)

    # --- documents ---------------------------------------------------------------------

    def is_dirty(self) -> bool:
        """True if the engine's document has changed since it was last saved or loaded."""
        return self.client.version != self._saved_version

    def _update_title(self) -> None:
        name = os.path.basename(self.current_path) if self.current_path else "Untitled"
        self.setWindowTitle(f"{name}{' *' if self.is_dirty() else ''} - Sheet")

    def _on_model_reset(self) -> None:
        self._formula_dirty = False
        self.view.setCurrentIndex(self.model.index(0, 0))
        self._refresh_bars()

    def _confirm_discard(self) -> bool:
        """Ask before throwing away unsaved changes. False means the user cancelled."""
        if not self.is_dirty() or not self.interactive:
            return True
        choice = self._ask_unsaved()
        if choice == QMessageBox.StandardButton.Save:
            return self._save_and_wait()
        return choice == QMessageBox.StandardButton.Discard

    def _save_and_wait(self) -> bool:
        """Save now, running a local event loop until the engine answers.

        False if the user cancelled the file chooser, the save failed, or the engine died
        (a failed request is answered with an error, so the loop always ends; the timer is
        a last-resort ceiling).
        """
        path = self.current_path
        if path is None:
            chosen = self._choose_save_path("Save", FILE_FILTER)
            if not chosen:
                return False
            path = with_extension(chosen)
        loop, result = QEventLoop(self), []

        def done(response: dict[str, Any]) -> None:
            result.append(self._saved(path, response))
            loop.quit()

        QTimer.singleShot(SAVE_WAIT_CEILING_MS, loop.quit)
        self.client.request("save", done, path=path)
        loop.exec()
        return bool(result and result[0])

    def new_file(self) -> None:
        if not self._confirm_discard():
            return
        self.model.send("new", on_done=lambda response: self._document_replaced(None, response))

    def open_dialog(self) -> None:
        path = self._choose_open_path("Open", FILE_FILTER)
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        if not self._confirm_discard():
            return

        def done(response: dict[str, Any]) -> None:
            if self._document_replaced(path, response):
                self.show_message(f"Opened {path} ({response['cells']} cells)")

        self.model.send("load", on_done=done, path=path)

    def _document_replaced(self, path: str | None, response: dict[str, Any]) -> bool:
        if not response.get("ok"):
            return False  # the model has reported the error; the old document is untouched
        self.current_path = path
        self._saved_version = response["version"]
        self._update_title()
        return True

    def save_as_dialog(self) -> None:
        path = self._choose_save_path("Save As", FILE_FILTER, self.current_path or "")
        if path:
            self.save_file(with_extension(path))

    def save_file(self, path: str | None = None) -> None:
        """Save to ``path``, or to the document's file; ask for a name if it has none."""
        path = path or self.current_path
        if path is None:
            self.save_as_dialog()
            return
        self.client.request("save", lambda response: self._saved(path, response), path=path)

    def _saved(self, path: str, response: dict[str, Any]) -> bool:
        if not response.get("ok"):
            self._report_error(response["error"]["kind"], response["error"]["message"])
            return False
        self.current_path = path
        self._saved_version = response["version"]
        self._update_title()
        self.show_message(f"Saved {response['cells']} cells to {path}")
        return True

    # --- csv ---------------------------------------------------------------------------

    def import_csv_dialog(self) -> None:
        path = self._choose_open_path("Import CSV", "CSV files (*.csv);;All files (*)")
        if path:
            self.open_csv(path)

    def export_csv_dialog(self, mode: str) -> None:
        path = self._choose_save_path("Export CSV", "CSV files (*.csv)")
        if path:
            self.save_csv(path, mode)

    def open_csv(self, path: str) -> None:
        row, col = self._current()

        def done(response: dict[str, Any]) -> None:
            if response.get("ok"):
                self.show_message(f"Imported {path}")

        self.model.send("import_csv", on_done=done, path=path, at=[row, col])

    def save_csv(self, path: str, mode: str = "display") -> None:
        def done(response: dict[str, Any]) -> None:
            if response.get("ok"):
                self.show_message(f"Exported {response['rows']} rows to {path}")
            else:
                self._report_error(response["error"]["kind"], response["error"]["message"])

        self.client.request("export_csv", done, path=path, mode=mode)

    # --- closing -----------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if not self._confirm_discard():
            if event is not None:
                event.ignore()
            return
        self.client.shutdown()
        if event is not None:
            event.accept()

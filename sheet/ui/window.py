"""The main window: grid, formula bar, name box, menus and status bar.

The window owns no spreadsheet data. Every command is a request to the engine process, and
the grid shows whatever the model has cached from it.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt, QTimer, pyqtSignal
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
        self._busy = False
        self._status_error = False
        self.actions_by_name: dict[str, QAction] = {}

        self.setWindowTitle("Sheet")
        self.resize(1100, 700)
        self._build_widgets()
        self._build_actions()
        self._build_menus()
        self._connect()
        self._update_undo_actions()
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
        a("open", "&Open CSV…", self.open_csv_dialog, QKeySequence.StandardKey.Open)
        a(
            "save_values",
            "Save CSV (&values)…",
            lambda: self.save_csv_dialog("display"),
            QKeySequence.StandardKey.Save,
        )
        a("save_raw", "Save CSV (&formulas)…", lambda: self.save_csv_dialog("raw"))
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
        for n in ("open", "save_values", "save_raw"):
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
        self.name_box.returnPressed.connect(self._name_box_entered)
        self.formula_bar.returnPressed.connect(self.commit_formula_bar)
        self.formula_bar.escaped.connect(self._abandon_formula_edit)
        self.formula_bar.textEdited.connect(lambda _: setattr(self, "_formula_dirty", True))
        self.delegate.enter_pressed.connect(self._move_down)
        self.client.ready.connect(self._on_ready)
        self.client.failed.connect(self._on_engine_failed)
        self.client.state_changed.connect(self._update_undo_actions)
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
        if self.interactive:
            QMessageBox.critical(self, "Sheet", f"The calculation engine stopped.\n\n{message}")

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
            "engine": f"Engine error: {message}",
        }.get(kind, message)
        self.show_message(text, error=True)
        if self.interactive and kind in ("csv", "io"):
            QMessageBox.warning(self, "Sheet", message)

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
        if not self.interactive:
            return
        text, ok = QInputDialog.getText(
            self, "Custom format", "Format (for example #,##0.00 or DD/MM/YYYY):"
        )
        if ok and text:
            self.apply_format(text)

    def structure(self, op: str) -> None:
        r1, c1, r2, c2 = self.selection_rect()
        if op.endswith("rows"):
            self.model.send(op, at=r1, count=r2 - r1 + 1)
        else:
            self.model.send(op, at=c1, count=c2 - c1 + 1)

    # --- files -------------------------------------------------------------------------

    def open_csv_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open CSV", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self.open_csv(path)

    def save_csv_dialog(self, mode: str) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "", "CSV files (*.csv)")
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
                self.show_message(f"Saved {response['rows']} rows to {path}")
            else:
                self._report_error(response["error"]["kind"], response["error"]["message"])

        self.client.request("export_csv", done, path=path, mode=mode)

    # --- closing -----------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self.client.shutdown()
        if event is not None:
            event.accept()

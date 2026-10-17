"""Table model over the engine, with a block cache so ``data()`` never blocks.

Qt asks for cells one at a time and expects an immediate answer, but the engine is another
process. The grid is therefore cached in blocks of ``BLOCK_ROWS x BLOCK_COLS`` cells:
``data()`` answers from the cache (blank if the block has not arrived yet) and asks the
engine for any missing block; when it arrives the view is told to repaint. Edits go to the
engine, and the blocks that contain changed cells are refetched.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from functools import partial
from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor

from sheet.ui.client import EngineClient
from sheet.ui.names import MAX_COLS, MAX_ROWS, column_name

BLOCK_ROWS = 64
BLOCK_COLS = 16
DEFAULT_MAX_BLOCKS = 600

_ERROR_BRUSH = QBrush(QColor("#c62828"))
_ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_ALIGN_CENTER = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter

Item = tuple[str, str, str]  # (display text, kind n/s/b/e, raw text)
Block = list[list[Item | None]]
Key = tuple[int, int]


class SheetModel(QAbstractTableModel):
    error = pyqtSignal(str, str)  # (kind, message) for a rejected request
    changed = pyqtSignal(object)  # bounds [r1, c1, r2, c2] of a successful change

    def __init__(
        self,
        client: EngineClient,
        max_blocks: int = DEFAULT_MAX_BLOCKS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._max_blocks = max_blocks
        self._blocks: OrderedDict[Key, Block] = OrderedDict()
        self._pending: set[Key] = set()
        self._refetch: set[Key] = set()  # invalidated while a fetch was in flight

    @property
    def cached_blocks(self) -> int:
        return len(self._blocks)

    # --- model interface ---------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else MAX_ROWS

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else MAX_COLS

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                column_name(section)
                if orientation == Qt.Orientation.Horizontal
                else str(section + 1)
            )
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        item = self.item_at(index.row(), index.column())
        if role == Qt.ItemDataRole.DisplayRole:
            return item[0] if item else ""
        if role == Qt.ItemDataRole.EditRole:
            return item[2] if item else ""
        if role == Qt.ItemDataRole.TextAlignmentRole:
            kind = item[1] if item else "s"
            return _ALIGN_RIGHT if kind == "n" else _ALIGN_CENTER if kind in "be" else _ALIGN_LEFT
        if role == Qt.ItemDataRole.ForegroundRole and item and item[1] == "e":
            return _ERROR_BRUSH
        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        self.send("edit", entries=[[index.row(), index.column(), str(value)]])
        return True  # the engine applies it asynchronously; the view refreshes when it answers

    # --- cache -------------------------------------------------------------------------

    def item_at(self, row: int, col: int) -> Item | None:
        """The cached cell, requesting its block if it is not loaded yet."""
        key = (row // BLOCK_ROWS, col // BLOCK_COLS)
        block = self._blocks.get(key)
        if block is None:
            self._fetch(key)
            return None
        return block[row % BLOCK_ROWS][col % BLOCK_COLS]

    def _block_rect(self, key: Key) -> list[int]:
        r1, c1 = key[0] * BLOCK_ROWS, key[1] * BLOCK_COLS
        return [r1, c1, min(r1 + BLOCK_ROWS, MAX_ROWS) - 1, min(c1 + BLOCK_COLS, MAX_COLS) - 1]

    def _fetch(self, key: Key) -> None:
        if key in self._pending:
            return
        self._pending.add(key)
        self._client.request("view", partial(self._on_block, key), rect=self._block_rect(key))

    def _on_block(self, key: Key, response: dict[str, Any]) -> None:
        self._pending.discard(key)
        if response.get("ok"):
            block: Block = [
                [None if c is None else (c[0], c[1], c[2] if len(c) > 2 else c[0]) for c in row]
                for row in response["cells"]
            ]
            self._blocks[key] = block
            self._blocks.move_to_end(key)
            while len(self._blocks) > self._max_blocks:
                self._blocks.popitem(last=False)
            r1, c1, r2, c2 = self._block_rect(key)
            self.dataChanged.emit(self.index(r1, c1), self.index(r2, c2))
        if key in self._refetch:  # the data was out of date by the time it arrived
            self._refetch.discard(key)
            self._fetch(key)

    def reset_cache(self) -> None:
        """Forget everything cached (a different document was loaded)."""
        self.beginResetModel()
        self._blocks.clear()
        self._pending.clear()
        self._refetch.clear()
        self.endResetModel()

    def _invalidate(self, bounds: list[int] | None) -> None:
        """Refetch cached blocks that overlap ``bounds`` (the old data shows until then)."""
        if bounds is None:
            return
        r1, c1, r2, c2 = bounds
        rows = range(r1 // BLOCK_ROWS, r2 // BLOCK_ROWS + 1)
        cols = range(c1 // BLOCK_COLS, c2 // BLOCK_COLS + 1)
        if len(rows) * len(cols) <= len(self._blocks) + len(self._pending):
            keys = [(br, bc) for br in rows for bc in cols]
        else:  # a huge change: look at what is cached instead
            keys = [k for k in (*self._blocks, *self._pending) if k[0] in rows and k[1] in cols]
        for key in keys:
            if key in self._pending:
                self._refetch.add(key)
            elif key in self._blocks:
                self._fetch(key)

    # --- requests that change the sheet ------------------------------------------------

    def send(
        self, op: str, *, on_done: Callable[[dict[str, Any]], object] | None = None, **args: Any
    ) -> None:
        """Send a request that may change cells; refresh and report when it is answered.

        ``on_done(response)``, if given, is called after the model has handled the answer.
        """

        def handle(response: dict[str, Any]) -> None:
            self._on_result(response)
            if on_done is not None:
                on_done(response)

        self._client.request(op, handle, **args)

    def _on_result(self, response: dict[str, Any]) -> None:
        if not response.get("ok"):
            err = response.get("error", {})
            self.error.emit(err.get("kind", "internal"), err.get("message", "request failed"))
            return
        if response.get("reset"):  # a whole new document: nothing cached is valid
            self.reset_cache()
            self.changed.emit(None)
            return
        bounds = response.get("bounds")
        self._invalidate(bounds)
        if bounds is not None:
            self.changed.emit(bounds)

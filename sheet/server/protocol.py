"""The engine's request/response protocol, independent of how messages travel.

A request is a dict with an ``op`` (and an optional ``id`` that is echoed back); a response
is a dict with ``ok``. Failures are ``{"ok": false, "error": {"kind", "message"}}`` and
never raise, so a bad request cannot take the engine down. Every successful response also
carries the document ``version`` (bumped whenever anything changes) and the undo/redo
availability, so a client can tell when cached data is stale without asking.

Cell positions are 0-based ``[row, col]``; rectangles are ``[r1, c1, r2, c2]`` (inclusive).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from sheet.engine.csv_io import CsvError, export_csv, import_csv
from sheet.engine.files import FileFormatError, load_sheet, save_sheet
from sheet.engine.formats import FormatError, format_value
from sheet.engine.graph import CycleError
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.parser import MAX_COLS, MAX_ROWS
from sheet.engine.sheet import Clipboard, Pos, Sheet
from sheet.engine.values import ErrorValue

MAX_VIEW_CELLS = 20_000  # largest viewport a client may request
CHANGED_LIMIT = 5_000  # above this a response gives only the bounds of what changed

Request = dict[str, Any]
Response = dict[str, Any]


class RequestError(Exception):
    """A request that cannot be carried out; ``kind`` tells the client why."""

    def __init__(self, kind: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.extra = extra


def _invalid(message: str) -> RequestError:
    return RequestError("invalid", message)


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{what} must be an integer")
    return value


def _pos(request: Request, key: str) -> Pos:
    value = request.get(key)
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise _invalid(f"{key} must be [row, col]")
    return _int(value[0], key), _int(value[1], key)


def _rect(request: Request, key: str = "rect") -> tuple[int, int, int, int]:
    value = request.get(key)
    if not isinstance(value, list | tuple) or len(value) != 4:
        raise _invalid(f"{key} must be [r1, c1, r2, c2]")
    r1, c1, r2, c2 = (_int(v, key) for v in value)
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def _text(request: Request, key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str):
        raise _invalid(f"{key} must be a string")
    return value


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "b"
    if isinstance(value, float):
        return "n"
    if isinstance(value, ErrorValue):
        return "e"
    return "s"


def _bounds(positions: frozenset[Pos]) -> list[int] | None:
    if not positions:
        return None
    rows = [r for r, _ in positions]
    cols = [c for _, c in positions]
    return [min(rows), min(cols), max(rows), max(cols)]


class Session:
    """One open spreadsheet and the state a client needs around it."""

    def __init__(self, workers: int | None = None) -> None:
        self.sheet = Sheet(workers=workers)
        self.version = 0
        self._clipboard: Clipboard | None = None
        self._ops: dict[str, Callable[[Request], Response]] = {
            "hello": self._hello,
            "view": self._view,
            "cell": self._cell,
            "used_range": self._used_range,
            "edit": self._edit,
            "format": self._format,
            "clear": self._clear,
            "insert_rows": lambda r: self._structure(self.sheet.insert_rows, r),
            "delete_rows": lambda r: self._structure(self.sheet.delete_rows, r),
            "insert_cols": lambda r: self._structure(self.sheet.insert_cols, r),
            "delete_cols": lambda r: self._structure(self.sheet.delete_cols, r),
            "copy": self._copy,
            "paste": self._paste,
            "paste_text": self._paste_text,
            "fill": self._fill,
            "undo": lambda r: self._changed(self.sheet.undo()),
            "redo": lambda r: self._changed(self.sheet.redo()),
            "save": self._save,
            "load": self._load,
            "new": self._new,
            "import_csv": self._import_csv,
            "export_csv": self._export_csv,
            "shutdown": lambda r: {},
        }

    def close(self) -> None:
        self.sheet.close()

    # --- dispatch ----------------------------------------------------------------------

    def handle(self, request: object) -> Response:
        """Carry out one request. Never raises."""
        req_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise _invalid("a request must be an object")
            op = request.get("op")
            handler = self._ops.get(op) if isinstance(op, str) else None
            if handler is None:
                raise _invalid(f"unknown op {op!r}")
            body = handler(request)
            response: Response = {"ok": True, **body}
            response["version"] = self.version
            response["can_undo"] = self.sheet.can_undo
            response["can_redo"] = self.sheet.can_redo
        except RequestError as exc:
            response = {"ok": False, "error": {"kind": exc.kind, "message": str(exc), **exc.extra}}
        except FormulaSyntaxError as exc:
            response = {"ok": False, "error": {"kind": "syntax", "message": str(exc)}}
        except CycleError as exc:
            response = {"ok": False, "error": {"kind": "circular", "message": str(exc)}}
        except CsvError as exc:
            err = {"kind": "csv", "message": str(exc), "row": exc.row, "col": exc.col}
            response = {"ok": False, "error": err}
        except FormatError as exc:
            response = {"ok": False, "error": {"kind": "format", "message": str(exc)}}
        except FileFormatError as exc:
            response = {"ok": False, "error": {"kind": "file", "message": str(exc)}}
        except OSError as exc:
            response = {"ok": False, "error": {"kind": "io", "message": str(exc)}}
        except (IndexError, ValueError) as exc:
            response = {"ok": False, "error": {"kind": "invalid", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 - an engine bug must not kill the server
            print(f"internal error handling {request!r}: {exc!r}", file=sys.stderr)
            response = {"ok": False, "error": {"kind": "internal", "message": repr(exc)}}
        if req_id is not None:
            response["id"] = req_id
        return response

    # --- helpers -----------------------------------------------------------------------

    def _changed(self, positions: frozenset[Pos]) -> Response:
        """The body for an operation that changed (or did not change) cells."""
        if positions:
            self.version += 1
        listing = None if len(positions) > CHANGED_LIMIT else sorted([r, c] for r, c in positions)
        return {"changed": listing, "bounds": _bounds(positions)}

    def _display(self, cell_value: object, fmt: str) -> str:
        return format_value(cell_value, fmt)  # type: ignore[arg-type]

    # --- reading -----------------------------------------------------------------------

    def _hello(self, request: Request) -> Response:
        gil = getattr(sys, "_is_gil_enabled", lambda: True)()
        return {"max_rows": MAX_ROWS, "max_cols": MAX_COLS, "gil": bool(gil)}

    def _view(self, request: Request) -> Response:
        r1, c1, r2, c2 = _rect(request)
        if r1 < 0 or c1 < 0 or r2 >= MAX_ROWS or c2 >= MAX_COLS:
            raise _invalid("rect is outside the grid")
        if (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_VIEW_CELLS:
            raise _invalid(f"rect is larger than {MAX_VIEW_CELLS} cells")
        get = self.sheet.store.get_cell
        rows: list[list[list[str] | None]] = []
        for r in range(r1, r2 + 1):
            row: list[list[str] | None] = []
            for c in range(c1, c2 + 1):
                cell = get(r, c)
                if cell is None or (cell.value is None and not cell.text):
                    row.append(None)
                else:
                    shown = self._display(cell.value, cell.format)
                    item = [shown, _kind(cell.value)]
                    if cell.text != shown:
                        item.append(cell.text)  # raw text, for editing and the formula bar
                    row.append(item)
            rows.append(row)
        return {"cells": rows}

    def _cell(self, request: Request) -> Response:
        row, col = _pos(request, "at")
        cell = self.sheet.store.get_cell(row, col)
        if cell is None:
            return {
                "text": "",
                "display": "",
                "kind": "s",
                "format": "General",
                "error": None,
                "formula": False,
            }
        err = cell.error
        return {
            "text": cell.text,
            "display": self._display(cell.value, cell.format),
            "kind": _kind(cell.value),
            "format": cell.format,
            "error": str(err) if err is not None else None,
            "formula": cell.ast is not None,
        }

    def _used_range(self, request: Request) -> Response:
        used = self.sheet.store.used_range()
        return {"range": None if used is None else [0, 0, used[2], used[3]]}

    # --- editing -----------------------------------------------------------------------

    def _edit(self, request: Request) -> Response:
        raw = request.get("entries")
        if not isinstance(raw, list):
            raise _invalid("entries must be a list of [row, col, text]")
        entries = []
        for item in raw:
            if not isinstance(item, list | tuple) or len(item) != 3 or not isinstance(item[2], str):
                raise _invalid("each entry must be [row, col, text]")
            entries.append((_int(item[0], "row"), _int(item[1], "col"), item[2]))
        return self._changed(self.sheet.set_entries(entries))

    def _format(self, request: Request) -> Response:
        r1, c1, r2, c2 = _rect(request)
        return self._changed(self.sheet.set_format_range(r1, c1, r2, c2, _text(request, "fmt")))

    def _clear(self, request: Request) -> Response:
        """Empty every populated cell in the rectangle (formats are kept), as one edit."""
        r1, c1, r2, c2 = _rect(request)
        cells = self.sheet.store.cells_in(r1, c1, r2, c2)
        return self._changed(self.sheet.set_entries([(r, c, "") for (r, c) in cells]))

    def _structure(self, op: Callable[[int, int], frozenset[Pos]], request: Request) -> Response:
        return self._changed(
            op(_int(request.get("at"), "at"), _int(request.get("count", 1), "count"))
        )

    def _copy(self, request: Request) -> Response:
        r1, c1, r2, c2 = _rect(request)
        self._clipboard = self.sheet.copy(r1, c1, r2, c2)
        used = self.sheet.store.used_range()
        if used is None or r1 > used[2] or c1 > used[3]:
            return {"text": ""}
        r2, c2 = min(r2, used[2]), min(c2, used[3])  # text only needs the populated area
        lines = []
        for r in range(r1, r2 + 1):
            fields = []
            for c in range(c1, c2 + 1):
                cell = self.sheet.store.get_cell(r, c)
                fields.append("" if cell is None else self._display(cell.value, cell.format))
            lines.append("\t".join(fields))
        return {"text": "\n".join(lines)}

    def _paste(self, request: Request) -> Response:
        if self._clipboard is None:
            raise _invalid("nothing has been copied")
        row, col = _pos(request, "at")
        values_only = bool(request.get("values_only", False))
        return self._changed(self.sheet.paste(self._clipboard, row, col, values_only=values_only))

    def _paste_text(self, request: Request) -> Response:
        row, col = _pos(request, "at")
        text = _text(request, "text").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        entries = [
            (row + i, col + j, field)
            for i, line in enumerate(lines)
            for j, field in enumerate(line.split("\t"))
        ]
        return self._changed(self.sheet.set_entries(entries))

    def _fill(self, request: Request) -> Response:
        r1, c1, r2, c2 = _rect(request)
        direction = request.get("direction")
        if direction == "down":
            return self._changed(self.sheet.fill_down(r1, c1, r2, c2))
        if direction == "right":
            return self._changed(self.sheet.fill_right(r1, c1, r2, c2))
        raise _invalid("direction must be 'down' or 'right'")

    # --- files -------------------------------------------------------------------------

    def _save(self, request: Request) -> Response:
        path = _text(request, "path")
        return {"cells": save_sheet(self.sheet, path), "path": path}

    def _replace_sheet(self, sheet: Sheet) -> None:
        """Swap in a new document. The old one is closed only after the new one exists."""
        old, self.sheet = self.sheet, sheet
        old.close()
        self.version += 1

    def _load(self, request: Request) -> Response:
        path = _text(request, "path")
        new = load_sheet(path)  # raises before anything is touched if the file is bad
        self._replace_sheet(new)
        return {"cells": len(new.store), "path": path, "reset": True}

    def _new(self, request: Request) -> Response:
        self._replace_sheet(Sheet())
        return {"reset": True}

    def _import_csv(self, request: Request) -> Response:
        row, col = _pos(request, "at") if "at" in request else (0, 0)
        return self._changed(import_csv(self.sheet, _text(request, "path"), row=row, col=col))

    def _export_csv(self, request: Request) -> Response:
        mode = request.get("mode", "display")
        if mode not in ("display", "raw"):
            raise _invalid("mode must be 'display' or 'raw'")
        rect = _rect(request) if "rect" in request else None
        rows = export_csv(self.sheet, _text(request, "path"), mode=mode, rect=rect)
        return {"rows": rows}

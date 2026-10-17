"""CSV import and export (RFC 4180 quoting, UTF-8).

Export writes the sheet from A1 so cell positions are preserved: either the displayed text
(numbers and dates formatted as on screen) or the raw text as typed. Import feeds every
field through the normal entry path as a single transaction, so formulas, dates and
numbers are parsed exactly as if typed, and one undo reverts the whole import. Formats are
not stored in CSV.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal, TextIO, cast

from sheet.engine.address import cell_name
from sheet.engine.entry import Entry, parse_entry
from sheet.engine.formats import format_value
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.parser import MAX_COLS, MAX_ROWS
from sheet.engine.sheet import Pos, Sheet

Source = str | os.PathLike[str] | TextIO


class CsvError(ValueError):
    """A problem with CSV content. ``row`` and ``col`` (0-based) locate it when known."""

    def __init__(self, message: str, row: int | None = None, col: int | None = None) -> None:
        super().__init__(message)
        self.row = row
        self.col = col


@contextmanager
def _text(source: Source, mode: str, encoding: str) -> Iterator[TextIO]:
    if isinstance(source, str | os.PathLike):
        with open(source, mode, encoding=encoding, newline="") as f:
            yield cast(TextIO, f)
    else:
        yield source


def export_csv(
    sheet: Sheet,
    dest: Source,
    mode: Literal["display", "raw"] = "display",
    rect: tuple[int, int, int, int] | None = None,
    delimiter: str = ",",
    encoding: str = "utf-8",
) -> int:
    """Write cells as CSV and return the number of rows written.

    ``mode="display"`` writes each value as formatted on screen; ``"raw"`` writes the text
    as typed (formulas included). By default the area is A1 to the last used cell; pass
    ``rect=(r1, c1, r2, c2)`` for part of the sheet. A sheet with a cell far down or right
    writes every row up to it, mostly empty fields.
    """
    if mode not in ("display", "raw"):
        raise ValueError(f"mode must be 'display' or 'raw', not {mode!r}")
    if rect is None:
        used = sheet.store.used_range()
        if used is None:
            return 0
        rect = (0, 0, used[2], used[3])
    r1, c1, r2, c2 = rect
    cells = sheet.store.cells_in(r1, c1, r2, c2)
    with _text(dest, "w", encoding) as f:
        writer = csv.writer(f, delimiter=delimiter, lineterminator="\r\n")
        for r in range(r1, r2 + 1):
            row = []
            for c in range(c1, c2 + 1):
                cell = cells.get((r, c))
                if cell is None:
                    row.append("")
                else:
                    row.append(
                        format_value(cell.value, cell.format) if mode == "display" else cell.text
                    )
            writer.writerow(row)
    return r2 - r1 + 1


def import_csv(
    sheet: Sheet,
    source: Source,
    row: int = 0,
    col: int = 0,
    delimiter: str = ",",
    bad_formulas: Literal["raise", "text"] = "raise",
    encoding: str = "utf-8-sig",
) -> frozenset[Pos]:
    """Enter every CSV field into the sheet starting at ``(row, col)``; one transaction.

    Empty fields clear their target cells, as paste does; cells outside the data are left
    alone. All-or-nothing: a malformed formula raises ``CsvError`` (or, with
    ``bad_formulas="text"``, is kept as text), a circular reference raises ``CycleError``,
    and a file that does not fit on the grid raises ``CsvError``; the sheet is unchanged in
    every case. ``utf-8-sig`` also accepts a leading byte-order mark.
    """
    if bad_formulas not in ("raise", "text"):
        raise ValueError(f"bad_formulas must be 'raise' or 'text', not {bad_formulas!r}")
    with _text(source, "r", encoding) as f:
        reader = csv.reader(f, delimiter=delimiter)
        try:
            rows = list(reader)
        except csv.Error as exc:
            raise CsvError(f"Line {reader.line_num}: {exc}") from exc

    width = max((len(r) for r in rows), default=0)
    if row < 0 or col < 0 or row + len(rows) > MAX_ROWS or col + width > MAX_COLS:
        raise CsvError(
            f"{len(rows)} rows x {width} columns does not fit on the grid at that position"
        )

    positions = [(row + i, col + j) for i, fields in enumerate(rows) for j in range(len(fields))]
    texts = [text for fields in rows for text in fields]
    entries: list[tuple[int, int, Entry]] = []
    for (r, c), text, parsed in zip(positions, texts, sheet.parse_all(texts), strict=True):
        if isinstance(parsed, FormulaSyntaxError):
            if bad_formulas == "raise":
                raise CsvError(f"{cell_name(r, c)}: {parsed}", r, c) from parsed
            parsed = parse_entry("'" + text)  # keep the malformed formula as plain text
        entries.append((r, c, parsed))
    return sheet.set_parsed(entries)

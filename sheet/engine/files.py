"""Native file format: a versioned JSON document holding each cell's text and format.

Values are not stored; they are recalculated on load, so a file can never disagree with its
own formulas. The file has one cell per line so that diffs and version control stay useful:

    {"format":"sheet","version":1,"formats":["General","0.00"],"cells":[
    [0,0,"5"],
    [1,1,"=A1*2",1]
    ]}

A cell is ``[row, col, text]`` or ``[row, col, text, format_index]`` (an index into
``formats``, whose first entry is always ``General``; ``General`` cells omit the index).
Saving writes to a temporary file and renames it, so a failed save never damages an
existing file. Unknown extra keys are ignored when loading, so older versions can read
files that only add information.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypeGuard

from sheet.engine.address import cell_name
from sheet.engine.entry import Entry, parse_entry
from sheet.engine.formats import GENERAL, FormatError, validate_format
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.parser import MAX_COLS, MAX_ROWS
from sheet.engine.sheet import Sheet

FILE_VERSION = 1
FORMAT_NAME = "sheet"


class FileFormatError(ValueError):
    """The file is not a valid sheet file (or is from a newer version of the program)."""


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def save_sheet(sheet: Sheet, path: str | os.PathLike[str]) -> int:
    """Write the sheet to ``path`` atomically and return the number of cells saved."""
    formats = [GENERAL]
    index = {GENERAL: 0}
    rows: list[list[Any]] = []
    for (row, col), cell in sorted(sheet.store.items(), key=lambda item: item[0]):
        if not cell.text.strip() and cell.format == GENERAL:
            continue  # nothing to remember
        record: list[Any] = [row, col, cell.text]
        if cell.format != GENERAL:
            if cell.format not in index:
                index[cell.format] = len(formats)
                formats.append(cell.format)
            record.append(index[cell.format])
        rows.append(record)

    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                f'{{"format":"{FORMAT_NAME}","version":{FILE_VERSION},"formats":{_dump(formats)},"cells":[\n'
            )
            for i, record in enumerate(rows):
                f.write(_dump(record) + (",\n" if i < len(rows) - 1 else "\n"))
            f.write("]}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return len(rows)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _read(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FileFormatError(f"not a sheet file ({exc})") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
        raise FileFormatError("not a sheet file")
    version = data.get("version")
    if not _is_int(version) or version < 1:
        raise FileFormatError(f"bad version number {version!r}")
    if version > FILE_VERSION:
        raise FileFormatError(
            f"this file was saved by a newer version of Sheet (file version {version}, "
            f"this program reads up to {FILE_VERSION})"
        )
    return data


def _formats(data: dict[str, Any]) -> list[str]:
    formats = data.get("formats")
    if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
        raise FileFormatError("formats must be a list of strings")
    if not formats or formats[0] != GENERAL:
        raise FileFormatError("the first format must be General")
    for fmt in formats:
        try:
            validate_format(fmt)
        except FormatError as exc:
            raise FileFormatError(f"unsupported format {fmt!r}: {exc}") from exc
    return formats


def _cells(data: dict[str, Any], formats: list[str]) -> list[tuple[int, int, Entry, str]]:
    raw = data.get("cells")
    if not isinstance(raw, list):
        raise FileFormatError("cells must be a list")
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, Entry, str]] = []
    for i, record in enumerate(raw, start=1):
        ok = (
            isinstance(record, list)
            and len(record) in (3, 4)
            and _is_int(record[0])
            and _is_int(record[1])
            and isinstance(record[2], str)
            and (len(record) == 3 or _is_int(record[3]))
        )
        if not ok:
            raise FileFormatError(f"cell {i} is malformed")
        row, col, text = record[0], record[1], record[2]
        if not (0 <= row < MAX_ROWS and 0 <= col < MAX_COLS):
            raise FileFormatError(f"cell {i} is outside the grid")
        fmt_index = record[3] if len(record) == 4 else 0
        if not 0 <= fmt_index < len(formats):
            raise FileFormatError(f"cell {i} refers to an unknown format")
        if (row, col) in seen:
            raise FileFormatError(f"cell {cell_name(row, col)} appears twice")
        seen.add((row, col))
        try:
            entry = parse_entry(text)
        except FormulaSyntaxError as exc:
            raise FileFormatError(f"{cell_name(row, col)}: {exc}") from exc
        out.append((row, col, entry, formats[fmt_index]))
    return out


def load_sheet(path: str | os.PathLike[str], workers: int | None = None) -> Sheet:
    """Read a sheet file into a new ``Sheet`` (with no undo history).

    Raises FileFormatError for an invalid file, CycleError if its formulas are circular,
    and OSError if it cannot be read. Nothing existing is touched: the caller swaps the new
    sheet in only if this returns.
    """
    data = _read(path)
    cells = _cells(data, _formats(data))
    sheet = Sheet(workers=workers)
    try:
        sheet.load_cells(cells)
    except BaseException:
        sheet.close()
        raise
    sheet.clear_history()
    return sheet

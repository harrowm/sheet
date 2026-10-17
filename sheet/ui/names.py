"""Cell naming for the UI: ``A1``-style names and column letters."""

from __future__ import annotations

import re

from sheet.engine.address import cell_name, column_name

__all__ = ["MAX_COLS", "MAX_ROWS", "cell_name", "column_name", "parse_cell_name"]

MAX_ROWS = 1_048_576
MAX_COLS = 16_384

_NAME = re.compile(r"\s*\$?([A-Za-z]{1,3})\$?(\d+)\s*", re.ASCII)


def parse_cell_name(text: str) -> tuple[int, int] | None:
    """``(row, col)`` for a name like ``B7`` or ``$C$3``, or None if it is not a cell name."""
    m = _NAME.fullmatch(text)
    if m is None:
        return None
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + ord(ch) - ord("A") + 1
    row = int(m.group(2))
    if not (1 <= col <= MAX_COLS and 1 <= row <= MAX_ROWS):
        return None
    return row - 1, col - 1

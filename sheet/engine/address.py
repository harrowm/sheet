"""A1-style names for cells."""

from __future__ import annotations


def column_name(col: int) -> str:
    """Column letters for a 0-based column index: 0 -> A, 26 -> AA, 16383 -> XFD."""
    letters = ""
    n = col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def cell_name(row: int, col: int) -> str:
    return f"{column_name(col)}{row + 1}"

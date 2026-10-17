"""Interpret text typed into a cell. Lexing and parsing happen here, once, on entry."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sheet.engine.ast import Literal, Node
from sheet.engine.compiler import compile_formula
from sheet.engine.dates import parse_date_text
from sheet.engine.values import Value

_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)


@dataclass(frozen=True, slots=True)
class Entry:
    text: str  # exactly what the user typed
    ast: Node | None  # None unless the entry is a formula
    value: Value  # None for empty entries and for formulas that still need calculating
    format: str | None  # a format implied by the entry (dates), else None


def parse_entry(text: str) -> Entry:
    """Classify ``text`` as empty, formula, number, boolean, date or string.

    Raises FormulaSyntaxError for a malformed formula, so the edit can be rejected.
    """
    stripped = text.strip()
    if not stripped:
        return Entry(text, None, None, None)
    if stripped.startswith("="):
        ast = compile_formula(stripped)
        value = ast.value if isinstance(ast, Literal) else None  # constants are pre-calculated
        return Entry(text, ast, value, None)
    if stripped.startswith("'"):
        return Entry(text, None, stripped[1:], None)
    if _NUMBER.fullmatch(stripped) and math.isfinite(number := float(stripped)):
        return Entry(text, None, number, None)  # (a number beyond float range stays text)
    if stripped.upper() in ("TRUE", "FALSE"):
        return Entry(text, None, stripped.upper() == "TRUE", None)
    if (date := parse_date_text(stripped)) is not None:
        return Entry(text, None, date[0], date[1])
    return Entry(text, None, text, None)

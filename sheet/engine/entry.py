"""Interpret text typed into a cell. Lexing and parsing happen here, once, on entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sheet.engine.ast import Literal, Node
from sheet.engine.compiler import compile_formula
from sheet.engine.dates import parse_date_text
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.limits import MAX_FORMULA_LENGTH
from sheet.engine.values import parse_number

if TYPE_CHECKING:
    from sheet.engine.values import Value


@dataclass(frozen=True, slots=True)
class Entry:
    text: str  # exactly what the user typed
    ast: Node | None  # None unless the entry is a formula
    value: Value  # None for empty entries and for formulas that still need calculating
    format: str | None  # a format implied by the entry (dates), else None


def parse_entry(text: str, enforce_limits: bool = True) -> Entry:
    """Classify ``text`` as empty, formula, number, boolean, date or string.

    Raises FormulaSyntaxError for a malformed or over-long formula, so the edit can be
    rejected. ``enforce_limits=False`` skips the length limit, for text the program itself
    regenerated (rewriting references can lengthen a formula the user typed within the limit).
    """
    stripped = text.strip()
    if not stripped:
        return Entry(text, None, None, None)
    if stripped.startswith("="):
        if enforce_limits and len(stripped) > MAX_FORMULA_LENGTH:
            raise FormulaSyntaxError(
                f"Formulas can be at most {MAX_FORMULA_LENGTH} characters ({len(stripped)} typed)",
                0,
            )
        ast = compile_formula(stripped)
        value = ast.value if isinstance(ast, Literal) else None  # constants are pre-calculated
        return Entry(text, ast, value, None)
    if stripped.startswith("'"):
        return Entry(text, None, stripped[1:], None)
    if (number := parse_number(stripped)) is not None:
        return Entry(text, None, number + 0.0, None)  # (-0 is 0)
    if stripped.upper() in ("TRUE", "FALSE"):
        return Entry(text, None, stripped.upper() == "TRUE", None)
    if (date := parse_date_text(stripped)) is not None:
        return Entry(text, None, date[0], date[1])
    return Entry(text, None, text, None)

"""Lexer for formula text. Runs once, on data entry."""

from __future__ import annotations

import math
import re
from enum import Enum, auto
from typing import NamedTuple

from sheet.engine.values import NUMBER_PATTERN, ErrorKind


class FormulaSyntaxError(ValueError):
    """Raised when formula text cannot be lexed or parsed."""

    def __init__(self, message: str, pos: int = 0) -> None:
        super().__init__(f"{message} (at position {pos})")
        self.pos = pos


class TokenKind(Enum):
    NUMBER = auto()
    STRING = auto()
    BOOL = auto()
    REF = auto()
    IDENT = auto()
    OP = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    COLON = auto()
    ERROR = auto()
    EOF = auto()


class Token(NamedTuple):
    kind: TokenKind
    value: float | str | bool | None
    pos: int


# One pattern with a named group per token kind. Alternatives are tried in order, so earlier
# ones win: a number beats a reference beats a name, and "<>" beats "<".
_ERRORS = tuple(k.value for k in ErrorKind)
_TOKEN = re.compile(
    "|".join(
        [
            r"(?P<space>\s+)",
            r'(?P<string>"(?:[^"]|"")*+")',  # possessive: "" is always an escaped quote
            r'(?P<unterminated>")',
            "(?P<error>" + "|".join(re.escape(e) for e in _ERRORS) + ")",
            r"(?P<badhash>\#)",
            r"(?P<punct>[(),:])",
            f"(?P<number>(?a:{NUMBER_PATTERN}))",  # ASCII digits only
            r"(?P<ref>(?a:\$?[A-Za-z]{1,3}\$?\d+(?![A-Za-z0-9_(])))",
            r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)",
            r"(?P<op><>|<=|>=|[-+*/^%&=<>])",
        ]
    )
)
_PUNCTUATION = {
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    ",": TokenKind.COMMA,
    ":": TokenKind.COLON,
}


def tokenize(text: str, offset: int = 0) -> list[Token]:
    """Split formula text into tokens. ``offset`` is added to the position in an error, for
    callers that strip a leading character (the ``=``) before lexing."""
    tokens: list[Token] = []
    append = tokens.append
    match = _TOKEN.match
    i, n = 0, len(text)
    while i < n:
        m = match(text, i)
        if m is None:
            raise FormulaSyntaxError(f"Unexpected character {text[i]!r}", i + offset)
        kind, end = m.lastgroup, m.end()
        if kind == "space":
            pass
        elif kind == "ref":
            append(Token(TokenKind.REF, m.group(), i))
        elif kind == "number":
            number = float(m.group())
            if not math.isfinite(number):
                raise FormulaSyntaxError(f"The number {m.group()[:20]!r} is too large", i + offset)
            append(Token(TokenKind.NUMBER, number, i))
        elif kind == "op":
            append(Token(TokenKind.OP, m.group(), i))
        elif kind == "punct":
            append(Token(_PUNCTUATION[m.group()], m.group(), i))
        elif kind == "name":
            word = m.group()
            if word.upper() in ("TRUE", "FALSE"):
                append(Token(TokenKind.BOOL, word.upper() == "TRUE", i))
            else:
                append(Token(TokenKind.IDENT, word, i))
        elif kind == "string":
            append(Token(TokenKind.STRING, m.group()[1:-1].replace('""', '"'), i))
        elif kind == "error":
            append(Token(TokenKind.ERROR, m.group(), i))
        elif kind == "unterminated":
            raise FormulaSyntaxError("Unterminated string", i + offset)
        else:  # badhash
            raise FormulaSyntaxError("Unknown error literal", i + offset)
        i = end
    append(Token(TokenKind.EOF, None, n))
    return tokens

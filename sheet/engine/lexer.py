"""Lexer for formula text. Runs once, on data entry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto


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
    EOF = auto()


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: float | str | bool | None
    pos: int


_NUMBER = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_REF = re.compile(r"\$?[A-Za-z]{1,3}\$?\d+(?![A-Za-z0-9_(])")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_OPS = ("<>", "<=", ">=", "+", "-", "*", "/", "^", "%", "&", "=", "<", ">")
_SINGLE = {"(": TokenKind.LPAREN, ")": TokenKind.RPAREN, ",": TokenKind.COMMA, ":": TokenKind.COLON}


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == '"':
            i = _string(text, i, tokens)
        elif ch in _SINGLE:
            tokens.append(Token(_SINGLE[ch], ch, i))
            i += 1
        elif (m := _NUMBER.match(text, i)) is not None:
            tokens.append(Token(TokenKind.NUMBER, float(m.group()), i))
            i = m.end()
        elif (m := _REF.match(text, i)) is not None:
            tokens.append(Token(TokenKind.REF, m.group(), i))
            i = m.end()
        elif (m := _IDENT.match(text, i)) is not None:
            word = m.group()
            if word.upper() in ("TRUE", "FALSE"):
                tokens.append(Token(TokenKind.BOOL, word.upper() == "TRUE", i))
            else:
                tokens.append(Token(TokenKind.IDENT, word, i))
            i = m.end()
        else:
            op = next((o for o in _OPS if text.startswith(o, i)), None)
            if op is None:
                raise FormulaSyntaxError(f"Unexpected character {ch!r}", i)
            tokens.append(Token(TokenKind.OP, op, i))
            i += len(op)
    tokens.append(Token(TokenKind.EOF, None, n))
    return tokens


def _string(text: str, start: int, tokens: list[Token]) -> int:
    parts: list[str] = []
    i = start + 1
    while i < len(text):
        if text[i] == '"':
            if text.startswith('""', i):
                parts.append('"')
                i += 2
                continue
            tokens.append(Token(TokenKind.STRING, "".join(parts), start))
            return i + 1
        parts.append(text[i])
        i += 1
    raise FormulaSyntaxError("Unterminated string", start)

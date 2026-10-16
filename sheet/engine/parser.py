"""Recursive-descent parser: formula text -> AST. Runs once, on data entry.

Grammar (lowest to highest precedence):
    comparison := concat (("=" | "<>" | "<" | ">" | "<=" | ">=") concat)*
    concat     := additive ("&" additive)*
    additive   := term (("+" | "-") term)*
    term       := power (("*" | "/") power)*
    power      := unary ("^" power)?          right-associative
    unary      := ("-" | "+") unary | postfix  binds tighter than ^, as in Excel
    postfix    := primary "%"*
    primary    := NUMBER | STRING | BOOL | ERROR | ref [":" ref] | IDENT "(" [args] ")" | "(" comparison ")"
"""

from __future__ import annotations

from collections.abc import Callable

from sheet.engine.ast import (
    BinaryOp,
    CellRef,
    FuncCall,
    Literal,
    Node,
    UnaryOp,
    normalised_range,
)
from sheet.engine.lexer import FormulaSyntaxError, Token, TokenKind, tokenize
from sheet.engine.values import ErrorKind, ErrorValue

MAX_ROWS = 1_048_576
MAX_COLS = 16_384

_COMPARE = ("=", "<>", "<", ">", "<=", ">=")


def parse_ref(text: str, pos: int = 0) -> CellRef:
    """Parse an A1-style reference such as ``$B$3`` into a 0-based CellRef."""
    i = 0
    abs_col = text.startswith("$")
    if abs_col:
        i = 1
    j = i
    while text[j].isalpha():
        j += 1
    letters = text[i:j].upper()
    abs_row = text[j] == "$"
    if abs_row:
        j += 1
    row = int(text[j:])
    col = 0
    for c in letters:
        col = col * 26 + (ord(c) - ord("A") + 1)
    if not 1 <= col <= MAX_COLS:
        raise FormulaSyntaxError(f"Column out of range in {text!r}", pos)
    if not 1 <= row <= MAX_ROWS:
        raise FormulaSyntaxError(f"Row out of range in {text!r}", pos)
    return CellRef(row - 1, col - 1, abs_row, abs_col)


def parse(text: str) -> Node:
    """Parse formula text (which must start with ``=``) into an AST."""
    if not text.startswith("="):
        raise FormulaSyntaxError("Formula must start with '='", 0)
    return _Parser(tokenize(text[1:]), offset=1).parse()


class _Parser:
    def __init__(self, tokens: list[Token], offset: int) -> None:
        self._tokens = tokens
        self._i = 0
        self._offset = offset

    def parse(self) -> Node:
        node = self._comparison()
        if self._peek().kind is not TokenKind.EOF:
            self._fail("Unexpected token")
        return node

    def _peek(self) -> Token:
        return self._tokens[self._i]

    def _next(self) -> Token:
        tok = self._tokens[self._i]
        self._i += 1
        return tok

    def _fail(self, message: str) -> None:
        raise FormulaSyntaxError(message, self._peek().pos + self._offset)

    def _at_op(self, *ops: str) -> bool:
        tok = self._peek()
        return tok.kind is TokenKind.OP and tok.value in ops

    def _expect(self, kind: TokenKind, what: str) -> Token:
        if self._peek().kind is not kind:
            self._fail(f"Expected {what}")
        return self._next()

    def _binary_left(self, operand: Callable[[], Node], *ops: str) -> Node:
        node = operand()
        while self._at_op(*ops):
            op = str(self._next().value)
            node = BinaryOp(op, node, operand())
        return node

    def _comparison(self) -> Node:
        return self._binary_left(self._concat, *_COMPARE)

    def _concat(self) -> Node:
        return self._binary_left(self._additive, "&")

    def _additive(self) -> Node:
        return self._binary_left(self._term, "+", "-")

    def _term(self) -> Node:
        return self._binary_left(self._power, "*", "/")

    def _power(self) -> Node:
        node = self._unary()
        if self._at_op("^"):
            self._next()
            return BinaryOp("^", node, self._power())
        return node

    def _unary(self) -> Node:
        if self._at_op("-", "+"):
            op = str(self._next().value)
            return UnaryOp(op, self._unary())
        return self._postfix()

    def _postfix(self) -> Node:
        node = self._primary()
        while self._at_op("%"):
            self._next()
            node = UnaryOp("%", node)
        return node

    def _primary(self) -> Node:
        tok = self._peek()
        match tok.kind:
            case TokenKind.NUMBER | TokenKind.STRING | TokenKind.BOOL:
                self._next()
                assert tok.value is not None
                return Literal(tok.value)
            case TokenKind.ERROR:
                self._next()
                return Literal(ErrorValue(ErrorKind(tok.value)))
            case TokenKind.REF:
                return self._reference()
            case TokenKind.IDENT:
                return self._function()
            case TokenKind.LPAREN:
                self._next()
                node = self._comparison()
                self._expect(TokenKind.RPAREN, "')'")
                return node
            case _:
                self._fail("Unexpected token")
                raise AssertionError  # unreachable

    def _cell(self) -> CellRef:
        tok = self._expect(TokenKind.REF, "cell reference")
        return parse_ref(str(tok.value), tok.pos + self._offset)

    def _reference(self) -> Node:
        start = self._cell()
        if self._peek().kind is not TokenKind.COLON:
            return start
        self._next()
        end = self._cell()
        return normalised_range(start, end)

    def _function(self) -> Node:
        name = str(self._next().value).upper()
        self._expect(TokenKind.LPAREN, "'(' after function name")
        args: list[Node] = []
        if self._peek().kind is not TokenKind.RPAREN:
            args.append(self._comparison())
            while self._peek().kind is TokenKind.COMMA:
                self._next()
                args.append(self._comparison())
        self._expect(TokenKind.RPAREN, "')'")
        return FuncCall(name, tuple(args))

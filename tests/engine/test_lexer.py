import pytest

from sheet.engine.lexer import FormulaSyntaxError, Token, TokenKind, tokenize


def kinds(text: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(text)]


def test_always_ends_with_eof():
    assert kinds("") == [TokenKind.EOF]
    assert kinds("1")[-1] == TokenKind.EOF


@pytest.mark.parametrize("text,value", [("42", 42.0), ("3.5", 3.5), (".5", 0.5), ("1e3", 1000.0), ("1.5E-2", 0.015)])
def test_numbers(text, value):
    tok = tokenize(text)[0]
    assert tok.kind == TokenKind.NUMBER
    assert tok.value == pytest.approx(value)


def test_string_with_escaped_quote():
    tok = tokenize('"say ""hi"""')[0]
    assert tok.kind == TokenKind.STRING
    assert tok.value == 'say "hi"'


def test_unterminated_string_is_error():
    with pytest.raises(FormulaSyntaxError):
        tokenize('"abc')


@pytest.mark.parametrize("text", ["TRUE", "true", "False"])
def test_booleans_case_insensitive(text):
    tok = tokenize(text)[0]
    assert tok.kind == TokenKind.BOOL
    assert tok.value is (text.upper() == "TRUE")


@pytest.mark.parametrize("text", ["A1", "a1", "$A$1", "$A1", "A$1", "XFD1048576"])
def test_cell_references(text):
    assert tokenize(text)[0].kind == TokenKind.REF


def test_range_tokens():
    assert kinds("A1:B2") == [TokenKind.REF, TokenKind.COLON, TokenKind.REF, TokenKind.EOF]


def test_function_call_tokens():
    assert kinds("SUM(A1,2)") == [
        TokenKind.IDENT, TokenKind.LPAREN, TokenKind.REF, TokenKind.COMMA,
        TokenKind.NUMBER, TokenKind.RPAREN, TokenKind.EOF,
    ]


@pytest.mark.parametrize("op", ["+", "-", "*", "/", "^", "%", "&", "=", "<>", "<", ">", "<=", ">="])
def test_operators(op):
    tok = tokenize(op)[0]
    assert tok.kind == TokenKind.OP
    assert tok.value == op


def test_whitespace_ignored_and_positions_recorded():
    toks = tokenize("  1 +  2")
    assert [t.pos for t in toks[:3]] == [2, 4, 7]


def test_unexpected_character_is_error():
    with pytest.raises(FormulaSyntaxError):
        tokenize("1 # 2")


def test_token_is_value_object():
    assert Token(TokenKind.NUMBER, 1.0, 0) == Token(TokenKind.NUMBER, 1.0, 0)

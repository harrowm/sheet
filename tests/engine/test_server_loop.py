"""The server loop must survive any input: every line gets a reply, and nothing kills it."""

import io
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sheet.server.main import serve


def run(*lines: bytes) -> list[dict]:
    """Feed raw request lines to the server loop; return its replies."""
    out = io.StringIO()
    code = serve(io.BytesIO(b"".join(line + b"\n" for line in lines)), out)
    assert code == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


HELLO = b'{"id":9,"op":"hello"}'


def test_a_bad_first_line_is_an_error_reply_not_a_crash():
    # Regression: this used to raise UnboundLocalError when no valid request had come first.
    replies = run(b"this is not json", HELLO)
    assert replies[0]["ok"] is False and replies[0]["error"]["kind"] == "invalid"
    assert replies[1]["ok"] is True and replies[1]["id"] == 9


@pytest.mark.parametrize(
    "bad",
    [
        b"\xff\xfe\xfa not utf-8",
        b"\x00\x00\x00",
        b"[" * 200_000,  # deeper than the JSON parser's recursion limit
        b'{"id":1,"op":"hello","pad":'
        + b"1" * 6000
        + b"}",  # integer longer than Python's digit limit
        b'"just a string"',
        b"123",
        b"null",
        b"[]",
        b'{"op": 5}',
        b"{" * 1000,
    ],
)
def test_hostile_lines_get_an_error_reply_and_the_server_carries_on(bad):
    replies = run(bad, HELLO)
    assert len(replies) == 2
    assert replies[0]["ok"] is False and replies[0]["error"]["kind"] == "invalid"
    assert replies[1]["ok"] is True


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_standard_json_constants_are_rejected(constant):
    replies = run(b'{"id":1,"op":"hello","pad":' + constant + b"}", HELLO)
    assert replies[0]["ok"] is False and replies[0]["error"]["kind"] == "invalid"
    assert replies[1]["ok"] is True


def test_every_reply_is_strict_json_even_if_the_client_sent_a_weird_id():
    out = io.StringIO()
    serve(io.BytesIO(b'{"id":1e999,"op":"hello"}\n{"id":[1],"op":"hello"}\n'), out)
    for line in out.getvalue().splitlines():
        json.loads(line, parse_constant=lambda c: pytest.fail(f"non-standard constant {c}"))


def test_blank_lines_are_ignored_and_every_other_line_is_answered():
    assert len(run(b"", b"   ", HELLO, b"\t", HELLO)) == 2


def test_shutdown_stops_the_loop_and_later_lines_are_not_processed():
    replies = run(b'{"id":1,"op":"shutdown"}', HELLO)
    assert [r["id"] for r in replies] == [1]


def test_a_malformed_line_after_a_shutdown_cannot_confuse_the_loop():
    assert len(run(HELLO, b'{"op":"shutdown"}', b"junk")) == 2


def test_end_of_input_is_a_clean_exit():
    assert run() == []


def test_a_closed_stdout_ends_the_server_quietly():
    class Closed:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    assert serve(io.BytesIO(HELLO + b"\n" + HELLO + b"\n"), Closed()) == 0


def test_stray_prints_go_to_stderr_not_the_protocol_stream(monkeypatch, capsys):
    from sheet.server.protocol import Session

    real = Session.handle

    def noisy(self, request):
        print("a stray print while handling a request")  # noqa: T201 - the stray output is the test
        return real(self, request)

    monkeypatch.setattr(Session, "handle", noisy)
    out = io.StringIO()
    serve(io.BytesIO(HELLO + b"\n"), out)
    assert "stray" not in out.getvalue() and json.loads(out.getvalue())["ok"] is True
    captured = capsys.readouterr()
    assert "stray" in captured.err and "stray" not in captured.out


def test_serve_restores_sys_stdout():
    import sys

    before = sys.stdout
    serve(io.BytesIO(HELLO + b"\n"), io.StringIO())
    assert sys.stdout is before


@settings(max_examples=200, deadline=None)
@given(st.lists(st.binary(max_size=200), max_size=12))
def test_any_bytes_get_exactly_one_json_reply_per_nonblank_line(chunks):
    lines = [c.replace(b"\n", b" ").replace(b"\r", b" ") for c in chunks]
    replies = run(*lines)
    nonblank = [line for line in lines if line.strip()]
    # a line that happens to be a valid shutdown request ends the run early
    assert len(replies) <= len(nonblank)
    assert all(isinstance(r, dict) and "ok" in r for r in replies)
    if not any(b"shutdown" in line for line in lines):
        assert len(replies) == len(nonblank)

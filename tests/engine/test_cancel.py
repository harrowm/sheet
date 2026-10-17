"""Cancelling and time-limiting long operations: a stopped edit leaves the document exactly as
it was, at whatever point it stops."""

import io
import json
import threading
import time

import pytest

import sheet.engine.sheet as sheet_module
from sheet.engine.cancel import CancelledError, CancelToken
from sheet.engine.sheet import Sheet
from sheet.server import main as server_main
from sheet.server.protocol import Session, default_time_limit
from tests.engine.helpers import exact_state

# --- the token ------------------------------------------------------------------------------


def test_a_token_raises_once_cancelled_and_start_clears_it():
    token = CancelToken()
    token.check()
    token.cancel()
    with pytest.raises(CancelledError) as info:
        token.check()
    assert info.value.reason == "cancelled"
    token.start(None)
    token.check()


def test_a_token_raises_after_its_deadline_and_stop_clears_it():
    token = CancelToken()
    token.start(0.001)
    time.sleep(0.01)
    with pytest.raises(CancelledError) as info:
        token.check()
    assert info.value.reason == "timeout" and "0.001 second limit" in str(info.value)
    token.stop()
    token.check()


# --- a stopped edit leaves nothing behind -----------------------------------------------------


class StopsAfter(CancelToken):
    """Passes the first ``n`` checks, then cancels every later one."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.left = n
        self._lock = threading.Lock()  # checks come from several threads

    def check(self) -> None:
        with self._lock:
            self.left -= 1
            stop = self.left < 0
        if stop:
            raise CancelledError("cancelled", "stopped by the test")


def _sheet(token: CancelToken, n: int = 3_000) -> Sheet:
    s = Sheet(workers=4, parallel_threshold=1, cancel=token)
    s.set_entries([(r, 0, str(r)) for r in range(n)])
    s.set_entries([(r, 1, f"=A{r + 1}*2") for r in range(n)])
    s.set_entries([(r, 2, f"=SUM(A{max(1, r - 3)}:A{r + 1})") for r in range(0, n, 10)])
    s.clear_history()
    return s


OPERATIONS = {
    "insert rows": lambda s: s.insert_rows(5),
    "delete rows": lambda s: s.delete_rows(5, 2),
    "insert columns": lambda s: s.insert_cols(0),
    "delete columns": lambda s: s.delete_cols(0),
    "bulk edit": lambda s: s.set_entries([(r, 0, str(r + 7)) for r in range(0, 3_000, 3)]),
    "format": lambda s: s.set_format_range(0, 0, 2_999, 3, "0.00"),
    "paste": lambda s: s.paste(s.copy(0, 0, 1_499, 2), 0, 5),
    "fill": lambda s: s.fill_down(0, 0, 2_999, 1),
    "clear": lambda s: s.set_entries([(r, 0, "") for r in range(0, 3_000, 2)]),
}


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch):
    """Make a few thousand cells enough to be split into several chunks on the pool."""
    monkeypatch.setattr(sheet_module, "PARALLEL_REWRITE_MIN", 200)
    monkeypatch.setattr(sheet_module, "PARALLEL_REWRITE_CHUNK", 100)
    monkeypatch.setattr(sheet_module, "PARALLEL_PARSE_MIN", 200)
    monkeypatch.setattr(sheet_module, "PARALLEL_PARSE_CHUNK", 100)


@pytest.mark.parametrize("name", OPERATIONS)
def test_stopping_an_edit_at_any_point_restores_the_document_exactly(name):
    operation = OPERATIONS[name]
    token = StopsAfter(10**9)
    expected_sheet = _sheet(token)
    token.left = 10**9
    operation(expected_sheet)
    expected = exact_state(expected_sheet)
    checks = 10**9 - token.left  # how many checkpoints a complete run passes
    assert checks > 1, "the operation should reach several checkpoints"

    stops = sorted({0, 1, checks // 3, 2 * checks // 3, checks - 1})
    for stop in stops:
        token = StopsAfter(10**9)
        s = _sheet(token)
        before = exact_state(s)
        graph_before = {pos: s.graph.dependencies(pos) for pos, _ in s.store.items()}
        token.left = stop
        with pytest.raises(CancelledError):
            operation(s)
        assert exact_state(s) == before, f"changed after stopping at check {stop}"
        assert {pos: s.graph.dependencies(pos) for pos, _ in s.store.items()} == graph_before
        assert not s.can_undo and not s.can_redo  # and nothing was added to the history
        token.left = 10**9  # and the same edit then goes through, giving the normal result
        operation(s)
        assert exact_state(s) == expected
        s.close()
    expected_sheet.close()


# --- the protocol --------------------------------------------------------------------------


def test_a_request_past_the_time_limit_is_stopped_and_changes_nothing():
    session = Session(time_limit=1e-6, workers=4)
    session.time_limit = 0  # build the document without a limit
    session.handle({"op": "edit", "entries": [[r, 0, str(r)] for r in range(30_000)]})
    session.time_limit = 1e-6
    response = session.handle({"op": "insert_rows", "at": 0, "count": 1, "id": 3})
    assert response["ok"] is False and response["id"] == 3
    assert response["error"]["kind"] == "timeout"
    assert session.sheet.get(0, 0) == 0.0 and session.sheet.get(29_999, 0) == 29_999.0
    session.time_limit = 0
    assert session.handle({"op": "insert_rows", "at": 0, "count": 1})["ok"] is True
    assert session.sheet.get(1, 0) == 0.0
    session.sheet.close()


def test_the_time_limit_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("SHEET_TIME_LIMIT", raising=False)
    assert default_time_limit() == 60.0
    for text, expected in [
        ("5", 5.0),
        ("0", 0.0),
        ("-1", 0.0),
        ("inf", 0.0),
        ("2.5", 2.5),
        ("x", 60.0),
    ]:
        monkeypatch.setenv("SHEET_TIME_LIMIT", text)
        assert default_time_limit() == expected, text


def _slow_op(session: Session, started: threading.Event):
    def run(_request):
        started.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # stands in for a long edit that checks in regularly
            session._token.check()
            time.sleep(0.001)
        return {}

    return run


def test_cancelling_a_running_request_from_another_thread_stops_it():
    session = Session(time_limit=0)
    started = threading.Event()
    session._ops["slow"] = _slow_op(session, started)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(session.handle({"op": "slow", "id": 7})))
    worker.start()
    assert started.wait(5)
    session.cancel(7)
    worker.join(5)
    assert not worker.is_alive()
    assert result["ok"] is False and result["error"]["kind"] == "cancelled" and result["id"] == 7
    assert session.handle({"op": "hello"})["ok"] is True  # the next request is unaffected
    session.sheet.close()


def test_cancel_without_a_target_stops_whatever_is_running_and_is_harmless_when_idle():
    session = Session(time_limit=0)
    session.cancel()  # nothing running
    session.cancel(12345)  # unknown request
    assert session.handle({"op": "hello"})["ok"] is True
    started = threading.Event()
    session._ops["slow"] = _slow_op(session, started)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(session.handle({"op": "slow"})))
    worker.start()
    assert started.wait(5)
    session.cancel()
    worker.join(5)
    assert result["error"]["kind"] == "cancelled"
    session.sheet.close()


def test_a_request_cancelled_before_it_starts_is_refused_without_running():
    session = Session(time_limit=0)
    session.cancel(9)
    ran = []
    session._ops["note"] = lambda _r: ran.append(1) or {}
    response = session.handle({"op": "note", "id": 9})
    assert response["error"]["kind"] == "cancelled" and not ran
    assert session.handle({"op": "note", "id": 9})["ok"] is True  # the cancel was used up
    assert ran == [1]
    session.sheet.close()


# --- the running server --------------------------------------------------------------------


def test_the_server_answers_a_cancel_while_busy_and_keeps_every_reply_in_order(monkeypatch):
    original_init = Session.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._ops["slow"] = _slow_op(self, threading.Event())

    monkeypatch.setattr(Session, "__init__", init)
    lines = [
        {"op": "hello", "id": 1},
        {"op": "slow", "id": 2},
        {"op": "cancel", "target": 2, "id": 3},
        {"op": "hello", "id": 4},
        {"op": "shutdown", "id": 5},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(x).encode() + b"\n" for x in lines))
    out = io.StringIO()
    assert server_main.serve(stdin, out) == 0
    replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    assert replies[1]["ok"] and replies[4]["ok"] and replies[5]["ok"]
    assert replies[2]["error"]["kind"] == "cancelled"
    assert replies[3] == {"ok": True, "id": 3}

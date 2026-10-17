"""The engine process: reads one JSON request per line on stdin, writes one JSON response
per line on stdout. Anything else the program prints goes to stderr, so stdout carries
protocol messages only.

The loop must never die because of what a client sends: every non-blank line gets exactly
one reply, whether it is valid JSON, valid UTF-8, absurdly nested or not an object at all.
(A ``cancel`` request is answered at once by the reader thread; see ``Session.cancel``.)
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from typing import Any, BinaryIO, TextIO

from sheet.server.protocol import Session


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")  # Python accepts NaN/Infinity; JSON does not


def _invalid(message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"kind": "invalid", "message": message}}


def _parse(raw: bytes) -> tuple[Any, dict[str, Any] | None]:
    """Decode one request line. Returns (request, None) or (None, error response)."""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant), None
    except UnicodeDecodeError:
        return None, _invalid("request is not valid UTF-8")
    except RecursionError:
        return None, _invalid("request is nested too deeply")
    except json.JSONDecodeError as exc:
        return None, _invalid(f"bad JSON: {exc}")
    except ValueError as exc:  # e.g. an integer with thousands of digits
        return None, _invalid(f"bad request: {exc}")


def serve(stdin: BinaryIO | None = None, stdout: TextIO | None = None) -> int:
    """Serve requests until end of input or a ``shutdown`` request.

    A reader thread takes lines off stdin so that a ``cancel`` request can reach the engine
    while it is busy with an earlier one; everything else is handled in order, one at a time,
    on this thread.
    """
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    saved_stdout = sys.stdout
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol stream
    session = Session()
    write_lock = threading.Lock()  # the reader thread answers cancels while this one answers
    inbox: queue.SimpleQueue[tuple[Any, dict[str, Any] | None] | None] = queue.SimpleQueue()

    def reply(response: dict[str, Any]) -> bool:
        """Write one response line. False if the client has gone away."""
        try:
            try:
                line = json.dumps(response, separators=(",", ":"), allow_nan=False)
            except ValueError:  # a non-finite number slipped into a reply: say so, don't crash
                line = json.dumps(_invalid("internal error: reply was not valid JSON"))
            with write_lock:
                stdout.write(line + "\n")
                stdout.flush()
        except BrokenPipeError:
            return False
        return True

    def read_requests() -> None:
        try:
            for raw in stdin:
                if not raw.strip():
                    continue
                request, response = _parse(raw)
                if response is None and isinstance(request, dict) and request.get("op") == "cancel":
                    session.cancel(request.get("target"))
                    ack: dict[str, Any] = {"ok": True}
                    if "id" in request and isinstance(request["id"], int | str):
                        ack["id"] = request["id"]
                    if not reply(ack):
                        break
                    continue
                inbox.put((request, response))
        finally:
            inbox.put(None)  # end of input

    threading.Thread(target=read_requests, name="stdin-reader", daemon=True).start()
    try:
        while (item := inbox.get()) is not None:
            request, response = item
            if response is None:
                response = session.handle(request)
            if not reply(response):
                break  # the client has gone away; nothing left to do
            if isinstance(request, dict) and request.get("op") == "shutdown":
                break
    finally:
        session.close()
        sys.stdout = saved_stdout
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())

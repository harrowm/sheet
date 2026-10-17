"""The engine process: reads one JSON request per line on stdin, writes one JSON response
per line on stdout. Anything else the program prints goes to stderr, so stdout carries
protocol messages only.

The loop must never die because of what a client sends: every non-blank line gets exactly
one reply, whether it is valid JSON, valid UTF-8, absurdly nested or not an object at all.
"""

from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO, TextIO

from sheet.server.protocol import Session


def _invalid(message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"kind": "invalid", "message": message}}


def _parse(raw: bytes) -> tuple[Any, dict[str, Any] | None]:
    """Decode one request line. Returns (request, None) or (None, error response)."""
    try:
        return json.loads(raw.decode("utf-8")), None
    except UnicodeDecodeError:
        return None, _invalid("request is not valid UTF-8")
    except RecursionError:
        return None, _invalid("request is nested too deeply")
    except json.JSONDecodeError as exc:
        return None, _invalid(f"bad JSON: {exc}")
    except ValueError as exc:  # e.g. an integer with thousands of digits
        return None, _invalid(f"bad request: {exc}")


def serve(stdin: BinaryIO | None = None, stdout: TextIO | None = None) -> int:
    """Serve requests until end of input or a ``shutdown`` request."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    saved_stdout = sys.stdout
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol stream
    session = Session()
    try:
        for raw in stdin:
            if not raw.strip():
                continue
            request, response = _parse(raw)
            if response is None:
                response = session.handle(request)
            try:
                stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                stdout.flush()
            except BrokenPipeError:
                break  # the client has gone away; nothing left to do
            if isinstance(request, dict) and request.get("op") == "shutdown":
                break
    finally:
        session.close()
        sys.stdout = saved_stdout
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())

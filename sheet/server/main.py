"""The engine process: reads one JSON request per line on stdin, writes one JSON response
per line on stdout. Anything else the program prints goes to stderr, so stdout carries
protocol messages only."""

from __future__ import annotations

import json
import sys

from sheet.server.protocol import Session


def serve(stdin=None, stdout=None) -> int:  # type: ignore[no-untyped-def]
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol stream
    session = Session()
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {
                    "ok": False,
                    "error": {"kind": "invalid", "message": f"bad JSON: {exc}"},
                }
            else:
                response = session.handle(request)
            stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            stdout.flush()
            if isinstance(request, dict) and request.get("op") == "shutdown":
                break
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())

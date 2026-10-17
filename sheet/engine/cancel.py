"""Cooperative cancellation and time limits for long operations.

The engine does its work on one request at a time, and a big edit can take many seconds. A
``CancelToken`` lets another thread cancel the running request, and gives each request a
deadline. Long loops call ``check()`` every so often; it raises ``CancelledError``, which unwinds
through the usual all-or-nothing rollback, so a cancelled edit leaves the document as it was.
"""

from __future__ import annotations

import time
from typing import Literal


class CancelledError(Exception):
    """The running operation was cancelled, or ran past its time limit."""

    def __init__(self, reason: Literal["cancelled", "timeout"], message: str) -> None:
        super().__init__(message)
        self.reason = reason


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._deadline: float | None = None
        self._seconds = 0.0

    def start(self, seconds: float | None) -> None:
        """Begin a request: clear any earlier cancel and set its time limit (None for none)."""
        self._cancelled = False
        self._seconds = seconds or 0.0
        self._deadline = time.monotonic() + seconds if seconds else None

    def stop(self) -> None:
        """End a request."""
        self._cancelled = False
        self._deadline = None

    def cancel(self) -> None:
        """Ask the running operation to stop. Safe to call from any thread."""
        self._cancelled = True

    def check(self) -> None:
        """Raise ``CancelledError`` if the operation was cancelled or is past its deadline."""
        if self._cancelled:
            raise CancelledError("cancelled", "Cancelled; nothing was changed")
        deadline = self._deadline
        if deadline is not None and time.monotonic() > deadline:
            raise CancelledError(
                "timeout",
                f"This took longer than the {self._seconds:g} second limit and was stopped; "
                "nothing was changed",
            )

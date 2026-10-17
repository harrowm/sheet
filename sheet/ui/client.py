"""Asynchronous client for the engine process, built on QProcess.

Requests are JSON lines written to the engine's stdin; responses come back as JSON lines on
stdout and are matched to callbacks by id. Nothing here blocks the GUI thread except
``start`` (waits for the process to launch) and ``shutdown`` (waits for it to exit).
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

from sheet.ui.launcher import engine_command

Callback = Callable[[dict[str, Any]], object]  # the return value is ignored


def _engine_error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"kind": "engine", "message": message}}


class EngineClient(QObject):
    ready = pyqtSignal(dict)  # the engine answered hello
    failed = pyqtSignal(str)  # the engine could not start or died unexpectedly
    state_changed = pyqtSignal()  # version, can_undo or can_redo changed
    busy_changed = pyqtSignal(bool)  # requests are outstanding / all answered

    def __init__(self, command: list[str] | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._command = command or engine_command()
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.ForwardedErrorChannel)
        self._process.readyReadStandardOutput.connect(self._on_output)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error)
        self._buffer = bytearray()
        self._pending: dict[int, Callback] = {}
        self._next_id = 1
        self._dead = False
        self._shutting_down = False
        self.hello: dict[str, Any] = {}
        self.exit_code: int | None = None
        self.version = 0
        self.can_undo = False
        self.can_redo = False

    # --- lifecycle ---------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self._dead and self._process.state() != QProcess.ProcessState.NotRunning

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def start(self) -> None:
        program, *args = self._command
        self._process.start(program, args)
        if not self._process.waitForStarted(10_000):
            return  # errorOccurred reports the failure
        self.request("hello", self._on_hello)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Ask the engine to exit and wait for it, killing it if it does not."""
        if self._dead or self._process.state() == QProcess.ProcessState.NotRunning:
            self._dead = True
            return
        self._shutting_down = True
        self._process.write((json.dumps({"id": 0, "op": "shutdown"}) + "\n").encode())
        self._process.closeWriteChannel()
        if not self._process.waitForFinished(timeout_ms):
            self._process.kill()
            self._process.waitForFinished(2000)
        self._dead = True

    # --- requests ----------------------------------------------------------------------

    def request(self, op: str, callback: Callback | None = None, **args: Any) -> int:
        """Send a request; ``callback(response)`` is called later on the GUI thread."""
        req_id = self._next_id
        self._next_id += 1
        callback = callback or (lambda response: None)
        if not self.alive:
            QTimer.singleShot(
                0, lambda: self._safe_call(callback, _engine_error("the engine is not running"))
            )
            return req_id
        was_busy = self.busy
        self._pending[req_id] = callback
        self._process.write(
            (json.dumps({"id": req_id, "op": op, **args}, separators=(",", ":")) + "\n").encode()
        )
        if not was_busy:
            self.busy_changed.emit(True)
        return req_id

    # --- receiving ---------------------------------------------------------------------

    def _on_output(self) -> None:
        self._buffer += bytes(self._process.readAllStandardOutput().data())
        while (end := self._buffer.find(b"\n")) >= 0:
            line = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            if line.strip():
                self._dispatch(line)

    def _dispatch(self, line: bytes) -> None:
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write(f"engine sent invalid JSON: {line[:200]!r}\n")
            return
        self._track_state(response)
        callback = self._pending.pop(response.get("id"), None)
        if callback is not None:
            self._safe_call(callback, response)
        if callback is not None and not self._pending:
            self.busy_changed.emit(False)

    def _track_state(self, response: dict[str, Any]) -> None:
        if "version" not in response:
            return
        state = (
            response["version"],
            response.get("can_undo", False),
            response.get("can_redo", False),
        )
        if state != (self.version, self.can_undo, self.can_redo):
            self.version, self.can_undo, self.can_redo = state
            self.state_changed.emit()

    @staticmethod
    def _safe_call(callback: Callback, response: dict[str, Any]) -> None:
        try:
            callback(response)
        except Exception:  # noqa: BLE001 - a faulty callback must not stop the client
            traceback.print_exc()

    def _on_hello(self, response: dict[str, Any]) -> None:
        if response.get("ok"):
            self.hello = response
            self.ready.emit(response)
        else:
            self.failed.emit(response.get("error", {}).get("message", "engine handshake failed"))

    # --- failure -----------------------------------------------------------------------

    def _fail_pending(self, message: str) -> None:
        pending, self._pending = self._pending, {}
        for callback in pending.values():
            self._safe_call(callback, _engine_error(message))
        if pending:
            self.busy_changed.emit(False)

    def _on_finished(self, exit_code: int, status: QProcess.ExitStatus) -> None:
        self.exit_code = exit_code
        self._dead = True
        if self._shutting_down:
            self._fail_pending("the engine has shut down")
            return
        message = f"the engine stopped unexpectedly (exit code {exit_code})"
        self._fail_pending(message)
        self.failed.emit(message)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._dead = True
            self.failed.emit(f"could not start the engine: {self._command[0]}")

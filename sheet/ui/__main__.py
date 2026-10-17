"""Start the spreadsheet: ``python -m sheet.ui`` (see scripts/run.sh)."""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from types import TracebackType

from PyQt6.QtWidgets import QApplication, QMessageBox

from sheet.ui.client import EngineClient
from sheet.ui.window import MainWindow


def show_startup_error(message: str) -> None:
    QMessageBox.critical(None, "Sheet", message)


def install_excepthook(get_window: Callable[[], MainWindow | None]) -> None:
    """Report an unexpected exception instead of letting PyQt6 abort the application.

    By default, an exception that escapes a Qt slot terminates the whole process (it calls
    ``qFatal``), which would lose the user's unsaved work. With this hook the traceback goes
    to stderr and a message appears in the status bar, and the program carries on.
    """

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        traceback.print_exception(exc_type, exc, tb)
        try:
            window = get_window()
            if window is not None:
                window.show_message(f"Unexpected error: {exc}", error=True)
        except Exception:  # noqa: BLE001 - the window may already be gone; never raise from a hook
            traceback.print_exc()

    sys.excepthook = hook


def create_window(app: QApplication) -> MainWindow | None:
    """Start the engine and open the main window, or explain why that was not possible."""
    client = EngineClient(parent=app)
    client.start()
    if not client.alive:
        show_startup_error(
            "The calculation engine could not be started.\nRun scripts/setup.sh first."
        )
        return None
    window = MainWindow(client)
    window.show()
    return window


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Sheet")
    current: list[MainWindow | None] = [None]
    install_excepthook(lambda: current[0])
    current[0] = create_window(app)
    if current[0] is None:
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

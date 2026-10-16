"""Start the spreadsheet: ``python -m sheet.ui`` (see scripts/run.sh)."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from sheet.ui.client import EngineClient
from sheet.ui.window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Sheet")
    client = EngineClient(parent=app)
    client.start()
    if not client.alive:
        QMessageBox.critical(
            None,
            "Sheet",
            "The calculation engine could not be started.\nRun scripts/setup.sh first.",
        )
        return 1
    window = MainWindow(client)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

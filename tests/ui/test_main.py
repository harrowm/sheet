"""The entry point and the global exception hook."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import sheet.ui.__main__ as entry
from sheet.ui.launcher import repo_root
from sheet.ui.window import MainWindow


class Emitter(QObject):
    ping = pyqtSignal()


@pytest.fixture
def restore_excepthook():
    saved = sys.excepthook
    yield
    sys.excepthook = saved


def test_a_failing_slot_is_reported_instead_of_aborting_the_application(
    engine, qapp, wait_until, restore_excepthook, capsys
):
    # Without a hook, PyQt6 aborts the whole process when a slot raises (exit code 134).
    w = MainWindow(engine, interactive=False)
    w.show()
    entry.install_excepthook(lambda: w)
    emitter = Emitter()

    def bad_slot():
        raise RuntimeError("a bug inside a slot")

    emitter.ping.connect(bad_slot)
    QTimer.singleShot(0, emitter.ping.emit)
    wait_until(lambda: "Unexpected error" in w.status_text())
    assert w.status_is_error() and "a bug inside a slot" in w.status_text()
    assert "RuntimeError: a bug inside a slot" in capsys.readouterr().err  # the traceback is logged
    assert engine.alive and w.view.isEnabled()  # and the application carries on
    w.close()


def test_the_hook_works_before_a_window_exists_and_after_it_is_gone(restore_excepthook, capsys):
    entry.install_excepthook(lambda: None)
    sys.excepthook(ValueError, ValueError("early"), None)
    assert "early" in capsys.readouterr().err


def test_a_hook_that_itself_fails_does_not_raise(restore_excepthook):
    class Broken:
        def show_message(self, *a, **k):
            raise RuntimeError("window already deleted")

    entry.install_excepthook(lambda: Broken())
    sys.excepthook(ValueError, ValueError("x"), None)  # must not raise


def test_create_window_starts_the_engine_and_shows_it(qapp, wait_until):
    w = entry.create_window(qapp)
    assert w is not None and w.client.alive and w.isVisible()
    w.interactive = False
    w.close()


def test_create_window_reports_an_engine_that_cannot_start(qapp, monkeypatch):
    monkeypatch.setenv("SHEET_ENGINE_PYTHON", "/nonexistent/python")
    shown = []
    monkeypatch.setattr(entry, "show_startup_error", shown.append)
    assert entry.create_window(qapp) is None
    assert len(shown) == 1 and "engine" in shown[0].lower()


def test_a_failing_slot_does_not_kill_a_real_process_when_the_hook_is_installed(tmp_path):
    # Without the hook PyQt6 aborts the process (SIGABRT, exit 134: measured, entry 126). That
    # half is deliberately NOT tested here: crashing a subprocess on every run makes macOS
    # log a crash report and pop up "Python quit unexpectedly" each time.
    script = tmp_path / "slot.py"
    script.write_text(
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "from PyQt6.QtCore import QObject, QTimer, pyqtSignal\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "import sheet.ui.__main__ as m\n"
        "app = QApplication([])\n"
        "m.install_excepthook(lambda: None)\n"
        "class E(QObject):\n"
        "    ping = pyqtSignal()\n"
        "e = E()\n"
        "def bad(): raise RuntimeError('boom')\n"
        "e.ping.connect(bad)\n"
        "QTimer.singleShot(0, e.ping.emit)\n"
        "QTimer.singleShot(300, app.quit)\n"
        "sys.exit(app.exec())\n"
    )
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr[-300:]
    assert b"RuntimeError: boom" in done.stderr  # the traceback was logged, not swallowed


def sheet_server_pids():
    out = subprocess.run(
        ["pgrep", "-f", "sheet.server"], capture_output=True, text=True, check=False
    ).stdout
    return set(out.split())


@pytest.mark.skipif(os.name == "nt", reason="uses pgrep and SIGTERM")
def test_the_real_application_starts_runs_and_leaves_no_engine_behind():
    before = sheet_server_pids()
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "sheet.ui"],
        cwd=Path(repo_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(3)
    assert proc.poll() is None, "the application exited by itself"
    assert len(sheet_server_pids() - before) == 1  # it started its engine
    proc.terminate()
    _, err = proc.communicate(timeout=20)
    assert "Traceback" not in err
    deadline = time.monotonic() + 10
    while sheet_server_pids() - before and time.monotonic() < deadline:
        time.sleep(0.2)
    assert sheet_server_pids() - before == set(), "the engine was left running"

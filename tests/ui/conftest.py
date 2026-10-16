"""UI tests run in the standard-Python environment (.venv-ui) where PyQt6 is installed,
and talk to a real engine subprocess running on free-threaded Python. In the engine
environment, where PyQt6 is absent, they are skipped."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    import PyQt6  # noqa: F401
except ImportError:
    collect_ignore_glob = ["test_*.py"]
else:
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    from sheet.ui.client import EngineClient

    @pytest.fixture(scope="session")
    def qapp():
        app = QApplication.instance() or QApplication([])
        yield app

    @pytest.fixture
    def wait_until(qapp):
        def wait(predicate, timeout=6.0, message="condition not met in time"):
            import time

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                QTest.qWait(2)
            raise AssertionError(message)

        return wait

    @pytest.fixture
    def engine(qapp, wait_until):
        client = EngineClient()
        ready = []
        client.ready.connect(ready.append)
        client.start()
        wait_until(lambda: ready, message="engine did not start")
        yield client
        client.shutdown()

    @pytest.fixture
    def call(engine, wait_until):
        """Send a request and wait for its response."""

        def run(op, **args):
            box = []
            engine.request(op, box.append, **args)
            wait_until(lambda: box, message=f"no response to {op}")
            return box[0]

        return run

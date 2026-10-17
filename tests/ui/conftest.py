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
        def wait(predicate, timeout=20.0, message="condition not met in time"):
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

    SLOW_ENGINE = """
import time
from sheet.server import main
from sheet.server.protocol import Session

original_init = Session.__init__

def init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    def slow(_request):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:  # a stand-in for a very long edit
            self._token.check()
            time.sleep(0.002)
        return {}
    self._ops["slow"] = slow

Session.__init__ = init
raise SystemExit(main.serve())
"""

    @pytest.fixture
    def slow_engine(qapp, wait_until):
        """An engine with one extra operation, ``slow``, that runs until it is cancelled."""
        from sheet.ui.launcher import engine_command

        client = EngineClient(command=[*engine_command()[:-2], "-c", SLOW_ENGINE])
        ready = []
        client.ready.connect(ready.append)
        client.start()
        wait_until(lambda: ready, message="slow engine did not start")
        yield client
        client.shutdown()

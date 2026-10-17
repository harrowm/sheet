import sys

from sheet.ui.client import EngineClient
from sheet.ui.launcher import engine_command


def test_engine_command_points_at_a_free_threaded_interpreter():
    cmd = engine_command()
    assert cmd[-2:] == ["-m", "sheet.server"]


def test_start_emits_ready_with_the_engine_description(engine):
    assert engine.alive
    assert engine.hello["gil"] is False  # the engine process runs free-threaded
    assert engine.hello["max_rows"] == 1_048_576
    assert engine.version == 0


def test_requests_get_their_own_responses_in_order(call, engine, wait_until):
    got = []
    for i in range(20):
        engine.request("edit", got.append, entries=[[i, 0, str(i)]])
    wait_until(lambda: len(got) == 20)
    assert [r["version"] for r in got] == list(range(1, 21))  # processed in order
    assert call("cell", at=[19, 0])["text"] == "19"


def test_ids_are_unique_and_returned(engine, wait_until):
    ids = {engine.request("hello") for _ in range(5)}
    assert len(ids) == 5


def test_error_responses_reach_the_callback(call):
    resp = call("edit", entries=[[0, 0, "=1+"]])
    assert resp["ok"] is False and resp["error"]["kind"] == "syntax"


def test_state_tracks_version_and_undo_availability(call, engine, wait_until):
    changes = []
    engine.state_changed.connect(
        lambda: changes.append((engine.version, engine.can_undo, engine.can_redo))
    )
    call("edit", entries=[[0, 0, "1"]])
    assert (engine.version, engine.can_undo, engine.can_redo) == (1, True, False)
    call("undo")
    assert (engine.version, engine.can_undo, engine.can_redo) == (2, False, True)
    assert changes[0] == (1, True, False) and len(changes) >= 2


def test_busy_signal_brackets_outstanding_requests(engine, wait_until):
    states = []
    engine.busy_changed.connect(states.append)
    done = []
    engine.request("hello", done.append)
    engine.request("hello", done.append)
    assert engine.busy
    wait_until(lambda: len(done) == 2)
    assert states == [True, False]
    assert not engine.busy


def test_large_responses_arrive_whole(call):
    entries = [[r, c, f"{r}-{c}"] for r in range(100) for c in range(100)]
    call("edit", entries=entries)
    resp = call("view", rect=[0, 0, 99, 99])  # 10,000 cells, several hundred KB of JSON
    assert resp["cells"][99][99] == ["99-99", "s"]


def test_shutdown_stops_the_engine_cleanly(engine):
    engine.shutdown()
    assert not engine.alive
    assert engine.exit_code == 0


def test_a_crashed_engine_fails_pending_and_later_requests(engine, wait_until):
    failures = []
    engine.failed.connect(failures.append)
    box = []
    engine._process.kill()  # simulate the engine dying
    engine.request("hello", box.append)
    wait_until(lambda: failures and box, message="crash not reported")
    assert box[0]["ok"] is False and box[0]["error"]["kind"] == "engine"
    late = []
    engine.request("hello", late.append)
    wait_until(lambda: late)
    assert late[0]["error"]["kind"] == "engine"
    assert not engine.alive


def test_a_missing_program_reports_failure(qapp, wait_until):
    client = EngineClient(command=["/nonexistent/python", "-m", "sheet.server"])
    failures = []
    client.failed.connect(failures.append)
    client.start()
    wait_until(lambda: failures, message="start failure not reported")
    assert not client.alive


def test_stray_stderr_does_not_break_the_protocol(qapp, wait_until):
    code = (
        "import sys, json\n"
        "print('noise', file=sys.stderr)\n"
        "print(json.dumps({'ok': True, 'id': 1, 'gil': False, 'max_rows': 1, 'max_cols': 1, 'version': 0,"
        " 'can_undo': False, 'can_redo': False}), flush=True)\n"
        "sys.stdin.read()\n"
    )
    client = EngineClient(command=[sys.executable, "-c", code])
    ready = []
    client.ready.connect(ready.append)
    client.start()
    wait_until(lambda: ready)
    client.shutdown()


def test_command_can_be_overridden_by_environment(monkeypatch):
    monkeypatch.setenv("SHEET_ENGINE_PYTHON", "/opt/custom/python")
    assert engine_command() == ["/opt/custom/python", "-m", "sheet.server"]


def test_cancel_stops_outstanding_requests_and_the_engine_stays_usable(slow_engine, wait_until):
    answers = []
    slow_engine.request("slow", answers.append)
    slow_engine.request("edit", answers.append, entries=[[0, 0, "1"]])  # queued behind it
    wait_until(lambda: slow_engine.busy)
    slow_engine.cancel()
    wait_until(lambda: len(answers) == 2)
    assert answers[0]["ok"] is False and answers[0]["error"]["kind"] == "cancelled"
    assert answers[1]["error"]["kind"] == "cancelled"  # the queued edit was cancelled too
    after = []
    slow_engine.request("edit", after.append, entries=[[0, 0, "2"]])
    wait_until(lambda: after)
    assert after[0]["ok"] is True  # nothing was left half done
    assert not slow_engine.busy


def test_cancel_with_nothing_outstanding_does_nothing(engine, call):
    engine.cancel()
    assert call("hello")["ok"] is True

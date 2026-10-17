"""Smoke test for the import/export/paste benchmark: it runs and every round trip is exact."""

from benchmarks.io import main, run


def test_every_operation_runs_and_reproduces_the_original():
    times, problems = run(rows=300)
    assert problems == []
    assert {
        "save (.sheet)",
        "load (.sheet)",
        "import CSV (raw)",
        "paste text",
        "paste block",
    } <= set(times)
    assert all(seconds >= 0 for seconds in times.values())


def test_the_command_line_quick_run_exits_cleanly(capsys):
    assert main(["--quick", "--check"]) == 0
    assert "Every round trip reproduced the original." in capsys.readouterr().out

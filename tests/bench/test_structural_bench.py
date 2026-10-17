"""Smoke test for the structural-edit benchmark: it runs, and its internal checks hold."""

from benchmarks.structural import bulk_edit_scaling, main, structural


def test_the_structural_scenario_runs_and_an_insert_changes_no_value():
    results = structural(cells=600, ranges=60)
    assert set(results) == {
        "insert_rows(0)",
        "undo",
        "redo",
        "insert_cols(0)",
        "delete_cols(0)",
        "delete_rows(5)",
    }
    assert all(seconds >= 0 for seconds in results.values())


def test_the_bulk_edit_scenario_runs_for_each_range_count():
    times = bulk_edit_scaling(inputs=600, range_counts=(10, 40))
    assert set(times) == {10, 40} and all(t >= 0 for t in times.values())


def test_the_command_line_quick_run_exits_cleanly(capsys):
    assert main(["--quick"]) == 0
    out = capsys.readouterr().out
    assert "Scenario 1" in out and "Scenario 2" in out

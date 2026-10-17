"""Behaviour at the edges that the other tests did not pin down (found by coverage)."""

import io
import json
import os

import pytest

import sheet.engine.files as files_module
from sheet.engine.ast import Literal
from sheet.engine.cells import Cell
from sheet.engine.dates import MAX_SERIAL, make_serial, split_serial
from sheet.engine.evaluator import _arith, evaluate
from sheet.engine.files import load_sheet, save_sheet
from sheet.engine.formats import FormatError, validate_format
from sheet.engine.graph import DependencyGraph, RecalcPlan
from sheet.engine.recalc import ParallelRecalculator
from sheet.engine.refs import render
from sheet.engine.sheet import Sheet
from sheet.server.main import serve
from sheet.server.protocol import Session


def calc(formula, **cells):
    s = Sheet(workers=1)
    for name, text in cells.items():
        s.set_entry(int(name[1:]) - 1, ord(name[0]) - 65, text)
    s.set_entry(30, 0, formula)
    return s.get(30, 0)


# --- dates ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("hms", [(24, 0, 0), (-1, 0, 0), (0, 60, 0), (0, 0, 60), (0, -1, 0)])
def test_an_impossible_time_of_day_is_rejected(hms):
    with pytest.raises(ValueError, match="time of day"):
        make_serial(2024, 1, 1, *hms)


def test_a_time_that_rounds_up_past_the_last_day_is_out_of_range():
    with pytest.raises(ValueError):
        split_serial(MAX_SERIAL + 0.999999999)  # rounds to midnight of the day after the last


# --- evaluator -----------------------------------------------------------------------------------------


def test_unary_plus_keeps_the_value_and_coerces_text():
    assert calc("=+A1", A1="7") == 7.0
    assert calc('=+"5"') == 5.0
    assert calc("=+B1") == 0.0  # an empty cell


@pytest.mark.parametrize(
    "formula,expected",
    [
        ("=B1=0", True),
        ('=B1=""', True),
        ("=B1=FALSE", True),
        ("=B1=C1", True),  # empty equals every empty value
        ("=0=B1", True),
        ('=""=B1', True),
        ("=FALSE=B1", True),
        ("=B1<1", True),
        ("=B1>-1", True),
        ('=B1<"a"', True),
        ("=B1<>5", True),
        ("=B1=TRUE", False),
        ('=B1="x"', False),
    ],
)
def test_comparisons_with_empty_cells(formula, expected):
    assert calc(formula) is expected


def test_count_counts_literal_arguments_that_are_numbers():
    assert calc("=COUNT(5)") == 1.0
    assert calc('=COUNT(5,"x",TRUE,7)') == 2.0
    assert calc("=COUNT()") == 0.0


def test_unknown_operators_and_nodes_are_programming_errors():
    with pytest.raises(ValueError, match="Unknown operator"):
        _arith("?", 1.0, 2.0)
    with pytest.raises(TypeError, match="Unknown AST node"):
        evaluate("not a node", None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Unknown AST node"):
        render("not a node")  # type: ignore[arg-type]


def test_a_non_finite_literal_cannot_be_rendered():
    with pytest.raises(ValueError, match="non-finite"):
        render(Literal(float("inf")))


# --- formats ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fmt,fragment",
    [
        ("$", "No digit placeholder"),
        ("%", "No digit placeholder"),
        ("0 0", "contiguous"),
        ('0 "x" 0', "contiguous"),
        ("0.0,0", "Thousands separator after decimal"),
    ],
)
def test_format_validation_messages(fmt, fragment):
    with pytest.raises(FormatError, match=fragment):
        validate_format(fmt)


# --- files -----------------------------------------------------------------------------------------------


def test_a_stored_cell_with_no_text_and_no_format_is_not_saved(tmp_path):
    s = Sheet(workers=1)
    s.set_entry(0, 0, "keep")
    s.store.set_cell(
        5, 5, Cell("", None, None)
    )  # cannot be made through the UI, but must be harmless
    assert save_sheet(s, tmp_path / "a.sheet") == 1
    assert len(load_sheet(tmp_path / "a.sheet").store) == 1


def test_a_failure_to_clean_up_after_a_failed_save_does_not_hide_the_real_error(
    tmp_path, monkeypatch
):
    s = Sheet(workers=1)

    def no_replace(*a, **k):
        raise OSError("disk full")

    def no_unlink(*a, **k):
        raise OSError("cannot remove")

    monkeypatch.setattr(files_module.os, "replace", no_replace)
    monkeypatch.setattr(files_module.os, "unlink", no_unlink)
    with pytest.raises(OSError, match="disk full"):
        save_sheet(s, tmp_path / "a.sheet")


# --- the scheduler's failure paths --------------------------------------------------------------------


class RefusingPool:
    """A pool that has shut down: every submit is refused."""

    def submit(self, *a, **k):
        raise RuntimeError("cannot schedule new futures after shutdown")

    def shutdown(self, wait=True):
        pass


def independent_plan(n):
    g = DependencyGraph()
    for i in range(n):
        g.restore_dependencies((i, 0), [], [])
    return g.plan([(i, 0) for i in range(n)])


def test_a_pool_that_refuses_work_fails_the_run_instead_of_hanging():
    r = ParallelRecalculator(workers=4, threshold=1, grain=2)
    r._pool = RefusingPool()  # type: ignore[assignment]
    with pytest.raises(
        RuntimeError, match="shutdown"
    ):  # fails fast: no node runs once a hand-off failed
        r.run(independent_plan(40), lambda node: None)
    r._pool = None


def test_a_plan_that_never_unlocks_a_node_is_reported_not_silently_dropped():
    a, b = (0, 0), (1, 0)
    broken = RecalcPlan(order=[a, b], in_degree={a: 0, b: 1}, successors={a: [], b: []})
    r = ParallelRecalculator(workers=4, threshold=1)
    with pytest.raises(RuntimeError, match="incomplete"):
        r.run(broken, lambda node: None)
    r.close()


def test_a_closed_recalculator_has_no_pool():
    r = ParallelRecalculator(workers=4, threshold=1)
    r.close()
    assert r._get_pool() is None and not r.is_parallel(1000)


# --- the server and handler safety nets ------------------------------------------------------------------


def test_the_handler_survives_a_bug_in_an_operation(monkeypatch, capsys):
    def broken(self, request):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(Session, "_used_range", broken)  # handlers are bound when a session is made
    session = Session(workers=1)
    resp = session.handle({"op": "used_range", "id": 3})
    assert resp == {
        "ok": False,
        "error": {"kind": "internal", "message": "RuntimeError('engine bug')"},
        "id": 3,
    }
    assert "engine bug" in capsys.readouterr().err  # logged for whoever debugs it
    assert session.handle({"op": "hello"})["ok"] is True  # the session carries on
    session.close()


def test_an_invalid_export_mode_is_refused():
    session = Session(workers=1)
    resp = session.handle({"op": "export_csv", "path": "x.csv", "mode": "pretty"})
    assert (
        resp["ok"] is False and resp["error"]["kind"] == "invalid" and not os.path.exists("x.csv")
    )
    session.close()


def test_a_reply_that_is_not_valid_json_is_replaced_by_an_error(monkeypatch):
    monkeypatch.setattr(Session, "handle", lambda self, request: {"ok": True, "oops": float("nan")})
    out = io.StringIO()
    serve(io.BytesIO(b'{"op":"hello"}\n{"op":"hello"}\n'), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 2 and all(
        r["ok"] is False and r["error"]["kind"] == "invalid" for r in replies
    )

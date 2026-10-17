"""Limits on formula size and nesting: generous (Excel's own), enforced with clear messages,
and always safely inside the interpreter's recursion limit."""

import json
import sys
import threading

import pytest

from sheet.engine.entry import parse_entry
from sheet.engine.files import FileFormatError, load_sheet, save_sheet
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.limits import MAX_FORMULA_LENGTH, MAX_NESTING, REQUIRED_RECURSION_LIMIT
from sheet.engine.parser import parse
from sheet.engine.sheet import Sheet
from sheet.server.protocol import Session


def test_limits_match_excels():
    assert MAX_FORMULA_LENGTH == 8192 and MAX_NESTING == 64


def test_the_recursion_limit_leaves_room_for_the_longest_legal_formula():
    assert sys.getrecursionlimit() >= REQUIRED_RECURSION_LIMIT


# --- nesting ---------------------------------------------------------------------------------------


def parens(n):
    return "=" + "(" * n + "1" + ")" * n


def calls(n):
    return "=" + "SUM(" * n + "1" + ")" * n


@pytest.mark.parametrize("make", [parens, calls])
def test_nesting_up_to_the_limit_works(make):
    assert parse(make(MAX_NESTING)) is not None


@pytest.mark.parametrize("make", [parens, calls])
def test_nesting_beyond_the_limit_is_a_clear_syntax_error(make):
    with pytest.raises(FormulaSyntaxError, match="nested at most 64"):
        parse(make(MAX_NESTING + 1))


def test_parentheses_and_function_calls_count_together():
    mixed = "=" + "(SUM(" * 32 + "1" + "))" * 32  # 64 levels
    assert parse(mixed) is not None
    with pytest.raises(FormulaSyntaxError, match="nested at most 64"):
        parse("=" + "(SUM(" * 33 + "1" + "))" * 33)


def test_sibling_groups_do_not_accumulate_depth():
    assert parse("=" + "+".join(["(1)"] * 500)) is not None


def test_deeply_nested_input_never_reaches_a_recursion_error():
    for n in (1_000, 20_000, 200_000):
        with pytest.raises(FormulaSyntaxError):
            parse(parens(n))


# --- length ------------------------------------------------------------------------------------------


def formula_of_length(n):
    body = "1" + "+1" * ((n - 2) // 2)  # '=' + body
    return "=" + body[: n - 1]


def test_a_formula_of_exactly_the_maximum_length_is_accepted():
    f = formula_of_length(MAX_FORMULA_LENGTH)
    assert len(f) == MAX_FORMULA_LENGTH
    assert parse_entry(f).ast is not None


def test_a_longer_formula_is_a_clear_error():
    with pytest.raises(FormulaSyntaxError, match="8192 characters"):
        parse_entry("=" + "1+" * 4097 + "1")


def test_text_cells_are_not_subject_to_the_formula_limit():
    assert parse_entry("x" * 100_000).value == "x" * 100_000


# --- the longest legal formulas work everywhere --------------------------------------------------------


def long_sum(terms, ref="A1"):
    return "=" + "+".join([ref] * terms)


def test_a_formula_of_over_2700_references_works_end_to_end(tmp_path):
    terms = 2700  # 2 + 3 * 2699 = 8099 characters
    f = long_sum(terms)
    assert len(f) <= MAX_FORMULA_LENGTH
    s = Sheet(workers=1)
    s.set_entry(0, 0, "2")
    s.set_entry(1, 0, f)
    assert s.get(1, 0) == 2.0 * terms
    s.set_entry(0, 0, "3")
    assert s.get(1, 0) == 3.0 * terms
    s.undo()
    s.redo()
    s.copy(1, 0, 1, 0)
    s.paste(s.copy(1, 0, 1, 0), 2, 0)  # relative references shift
    save_sheet(s, tmp_path / "long.sheet")
    assert load_sheet(tmp_path / "long.sheet").get(1, 0) == 3.0 * terms


def test_rewriting_may_lengthen_a_formula_beyond_the_entry_limit():
    s = Sheet(workers=1)
    s.set_entry(8, 0, "1")  # A9
    s.set_entry(0, 1, long_sum(2700, "A9"))  # 8099 characters
    s.insert_rows(0, 1)  # every A9 becomes A10: 2700 more characters
    cell = s.store.get_cell(1, 1)
    assert len(cell.text) > MAX_FORMULA_LENGTH and s.get(1, 1) == 2700.0
    s.undo()
    assert s.get(0, 1) == 2700.0


def test_long_chains_of_prefix_and_power_operators_work():
    s = Sheet(workers=1)
    s.set_entry(0, 0, "=" + "-" * 4000 + "5")
    assert s.get(0, 0) == 5.0  # an even number of minus signs
    s.set_entry(1, 0, "=2" + "^1" * 3000)
    assert s.get(1, 0) == 2.0


def test_long_formulas_recalculate_correctly_in_worker_threads():
    s = Sheet(workers=4, parallel_threshold=1)
    s.set_entry(0, 0, "1")
    f = long_sum(2500)
    s.set_entries([(r, 1, f) for r in range(1, 120)])
    s.set_entry(0, 0, "2")
    assert all(s.get(r, 1) == 5000.0 for r in range(1, 120))
    s.close()


def test_long_formulas_work_on_a_small_stack_thread():
    out = []
    threading.stack_size(512 * 1024)  # the smallest stacks platforms hand to threads
    try:

        def work():
            s = Sheet(workers=1)
            s.set_entry(0, 0, "1")
            s.set_entry(1, 0, long_sum(2700))
            out.append(s.get(1, 0))

        t = threading.Thread(target=work)
        t.start()
        t.join()
    finally:
        threading.stack_size(0)
    assert out == [2700.0]


# --- a recursion error that still gets through is reported, not "internal" --------------------------------


def test_a_recursion_error_is_reported_as_a_formula_problem(monkeypatch):
    import sheet.engine.sheet as sheet_module

    def boom(text):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(sheet_module, "parse_entry", boom)
    session = Session(workers=1)
    resp = session.handle({"op": "edit", "entries": [[0, 0, "=1"]]})
    assert resp["ok"] is False and resp["error"]["kind"] == "syntax"
    assert "too deeply nested" in resp["error"]["message"]
    assert session.handle({"op": "hello"})["ok"] is True
    session.close()


# --- a deeply nested file --------------------------------------------------------------------------------


def test_a_deeply_nested_json_file_is_a_file_error(tmp_path):
    path = tmp_path / "deep.sheet"
    path.write_text("[" * 200_000)
    with pytest.raises(FileFormatError, match="nested too deeply"):
        load_sheet(path)


def test_a_file_whose_cells_are_nested_json_is_rejected_cleanly(tmp_path):
    path = tmp_path / "nested.sheet"
    path.write_text(
        json.dumps(
            {"format": "sheet", "version": 1, "formats": ["General"], "cells": [[0, 0, [[[1]]]]]}
        )
    )
    with pytest.raises(FileFormatError):
        load_sheet(path)

"""Structural edits (insert/delete rows and columns) on large sheets: what they must not redo."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import sheet.engine.compiler as compiler_module
import sheet.engine.sheet as sheet_module
from sheet.engine.entry import parse_entry
from sheet.engine.parser import parse
from sheet.engine.refs import Axis, delete_refs, insert_refs, render
from sheet.engine.sheet import Sheet, _rewritten
from sheet.engine.values import ErrorKind, ErrorValue


def put(s: Sheet, r: int, c: int, text: str):
    s.set_entry(r, c, text)


def sheet_with_formulas(n=50) -> Sheet:
    s = Sheet(workers=1)
    s.set_entries([(r, 0, str(r + 1)) for r in range(n)])  # A1..An = 1..n
    s.set_entries(
        [(r, 1, f"=A{r + 1}*2+(2*3)") for r in range(n)]
    )  # column B: a foldable constant too
    s.clear_history()
    return s


# --- step 1: every rewritten formula is parsed once, not twice -----------------------------------------


@pytest.fixture
def parse_calls(monkeypatch):
    calls = []

    def spy(module, name):
        real = getattr(module, name)

        def wrapper(*a, **k):
            calls.append(name)
            return real(*a, **k)

        monkeypatch.setattr(module, name, wrapper)

    spy(sheet_module, "parse")  # the rewriter's own parse of the unfolded formula
    spy(compiler_module, "parse")  # parse_entry -> compile_formula
    return calls


def test_a_rewritten_formula_is_parsed_exactly_once(parse_calls):
    s = sheet_with_formulas(50)
    parse_calls.clear()  # building the sheet parsed every formula once already
    s.insert_rows(0)  # every reference moves, so all 50 formulas are rewritten
    assert len(parse_calls) == 50
    assert (
        s.store.get_cell(1, 1).text == "=A2*2+2*3"
    )  # regenerated from the unfolded formula, so the constant is kept


def test_formulas_whose_references_do_not_move_are_not_parsed_at_all(parse_calls):
    s = sheet_with_formulas(50)
    parse_calls.clear()
    s.insert_rows(500)  # far below everything: nothing changes
    assert parse_calls == []


# --- the single-parse path gives exactly the same cells as the old two-parse path ---------------------------

FORMULAS = [
    "=A1+B2",
    "=SUM(A1:A9)+(2*3)",
    "=-A1",
    "=A1:B2",
    "=MAX(A1,$B$3,C$4)*2^2",
    '="x"&A1',
    "=A1%+1",
    "=1+2",
    "=A$1+$A2",
    "=SUM(A1:A3,C5:D8)",
    "=IF",
    "=SUM()",
    "=#REF!+A1",
    "=(A1+B1)*(C1-D1)",
]


def reference_rewrite(cell, rewrite):
    """The previous implementation: render the rewritten tree, then parse the text again."""
    if cell.ast is None or rewrite(cell.ast) == cell.ast:
        return cell
    entry = parse_entry("=" + render(rewrite(parse(cell.text.strip()))), enforce_limits=False)
    return type(cell)(entry.text, entry.ast, entry.value, cell.format)


@settings(max_examples=300, deadline=None)
@given(
    st.sampled_from([f for f in FORMULAS if f != "=IF"]),
    st.sampled_from([Axis.ROW, Axis.COL]),
    st.integers(0, 8),
    st.integers(1, 3),
    st.booleans(),
    st.sampled_from(["General", "0.00"]),
)
def test_the_rewriter_matches_the_reference_implementation(formula, axis, at, count, insert, fmt):
    entry = parse_entry(formula)
    from sheet.engine.cells import Cell

    cell = Cell(entry.text, entry.ast, entry.value, fmt)
    rewrite = (
        (lambda n: insert_refs(n, axis, at, count))
        if insert
        else (lambda n: delete_refs(n, axis, at, count))
    )
    new, _ = _rewritten(cell, lambda node, fx: rewrite(node))
    ref = reference_rewrite(cell, rewrite)
    assert (new.text, new.ast) == (ref.text, ref.ast)
    assert new.format == ref.format


# --- step 2: values that cannot have changed are not recalculated -------------------------------------------


@pytest.fixture
def evaluations(monkeypatch):
    calls = []
    real = sheet_module.evaluate

    def spy(node, reader):
        calls.append(1)
        return real(node, reader)

    monkeypatch.setattr(sheet_module, "evaluate", spy)
    return calls


def test_inserting_rows_or_columns_recalculates_nothing(evaluations):
    s = sheet_with_formulas(50)
    expected = {r: s.get(r, 1) for r in range(50)}
    evaluations.clear()
    s.insert_rows(10, 3)
    s.insert_cols(0)
    assert evaluations == []
    for r, value in expected.items():
        assert s.get(r + (3 if r >= 10 else 0), 2) == value  # column B is now column C


def test_inserting_into_a_range_keeps_its_value_without_recalculating(evaluations):
    s = Sheet(workers=1)
    s.set_entries([(r, 0, str(r + 1)) for r in range(5)])
    s.set_entry(0, 2, "=SUM(A1:A5)")
    evaluations.clear()
    s.insert_rows(2)  # a blank row inside the range: the sum is unchanged
    assert s.get(0, 2) == 15.0 and s.store.get_cell(0, 2).text == "=SUM(A1:A6)"
    assert evaluations == []


def test_deleting_rows_recalculates_only_what_it_affected(evaluations):
    s = sheet_with_formulas(50)
    s.set_entry(0, 3, "=B1+B50")  # D1 depends on B1 and B50
    evaluations.clear()
    s.delete_rows(10, 2)  # deletes rows 11-12: B11 and B12 and their A cells vanish
    assert evaluations == []  # B1 and B50 survive, so nothing reads a deleted cell
    assert s.get(0, 3) == 2 * 1 + 6 + 2 * 50 + 6  # D1 unchanged, now reading B48 (the old B50)


def test_a_formula_that_read_a_deleted_cell_becomes_ref_error_and_its_dependents_follow(
    evaluations,
):
    s = sheet_with_formulas(20)
    s.set_entry(0, 3, "=B5+1")  # D1 reads B5
    s.set_entry(0, 4, "=D1*2")  # E1 reads D1
    evaluations.clear()
    s.delete_rows(4)  # deletes row 5, taking B5 with it
    assert s.get(0, 3) == ErrorValue(ErrorKind.REF) and s.get(0, 4) == ErrorValue(ErrorKind.REF)
    assert len(evaluations) == 2  # D1 and E1, nothing else
    s.undo()
    assert s.get(0, 3) == 2 * 5 + 6 + 1 and s.get(0, 4) == (2 * 5 + 6 + 1) * 2


def test_a_range_that_loses_cells_is_recalculated(evaluations):
    s = Sheet(workers=1)
    s.set_entries([(r, 0, str(r + 1)) for r in range(5)])  # 1..5
    s.set_entry(0, 2, "=SUM(A1:A5)")
    s.set_entry(0, 3, "=C1*10")
    evaluations.clear()
    s.delete_rows(1)  # removes the 2: the range shrinks to A1:A4 = 1+3+4+5
    assert s.get(0, 2) == 13.0 and s.get(0, 3) == 130.0
    assert len(evaluations) == 2 and s.store.get_cell(0, 2).text == "=SUM(A1:A4)"


def test_a_range_that_loses_only_empty_rows_keeps_the_right_value(evaluations):
    s = Sheet(workers=1)
    s.set_entries([(0, 0, "1"), (1, 0, "2"), (4, 0, "5")])  # A3 and A4 are empty
    s.set_entry(0, 2, "=SUM(A1:A5)")
    evaluations.clear()
    s.delete_rows(2, 2)  # deletes the two empty rows inside the range
    assert s.get(0, 2) == 8.0 and s.store.get_cell(0, 2).text == "=SUM(A1:A3)"


def test_a_formula_keeps_the_right_value_after_it_moves():
    s = Sheet(workers=1)
    s.set_entry(0, 0, "=B1+1")
    s.set_entry(0, 1, "4")
    s.insert_rows(0)
    assert s.get(1, 0) == 5.0
    s.set_entry(1, 1, "10")  # and it still follows its input
    assert s.get(1, 0) == 11.0

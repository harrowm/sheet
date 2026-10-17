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
from tests.engine.helpers import cell_state


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


# --- step 3: the rewrite runs on several threads for big sheets, with identical results -----------------------


@pytest.fixture
def parallel_rewrite(monkeypatch):
    """Make a small sheet count as 'big' so the threaded path is used."""
    monkeypatch.setattr(sheet_module, "PARALLEL_REWRITE_MIN", 100)
    monkeypatch.setattr(sheet_module, "PARALLEL_REWRITE_CHUNK", 100)


def big_sheet(workers: int, n=600) -> Sheet:
    s = Sheet(workers=workers)
    s.set_entries([(r, 0, str(r + 1)) for r in range(n)])
    s.set_entries([(r, 1, f"=A{r + 1}*2+(2*3)") for r in range(n)])
    s.set_entries([(r, 2, f"=SUM(A{max(1, r - 5)}:A{r + 1})") for r in range(0, n, 3)])
    s.set_entries([(r, 3, f"=B{r + 1}+C{(r // 3) * 3 + 1}") for r in range(n)])
    s.clear_history()
    return s


def test_the_rewrite_uses_several_threads_when_the_sheet_is_big_enough(
    parallel_rewrite, monkeypatch
):
    import threading

    threads = set()
    real = sheet_module._rewritten

    def spy(*a, **k):
        threads.add(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(sheet_module, "_rewritten", spy)
    s = big_sheet(workers=4)
    s.insert_rows(0)
    assert len(threads) > 1
    s.close()


def test_a_small_sheet_or_a_single_worker_rewrites_on_one_thread(monkeypatch):
    import threading

    threads = set()
    real = sheet_module._rewritten

    def spy(*a, **k):
        threads.add(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(sheet_module, "_rewritten", spy)
    big_sheet(workers=4, n=50).insert_rows(0)  # below the threshold: no threads
    monkeypatch.setattr(sheet_module, "PARALLEL_REWRITE_MIN", 100)
    monkeypatch.setattr(sheet_module, "PARALLEL_REWRITE_CHUNK", 100)
    big_sheet(workers=1).insert_rows(0)  # one worker: no threads
    assert threads == {threading.get_ident()}


@pytest.mark.parametrize(
    "op",
    [
        lambda s: s.insert_rows(7, 3),
        lambda s: s.delete_rows(50, 40),
        lambda s: s.insert_cols(1),
        lambda s: s.delete_cols(0),
        lambda s: s.delete_rows(0, 600),
        lambda s: s.insert_rows(0),
    ],
)
def test_threaded_and_sequential_rewrites_give_identical_sheets_undo_and_redo(parallel_rewrite, op):
    seq, par = big_sheet(workers=1), big_sheet(workers=4)
    op(seq)
    op(par)
    assert cell_state(seq) == cell_state(par)
    seq.undo()
    par.undo()
    assert cell_state(seq) == cell_state(par)
    seq.redo()
    par.redo()
    assert cell_state(seq) == cell_state(par)
    par.close()


def test_a_failure_in_a_worker_changes_nothing(parallel_rewrite):
    s = big_sheet(workers=4)
    s.set_entry(1_048_575, 5, "last row")  # inserting would push it off the grid
    before = cell_state(s)
    with pytest.raises(ValueError, match="edge of the grid"):
        s.insert_rows(0)
    assert cell_state(s) == before
    s.close()


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["insert_rows", "delete_rows", "insert_cols", "delete_cols"]),
            st.integers(0, 40),
            st.integers(1, 3),
        ),
        max_size=6,
    )
)
def test_random_structural_sequences_agree_between_threaded_and_sequential(ops):
    saved = (sheet_module.PARALLEL_REWRITE_MIN, sheet_module.PARALLEL_REWRITE_CHUNK)
    sheet_module.PARALLEL_REWRITE_MIN, sheet_module.PARALLEL_REWRITE_CHUNK = 100, 100
    try:
        seq, par = big_sheet(workers=1, n=150), big_sheet(workers=4, n=150)
        for name, at, count in ops:
            for s in (seq, par):
                getattr(s, name)(at, count)
            assert cell_state(seq) == cell_state(par)
        par.close()
    finally:
        sheet_module.PARALLEL_REWRITE_MIN, sheet_module.PARALLEL_REWRITE_CHUNK = saved

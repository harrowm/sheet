import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import sheet.engine.sheet as sheet_module
from sheet.engine.ast import Literal
from sheet.engine.formats import FormatError
from sheet.engine.graph import CycleError, extract_references
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.sheet import Sheet
from sheet.engine.values import ErrorKind, ErrorValue
from tests.engine.helpers import c, full_state, put, val

# --- basic entry and recalculation ----------------------------------------------


def test_numbers_text_and_empty():
    s = Sheet()
    put(s, "A1", "5")
    put(s, "A2", "hello")
    assert val(s, "A1") == 5.0
    assert val(s, "A2") == "hello"
    assert val(s, "A3") is None
    assert s.store.get_cell(*c("A1")).text == "5"


def test_constant_formula_is_precalculated():
    s = Sheet()
    put(s, "A1", "=1/2")
    cell = s.store.get_cell(*c("A1"))
    assert cell.text == "=1/2"
    assert cell.ast == Literal(0.5)
    assert cell.value == 0.5


def test_dependents_recalculate_through_a_chain():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B1", "=A1+1")
    put(s, "C1", "=B1*10")
    assert val(s, "C1") == 20.0
    put(s, "A1", "4")
    assert val(s, "B1") == 5.0
    assert val(s, "C1") == 50.0


def test_formula_can_be_entered_before_its_inputs():
    s = Sheet()
    put(s, "B1", "=A1+1")
    assert val(s, "B1") == 1.0  # empty cell counts as 0
    put(s, "A1", "9")
    assert val(s, "B1") == 10.0


def test_formula_of_an_empty_cell_is_zero_not_empty():
    s = Sheet()
    put(s, "B1", "=A1")
    assert val(s, "B1") == 0.0


def test_range_function_updates_when_cells_in_range_change():
    s = Sheet()
    put(s, "B1", "=SUM(A1:A3)")
    assert val(s, "B1") == 0.0
    put(s, "A2", "5")
    put(s, "A3", "6")
    assert val(s, "B1") == 11.0
    put(s, "A4", "100")  # outside the range
    assert val(s, "B1") == 11.0


def test_errors_propagate_and_clear():
    s = Sheet()
    put(s, "A1", "0")
    put(s, "B1", "=1/A1")
    put(s, "C1", "=B1+1")
    assert val(s, "B1") == ErrorValue(ErrorKind.DIV0)
    assert val(s, "C1") == ErrorValue(ErrorKind.DIV0)
    assert s.store.get_cell(*c("C1")).error is not None
    put(s, "A1", "4")
    assert val(s, "B1") == 0.25
    assert val(s, "C1") == 1.25


def test_set_entry_returns_every_changed_position():
    s = Sheet()
    put(s, "B1", "=A1+1")
    put(s, "C1", "=B1+1")
    assert put(s, "A1", "1") == {c("A1"), c("B1"), c("C1")}


def test_overwriting_a_formula_removes_its_graph_edges():
    s = Sheet()
    put(s, "B1", "=A1")
    put(s, "B1", "7")
    assert s.graph.dependents(c("A1")) == set()
    assert s.graph.dependencies(c("B1")) == (set(), set())


def test_clearing_a_cell_recalculates_dependents():
    s = Sheet()
    put(s, "A1", "5")
    put(s, "B1", "=A1+1")
    put(s, "A1", "")
    assert s.store.get_cell(*c("A1")) is None
    assert val(s, "B1") == 1.0


def test_date_entry_sets_the_format_and_existing_format_is_kept():
    s = Sheet()
    put(s, "A1", "25/12/2024")
    assert s.store.get_cell(*c("A1")).format == "DD/MM/YYYY"
    s.set_format_range(*c("B1"), *c("B1"), "0.00")
    put(s, "B1", "3")
    assert s.store.get_cell(*c("B1")).format == "0.00"


def test_clearing_a_formatted_cell_keeps_the_format():
    s = Sheet()
    s.set_format_range(*c("A1"), *c("A1"), "0.00")
    put(s, "A1", "3")
    put(s, "A1", "")
    cell = s.store.get_cell(*c("A1"))
    assert cell is not None and cell.value is None and cell.format == "0.00"


# --- rejected edits leave the sheet untouched ----------------------------------------


def test_syntax_error_is_rejected_without_changes_or_history():
    s = Sheet()
    put(s, "A1", "1")
    before = full_state(s)
    with pytest.raises(FormulaSyntaxError):
        put(s, "A1", "=1+")
    assert full_state(s) == before
    s.undo()
    assert val(s, "A1") is None  # the only history entry was the valid edit


def test_self_reference_is_rejected():
    s = Sheet()
    put(s, "A1", "5")
    before = full_state(s)
    with pytest.raises(CycleError):
        put(s, "A1", "=A1+1")
    assert full_state(s) == before


def test_cycle_is_rejected_and_everything_is_restored():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B1", "=A1+1")
    put(s, "C1", "=B1+1")
    before = full_state(s)
    with pytest.raises(CycleError):
        put(s, "A1", "=C1")
    assert full_state(s) == before
    put(s, "A1", "10")  # the engine still works afterwards
    assert val(s, "C1") == 12.0


def test_cycle_through_a_range_is_rejected():
    s = Sheet()
    put(s, "C1", "=SUM(A1:A3)")
    with pytest.raises(CycleError):
        put(s, "A2", "=C1")
    assert s.store.get_cell(*c("A2")) is None


def test_out_of_bounds_rejected_without_partial_batch():
    s = Sheet()
    with pytest.raises(IndexError):
        s.set_entries([(0, 0, "1"), (2_000_000, 0, "2")])
    assert len(s.store) == 0


def test_failure_during_recalculation_rolls_everything_back(monkeypatch):
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B1", "=A1+1")
    before = full_state(s)
    real = sheet_module.evaluate
    calls = []

    def flaky(node, reader):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(node, reader)

    monkeypatch.setattr(sheet_module, "evaluate", flaky)
    with pytest.raises(RuntimeError):
        put(s, "A1", "=3*2")  # recalculates A1 then B1; the second evaluation fails
    monkeypatch.setattr(sheet_module, "evaluate", real)
    assert full_state(s) == before


# --- batches -------------------------------------------------------------------------


def test_batch_is_order_independent_and_one_undo_step():
    s = Sheet()
    s.set_entries([(*c("B1"), "=A1+1"), (*c("A1"), "5")])
    assert val(s, "B1") == 6.0
    s.undo()
    assert len(s.store) == 0


def test_batch_with_a_cycle_applies_nothing():
    s = Sheet()
    put(s, "Z1", "1")
    before = full_state(s)
    with pytest.raises(CycleError):
        s.set_entries([(*c("A1"), "=B1"), (*c("B1"), "=A1"), (*c("C1"), "3")])
    assert full_state(s) == before


def test_batch_last_write_wins_for_duplicate_positions():
    s = Sheet()
    s.set_entries([(*c("A1"), "1"), (*c("A1"), "2")])
    assert val(s, "A1") == 2.0


# --- formats -------------------------------------------------------------------------


def test_set_format_range_keeps_values_and_is_undoable():
    s = Sheet()
    put(s, "A1", "5")
    s.set_format_range(*c("A1"), *c("B2"), "0.00")
    assert val(s, "A1") == 5.0
    assert {s.store.get_cell(r, k).format for r in (0, 1) for k in (0, 1)} == {"0.00"}
    s.undo()
    assert s.store.get_cell(*c("A1")).format == "General"
    assert len(s.store) == 1


def test_invalid_format_is_rejected_without_changes():
    s = Sheet()
    with pytest.raises(FormatError):
        s.set_format_range(0, 0, 1, 1, "QQ")
    assert len(s.store) == 0
    assert not s.can_undo


# --- undo / redo ---------------------------------------------------------------------


def test_undo_and_redo_a_simple_edit():
    s = Sheet()
    put(s, "A1", "5")
    assert s.can_undo and not s.can_redo
    assert s.undo() == {c("A1")}
    assert s.store.get_cell(*c("A1")) is None
    assert s.can_redo
    assert s.redo() == {c("A1")}
    assert val(s, "A1") == 5.0


def test_undo_restores_overwritten_formula_and_dependent_values():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B1", "=A1*2")
    put(s, "C1", "=B1+1")
    put(s, "B1", "100")
    assert val(s, "C1") == 101.0
    s.undo()
    assert val(s, "B1") == 2.0
    assert val(s, "C1") == 3.0
    put(s, "A1", "7")  # graph edges were restored too
    assert val(s, "B1") == 14.0
    assert val(s, "C1") == 15.0


def test_undo_of_a_formula_entry_removes_its_edges():
    s = Sheet()
    put(s, "B1", "=A1")
    s.undo()
    assert s.graph.dependents(c("A1")) == set()
    put(s, "A1", "3")
    assert s.store.get_cell(*c("B1")) is None


def test_redo_is_cleared_by_a_new_edit():
    s = Sheet()
    put(s, "A1", "1")
    s.undo()
    put(s, "A2", "2")
    assert not s.can_redo
    assert s.redo() == frozenset()


def test_undo_with_nothing_to_undo_is_a_no_op():
    assert Sheet().undo() == frozenset()


def test_history_depth_is_limited():
    s = Sheet(max_history=2)
    for i in range(3):
        put(s, "A1", str(i + 1))
    s.undo()
    s.undo()
    assert val(s, "A1") == 1.0
    assert not s.can_undo


def test_undo_redo_cycle_many_times_is_stable():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B1", "=A1+1")
    put(s, "A1", "5")
    final = full_state(s)
    for _ in range(3):
        s.undo()
        s.undo()
        s.redo()
        s.redo()
    assert full_state(s) == final


# --- randomised consistency ----------------------------------------------------------

TEXTS = [
    "1",
    "2",
    "-3",
    "hello",
    "",
    "=A1+1",
    "=B2*2",
    "=SUM(A1:A3)",
    "=C3",
    '=A2&"x"',
    "=1/0",
    "=MAX(A1:C1)",
    "=B1",
    "=C2+A3",
    "25/12/2024",
]
POS = st.tuples(st.integers(0, 2), st.integers(0, 2))
OPS = st.lists(
    st.one_of(
        st.tuples(st.just("set"), POS, st.sampled_from(TEXTS)),
        st.just(("undo",)),
        st.just(("redo",)),
    ),
    max_size=40,
)


def check_consistent(s: Sheet):
    for pos, cell in s.store.items():
        refs = extract_references(cell.ast) if cell.ast is not None else (set(), set())
        assert s.graph.dependencies(pos) == refs
    fresh = Sheet()
    fresh.set_entries([(r, k, cell.text) for (r, k), cell in s.store.items() if cell.text])
    for pos, cell in s.store.items():
        if cell.text:
            other = fresh.get(*pos)
            assert type(other) is type(cell.value) and other == cell.value, pos


@settings(max_examples=150, deadline=None)
@given(OPS)
def test_random_operations_keep_graph_and_values_consistent(ops):
    s = Sheet()
    for op in ops:
        if op[0] == "set":
            before = full_state(s)
            try:
                s.set_entry(*op[1], op[2])
            except CycleError:
                assert full_state(s) == before
        elif op[0] == "undo":
            s.undo()
        else:
            s.redo()
        check_consistent(s)

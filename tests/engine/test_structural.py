import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sheet.engine.cells import MAX_COLS, MAX_ROWS
from sheet.engine.graph import CycleError, extract_references
from sheet.engine.sheet import Sheet
from sheet.engine.values import ErrorKind, ErrorValue
from tests.engine.helpers import c, full_state, put, text, val


def column_sheet() -> Sheet:
    s = Sheet()
    for i, v in enumerate(["1", "2", "3"]):
        put(s, f"A{i + 1}", v)
    put(s, "B1", "=SUM(A1:A3)")
    return s


# --- insert / delete rows -------------------------------------------------------------------


def test_insert_rows_moves_cells_and_expands_ranges():
    s = column_sheet()
    s.insert_rows(1)  # new empty row 2
    assert [val(s, f"A{i}") for i in range(1, 5)] == [1.0, None, 2.0, 3.0]
    assert text(s, "B1") == "=SUM(A1:A4)"
    assert val(s, "B1") == 6.0
    put(s, "A2", "10")
    assert val(s, "B1") == 16.0


def test_insert_rows_adjusts_the_formulas_of_cells_that_move():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "A2", "2")
    put(s, "B3", "=A1+A2")
    s.insert_rows(0)
    assert text(s, "B4") == "=A2+A3"
    assert val(s, "B4") == 3.0
    assert s.store.get_cell(*c("B3")) is None


def test_insert_rows_after_a_range_leaves_it_alone():
    s = column_sheet()
    s.insert_rows(3, 5)
    assert text(s, "B1") == "=SUM(A1:A3)"
    assert val(s, "B1") == 6.0


def test_unaffected_formulas_keep_their_exact_text():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B1", "=A1 + (2 * 3)")
    s.insert_rows(5)
    assert text(s, "B1") == "=A1 + (2 * 3)"


def test_rewritten_formulas_keep_constant_folding():
    s = Sheet()
    put(s, "A2", "1")
    put(s, "B1", "=A2+(2*3)")
    s.insert_rows(0)
    assert text(s, "B2") == "=A3+2*3"  # regenerated from the AST (parentheses normalised)
    assert val(s, "B2") == 7.0


def test_delete_rows_shrinks_ranges_and_recalculates():
    s = column_sheet()
    s.delete_rows(1)  # delete the row holding 2
    assert [val(s, f"A{i}") for i in range(1, 4)] == [1.0, 3.0, None]
    assert text(s, "B1") == "=SUM(A1:A2)"
    assert val(s, "B1") == 4.0


def test_delete_rows_turns_references_to_deleted_cells_into_ref_errors():
    s = Sheet()
    put(s, "A2", "5")
    put(s, "B1", "=A2+1")
    put(s, "C1", "=B1*2")
    s.delete_rows(1)
    assert text(s, "B1") == "=#REF!+1"
    assert val(s, "B1") == ErrorValue(ErrorKind.REF)
    assert val(s, "C1") == ErrorValue(ErrorKind.REF)


def test_delete_rows_removes_the_cells_in_the_band_and_their_graph_edges():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "A2", "=A1+1")
    s.delete_rows(1)
    assert len(s.store) == 1
    assert s.graph.dependents(c("A1")) == set()


def test_formats_move_with_cells():
    s = Sheet()
    s.set_format_range(*c("A2"), *c("A2"), "0.00")
    put(s, "A2", "5")
    s.insert_rows(0)
    assert s.store.get_cell(*c("A3")).format == "0.00"
    assert s.store.get_cell(*c("A2")) is None


def test_insert_and_delete_columns():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B1", "2")
    put(s, "C1", "=A1+B1")
    s.insert_cols(1)
    assert text(s, "D1") == "=A1+C1"
    assert val(s, "D1") == 3.0
    s.delete_cols(1)
    assert text(s, "C1") == "=A1+B1"
    assert val(s, "C1") == 3.0


def test_structural_edit_is_one_undo_step():
    s = column_sheet()
    before = full_state(s)
    s.insert_rows(1)
    s.undo()
    assert full_state(s) == before
    s.redo()
    assert text(s, "B1") == "=SUM(A1:A4)"
    s.undo()
    assert full_state(s) == before
    put(s, "A1", "10")  # graph is intact after the undo
    assert val(s, "B1") == 15.0


def test_delete_is_undoable_including_values_and_formulas():
    s = Sheet()
    put(s, "A2", "5")
    put(s, "B1", "=A2+1")
    before = full_state(s)
    s.delete_rows(1)
    s.undo()
    assert full_state(s) == before
    put(s, "A2", "7")
    assert val(s, "B1") == 8.0


def test_insert_that_would_push_cells_off_the_grid_is_rejected():
    s = Sheet()
    s.set_entry(MAX_ROWS - 1, 0, "1")
    before = full_state(s)
    with pytest.raises(ValueError):
        s.insert_rows(0)
    assert full_state(s) == before
    s.set_entry(0, MAX_COLS - 1, "1")
    with pytest.raises(ValueError):
        s.insert_cols(0)


@pytest.mark.parametrize("at,count", [(-1, 1), (0, 0), (0, -2), (MAX_ROWS, 1)])
def test_invalid_structural_arguments(at, count):
    s = Sheet()
    for fn in (s.insert_rows, s.delete_rows):
        with pytest.raises((ValueError, IndexError)):
            fn(at, count)


def test_delete_beyond_used_area_is_a_no_op():
    s = column_sheet()
    before = full_state(s)
    assert s.delete_rows(100, 5) == frozenset()
    assert full_state(s) == before
    assert s.undo() != frozenset()  # the previous (real) edit is still the top of history


# --- copy / paste / fill ----------------------------------------------------------------------


def test_paste_adjusts_relative_references_and_keeps_absolute_ones():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "A3", "10")
    put(s, "B1", "=A1+1")
    put(s, "C1", "=$A$1+A1")
    clip = s.copy(*c("B1"), *c("C1"))
    s.paste(clip, *c("B3"))
    assert text(s, "B3") == "=A3+1"
    assert text(s, "C3") == "=$A$1+A3"
    assert val(s, "B3") == 11.0
    assert val(s, "C3") == 11.0


def test_paste_a_block_keeps_layout_and_clears_targets_for_empty_source_cells():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "B2", "2")
    put(s, "D1", "old")
    put(s, "E2", "old")
    clip = s.copy(*c("A1"), *c("B2"))
    s.paste(clip, *c("D1"))
    assert [val(s, n) for n in ("D1", "E1", "D2", "E2")] == [1.0, None, None, 2.0]


def test_paste_copies_formats():
    s = Sheet()
    put(s, "A1", "5")
    s.set_format_range(*c("A1"), *c("A1"), "$#,##0.00")
    s.paste(s.copy(*c("A1"), *c("A1")), *c("B1"))
    assert s.store.get_cell(*c("B1")).format == "$#,##0.00"


def test_paste_is_one_undo_step():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "A2", "2")
    before = full_state(s)
    s.paste(s.copy(*c("A1"), *c("A2")), *c("C1"))
    assert len(s.store) == 4
    s.undo()
    assert full_state(s) == before


def test_paste_over_the_source_uses_a_snapshot():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "A2", "=A1+1")
    s.paste(s.copy(*c("A1"), *c("A2")), *c("A2"))
    assert val(s, "A2") == 1.0
    assert text(s, "A3") == "=A2+1"
    assert val(s, "A3") == 2.0


def test_paste_off_the_grid_is_rejected_without_changes():
    s = Sheet()
    put(s, "A1", "1")
    put(s, "A2", "2")
    clip = s.copy(*c("A1"), *c("A2"))
    before = full_state(s)
    with pytest.raises(ValueError):
        s.paste(clip, MAX_ROWS - 1, 0)
    assert full_state(s) == before


def test_relative_reference_pasted_off_the_grid_becomes_ref_error():
    s = Sheet()
    put(s, "B2", "=A1")
    s.paste(s.copy(*c("B2"), *c("B2")), *c("A1"))
    assert text(s, "A1") == "=#REF!"
    assert val(s, "A1") == ErrorValue(ErrorKind.REF)


def test_paste_values_only():
    s = Sheet()
    put(s, "A1", "2")
    put(s, "B1", "=A1*5")
    put(s, "C1", "text")
    put(s, "D1", "'123")
    put(s, "E1", "=1/0")
    put(s, "F1", "TRUE")
    s.paste(s.copy(*c("B1"), *c("F1")), *c("B3"), values_only=True)
    assert text(s, "B3") == "10"
    assert s.store.get_cell(*c("B3")).ast is None
    assert val(s, "C3") == "text"
    assert val(s, "D3") == "123"
    assert val(s, "E3") == ErrorValue(ErrorKind.DIV0)
    assert val(s, "F3") is True
    put(s, "A1", "100")
    assert val(s, "B3") == 10.0  # no longer follows A1


def test_paste_cycle_is_rejected_atomically():
    s = Sheet()
    put(s, "A2", "=B1")
    put(s, "B1", "1")
    put(s, "C1", "=B2")
    before = full_state(s)
    with pytest.raises(CycleError):
        s.paste(s.copy(*c("C1"), *c("C1")), *c("B1"))  # B1 becomes "=A2", and A2 is "=B1"
    assert full_state(s) == before


def test_fill_down_and_right():
    s = Sheet()
    put(s, "B1", "1")
    put(s, "B2", "2")
    put(s, "B3", "3")
    put(s, "A1", "=B1*2")
    s.fill_down(*c("A1"), *c("A3"))
    assert [text(s, f"A{i}") for i in (1, 2, 3)] == ["=B1*2", "=B2*2", "=B3*2"]
    assert [val(s, f"A{i}") for i in (1, 2, 3)] == [2.0, 4.0, 6.0]
    put(s, "A5", "=A1+1")
    s.fill_right(*c("A5"), *c("C5"))
    assert text(s, "C5") == "=C1+1"


def test_fill_is_a_single_undo_step_and_constants_are_copied():
    s = Sheet()
    put(s, "A1", "7")
    before = full_state(s)
    s.fill_down(*c("A1"), *c("A4"))
    assert [val(s, f"A{i}") for i in range(1, 5)] == [7.0] * 4
    s.undo()
    assert full_state(s) == before


# --- randomised consistency -----------------------------------------------------------------

TEXTS = [
    "1",
    "2",
    "hello",
    "",
    "=A1+1",
    "=B2*2",
    "=SUM(A1:A3)",
    "=C3",
    "=MAX(A1:C1)",
    "=B1",
    "=$A$1+C2",
]
POS = st.tuples(st.integers(0, 3), st.integers(0, 3))
OPS = st.lists(
    st.one_of(
        st.tuples(st.just("set"), POS, st.sampled_from(TEXTS)),
        st.tuples(
            st.sampled_from(["insert_rows", "delete_rows", "insert_cols", "delete_cols"]),
            st.integers(0, 4),
            st.integers(1, 2),
        ),
        st.tuples(st.just("paste"), POS, POS, POS),
        st.just(("undo",)),
        st.just(("redo",)),
    ),
    max_size=30,
)


def check_consistent(s: Sheet):
    for pos, cell in s.store.items():
        refs = extract_references(cell.ast) if cell.ast is not None else (set(), set())
        assert s.graph.dependencies(pos) == refs
        assert pos[0] < MAX_ROWS and pos[1] < MAX_COLS
    fresh = Sheet()
    fresh.set_entries([(r, k, cell.text) for (r, k), cell in s.store.items() if cell.text])
    for pos, cell in s.store.items():
        if cell.text:
            other = fresh.get(*pos)
            assert type(other) is type(cell.value) and other == cell.value, pos


@settings(max_examples=150, deadline=None)
@given(OPS)
def test_random_structural_operations_keep_graph_and_values_consistent(ops):
    s = Sheet()
    for op in ops:
        before = full_state(s)
        try:
            match op[0]:
                case "set":
                    s.set_entry(*op[1], op[2])
                case "undo":
                    s.undo()
                case "redo":
                    s.redo()
                case "paste":
                    a, b = sorted([op[1], op[2]])
                    s.paste(s.copy(a[0], min(a[1], b[1]), b[0], max(a[1], b[1])), *op[3])
                case name:
                    getattr(s, name)(op[1], op[2])
        except CycleError, ValueError:
            assert full_state(s) == before
        check_consistent(s)

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sheet.engine.cells import Cell, CellStore
from sheet.engine.compiler import compile_formula
from sheet.engine.evaluator import evaluate
from sheet.engine.sheet import Sheet
from sheet.engine.values import ErrorKind, ErrorValue


def num(v):
    return Cell(str(v), None, float(v))


def store_with(values: dict) -> CellStore:
    s = CellStore()
    for (r, c), v in values.items():
        s.set_cell(r, c, Cell("x", None, v))
    return s


# --- the store ----------------------------------------------------------------------------


def test_no_snapshot_unless_requested():
    s = store_with({(0, 0): 1.0})
    assert s.range_snapshot(0, 0, 3, 0) is None


def test_snapshot_holds_only_the_numbers_and_the_first_error():
    err = ErrorValue(ErrorKind.NUM)
    s = store_with(
        {(0, 0): 1.0, (1, 0): "text", (2, 0): True, (3, 0): 4.5, (4, 0): err, (9, 0): 99.0}
    )
    s.begin_snapshots([(0, 0, 4, 0)])
    snap = s.range_snapshot(0, 0, 4, 0)
    assert sorted(snap.numbers) == [1.0, 4.5]
    assert snap.error == err
    assert s.range_snapshot(0, 0, 9, 0) is None  # only requested rectangles
    s.end_snapshots()
    assert s.range_snapshot(0, 0, 4, 0) is None


def test_snapshot_of_a_huge_sparse_rectangle():
    s = store_with({(5, 5): 2.0, (1_000_000, 16_000): 3.0})
    s.begin_snapshots([(0, 0, 1_048_575, 16_383)])
    assert sorted(s.range_snapshot(0, 0, 1_048_575, 16_383).numbers) == [2.0, 3.0]


# --- the evaluator gives the same answer with or without a snapshot -----------------------

VALUES = st.one_of(
    st.none(),
    st.floats(-1e6, 1e6, allow_nan=False),
    st.integers(-5, 5).map(float),
    st.text(alphabet="ab", max_size=2),
    st.booleans(),
    st.just(ErrorValue(ErrorKind.DIV0)),
)
FORMULAS = [
    "=SUM(A1:A8)",
    "=AVERAGE(A1:A8)",
    "=MAX(A1:A8)",
    "=MIN(A1:A8)",
    "=COUNT(A1:A8)",
    "=SUM(A1:A8, 10)+COUNT(A1:A4)",
    "=SUM(A1:A4)+SUM(A5:A8)",
]


@settings(max_examples=200, deadline=None)
@given(st.lists(VALUES, min_size=8, max_size=8), st.sampled_from(FORMULAS))
def test_snapshot_and_live_evaluation_agree(column, formula):
    s = CellStore()
    for r, v in enumerate(column):
        if v is not None:
            s.set_cell(r, 0, Cell("x", None, v))
    ast = compile_formula(formula)
    live = evaluate(ast, s)
    s.begin_snapshots([(0, 0, 7, 0), (0, 0, 3, 0), (4, 0, 7, 0)])
    snapped = evaluate(ast, s)
    s.end_snapshots()
    assert type(live) is type(snapped)
    assert live == snapped or (isinstance(live, ErrorValue) and isinstance(snapped, ErrorValue))


# --- the sheet uses snapshots for shared clean ranges in parallel runs ---------------------


def test_parallel_recalculation_scans_a_shared_clean_range_once(monkeypatch):
    s = Sheet(workers=4, parallel_threshold=1)
    s.set_entries([(r, 5, str(r + 1)) for r in range(10)])  # data in F1:F10
    s.set_entry(0, 7, "1")  # seed in H1
    s.set_entries([(r, 0, f"=SUM($F$1:$F$10)+$H$1+{r}") for r in range(40)])
    scans = []
    real = CellStore._cells_in

    def counting(self, *args):
        scans.append(args)
        return real(self, *args)

    monkeypatch.setattr(CellStore, "_cells_in", counting)
    monkeypatch.setattr(CellStore, "range_values", lambda *a: pytest.fail("live scan used"))
    s.set_entry(0, 7, "2")
    assert s.get(0, 0) == 55.0 + 2 + 0
    assert s.get(39, 0) == 55.0 + 2 + 39
    assert scans == [(0, 5, 9, 5)]  # one snapshot build, then no scans at all
    s.close()


def test_a_range_containing_a_recalculated_formula_is_read_live():
    s = Sheet(workers=4, parallel_threshold=1)
    s.set_entry(0, 0, "1")  # A1
    s.set_entry(1, 0, "=A1+1")  # A2 is a formula inside the range below
    s.set_entries([(r, 2, f"=SUM($A$1:$A$2)+{r}") for r in range(20)])  # C1..C20
    s.set_entry(0, 0, "10")
    assert s.get(1, 0) == 11.0
    assert s.get(0, 2) == 10.0 + 11.0 + 0
    assert s.get(19, 2) == 10.0 + 11.0 + 19
    s.close()


def test_snapshots_are_released_even_when_recalculation_fails(monkeypatch):
    import sheet.engine.sheet as sheet_module

    s = Sheet(workers=4, parallel_threshold=1)
    s.set_entries([(r, 5, "1") for r in range(5)])
    s.set_entry(0, 7, "1")
    s.set_entries([(r, 0, f"=SUM($F$1:$F$5)+$H$1+{r}") for r in range(20)])
    real = sheet_module.evaluate
    n = []

    def boom(node, reader):
        n.append(1)
        if len(n) > 3:
            raise RuntimeError("boom")
        return real(node, reader)

    monkeypatch.setattr(sheet_module, "evaluate", boom)
    with pytest.raises(RuntimeError):
        s.set_entry(0, 7, "2")
    assert s.store.range_snapshot(0, 5, 4, 5) is None
    s.close()


def test_sequential_runs_also_use_snapshots(monkeypatch):
    s = Sheet(workers=1)
    s.set_entries([(r, 5, "1") for r in range(5)])
    s.set_entry(0, 7, "1")
    s.set_entries([(r, 0, f"=SUM($F$1:$F$5)+$H$1+{r}") for r in range(20)])
    monkeypatch.setattr(CellStore, "range_values", lambda *a: pytest.fail("live scan used"))
    s.set_entry(0, 7, "2")
    assert s.get(19, 0) == 5.0 + 2 + 19


def test_a_range_read_by_few_formulas_is_not_snapshotted(monkeypatch):
    s = Sheet(workers=1)
    s.set_entries([(r, 5, "1") for r in range(5)])
    s.set_entry(0, 7, "1")
    s.set_entries([(r, 0, f"=SUM($F$1:$F$5)+$H$1+{r}") for r in range(3)])
    monkeypatch.setattr(CellStore, "begin_snapshots", lambda *a: pytest.fail("snapshot built"))
    s.set_entry(0, 7, "2")

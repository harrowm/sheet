import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sheet.engine.compiler import compile_formula
from sheet.engine.graph import CycleError, DependencyGraph, Rect, extract_references


def set_deps(g: DependencyGraph, cell, cells, ranges=()):
    """Install a cell's dependencies, check for a cycle, and restore the old ones on failure.

    This is the same install / ``plan`` / restore sequence ``Sheet`` performs for an edit, so
    the graph's two halves (``restore_dependencies`` and ``plan``) are tested together.
    """
    old_cells, old_ranges = g.dependencies(cell)
    g.restore_dependencies(cell, cells, ranges)
    try:
        return g.plan([cell])
    except CycleError:
        g.restore_dependencies(cell, old_cells, old_ranges)
        raise


def deps(g: DependencyGraph, name: str, *cells: str, ranges: tuple[Rect, ...] = ()):
    return set_deps(g, c(name), [c(x) for x in cells], ranges)


def range_count(g: DependencyGraph) -> int:
    return len(g._range_dependents)


# --- reference extraction ----------------------------------------------------


def test_extract_references():
    cells, ranges = extract_references(compile_formula("=A1+$B$2*SUM(C1:C3, D4)"))
    assert cells == {c("A1"), c("B2"), c("D4")}
    assert ranges == {Rect(0, 2, 2, 2)}


def test_extract_references_from_constants_and_nested_calls():
    assert extract_references(compile_formula("=1+2")) == (set(), set())
    cells, ranges = extract_references(compile_formula("=MAX(SUM(A1:B2), -C3)"))
    assert cells == {c("C3")}
    assert ranges == {Rect(0, 0, 1, 1)}


# --- edges -------------------------------------------------------------------


def test_installing_dependencies_builds_both_directions():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    assert g.dependencies(c("B1")) == ({c("A1")}, set())
    assert g.dependents(c("A1")) == {c("B1")}
    assert g.dependents(c("B1")) == set()


def test_replacing_dependencies_removes_old_reverse_edges():
    g = DependencyGraph()
    deps(g, "C1", "A1")
    deps(g, "C1", "B1")
    assert g.dependents(c("A1")) == set()
    assert g.dependents(c("B1")) == {c("C1")}


def test_remove_cell_clears_its_dependencies_but_not_its_dependents():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    deps(g, "C1", "B1")
    g.remove_cell(c("B1"))
    assert g.dependencies(c("B1")) == (set(), set())
    assert g.dependents(c("A1")) == set()
    assert g.dependents(c("B1")) == {c("C1")}  # C1 still reads B1


def test_range_dependents_are_found_through_the_index():
    g = DependencyGraph()
    deps(g, "C1", ranges=(Rect(0, 0, 2, 0),))  # A1:A3
    assert g.dependents(c("A1")) == {c("C1")}
    assert g.dependents(c("A3")) == {c("C1")}
    assert g.dependents(c("A4")) == set()
    assert g.dependents(c("B1")) == set()


def test_wide_range_dependents_are_found():
    g = DependencyGraph()
    wide = Rect(0, 0, 5, 300)  # wider than the per-column bucket limit
    deps(g, "A10", ranges=(wide,))
    assert g.dependents((3, 150)) == {c("A10")}
    assert g.dependents((6, 150)) == set()
    assert g.dependents((3, 301)) == set()


def test_removing_range_dependencies_cleans_the_index():
    g = DependencyGraph()
    deps(g, "A10", ranges=(Rect(0, 0, 2, 0), Rect(0, 0, 5, 300)))
    deps(g, "A10")
    assert g.dependents(c("A1")) == set()
    assert g.dependents((3, 150)) == set()
    assert range_count(g) == 0


def test_two_cells_sharing_a_range():
    g = DependencyGraph()
    r = Rect(0, 0, 2, 0)
    deps(g, "C1", ranges=(r,))
    deps(g, "C2", ranges=(r,))
    deps(g, "C1")
    assert g.dependents(c("A2")) == {c("C2")}
    assert range_count(g) == 1


# --- localized Kahn / recalculation plan --------------------------------------


def test_plan_for_a_chain():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    deps(g, "C1", "B1")
    deps(g, "Z9", "Y9")  # unrelated
    plan = g.plan([c("A1")])
    assert plan.order == [c("A1"), c("B1"), c("C1")]
    assert plan.in_degree == {c("A1"): 0, c("B1"): 1, c("C1"): 1}
    assert plan.successors[c("A1")] == [c("B1")]
    assert plan.successors[c("C1")] == []


def test_plan_for_a_diamond_is_topological_and_counts_in_degree():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    deps(g, "C1", "A1")
    deps(g, "D1", "B1", "C1")
    plan = g.plan([c("A1")])
    pos = {n: i for i, n in enumerate(plan.order)}
    assert pos[c("A1")] < pos[c("B1")] < pos[c("D1")]
    assert pos[c("A1")] < pos[c("C1")] < pos[c("D1")]
    assert plan.in_degree[c("D1")] == 2
    assert len(plan.order) == 4


def test_plan_is_localised_to_the_downstream_subgraph():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    deps(g, "B2", "A2")
    assert set(g.plan([c("A2")]).order) == {c("A2"), c("B2")}


def test_plan_with_multiple_seeds_where_one_is_downstream_of_another():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    plan = g.plan([c("A1"), c("B1")])
    assert plan.order == [c("A1"), c("B1")]
    assert plan.in_degree[c("B1")] == 1


def test_plan_follows_range_edges():
    g = DependencyGraph()
    deps(g, "C1", ranges=(Rect(0, 0, 2, 0),))
    deps(g, "D1", "C1")
    assert g.plan([c("A2")]).order == [c("A2"), c("C1"), c("D1")]


def test_plan_for_cell_with_no_dependents():
    assert DependencyGraph().plan([c("A1")]).order == [c("A1")]


# --- cycles and rollback -------------------------------------------------------


def snapshot(g: DependencyGraph, names):
    return {n: (g.dependencies(c(n)), g.dependents(c(n))) for n in names}


def test_self_reference_is_rejected_and_nothing_changes():
    g = DependencyGraph()
    with pytest.raises(CycleError):
        deps(g, "A1", "A1")
    assert g.dependencies(c("A1")) == (set(), set())
    assert g.dependents(c("A1")) == set()


def test_direct_cycle_is_rejected_and_old_edges_are_restored():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    deps(g, "A1", "C1")  # A1 currently depends on C1
    before = snapshot(g, ["A1", "B1", "C1"])
    with pytest.raises(CycleError) as info:
        deps(g, "A1", "B1")  # would make A1 <-> B1
    assert c("A1") in info.value.cells
    assert snapshot(g, ["A1", "B1", "C1"]) == before


def test_indirect_cycle_through_a_range_is_rejected():
    g = DependencyGraph()
    deps(g, "C1", ranges=(Rect(0, 0, 2, 0),))  # C1 = SUM(A1:A3)
    with pytest.raises(CycleError):
        deps(g, "A2", "C1")  # A2 = C1 would close the loop
    assert g.dependencies(c("A2")) == (set(), set())
    assert g.dependents(c("C1")) == set()
    assert g.dependents(c("A2")) == {c("C1")}


def test_rollback_restores_the_range_index():
    g = DependencyGraph()
    deps(g, "C1", ranges=(Rect(0, 0, 2, 0),))
    deps(g, "A2", ranges=(Rect(5, 5, 6, 6),))  # A2 depends on F6:G7
    with pytest.raises(CycleError):
        deps(g, "A2", "C1", ranges=(Rect(9, 9, 9, 9),))
    assert g.dependencies(c("A2")) == (set(), {Rect(5, 5, 6, 6)})
    assert g.dependents((5, 5)) == {c("A2")}
    assert g.dependents((9, 9)) == set()


def test_failed_edit_leaves_graph_usable():
    g = DependencyGraph()
    deps(g, "B1", "A1")
    with pytest.raises(CycleError):
        deps(g, "A1", "B1")
    deps(g, "A1", "C1")  # a valid edit afterwards works
    assert g.plan([c("C1")]).order == [c("C1"), c("A1"), c("B1")]


def test_installing_dependencies_returns_the_plan_for_the_changed_cell():
    g = DependencyGraph()
    deps(g, "C1", "B1")
    plan = deps(g, "B1", "A1")
    assert plan.order == [c("B1"), c("C1")]


# --- scale ---------------------------------------------------------------------


def test_long_chain_has_no_recursion_limit_and_is_fast():
    g = DependencyGraph()
    n = 20_000
    for i in range(1, n):
        set_deps(g, (i, 0), [(i - 1, 0)])
    start = time.perf_counter()
    plan = g.plan([(0, 0)])
    assert time.perf_counter() - start < 10  # ~20 ms; guards against a quadratic plan
    assert plan.order == [(i, 0) for i in range(n)]


def test_wide_fan_out():
    g = DependencyGraph()
    for i in range(1, 5001):
        set_deps(g, (i, 1), [(0, 0)])
    plan = g.plan([(0, 0)])
    assert len(plan.order) == 5001
    assert plan.order[0] == (0, 0)


# --- property test against a brute-force model ---------------------------------


def has_cycle(model: dict[int, set[int]]) -> bool:
    state: dict[int, int] = {}

    def visit(n: int) -> bool:
        state[n] = 1
        for m in model.get(n, ()):
            if state.get(m) == 1 or (state.get(m) is None and visit(m)):
                return True
        state[n] = 2
        return False

    return any(state.get(n) is None and visit(n) for n in list(model))


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 7), st.sets(st.integers(0, 7), max_size=4)), max_size=25))
def test_random_edits_match_brute_force_cycle_detection(edits):
    g = DependencyGraph()
    model: dict[int, set[int]] = {}  # cell -> cells it depends on
    for cell, new_deps in edits:
        candidate = {**model, cell: set(new_deps)}
        expect_cycle = has_cycle(candidate)
        try:
            plan = set_deps(g, (cell, 0), [(d, 0) for d in new_deps])
        except CycleError:
            assert expect_cycle
        else:
            assert not expect_cycle
            model = candidate
            pos = {n: i for i, n in enumerate(plan.order)}
            for n in plan.order:  # every in-plan dependency comes first
                for d in model.get(n[0], ()):
                    if (d, 0) in pos:
                        assert pos[(d, 0)] < pos[n]
        for n in range(8):  # graph always mirrors the model, including after rollback
            assert g.dependencies((n, 0))[0] == {(d, 0) for d in model.get(n, ())}
            assert g.dependents((n, 0)) == {(m, 0) for m, ds in model.items() if n in ds}


def test_restore_dependencies_installs_edges_without_a_cycle_check():
    g = DependencyGraph()
    g.restore_dependencies(c("A1"), [c("B1")], [Rect(0, 5, 2, 5)])
    g.restore_dependencies(c("B1"), [c("A1")], [])  # forms a cycle; trusted, not checked
    assert g.dependents(c("A1")) == {c("B1")}
    assert g.dependents((1, 5)) == {c("A1")}
    g.restore_dependencies(c("A1"), [], [])
    assert g.dependents((1, 5)) == set()
    assert range_count(g) == 0


# --- the range index: which ranges contain a given cell? ----------------------------------------------------

from sheet.engine.graph import _RowIntervals
from tests.engine.helpers import c


def brute(rects, row):
    return {r for r in rects if r.r1 <= row <= r.r2}


def test_row_intervals_basic():
    idx = _RowIntervals()
    assert not idx
    a, b, c = Rect(0, 0, 8, 0), Rect(5, 0, 5, 0), Rect(0, 0, 1_048_575, 0)
    for r in (a, b, c):
        idx.add(r)
    assert idx
    assert set(idx.stab(0)) == {a, c} and set(idx.stab(5)) == {a, b, c} and set(idx.stab(9)) == {c}
    assert set(idx.stab(1_048_575)) == {c}
    idx.remove(b)
    assert set(idx.stab(5)) == {a, c}
    for r in (a, c):
        idx.remove(r)
    assert not idx and list(idx.stab(5)) == []


@settings(max_examples=300, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 60), st.integers(0, 40), st.booleans()),
        min_size=0,
        max_size=60,
    ),
    st.lists(st.integers(0, 120), max_size=15),
)
def test_row_intervals_match_a_brute_force_scan_through_adds_and_removes(spec, probes):
    idx, live = _RowIntervals(), set()
    for start, height, remove in spec:
        rect = Rect(start, 0, start + height, 0)
        if remove and rect in live:
            idx.remove(rect)
            live.discard(rect)
        elif rect not in live:
            idx.add(rect)
            live.add(rect)
        for row in probes:
            assert set(idx.stab(row)) == brute(live, row)
    for row in range(110):
        assert set(idx.stab(row)) == brute(live, row)


def test_many_short_ranges_are_found_without_scanning_them_all():
    g = DependencyGraph()
    n = 20_000
    for r in range(
        n
    ):  # a running sum down column C reading the nine cells above it, as in a ledger
        g.restore_dependencies((r, 2), [], [Rect(max(0, r - 8), 0, r, 0)])
    start = time.perf_counter()
    hits = [len(g.dependents((r, 0))) for r in range(0, n, 2)]
    elapsed = time.perf_counter() - start
    assert (
        hits[5] == 9 and hits[0] == 9
    )  # row 10: the sums at rows 10..18; row 0: those at rows 0..8
    assert elapsed < 5  # 10,000 lookups: ~0.1 s now; scanning all 20,000 ranges for each took ~15 s


def test_removing_and_replacing_ranges_keeps_the_index_exact():
    g = DependencyGraph()
    for r in range(200):
        g.restore_dependencies((r, 2), [], [Rect(r, 0, r + 3, 0)])
    for r in range(0, 200, 2):
        g.restore_dependencies((r, 2), [], [])
    for r in range(1, 200, 2):
        g.restore_dependencies((r, 2), [], [Rect(0, 0, 1, 0)])  # replaced by something else
    assert g.dependents((0, 0)) == {(r, 2) for r in range(1, 200, 2)}
    assert g.dependents((100, 0)) == set()
    assert range_count(g) == 1

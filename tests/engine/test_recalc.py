import os
import threading
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import sheet.engine.sheet as sheet_module
from sheet.engine.graph import CycleError, DependencyGraph, RecalcPlan
from sheet.engine.recalc import AtomicCounter, ParallelRecalculator
from sheet.engine.sheet import Sheet


def make_plan(preds: dict[int, list[int]]) -> RecalcPlan:
    """A plan covering every node of a DAG given as node -> list of nodes it depends on."""
    g = DependencyGraph()
    for node, ps in preds.items():
        g.restore_dependencies((node, 0), [(p, 0) for p in ps], [])
    return g.plan([(n, 0) for n in preds])


def predecessors(plan: RecalcPlan) -> dict:
    out: dict = {n: [] for n in plan.order}
    for node, succs in plan.successors.items():
        for s in succs:
            out[s].append(node)
    return out


class Recorder:
    """A compute function that checks scheduling invariants from any thread."""

    def __init__(self, plan: RecalcPlan, work=None):
        self.preds = predecessors(plan)
        self.done: set = set()
        self.started: list = []
        self.threads: set = set()
        self.violations: list = []
        self.lock = threading.Lock()
        self.work = work

    def __call__(self, node):
        with self.lock:
            self.started.append(node)
            self.threads.add(threading.get_ident())
            missing = [p for p in self.preds[node] if p not in self.done]
            if missing:
                self.violations.append((node, missing))
        if self.work:
            self.work(node)
        with self.lock:
            self.done.add(node)


@pytest.fixture
def pool():
    r = ParallelRecalculator(workers=4, threshold=1, grain=2)
    yield r
    r.close()


# --- AtomicCounter ----------------------------------------------------------------------


def test_atomic_counter_is_exact_under_contention():
    counter = AtomicCounter()

    def work():
        for _ in range(20_000):
            counter.add(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter.value == 160_000
    assert counter.add(-5) == 159_995


# --- scheduling invariants --------------------------------------------------------------


def test_every_node_runs_exactly_once_after_its_dependencies(pool):
    plan = make_plan({0: [], 1: [0], 2: [0], 3: [1, 2], 4: [3], 5: [], 6: [5, 4]})
    rec = Recorder(plan)
    pool.run(plan, rec)
    assert sorted(rec.started) == sorted(plan.order)
    assert len(rec.started) == len(set(rec.started))
    assert rec.violations == []


def test_empty_and_single_node_plans(pool):
    pool.run(make_plan({}), lambda n: pytest.fail("nothing to run"))
    rec = Recorder(plan := make_plan({0: []}))
    pool.run(plan, rec)
    assert rec.started == [(0, 0)]


def test_long_chain_and_wide_fan_out(pool):
    chain = make_plan({i: [i - 1] if i else [] for i in range(5000)})
    rec = Recorder(chain)
    pool.run(chain, rec)
    assert rec.started == [(i, 0) for i in range(5000)]  # a chain can only run in order

    fan = make_plan({0: [], **{i: [0] for i in range(1, 5001)}})
    rec = Recorder(fan)
    pool.run(fan, rec)
    assert len(rec.started) == 5001 and rec.violations == []


def test_independent_work_runs_on_several_threads_and_overlaps(pool):
    plan = make_plan({i: [] for i in range(64)})
    rec = Recorder(plan, work=lambda n: time.sleep(0.01))
    start = time.perf_counter()
    pool.run(plan, rec)
    elapsed = time.perf_counter() - start
    assert len(rec.threads) > 1
    assert elapsed < 0.55  # 64 x 10 ms = 0.64 s sequentially; ~0.2 s with 4 workers


def test_small_plans_run_on_the_calling_thread():
    r = ParallelRecalculator(workers=4, threshold=100)
    plan = make_plan({i: [] for i in range(10)})
    rec = Recorder(plan)
    r.run(plan, rec)
    assert rec.threads == {threading.get_ident()}
    r.close()


def test_single_worker_runs_sequentially_in_plan_order():
    r = ParallelRecalculator(workers=1, threshold=1)
    plan = make_plan({0: [], 1: [0], 2: [1]})
    rec = Recorder(plan)
    r.run(plan, rec)
    assert rec.started == plan.order
    assert rec.threads == {threading.get_ident()}


def test_an_exception_stops_the_run_propagates_and_leaves_the_pool_usable(pool):
    plan = make_plan({0: [], **{i: [0] for i in range(1, 200)}, 200: list(range(1, 200))})
    ran = []

    def compute(node):
        ran.append(node)
        if node == (7, 0):
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        pool.run(plan, compute)
    assert (200, 0) not in ran  # nothing downstream of the failure ran
    rec = Recorder(plan)
    pool.run(plan, rec)  # the pool still works afterwards
    assert len(rec.started) == len(plan.order)


def test_worker_count_defaults_to_the_cpu_count():
    r = ParallelRecalculator()
    assert r.workers == (os.cpu_count() or 1)
    r.close()


def test_close_is_idempotent_and_a_closed_recalculator_still_runs_sequentially():
    r = ParallelRecalculator(workers=4, threshold=1)
    plan = make_plan({0: [], 1: [0]})
    r.run(plan, Recorder(plan))
    r.close()
    r.close()
    rec = Recorder(plan)
    r.run(plan, rec)
    assert len(rec.started) == 2


@st.composite
def dags(draw):
    n = draw(st.integers(1, 60))
    return {
        i: sorted(draw(st.sets(st.integers(0, i - 1), max_size=4))) if i else [] for i in range(n)
    }


@settings(max_examples=100, deadline=None)
@given(dags())
def test_parallel_values_match_sequential_for_random_dags(dag):
    plan = make_plan(dag)

    def evaluate_all(recalc):
        values: dict = {}

        def compute(node):
            values[node] = 1 + sum(
                values[p] for p in preds[node]
            )  # KeyError if a dependency has not run

        preds = predecessors(plan)
        recalc.run(plan, compute)
        return values

    seq = evaluate_all(ParallelRecalculator(workers=1))
    par_r = ParallelRecalculator(workers=4, threshold=1, grain=2)
    try:
        assert evaluate_all(par_r) == seq
    finally:
        par_r.close()


# --- integration with Sheet ---------------------------------------------------------------


def c(name: str) -> tuple[int, int]:
    return int(name[1:]) - 1, ord(name[0]) - ord("A")


def test_sheet_parallel_recalculation_matches_sequential():
    seq = Sheet(workers=1)
    par = Sheet(workers=4, parallel_threshold=1)
    for s in (seq, par):
        s.set_entry(0, 0, "1")
        s.set_entries([(r, 1, f"=$A$1*{r}") for r in range(1, 300)])  # 299 independent formulas
        s.set_entries([(r, 2, f"=B{r + 1}+C{r}") for r in range(1, 300)])  # a dependent running sum
        s.set_entry(0, 0, "3")
    assert {p: x.value for p, x in seq.store.items()} == {p: x.value for p, x in par.store.items()}
    assert par.get(299, 2) == sum(3 * r for r in range(1, 300))
    par.close()


def test_sheet_undo_after_parallel_recalculation():
    s = Sheet(workers=4, parallel_threshold=1)
    s.set_entry(0, 0, "1")
    s.set_entries([(r, 1, f"=A1+{r}") for r in range(1, 100)])
    s.set_entry(0, 0, "10")
    assert s.get(50, 1) == 60.0
    s.undo()
    assert s.get(50, 1) == 51.0
    s.close()


def test_sheet_rolls_back_when_a_worker_fails(monkeypatch):
    s = Sheet(workers=4, parallel_threshold=1)
    s.set_entry(0, 0, "1")
    s.set_entries([(r, 1, f"=A1+{r}") for r in range(1, 50)])
    before = {p: (x.text, x.value) for p, x in s.store.items()}
    real = sheet_module.evaluate
    calls = AtomicCounter()  # a plain list's len() can skip 20 when threads append together

    def flaky(node, reader):
        if calls.add(1) == 20:
            raise RuntimeError("boom")
        return real(node, reader)

    monkeypatch.setattr(sheet_module, "evaluate", flaky)
    with pytest.raises(RuntimeError):
        s.set_entry(0, 0, "5")
    monkeypatch.setattr(sheet_module, "evaluate", real)
    assert {p: (x.text, x.value) for p, x in s.store.items()} == before
    s.set_entry(0, 0, "7")  # still works
    assert s.get(10, 1) == 17.0
    s.close()


def test_sheet_cycle_detection_is_unchanged_with_a_parallel_recalculator():
    s = Sheet(workers=4, parallel_threshold=1)
    s.set_entry(*c("B1"), "=A1")
    with pytest.raises(CycleError):
        s.set_entry(*c("A1"), "=B1")
    s.close()


def test_sheet_context_manager_and_close():
    with Sheet(workers=2, parallel_threshold=1) as s:
        s.set_entry(0, 0, "1")
        s.set_entries([(r, 1, f"=A1+{r}") for r in range(1, 20)])
    s.close()  # idempotent


OPS = st.lists(
    st.one_of(
        st.tuples(
            st.just("set"),
            st.integers(0, 3),
            st.integers(0, 3),
            st.sampled_from(
                [
                    "1",
                    "2",
                    "",
                    "=A1+1",
                    "=B2*2",
                    "=SUM(A1:A4)",
                    "=C3",
                    "=MAX(A1:C1)",
                    "=B1+A2",
                    "=1/A1",
                ]
            ),
        ),
        st.just(("undo",)),
        st.just(("redo",)),
        st.tuples(st.just("insert_rows"), st.integers(0, 3)),
        st.tuples(st.just("delete_rows"), st.integers(0, 3)),
    ),
    max_size=30,
)


@settings(max_examples=60, deadline=None)
@given(OPS)
def test_random_operations_give_identical_results_sequential_and_parallel(ops):
    seq = Sheet(workers=1)
    par = Sheet(workers=3, parallel_threshold=1)
    try:
        for op in ops:
            for s in (seq, par):
                try:
                    match op[0]:
                        case "set":
                            s.set_entry(op[1], op[2], op[3])
                        case "undo":
                            s.undo()
                        case "redo":
                            s.redo()
                        case name:
                            getattr(s, name)(op[1])
                except CycleError, ValueError:
                    pass
            assert {p: (x.text, x.value, x.format) for p, x in seq.store.items()} == {
                p: (x.text, x.value, x.format) for p, x in par.store.items()
            }
    finally:
        par.close()


# --- ParallelRecalculator.map -------------------------------------------------------------------------------


def test_map_applies_the_function_to_every_item_in_order_on_several_threads():
    r = ParallelRecalculator(workers=4, threshold=1)
    seen = set()

    def work(chunk):
        seen.add(threading.get_ident())
        time.sleep(0.01)
        return [x * 2 for x in chunk]

    items = list(range(10_000))
    out = r.map(work, items, min_items=100, chunk=500)
    assert [x for part in out for x in part] == [x * 2 for x in items]
    assert len(seen) > 1
    r.close()


def test_map_is_sequential_for_small_inputs_one_worker_or_a_closed_pool():
    main = threading.get_ident()
    ids = []

    def work(chunk):
        ids.append(threading.get_ident())
        return list(chunk)

    items = list(range(50))
    assert ParallelRecalculator(workers=4).map(work, items, min_items=100) == [items]
    assert ParallelRecalculator(workers=1).map(work, items * 100, min_items=10, chunk=50) == [
        items * 100
    ]
    closed = ParallelRecalculator(workers=4)
    closed.close()
    closed.map(work, items * 100, min_items=10, chunk=50)
    assert set(ids) == {main}


def test_map_propagates_an_exception_from_any_chunk():
    r = ParallelRecalculator(workers=4)

    def work(chunk):
        if 4200 in chunk:
            raise KeyError("boom")
        return chunk

    with pytest.raises(KeyError):
        r.map(work, list(range(10_000)), min_items=100, chunk=500)
    assert r.map(lambda c: c, [1, 2, 3], min_items=100) == [[1, 2, 3]]  # still usable
    r.close()


def test_map_of_nothing_is_nothing():
    assert ParallelRecalculator(workers=4).map(lambda c: c, [], min_items=1) == []

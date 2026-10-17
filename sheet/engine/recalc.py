"""Task-based parallel recalculation over a dependency plan.

The plan (from ``DependencyGraph.plan``) gives every node's in-degree and successor list.
Each node gets a pending-dependency counter; when a node finishes it decrements its
successors' counters and any that reach zero are ready to run. Ready nodes are handled in
batches by a thread pool: a worker keeps the ready nodes it unlocks and runs them itself
(so a chain costs no pool round trips) and hands a batch back to the pool when it is
holding more than it should or when pool threads sit idle. An outstanding-task counter
tells the caller when the whole plan has finished. Python has no lock-free atomics, so
counters use tiny locks: one striped lock per group of nodes for the pending counters, and
one inside ``AtomicCounter``.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TypeVar

from sheet.engine.graph import Cell, RecalcPlan

DEFAULT_THRESHOLD = 256  # plans smaller than this run on the calling thread
DEFAULT_GRAIN = 8  # nodes per hand-off to the pool; amortises task overhead
DEFAULT_MAP_CHUNK = 5_000  # items per task for ``map``
_STRIPES = 64

Compute = Callable[[Cell], None]
_T = TypeVar("_T")
_R = TypeVar("_R")


class AtomicCounter:
    """An integer that can be updated from several threads."""

    __slots__ = ("_lock", "_value")

    def __init__(self, value: int = 0) -> None:
        self._lock = threading.Lock()
        self._value = value

    def add(self, delta: int) -> int:
        """Add ``delta`` and return the new value."""
        with self._lock:
            self._value += delta
            return self._value

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class _Run:
    """State of one parallel recalculation."""

    def __init__(
        self, plan: RecalcPlan, compute: Compute, pool: ThreadPoolExecutor, grain: int, workers: int
    ) -> None:
        self.plan = plan
        self.compute = compute
        self.pool = pool
        self.grain = grain
        self.workers = workers
        self.pending = dict(plan.in_degree)
        self.stripes = [threading.Lock() for _ in range(_STRIPES)]
        self.outstanding = AtomicCounter()  # tasks submitted and not yet finished
        self.completed = AtomicCounter()  # nodes computed
        self.done = threading.Event()
        self.failed = False
        self.error: BaseException | None = None
        self._error_lock = threading.Lock()

    def unlock_successor(self, node: Cell) -> bool:
        """Count one finished dependency of ``node``; True if it is now ready to run."""
        with self.stripes[hash(node) % _STRIPES]:
            left = self.pending[node] - 1
            self.pending[node] = left
        return left == 0

    def fail(self, exc: BaseException) -> None:
        with self._error_lock:
            if self.error is None:
                self.error = exc
        self.failed = True

    def submit(self, batch: list[Cell]) -> None:
        self.outstanding.add(1)
        try:
            self.pool.submit(self.work, batch)
        except BaseException as exc:  # noqa: BLE001 - captured and re-raised on the calling thread
            self.fail(exc)
            self.finish_task()

    def finish_task(self) -> None:
        if self.outstanding.add(-1) == 0:
            self.done.set()

    def rebalance(self, local: deque[Cell]) -> None:
        """Hand surplus ready nodes back to the pool.

        Two cases: the worker holds more than a working set (``2 * grain``), or pool
        threads are idle while it holds several nodes, in which case half go back so the
        work spreads out. The oldest nodes are handed over; the worker keeps its newest,
        which it runs next.
        """
        while len(local) > 2 * self.grain:
            self.submit([local.popleft() for _ in range(self.grain)])
        if len(local) > 1 and self.outstanding.value < self.workers:
            self.submit([local.popleft() for _ in range(len(local) // 2)])

    def work(self, batch: list[Cell]) -> None:
        """Run a batch of ready nodes and everything they unlock."""
        local = deque(batch)
        try:
            while local and not self.failed:
                node = local.pop()
                self.compute(node)
                self.completed.add(1)
                for nxt in self.plan.successors[node]:
                    if self.unlock_successor(nxt):
                        local.append(nxt)
                self.rebalance(local)
        except BaseException as exc:  # noqa: BLE001 - captured and re-raised on the calling thread
            self.fail(exc)
        finally:
            self.finish_task()


MAX_DEFAULT_WORKERS = (
    4  # measured best; more threads slowed both recalculation and structural edits
)


def default_workers() -> int:
    """Worker threads to use: ``SHEET_WORKERS`` if set to a positive integer, else the CPU
    count capped at four (extra threads contended for memory and, on machines with
    efficiency cores, ran slower)."""
    try:
        wanted = int(os.environ.get("SHEET_WORKERS", ""))
    except ValueError:
        wanted = 0
    return wanted if wanted > 0 else max(1, min(os.cpu_count() or 1, MAX_DEFAULT_WORKERS))


def no_check() -> None:
    pass


class ParallelRecalculator:
    def __init__(
        self,
        workers: int | None = None,
        threshold: int = DEFAULT_THRESHOLD,
        grain: int = DEFAULT_GRAIN,
    ) -> None:
        self.workers = workers if workers is not None else default_workers()
        self.threshold = threshold
        self.grain = grain
        self._pool: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        self._closed = False
        # Called at the start of every ``map`` chunk; it raises to abandon the work (see
        # ``cancel.py``). Chunks that have not started then stop at once.
        self.checkpoint: Callable[[], None] = no_check

    def close(self) -> None:
        """Shut the thread pool down. Later runs execute sequentially."""
        with self._pool_lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)

    def _get_pool(self) -> ThreadPoolExecutor | None:
        with self._pool_lock:
            if self._closed:
                return None
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self.workers, thread_name_prefix="recalc"
                )
            return self._pool

    def map(
        self,
        fn: Callable[[Sequence[_T]], _R],
        items: Sequence[_T],
        min_items: int,
        chunk: int = DEFAULT_MAP_CHUNK,
    ) -> list[_R]:
        """Apply ``fn`` to contiguous chunks of ``items`` and return the results in order.

        Chunks run on the pool, and the calling thread takes the first one itself. Fewer
        than ``min_items`` items, a single worker or a closed recalculator run ``fn`` once,
        on the calling thread, over everything. An exception from any chunk is re-raised here
        after all chunks have finished, so nothing is still running when the caller reacts.
        """
        n = len(items)
        if n == 0:
            return []
        pool = self._get_pool() if self.workers > 1 and n >= min_items else None
        size = max(chunk, -(-n // (self.workers * 4)))  # at most about four chunks per worker
        checkpoint = self.checkpoint

        def run_chunk(part: Sequence[_T]) -> _R:
            checkpoint()
            return fn(part)

        if pool is None or size >= n:
            return [run_chunk(items)]
        parts = [items[i : i + size] for i in range(0, n, size)]
        futures = [pool.submit(run_chunk, part) for part in parts[1:]]
        try:
            results = [run_chunk(parts[0])]
        finally:
            wait(futures)  # never leave chunks running behind an exception
        return results + [f.result() for f in futures]

    def is_parallel(self, nodes: int) -> bool:
        """Whether a plan of ``nodes`` cells would run on the thread pool."""
        return self.workers > 1 and nodes >= max(self.threshold, 1) and not self._closed

    def run(self, plan: RecalcPlan, compute: Compute) -> None:
        """Call ``compute(node)`` once for every node, each after all its dependencies.

        Small plans, a single worker, or a closed recalculator run sequentially on the
        calling thread. The first exception raised by ``compute`` stops the run (no
        further nodes start) and is re-raised here once running tasks have finished.
        """
        n = len(plan.order)
        pool = self._get_pool() if self.is_parallel(n) else None
        if pool is None:
            for node in plan.order:
                compute(node)
            return

        run = _Run(plan, compute, pool, self.grain, self.workers)
        ready = [node for node in plan.order if plan.in_degree[node] == 0]
        k = min(len(ready), self.workers * 2)
        chunks = [ready[i::k] for i in range(k)]
        run.outstanding.add(len(chunks))  # count every chunk before any can finish
        for chunk in chunks[1:]:
            try:
                pool.submit(run.work, chunk)
            except BaseException as exc:  # noqa: BLE001 - captured and re-raised below
                run.fail(exc)
                run.finish_task()
        run.work(chunks[0])  # the calling thread works too
        run.done.wait()
        if run.error is not None:
            raise run.error
        if run.completed.value != n:
            raise RuntimeError(f"Recalculation incomplete: {run.completed.value} of {n} nodes ran")

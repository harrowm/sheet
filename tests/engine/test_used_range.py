import random

from sheet.engine.cells import Cell, CellStore


def slow(store: CellStore):
    keys = [pos for pos, _ in store.items()]
    if not keys:
        return None
    rows, cols = [r for r, _ in keys], [c for _, c in keys]
    return min(rows), min(cols), max(rows), max(cols)


def test_used_range_follows_every_kind_of_write():
    store = CellStore()
    assert store.used_range() is None
    store.set_cell(5, 7, Cell("1", None, 1.0))
    assert store.used_range() == (5, 7, 5, 7)
    store.set_cell(2, 9, Cell("1", None, 1.0))
    assert store.used_range() == (2, 7, 5, 9)
    store.set_cell(5, 7, Cell("2", None, 2.0))  # replacing changes nothing
    assert store.used_range() == (2, 7, 5, 9)
    store.remove(2, 9)  # removing the extreme shrinks it
    assert store.used_range() == (5, 7, 5, 7)
    store.remove(5, 7)
    assert store.used_range() is None
    store.remove(5, 7)  # removing a missing cell is harmless
    assert store.used_range() is None


def test_used_range_matches_a_full_scan_after_random_writes_and_removals():
    rnd = random.Random(7)
    store = CellStore()
    for _ in range(600):
        pos = (rnd.randrange(30), rnd.randrange(30))
        if rnd.random() < 0.35:
            store.remove(*pos)
        else:
            store.set_cell(*pos, Cell("x", None, "x"))
        if rnd.random() < 0.5:  # ask at random moments, so the cache is used and invalidated
            assert store.used_range() == slow(store)
    assert store.used_range() == slow(store)


def test_the_answer_is_cached_between_writes():
    store = CellStore()
    store.set_cell(1, 1, Cell("1", None, 1.0))
    first = store.used_range()
    assert store.used_range() is first  # the same tuple: nothing was recomputed


def test_edits_inside_the_range_and_removals_off_its_edge_keep_the_cache():
    store = CellStore()
    for pos in [(0, 0), (9, 9), (4, 4)]:
        store.set_cell(*pos, Cell("1", None, 1.0))
    first = store.used_range()
    store.set_cell(4, 5, Cell("1", None, 1.0))  # inside the range
    store.remove(4, 4)  # not on an edge
    assert store.used_range() is first
    store.remove(9, 9)  # on an edge: must be recomputed
    assert store.used_range() == (0, 0, 4, 5)

"""A formula that reaches no further than a structural edit is skipped, not rewritten."""

import contextlib
import random

import pytest

from sheet.engine import sheet as sheet_module
from sheet.engine.graph import CycleError
from sheet.engine.parser import parse
from sheet.engine.refs import REACH_SHIFT, reach
from sheet.engine.sheet import Sheet

MASK = (1 << REACH_SHIFT) - 1


@pytest.mark.parametrize(
    ("formula", "rows", "cols"),
    [
        ("=1+2", 0, 0),
        ("=A1", 1, 1),
        ("=C7", 7, 3),
        ("=SUM(A1:B9)", 9, 2),
        ("=SUM(B9:A1)", 9, 2),  # corners in any order
        ("=-A2+Z1%", 2, 26),
        ('=IF(A1>5,"x"&D4,SUM(E2:E3))', 4, 5),
        ("=XFD1048576", 1_048_576, 16_384),
    ],
)
def test_reach_is_the_highest_row_and_column_referenced(formula, rows, cols):
    packed = reach(parse(formula))
    assert (packed >> REACH_SHIFT, packed & MASK) == (rows, cols)


def test_the_cache_is_filled_once_and_not_carried_to_changed_cells():
    sheet = Sheet()
    sheet.set_entries([(0, 0, "1"), (1, 0, "=A1+1")])
    cell = sheet.store.get_cell(1, 0)
    assert cell.reach_cache == -1
    sheet.insert_rows(5)
    assert cell.reach_cache >= 0  # computed by the edit
    sheet.set_entries([(0, 0, "2")])  # recalculation replaces the formula cell
    assert sheet.store.get_cell(1, 0).reach_cache == -1


def _build(seed: int) -> Sheet:
    rnd = random.Random(seed)
    sheet = Sheet(workers=1)
    entries = []
    for _ in range(60):
        r, c = rnd.randrange(12), rnd.randrange(6)
        kind = rnd.randrange(4)
        a = f"{chr(65 + rnd.randrange(6))}{rnd.randrange(1, 13)}"
        b = f"{chr(65 + rnd.randrange(6))}{rnd.randrange(1, 13)}"
        entries.append(
            (
                r,
                c,
                [str(rnd.randrange(100)), f"={a}+1", f"=SUM({a}:{b})", f"=IF({a}>5,{b},{a}*2)"][
                    kind
                ],
            )
        )
    for entry in entries:  # a random entry may make a circular reference: skip it
        with contextlib.suppress(CycleError):
            sheet.set_entries([entry])
    return sheet


def _state(sheet: Sheet):
    return {pos: (c.text, c.ast, c.value, c.format) for pos, c in sheet.store.items()}


@pytest.mark.parametrize("seed", range(40))
def test_skipping_unreaching_formulas_gives_the_same_result_as_rewriting_all(seed, monkeypatch):
    rnd = random.Random(seed + 1000)
    ops = [
        (
            rnd.choice(["insert_rows", "delete_rows", "insert_cols", "delete_cols"]),
            rnd.randrange(0, 14),
            rnd.randrange(1, 3),
        )
        for _ in range(6)
    ]
    fast, slow = _build(seed), _build(seed)
    monkeypatch.setattr(
        sheet_module, "_reaches_at_most", lambda cell, axis, at, compute=True: False
    )
    for name, at, count in ops:
        getattr(slow, name)(at, count)
    monkeypatch.undo()
    for name, at, count in ops:
        getattr(fast, name)(at, count)
    assert _state(fast) == _state(slow)
    for _ in ops:  # and undo walks both back identically
        fast.undo()
        slow.undo()
    assert _state(fast) == _state(slow)

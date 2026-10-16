"""Spreadsheet shapes for the parallel-recalculation benchmarks.

Every formula cell carries a deliberately heavy term that evaluates to 0, so each cell costs
a real amount of work (a sum over a shared data range plus a few dozen multiplications)
without changing its value. Changing
the single seed cell then recalculates the whole shape.

  wide     many independent chains hanging off the seed: ideal for parallelism
  fan_out  one level of independent cells on the seed: a pure scheduling test
  lattice  layers where each cell reads two cells of the layer above: a diamond mesh
  deep     one long chain: nothing to parallelise, so it measures pool overhead
"""

from __future__ import annotations

from dataclasses import dataclass

from sheet.engine.sheet import Sheet

DATA_COL = 100
SEED_COL = 101
DATA_ROWS = 2000


def a1(row: int, col: int) -> str:
    letters = ""
    n = col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"{letters}{row + 1}"


_DATA = f"${a1(0, DATA_COL)[:-1]}$1:${a1(0, DATA_COL)[:-1]}${DATA_ROWS}"
SEED = f"${a1(0, SEED_COL)[:-1]}$1"
TERMS = 40  # arithmetic terms per cell; tunes how heavy each cell is


def heavy(prev: str) -> str:
    """A term that always evaluates to 0 but costs real work: a shared range sum plus
    ``TERMS`` multiplications of the cell this one depends on."""
    products = "+".join(f"{prev}*{i}" for i in range(1, TERMS))
    return f"(SUM({_DATA})+{products})*0"


@dataclass(frozen=True)
class Shape:
    name: str
    formula_cells: int
    build: object  # Callable[[Sheet], list[tuple[int, int, str]]]; kept loose for simplicity


def _prepare(sheet: Sheet) -> None:
    sheet.set_entries([(r, DATA_COL, str(r + 1)) for r in range(DATA_ROWS)])
    sheet.set_entry(0, SEED_COL, "1")


def build_wide(sheet: Sheet, chains: int = 64, length: int = 20) -> int:
    _prepare(sheet)
    edits = []
    for j in range(chains):
        for i in range(length):
            prev = SEED if i == 0 else a1(j, i - 1)
            edits.append((j, i, f"={heavy(prev)}+{prev}"))
    sheet.set_entries(edits)
    return len(edits)


def build_fan_out(sheet: Sheet, n: int = 1500) -> int:
    _prepare(sheet)
    sheet.set_entries([(r, 0, f"={heavy(SEED)}+{SEED}") for r in range(n)])
    return n


def build_lattice(sheet: Sheet, width: int = 32, layers: int = 20) -> int:
    _prepare(sheet)
    edits = []
    for layer in range(layers):
        for w in range(width):
            if layer == 0:
                edits.append((w, 0, f"={heavy(SEED)}+{SEED}"))
            else:
                left, right = a1((w - 1) % width, layer - 1), a1((w + 1) % width, layer - 1)
                edits.append((w, layer, f"={heavy(left)}+({left}+{right})/2"))
    sheet.set_entries(edits)
    return len(edits)


def build_deep(sheet: Sheet, length: int = 600) -> int:
    _prepare(sheet)
    edits = []
    for i in range(length):
        prev = SEED if i == 0 else a1(i - 1, 0)
        edits.append((i, 0, f"={heavy(prev)}+{prev}"))
    sheet.set_entries(edits)
    return length


SHAPES = {
    "wide": build_wide,
    "fan_out": build_fan_out,
    "lattice": build_lattice,
    "deep": build_deep,
}


def edit_seed(sheet: Sheet, value: int) -> None:
    """The measured operation: change the seed and recalculate everything downstream."""
    sheet.set_entry(0, SEED_COL, str(value))


def snapshot(sheet: Sheet) -> dict:
    return {pos: cell.value for pos, cell in sheet.store.items() if pos[1] < DATA_COL}

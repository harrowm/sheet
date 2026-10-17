import pytest
from PyQt6.QtCore import QModelIndex, Qt

from sheet.ui.model import BLOCK_COLS, BLOCK_ROWS, SheetModel

DISPLAY = Qt.ItemDataRole.DisplayRole
EDIT = Qt.ItemDataRole.EditRole
ALIGN = Qt.ItemDataRole.TextAlignmentRole
FOREGROUND = Qt.ItemDataRole.ForegroundRole


@pytest.fixture
def model(engine, qapp):
    return SheetModel(engine)


@pytest.fixture
def requests_seen(engine):
    """Every (op, args) the client is asked to send."""
    seen = []
    original = engine.request

    def spy(op, callback=None, **args):
        seen.append((op, args))
        return original(op, callback, **args)

    engine.request = spy
    return seen


def idx(model, row, col):
    return model.index(row, col)


def shown(model, row, col):
    return model.data(idx(model, row, col), DISPLAY)


def test_dimensions_cover_the_whole_grid(model):
    assert model.rowCount() == 1_048_576
    assert model.columnCount() == 16_384
    assert model.rowCount(QModelIndex()) == 1_048_576
    assert model.rowCount(idx(model, 0, 0)) == 0  # a flat table has no children


def test_headers_are_column_letters_and_one_based_rows(model):
    H, V = Qt.Orientation.Horizontal, Qt.Orientation.Vertical
    assert [model.headerData(i, H) for i in (0, 25, 26, 16383)] == ["A", "Z", "AA", "XFD"]
    assert [model.headerData(i, V) for i in (0, 1_048_575)] == ["1", "1048576"]


def test_cells_start_blank_then_fill_in_asynchronously(model, call, wait_until):
    call("edit", entries=[[0, 0, "42"], [1, 1, "hello"]])
    assert shown(model, 0, 0) in ("", None)  # not fetched yet
    changed = []
    model.dataChanged.connect(lambda tl, br, roles=None: changed.append((tl.row(), br.row())))
    wait_until(lambda: shown(model, 0, 0) == "42")
    assert shown(model, 1, 1) == "hello"
    assert changed  # the view was told to repaint


def test_edit_role_gives_the_raw_text_and_display_gives_the_result(model, call, wait_until):
    call("edit", entries=[[0, 0, "5"], [0, 1, "=A1*2"]])
    call("format", rect=[0, 0, 0, 0], fmt="0.00")
    shown(model, 0, 0)
    wait_until(lambda: shown(model, 0, 1) == "10")
    assert shown(model, 0, 0) == "5.00"
    assert model.data(idx(model, 0, 0), EDIT) == "5"
    assert model.data(idx(model, 0, 1), EDIT) == "=A1*2"
    assert model.data(idx(model, 7, 7), EDIT) in ("", None)


def test_alignment_follows_the_value_type_and_errors_are_red(model, call, wait_until):
    call("edit", entries=[[0, 0, "1"], [0, 1, "text"], [0, 2, "TRUE"], [0, 3, "=1/0"]])
    shown(model, 0, 0)
    wait_until(lambda: shown(model, 0, 3) == "#DIV/0!")
    h = lambda c: model.data(idx(model, 0, c), ALIGN) & Qt.AlignmentFlag.AlignHorizontal_Mask
    assert h(0) == Qt.AlignmentFlag.AlignRight
    assert h(1) == Qt.AlignmentFlag.AlignLeft
    assert h(2) == Qt.AlignmentFlag.AlignHCenter
    assert model.data(idx(model, 0, 3), FOREGROUND) is not None
    assert model.data(idx(model, 0, 0), FOREGROUND) is None


def test_cells_in_one_block_cost_one_request_and_far_cells_are_fetchable(
    model, call, wait_until, requests_seen
):
    call("edit", entries=[[900_000, 9_000, "far"]])
    requests_seen.clear()
    for r in range(BLOCK_ROWS):
        for c in range(BLOCK_COLS):
            shown(model, r, c)
    shown(model, 900_000, 9_000)
    wait_until(lambda: shown(model, 900_000, 9_000) == "far")
    views = [a for op, a in requests_seen if op == "view"]
    assert len(views) == 2  # one block near the origin, one far away
    for r in range(BLOCK_ROWS):
        shown(model, r, 0)
    assert len([1 for op, _ in requests_seen if op == "view"]) == 2  # cached


def test_setdata_sends_an_edit_and_refreshes_the_cell_and_its_dependents(model, call, wait_until):
    call("edit", entries=[[0, 1, "=A1+1"]])
    shown(model, 0, 0)
    wait_until(lambda: shown(model, 0, 1) == "1")
    assert model.setData(idx(model, 0, 0), "5", EDIT) is True
    wait_until(lambda: shown(model, 0, 0) == "5" and shown(model, 0, 1) == "6")


def test_rejected_edits_signal_an_error_and_change_nothing(model, call, wait_until):
    call("edit", entries=[[0, 0, "1"], [0, 1, "=A1"]])
    shown(model, 0, 0)
    wait_until(lambda: shown(model, 0, 1) == "1")
    errors = []
    model.error.connect(lambda kind, msg: errors.append(kind))
    model.setData(idx(model, 0, 1), "=1+", EDIT)
    wait_until(lambda: errors == ["syntax"])
    model.setData(idx(model, 0, 0), "=B1", EDIT)
    wait_until(lambda: errors == ["syntax", "circular"])
    assert shown(model, 0, 0) == "1" and shown(model, 0, 1) == "1"


def test_send_structural_edit_reloads_the_shifted_cells(model, call, wait_until):
    call("edit", entries=[[0, 0, "top"], [1, 0, "next"]])
    shown(model, 0, 0)
    wait_until(lambda: shown(model, 1, 0) == "next")
    model.send("insert_rows", at=0, count=1)
    wait_until(
        lambda: (
            shown(model, 0, 0) in ("", None)
            and shown(model, 1, 0) == "top"
            and shown(model, 2, 0) == "next"
        )
    )


def test_undo_through_send_restores_the_display(model, call, wait_until):
    call("edit", entries=[[0, 0, "1"]])
    shown(model, 0, 0)
    wait_until(lambda: shown(model, 0, 0) == "1")
    model.send("edit", entries=[[0, 0, "2"]])
    wait_until(lambda: shown(model, 0, 0) == "2")
    model.send("undo")
    wait_until(lambda: shown(model, 0, 0) == "1")


def test_changed_signal_reports_the_bounds(model, wait_until):
    bounds = []
    model.changed.connect(bounds.append)
    model.send("edit", entries=[[2, 3, "x"], [4, 1, "y"]])
    wait_until(lambda: bounds)
    assert bounds[0] == [2, 1, 4, 3]


def test_old_blocks_are_evicted(engine, call, wait_until):
    small = SheetModel(engine, max_blocks=3)
    for i in range(6):
        call("edit", entries=[[i * BLOCK_ROWS, 0, str(i)]])
    for i in range(6):
        shown(small, i * BLOCK_ROWS, 0)
        wait_until(lambda i=i: shown(small, i * BLOCK_ROWS, 0) == str(i))
    assert small.cached_blocks <= 3


def test_flags_make_cells_editable_and_selectable(model):
    flags = model.flags(idx(model, 3, 3))
    assert flags & Qt.ItemFlag.ItemIsEditable
    assert flags & Qt.ItemFlag.ItemIsSelectable and flags & Qt.ItemFlag.ItemIsEnabled


def test_loading_or_starting_a_document_resets_the_whole_cache(model, call, wait_until, tmp_path):
    call("edit", entries=[[0, 0, "old"], [500, 500, "far"]])
    shown(model, 0, 0)
    shown(model, 500, 500)
    wait_until(lambda: shown(model, 0, 0) == "old" and shown(model, 500, 500) == "far")
    other = tmp_path / "o.sheet"
    other.write_text('{"format":"sheet","version":1,"formats":["General"],"cells":[[0,0,"new"]]}\n')
    resets = []
    model.modelReset.connect(lambda: resets.append(model.cached_blocks))
    model.send("load", path=str(other))
    wait_until(lambda: resets)
    assert resets == [0]  # the old document's blocks were dropped
    wait_until(lambda: shown(model, 0, 0) == "new")
    wait_until(lambda: shown(model, 500, 500) in ("", None))
    resets.clear()
    model.send("new")
    wait_until(lambda: resets)
    wait_until(lambda: shown(model, 0, 0) in ("", None))


def test_a_failed_load_does_not_reset_the_cache(model, call, wait_until, tmp_path):
    call("edit", entries=[[0, 0, "keep"]])
    shown(model, 0, 0)
    wait_until(lambda: shown(model, 0, 0) == "keep")
    bad = tmp_path / "bad.sheet"
    bad.write_text("junk")
    resets, errors = [], []
    model.modelReset.connect(lambda: resets.append(1))
    model.error.connect(lambda kind, msg: errors.append(kind))
    model.send("load", path=str(bad))
    wait_until(lambda: errors == ["file"])
    assert not resets and shown(model, 0, 0) == "keep"

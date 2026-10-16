import json
import subprocess
import sys

import pytest

from sheet.server.protocol import CHANGED_LIMIT, MAX_VIEW_CELLS, Session


@pytest.fixture
def session():
    s = Session(workers=1)
    yield s
    s.close()


def call(session, op, **args):
    return session.handle({"op": op, **args})


def edit(session, r, c, text):
    return call(session, "edit", entries=[[r, c, text]])


def display(session, r1, c1, r2, c2):
    resp = call(session, "view", rect=[r1, c1, r2, c2])
    assert resp["ok"], resp
    return resp["cells"]


# --- basics -----------------------------------------------------------------------------


def test_hello_describes_the_engine(session):
    resp = call(session, "hello")
    assert resp["ok"] is True
    assert resp["max_rows"] == 1_048_576 and resp["max_cols"] == 16_384
    assert resp["version"] == 0
    assert isinstance(resp["gil"], bool)


def test_request_id_is_echoed(session):
    assert session.handle({"op": "hello", "id": 41})["id"] == 41
    assert session.handle({"op": "nonsense", "id": 42})["id"] == 42


@pytest.mark.parametrize("bad", [None, [], "hello", {"no": "op"}, {"op": 5}, {"op": "nonsense"}])
def test_bad_requests_are_errors_not_crashes(session, bad):
    resp = session.handle(bad)
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "invalid"


@pytest.mark.parametrize("args", [{}, {"rect": [0, 0]}, {"rect": "x"}, {"rect": [0, 0, "a", 1]}])
def test_missing_or_malformed_arguments_are_invalid(session, args):
    resp = call(session, "view", **args)
    assert resp["ok"] is False and resp["error"]["kind"] == "invalid"


# --- view / cell ------------------------------------------------------------------------


def test_view_returns_display_text_and_kind_with_none_for_empty(session):
    edit(session, 0, 0, "1234.5")
    call(session, "format", rect=[0, 0, 0, 0], fmt="$#,##0.00")
    edit(session, 0, 1, "hello")
    edit(session, 0, 2, "TRUE")
    edit(session, 0, 3, "=1/0")
    edit(session, 1, 0, "=A1*2")
    # a third element carries the raw text, only when it differs from what is displayed
    assert display(session, 0, 0, 1, 4) == [
        [
            ["$1,234.50", "n", "1234.5"],
            ["hello", "s"],
            ["TRUE", "b"],
            ["#DIV/0!", "e", "=1/0"],
            None,
        ],
        [["2469", "n", "=A1*2"], None, None, None, None],
    ]


def test_view_is_limited_in_size(session):
    n = int(MAX_VIEW_CELLS**0.5) + 1
    resp = call(session, "view", rect=[0, 0, n, n])
    assert resp["ok"] is False and resp["error"]["kind"] == "invalid"
    assert call(session, "view", rect=[2_000_000, 0, 2_000_001, 0])["ok"] is False


def test_cell_returns_raw_text_display_and_details(session):
    edit(session, 0, 0, "5")
    edit(session, 0, 1, "=A1+1")
    edit(session, 0, 2, "=1/0")
    call(session, "format", rect=[0, 1, 0, 1], fmt="0.00")
    a, b, c = (call(session, "cell", at=[0, i]) for i in range(3))
    assert (a["text"], a["display"], a["kind"], a["formula"]) == ("5", "5", "n", False)
    assert (b["text"], b["display"], b["format"], b["formula"]) == ("=A1+1", "6.00", "0.00", True)
    assert c["kind"] == "e" and c["error"] == "#DIV/0!"
    empty = call(session, "cell", at=[9, 9])
    assert empty["text"] == "" and empty["display"] == "" and empty["kind"] == "s"


# --- editing ----------------------------------------------------------------------------


def test_edit_reports_changed_cells_bounds_and_bumps_the_version(session):
    edit(session, 0, 0, "1")
    edit(session, 0, 1, "=A1+1")
    resp = edit(session, 0, 0, "5")
    assert resp["ok"] and resp["version"] == 3
    assert sorted(map(tuple, resp["changed"])) == [(0, 0), (0, 1)]
    assert resp["bounds"] == [0, 0, 0, 1]
    assert resp["can_undo"] is True and resp["can_redo"] is False


def test_a_rejected_edit_changes_nothing_and_reports_why(session):
    edit(session, 0, 0, "1")
    edit(session, 0, 1, "=A1")
    version = call(session, "hello")["version"]
    syntax = edit(session, 1, 0, "=1+")
    assert syntax["ok"] is False and syntax["error"]["kind"] == "syntax"
    cycle = edit(session, 0, 0, "=B1")
    assert cycle["error"]["kind"] == "circular"
    bounds = call(session, "edit", entries=[[2_000_000, 0, "x"]])
    assert bounds["error"]["kind"] == "invalid"
    assert call(session, "hello")["version"] == version
    assert call(session, "cell", at=[0, 0])["text"] == "1"


def test_an_edit_that_changes_nothing_does_not_bump_the_version(session):
    edit(session, 0, 0, "1")
    version = call(session, "hello")["version"]
    resp = edit(session, 0, 0, "1")
    assert resp["ok"] and resp["changed"] == [] and resp["version"] == version


def test_huge_changes_are_summarised_by_their_bounds(session):
    entries = [[r, 0, str(r)] for r in range(CHANGED_LIMIT + 1)]
    resp = call(session, "edit", entries=entries)
    assert resp["ok"] and resp["changed"] is None
    assert resp["bounds"] == [0, 0, CHANGED_LIMIT, 0]


def test_undo_and_redo_report_changes_and_availability(session):
    edit(session, 0, 0, "1")
    undone = call(session, "undo")
    assert (
        undone["changed"] == [[0, 0]] and undone["can_undo"] is False and undone["can_redo"] is True
    )
    assert display(session, 0, 0, 0, 0) == [[None]]
    redone = call(session, "redo")
    assert redone["can_redo"] is False and display(session, 0, 0, 0, 0) == [[["1", "n"]]]
    assert call(session, "undo")["version"] > undone["version"]
    assert call(session, "undo")["changed"] == []  # nothing left


def test_format_is_one_undoable_change(session):
    edit(session, 0, 0, "5")
    resp = call(session, "format", rect=[0, 0, 1, 1], fmt="0.00")
    assert resp["ok"] and len(resp["changed"]) == 4
    assert display(session, 0, 0, 0, 0) == [
        [["5.00", "n", "5"]]
    ]  # raw text "5" differs from "5.00"
    call(session, "undo")
    assert display(session, 0, 0, 0, 0) == [[["5", "n"]]]
    bad = call(session, "format", rect=[0, 0, 0, 0], fmt="QQ")
    assert bad["ok"] is False and bad["error"]["kind"] == "format"


def test_clear_empties_only_populated_cells_in_one_undo_step(session):
    for r in range(3):
        edit(session, r, 0, str(r + 1))
    edit(session, 5, 5, "keep")
    resp = call(session, "clear", rect=[0, 0, 2, 0])
    assert resp["ok"] and len(resp["changed"]) == 3
    assert display(session, 0, 0, 2, 0) == [[None], [None], [None]]
    assert call(session, "cell", at=[5, 5])["text"] == "keep"
    call(session, "undo")
    assert display(session, 0, 0, 2, 0) == [[["1", "n"]], [["2", "n"]], [["3", "n"]]]


def test_clear_keeps_formats(session):
    edit(session, 0, 0, "5")
    call(session, "format", rect=[0, 0, 0, 0], fmt="0.00")
    call(session, "clear", rect=[0, 0, 0, 0])
    assert call(session, "cell", at=[0, 0])["format"] == "0.00"
    assert call(session, "cell", at=[0, 0])["text"] == ""


def test_clear_of_a_huge_rectangle_is_cheap(session):
    edit(session, 7, 7, "x")
    resp = call(session, "clear", rect=[0, 0, 1_048_575, 16_383])
    assert resp["ok"] and resp["changed"] == [[7, 7]]


# --- structure, clipboard, fill -----------------------------------------------------------


def test_structural_edits(session):
    for i, v in enumerate(["1", "2", "3"]):
        edit(session, i, 0, v)
    edit(session, 0, 1, "=SUM(A1:A3)")
    resp = call(session, "insert_rows", at=1, count=1)
    assert resp["ok"] and resp["bounds"][0] == 0
    assert call(session, "cell", at=[0, 1])["text"] == "=SUM(A1:A4)"
    call(session, "delete_rows", at=1, count=1)
    assert call(session, "cell", at=[0, 1])["text"] == "=SUM(A1:A3)"
    call(session, "insert_cols", at=0, count=1)
    assert call(session, "cell", at=[0, 2])["text"] == "=SUM(B1:B3)"
    call(session, "delete_cols", at=0, count=1)
    assert call(session, "insert_rows", at=0, count=0)["error"]["kind"] == "invalid"


def test_copy_returns_clipboard_text_and_paste_shifts_references(session):
    edit(session, 0, 0, "1")
    edit(session, 0, 1, "=A1+1")
    edit(session, 2, 0, "10")
    copied = call(session, "copy", rect=[0, 0, 0, 1])
    assert copied["text"] == "1\t2"
    pasted = call(session, "paste", at=[2, 0])
    assert pasted["ok"]
    assert call(session, "cell", at=[2, 1])["text"] == "=A3+1"
    assert call(session, "cell", at=[2, 1])["display"] == "2"  # A3 was overwritten with 1
    vals = call(session, "paste", at=[4, 0], values_only=True)
    assert vals["ok"] and call(session, "cell", at=[4, 1])["text"] == "2"


def test_copy_text_of_a_huge_selection_is_clamped_to_the_used_range(session):
    edit(session, 0, 0, "1")
    edit(session, 1, 1, "2")
    resp = call(session, "copy", rect=[0, 0, 1_048_575, 16_383])  # select-all
    assert resp["text"] == "1\t\n\t2"
    empty = call(session, "copy", rect=[500, 500, 600, 600])
    assert empty["text"] == ""
    assert call(session, "paste", at=[10, 0])["ok"]  # the snapshot is still usable


def test_paste_without_a_copy_is_invalid(session):
    resp = call(session, "paste", at=[0, 0])
    assert resp["ok"] is False and resp["error"]["kind"] == "invalid"


def test_paste_text_from_outside_enters_tab_separated_values(session):
    resp = call(session, "paste_text", at=[1, 1], text="a\t2\r\n=B2*2\t\r\n")
    assert resp["ok"]
    assert display(session, 1, 1, 2, 2) == [
        [["a", "s"], ["2", "n"]],
        [["#VALUE!", "e", "=B2*2"], None],
    ]
    assert call(session, "cell", at=[1, 1])["text"] == "a"
    assert call(session, "cell", at=[2, 1])["text"] == "=B2*2"
    assert call(session, "undo")["changed"]  # one undo step


def test_fill_down_and_right(session):
    edit(session, 0, 0, "2")
    edit(session, 0, 1, "=A1*2")
    call(session, "fill", rect=[0, 0, 2, 1], direction="down")
    assert call(session, "cell", at=[2, 1])["text"] == "=A3*2"
    edit(session, 5, 5, "=A1*2")
    call(session, "fill", rect=[5, 5, 5, 7], direction="right")
    assert call(session, "cell", at=[5, 7])["text"] == "=C1*2"
    assert call(session, "fill", rect=[0, 0, 1, 1], direction="up")["error"]["kind"] == "invalid"


# --- csv and misc -----------------------------------------------------------------------


def test_csv_round_trip_through_files(session, tmp_path):
    edit(session, 0, 0, "1")
    edit(session, 0, 1, "=A1+1")
    path = str(tmp_path / "out.csv")
    assert call(session, "export_csv", path=path, mode="raw")["ok"]
    assert (tmp_path / "out.csv").read_bytes() == b"1,=A1+1\r\n"
    fresh = Session(workers=1)
    resp = fresh.handle({"op": "import_csv", "path": path, "at": [3, 3]})
    assert resp["ok"] and resp["bounds"] == [3, 3, 3, 4]
    # imported formulas are entered as typed (not shifted): E4 "=A1+1" reads the empty A1
    assert fresh.handle({"op": "cell", "at": [3, 4]})["display"] == "1"
    fresh.close()


def test_csv_errors_are_reported(session, tmp_path):
    missing = call(session, "import_csv", path=str(tmp_path / "nope.csv"))
    assert missing["ok"] is False and missing["error"]["kind"] == "io"
    bad = tmp_path / "bad.csv"
    bad.write_text("1,=1+\n")
    resp = call(session, "import_csv", path=str(bad))
    assert resp["error"]["kind"] == "csv" and "B1" in resp["error"]["message"]


def test_used_range(session):
    assert call(session, "used_range")["range"] is None
    edit(session, 4, 2, "x")
    assert call(session, "used_range")["range"] == [0, 0, 4, 2]


# --- the real server process ----------------------------------------------------------------


def test_server_process_speaks_json_lines_and_keeps_stdout_clean():
    proc = subprocess.Popen(
        [sys.executable, "-m", "sheet.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    requests = [
        {"id": 1, "op": "hello"},
        {"id": 2, "op": "edit", "entries": [[0, 0, "7"], [0, 1, "=A1*6"]]},
        "this is not json",
        {"id": 3, "op": "view", "rect": [0, 0, 0, 1]},
        {"id": 4, "op": "shutdown"},
    ]
    stdin = "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in requests)
    out, _ = proc.communicate(stdin, timeout=60)
    assert proc.returncode == 0
    replies = [json.loads(line) for line in out.splitlines()]  # every stdout line is JSON
    assert [r.get("id") for r in replies] == [1, 2, None, 3, 4]
    assert replies[0]["gil"] is False  # the engine runs free-threaded
    assert replies[2]["ok"] is False and replies[2]["error"]["kind"] == "invalid"
    assert replies[3]["cells"] == [[["7", "n"], ["42", "n", "=A1*6"]]]
    assert replies[4]["ok"] is True


def test_server_exits_cleanly_when_stdin_closes():
    proc = subprocess.Popen(
        [sys.executable, "-m", "sheet.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    proc.stdin.close()
    assert proc.wait(timeout=30) == 0

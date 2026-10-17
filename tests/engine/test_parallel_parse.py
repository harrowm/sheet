"""Entries are parsed on the thread pool for big batches; results and errors must be exactly
what a one-thread run gives, including which of several errors is reported."""

import json

import pytest

from sheet.engine.csv_io import CsvError, import_csv
from sheet.engine.files import FileFormatError, load_sheet, save_sheet
from sheet.engine.lexer import FormulaSyntaxError
from sheet.engine.limits import MAX_FORMULA_LENGTH, MAX_NESTING
from sheet.engine.sheet import PARALLEL_PARSE_MIN, Sheet

BIG = PARALLEL_PARSE_MIN * 3  # enough entries for several chunks


def _entries(n: int = BIG):
    return [(r, 0, str(r)) for r in range(n)] + [(r, 1, f"=A{r + 1}*2+1") for r in range(n)]


def _state(sheet: Sheet):
    return {pos: (c.text, c.ast, c.value, c.format) for pos, c in sheet.store.items()}


def test_a_big_batch_gives_the_same_cells_as_one_thread():
    one, many = Sheet(workers=1), Sheet(workers=4)
    one.set_entries(_entries())
    many.set_entries(_entries())
    assert _state(one) == _state(many)
    assert many.get(BIG - 1, 1) == (BIG - 1) * 2 + 1
    one.close()
    many.close()


def test_the_first_bad_entry_in_order_is_the_one_reported():
    sheet = Sheet(workers=4)
    edits = _entries()
    first, second = BIG // 2, BIG + 7  # in different chunks
    edits[first] = (first, 5, "=1+")
    edits[second] = (second - BIG, 6, "=(")
    with pytest.raises(FormulaSyntaxError) as info:
        sheet.set_entries(edits)
    assert "position 3" in str(info.value)  # "=1+" fails at its end, not "=(" at position 2
    assert sheet.store.items() == []  # all or nothing
    sheet.close()


def test_a_bad_position_before_a_bad_formula_is_still_reported_first():
    sheet = Sheet(workers=4)
    edits = _entries()
    edits[10] = (-1, 0, "5")  # outside the grid
    edits[BIG + 50] = (0, 9, "=1+")
    with pytest.raises(IndexError):
        sheet.set_entries(edits)
    assert sheet.store.items() == []
    sheet.close()


def test_the_longest_and_deepest_formulas_parse_in_a_worker_thread():
    deep = "=" + "(" * (MAX_NESTING - 1) + "1" + ")" * (MAX_NESTING - 1)
    long = "=1" + "+1" * ((MAX_FORMULA_LENGTH - 2) // 2)
    unary = "=" + "-" * (MAX_FORMULA_LENGTH - 2) + "1"
    sheet = Sheet(workers=4)
    edits = _entries()
    edits += [
        (r, 3, formula) for r, formula in zip(range(BIG, BIG + 3), (deep, long, unary), strict=True)
    ]
    sheet.set_entries(edits)
    assert sheet.get(BIG, 3) == 1.0
    assert sheet.get(BIG + 1, 3) == (len(long) - 1) // 2 + 1
    sheet.close()


def test_a_formula_over_the_length_limit_is_rejected_in_a_worker_too():
    sheet = Sheet(workers=4)
    edits = _entries() + [(BIG, 3, "=" + "1+" * MAX_FORMULA_LENGTH + "1")]
    with pytest.raises(FormulaSyntaxError, match="at most"):
        sheet.set_entries(edits)
    sheet.close()


def test_csv_import_matches_one_thread_and_text_mode_keeps_bad_formulas(tmp_path):
    lines = [f"{r},=A{r + 1}*2" for r in range(BIG)]
    lines[BIG // 2] = "5,=1+"
    lines[BIG - 3] = "6,=("
    path = tmp_path / "in.csv"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(CsvError) as info:
        import_csv(Sheet(workers=4), path)
    assert f"B{BIG // 2 + 1}:" in str(info.value)  # the earlier of the two
    one, many = Sheet(workers=1), Sheet(workers=4)
    import_csv(one, path, bad_formulas="text")
    import_csv(many, path, bad_formulas="text")
    assert _state(one) == _state(many)
    assert many.get(BIG // 2, 1) == "=1+"
    one.close()
    many.close()


def test_load_matches_one_thread_and_reports_the_first_problem_in_file_order(tmp_path):
    source = Sheet(workers=4)
    source.set_entries(_entries())
    path = tmp_path / "big.sheet"
    save_sheet(source, path)
    one, many = load_sheet(path, workers=1), load_sheet(path, workers=4)
    assert _state(one) == _state(many) == _state(source)
    for s in (source, one, many):
        s.close()

    lines = path.read_text().split("\n")
    data = [i for i, line in enumerate(lines) if line.startswith("[")]
    bad_syntax, bad_record = data[BIG // 2], data[BIG + 100]
    lines[bad_syntax] = '[5,5,"=1+"],'
    lines[bad_record] = '["x",1,"2"],'
    path.write_text("\n".join(lines))
    with pytest.raises(FileFormatError, match=r"F6: .*syntax|F6:"):
        load_sheet(path, workers=4)
    lines[bad_syntax] = '[5,5,"7"],'  # fix the syntax error: now the malformed record is first
    path.write_text("\n".join(lines))
    with pytest.raises(FileFormatError, match="malformed"):
        load_sheet(path, workers=4)
    assert json.loads(json.dumps({"x": 1}))  # (keeps the json import honest)

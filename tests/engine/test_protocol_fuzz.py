"""Fuzz the request handler: whatever arrives, it answers with a dict and stays consistent."""

import json
import math

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sheet.engine.graph import extract_references
from sheet.engine.sheet import Sheet
from sheet.server.protocol import Session

# Operations that touch the filesystem are covered by their own tests (a fuzzer must not write files).
FILE_OPS = {"save", "load", "import_csv", "export_csv"}
SAFE_OPS = [
    "hello",
    "view",
    "cell",
    "used_range",
    "edit",
    "format",
    "clear",
    "insert_rows",
    "delete_rows",
    "insert_cols",
    "delete_cols",
    "copy",
    "paste",
    "paste_text",
    "undo",
    "redo",
    "new",
    "shutdown",
    "nonsense",
    "",
    "EDIT",
]
ARG_NAMES = [
    "rect",
    "at",
    "entries",
    "text",
    "fmt",
    "direction",
    "count",
    "values_only",
    "mode",
    "id",
    "path",
    "op",
]

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=30),
)
json_values = st.recursive(
    scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=5), st.dictionaries(st.text(max_size=5), inner, max_size=4)
    ),
    max_leaves=12,
)


def valid_response(resp) -> bool:
    return (
        isinstance(resp, dict)
        and isinstance(resp.get("ok"), bool)
        and (
            resp["ok"]
            or (
                isinstance(resp.get("error"), dict)
                and "kind" in resp["error"]
                and "message" in resp["error"]
            )
        )
    )


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from(SAFE_OPS),
            st.dictionaries(st.sampled_from(ARG_NAMES), json_values, max_size=5),
        ),
        max_size=8,
    )
)
def test_garbage_arguments_always_get_a_typed_reply_and_never_break_the_session(requests):
    session = Session(workers=1)
    try:
        for op, args in requests:
            request = (
                {**args, "op": op}
                if "op" not in args or not isinstance(args.get("op"), str)
                else {**args}
            )
            resp = session.handle(request)
            assert valid_response(resp), (request, resp)
            # the response must survive the wire: strict JSON (no NaN/Infinity), ASCII-safe
            json.loads(json.dumps(resp, allow_nan=False))
            assert session.handle({"op": "hello"})["ok"] is True
    finally:
        session.close()


# --- structured sequences: small, mostly valid commands, checked against the engine's invariants ----

COORD = st.integers(0, 6)
TEXT = st.sampled_from(
    [
        "1",
        "2.5",
        "hello",
        "",
        "TRUE",
        "25/12/2024",
        "'12",
        "=A1+1",
        "=SUM(A1:A4)",
        "=B2*2",
        "=C3",
        "=MAX(A1:C1)",
        "=1/0",
        '=A1&"x"',
        "=-A1",
        "=SUM(A1:A3)+B1",
        "=1+",
    ]
)
FORMATS = st.sampled_from(["General", "0.00", "$#,##0.00", "DD/MM/YYYY", "0%", "QQ"])
RECT = st.tuples(COORD, COORD, COORD, COORD).map(list)

COMMANDS = st.one_of(
    st.builds(lambda r, c, t: ("edit", {"entries": [[r, c, t]]}), COORD, COORD, TEXT),
    st.builds(
        lambda es: ("edit", {"entries": es}),
        st.lists(st.tuples(COORD, COORD, TEXT).map(list), max_size=6),
    ),
    st.builds(lambda rect, f: ("format", {"rect": rect, "fmt": f}), RECT, FORMATS),
    st.builds(lambda rect: ("clear", {"rect": rect}), RECT),
    st.builds(
        lambda op, at, n: (op, {"at": at, "count": n}),
        st.sampled_from(["insert_rows", "delete_rows", "insert_cols", "delete_cols"]),
        st.integers(0, 7),
        st.integers(1, 3),
    ),
    st.builds(lambda rect: ("copy", {"rect": rect}), RECT),
    st.builds(
        lambda at, v: ("paste", {"at": at, "values_only": v}),
        st.tuples(COORD, COORD).map(list),
        st.booleans(),
    ),
    st.builds(
        lambda at, t: ("paste_text", {"at": at, "text": t}),
        st.tuples(COORD, COORD).map(list),
        st.sampled_from(["a\tb\n1\t=A1", "x", "1\t2\t3\r\n4", ""]),
    ),
    st.builds(
        lambda rect, d: ("fill", {"rect": rect, "direction": d}),
        RECT,
        st.sampled_from(["down", "right"]),
    ),
    st.just(("undo", {})),
    st.just(("redo", {})),
)


def snapshot(session: Session):
    return {pos: (c.text, c.value, c.format) for pos, c in session.sheet.store.items()}


def check_invariants(session: Session):
    sheet: Sheet = session.sheet
    for pos, cell in sheet.store.items():
        refs = extract_references(cell.ast) if cell.ast is not None else (set(), set())
        assert sheet.graph.dependencies(pos) == refs, pos
        if isinstance(cell.value, float):
            assert math.isfinite(cell.value), (pos, cell.value)
    fresh = Sheet(workers=1)
    fresh.set_entries([(r, c, x.text) for (r, c), x in sheet.store.items() if x.text.strip()])
    for pos, cell in sheet.store.items():
        if cell.text.strip():
            other = fresh.get(*pos)
            assert type(other) is type(cell.value) and other == cell.value, pos


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(COMMANDS, max_size=14))
def test_command_sequences_keep_the_engine_consistent_and_failed_requests_change_nothing(commands):
    session = Session(workers=1)
    try:
        for op, args in commands:
            before, version = snapshot(session), session.version
            resp = session.handle({"op": op, **args})
            assert valid_response(resp), (op, args, resp)
            if not resp["ok"]:
                assert snapshot(session) == before, (
                    op,
                    args,
                    resp,
                )  # a rejected request is a no-op
                assert session.version == version
            else:
                assert resp["version"] >= version
            check_invariants(session)
    finally:
        session.close()

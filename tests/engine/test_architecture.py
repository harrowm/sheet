"""The package layers stay separate: the engine knows nothing of the server or the UI, and the
UI (which runs on a different Python build) shares only one tiny pure module with the engine."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "sheet"

# The only engine module the UI may import (it is pure: column letters and cell names).
UI_MAY_IMPORT = {"sheet.engine.address"}


def imports(path: Path) -> set[str]:
    """Every module a file imports, wherever the import appears (also inside functions)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {m for m in found if m == "sheet" or m.startswith("sheet.")}


def files(layer: str) -> list[Path]:
    return sorted((ROOT / layer).glob("*.py"))


def under(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


@pytest.mark.parametrize("path", files("engine"), ids=lambda p: p.name)
def test_engine_modules_import_only_the_engine(path):
    bad = {m for m in imports(path) if not under(m, "sheet.engine")}
    assert not bad, f"{path.name} imports {sorted(bad)}"


@pytest.mark.parametrize("path", files("server"), ids=lambda p: p.name)
def test_server_modules_never_import_the_ui(path):
    bad = {m for m in imports(path) if under(m, "sheet.ui")}
    assert not bad, f"{path.name} imports {sorted(bad)}"


@pytest.mark.parametrize("path", files("ui"), ids=lambda p: p.name)
def test_ui_modules_import_no_engine_or_server_code_but_the_allowed_one(path):
    bad = {
        m
        for m in imports(path)
        if (under(m, "sheet.engine") or under(m, "sheet.server"))
        and not any(under(m, allowed) for allowed in UI_MAY_IMPORT)
    }
    assert not bad, f"{path.name} imports {sorted(bad)}"


def test_the_checker_itself_notices_a_forbidden_import(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import sheet.ui.window\nfrom sheet.server import main\ndef f():\n    from sheet.engine.sheet import Sheet\n"
    )
    assert imports(sample) == {
        "sheet.ui.window",
        "sheet.server",
        "sheet.server.main",
        "sheet.engine.sheet",
        "sheet.engine.sheet.Sheet",
    }

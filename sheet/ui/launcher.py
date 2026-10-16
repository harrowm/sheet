"""How the UI starts the engine: a separate process on free-threaded Python."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def engine_command() -> list[str]:
    """Command line that starts the engine server.

    ``SHEET_ENGINE_PYTHON`` overrides the interpreter. Otherwise the project's
    free-threaded virtual environment (created by ``scripts/setup.sh``) is used, falling
    back to ``uv run``.
    """
    override = os.environ.get("SHEET_ENGINE_PYTHON")
    if override:
        return [override, "-m", "sheet.server"]
    root = repo_root()
    venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_python.exists():
        return [str(venv_python), "-m", "sheet.server"]
    return ["uv", "run", "--project", str(root), "python", "-m", "sheet.server"]

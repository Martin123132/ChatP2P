"""Small JSON file helpers shared by CLI-facing config loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json_file(path: Path, *, description: str = "JSON file") -> Any:
    """Read JSON from disk, accepting UTF-8 files with or without a BOM."""

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{description} not found: {path}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{description} is not valid JSON: {path} "
            f"(line {exc.lineno}, column {exc.colno}: {exc.msg})"
        ) from exc

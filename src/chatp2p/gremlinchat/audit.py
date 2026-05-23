"""Append-only GremlinChat audit log."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .config import ensure_home
from .redaction import redact_value


def audit_path(home: Path) -> Path:
    return home / "audit.jsonl"


def append_audit_event(home: Path | None, event: dict[str, Any]) -> dict[str, Any]:
    resolved = ensure_home(home)
    record = {
        "audit_id": f"audit_{uuid.uuid4().hex}",
        "created_at": round(time.time(), 3),
        **redact_value(event),
    }
    with audit_path(resolved).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def read_audit_events(home: Path | None, *, limit: int = 50) -> list[dict[str, Any]]:
    resolved = ensure_home(home)
    path = audit_path(resolved)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows[-limit:]


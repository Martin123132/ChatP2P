"""Canonical serialization helpers used before hashing and signing."""

from __future__ import annotations

import json
from typing import Any


def canonical_json(data: Any) -> str:
    """Return a deterministic JSON string for signatures and hashes."""

    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_bytes(data: Any) -> bytes:
    """Return deterministic UTF-8 bytes for signatures and hashes."""

    return canonical_json(data).encode("utf-8")

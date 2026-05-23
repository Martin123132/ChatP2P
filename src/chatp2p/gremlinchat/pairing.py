"""Invite-code creation and validation for GremlinChat rooms."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from chatp2p.canonical import canonical_bytes
from chatp2p.crypto import NodeIdentity

from .security import b64decode, b64encode, generate_pair_secret

INVITE_PREFIX = "GC1:"
DEFAULT_INVITE_TTL_SECONDS = 600


@dataclass(frozen=True)
class Invite:
    relay_url: str
    room_id: str
    relay_token: str
    creator_node_id: str
    creator_public_key: str
    creator_x25519_public_key: str | None
    pair_secret: str
    expires_at: float
    checksum: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "v": 1,
            "relay_url": self.relay_url,
            "room_id": self.room_id,
            "relay_token": self.relay_token,
            "creator_node_id": self.creator_node_id,
            "creator_public_key": self.creator_public_key,
            "creator_x25519_public_key": self.creator_x25519_public_key,
            "pair_secret": self.pair_secret,
            "expires_at": self.expires_at,
            "checksum": self.checksum,
        }


def create_invite_code(
    *,
    creator: NodeIdentity,
    relay_url: str,
    room_id: str | None = None,
    relay_token: str | None = None,
    creator_x25519_public_key: str | None = None,
    ttl_seconds: int = DEFAULT_INVITE_TTL_SECONDS,
) -> str:
    payload = {
        "v": 1,
        "relay_url": relay_url.rstrip("/"),
        "room_id": room_id or f"room_{secrets.token_urlsafe(18)}",
        "relay_token": relay_token or secrets.token_urlsafe(32),
        "creator_node_id": creator.node_id,
        "creator_public_key": creator.public_key,
        "creator_x25519_public_key": creator_x25519_public_key,
        "pair_secret": generate_pair_secret(),
        "expires_at": round(time.time() + ttl_seconds, 3),
    }
    payload["checksum"] = _checksum(payload)
    return INVITE_PREFIX + b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def parse_invite_code(code: str, *, now: float | None = None, require_fresh: bool = True) -> Invite:
    if not code.startswith(INVITE_PREFIX):
        raise ValueError("GremlinChat invite codes must start with GC1:")
    try:
        payload = json.loads(b64decode(code.removeprefix(INVITE_PREFIX)).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("GremlinChat invite code could not be decoded") from exc
    required = {
        "v",
        "relay_url",
        "room_id",
        "relay_token",
        "creator_node_id",
        "creator_public_key",
        "pair_secret",
        "expires_at",
        "checksum",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"GremlinChat invite code is missing fields: {', '.join(missing)}")
    if payload["v"] != 1:
        raise ValueError("Unsupported GremlinChat invite version")
    expected = _checksum({key: value for key, value in payload.items() if key != "checksum"})
    if not secrets.compare_digest(str(payload["checksum"]), expected):
        raise ValueError("GremlinChat invite checksum mismatch")
    timestamp = time.time() if now is None else now
    if require_fresh and float(payload["expires_at"]) < timestamp:
        raise ValueError("GremlinChat invite code has expired")
    return Invite(
        relay_url=str(payload["relay_url"]).rstrip("/"),
        room_id=str(payload["room_id"]),
        relay_token=str(payload["relay_token"]),
        creator_node_id=str(payload["creator_node_id"]),
        creator_public_key=str(payload["creator_public_key"]),
        creator_x25519_public_key=(
            None if payload.get("creator_x25519_public_key") is None else str(payload["creator_x25519_public_key"])
        ),
        pair_secret=str(payload["pair_secret"]),
        expires_at=float(payload["expires_at"]),
        checksum=str(payload["checksum"]),
    )


def _checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()[:16]

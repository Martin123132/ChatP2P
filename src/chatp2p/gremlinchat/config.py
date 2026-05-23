"""GremlinChat local configuration and identity storage."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatp2p.crypto import NodeIdentity

from .security import X25519Identity, protect_secret, unprotect_secret


def default_home() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "GremlinChat"
    return Path.home() / ".gremlinchat"


@dataclass
class ApprovedRepo:
    name: str
    path: str
    allow_pull_ff_only: bool = False
    allow_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "allow_pull_ff_only": self.allow_pull_ff_only,
            "allow_tests": list(self.allow_tests),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovedRepo":
        return cls(
            name=str(data["name"]),
            path=str(data["path"]),
            allow_pull_ff_only=bool(data.get("allow_pull_ff_only", False)),
            allow_tests=[str(item) for item in data.get("allow_tests", [])],
        )


@dataclass
class RunbookPolicy:
    emergency_stop: bool = False
    approved_repos: list[ApprovedRepo] = field(default_factory=list)
    enabled_write_runbooks: list[str] = field(default_factory=list)
    allowlisted_tests: dict[str, list[str]] = field(default_factory=dict)
    managed_workers: dict[str, list[str]] = field(default_factory=dict)
    revoked_node_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "emergency_stop": self.emergency_stop,
            "approved_repos": [repo.to_dict() for repo in self.approved_repos],
            "enabled_write_runbooks": list(self.enabled_write_runbooks),
            "allowlisted_tests": dict(self.allowlisted_tests),
            "managed_workers": dict(self.managed_workers),
            "revoked_node_ids": list(self.revoked_node_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunbookPolicy":
        return cls(
            emergency_stop=bool(data.get("emergency_stop", False)),
            approved_repos=[ApprovedRepo.from_dict(item) for item in data.get("approved_repos", [])],
            enabled_write_runbooks=[str(item) for item in data.get("enabled_write_runbooks", [])],
            allowlisted_tests={
                str(key): [str(part) for part in value]
                for key, value in data.get("allowlisted_tests", {}).items()
            },
            managed_workers={
                str(key): [str(part) for part in value]
                for key, value in data.get("managed_workers", {}).items()
            },
            revoked_node_ids=[str(item) for item in data.get("revoked_node_ids", [])],
        )


def ensure_home(home: Path | None = None) -> Path:
    resolved = default_home() if home is None else Path(home)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def gremlin_identity_path(home: Path) -> Path:
    return home / "identity.json"


def load_or_create_identity(home: Path | None = None) -> NodeIdentity:
    resolved = ensure_home(home)
    path = gremlin_identity_path(resolved)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return NodeIdentity(
            node_id=data["node_id"],
            public_key=data["public_key"],
            private_key=unprotect_secret(data["private_key_protected"]),
        )
    identity = NodeIdentity.generate(prefix="gremlin")
    data = {
        "schema": "gremlinchat.identity.v1",
        "node_id": identity.node_id,
        "public_key": identity.public_key,
        "private_key_protected": protect_secret(identity.private_key or ""),
        "created_at": round(time.time(), 3),
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return identity


def load_or_create_x25519_identity(home: Path | None = None) -> X25519Identity:
    resolved = ensure_home(home)
    path = resolved / "x25519.identity.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return X25519Identity(
            private_key=unprotect_secret(data["private_key_protected"]),
            public_key=data["public_key"],
        )
    identity = X25519Identity.generate()
    path.write_text(
        json.dumps(
            {
                "schema": "gremlinchat.x25519-identity.v1",
                "public_key": identity.public_key,
                "private_key_protected": protect_secret(identity.private_key),
                "created_at": round(time.time(), 3),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return identity


def policy_path(home: Path) -> Path:
    return home / "runbook-policy.json"


def load_policy(home: Path | None = None) -> RunbookPolicy:
    resolved = ensure_home(home)
    path = policy_path(resolved)
    if not path.exists():
        policy = RunbookPolicy()
        save_policy(policy, resolved)
        return policy
    return RunbookPolicy.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_policy(policy: RunbookPolicy, home: Path | None = None) -> None:
    resolved = ensure_home(home)
    policy_path(resolved).write_text(json.dumps(policy.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def rooms_path(home: Path) -> Path:
    return home / "rooms.json"


def load_rooms(home: Path | None = None) -> list[dict[str, Any]]:
    resolved = ensure_home(home)
    path = rooms_path(resolved)
    if not path.exists():
        return []
    rooms = []
    for room in json.loads(path.read_text(encoding="utf-8")).get("rooms", []):
        loaded = dict(room)
        for field_name in ["relay_token", "pair_secret"]:
            protected_name = f"{field_name}_protected"
            if protected_name in loaded and field_name not in loaded:
                loaded[field_name] = unprotect_secret(loaded[protected_name])
        rooms.append(loaded)
    return rooms


def save_room(room: dict[str, Any], home: Path | None = None) -> None:
    resolved = ensure_home(home)
    rooms = [existing for existing in load_rooms(resolved) if existing.get("room_id") != room.get("room_id")]
    rooms.append(dict(room))
    rooms_path(resolved).write_text(
        json.dumps({"rooms": [_room_for_disk(item) for item in rooms]}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _room_for_disk(room: dict[str, Any]) -> dict[str, Any]:
    saved = dict(room)
    for field_name in ["relay_token", "pair_secret"]:
        if field_name in saved:
            saved[f"{field_name}_protected"] = protect_secret(str(saved.pop(field_name)))
    return saved

"""Durable local approval queue for write-capable GremlinChat requests."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from chatp2p.canonical import canonical_bytes

from .config import ensure_home
from .redaction import redact_value


def approvals_path(home: Path) -> Path:
    return home / "approvals.json"


def approval_id_for(
    *,
    room_id: str,
    task_id: str,
    requester_node_id: str,
    runbook: str,
    payload: dict[str, Any],
) -> str:
    digest = hashlib.sha256(
        canonical_bytes(
            {
                "room_id": room_id,
                "task_id": task_id,
                "requester_node_id": requester_node_id,
                "runbook": runbook,
                "payload": payload,
            }
        )
    ).hexdigest()[:20]
    return f"approval_{digest}"


def load_approvals(home: Path | None) -> list[dict[str, Any]]:
    resolved = ensure_home(home)
    path = approvals_path(resolved)
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("approvals", []))


def save_approvals(home: Path | None, approvals: list[dict[str, Any]]) -> None:
    resolved = ensure_home(home)
    approvals_path(resolved).write_text(
        json.dumps({"approvals": approvals}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def create_pending_approval(
    home: Path | None,
    *,
    room_id: str,
    task_id: str,
    requester_node_id: str,
    runbook: str,
    payload: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    approvals = load_approvals(home)
    approval_id = approval_id_for(
        room_id=room_id,
        task_id=task_id,
        requester_node_id=requester_node_id,
        runbook=runbook,
        payload=payload,
    )
    for approval in approvals:
        if approval.get("approval_id") == approval_id:
            return approval
    approval = {
        "approval_id": approval_id,
        "status": "pending",
        "room_id": room_id,
        "task_id": task_id,
        "requester_node_id": requester_node_id,
        "runbook": runbook,
        "payload": redact_value(payload),
        "reason": reason,
        "created_at": round(time.time(), 3),
        "decided_at": None,
    }
    approvals.append(approval)
    save_approvals(home, approvals)
    return approval


def decide_approval(home: Path | None, approval_id: str, *, approved: bool) -> dict[str, Any]:
    approvals = load_approvals(home)
    for approval in approvals:
        if approval.get("approval_id") == approval_id:
            approval["status"] = "approved" if approved else "rejected"
            approval["decided_at"] = round(time.time(), 3)
            save_approvals(home, approvals)
            return approval
    raise KeyError(f"Unknown GremlinChat approval: {approval_id}")


def approval_for_task(home: Path | None, task_id: str) -> dict[str, Any] | None:
    for approval in load_approvals(home):
        if approval.get("task_id") == task_id:
            return approval
    return None


def mark_approval_consumed(home: Path | None, approval_id: str) -> None:
    approvals = load_approvals(home)
    for approval in approvals:
        if approval.get("approval_id") == approval_id:
            approval["status"] = "consumed"
            approval["consumed_at"] = round(time.time(), 3)
            save_approvals(home, approvals)
            return


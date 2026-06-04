"""Credit ledger primitives for ChatP2P."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


CREDIT_LEDGER_ENTRY_SCHEMA = "chatp2p.credit-ledger-entry.v1"


@dataclass(frozen=True)
class CreditLedgerEntry:
    transaction_id: str
    account_id: str
    account_type: str
    delta: int
    balance_after: int
    reason: str
    created_at: float
    job_id: str | None = None
    node_id: str | None = None
    counterparty_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        delta: int,
        balance_after: int,
        reason: str,
        account_type: str = "node",
        transaction_id: str | None = None,
        job_id: str | None = None,
        node_id: str | None = None,
        counterparty_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> "CreditLedgerEntry":
        return cls(
            transaction_id=transaction_id or f"credit_txn_{uuid.uuid4().hex}",
            account_id=account_id,
            account_type=account_type,
            delta=int(delta),
            balance_after=int(balance_after),
            reason=reason,
            created_at=round(created_at if created_at is not None else time.time(), 3),
            job_id=job_id,
            node_id=node_id,
            counterparty_id=counterparty_id,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CreditLedgerEntry":
        values = dict(data)
        values.pop("schema", None)
        values["metadata"] = dict(values.get("metadata") or {})
        values["delta"] = int(values["delta"])
        values["balance_after"] = int(values["balance_after"])
        values["created_at"] = float(values["created_at"])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CREDIT_LEDGER_ENTRY_SCHEMA,
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "account_type": self.account_type,
            "delta": self.delta,
            "balance_after": self.balance_after,
            "reason": self.reason,
            "created_at": self.created_at,
            "job_id": self.job_id,
            "node_id": self.node_id,
            "counterparty_id": self.counterparty_id,
            "metadata": dict(self.metadata),
        }

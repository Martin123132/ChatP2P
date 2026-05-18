"""Signed registration, job packet, and result models."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_bytes, canonical_json
from .crypto import NodeIdentity


def sha256_json(data: Any) -> str:
    return hashlib.sha256(canonical_bytes(data)).hexdigest()


@dataclass(frozen=True)
class NodeRegistration:
    node_id: str
    node_public_key: str
    capabilities: dict[str, Any]
    created_at: float
    node_signature: str

    @classmethod
    def create(
        cls,
        *,
        node: NodeIdentity,
        capabilities: dict[str, Any] | None = None,
    ) -> "NodeRegistration":
        unsigned = {
            "node_id": node.node_id,
            "node_public_key": node.public_key,
            "capabilities": capabilities or {},
            "created_at": round(time.time(), 3),
        }
        signature = node.sign(canonical_bytes(unsigned))
        return cls(**unsigned, node_signature=signature)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeRegistration":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_public_key": self.node_public_key,
            "capabilities": self.capabilities,
            "created_at": self.created_at,
            "node_signature": self.node_signature,
        }

    def unsigned_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("node_signature")
        return data

    def verify_signature(self) -> bool:
        node = NodeIdentity(node_id=self.node_id, public_key=self.node_public_key)
        return node.verify(canonical_bytes(self.unsigned_dict()), self.node_signature)


@dataclass(frozen=True)
class JobPacket:
    job_id: str
    job_type: str
    model_id: str
    payload: dict[str, Any]
    input_hash: str
    resource_requirements: dict[str, Any]
    deadline: float
    expected_output_schema: dict[str, Any]
    verification_strategy: str
    reward: int
    coordinator_id: str
    coordinator_public_key: str
    coordinator_signature: str

    @classmethod
    def create(
        cls,
        *,
        coordinator: NodeIdentity,
        job_type: str,
        model_id: str,
        payload: dict[str, Any],
        resource_requirements: dict[str, Any] | None = None,
        expected_output_schema: dict[str, Any] | None = None,
        verification_strategy: str = "duplicate-or-sample",
        reward: int = 1,
        ttl_seconds: int = 300,
    ) -> "JobPacket":
        unsigned = {
            "job_id": f"job_{uuid.uuid4().hex}",
            "job_type": job_type,
            "model_id": model_id,
            "payload": payload,
            "input_hash": sha256_json(payload),
            "resource_requirements": resource_requirements or {},
            "deadline": round(time.time() + ttl_seconds, 3),
            "expected_output_schema": expected_output_schema or {},
            "verification_strategy": verification_strategy,
            "reward": reward,
            "coordinator_id": coordinator.node_id,
            "coordinator_public_key": coordinator.public_key,
        }
        signature = coordinator.sign(canonical_bytes(unsigned))
        return cls(**unsigned, coordinator_signature=signature)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobPacket":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "model_id": self.model_id,
            "payload": self.payload,
            "input_hash": self.input_hash,
            "resource_requirements": self.resource_requirements,
            "deadline": self.deadline,
            "expected_output_schema": self.expected_output_schema,
            "verification_strategy": self.verification_strategy,
            "reward": self.reward,
            "coordinator_id": self.coordinator_id,
            "coordinator_public_key": self.coordinator_public_key,
            "coordinator_signature": self.coordinator_signature,
        }

    def unsigned_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("coordinator_signature")
        return data

    def verify_signature(self) -> bool:
        coordinator = NodeIdentity(
            node_id=self.coordinator_id,
            public_key=self.coordinator_public_key,
        )
        return coordinator.verify(canonical_bytes(self.unsigned_dict()), self.coordinator_signature)

    def verify_payload_hash(self) -> bool:
        return sha256_json(self.payload) == self.input_hash

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class JobResult:
    job_id: str
    node_id: str
    node_public_key: str
    output: dict[str, Any]
    output_hash: str
    metrics: dict[str, Any]
    runtime_seconds: float
    hardware_attestation: dict[str, Any]
    worker_signature: str
    created_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        *,
        node: NodeIdentity,
        job: JobPacket,
        output: dict[str, Any],
        metrics: dict[str, Any] | None = None,
        runtime_seconds: float = 0.0,
        hardware_attestation: dict[str, Any] | None = None,
    ) -> "JobResult":
        unsigned = {
            "job_id": job.job_id,
            "node_id": node.node_id,
            "node_public_key": node.public_key,
            "output": output,
            "output_hash": sha256_json(output),
            "metrics": metrics or {},
            "runtime_seconds": round(runtime_seconds, 6),
            "hardware_attestation": hardware_attestation or {},
            "created_at": round(time.time(), 3),
        }
        signature = node.sign(canonical_bytes(unsigned))
        return cls(**unsigned, worker_signature=signature)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobResult":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "node_id": self.node_id,
            "node_public_key": self.node_public_key,
            "output": self.output,
            "output_hash": self.output_hash,
            "metrics": self.metrics,
            "runtime_seconds": self.runtime_seconds,
            "hardware_attestation": self.hardware_attestation,
            "created_at": self.created_at,
            "worker_signature": self.worker_signature,
        }

    def unsigned_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("worker_signature")
        return data

    def verify_signature(self) -> bool:
        node = NodeIdentity(node_id=self.node_id, public_key=self.node_public_key)
        return node.verify(canonical_bytes(self.unsigned_dict()), self.worker_signature)

    def verify_output_hash(self) -> bool:
        return sha256_json(self.output) == self.output_hash

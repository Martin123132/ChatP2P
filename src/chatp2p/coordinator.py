"""Coordinator for the first local mesh prototype."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .benchmark import tier_meets_requirement
from .crypto import NodeIdentity
from .packets import (
    JobLeaseAcknowledgement,
    JobLeaseGrant,
    JobLeaseRequest,
    JobPacket,
    JobResult,
    NodeHeartbeat,
    NodeRegistration,
)
from .storage import SQLiteCoordinatorStore

DEFAULT_LEASE_TIMEOUT_SECONDS = 30.0
DEFAULT_NODE_STALE_SECONDS = 60.0
DEFAULT_PACKET_MAX_AGE_SECONDS = 300.0
DEFAULT_PACKET_FUTURE_SKEW_SECONDS = 30.0


@dataclass
class Coordinator:
    identity: NodeIdentity
    store: SQLiteCoordinatorStore | None = None
    lease_timeout_seconds: float = DEFAULT_LEASE_TIMEOUT_SECONDS
    node_stale_seconds: float = DEFAULT_NODE_STALE_SECONDS
    packet_max_age_seconds: float = DEFAULT_PACKET_MAX_AGE_SECONDS
    packet_future_skew_seconds: float = DEFAULT_PACKET_FUTURE_SKEW_SECONDS
    known_nodes: dict[str, NodeIdentity] = field(default_factory=dict)
    node_capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    node_last_seen: dict[str, float] = field(default_factory=dict)
    jobs: dict[str, JobPacket] = field(default_factory=dict)
    leased_jobs: dict[str, set[str]] = field(default_factory=dict)
    lease_started_at: dict[str, dict[str, float]] = field(default_factory=dict)
    lease_expires_at: dict[str, dict[str, float]] = field(default_factory=dict)
    lease_grants: dict[str, dict[str, JobLeaseGrant]] = field(default_factory=dict)
    lease_acknowledged_at: dict[str, dict[str, float]] = field(default_factory=dict)
    lease_acknowledgements: dict[str, dict[str, JobLeaseAcknowledgement]] = field(default_factory=dict)
    expired_leases: list[dict[str, Any]] = field(default_factory=list)
    seen_packet_ids: set[str] = field(default_factory=set)
    results: dict[str, list[JobResult]] = field(default_factory=dict)
    credits: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.store is None:
            return

        stored_nodes, stored_capabilities, stored_last_seen = self.store.load_nodes()
        (
            stored_jobs,
            stored_leases,
            stored_lease_started_at,
            stored_lease_expires_at,
            stored_lease_acknowledged_at,
            stored_lease_grants,
            stored_lease_acknowledgements,
            stored_expired_leases,
        ) = self.store.load_jobs(default_lease_timeout_seconds=self.lease_timeout_seconds)
        stored_results = self.store.load_results()
        stored_credits = self.store.load_credits()
        stored_seen_packet_ids = self.store.load_seen_packet_ids()

        self.known_nodes.update(stored_nodes)
        self.node_capabilities.update(stored_capabilities)
        self.node_last_seen.update(stored_last_seen)
        self.jobs.update(stored_jobs)
        self.leased_jobs.update(stored_leases)
        self.lease_started_at.update(stored_lease_started_at)
        self.lease_expires_at.update(stored_lease_expires_at)
        self.lease_grants.update(stored_lease_grants)
        self.lease_acknowledged_at.update(stored_lease_acknowledged_at)
        self.lease_acknowledgements.update(stored_lease_acknowledgements)
        self.expired_leases.extend(stored_expired_leases)
        self.results.update(stored_results)
        self.credits.update(stored_credits)
        self.seen_packet_ids.update(stored_seen_packet_ids)
        self.reap_expired_leases()

    def register_node(self, node: NodeIdentity, capabilities: dict[str, Any] | None = None) -> None:
        self.known_nodes[node.node_id] = node.public()
        self.node_capabilities[node.node_id] = capabilities or {
            "supported_job_types": ["eval.math.v1", "eval.deterministic.v1", "inference.echo.v1"]
        }
        self.node_last_seen[node.node_id] = round(time.time(), 3)
        self.credits.setdefault(node.node_id, 0)

    def register_signed_node(self, registration: NodeRegistration) -> bool:
        if not registration.verify_signature():
            return False
        seen_at = round(time.time(), 3)
        self.known_nodes[registration.node_id] = NodeIdentity(
            node_id=registration.node_id,
            public_key=registration.node_public_key,
        )
        self.node_capabilities[registration.node_id] = registration.capabilities
        self.node_last_seen[registration.node_id] = seen_at
        self.credits.setdefault(registration.node_id, 0)
        if self.store is not None:
            self.store.save_node(registration, last_seen_at=seen_at)
        return True

    def record_signed_heartbeat(self, heartbeat: NodeHeartbeat) -> bool:
        if not self.consume_signed_node_packet(heartbeat):
            return False
        return self.touch_node(heartbeat.node_id)

    def verify_signed_node_packet(self, packet: Any) -> bool:
        known_node = self.known_nodes.get(packet.node_id)
        if known_node is None:
            return False
        if known_node.public_key != packet.node_public_key:
            return False
        return bool(packet.verify_signature())

    def consume_signed_node_packet(self, packet: Any, now: float | None = None) -> bool:
        timestamp = round(now if now is not None else time.time(), 3)
        if self.signed_node_packet_rejection_reason(packet, now=timestamp) is not None:
            return False

        packet_id = self._packet_replay_id(packet)
        self.seen_packet_ids.add(packet_id)
        if self.store is not None:
            self.store.save_seen_packet(
                packet_id=packet_id,
                packet_type=packet.packet_type,
                node_id=packet.node_id,
                created_at=packet.created_at,
                seen_at=timestamp,
            )
        return True

    def signed_node_packet_rejection_reason(self, packet: Any, now: float | None = None) -> str | None:
        timestamp = round(now if now is not None else time.time(), 3)
        packet_id = self._packet_replay_id(packet)
        if not packet_id:
            return "missing packet id"
        if packet_id in self.seen_packet_ids:
            return "replayed packet"
        if not self.verify_signed_node_packet(packet):
            return "invalid signature"
        if not self._packet_is_fresh(packet, now=timestamp):
            return "stale packet"
        return None

    def create_math_eval_job(self) -> JobPacket:
        return self.create_job(
            job_type="eval.math.v1",
            model_id="deterministic-math-smoke-test",
            payload={"expression": "2 + 2", "expected": 4},
            resource_requirements={"cpu": "tiny", "network": "none-after-download"},
            expected_output_schema={
                "required": ["passed", "answer", "expected", "confidence"],
            },
            verification_strategy="duplicate-on-random-sample",
            reward=1,
        )

    def create_deterministic_eval_jobs(self) -> list[JobPacket]:
        job_specs = [
            {
                "task": "arithmetic",
                "operation": "add",
                "operands": [19, 23],
                "expected": 42,
            },
            {
                "task": "arithmetic",
                "operation": "multiply",
                "operands": [12, 12],
                "expected": 144,
            },
            {
                "task": "number_theory",
                "check": "is_prime",
                "value": 97,
                "expected": True,
            },
            {
                "task": "text",
                "operation": "normalize_whitespace",
                "value": "open     compute\nmesh",
                "expected": "open compute mesh",
            },
        ]

        jobs = [
            self.create_job(
                job_type="eval.deterministic.v1",
                model_id=f"deterministic-{spec['task']}",
                payload=spec,
                resource_requirements={"cpu": "tiny", "network": "none-after-download"},
                expected_output_schema={
                    "required": ["passed", "answer", "expected", "confidence"],
                },
                verification_strategy="duplicate-on-random-sample",
                reward=1,
            )
            for spec in job_specs
        ]
        return jobs

    def create_echo_inference_job(self, prompt: str) -> JobPacket:
        return self.create_job(
            job_type="inference.echo.v1",
            model_id="echo-smoke-test",
            payload={"prompt": prompt},
            resource_requirements={"cpu": "tiny"},
            expected_output_schema={"required": ["answer", "confidence"]},
            verification_strategy="signature-and-schema-check",
            reward=1,
        )

    def create_ollama_inference_job(
        self,
        *,
        model: str,
        prompt: str,
        temperature: float | None = None,
        reward: int = 1,
        ttl_seconds: int = 300,
    ) -> JobPacket:
        payload: dict[str, Any] = {"model": model, "prompt": prompt}
        if temperature is not None:
            payload["temperature"] = temperature
        return self.create_job(
            job_type="inference.ollama.v1",
            model_id=model,
            payload=payload,
            reward=reward,
            ttl_seconds=ttl_seconds,
        )

    def create_job(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        model_id: str | None = None,
        resource_requirements: dict[str, Any] | None = None,
        expected_output_schema: dict[str, Any] | None = None,
        verification_strategy: str | None = None,
        reward: int = 1,
        ttl_seconds: int = 300,
    ) -> JobPacket:
        if reward < 1:
            raise ValueError("reward must be at least 1")

        normalized_payload = self._validate_payload(job_type, payload)
        defaults = self._job_defaults(job_type, normalized_payload)

        effective_resource_requirements = (
            dict(defaults["resource_requirements"])
            if resource_requirements is None
            else {**defaults["resource_requirements"], **resource_requirements}
        )
        if job_type == "inference.ollama.v1":
            effective_resource_requirements["ollama_model"] = normalized_payload["model"]

        job = JobPacket.create(
            coordinator=self.identity,
            job_type=job_type,
            model_id=model_id or defaults["model_id"],
            payload=normalized_payload,
            resource_requirements=effective_resource_requirements,
            expected_output_schema=expected_output_schema or defaults["expected_output_schema"],
            verification_strategy=verification_strategy or defaults["verification_strategy"],
            reward=reward,
            ttl_seconds=ttl_seconds,
        )
        self.jobs[job.job_id] = job
        if self.store is not None:
            self.store.save_job(job)
        return job

    def _job_defaults(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if job_type == "eval.math.v1":
            return {
                "model_id": "deterministic-math-smoke-test",
                "resource_requirements": {"cpu": "tiny", "network": "none-after-download"},
                "expected_output_schema": {"required": ["passed", "answer", "expected", "confidence"]},
                "verification_strategy": "duplicate-on-random-sample",
            }
        if job_type == "eval.deterministic.v1":
            return {
                "model_id": f"deterministic-{payload['task']}",
                "resource_requirements": {"cpu": "tiny", "network": "none-after-download"},
                "expected_output_schema": {"required": ["passed", "answer", "expected", "confidence"]},
                "verification_strategy": "duplicate-on-random-sample",
            }
        if job_type == "inference.echo.v1":
            return {
                "model_id": "echo-smoke-test",
                "resource_requirements": {"cpu": "tiny"},
                "expected_output_schema": {"required": ["answer", "confidence"]},
                "verification_strategy": "signature-and-schema-check",
            }
        if job_type == "inference.ollama.v1":
            return {
                "model_id": payload["model"],
                "resource_requirements": {
                    "runtime": "ollama",
                    "ollama_model": payload["model"],
                    "min_capability_tier": "standard",
                    "network": "local-ollama",
                },
                "expected_output_schema": {"required": ["answer", "model", "confidence"]},
                "verification_strategy": "signature-and-schema-check",
            }
        raise ValueError(f"unsupported job_type: {job_type}")

    def _validate_payload(self, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        if job_type == "eval.math.v1":
            return self._validate_math_payload(payload)
        if job_type == "eval.deterministic.v1":
            return self._validate_deterministic_payload(payload)
        if job_type == "inference.echo.v1":
            return self._validate_echo_payload(payload)
        if job_type == "inference.ollama.v1":
            return self._validate_ollama_payload(payload)
        raise ValueError(f"unsupported job_type: {job_type}")

    def _validate_math_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        expression = payload.get("expression")
        if expression != "2 + 2":
            raise ValueError("eval.math.v1 currently supports only expression '2 + 2'")
        if "expected" not in payload:
            raise ValueError("eval.math.v1 requires expected")
        return {"expression": expression, "expected": payload["expected"]}

    def _validate_echo_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("inference.echo.v1 requires a non-empty prompt string")
        return {"prompt": prompt}

    def _validate_ollama_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("inference.ollama.v1 requires a non-empty model string")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("inference.ollama.v1 requires a non-empty prompt string")
        normalized: dict[str, Any] = {"model": model.strip(), "prompt": prompt}
        if "temperature" in payload:
            temperature = payload["temperature"]
            if isinstance(temperature, bool) or not isinstance(temperature, int | float):
                raise ValueError("inference.ollama.v1 temperature must be a number")
            if temperature < 0 or temperature > 2:
                raise ValueError("inference.ollama.v1 temperature must be between 0 and 2")
            normalized["temperature"] = temperature
        return normalized

    def _validate_deterministic_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = payload.get("task")
        if task == "arithmetic":
            return self._validate_arithmetic_payload(payload)
        if task == "number_theory":
            return self._validate_number_theory_payload(payload)
        if task == "text":
            return self._validate_text_payload(payload)
        raise ValueError("eval.deterministic.v1 task must be arithmetic, number_theory, or text")

    def _validate_arithmetic_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation")
        if operation not in {"add", "subtract", "multiply", "divide"}:
            raise ValueError("arithmetic operation must be add, subtract, multiply, or divide")
        operands = payload.get("operands")
        if not isinstance(operands, list) or len(operands) != 2:
            raise ValueError("arithmetic operands must be a two-item list")
        if not all(isinstance(operand, int | float) and not isinstance(operand, bool) for operand in operands):
            raise ValueError("arithmetic operands must be numbers")
        if operation == "divide" and operands[1] == 0:
            raise ValueError("division by zero is not allowed")
        if "expected" not in payload:
            raise ValueError("arithmetic payload requires expected")
        return {
            "task": "arithmetic",
            "operation": operation,
            "operands": operands,
            "expected": payload["expected"],
        }

    def _validate_number_theory_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("check") != "is_prime":
            raise ValueError("number_theory check must be is_prime")
        value = payload.get("value")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("number_theory value must be an integer")
        expected = payload.get("expected")
        if not isinstance(expected, bool):
            raise ValueError("number_theory expected must be a boolean")
        return {
            "task": "number_theory",
            "check": "is_prime",
            "value": value,
            "expected": expected,
        }

    def _validate_text_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("operation") != "normalize_whitespace":
            raise ValueError("text operation must be normalize_whitespace")
        value = payload.get("value")
        expected = payload.get("expected")
        if not isinstance(value, str):
            raise ValueError("text value must be a string")
        if not isinstance(expected, str):
            raise ValueError("text expected must be a string")
        return {
            "task": "text",
            "operation": "normalize_whitespace",
            "value": value,
            "expected": expected,
        }

    def lease_next_job(self, node_id: str, request_id: str | None = None) -> JobPacket | None:
        now = time.time()
        self.reap_expired_leases(now=now)
        if node_id not in self.known_nodes:
            return None
        self.touch_node(node_id, seen_at=now)

        for job_id in self._lease_candidates_for_node(node_id):
            job = self.jobs[job_id]
            leased_to = self.leased_jobs.setdefault(job_id, set())
            leased_at = round(now, 3)
            expires_at = round(now + self.lease_timeout_seconds, 3)
            leased_to.add(node_id)
            self.lease_started_at.setdefault(job_id, {})[node_id] = leased_at
            self.lease_expires_at.setdefault(job_id, {})[node_id] = expires_at
            grant = None
            if request_id is not None:
                node = self.known_nodes[node_id]
                grant = JobLeaseGrant.create(
                    coordinator=self.identity,
                    request_id=request_id,
                    job_id=job_id,
                    node_id=node_id,
                    node_public_key=node.public_key,
                    leased_at=leased_at,
                    expires_at=expires_at,
                )
                self.lease_grants.setdefault(job_id, {})[node_id] = grant
            if self.store is not None:
                self.store.save_lease(job_id, node_id, leased_at=leased_at, expires_at=expires_at, grant=grant)
            return job
        return None

    def lease_next_signed_job(self, request: JobLeaseRequest) -> tuple[JobPacket, dict[str, Any]] | None:
        if not self.consume_signed_node_packet(request):
            return None
        job = self.lease_next_job(request.node_id, request_id=request.request_id)
        if job is None:
            return None
        return job, self.lease_metadata(job.job_id, request.node_id)

    def lease_metadata(self, job_id: str, node_id: str) -> dict[str, Any]:
        grant = self.lease_grants.get(job_id, {}).get(node_id)
        return {
            "job_id": job_id,
            "node_id": node_id,
            "leased_at": self.lease_started_at.get(job_id, {}).get(node_id),
            "expires_at": self.lease_expires_at.get(job_id, {}).get(node_id),
            "lease_id": grant.lease_id if grant is not None else None,
            "request_id": grant.request_id if grant is not None else None,
            "grant": grant.to_dict() if grant is not None else None,
            "grant_hash": grant.grant_hash() if grant is not None else None,
            "acknowledged": node_id in self.lease_acknowledged_at.get(job_id, {}),
            "acknowledged_at": self.lease_acknowledged_at.get(job_id, {}).get(node_id),
        }

    def acknowledge_lease(self, acknowledgement: JobLeaseAcknowledgement) -> bool:
        now = time.time()
        self.reap_expired_leases(now=now)
        if not self.consume_signed_node_packet(acknowledgement, now=now):
            return False
        if acknowledgement.job_id not in self.jobs:
            return False
        if acknowledgement.node_id not in self.leased_jobs.get(acknowledgement.job_id, set()):
            return False
        if self._lease_is_expired(acknowledgement.job_id, acknowledgement.node_id, now):
            return False

        grant = self.lease_grants.get(acknowledgement.job_id, {}).get(acknowledgement.node_id)
        if grant is None:
            return False
        if not self._grant_matches_coordinator(grant):
            return False
        if acknowledgement.lease_id != grant.lease_id:
            return False
        if acknowledgement.grant_hash != grant.grant_hash():
            return False

        self.lease_acknowledged_at.setdefault(acknowledgement.job_id, {})[
            acknowledgement.node_id
        ] = acknowledgement.created_at
        self.lease_acknowledgements.setdefault(acknowledgement.job_id, {})[
            acknowledgement.node_id
        ] = acknowledgement
        self.touch_node(acknowledgement.node_id, seen_at=now)
        if self.store is not None:
            self.store.save_lease_acknowledgement(acknowledgement)
        return True

    def submit_result(self, result: JobResult) -> bool:
        now = time.time()
        self.reap_expired_leases(now=now)
        if result.job_id not in self.jobs:
            return False
        if result.node_id not in self.known_nodes:
            return False
        self.touch_node(result.node_id, seen_at=now)
        if self._job_is_terminal(result.job_id):
            return False
        leased_to = self.leased_jobs.get(result.job_id, set())
        if result.node_id not in leased_to:
            return False
        if any(existing.node_id == result.node_id for existing in self.results.get(result.job_id, [])):
            return False
        known_node = self.known_nodes[result.node_id]
        if known_node.public_key != result.node_public_key:
            return False
        if not result.verify_signature():
            return False
        if not result.verify_output_hash():
            return False

        self.results.setdefault(result.job_id, []).append(result)
        self.credits[result.node_id] = self.credits.get(result.node_id, 0) + self.jobs[result.job_id].reward
        if self.store is not None:
            self.store.save_result(result)
            self.store.set_credit(result.node_id, self.credits[result.node_id])
        return True

    def touch_node(self, node_id: str, seen_at: float | None = None) -> bool:
        if node_id not in self.known_nodes:
            return False
        timestamp = round(seen_at if seen_at is not None else time.time(), 3)
        self.node_last_seen[node_id] = timestamp
        if self.store is not None:
            self.store.touch_node(node_id, timestamp)
        return True

    def reap_expired_leases(self, now: float | None = None) -> list[dict[str, Any]]:
        timestamp = round(now if now is not None else time.time(), 3)
        expired: list[dict[str, Any]] = []
        for job_id, node_ids in list(self.leased_jobs.items()):
            result_node_ids = {result.node_id for result in self.results.get(job_id, [])}
            for node_id in list(node_ids):
                if node_id in result_node_ids:
                    continue
                expires_at = self.lease_expires_at.get(job_id, {}).get(node_id)
                if expires_at is None or expires_at > timestamp:
                    continue

                node_ids.remove(node_id)
                leased_at = self.lease_started_at.get(job_id, {}).pop(node_id, None)
                self.lease_expires_at.get(job_id, {}).pop(node_id, None)
                acknowledged_at = self.lease_acknowledged_at.get(job_id, {}).pop(node_id, None)
                grant = self.lease_grants.get(job_id, {}).pop(node_id, None)
                self.lease_acknowledgements.get(job_id, {}).pop(node_id, None)
                lease = {
                    "job_id": job_id,
                    "node_id": node_id,
                    "leased_at": leased_at,
                    "expires_at": expires_at,
                    "expired_at": timestamp,
                    "acknowledged_at": acknowledged_at,
                    "lease_id": grant.lease_id if grant is not None else None,
                    "request_id": grant.request_id if grant is not None else None,
                    "grant_hash": grant.grant_hash() if grant is not None else None,
                }
                self.expired_leases.append(lease)
                expired.append(lease)
                if self.store is not None:
                    self.store.mark_lease_expired(job_id, node_id, expired_at=timestamp)
            if not node_ids:
                self.leased_jobs.pop(job_id, None)
        return expired

    def status(self) -> dict:
        self.reap_expired_leases()
        verification_summaries = [self.verification_summary(job_id) for job_id in self.jobs]
        verified_jobs = sum(1 for summary in verification_summaries if summary["status"] == "verified")
        disputed_jobs = sum(1 for summary in verification_summaries if summary["status"] == "disputed")
        completed_jobs = verified_jobs + disputed_jobs
        queued_jobs = sum(1 for summary in verification_summaries if summary["status"] == "queued")
        pending_jobs = sum(1 for summary in verification_summaries if summary["status"] == "pending")
        active_leases = sum(len(self._active_leases(job_id)) for job_id in self.leased_jobs)
        total_leases = sum(len(nodes) for nodes in self.leased_jobs.values())
        now = time.time()
        liveness = [self._node_liveness_status(node_id, now=now) for node_id in self.known_nodes]
        return {
            "coordinator_id": self.identity.node_id,
            "known_nodes": len(self.known_nodes),
            "live_nodes": liveness.count("live"),
            "stale_nodes": liveness.count("stale"),
            "offline_nodes": liveness.count("offline"),
            "jobs": len(self.jobs),
            "queued_jobs": queued_jobs,
            "pending_jobs": pending_jobs,
            "leased_jobs": active_leases,
            "total_leases": total_leases,
            "expired_leases": len(self.expired_leases),
            "lease_timeout_seconds": self.lease_timeout_seconds,
            "node_stale_seconds": self.node_stale_seconds,
            "completed_jobs": completed_jobs,
            "verified_jobs": verified_jobs,
            "disputed_jobs": disputed_jobs,
            "credits": dict(self.credits),
            "leasing_policy": self.leasing_policy(),
        }

    def node_summaries(self) -> list[dict[str, Any]]:
        self.reap_expired_leases()
        summaries = []
        reputation = self.reputation_summaries()
        now = time.time()
        for node_id, identity in self.known_nodes.items():
            capabilities = self.node_capabilities.get(node_id, {})
            last_seen_at = self.node_last_seen.get(node_id)
            summaries.append(
                {
                    "node_id": node_id,
                    "public_key": identity.public_key,
                    "credits": self.credits.get(node_id, 0),
                    "reputation": reputation.get(node_id, self._empty_reputation(node_id)),
                    "last_seen_at": last_seen_at,
                    "last_seen_seconds_ago": (
                        round(max(0.0, now - last_seen_at), 3) if last_seen_at is not None else None
                    ),
                    "liveness_status": self._node_liveness_status(node_id, now=now),
                    "active_leases": self._active_lease_count_for_node(node_id, now=now),
                    "expired_leases": self._expired_lease_count_for_node(node_id),
                    "capability_tier": capabilities.get("capability_tier", "light"),
                    "supported_job_types": capabilities.get("supported_job_types", []),
                    "hardware": capabilities.get("hardware", {}),
                    "benchmark": capabilities.get("benchmark", {}),
                    "gpu": capabilities.get("gpu", {}),
                    "model_runtimes": capabilities.get("model_runtimes", {}),
                }
            )
        return sorted(summaries, key=lambda item: item["node_id"])

    def job_summaries(self) -> list[dict[str, Any]]:
        self.reap_expired_leases()
        summaries = []
        now = time.time()
        for job_id, job in self.jobs.items():
            result_count = len(self.results.get(job_id, []))
            leased_to = sorted(self.leased_jobs.get(job_id, set()))
            verification = self.verification_summary(job_id)
            leases = self._lease_summaries_for_job(job_id, now=now)
            summaries.append(
                {
                    "job_id": job_id,
                    "job_type": job.job_type,
                    "model_id": job.model_id,
                    "status": verification["status"],
                    "leased_to": leased_to,
                    "active_leases": sorted(self._active_leases(job_id)),
                    "lease_count": len(leases),
                    "acknowledged_lease_count": sum(1 for lease in leases if lease["acknowledged"]),
                    "expired_lease_count": sum(1 for lease in leases if lease["status"] == "expired"),
                    "leases": leases,
                    "reward": job.reward,
                    "deadline": job.deadline,
                    "result_count": result_count,
                    "required_results": verification["required_results"],
                    "max_results": verification["max_results"],
                    "winning_output_hash": verification["winning_output_hash"],
                    "output_hash_counts": verification["output_hash_counts"],
                    "verification_strategy": job.verification_strategy,
                    "payload": job.payload,
                }
            )
        return sorted(summaries, key=lambda item: item["job_id"])

    def result_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for job_id, results in self.results.items():
            for result in results:
                job = self.jobs.get(job_id)
                summaries.append(
                    {
                        "job_id": job_id,
                        "job_type": job.job_type if job else None,
                        "node_id": result.node_id,
                        "output": result.output,
                        "output_hash": result.output_hash,
                        "runtime_seconds": result.runtime_seconds,
                        "created_at": result.created_at,
                    }
                )
        return sorted(summaries, key=lambda item: (item["created_at"], item["job_id"]))

    def reputation_summaries(self) -> dict[str, dict[str, Any]]:
        self.reap_expired_leases()
        reputation = {
            node_id: self._empty_reputation(node_id)
            for node_id in self.known_nodes
        }

        for job_id, results in self.results.items():
            verification = self.verification_summary(job_id)
            status = verification["status"]
            if status not in {"verified", "disputed"}:
                continue

            winning_output_hash = verification["winning_output_hash"]
            for result in results:
                entry = reputation.setdefault(result.node_id, self._empty_reputation(result.node_id))
                entry["terminal_results"] += 1
                if status == "verified" and result.output_hash == winning_output_hash:
                    entry["verified_matches"] += 1
                elif status == "verified":
                    entry["mismatches"] += 1
                else:
                    entry["disputed_results"] += 1

        for lease in self.expired_leases:
            entry = reputation.setdefault(lease["node_id"], self._empty_reputation(lease["node_id"]))
            entry["timeouts"] += 1

        for entry in reputation.values():
            timeout_penalty = entry["timeouts"] * 0.25
            score = entry["verified_matches"] - entry["mismatches"] - entry["disputed_results"] - timeout_penalty
            terminal_results = entry["terminal_results"]
            entry["timeout_penalty"] = round(timeout_penalty, 3)
            entry["score"] = round(score, 3)
            entry["reliability"] = round(entry["verified_matches"] / terminal_results, 3) if terminal_results else None
            entry["status"] = self._reputation_status(entry)

        return dict(sorted(reputation.items(), key=lambda item: item[0]))

    def snapshot(self) -> dict[str, Any]:
        self.reap_expired_leases()
        return {
            "status": self.status(),
            "nodes": self.node_summaries(),
            "jobs": self.job_summaries(),
            "results": self.result_summaries(),
            "reputation": list(self.reputation_summaries().values()),
            "leasing_policy": self.leasing_policy(),
        }

    def leasing_policy(self) -> dict[str, Any]:
        return {
            "trusted_statuses": ["trusted", "ok"],
            "trusted_order": ["tie_breaker", "pending_verification", "queued"],
            "new_order": ["queued", "pending_verification", "tie_breaker"],
            "watch_order": ["queued", "tie_breaker", "pending_verification"],
            "flagged_order": ["tie_breaker"],
            "flagged_rule": "Flagged workers receive only conflicting pending jobs that need a tie-breaker.",
        }

    def _node_supports_job(self, node_id: str, job: JobPacket) -> bool:
        capabilities = self.node_capabilities.get(node_id, {})
        supported_job_types = capabilities.get("supported_job_types")
        if not supported_job_types:
            return False
        if job.job_type not in supported_job_types:
            return False
        if job.job_type == "inference.ollama.v1":
            required_model = job.resource_requirements.get("ollama_model") or job.payload.get("model")
            if required_model not in capabilities.get("ollama_models", []):
                return False
        required_tier = job.resource_requirements.get("min_capability_tier")
        return tier_meets_requirement(capabilities.get("capability_tier"), required_tier)

    def _lease_candidates_for_node(self, node_id: str) -> list[str]:
        reputation = self.reputation_summaries().get(node_id, self._empty_reputation(node_id))
        candidates = []
        for job_id, job in self.jobs.items():
            priority = self._lease_priority(node_id, reputation, job_id, job)
            if priority is None:
                continue
            candidates.append((priority, job_id))
        return [job_id for _, job_id in sorted(candidates)]

    def _lease_priority(
        self,
        node_id: str,
        reputation: dict[str, Any],
        job_id: str,
        job: JobPacket,
    ) -> tuple[int, str] | None:
        if self._job_is_terminal(job_id):
            return None
        if not self._node_supports_job(node_id, job):
            return None
        if any(result.node_id == node_id for result in self.results.get(job_id, [])):
            return None
        if self._node_timed_out_job(job_id, node_id):
            return None

        leased_to = self.leased_jobs.get(job_id, set())
        if node_id in leased_to:
            return None
        if len(leased_to) >= self._max_results_for_job(job):
            return None

        verification = self.verification_summary(job_id)
        kind = self._verification_need_kind(job_id)
        active_lease_count = len(self._active_leases(job_id))
        result_count = len(self.results.get(job_id, []))
        required_results = self._required_results_for_job(job)
        max_results = self._max_results_for_job(job)
        if kind in {"queued", "leased", "pending_verification"}:
            needed_results = max(0, required_results - result_count)
            if active_lease_count >= needed_results:
                return None
        if kind == "tie_breaker":
            needed_results = max(0, max_results - result_count)
            if active_lease_count >= needed_results:
                return None

        status = reputation["status"]

        if status == "flagged":
            if kind == "tie_breaker":
                return (0, job_id)
            return None

        if status in {"trusted", "ok"}:
            order = {"tie_breaker": 0, "pending_verification": 1, "queued": 2, "leased": 3}
        elif status == "watch":
            order = {"queued": 0, "tie_breaker": 1, "pending_verification": 2, "leased": 3}
        else:
            order = {"queued": 0, "pending_verification": 1, "tie_breaker": 2, "leased": 3}

        priority = order.get(kind)
        if priority is None:
            return None
        return (priority, job_id)

    def _verification_need_kind(self, job_id: str) -> str:
        verification = self.verification_summary(job_id)
        if verification["status"] == "queued":
            return "queued"
        if verification["status"] == "leased":
            return "leased"
        if verification["status"] == "pending":
            if len(verification["output_hash_counts"]) > 1:
                return "tie_breaker"
            return "pending_verification"
        return verification["status"]

    def verification_summary(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        results = self.results.get(job_id, [])
        output_hash_counts: dict[str, int] = {}
        for result in results:
            output_hash_counts[result.output_hash] = output_hash_counts.get(result.output_hash, 0) + 1

        required_results = self._required_results_for_job(job)
        max_results = self._max_results_for_job(job)
        winning_output_hash = None
        for output_hash, count in output_hash_counts.items():
            if count >= required_results:
                winning_output_hash = output_hash
                break

        if winning_output_hash is not None:
            status = "verified"
        elif len(results) >= max_results:
            status = "disputed"
        elif results:
            status = "pending"
        elif self._active_leases(job_id):
            status = "leased"
        else:
            status = "queued"

        return {
            "status": status,
            "required_results": required_results,
            "max_results": max_results,
            "result_count": len(results),
            "winning_output_hash": winning_output_hash,
            "output_hash_counts": output_hash_counts,
        }

    def _job_is_terminal(self, job_id: str) -> bool:
        return self.verification_summary(job_id)["status"] in {"verified", "disputed"}

    def _active_leases(self, job_id: str, now: float | None = None) -> set[str]:
        timestamp = now if now is not None else time.time()
        result_node_ids = {result.node_id for result in self.results.get(job_id, [])}
        return {
            node_id
            for node_id in self.leased_jobs.get(job_id, set())
            if node_id not in result_node_ids and not self._lease_is_expired(job_id, node_id, timestamp)
        }

    def _lease_is_expired(self, job_id: str, node_id: str, now: float) -> bool:
        expires_at = self.lease_expires_at.get(job_id, {}).get(node_id)
        return expires_at is not None and expires_at <= now

    def _lease_summaries_for_job(self, job_id: str, now: float | None = None) -> list[dict[str, Any]]:
        timestamp = now if now is not None else time.time()
        result_node_ids = {result.node_id for result in self.results.get(job_id, [])}
        summaries: list[dict[str, Any]] = []
        for node_id in sorted(self.leased_jobs.get(job_id, set())):
            expires_at = self.lease_expires_at.get(job_id, {}).get(node_id)
            grant = self.lease_grants.get(job_id, {}).get(node_id)
            status = "completed" if node_id in result_node_ids else "active"
            summaries.append(
                {
                    "job_id": job_id,
                    "node_id": node_id,
                    "status": status,
                    "leased_at": self.lease_started_at.get(job_id, {}).get(node_id),
                    "expires_at": expires_at,
                    "lease_id": grant.lease_id if grant is not None else None,
                    "request_id": grant.request_id if grant is not None else None,
                    "grant_hash": grant.grant_hash() if grant is not None else None,
                    "acknowledged": node_id in self.lease_acknowledged_at.get(job_id, {}),
                    "acknowledged_at": self.lease_acknowledged_at.get(job_id, {}).get(node_id),
                    "expires_in_seconds": (
                        round(max(0.0, expires_at - timestamp), 3)
                        if status == "active" and expires_at is not None
                        else None
                    ),
                }
            )
        summaries.extend(
            {
                "job_id": lease["job_id"],
                "node_id": lease["node_id"],
                "status": "expired",
                "leased_at": lease["leased_at"],
                "expires_at": lease["expires_at"],
                "lease_id": lease.get("lease_id"),
                "request_id": lease.get("request_id"),
                "grant_hash": lease.get("grant_hash"),
                "acknowledged": lease.get("acknowledged_at") is not None,
                "acknowledged_at": lease.get("acknowledged_at"),
                "expires_in_seconds": None,
                "expired_at": lease["expired_at"],
            }
            for lease in self.expired_leases
            if lease["job_id"] == job_id
        )
        return summaries

    def _packet_replay_id(self, packet: Any) -> str | None:
        if isinstance(packet, NodeHeartbeat):
            return packet.heartbeat_id
        if isinstance(packet, JobLeaseRequest):
            return packet.request_id
        if isinstance(packet, JobLeaseAcknowledgement):
            return packet.acknowledgement_id
        return None

    def _packet_is_fresh(self, packet: Any, now: float | None = None) -> bool:
        timestamp = now if now is not None else time.time()
        created_at = packet.created_at
        if created_at > timestamp + self.packet_future_skew_seconds:
            return False
        return timestamp - created_at <= self.packet_max_age_seconds

    def _grant_matches_coordinator(self, grant: JobLeaseGrant) -> bool:
        if grant.coordinator_id != self.identity.node_id:
            return False
        if grant.coordinator_public_key != self.identity.public_key:
            return False
        return grant.verify_signature()

    def _node_liveness_status(self, node_id: str, now: float | None = None) -> str:
        last_seen_at = self.node_last_seen.get(node_id)
        if last_seen_at is None:
            return "offline"
        elapsed = (now if now is not None else time.time()) - last_seen_at
        if elapsed <= self.node_stale_seconds:
            return "live"
        if elapsed <= self.node_stale_seconds * 3:
            return "stale"
        return "offline"

    def _active_lease_count_for_node(self, node_id: str, now: float | None = None) -> int:
        timestamp = now if now is not None else time.time()
        return sum(1 for job_id in self.jobs if node_id in self._active_leases(job_id, now=timestamp))

    def _expired_lease_count_for_node(self, node_id: str) -> int:
        return sum(1 for lease in self.expired_leases if lease["node_id"] == node_id)

    def _node_timed_out_job(self, job_id: str, node_id: str) -> bool:
        return any(
            lease["job_id"] == job_id and lease["node_id"] == node_id
            for lease in self.expired_leases
        )

    def _required_results_for_job(self, job: JobPacket) -> int:
        strategy = job.verification_strategy
        if strategy == "signature-and-schema-check":
            return 1
        if strategy.startswith("quorum-"):
            try:
                return max(1, int(strategy.removeprefix("quorum-")))
            except ValueError:
                return 2
        if "duplicate" in strategy:
            return 2
        return 1

    def _max_results_for_job(self, job: JobPacket) -> int:
        required_results = self._required_results_for_job(job)
        if required_results <= 1:
            return 1
        return (required_results * 2) - 1

    def _empty_reputation(self, node_id: str) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "score": 0,
            "status": "new",
            "terminal_results": 0,
            "verified_matches": 0,
            "mismatches": 0,
            "disputed_results": 0,
            "timeouts": 0,
            "timeout_penalty": 0,
            "reliability": None,
        }

    def _reputation_status(self, entry: dict[str, Any]) -> str:
        if entry["terminal_results"] == 0 and entry["timeouts"] == 0:
            return "new"
        if entry["mismatches"] or entry["disputed_results"]:
            if entry["score"] < 0:
                return "flagged"
            return "watch"
        if entry["timeouts"]:
            if entry["timeouts"] >= 4 and entry["score"] <= -1:
                return "flagged"
            return "watch"
        if entry["score"] < 0:
            return "flagged"
        if entry["score"] >= 3:
            return "trusted"
        return "ok"

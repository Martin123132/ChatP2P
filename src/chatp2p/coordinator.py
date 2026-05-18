"""Coordinator for the first local mesh prototype."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .crypto import NodeIdentity
from .packets import JobPacket, JobResult, NodeRegistration
from .storage import SQLiteCoordinatorStore


@dataclass
class Coordinator:
    identity: NodeIdentity
    store: SQLiteCoordinatorStore | None = None
    known_nodes: dict[str, NodeIdentity] = field(default_factory=dict)
    node_capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, JobPacket] = field(default_factory=dict)
    leased_jobs: dict[str, set[str]] = field(default_factory=dict)
    results: dict[str, list[JobResult]] = field(default_factory=dict)
    credits: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.store is None:
            return

        stored_nodes, stored_capabilities = self.store.load_nodes()
        stored_jobs, stored_leases = self.store.load_jobs()
        stored_results = self.store.load_results()
        stored_credits = self.store.load_credits()

        self.known_nodes.update(stored_nodes)
        self.node_capabilities.update(stored_capabilities)
        self.jobs.update(stored_jobs)
        self.leased_jobs.update(stored_leases)
        self.results.update(stored_results)
        self.credits.update(stored_credits)

    def register_node(self, node: NodeIdentity, capabilities: dict[str, Any] | None = None) -> None:
        self.known_nodes[node.node_id] = node.public()
        self.node_capabilities[node.node_id] = capabilities or {
            "supported_job_types": ["eval.math.v1", "eval.deterministic.v1", "inference.echo.v1"]
        }
        self.credits.setdefault(node.node_id, 0)

    def register_signed_node(self, registration: NodeRegistration) -> bool:
        if not registration.verify_signature():
            return False
        self.known_nodes[registration.node_id] = NodeIdentity(
            node_id=registration.node_id,
            public_key=registration.node_public_key,
        )
        self.node_capabilities[registration.node_id] = registration.capabilities
        self.credits.setdefault(registration.node_id, 0)
        if self.store is not None:
            self.store.save_node(registration)
        return True

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

        job = JobPacket.create(
            coordinator=self.identity,
            job_type=job_type,
            model_id=model_id or defaults["model_id"],
            payload=normalized_payload,
            resource_requirements=resource_requirements or defaults["resource_requirements"],
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

    def lease_next_job(self, node_id: str) -> JobPacket | None:
        if node_id not in self.known_nodes:
            return None

        for job_id in self._lease_candidates_for_node(node_id):
            job = self.jobs[job_id]
            leased_to = self.leased_jobs.setdefault(job_id, set())
            leased_to.add(node_id)
            if self.store is not None:
                self.store.save_lease(job_id, node_id)
            return job
        return None

    def submit_result(self, result: JobResult) -> bool:
        if result.job_id not in self.jobs:
            return False
        if result.node_id not in self.known_nodes:
            return False
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

    def status(self) -> dict:
        verification_summaries = [self.verification_summary(job_id) for job_id in self.jobs]
        verified_jobs = sum(1 for summary in verification_summaries if summary["status"] == "verified")
        disputed_jobs = sum(1 for summary in verification_summaries if summary["status"] == "disputed")
        completed_jobs = verified_jobs + disputed_jobs
        queued_jobs = sum(1 for summary in verification_summaries if summary["status"] == "queued")
        pending_jobs = sum(1 for summary in verification_summaries if summary["status"] == "pending")
        active_leases = sum(len(self._active_leases(job_id)) for job_id in self.leased_jobs)
        total_leases = sum(len(nodes) for nodes in self.leased_jobs.values())
        return {
            "coordinator_id": self.identity.node_id,
            "known_nodes": len(self.known_nodes),
            "jobs": len(self.jobs),
            "queued_jobs": queued_jobs,
            "pending_jobs": pending_jobs,
            "leased_jobs": active_leases,
            "total_leases": total_leases,
            "completed_jobs": completed_jobs,
            "verified_jobs": verified_jobs,
            "disputed_jobs": disputed_jobs,
            "credits": dict(self.credits),
            "leasing_policy": self.leasing_policy(),
        }

    def node_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        reputation = self.reputation_summaries()
        for node_id, identity in self.known_nodes.items():
            capabilities = self.node_capabilities.get(node_id, {})
            summaries.append(
                {
                    "node_id": node_id,
                    "public_key": identity.public_key,
                    "credits": self.credits.get(node_id, 0),
                    "reputation": reputation.get(node_id, self._empty_reputation(node_id)),
                    "supported_job_types": capabilities.get("supported_job_types", []),
                    "hardware": capabilities.get("hardware", {}),
                }
            )
        return sorted(summaries, key=lambda item: item["node_id"])

    def job_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for job_id, job in self.jobs.items():
            result_count = len(self.results.get(job_id, []))
            leased_to = sorted(self.leased_jobs.get(job_id, set()))
            verification = self.verification_summary(job_id)
            summaries.append(
                {
                    "job_id": job_id,
                    "job_type": job.job_type,
                    "model_id": job.model_id,
                    "status": verification["status"],
                    "leased_to": leased_to,
                    "active_leases": sorted(self._active_leases(job_id)),
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

        for entry in reputation.values():
            score = entry["verified_matches"] - entry["mismatches"] - entry["disputed_results"]
            terminal_results = entry["terminal_results"]
            entry["score"] = score
            entry["reliability"] = round(entry["verified_matches"] / terminal_results, 3) if terminal_results else None
            entry["status"] = self._reputation_status(entry)

        return dict(sorted(reputation.items(), key=lambda item: item[0]))

    def snapshot(self) -> dict[str, Any]:
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
        return job.job_type in supported_job_types

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

        leased_to = self.leased_jobs.get(job_id, set())
        if node_id in leased_to:
            return None
        if len(leased_to) >= self._max_results_for_job(job):
            return None

        verification = self.verification_summary(job_id)
        if verification["status"] == "leased" and len(self._active_leases(job_id)) >= self._required_results_for_job(job):
            return None

        kind = self._verification_need_kind(job_id)
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

    def _active_leases(self, job_id: str) -> set[str]:
        result_node_ids = {result.node_id for result in self.results.get(job_id, [])}
        return set(self.leased_jobs.get(job_id, set())) - result_node_ids

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
            "reliability": None,
        }

    def _reputation_status(self, entry: dict[str, Any]) -> str:
        if entry["terminal_results"] == 0:
            return "new"
        if entry["score"] < 0:
            return "flagged"
        if entry["mismatches"] or entry["disputed_results"]:
            return "watch"
        if entry["score"] >= 3:
            return "trusted"
        return "ok"

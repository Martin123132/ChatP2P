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
    leased_jobs: dict[str, str] = field(default_factory=dict)
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
        job = JobPacket.create(
            coordinator=self.identity,
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
        self.jobs[job.job_id] = job
        if self.store is not None:
            self.store.save_job(job)
        return job

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
            JobPacket.create(
                coordinator=self.identity,
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
        for job in jobs:
            self.jobs[job.job_id] = job
            if self.store is not None:
                self.store.save_job(job)
        return jobs

    def create_echo_inference_job(self, prompt: str) -> JobPacket:
        job = JobPacket.create(
            coordinator=self.identity,
            job_type="inference.echo.v1",
            model_id="echo-smoke-test",
            payload={"prompt": prompt},
            resource_requirements={"cpu": "tiny"},
            expected_output_schema={"required": ["answer", "confidence"]},
            verification_strategy="signature-and-schema-check",
            reward=1,
        )
        self.jobs[job.job_id] = job
        if self.store is not None:
            self.store.save_job(job)
        return job

    def lease_next_job(self, node_id: str) -> JobPacket | None:
        if node_id not in self.known_nodes:
            return None

        for job_id, job in self.jobs.items():
            if self.results.get(job_id):
                continue
            if not self._node_supports_job(node_id, job):
                continue
            leased_to = self.leased_jobs.get(job_id)
            if leased_to is None or leased_to == node_id:
                self.leased_jobs[job_id] = node_id
                if self.store is not None:
                    self.store.save_lease(job_id, node_id)
                return job
        return None

    def submit_result(self, result: JobResult) -> bool:
        if result.job_id not in self.jobs:
            return False
        if result.node_id not in self.known_nodes:
            return False
        leased_to = self.leased_jobs.get(result.job_id)
        if leased_to is not None and leased_to != result.node_id:
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
        completed_jobs = sum(1 for job_id in self.jobs if self.results.get(job_id))
        queued_jobs = sum(1 for job_id in self.jobs if not self.results.get(job_id) and job_id not in self.leased_jobs)
        active_leases = sum(1 for job_id in self.leased_jobs if not self.results.get(job_id))
        return {
            "coordinator_id": self.identity.node_id,
            "known_nodes": len(self.known_nodes),
            "jobs": len(self.jobs),
            "queued_jobs": queued_jobs,
            "leased_jobs": active_leases,
            "total_leases": len(self.leased_jobs),
            "completed_jobs": completed_jobs,
            "credits": dict(self.credits),
        }

    def node_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for node_id, identity in self.known_nodes.items():
            capabilities = self.node_capabilities.get(node_id, {})
            summaries.append(
                {
                    "node_id": node_id,
                    "public_key": identity.public_key,
                    "credits": self.credits.get(node_id, 0),
                    "supported_job_types": capabilities.get("supported_job_types", []),
                    "hardware": capabilities.get("hardware", {}),
                }
            )
        return sorted(summaries, key=lambda item: item["node_id"])

    def job_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for job_id, job in self.jobs.items():
            result_count = len(self.results.get(job_id, []))
            leased_to = self.leased_jobs.get(job_id)
            if result_count:
                status = "completed"
            elif leased_to:
                status = "leased"
            else:
                status = "queued"
            summaries.append(
                {
                    "job_id": job_id,
                    "job_type": job.job_type,
                    "model_id": job.model_id,
                    "status": status,
                    "leased_to": leased_to,
                    "reward": job.reward,
                    "deadline": job.deadline,
                    "result_count": result_count,
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

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status(),
            "nodes": self.node_summaries(),
            "jobs": self.job_summaries(),
            "results": self.result_summaries(),
        }

    def _node_supports_job(self, node_id: str, job: JobPacket) -> bool:
        capabilities = self.node_capabilities.get(node_id, {})
        supported_job_types = capabilities.get("supported_job_types")
        if not supported_job_types:
            return False
        return job.job_type in supported_job_types

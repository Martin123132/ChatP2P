"""ISP-edge / broadband-bundle simulation helpers."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .benchmark import CAPABILITY_PROFILE_NAME
from .coordinator import Coordinator
from .crypto import NodeIdentity
from .packets import JobLeaseAcknowledgement, JobLeaseGrant, JobLeaseRequest, NodeRegistration
from .worker import WorkerNode

PROVIDER_CONFIG_SCHEMA = "chatp2p.provider-config.v1"
PROVIDER_JOIN_REPORT_SCHEMA = "chatp2p.provider-join-report.v1"
PROVIDER_EDGE_PROOF_REPORT_SCHEMA = "chatp2p.provider-edge-proof-report.v1"

DEFAULT_ALLOWED_JOB_TYPES = ["eval.deterministic.v1", "inference.echo.v1"]
DEFAULT_ROUTING_POLICY = ["prefer_local", "prefer_provider_edge", "prefer_trusted_peer", "fallback_placeholder"]
PROVIDER_NODE_ROLES = [
    "subscriber_gateway",
    "subscriber_device",
    "provider_edge_worker",
    "contributor_worker",
    "verifier",
    "coordinator",
]
ROUTE_NAMES = ["local", "provider_edge", "peer", "fallback_placeholder"]


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    provider_name: str
    region: str
    allowed_job_types: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_JOB_TYPES))
    default_routing_policy: list[str] = field(default_factory=lambda: list(DEFAULT_ROUTING_POLICY))
    subscriber_credit_policy: dict[str, Any] = field(default_factory=dict)
    edge_worker_policy: dict[str, Any] = field(default_factory=dict)
    privacy_mode_defaults: dict[str, Any] = field(default_factory=dict)
    subscribers: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema: str = PROVIDER_CONFIG_SCHEMA

    @classmethod
    def create(
        cls,
        *,
        provider_name: str,
        region: str,
        provider_id: str | None = None,
        allowed_job_types: list[str] | None = None,
        default_routing_policy: list[str] | None = None,
    ) -> "ProviderConfig":
        clean_name = provider_name.strip()
        clean_region = region.strip()
        if not clean_name:
            raise ValueError("provider_name must be non-empty")
        if not clean_region:
            raise ValueError("region must be non-empty")
        return cls(
            provider_id=provider_id or f"provider_{uuid.uuid4().hex[:12]}",
            provider_name=clean_name,
            region=clean_region,
            allowed_job_types=list(allowed_job_types or DEFAULT_ALLOWED_JOB_TYPES),
            default_routing_policy=list(default_routing_policy or DEFAULT_ROUTING_POLICY),
            subscriber_credit_policy={
                "starting_credits": 100,
                "job_cost": 1,
                "subscriber_result_reward": 1,
                "peer_result_reward": 1,
            },
            edge_worker_policy={
                "internal_result_reward": 1,
                "simulated_edge_route_share": 0.34,
            },
            privacy_mode_defaults={
                "allow_peer_fallback": True,
                "payload_locality": "proof-only",
                "notes": "Simulation only; no real ISP deployment or billing.",
            },
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderConfig":
        if data.get("schema") != PROVIDER_CONFIG_SCHEMA:
            raise ValueError(f"unsupported provider config schema: {data.get('schema')!r}")
        config = cls(
            schema=data["schema"],
            provider_id=str(data["provider_id"]),
            provider_name=str(data["provider_name"]),
            region=str(data["region"]),
            allowed_job_types=list(data.get("allowed_job_types", DEFAULT_ALLOWED_JOB_TYPES)),
            default_routing_policy=list(data.get("default_routing_policy", DEFAULT_ROUTING_POLICY)),
            subscriber_credit_policy=dict(data.get("subscriber_credit_policy", {})),
            edge_worker_policy=dict(data.get("edge_worker_policy", {})),
            privacy_mode_defaults=dict(data.get("privacy_mode_defaults", {})),
            subscribers=dict(data.get("subscribers", {})),
        )
        _validate_provider_config(config)
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "region": self.region,
            "allowed_job_types": list(self.allowed_job_types),
            "default_routing_policy": list(self.default_routing_policy),
            "subscriber_credit_policy": dict(self.subscriber_credit_policy),
            "edge_worker_policy": dict(self.edge_worker_policy),
            "privacy_mode_defaults": dict(self.privacy_mode_defaults),
            "subscribers": dict(sorted(self.subscribers.items())),
        }

    def with_subscriber(self, subscriber_id: str, plan: str) -> "ProviderConfig":
        clean_id = subscriber_id.strip()
        clean_plan = plan.strip()
        if not clean_id:
            raise ValueError("subscriber_id must be non-empty")
        if not clean_plan:
            raise ValueError("plan must be non-empty")
        if clean_id in self.subscribers:
            raise ValueError(f"subscriber already exists: {clean_id}")
        subscribers = dict(self.subscribers)
        subscribers[clean_id] = {
            "subscriber_id": clean_id,
            "plan": clean_plan,
            "created_at": _iso_now(),
            "starting_credits": int(self.subscriber_credit_policy.get("starting_credits", 100)),
        }
        return ProviderConfig(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            region=self.region,
            allowed_job_types=list(self.allowed_job_types),
            default_routing_policy=list(self.default_routing_policy),
            subscriber_credit_policy=dict(self.subscriber_credit_policy),
            edge_worker_policy=dict(self.edge_worker_policy),
            privacy_mode_defaults=dict(self.privacy_mode_defaults),
            subscribers=subscribers,
            schema=self.schema,
        )


@dataclass(frozen=True)
class ProviderEdgeProofConfig:
    provider_config_path: Path
    subscribers: int = 3
    edge_workers: int = 1
    jobs: int = 25
    report_path: Path = Path(".mesh/proof/provider-edge-report.json")
    peer_workers: int = 1
    verifier_workers: int = 1
    timeout_seconds: float = 60.0


def bootstrap_provider_config(
    *,
    config_path: Path,
    provider_name: str,
    region: str,
    provider_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if config_path.exists() and not force:
        raise ValueError(f"provider config already exists: {config_path}")
    config = ProviderConfig.create(provider_name=provider_name, region=region, provider_id=provider_id)
    write_provider_config(config_path, config)
    return {
        "ok": True,
        "schema": PROVIDER_CONFIG_SCHEMA,
        "status": "created",
        "config": str(config_path.expanduser().resolve()),
        "provider": _provider_public_summary(config),
    }


def add_provider_subscriber(*, config_path: Path, subscriber_id: str, plan: str) -> dict[str, Any]:
    config = load_provider_config(config_path)
    updated = config.with_subscriber(subscriber_id=subscriber_id, plan=plan)
    write_provider_config(config_path, updated)
    return {
        "ok": True,
        "schema": PROVIDER_CONFIG_SCHEMA,
        "status": "subscriber_created",
        "config": str(config_path.expanduser().resolve()),
        "provider": _provider_public_summary(updated),
        "subscriber": updated.subscribers[subscriber_id],
    }


def join_provider_node(
    *,
    provider_config_path: Path,
    subscriber_id: str,
    home: Path,
    node_role: str = "subscriber_gateway",
) -> dict[str, Any]:
    config = load_provider_config(provider_config_path)
    if node_role not in PROVIDER_NODE_ROLES:
        raise ValueError(f"unsupported provider node role: {node_role}")
    if subscriber_id not in config.subscribers and node_role.startswith("subscriber_"):
        raise ValueError(f"unknown subscriber_id: {subscriber_id}")

    identity = _load_or_create_identity(home, "worker")
    subscriber = config.subscribers.get(subscriber_id, {})
    capabilities = provider_capability_profile(
        config=config,
        node_role=node_role,
        subscriber_id=subscriber_id if subscriber_id else None,
        subscriber_plan=subscriber.get("plan"),
    )
    profile_path = home / CAPABILITY_PROFILE_NAME
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(capabilities, indent=2, sort_keys=True), encoding="utf-8")

    return {
        "ok": True,
        "schema": PROVIDER_JOIN_REPORT_SCHEMA,
        "status": "joined_provider_profile",
        "home": str(home.expanduser().resolve()),
        "identity_path": str((home / "worker.identity.json").expanduser().resolve()),
        "capability_profile": str(profile_path.expanduser().resolve()),
        "provider": _provider_public_summary(config),
        "subscriber": subscriber or None,
        "node": {
            "node_id": identity.node_id,
            "node_role": node_role,
            "supported_job_types": capabilities["supported_job_types"],
            "provider_id": config.provider_id,
            "subscriber_id": subscriber_id if subscriber_id else None,
        },
    }


def run_provider_edge_proof(config: ProviderEdgeProofConfig) -> dict[str, Any]:
    _validate_provider_edge_proof_config(config)
    provider = load_provider_config(config.provider_config_path)
    subscribers = _proof_subscribers(provider, config.subscribers)
    started_at = time.time()

    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(
        identity=coordinator_identity,
        lease_timeout_seconds=max(10.0, config.timeout_seconds),
        node_stale_seconds=max(60.0, config.timeout_seconds),
    )

    local_workers = [
        _make_worker(
            provider=provider,
            role="subscriber_gateway",
            index=index,
            subscriber_id=subscriber["subscriber_id"],
            subscriber_plan=subscriber.get("plan"),
        )
        for index, subscriber in enumerate(subscribers)
    ]
    edge_workers = [
        _make_worker(provider=provider, role="provider_edge_worker", index=index)
        for index in range(config.edge_workers)
    ]
    peer_workers = [
        _make_worker(provider=provider, role="contributor_worker", index=index)
        for index in range(config.peer_workers)
    ]
    verifier_workers = [
        _make_worker(provider=provider, role="verifier", index=index)
        for index in range(config.verifier_workers)
    ]

    for worker in [*local_workers, *edge_workers, *peer_workers, *verifier_workers]:
        registration = NodeRegistration.create(node=worker.identity, capabilities=worker.capabilities())
        if not coordinator.register_signed_node(registration):
            raise RuntimeError(f"provider proof registration failed for {worker.identity.node_id}")

    route_counts = {route: 0 for route in ROUTE_NAMES}
    route_latencies: dict[str, list[float]] = {route: [] for route in ROUTE_NAMES}
    route_events: list[dict[str, Any]] = []
    failure_reasons: list[str] = []
    subscriber_ledger = _initial_subscriber_ledger(provider, subscribers)
    provider_internal_credits = 0
    peer_credits = 0
    verifier_credits = 0
    route_budgets = _route_budgets(config, local_workers=local_workers, edge_workers=edge_workers)

    deadline = started_at + config.timeout_seconds
    for index in range(config.jobs):
        if time.time() > deadline:
            failure_reasons.append("provider proof timed out before creating every job")
            break

        subscriber = subscribers[index % len(subscribers)]
        subscriber_id = subscriber["subscriber_id"]
        selection = select_provider_route(
            provider=provider,
            subscriber_id=subscriber_id,
            local_workers=local_workers,
            edge_workers=edge_workers,
            peer_workers=peer_workers,
            route_counts=route_counts,
            route_budgets=route_budgets,
        )
        route = selection["route"]
        route_counts[route] += 1
        _spend_subscriber_credit(provider, subscriber_ledger, subscriber_id)

        event: dict[str, Any] = {
            "job_index": index,
            "subscriber_id": subscriber_id,
            "route": route,
            "route_reason": selection["reason"],
            "worker_node_id": selection["worker"].identity.node_id if selection.get("worker") else None,
            "status": "fallback_placeholder" if route == "fallback_placeholder" else "created",
        }

        if route == "fallback_placeholder":
            event["status"] = "fallback_placeholder"
            route_events.append(event)
            failure_reasons.append(f"job {index} reached fallback placeholder")
            continue

        route_worker = selection["worker"]
        verifier = verifier_workers[index % len(verifier_workers)] if verifier_workers else None
        if verifier is None:
            failure_reasons.append("provider proof requires at least one verifier")
            route_events.append(event)
            continue

        job = coordinator.create_job(
            job_type="eval.deterministic.v1",
            payload=_provider_deterministic_payload(index),
            resource_requirements={
                "provider_mode": True,
                "provider_id": provider.provider_id,
                "subscriber_id": subscriber_id,
                "provider_route": route,
                "provider_routing_policy": provider.default_routing_policy,
            },
            ttl_seconds=max(300, int(config.timeout_seconds + 60)),
        )

        job_started = time.perf_counter()
        first = _run_signed_worker_once(coordinator, route_worker)
        second = _run_signed_worker_once(coordinator, verifier)
        latency = round(time.perf_counter() - job_started, 6)
        route_latencies[route].append(latency)
        verification = coordinator.verification_summary(job.job_id)

        event.update(
            {
                "job_id": job.job_id,
                "status": verification["status"],
                "accepted_results": int(first["accepted"]) + int(second["accepted"]),
                "verifier_node_id": verifier.identity.node_id,
                "latency_seconds": latency,
            }
        )
        route_events.append(event)

        if verification["status"] != "verified":
            failure_reasons.append(f"job {job.job_id} ended as {verification['status']}")
            continue

        if route == "local":
            subscriber_ledger[subscriber_id]["earned"] += int(
                provider.subscriber_credit_policy.get("subscriber_result_reward", 1)
            )
        elif route == "provider_edge":
            provider_internal_credits += int(provider.edge_worker_policy.get("internal_result_reward", 1))
        elif route == "peer":
            peer_credits += int(provider.subscriber_credit_policy.get("peer_result_reward", 1))
        verifier_credits += 1

    finished_at = time.time()
    snapshot = coordinator.snapshot()
    status = snapshot["status"]
    provider_stats = provider_snapshot_summary(snapshot)
    for ledger in subscriber_ledger.values():
        ledger["balance"] = ledger["starting_credits"] - ledger["spent"] + ledger["earned"]

    jobs_created = len(coordinator.jobs)
    jobs_verified = status["verified_jobs"]
    if jobs_created != config.jobs:
        failure_reasons.append(f"expected {config.jobs} jobs but created {jobs_created}")
    if jobs_verified != jobs_created:
        failure_reasons.append(f"expected all created jobs to verify, got {jobs_verified}/{jobs_created}")
    if status["disputed_jobs"]:
        failure_reasons.append(f"disputed jobs: {status['disputed_jobs']}")
    if status["expired_jobs"]:
        failure_reasons.append(f"expired jobs: {status['expired_jobs']}")
    if route_counts["fallback_placeholder"]:
        failure_reasons.append(f"fallback placeholder routes: {route_counts['fallback_placeholder']}")

    report = {
        "ok": not failure_reasons,
        "status": "pass" if not failure_reasons else "fail",
        "schema": PROVIDER_EDGE_PROOF_REPORT_SCHEMA,
        "generated_at": _iso_now(),
        "duration_seconds": round(finished_at - started_at, 3),
        "provider_id": provider.provider_id,
        "provider": _provider_public_summary(provider),
        "parameters": {
            "subscribers": config.subscribers,
            "edge_workers": config.edge_workers,
            "peer_workers": config.peer_workers,
            "verifier_workers": config.verifier_workers,
            "jobs": config.jobs,
            "timeout_seconds": config.timeout_seconds,
        },
        "subscribers_created": len(subscribers),
        "subscriber_nodes_live": provider_stats["subscriber_nodes"]["live"],
        "edge_workers_live": provider_stats["provider_edge_workers"]["live"],
        "jobs_created": jobs_created,
        "jobs_verified": jobs_verified,
        "jobs_disputed": status["disputed_jobs"],
        "jobs_expired": status["expired_jobs"],
        "route_counts": route_counts,
        "credit_summary": {
            "subscribers": subscriber_ledger,
            "provider_internal_credits": provider_internal_credits,
            "peer_credits": peer_credits,
            "verifier_credits": verifier_credits,
            "coordinator_worker_credits": dict(status["credits"]),
        },
        "verification_rate": round(jobs_verified / jobs_created, 3) if jobs_created else 0.0,
        "average_latency_by_route": _average_latency_by_route(route_latencies),
        "provider_snapshot": provider_stats,
        "route_events": route_events,
        "failure_reasons": failure_reasons,
        "final_coordinator_snapshot": snapshot,
    }
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    config.report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def provider_capability_profile(
    *,
    config: ProviderConfig,
    node_role: str,
    subscriber_id: str | None = None,
    subscriber_plan: str | None = None,
) -> dict[str, Any]:
    if node_role not in PROVIDER_NODE_ROLES:
        raise ValueError(f"unsupported provider node role: {node_role}")
    return {
        "supported_job_types": list(config.allowed_job_types),
        "capability_tier": "standard" if node_role == "provider_edge_worker" else "light",
        "node_role": node_role,
        "provider_role": node_role,
        "provider_id": config.provider_id,
        "provider_name": config.provider_name,
        "region": config.region,
        "subscriber_id": subscriber_id,
        "subscriber_plan": subscriber_plan,
        "hardware": {
            "system": "provider-edge-simulation",
            "machine": node_role,
            "capability_tier": "standard" if node_role == "provider_edge_worker" else "light",
        },
        "benchmark": {"cpu_iterations_per_second": 0},
        "gpu": {"available": False, "provider": None, "devices": [], "total_vram_mb": None},
        "model_runtimes": {},
        "privacy_mode": dict(config.privacy_mode_defaults),
    }


def select_provider_route(
    *,
    provider: ProviderConfig,
    subscriber_id: str,
    local_workers: list[WorkerNode],
    edge_workers: list[WorkerNode],
    peer_workers: list[WorkerNode],
    route_counts: dict[str, int],
    route_budgets: dict[str, int | None],
) -> dict[str, Any]:
    for policy in provider.default_routing_policy:
        if policy == "prefer_local":
            local = [
                worker
                for worker in local_workers
                if worker.capabilities().get("subscriber_id") == subscriber_id and _route_has_budget("local", route_counts, route_budgets)
            ]
            if local:
                return {"route": "local", "worker": local[route_counts["local"] % len(local)], "reason": policy}
        elif policy == "prefer_provider_edge":
            if edge_workers and _route_has_budget("provider_edge", route_counts, route_budgets):
                return {
                    "route": "provider_edge",
                    "worker": edge_workers[route_counts["provider_edge"] % len(edge_workers)],
                    "reason": policy,
                }
        elif policy == "prefer_trusted_peer":
            if peer_workers and bool(provider.privacy_mode_defaults.get("allow_peer_fallback", True)):
                return {"route": "peer", "worker": peer_workers[route_counts["peer"] % len(peer_workers)], "reason": policy}
        elif policy == "fallback_placeholder":
            return {"route": "fallback_placeholder", "worker": None, "reason": policy}
        else:
            raise ValueError(f"unsupported provider routing policy entry: {policy}")
    return {"route": "fallback_placeholder", "worker": None, "reason": "policy_exhausted"}


def provider_snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    nodes = snapshot.get("nodes", [])
    jobs = snapshot.get("jobs", [])
    results = snapshot.get("results", [])
    role_groups = {
        "subscriber_nodes": [node for node in nodes if str(node.get("node_role", "")).startswith("subscriber_")],
        "provider_edge_workers": [node for node in nodes if node.get("node_role") == "provider_edge_worker"],
        "contributor_workers": [node for node in nodes if node.get("node_role") == "contributor_worker"],
        "verifiers": [node for node in nodes if node.get("node_role") == "verifier"],
    }
    route_counts = {route: 0 for route in ROUTE_NAMES}
    route_latencies: dict[str, list[float]] = {route: [] for route in ROUTE_NAMES}
    subscriber_spend: dict[str, int] = {}
    for job in jobs:
        requirements = job.get("resource_requirements", {})
        route = requirements.get("provider_route")
        subscriber_id = requirements.get("subscriber_id")
        if route in route_counts:
            route_counts[route] += 1
        if subscriber_id:
            subscriber_spend[subscriber_id] = subscriber_spend.get(subscriber_id, 0) + int(job.get("reward", 1))
    job_routes = {
        job["job_id"]: job.get("resource_requirements", {}).get("provider_route")
        for job in jobs
    }
    for result in results:
        route = job_routes.get(result.get("job_id"))
        if route in route_latencies:
            route_latencies[route].append(float(result.get("runtime_seconds") or 0.0))
    return {
        "subscribers": len({node.get("subscriber_id") for node in nodes if node.get("subscriber_id")}),
        "subscriber_nodes": _liveness_counts(role_groups["subscriber_nodes"]),
        "provider_edge_workers": _liveness_counts(role_groups["provider_edge_workers"]),
        "contributor_workers": _liveness_counts(role_groups["contributor_workers"]),
        "verifiers": _liveness_counts(role_groups["verifiers"]),
        "jobs_routed": route_counts,
        "credits_spent_by_subscriber": subscriber_spend,
        "credits_earned_by_workers": dict(snapshot.get("status", {}).get("credits", {})),
        "verification_rate": (
            round(snapshot["status"]["verified_jobs"] / len(jobs), 3) if jobs else 0.0
        ),
        "disputed_jobs": snapshot.get("status", {}).get("disputed_jobs", 0),
        "expired_jobs": snapshot.get("status", {}).get("expired_jobs", 0),
        "average_latency_by_route": _average_latency_by_route(route_latencies),
    }


def write_provider_config(path: Path, config: ProviderConfig) -> None:
    _validate_provider_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def load_provider_config(path: Path) -> ProviderConfig:
    return ProviderConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _validate_provider_config(config: ProviderConfig) -> None:
    if config.schema != PROVIDER_CONFIG_SCHEMA:
        raise ValueError(f"unsupported provider config schema: {config.schema!r}")
    if not config.provider_id:
        raise ValueError("provider_id must be non-empty")
    if not config.provider_name:
        raise ValueError("provider_name must be non-empty")
    if not config.region:
        raise ValueError("region must be non-empty")
    if not config.allowed_job_types:
        raise ValueError("allowed_job_types must be non-empty")
    if not config.default_routing_policy:
        raise ValueError("default_routing_policy must be non-empty")
    unsupported = [policy for policy in config.default_routing_policy if policy not in DEFAULT_ROUTING_POLICY]
    if unsupported:
        raise ValueError(f"unsupported routing policy entries: {unsupported}")


def _validate_provider_edge_proof_config(config: ProviderEdgeProofConfig) -> None:
    if config.subscribers < 1:
        raise ValueError("--subscribers must be at least 1")
    if config.edge_workers < 0:
        raise ValueError("--edge-workers cannot be negative")
    if config.peer_workers < 0:
        raise ValueError("--peer-workers cannot be negative")
    if config.verifier_workers < 1:
        raise ValueError("--verifier-workers must be at least 1")
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")


def _provider_public_summary(config: ProviderConfig) -> dict[str, Any]:
    return {
        "schema": config.schema,
        "provider_id": config.provider_id,
        "provider_name": config.provider_name,
        "region": config.region,
        "allowed_job_types": list(config.allowed_job_types),
        "default_routing_policy": list(config.default_routing_policy),
        "subscriber_count": len(config.subscribers),
    }


def _proof_subscribers(config: ProviderConfig, count: int) -> list[dict[str, Any]]:
    subscribers = list(config.subscribers.values())[:count]
    while len(subscribers) < count:
        index = len(subscribers) + 1
        subscribers.append(
            {
                "subscriber_id": f"sub_demo_{index:03d}",
                "plan": "Broadband AI Plus",
                "created_at": _iso_now(),
                "starting_credits": int(config.subscriber_credit_policy.get("starting_credits", 100)),
                "synthetic": True,
            }
        )
    return subscribers


def _make_worker(
    *,
    provider: ProviderConfig,
    role: str,
    index: int,
    subscriber_id: str | None = None,
    subscriber_plan: str | None = None,
) -> WorkerNode:
    identity = NodeIdentity.generate(prefix=role)
    return WorkerNode(
        identity=identity,
        capability_profile=provider_capability_profile(
            config=provider,
            node_role=role,
            subscriber_id=subscriber_id,
            subscriber_plan=subscriber_plan,
        ),
    )


def _route_budgets(
    config: ProviderEdgeProofConfig,
    *,
    local_workers: list[WorkerNode],
    edge_workers: list[WorkerNode],
) -> dict[str, int | None]:
    local_budget = max(1, config.jobs // 3) if local_workers else 0
    edge_budget = max(1, config.jobs // 3) if edge_workers else 0
    return {
        "local": local_budget,
        "provider_edge": edge_budget,
        "peer": None,
        "fallback_placeholder": None,
    }


def _route_has_budget(route: str, route_counts: dict[str, int], route_budgets: dict[str, int | None]) -> bool:
    budget = route_budgets.get(route)
    return budget is None or route_counts.get(route, 0) < budget


def _run_signed_worker_once(coordinator: Coordinator, worker: WorkerNode) -> dict[str, Any]:
    request = JobLeaseRequest.create(node=worker.identity)
    leased = coordinator.lease_next_signed_job(request)
    if leased is None:
        raise RuntimeError(f"no provider proof job available for {worker.identity.node_id}")
    job, lease = leased
    grant = JobLeaseGrant.from_dict(lease["grant"])
    acknowledgement = JobLeaseAcknowledgement.create(node=worker.identity, grant=grant)
    if not coordinator.acknowledge_lease(acknowledgement):
        raise RuntimeError(f"lease acknowledgement rejected for {worker.identity.node_id}")
    result = worker.run_job(job)
    accepted = coordinator.submit_result(result)
    return {
        "job_id": job.job_id,
        "node_id": worker.identity.node_id,
        "accepted": accepted,
        "runtime_seconds": result.runtime_seconds,
    }


def _provider_deterministic_payload(index: int) -> dict[str, Any]:
    left = index + 11
    right = (index * 2) + 5
    return {
        "task": "arithmetic",
        "operation": "add",
        "operands": [left, right],
        "expected": left + right,
    }


def _initial_subscriber_ledger(
    provider: ProviderConfig,
    subscribers: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    starting_credits = int(provider.subscriber_credit_policy.get("starting_credits", 100))
    return {
        subscriber["subscriber_id"]: {
            "starting_credits": int(subscriber.get("starting_credits", starting_credits)),
            "spent": 0,
            "earned": 0,
            "balance": int(subscriber.get("starting_credits", starting_credits)),
        }
        for subscriber in subscribers
    }


def _spend_subscriber_credit(
    provider: ProviderConfig,
    ledger: dict[str, dict[str, int]],
    subscriber_id: str,
) -> None:
    ledger[subscriber_id]["spent"] += int(provider.subscriber_credit_policy.get("job_cost", 1))


def _liveness_counts(nodes: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(nodes),
        "live": sum(1 for node in nodes if node.get("liveness_status") == "live"),
        "stale": sum(1 for node in nodes if node.get("liveness_status") == "stale"),
        "offline": sum(1 for node in nodes if node.get("liveness_status") == "offline"),
    }


def _average_latency_by_route(route_latencies: dict[str, list[float]]) -> dict[str, float | None]:
    averages: dict[str, float | None] = {}
    for route, values in route_latencies.items():
        averages[route] = round(sum(values) / len(values), 6) if values else None
    return averages


def _load_or_create_identity(home: Path, name: str) -> NodeIdentity:
    path = home / f"{name}.identity.json"
    if path.exists():
        return NodeIdentity.load(path)
    identity = NodeIdentity.generate(prefix=name)
    identity.save(path)
    return identity


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

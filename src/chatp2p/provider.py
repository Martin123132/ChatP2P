"""ISP-edge / broadband-bundle simulation helpers."""

from __future__ import annotations

import json
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .benchmark import CAPABILITY_PROFILE_NAME
from .client import CoordinatorClient
from .coordinator import Coordinator
from .crypto import NodeIdentity
from .jsonio import read_json_file
from .packets import JobLeaseAcknowledgement, JobLeaseGrant, JobLeaseRequest, NodeRegistration
from .worker import WorkerNode

PROVIDER_CONFIG_SCHEMA = "chatp2p.provider-config.v1"
PROVIDER_JOIN_REPORT_SCHEMA = "chatp2p.provider-join-report.v1"
PROVIDER_EDGE_PROOF_REPORT_SCHEMA = "chatp2p.provider-edge-proof-report.v1"
PROVIDER_OPS_PACK_SCHEMA = "chatp2p.provider-ops-pack.v1"
PROVIDER_REMOTE_PROOF_REPORT_SCHEMA = "chatp2p.provider-remote-proof-report.v1"
PROVIDER_STATUS_REPORT_SCHEMA = "chatp2p.provider-status-report.v1"

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


@dataclass(frozen=True)
class ProviderOpsPackConfig:
    provider_config_path: Path
    out_dir: Path
    subscribers: int = 3
    edge_workers: int = 1
    jobs: int = 25
    peer_workers: int = 1
    verifier_workers: int = 1
    timeout_seconds: float = 60.0
    create_zip: bool = True
    zip_path: Path | None = None


@dataclass(frozen=True)
class ProviderRemoteProofConfig:
    provider_config_path: Path
    coordinator_url: str
    admission_token: str | None
    report_path: Path
    expected_worker_id: str | None = None
    subscriber_id: str | None = None
    jobs: int = 10
    min_live_workers: int = 2
    min_accepted_results: int | None = None
    min_verified_jobs: int | None = None
    min_expected_worker_results: int | None = None
    timeout_seconds: float = 120.0
    poll_interval: float = 0.5


@dataclass(frozen=True)
class ProviderStatusConfig:
    provider_config_path: Path
    coordinator_url: str
    admission_token: str | None = None
    report_path: Path | None = None
    expected_worker_id: str | None = None
    timeout_seconds: float = 10.0


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


def apply_provider_metadata(
    *,
    capabilities: dict[str, Any],
    config: ProviderConfig,
    node_role: str,
    subscriber_id: str | None = None,
) -> dict[str, Any]:
    if node_role not in PROVIDER_NODE_ROLES:
        raise ValueError(f"unsupported provider node role: {node_role}")
    if node_role.startswith("subscriber_") and not subscriber_id:
        raise ValueError("subscriber_id is required for subscriber provider roles")
    if subscriber_id and subscriber_id not in config.subscribers:
        raise ValueError(f"unknown subscriber_id: {subscriber_id}")

    updated = dict(capabilities)
    supported = list(dict.fromkeys([*updated.get("supported_job_types", []), *config.allowed_job_types]))
    subscriber = config.subscribers.get(subscriber_id or "", {})
    updated.update(
        {
            "supported_job_types": supported,
            "node_role": node_role,
            "provider_role": node_role,
            "provider_id": config.provider_id,
            "provider_name": config.provider_name,
            "region": config.region,
            "subscriber_id": subscriber_id,
            "subscriber_plan": subscriber.get("plan"),
            "privacy_mode": dict(config.privacy_mode_defaults),
        }
    )
    hardware = dict(updated.get("hardware", {}))
    hardware.setdefault("provider_mode", "provider-edge-simulation")
    hardware.setdefault("provider_role", node_role)
    updated["hardware"] = hardware
    return updated


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


def run_provider_ops_pack(config: ProviderOpsPackConfig) -> dict[str, Any]:
    _validate_provider_ops_pack_config(config)
    provider = load_provider_config(config.provider_config_path)
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    proof_path = out_dir / "provider-edge-proof.json"
    summary_path = out_dir / "provider-ops-pack-summary.json"
    markdown_path = out_dir / "provider-ops-pack-summary.md"
    handoff_path = out_dir / "provider-handoff.md"
    zip_path = config.zip_path.expanduser().resolve() if config.zip_path is not None else out_dir.with_suffix(".zip")

    proof = run_provider_edge_proof(
        ProviderEdgeProofConfig(
            provider_config_path=config.provider_config_path,
            subscribers=config.subscribers,
            edge_workers=config.edge_workers,
            peer_workers=config.peer_workers,
            verifier_workers=config.verifier_workers,
            jobs=config.jobs,
            report_path=proof_path,
            timeout_seconds=config.timeout_seconds,
        )
    )
    artifacts: dict[str, dict[str, Any]] = {
        "provider_edge_proof": {"path": str(proof_path), "status": "created"},
        "summary_json": {"path": str(summary_path), "status": "planned"},
        "summary_markdown": {"path": str(markdown_path), "status": "planned"},
        "provider_handoff": {"path": str(handoff_path), "status": "planned"},
    }
    if config.create_zip:
        artifacts["zip"] = {"path": str(zip_path), "status": "planned"}

    report = _provider_ops_pack_report(
        config=config,
        provider=provider,
        proof=proof,
        artifacts=artifacts,
    )
    _write_json(summary_path, report)
    _write_text(markdown_path, _provider_ops_pack_markdown(report))
    _write_text(handoff_path, _provider_ops_handoff(report))
    artifacts["summary_json"]["status"] = "created"
    artifacts["summary_markdown"]["status"] = "created"
    artifacts["provider_handoff"]["status"] = "created"

    if config.create_zip:
        try:
            _zip_directory(out_dir, zip_path)
            artifacts["zip"] = {"path": str(zip_path), "status": "created"}
        except OSError as exc:
            artifacts["zip"] = {"path": str(zip_path), "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    report = _provider_ops_pack_report(
        config=config,
        provider=provider,
        proof=proof,
        artifacts=artifacts,
    )
    _write_json(summary_path, report)
    _write_text(markdown_path, _provider_ops_pack_markdown(report))
    _write_text(handoff_path, _provider_ops_handoff(report))
    if config.create_zip and artifacts.get("zip", {}).get("status") == "created":
        _zip_directory(out_dir, zip_path)
    return report


def run_provider_remote_proof(config: ProviderRemoteProofConfig) -> dict[str, Any]:
    _validate_provider_remote_proof_config(config)
    provider = load_provider_config(config.provider_config_path)
    started_at = time.time()
    client = CoordinatorClient(config.coordinator_url, admission_token=config.admission_token)
    errors: list[str] = []
    created_jobs: list[dict[str, Any]] = []
    initial_snapshot: dict[str, Any] | None = None
    final_snapshot: dict[str, Any] | None = None
    initial_result_ids: set[tuple[str, str, str]] = set()

    try:
        initial_snapshot = client.snapshot()
        initial_result_ids = _result_ids(initial_snapshot)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    subscriber_ids = _remote_subscriber_ids(provider, config.subscriber_id)
    if not errors:
        try:
            for index in range(config.jobs):
                route = _remote_requested_route(index)
                subscriber_id = subscriber_ids[index % len(subscriber_ids)]
                job = client.create_job(
                    job_type="eval.deterministic.v1",
                    payload=_provider_deterministic_payload(index),
                    resource_requirements={
                        "provider_mode": True,
                        "provider_id": provider.provider_id,
                        "subscriber_id": subscriber_id,
                        "provider_route": route,
                        "provider_routing_policy": provider.default_routing_policy,
                        "remote_provider_proof": True,
                    },
                    ttl_seconds=300,
                )
                created_jobs.append(
                    {
                        "job_id": job.job_id,
                        "job_type": job.job_type,
                        "provider_route": route,
                        "subscriber_id": subscriber_id,
                        "verification_strategy": job.verification_strategy,
                    }
                )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    created_job_ids = {job["job_id"] for job in created_jobs}
    deadline = started_at + config.timeout_seconds
    criteria = _provider_remote_empty_criteria(config)
    while time.time() <= deadline and created_jobs and not errors:
        try:
            final_snapshot = client.snapshot()
            criteria = _provider_remote_criteria(final_snapshot, created_job_ids, initial_result_ids, config)
            if all(item["passed"] for item in criteria.values()):
                break
            if (
                criteria["all_created_jobs_terminal"]["passed"]
                and (
                    not criteria["verified_jobs"]["passed"]
                    or not criteria["disputed_jobs"]["passed"]
                    or not criteria["expired_jobs"]["passed"]
                )
            ):
                break
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            break
        time.sleep(config.poll_interval)

    if final_snapshot is None and not errors:
        try:
            final_snapshot = client.snapshot()
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    if final_snapshot is not None:
        criteria = _provider_remote_criteria(final_snapshot, created_job_ids, initial_result_ids, config)

    if created_jobs and not errors and not all(item["passed"] for item in criteria.values()):
        errors.append("provider remote proof criteria were not met before timeout")
    if len(created_jobs) != config.jobs and not errors:
        errors.append(f"created {len(created_jobs)} of {config.jobs} requested jobs")

    report = _provider_remote_report(
        config=config,
        provider=provider,
        duration_seconds=time.time() - started_at,
        initial_snapshot=initial_snapshot,
        final_snapshot=final_snapshot,
        created_jobs=created_jobs,
        criteria=criteria,
        errors=errors,
        initial_result_ids=initial_result_ids,
    )
    _write_json(config.report_path, report)
    return report


def run_provider_status(config: ProviderStatusConfig) -> dict[str, Any]:
    if not config.coordinator_url.strip():
        raise ValueError("coordinator_url must be non-empty")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.expected_worker_id is not None and not config.expected_worker_id.strip():
        raise ValueError("--expected-worker-id cannot be blank")

    provider = load_provider_config(config.provider_config_path)
    client = CoordinatorClient(
        config.coordinator_url,
        admission_token=config.admission_token,
        timeout_seconds=config.timeout_seconds,
    )
    errors: list[str] = []
    health: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None

    try:
        health = client.health()
    except Exception as exc:
        errors.append(f"health check failed: {type(exc).__name__}: {exc}")

    try:
        snapshot = client.snapshot()
    except Exception as exc:
        errors.append(f"snapshot failed: {type(exc).__name__}: {exc}")

    expected_worker = _provider_status_expected_worker(snapshot, config.expected_worker_id)
    summary = provider_snapshot_summary(snapshot or {})
    summary["configured_subscribers"] = len(provider.subscribers)
    expected_worker_ok = (
        config.expected_worker_id is None
        or bool(expected_worker and expected_worker.get("live") and expected_worker.get("present"))
    )
    ok = not errors and snapshot is not None and expected_worker_ok
    report = {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "schema": PROVIDER_STATUS_REPORT_SCHEMA,
        "generated_at": _iso_now(),
        "provider": _provider_public_summary(provider),
        "coordinator": config.coordinator_url,
        "criteria": {
            "coordinator_health": {
                "actual": bool(health),
                "required": True,
                "passed": bool(health) and not any(error.startswith("health check failed") for error in errors),
            },
            "snapshot": {
                "actual": snapshot is not None,
                "required": True,
                "passed": snapshot is not None,
            },
            "expected_worker_live": {
                "actual": int(bool(expected_worker and expected_worker.get("live"))),
                "required": 1 if config.expected_worker_id else 0,
                "passed": expected_worker_ok,
            },
        },
        "expected_worker": expected_worker,
        "health": health,
        "summary": summary,
        "nodes": _provider_status_node_summaries(snapshot or {}, provider.provider_id),
        "job_status_counts": _job_status_counts((snapshot or {}).get("jobs", [])),
        "result_node_counts": _result_node_counts((snapshot or {}).get("results", [])),
        "result_route_counts": _actual_result_route_counts(snapshot or {}, (snapshot or {}).get("results", [])),
        "coordinator_status": (snapshot or {}).get("status"),
        "errors": errors,
    }
    if config.report_path is not None:
        _write_json(config.report_path, report)
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
    result_route_counts = {route: 0 for route in [*ROUTE_NAMES, "unknown"]}
    route_latencies: dict[str, list[float]] = {route: [] for route in ROUTE_NAMES}
    subscriber_spend: dict[str, int] = {}
    credits_by_role = {
        "subscriber_nodes": 0,
        "provider_edge_workers": 0,
        "contributor_workers": 0,
        "verifiers": 0,
        "legacy_workers": 0,
    }
    nodes_by_id = {node.get("node_id"): node for node in nodes}
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
        role_route = _route_for_node_role((nodes_by_id.get(result.get("node_id")) or {}).get("node_role"))
        result_route_counts[role_route] += 1
    for node in nodes:
        credits = int(node.get("credits") or 0)
        role = str(node.get("node_role") or "worker")
        if role.startswith("subscriber_"):
            credits_by_role["subscriber_nodes"] += credits
        elif role == "provider_edge_worker":
            credits_by_role["provider_edge_workers"] += credits
        elif role == "contributor_worker":
            credits_by_role["contributor_workers"] += credits
        elif role == "verifier":
            credits_by_role["verifiers"] += credits
        else:
            credits_by_role["legacy_workers"] += credits
    return {
        "subscribers": len({node.get("subscriber_id") for node in nodes if node.get("subscriber_id")}),
        "subscriber_nodes": _liveness_counts(role_groups["subscriber_nodes"]),
        "provider_edge_workers": _liveness_counts(role_groups["provider_edge_workers"]),
        "contributor_workers": _liveness_counts(role_groups["contributor_workers"]),
        "verifiers": _liveness_counts(role_groups["verifiers"]),
        "jobs_routed": route_counts,
        "results_by_route": result_route_counts,
        "credits_spent_by_subscriber": subscriber_spend,
        "credits_earned_by_workers": dict(snapshot.get("status", {}).get("credits", {})),
        "credits_by_role": credits_by_role,
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
    data = read_json_file(path, description="provider config file")
    if not isinstance(data, dict):
        raise ValueError(f"provider config file must contain a JSON object: {path}")
    try:
        return ProviderConfig.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"provider config file is invalid: {path}: {exc}") from exc


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


def _validate_provider_ops_pack_config(config: ProviderOpsPackConfig) -> None:
    _validate_provider_edge_proof_config(
        ProviderEdgeProofConfig(
            provider_config_path=config.provider_config_path,
            subscribers=config.subscribers,
            edge_workers=config.edge_workers,
            peer_workers=config.peer_workers,
            verifier_workers=config.verifier_workers,
            jobs=config.jobs,
            report_path=config.out_dir / "provider-edge-proof.json",
            timeout_seconds=config.timeout_seconds,
        )
    )


def _validate_provider_remote_proof_config(config: ProviderRemoteProofConfig) -> None:
    if not config.coordinator_url.strip():
        raise ValueError("coordinator_url must be non-empty")
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.min_accepted_results is not None and config.min_accepted_results < 0:
        raise ValueError("--min-accepted-results cannot be negative")
    if config.min_verified_jobs is not None and config.min_verified_jobs < 0:
        raise ValueError("--min-verified-jobs cannot be negative")
    if config.min_expected_worker_results is not None and config.min_expected_worker_results < 0:
        raise ValueError("--min-expected-worker-results cannot be negative")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.expected_worker_id is not None and not config.expected_worker_id.strip():
        raise ValueError("--expected-worker-id cannot be blank")


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


def _provider_remote_required_accepted_results(config: ProviderRemoteProofConfig) -> int:
    if config.min_accepted_results is not None:
        return config.min_accepted_results
    return config.jobs * 2


def _provider_remote_required_verified_jobs(config: ProviderRemoteProofConfig) -> int:
    if config.min_verified_jobs is not None:
        return config.min_verified_jobs
    return config.jobs


def _provider_remote_required_expected_worker_results(config: ProviderRemoteProofConfig) -> int:
    if config.min_expected_worker_results is not None:
        return config.min_expected_worker_results
    return 1 if config.expected_worker_id else 0


def _provider_remote_empty_criteria(config: ProviderRemoteProofConfig) -> dict[str, Any]:
    expected_required = _provider_remote_required_expected_worker_results(config)
    return {
        "created_jobs": {"actual": 0, "required": config.jobs, "passed": False},
        "live_workers": {"actual": 0, "required": config.min_live_workers, "passed": False},
        "accepted_results": {
            "actual": 0,
            "required": _provider_remote_required_accepted_results(config),
            "passed": False,
        },
        "verified_jobs": {
            "actual": 0,
            "required": _provider_remote_required_verified_jobs(config),
            "passed": False,
        },
        "all_created_jobs_terminal": {"actual": 0, "required": config.jobs, "passed": False},
        "disputed_jobs": {"actual": 0, "required": 0, "passed": True},
        "expired_jobs": {"actual": 0, "required": 0, "passed": True},
        "incomplete_jobs": {"actual": config.jobs, "required": 0, "passed": False},
        "expected_worker_live": {
            "actual": 0,
            "required": 1 if config.expected_worker_id else 0,
            "passed": config.expected_worker_id is None,
        },
        "expected_worker_results": {
            "actual": 0,
            "required": expected_required,
            "passed": expected_required == 0,
        },
    }


def _provider_remote_criteria(
    snapshot: dict[str, Any],
    created_job_ids: set[str],
    initial_result_ids: set[tuple[str, str, str]],
    config: ProviderRemoteProofConfig,
) -> dict[str, Any]:
    status = snapshot.get("status", {})
    created_jobs = _snapshot_jobs_for_ids(snapshot, created_job_ids)
    created_results = _snapshot_results_for_ids(snapshot, created_job_ids, initial_result_ids)
    status_counts = _job_status_counts(created_jobs)
    terminal_jobs = sum(1 for job in created_jobs if job.get("status") in {"verified", "disputed", "expired"})
    incomplete_jobs = sum(1 for job in created_jobs if job.get("status") not in {"verified", "disputed", "expired"})
    expected_worker = _snapshot_node(snapshot, config.expected_worker_id) if config.expected_worker_id else None
    expected_worker_live = int(
        bool(expected_worker is not None and expected_worker.get("liveness_status") == "live")
    )
    expected_worker_results = (
        sum(1 for result in created_results if result.get("node_id") == config.expected_worker_id)
        if config.expected_worker_id
        else 0
    )
    expected_results_required = _provider_remote_required_expected_worker_results(config)
    return {
        "created_jobs": {
            "actual": len(created_job_ids),
            "required": config.jobs,
            "passed": len(created_job_ids) == config.jobs,
        },
        "live_workers": {
            "actual": int(status.get("live_nodes") or 0),
            "required": config.min_live_workers,
            "passed": int(status.get("live_nodes") or 0) >= config.min_live_workers,
        },
        "accepted_results": {
            "actual": len(created_results),
            "required": _provider_remote_required_accepted_results(config),
            "passed": len(created_results) >= _provider_remote_required_accepted_results(config),
        },
        "verified_jobs": {
            "actual": status_counts["verified"],
            "required": _provider_remote_required_verified_jobs(config),
            "passed": status_counts["verified"] >= _provider_remote_required_verified_jobs(config),
        },
        "all_created_jobs_terminal": {
            "actual": terminal_jobs,
            "required": len(created_job_ids),
            "passed": bool(created_job_ids) and terminal_jobs == len(created_job_ids),
        },
        "disputed_jobs": {
            "actual": status_counts["disputed"],
            "required": 0,
            "passed": status_counts["disputed"] == 0,
        },
        "expired_jobs": {
            "actual": status_counts["expired"],
            "required": 0,
            "passed": status_counts["expired"] == 0,
        },
        "incomplete_jobs": {
            "actual": incomplete_jobs,
            "required": 0,
            "passed": incomplete_jobs == 0,
        },
        "expected_worker_live": {
            "actual": expected_worker_live,
            "required": 1 if config.expected_worker_id else 0,
            "passed": config.expected_worker_id is None or expected_worker_live == 1,
        },
        "expected_worker_results": {
            "actual": expected_worker_results,
            "required": expected_results_required,
            "passed": expected_worker_results >= expected_results_required,
        },
    }


def _provider_remote_report(
    *,
    config: ProviderRemoteProofConfig,
    provider: ProviderConfig,
    duration_seconds: float,
    initial_snapshot: dict[str, Any] | None,
    final_snapshot: dict[str, Any] | None,
    created_jobs: list[dict[str, Any]],
    criteria: dict[str, Any],
    errors: list[str],
    initial_result_ids: set[tuple[str, str, str]],
) -> dict[str, Any]:
    created_job_ids = {job["job_id"] for job in created_jobs}
    final_created_jobs = _snapshot_jobs_for_ids(final_snapshot or {}, created_job_ids)
    created_results = _snapshot_results_for_ids(final_snapshot or {}, created_job_ids, initial_result_ids)
    expected_worker = _snapshot_node(final_snapshot or {}, config.expected_worker_id) if config.expected_worker_id else None
    ok = not errors and all(item["passed"] for item in criteria.values())
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "schema": PROVIDER_REMOTE_PROOF_REPORT_SCHEMA,
        "generated_at": _iso_now(),
        "duration_seconds": round(duration_seconds, 3),
        "provider": _provider_public_summary(provider),
        "coordinator": config.coordinator_url,
        "parameters": {
            "jobs": config.jobs,
            "subscriber_id": config.subscriber_id,
            "expected_worker_id": config.expected_worker_id,
            "min_live_workers": config.min_live_workers,
            "min_accepted_results": _provider_remote_required_accepted_results(config),
            "min_verified_jobs": _provider_remote_required_verified_jobs(config),
            "min_expected_worker_results": _provider_remote_required_expected_worker_results(config),
            "timeout_seconds": config.timeout_seconds,
            "poll_interval": config.poll_interval,
        },
        "baseline": {
            "job_count": len((initial_snapshot or {}).get("jobs", [])),
            "result_count": len((initial_snapshot or {}).get("results", [])),
            "status": (initial_snapshot or {}).get("status"),
        },
        "created_jobs": created_jobs,
        "created_job_statuses": final_created_jobs,
        "created_job_status_counts": _job_status_counts(final_created_jobs),
        "created_result_count": len(created_results),
        "result_node_counts": _result_node_counts(created_results),
        "requested_route_counts": _requested_route_counts(created_jobs),
        "actual_result_route_counts": _actual_result_route_counts(final_snapshot or {}, created_results),
        "expected_worker": _provider_remote_expected_worker_summary(expected_worker, created_results, config),
        "provider_snapshot": provider_snapshot_summary(final_snapshot or {}),
        "criteria": criteria,
        "errors": errors,
        "final_summary": {
            "status": (final_snapshot or {}).get("status"),
            "provider": (final_snapshot or {}).get("provider"),
        },
        "final_snapshot": final_snapshot,
    }


def _provider_remote_expected_worker_summary(
    node: dict[str, Any] | None,
    created_results: list[dict[str, Any]],
    config: ProviderRemoteProofConfig,
) -> dict[str, Any] | None:
    if config.expected_worker_id is None:
        return None
    results = [result for result in created_results if result.get("node_id") == config.expected_worker_id]
    return {
        "node_id": config.expected_worker_id,
        "present": node is not None,
        "live": bool(node and node.get("liveness_status") == "live"),
        "node_role": (node or {}).get("node_role"),
        "provider_id": (node or {}).get("provider_id"),
        "subscriber_id": (node or {}).get("subscriber_id"),
        "supported_job_types": (node or {}).get("supported_job_types", []),
        "result_count": len(results),
    }


def _provider_status_expected_worker(
    snapshot: dict[str, Any] | None,
    expected_worker_id: str | None,
) -> dict[str, Any] | None:
    if expected_worker_id is None:
        return None
    node = _snapshot_node(snapshot or {}, expected_worker_id)
    return {
        "node_id": expected_worker_id,
        "present": node is not None,
        "live": bool(node and node.get("liveness_status") == "live"),
        "liveness_status": (node or {}).get("liveness_status"),
        "node_role": (node or {}).get("node_role"),
        "provider_id": (node or {}).get("provider_id"),
        "subscriber_id": (node or {}).get("subscriber_id"),
        "credits": (node or {}).get("credits"),
        "supported_job_types": (node or {}).get("supported_job_types", []),
        "ollama_models": (node or {}).get("ollama_models", []),
    }


def _provider_status_node_summaries(snapshot: dict[str, Any], provider_id: str) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for node in snapshot.get("nodes", []):
        role = node.get("node_role") or "worker"
        if node.get("provider_id") != provider_id and role not in PROVIDER_NODE_ROLES:
            continue
        summaries.append(
            {
                "node_id": node.get("node_id"),
                "node_role": role,
                "provider_id": node.get("provider_id"),
                "subscriber_id": node.get("subscriber_id"),
                "subscriber_plan": node.get("subscriber_plan"),
                "region": node.get("region"),
                "liveness_status": node.get("liveness_status"),
                "last_seen_seconds_ago": node.get("last_seen_seconds_ago"),
                "credits": node.get("credits"),
                "reputation_status": (node.get("reputation") or {}).get("status"),
                "supported_job_types": node.get("supported_job_types", []),
                "ollama_available": bool((node.get("model_runtimes") or {}).get("ollama", {}).get("available")),
                "ollama_models": node.get("ollama_models", []),
            }
        )
    return sorted(summaries, key=lambda item: (str(item.get("node_role")), str(item.get("node_id"))))


def _remote_requested_route(index: int) -> str:
    return ["local", "provider_edge", "peer"][index % 3]


def _remote_subscriber_ids(provider: ProviderConfig, subscriber_id: str | None) -> list[str]:
    if subscriber_id:
        if subscriber_id not in provider.subscribers:
            raise ValueError(f"unknown subscriber_id: {subscriber_id}")
        return [subscriber_id]
    if provider.subscribers:
        return list(provider.subscribers)
    return ["sub_demo_001"]


def _result_ids(snapshot: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (str(result.get("job_id")), str(result.get("node_id")), str(result.get("output_hash")))
        for result in snapshot.get("results", [])
    }


def _snapshot_node(snapshot: dict[str, Any], node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    for node in snapshot.get("nodes", []):
        if node.get("node_id") == node_id:
            return node
    return None


def _snapshot_jobs_for_ids(snapshot: dict[str, Any], job_ids: set[str]) -> list[dict[str, Any]]:
    return [job for job in snapshot.get("jobs", []) if job.get("job_id") in job_ids]


def _snapshot_results_for_ids(
    snapshot: dict[str, Any],
    job_ids: set[str],
    initial_result_ids: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    return [
        result for result in snapshot.get("results", [])
        if result.get("job_id") in job_ids
        and (
            str(result.get("job_id")),
            str(result.get("node_id")),
            str(result.get("output_hash")),
        )
        not in initial_result_ids
    ]


def _job_status_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"queued": 0, "leased": 0, "pending": 0, "verified": 0, "disputed": 0, "expired": 0}
    for job in jobs:
        status = str(job.get("status", "queued"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _result_node_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        node_id = str(result.get("node_id"))
        counts[node_id] = counts.get(node_id, 0) + 1
    return dict(sorted(counts.items()))


def _requested_route_counts(created_jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {route: 0 for route in ROUTE_NAMES}
    for job in created_jobs:
        route = job.get("provider_route")
        if route in counts:
            counts[route] += 1
    return counts


def _actual_result_route_counts(snapshot: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, int]:
    nodes = {node.get("node_id"): node for node in snapshot.get("nodes", [])}
    counts = {route: 0 for route in [*ROUTE_NAMES, "unknown"]}
    for result in results:
        role = (nodes.get(result.get("node_id")) or {}).get("node_role")
        route = _route_for_node_role(role)
        counts[route] += 1
    return counts


def _route_for_node_role(role: str | None) -> str:
    if role in {"subscriber_gateway", "subscriber_device"}:
        return "local"
    if role == "provider_edge_worker":
        return "provider_edge"
    if role == "contributor_worker":
        return "peer"
    return "unknown"


def _provider_ops_pack_report(
    *,
    config: ProviderOpsPackConfig,
    provider: ProviderConfig,
    proof: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    checks = [
        {
            "name": "provider_edge_proof",
            "passed": bool(proof.get("ok")),
            "details": {
                "status": proof.get("status"),
                "jobs_verified": proof.get("jobs_verified"),
                "jobs_created": proof.get("jobs_created"),
                "failure_reasons": proof.get("failure_reasons", []),
            },
        },
        {
            "name": "zero_disputes",
            "passed": proof.get("jobs_disputed") == 0,
            "details": {"jobs_disputed": proof.get("jobs_disputed")},
        },
        {
            "name": "zero_fallback_placeholder",
            "passed": proof.get("route_counts", {}).get("fallback_placeholder") == 0,
            "details": {"route_counts": proof.get("route_counts", {})},
        },
    ]
    if config.create_zip:
        checks.append(
            {
                "name": "zip_pack",
                "passed": artifacts.get("zip", {}).get("status") == "created",
                "details": artifacts.get("zip", {}),
            }
        )
    ok = all(check["passed"] for check in checks)
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "schema": PROVIDER_OPS_PACK_SCHEMA,
        "generated_at": _iso_now(),
        "provider": _provider_public_summary(provider),
        "parameters": {
            "provider_config": str(config.provider_config_path.expanduser().resolve()),
            "out": str(config.out_dir.expanduser().resolve()),
            "subscribers": config.subscribers,
            "edge_workers": config.edge_workers,
            "peer_workers": config.peer_workers,
            "verifier_workers": config.verifier_workers,
            "jobs": config.jobs,
            "timeout_seconds": config.timeout_seconds,
            "create_zip": config.create_zip,
            "zip_path": str(config.zip_path.expanduser().resolve()) if config.zip_path else None,
        },
        "artifacts": artifacts,
        "checks": checks,
        "proof": _provider_edge_proof_summary(proof),
    }


def _provider_edge_proof_summary(proof: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": proof.get("schema"),
        "status": proof.get("status"),
        "provider_id": proof.get("provider_id"),
        "subscribers_created": proof.get("subscribers_created"),
        "subscriber_nodes_live": proof.get("subscriber_nodes_live"),
        "edge_workers_live": proof.get("edge_workers_live"),
        "jobs_created": proof.get("jobs_created"),
        "jobs_verified": proof.get("jobs_verified"),
        "jobs_disputed": proof.get("jobs_disputed"),
        "jobs_expired": proof.get("jobs_expired"),
        "route_counts": proof.get("route_counts", {}),
        "verification_rate": proof.get("verification_rate"),
        "credit_summary": proof.get("credit_summary", {}),
        "average_latency_by_route": proof.get("average_latency_by_route", {}),
        "failure_reasons": proof.get("failure_reasons", []),
    }


def _provider_ops_pack_markdown(report: dict[str, Any]) -> str:
    proof = report["proof"]
    artifacts = report["artifacts"]
    route_counts = proof.get("route_counts", {})
    checks = "\n".join(
        f"- [{'x' if check['passed'] else ' '}] {check['name']}"
        for check in report["checks"]
    )
    return "\n".join(
        [
            "# ChatP2P Provider Ops Pack",
            "",
            f"Status: **{report['status']}**",
            f"Generated: `{report['generated_at']}`",
            f"Provider: `{report['provider']['provider_name']}` (`{report['provider']['provider_id']}`)",
            f"Region: `{report['provider']['region']}`",
            "",
            "## Proof Summary",
            "",
            f"- Jobs verified: {proof.get('jobs_verified')}/{proof.get('jobs_created')}",
            f"- Disputed jobs: {proof.get('jobs_disputed')}",
            f"- Expired jobs: {proof.get('jobs_expired')}",
            f"- Verification rate: {proof.get('verification_rate')}",
            f"- Subscriber nodes live: {proof.get('subscriber_nodes_live')}",
            f"- Provider edge workers live: {proof.get('edge_workers_live')}",
            f"- Routes: local={route_counts.get('local', 0)}, provider_edge={route_counts.get('provider_edge', 0)}, peer={route_counts.get('peer', 0)}, fallback_placeholder={route_counts.get('fallback_placeholder', 0)}",
            "",
            "## Checks",
            "",
            checks,
            "",
            "## Artifacts",
            "",
            f"- Provider edge proof: `{artifacts['provider_edge_proof']['path']}`",
            f"- Summary JSON: `{artifacts['summary_json']['path']}`",
            f"- Provider handoff: `{artifacts['provider_handoff']['path']}`",
            f"- Zip: `{(artifacts.get('zip') or {}).get('path')}`",
            "",
            "This pack is a simulation artifact. It does not claim real ISP deployment, billing, or production privacy isolation.",
            "",
        ]
    )


def _provider_ops_handoff(report: dict[str, Any]) -> str:
    proof = report["proof"]
    route_counts = proof.get("route_counts", {})
    return "\n".join(
        [
            "# Provider Edge Handoff",
            "",
            "This folder is the ChatP2P ISP-edge / broadband-bundle simulation evidence pack.",
            "",
            "What this proves:",
            "",
            f"- Provider config loaded for `{report['provider']['provider_name']}` in `{report['provider']['region']}`.",
            f"- {proof.get('subscribers_created')} subscriber nodes were simulated.",
            f"- {proof.get('edge_workers_live')} provider edge worker(s) were live.",
            f"- {proof.get('jobs_verified')}/{proof.get('jobs_created')} signed jobs verified.",
            f"- Route counts were local={route_counts.get('local', 0)}, provider_edge={route_counts.get('provider_edge', 0)}, peer={route_counts.get('peer', 0)}, fallback_placeholder={route_counts.get('fallback_placeholder', 0)}.",
            f"- Disputes stayed at {proof.get('jobs_disputed')}.",
            "",
            "What this does not prove:",
            "",
            "- Real ISP deployment.",
            "- Real billing, tokenisation, or money movement.",
            "- Router firmware or physical broadband hardware integration.",
            "- Public internet exposure.",
            "",
            "Repeat command:",
            "",
            "```powershell",
            "python -m chatp2p.cli operator provider-ops-pack `",
            f"  --provider-config {report['parameters']['provider_config']} `",
            f"  --out {report['parameters']['out']} `",
            f"  --subscribers {report['parameters']['subscribers']} `",
            f"  --edge-workers {report['parameters']['edge_workers']} `",
            f"  --jobs {report['parameters']['jobs']}",
            "```",
            "",
            "Keep runtime homes, identity files, SQLite databases, and private alpha invite/operator files out of the pack.",
            "",
        ]
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _zip_directory(directory: Path, zip_path: Path) -> None:
    directory = directory.expanduser().resolve()
    zip_path = zip_path.expanduser().resolve()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved == zip_path:
                continue
            archive.write(resolved, resolved.relative_to(directory.parent).as_posix())


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

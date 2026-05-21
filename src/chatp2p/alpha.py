"""Public-alpha invite and join helpers."""

from __future__ import annotations

import json
import ipaddress
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .benchmark import CAPABILITY_PROFILE_NAME, load_node_capabilities, run_node_benchmark, save_node_benchmark
from .client import CoordinatorClient
from .crypto import NodeIdentity
from .node_runtime import managed_process_status, start_managed_process, stop_managed_process
from .ollama import DEFAULT_OLLAMA_BASE_URL
from .operator_config import DEFAULT_ALLOWED_JOB_TYPES, OperatorConfig, write_operator_config


ALPHA_INVITE_SCHEMA = "chatp2p.alpha-invite.v1"
ALPHA_PREFLIGHT_REPORT_SCHEMA = "chatp2p.alpha-preflight-report.v1"
ALPHA_SMOKE_REPORT_SCHEMA = "chatp2p.alpha-smoke-report.v1"
ALPHA_DRILL_REPORT_SCHEMA = "chatp2p.alpha-drill-report.v1"
ALPHA_ROUTE_REPORT_SCHEMA = "chatp2p.alpha-route-report.v1"
ALPHA_REMOTE_PROOF_REPORT_SCHEMA = "chatp2p.alpha-remote-proof-report.v1"
ALPHA_STATUS_REPORT_SCHEMA = "chatp2p.alpha-status-report.v1"
ALPHA_EVIDENCE_PACK_SCHEMA = "chatp2p.alpha-evidence-pack.v1"
ALPHA_INFERENCE_PROOF_REPORT_SCHEMA = "chatp2p.alpha-inference-proof-report.v1"
NODE_CAPABILITY_REFRESH_REPORT_SCHEMA = "chatp2p.node-capability-refresh-report.v1"
NODE_WATCHDOG_REPORT_SCHEMA = "chatp2p.node-watchdog-report.v1"
DEFAULT_ALPHA_NOTES = "ChatP2P public alpha invite. Keep this file private; it contains the admission token."
DEFAULT_OPERATOR_TASK_NAME = "ChatP2P Operator Watchdog"
DEFAULT_INFERENCE_PROOF_PROMPT = "ChatP2P alpha inference proof. Echo this signed work packet."
LOCAL_INVITE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
TERMINAL_JOB_STATUSES = {"verified", "disputed", "expired"}
KNOWN_WINDOWS_TOOL_PATHS = {
    "tailscale": (
        Path("C:/Program Files/Tailscale/tailscale.exe"),
        Path("C:/Program Files (x86)/Tailscale/tailscale.exe"),
    ),
}


@dataclass(frozen=True)
class AlphaInvite:
    coordinator: str
    admission_token: str
    created_at: str
    allowed_job_types: tuple[str, ...] = DEFAULT_ALLOWED_JOB_TYPES
    notes: str = DEFAULT_ALPHA_NOTES
    schema: str = ALPHA_INVITE_SCHEMA

    @classmethod
    def create(
        cls,
        *,
        coordinator: str,
        admission_token: str,
        allowed_job_types: tuple[str, ...] = DEFAULT_ALLOWED_JOB_TYPES,
        notes: str = DEFAULT_ALPHA_NOTES,
    ) -> "AlphaInvite":
        return cls(
            coordinator=coordinator.strip(),
            admission_token=admission_token.strip(),
            created_at=datetime.now(timezone.utc).isoformat(),
            allowed_job_types=tuple(allowed_job_types),
            notes=notes.strip() or DEFAULT_ALPHA_NOTES,
        ).validated()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlphaInvite":
        if data.get("schema") != ALPHA_INVITE_SCHEMA:
            raise ValueError(f"invite schema must be {ALPHA_INVITE_SCHEMA!r}")
        allowed_job_types = data.get("allowed_job_types", DEFAULT_ALLOWED_JOB_TYPES)
        if not isinstance(allowed_job_types, list | tuple) or not all(
            isinstance(job_type, str) and job_type.strip() for job_type in allowed_job_types
        ):
            raise ValueError("allowed_job_types must be a list of non-empty strings")
        invite = cls(
            schema=data["schema"],
            coordinator=_required_string(data, "coordinator"),
            admission_token=_required_string(data, "admission_token"),
            created_at=_required_string(data, "created_at"),
            allowed_job_types=tuple(str(job_type).strip() for job_type in allowed_job_types),
            notes=str(data.get("notes") or DEFAULT_ALPHA_NOTES).strip() or DEFAULT_ALPHA_NOTES,
        )
        return invite.validated()

    def validated(self) -> "AlphaInvite":
        parsed = urlparse(self.coordinator)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invite coordinator must be an http:// or https:// URL")
        OperatorConfig(public_alpha=True, admission_token=self.admission_token).validate()
        if not self.allowed_job_types or not all(self.allowed_job_types):
            raise ValueError("invite allowed_job_types cannot be empty")
        if not self.created_at:
            raise ValueError("invite created_at is required")
        return self

    def to_file_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "coordinator": self.coordinator,
            "admission_token": self.admission_token,
            "created_at": self.created_at,
            "allowed_job_types": list(self.allowed_job_types),
            "notes": self.notes,
        }

    def public_summary(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "coordinator": self.coordinator,
            "created_at": self.created_at,
            "allowed_job_types": list(self.allowed_job_types),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class AlphaJoinConfig:
    invite_path: Path
    home: Path = Path(".mesh")
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    worker_interval: float = 5.0
    startup_timeout_seconds: float = 15.0
    cpu_duration_seconds: float = 0.25
    force: bool = False


@dataclass(frozen=True)
class AlphaPreflightConfig:
    config_path: Path
    invite_path: Path
    home: Path
    report_path: Path
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class AlphaSmokeConfig:
    invite_path: Path
    report_path: Path
    jobs: int = 4
    min_live_workers: int = 1
    min_accepted_results: int = 1
    min_verified_jobs: int = 0
    timeout_seconds: float = 90.0
    poll_interval: float = 0.5


@dataclass(frozen=True)
class AlphaRemoteProofConfig:
    invite_path: Path
    report_path: Path
    jobs: int = 4
    expected_worker_id: str | None = None
    min_live_workers: int = 2
    min_accepted_results: int | None = None
    min_verified_jobs: int | None = None
    timeout_seconds: float = 180.0
    poll_interval: float = 0.5


@dataclass(frozen=True)
class AlphaInferenceProofConfig:
    invite_path: Path
    report_path: Path
    jobs: int = 10
    mode: str = "echo"
    model: str | None = None
    prompt: str = DEFAULT_INFERENCE_PROOF_PROMPT
    temperature: float | None = None
    expected_worker_id: str | None = None
    min_live_workers: int = 1
    min_accepted_results: int | None = None
    min_verified_jobs: int | None = None
    min_expected_worker_results: int | None = None
    timeout_seconds: float = 120.0
    poll_interval: float = 0.5


@dataclass(frozen=True)
class AlphaStatusConfig:
    home: Path
    invite_path: Path
    report_path: Path | None = None
    expected_worker_id: str | None = None
    min_live_workers: int = 1
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class AlphaEvidenceConfig:
    home: Path
    invite_path: Path
    out_dir: Path
    expected_worker_id: str | None = None
    jobs: int = 25
    min_live_workers: int = 2
    timeout_seconds: float = 300.0
    poll_interval: float = 0.5
    status_timeout_seconds: float = 5.0
    watchdog_report_path: Path | None = None
    operator_task_name: str | None = DEFAULT_OPERATOR_TASK_NAME
    query_operator_task: bool = True
    include_inference_proof: bool = False
    inference_mode: str = "echo"
    inference_model: str | None = None
    inference_jobs: int = 20


@dataclass(frozen=True)
class NodeWatchdogConfig:
    home: Path
    invite_path: Path
    report_path: Path | None = None
    role: str = "worker"
    restart: bool = True
    checks: int = 1
    interval_seconds: float = 30.0
    operator_config_path: Path | None = None
    coordinator_host: str = "0.0.0.0"
    coordinator_port: int | None = None
    lease_timeout_seconds: float = 30.0
    node_stale_seconds: float = 60.0
    worker_interval: float = 0.5
    startup_timeout_seconds: float = 15.0
    cpu_duration_seconds: float = 0.25
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL


@dataclass(frozen=True)
class NodeCapabilityRefreshConfig:
    home: Path
    invite_path: Path | None = None
    report_path: Path | None = None
    restart_worker: bool = False
    worker_interval: float = 0.5
    startup_timeout_seconds: float = 15.0
    cpu_duration_seconds: float = 0.25
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL


@dataclass(frozen=True)
class AlphaDrillConfig:
    home: Path
    invite_path: Path
    report_path: Path
    config_path: Path | None = None
    simulated_workers: int = 1
    jobs: int = 4
    worker_interval: float = 0.5
    startup_timeout_seconds: float = 15.0
    timeout_seconds: float = 90.0
    poll_interval: float = 0.5
    cpu_duration_seconds: float = 0.25
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    start_coordinator: bool = True
    coordinator_host: str = "0.0.0.0"
    coordinator_port: int | None = None
    lease_timeout_seconds: float = 30.0
    node_stale_seconds: float = 60.0
    start_primary_worker: bool = True
    force_workers: bool = False
    keep_simulated_workers: bool = True
    run_preflight: bool = True


@dataclass(frozen=True)
class AlphaRouteConfig:
    invite_path: Path
    report_path: Path
    home: Path | None = None
    candidate_url: str | None = None
    timeout_seconds: float = 5.0
    detect_tools: bool = True


def generate_admission_token() -> str:
    return secrets.token_urlsafe(32)


def load_alpha_invite(path: Path) -> AlphaInvite:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"invite file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invite file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("invite file must contain a JSON object")
    return AlphaInvite.from_dict(data)


def write_alpha_invite(path: Path, invite: AlphaInvite) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(invite.to_file_dict(), indent=2, sort_keys=True), encoding="utf-8")


def bootstrap_alpha(
    *,
    config_path: Path,
    invite_path: Path,
    coordinator_url: str,
    admission_token: str | None = None,
    max_request_bytes: int = 256 * 1024,
    max_job_payload_bytes: int = 16 * 1024,
    allowed_job_types: tuple[str, ...] = DEFAULT_ALLOWED_JOB_TYPES,
    notes: str = DEFAULT_ALPHA_NOTES,
    force: bool = False,
) -> dict[str, Any]:
    token = admission_token.strip() if admission_token else generate_admission_token()
    operator_config = OperatorConfig(
        public_alpha=True,
        admission_token=token,
        max_request_bytes=max_request_bytes,
        max_job_payload_bytes=max_job_payload_bytes,
        allowed_job_types=tuple(allowed_job_types),
    )
    operator_config.validate()
    invite = AlphaInvite.create(
        coordinator=coordinator_url,
        admission_token=token,
        allowed_job_types=operator_config.allowed_job_types,
        notes=notes,
    )

    if config_path.exists() and not force:
        raise FileExistsError(f"Operator config already exists at {config_path}. Use --force to replace it.")
    if invite_path.exists() and not force:
        raise FileExistsError(f"Alpha invite already exists at {invite_path}. Use --force to replace it.")

    write_operator_config(config_path, operator_config)
    write_alpha_invite(invite_path, invite)
    return {
        "config": str(config_path),
        "invite": str(invite_path),
        "coordinator": invite.coordinator,
        "operator": operator_config.public_summary(),
        "invite_summary": invite.public_summary(),
    }


def run_alpha_preflight(config: AlphaPreflightConfig) -> dict[str, Any]:
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")

    checks: list[dict[str, Any]] = []
    operator_config = _load_operator_config_for_report(config.config_path, checks)
    invite = _load_invite_for_report(config.invite_path, checks)

    if operator_config is not None:
        checks.append(
            _alpha_check(
                "operator_public_alpha",
                "pass" if operator_config.public_alpha else "fail",
                "Operator config enables public alpha."
                if operator_config.public_alpha
                else "Operator config does not enable public alpha.",
                details={"public_alpha": operator_config.public_alpha},
            )
        )
        checks.append(
            _alpha_check(
                "operator_token_present",
                "pass" if operator_config.admission_token else "fail",
                "Operator config has an admission token."
                if operator_config.admission_token
                else "Operator config is missing an admission token.",
                details={"token_present": operator_config.admission_token is not None},
            )
        )

    if operator_config is not None and invite is not None:
        token_matches = secrets.compare_digest(
            operator_config.admission_token or "",
            invite.admission_token,
        )
        checks.append(
            _alpha_check(
                "invite_token_matches_config",
                "pass" if token_matches else "fail",
                "Invite admission token matches the operator config."
                if token_matches
                else "Invite admission token does not match the operator config.",
                details={
                    "config_token_present": operator_config.admission_token is not None,
                    "invite_token_present": bool(invite.admission_token),
                    "token_matches": token_matches,
                },
            )
        )

        allowed_matches = operator_config.allowed_job_types == invite.allowed_job_types
        checks.append(
            _alpha_check(
                "invite_allowed_job_types_match_config",
                "pass" if allowed_matches else "fail",
                "Invite allowed job types match the operator config."
                if allowed_matches
                else "Invite allowed job types do not match the operator config.",
                details={
                    "config_allowed_job_types": list(operator_config.allowed_job_types),
                    "invite_allowed_job_types": list(invite.allowed_job_types),
                },
            )
        )

        checks.append(_invite_url_check(invite))
        health = _coordinator_health(invite, timeout_seconds=config.timeout_seconds)
    elif invite is not None:
        checks.append(_invite_url_check(invite))
        health = _coordinator_health(invite, timeout_seconds=config.timeout_seconds)
    else:
        health = {"ok": False, "url": None, "error": "invite could not be loaded"}

    checks.append(_coordinator_health_check(health))
    checks.extend(_coordinator_operator_checks(health, operator_config))

    managed_processes = _managed_processes_for_report(config.home)
    checks.append(_managed_coordinator_check(managed_processes))

    report = _alpha_report(
        schema=ALPHA_PREFLIGHT_REPORT_SCHEMA,
        config={
            "config_path": str(config.config_path),
            "invite_path": str(config.invite_path),
            "home": str(config.home.expanduser().resolve()),
            "timeout_seconds": config.timeout_seconds,
        },
        checks=checks,
        details={
            "operator": operator_config.public_summary() if operator_config else None,
            "invite": invite.public_summary() if invite else None,
            "token": {
                "config_token_present": operator_config.admission_token is not None
                if operator_config
                else False,
                "invite_token_present": bool(invite.admission_token) if invite else False,
                "token_matches": (
                    secrets.compare_digest(operator_config.admission_token or "", invite.admission_token)
                    if operator_config and invite
                    else False
                ),
            },
            "coordinator_health": health,
            "managed_processes": managed_processes,
        },
    )
    _write_json_report(config.report_path, report)
    return report


def run_alpha_smoke(config: AlphaSmokeConfig) -> dict[str, Any]:
    _validate_smoke_config(config)
    invite = load_alpha_invite(config.invite_path)
    started_at = time.time()
    client = CoordinatorClient(invite.coordinator, admission_token=invite.admission_token)
    errors: list[str] = []
    created_jobs: list[dict[str, Any]] = []
    final_snapshot: dict[str, Any] | None = None
    criteria: dict[str, Any] | None = None

    try:
        initial_snapshot = client.snapshot()
    except Exception as exc:
        report = _smoke_report(
            config=config,
            invite=invite,
            duration_seconds=time.time() - started_at,
            created_jobs=[],
            criteria={
                "live_workers": {"actual": 0, "required": config.min_live_workers, "passed": False},
                "accepted_results": {"actual": 0, "required": config.min_accepted_results, "passed": False},
                "verified_jobs": {"actual": 0, "required": config.min_verified_jobs, "passed": False},
                "disputed_jobs": {"actual": 0, "required": 0, "passed": False},
            },
            final_snapshot=None,
            errors=[f"{type(exc).__name__}: {exc}"],
        )
        _write_json_report(config.report_path, report)
        return report

    initial_result_ids = _result_ids(initial_snapshot)
    try:
        for index in range(config.jobs):
            job = client.create_job(
                job_type="eval.deterministic.v1",
                payload=_smoke_payload(index),
                ttl_seconds=300,
            )
            created_jobs.append(
                {
                    "job_id": job.job_id,
                    "job_type": job.job_type,
                    "verification_strategy": job.verification_strategy,
                }
            )
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    created_job_ids = {job["job_id"] for job in created_jobs}
    deadline = started_at + config.timeout_seconds
    while time.time() <= deadline and created_jobs and not errors:
        try:
            final_snapshot = client.snapshot()
            criteria = _smoke_criteria(final_snapshot, created_job_ids, initial_result_ids, config)
            if all(item["passed"] for item in criteria.values()):
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
        criteria = _smoke_criteria(final_snapshot, created_job_ids, initial_result_ids, config)
    elif criteria is None:
        criteria = {
            "live_workers": {"actual": 0, "required": config.min_live_workers, "passed": False},
            "accepted_results": {"actual": 0, "required": config.min_accepted_results, "passed": False},
            "verified_jobs": {"actual": 0, "required": config.min_verified_jobs, "passed": False},
            "disputed_jobs": {"actual": 0, "required": 0, "passed": False},
        }

    if created_jobs and not errors and not all(item["passed"] for item in criteria.values()):
        errors.append("smoke proof thresholds were not met before timeout")

    report = _smoke_report(
        config=config,
        invite=invite,
        duration_seconds=time.time() - started_at,
        created_jobs=created_jobs,
        criteria=criteria,
        final_snapshot=final_snapshot,
        errors=errors,
    )
    _write_json_report(config.report_path, report)
    return report


def run_alpha_remote_proof(config: AlphaRemoteProofConfig) -> dict[str, Any]:
    _validate_remote_proof_config(config)
    invite = load_alpha_invite(config.invite_path)
    started_at = time.time()
    client = CoordinatorClient(invite.coordinator, admission_token=invite.admission_token)
    errors: list[str] = []
    created_jobs: list[dict[str, Any]] = []
    initial_snapshot: dict[str, Any] | None = None
    final_snapshot: dict[str, Any] | None = None
    criteria: dict[str, Any] | None = None
    initial_result_ids: set[tuple[str, str, str]] = set()

    try:
        initial_snapshot = client.snapshot()
        initial_result_ids = _result_ids(initial_snapshot)
    except Exception as exc:
        criteria = _remote_proof_empty_criteria(config)
        report = _remote_proof_report(
            config=config,
            invite=invite,
            duration_seconds=time.time() - started_at,
            initial_snapshot=None,
            final_snapshot=None,
            created_jobs=[],
            criteria=criteria,
            errors=[f"{type(exc).__name__}: {exc}"],
            initial_result_ids=initial_result_ids,
        )
        _write_json_report(config.report_path, report)
        return report

    try:
        for index in range(config.jobs):
            job = client.create_job(
                job_type="eval.deterministic.v1",
                payload=_smoke_payload(index),
                ttl_seconds=300,
            )
            created_jobs.append(
                {
                    "job_id": job.job_id,
                    "job_type": job.job_type,
                    "verification_strategy": job.verification_strategy,
                }
            )
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    created_job_ids = {job["job_id"] for job in created_jobs}
    deadline = started_at + config.timeout_seconds
    while time.time() <= deadline and created_jobs and not errors:
        try:
            final_snapshot = client.snapshot()
            criteria = _remote_proof_criteria(final_snapshot, created_job_ids, initial_result_ids, config)
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
        criteria = _remote_proof_criteria(final_snapshot, created_job_ids, initial_result_ids, config)
    elif criteria is None:
        criteria = _remote_proof_empty_criteria(config)

    if created_jobs and not errors and not all(item["passed"] for item in criteria.values()):
        errors.append("remote proof criteria were not met before timeout")
    if len(created_jobs) != config.jobs and not errors:
        errors.append(f"created {len(created_jobs)} of {config.jobs} requested jobs")

    report = _remote_proof_report(
        config=config,
        invite=invite,
        duration_seconds=time.time() - started_at,
        initial_snapshot=initial_snapshot,
        final_snapshot=final_snapshot,
        created_jobs=created_jobs,
        criteria=criteria,
        errors=errors,
        initial_result_ids=initial_result_ids,
    )
    _write_json_report(config.report_path, report)
    return report


def run_alpha_inference_proof(config: AlphaInferenceProofConfig) -> dict[str, Any]:
    _validate_inference_proof_config(config)
    invite = load_alpha_invite(config.invite_path)
    started_at = time.time()
    client = CoordinatorClient(invite.coordinator, admission_token=invite.admission_token)
    errors: list[str] = []
    created_jobs: list[dict[str, Any]] = []
    initial_snapshot: dict[str, Any] | None = None
    final_snapshot: dict[str, Any] | None = None
    criteria: dict[str, Any] | None = None
    initial_result_ids: set[tuple[str, str, str]] = set()
    selected_job_type = "inference.echo.v1"
    mode_decision: dict[str, Any] = {}

    try:
        initial_snapshot = client.snapshot()
        initial_result_ids = _result_ids(initial_snapshot)
        mode_decision = _inference_mode_decision(config, initial_snapshot)
        selected_job_type = mode_decision["job_type"]
    except Exception as exc:
        report = _inference_proof_report(
            config=config,
            invite=invite,
            duration_seconds=time.time() - started_at,
            selected_job_type=selected_job_type,
            mode_decision=mode_decision,
            initial_snapshot=None,
            final_snapshot=None,
            created_jobs=[],
            criteria=_inference_proof_empty_criteria(config),
            errors=[f"{type(exc).__name__}: {exc}"],
            initial_result_ids=initial_result_ids,
        )
        _write_json_report(config.report_path, report)
        return report

    if mode_decision.get("status") == "fail":
        errors.append(mode_decision["message"])

    try:
        for index in range(config.jobs if not errors else 0):
            job = client.create_job(
                job_type=selected_job_type,
                payload=_inference_proof_payload(config, selected_job_type, index),
                ttl_seconds=300,
            )
            created_jobs.append(
                {
                    "job_id": job.job_id,
                    "job_type": job.job_type,
                    "model_id": job.model_id,
                    "verification_strategy": job.verification_strategy,
                }
            )
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    created_job_ids = {job["job_id"] for job in created_jobs}
    deadline = started_at + config.timeout_seconds
    while time.time() <= deadline and created_jobs and not errors:
        try:
            final_snapshot = client.snapshot()
            criteria = _inference_proof_criteria(
                final_snapshot,
                created_job_ids,
                initial_result_ids,
                config,
            )
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

    if final_snapshot is None:
        try:
            final_snapshot = client.snapshot()
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    if final_snapshot is not None:
        criteria = _inference_proof_criteria(
            final_snapshot,
            created_job_ids,
            initial_result_ids,
            config,
        )
    elif criteria is None:
        criteria = _inference_proof_empty_criteria(config)

    if created_jobs and not errors and not all(item["passed"] for item in criteria.values()):
        errors.append("inference proof criteria were not met before timeout")
    if len(created_jobs) != config.jobs and not errors:
        errors.append(f"created {len(created_jobs)} of {config.jobs} requested jobs")

    report = _inference_proof_report(
        config=config,
        invite=invite,
        duration_seconds=time.time() - started_at,
        selected_job_type=selected_job_type,
        mode_decision=mode_decision,
        initial_snapshot=initial_snapshot,
        final_snapshot=final_snapshot,
        created_jobs=created_jobs,
        criteria=criteria,
        errors=errors,
        initial_result_ids=initial_result_ids,
    )
    _write_json_report(config.report_path, report)
    return report


def run_alpha_drill(config: AlphaDrillConfig) -> dict[str, Any]:
    _validate_drill_config(config)
    started_at = time.time()
    home = config.home.expanduser().resolve()
    invite = load_alpha_invite(config.invite_path)
    errors: list[str] = []
    cleanup: list[dict[str, Any]] = []

    coordinator_before = managed_process_status(home=home, role="coordinator")
    initial_health = _coordinator_health(invite, timeout_seconds=min(config.startup_timeout_seconds, 5.0))
    coordinator_start = None
    final_health = initial_health

    if not initial_health.get("ok") and config.start_coordinator:
        if config.config_path is None:
            errors.append("coordinator is unreachable and no operator config was provided to start it")
        else:
            coordinator_start = _start_drill_coordinator(config=config, home=home, invite=invite)
            final_health = _wait_for_invite_health(
                invite=invite,
                timeout_seconds=config.startup_timeout_seconds,
                poll_interval=0.2,
            )
            if not final_health.get("ok"):
                errors.append("coordinator did not become reachable before startup timeout")

    coordinator_after_start = managed_process_status(home=home, role="coordinator")
    preflight = None
    if config.run_preflight and config.config_path is not None and final_health.get("ok"):
        preflight_path = _sidecar_report_path(config.report_path, "preflight")
        preflight = run_alpha_preflight(
            AlphaPreflightConfig(
                config_path=config.config_path,
                invite_path=config.invite_path,
                home=home,
                report_path=preflight_path,
                timeout_seconds=min(config.startup_timeout_seconds, 5.0),
            )
        )
        if not preflight["ok"]:
            errors.append("alpha preflight did not pass")

    primary_join = None
    if config.start_primary_worker and final_health.get("ok"):
        primary_join = run_alpha_join(
            AlphaJoinConfig(
                invite_path=config.invite_path,
                home=home,
                ollama_base_url=config.ollama_base_url,
                worker_interval=config.worker_interval,
                startup_timeout_seconds=config.startup_timeout_seconds,
                cpu_duration_seconds=config.cpu_duration_seconds,
                force=config.force_workers,
            )
        )
        if not primary_join["ok"]:
            errors.append("primary worker did not join successfully")

    simulated: list[dict[str, Any]] = []
    if final_health.get("ok"):
        for index in range(1, config.simulated_workers + 1):
            worker_home = _drill_simulated_worker_home(home, index)
            join_report = run_alpha_join(
                AlphaJoinConfig(
                    invite_path=config.invite_path,
                    home=worker_home,
                    ollama_base_url=config.ollama_base_url,
                    worker_interval=config.worker_interval,
                    startup_timeout_seconds=config.startup_timeout_seconds,
                    cpu_duration_seconds=config.cpu_duration_seconds,
                    force=True,
                )
            )
            simulated.append({"index": index, "home": str(worker_home), "join": join_report})
            if not join_report["ok"]:
                errors.append(f"simulated worker {index} did not join successfully")

    smoke = None
    if final_health.get("ok"):
        required_workers = _drill_required_workers(config)
        quorum_mode = required_workers >= 2
        smoke_path = _sidecar_report_path(config.report_path, "smoke")
        smoke = run_alpha_smoke(
            AlphaSmokeConfig(
                invite_path=config.invite_path,
                report_path=smoke_path,
                jobs=config.jobs,
                min_live_workers=required_workers,
                min_accepted_results=config.jobs * (2 if quorum_mode else 1),
                min_verified_jobs=config.jobs if quorum_mode else 0,
                timeout_seconds=config.timeout_seconds,
                poll_interval=config.poll_interval,
            )
        )
        if not smoke["ok"]:
            errors.append("alpha smoke proof did not pass")

    if not config.keep_simulated_workers:
        for index in range(1, config.simulated_workers + 1):
            worker_home = _drill_simulated_worker_home(home, index)
            cleanup.append(
                {
                    "index": index,
                    "home": str(worker_home),
                    "stop": stop_managed_process(home=worker_home, role="worker"),
                }
            )

    coordinator_after = managed_process_status(home=home, role="coordinator")
    primary_worker_status = managed_process_status(home=home, role="worker")
    simulated_statuses = [
        {
            "index": index,
            "home": str(_drill_simulated_worker_home(home, index)),
            "worker": managed_process_status(home=_drill_simulated_worker_home(home, index), role="worker"),
        }
        for index in range(1, config.simulated_workers + 1)
    ]
    ok = (
        not errors
        and final_health.get("ok", False)
        and (smoke is not None and smoke.get("ok", False))
        and (preflight is None or preflight.get("ok", False))
        and (primary_join is None or primary_join.get("ok", False))
        and all(item["join"].get("ok", False) for item in simulated)
    )
    report = {
        "schema": ALPHA_DRILL_REPORT_SCHEMA,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "home": str(home),
            "invite_path": str(config.invite_path),
            "config_path": str(config.config_path) if config.config_path else None,
            "report_path": str(config.report_path),
            "simulated_workers": config.simulated_workers,
            "jobs": config.jobs,
            "worker_interval": config.worker_interval,
            "startup_timeout_seconds": config.startup_timeout_seconds,
            "timeout_seconds": config.timeout_seconds,
            "poll_interval": config.poll_interval,
            "start_coordinator": config.start_coordinator,
            "coordinator_host": config.coordinator_host,
            "coordinator_port": _drill_coordinator_port(config, invite),
            "start_primary_worker": config.start_primary_worker,
            "force_workers": config.force_workers,
            "keep_simulated_workers": config.keep_simulated_workers,
            "run_preflight": config.run_preflight,
        },
        "invite": invite.public_summary(),
        "coordinator": {
            "before": coordinator_before,
            "initial_health": initial_health,
            "start": coordinator_start,
            "after_start": coordinator_after_start,
            "final_health": final_health,
            "after": coordinator_after,
        },
        "preflight": preflight,
        "primary_worker": {
            "join": primary_join,
            "status": primary_worker_status,
        },
        "simulated_workers": simulated,
        "simulated_worker_statuses": simulated_statuses,
        "smoke": smoke,
        "cleanup": cleanup,
        "errors": errors,
    }
    _write_json_report(config.report_path, report)
    return report


def run_alpha_route(config: AlphaRouteConfig) -> dict[str, Any]:
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")

    invite = load_alpha_invite(config.invite_path)
    tools = _remote_route_tooling() if config.detect_tools else None
    current_route = _route_probe(
        invite=invite,
        coordinator_url=invite.coordinator,
        timeout_seconds=config.timeout_seconds,
        tools=tools,
    )
    candidate_route = (
        _route_probe(
            invite=invite,
            coordinator_url=config.candidate_url,
            timeout_seconds=config.timeout_seconds,
            tools=tools,
        )
        if config.candidate_url
        else None
    )
    managed_processes = (
        _managed_processes_for_report(config.home)
        if config.home is not None
        else None
    )
    recommendations = _route_recommendations(
        current_route=current_route,
        candidate_route=candidate_route,
        tools=tools,
    )
    errors: list[str] = []
    if not current_route["health"].get("ok"):
        errors.append("current invite coordinator health is not reachable from this machine")
    if candidate_route is not None and not candidate_route["health"].get("ok"):
        errors.append("candidate coordinator health is not reachable from this machine")

    status = "fail" if errors else ("warn" if recommendations else "pass")
    report = {
        "schema": ALPHA_ROUTE_REPORT_SCHEMA,
        "ok": status != "fail",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "invite_path": str(config.invite_path),
            "report_path": str(config.report_path),
            "home": str(config.home.expanduser().resolve()) if config.home else None,
            "candidate_url": config.candidate_url,
            "timeout_seconds": config.timeout_seconds,
            "detect_tools": config.detect_tools,
        },
        "invite": invite.public_summary(),
        "current_route": current_route,
        "candidate_route": candidate_route,
        "tooling": tools,
        "managed_processes": managed_processes,
        "recommendations": recommendations,
        "errors": errors,
    }
    _write_json_report(config.report_path, report)
    return report


def run_alpha_status(config: AlphaStatusConfig) -> dict[str, Any]:
    _validate_alpha_status_config(config)
    invite = load_alpha_invite(config.invite_path)
    health = _coordinator_health(invite, timeout_seconds=config.timeout_seconds)
    snapshot = _snapshot_for_status(invite, timeout_seconds=config.timeout_seconds) if health.get("ok") else None
    processes = _managed_processes_for_report(config.home)
    checks = _alpha_status_checks(
        health=health,
        snapshot=snapshot,
        processes=processes,
        config=config,
    )
    report = _alpha_report(
        schema=ALPHA_STATUS_REPORT_SCHEMA,
        config={
            "home": str(config.home.expanduser().resolve()),
            "invite_path": str(config.invite_path),
            "report_path": str(config.report_path) if config.report_path else None,
            "expected_worker_id": config.expected_worker_id,
            "min_live_workers": config.min_live_workers,
            "timeout_seconds": config.timeout_seconds,
        },
        checks=checks,
        details={
            "invite": invite.public_summary(),
            "coordinator_health": health,
            "managed_processes": processes,
            "snapshot_summary": _snapshot_baseline(snapshot),
            "nodes": _status_node_summaries(snapshot),
            "expected_worker": _status_expected_worker_summary(snapshot, config.expected_worker_id),
        },
    )
    if config.report_path is not None:
        _write_json_report(config.report_path, report)
    return report


def run_alpha_evidence(config: AlphaEvidenceConfig) -> dict[str, Any]:
    _validate_alpha_evidence_config(config)
    started_at = time.time()
    home = config.home.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    invite = load_alpha_invite(config.invite_path)
    secrets_to_redact = tuple(value for value in (invite.admission_token,) if value)
    status_path = out_dir / "alpha-status.json"
    remote_proof_path = out_dir / "alpha-remote-proof.json"
    inference_proof_path = out_dir / "alpha-evidence-inference-proof.json"
    task_status_path = out_dir / "operator-watchdog-task.json"
    summary_path = out_dir / "alpha-evidence-summary.json"
    markdown_path = out_dir / "alpha-evidence-summary.md"
    watchdog_source = (
        config.watchdog_report_path.expanduser().resolve()
        if config.watchdog_report_path is not None
        else home.parent / "node-watchdog-report.json"
    )
    watchdog_copy_path = out_dir / watchdog_source.name

    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    inference_proof_report = None

    try:
        status_report = run_alpha_status(
            AlphaStatusConfig(
                home=home,
                invite_path=config.invite_path,
                report_path=status_path,
                expected_worker_id=config.expected_worker_id,
                min_live_workers=config.min_live_workers,
                timeout_seconds=config.status_timeout_seconds,
            )
        )
        _redact_artifact_file(status_path, secrets_to_redact)
        checks.append(
            _alpha_check(
                "alpha_status",
                "pass" if status_report.get("ok") else "fail",
                "Alpha status passed." if status_report.get("ok") else "Alpha status failed.",
                details=_evidence_status_summary(status_report),
            )
        )
    except Exception as exc:
        status_report = None
        message = f"{type(exc).__name__}: {exc}"
        errors.append(message)
        checks.append(_alpha_check("alpha_status", "fail", "Alpha status could not be collected.", details={"error": message}))

    try:
        remote_proof_report = run_alpha_remote_proof(
            AlphaRemoteProofConfig(
                invite_path=config.invite_path,
                report_path=remote_proof_path,
                jobs=config.jobs,
                expected_worker_id=config.expected_worker_id,
                min_live_workers=config.min_live_workers,
                timeout_seconds=config.timeout_seconds,
                poll_interval=config.poll_interval,
            )
        )
        _redact_artifact_file(remote_proof_path, secrets_to_redact)
        checks.append(
            _alpha_check(
                "remote_proof",
                "pass" if remote_proof_report.get("ok") else "fail",
                "Remote proof passed." if remote_proof_report.get("ok") else "Remote proof failed.",
                details=_evidence_remote_proof_summary(remote_proof_report),
            )
        )
    except Exception as exc:
        remote_proof_report = None
        message = f"{type(exc).__name__}: {exc}"
        errors.append(message)
        checks.append(_alpha_check("remote_proof", "fail", "Remote proof could not be collected.", details={"error": message}))

    if config.include_inference_proof:
        try:
            inference_proof_report = run_alpha_inference_proof(
                AlphaInferenceProofConfig(
                    invite_path=config.invite_path,
                    report_path=inference_proof_path,
                    jobs=config.inference_jobs,
                    mode=config.inference_mode,
                    model=config.inference_model,
                    expected_worker_id=config.expected_worker_id,
                    min_live_workers=config.min_live_workers,
                    min_accepted_results=config.inference_jobs,
                    min_verified_jobs=config.inference_jobs,
                    timeout_seconds=config.timeout_seconds,
                    poll_interval=config.poll_interval,
                )
            )
            _redact_artifact_file(inference_proof_path, secrets_to_redact)
            checks.append(
                _alpha_check(
                    "inference_proof",
                    "pass" if inference_proof_report.get("ok") else "fail",
                    "Inference proof passed."
                    if inference_proof_report.get("ok")
                    else "Inference proof failed.",
                    details=_evidence_inference_proof_summary(inference_proof_report),
                )
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            errors.append(message)
            checks.append(
                _alpha_check(
                    "inference_proof",
                    "fail",
                    "Inference proof could not be collected.",
                    details={"error": message},
                )
            )

    watchdog_artifact = _copy_redacted_artifact(
        source=watchdog_source,
        destination=watchdog_copy_path,
        secrets_to_redact=secrets_to_redact,
    )
    checks.append(
        _alpha_check(
            "watchdog_report",
            "pass" if watchdog_artifact.get("ok") else "warn",
            "Watchdog report was copied into the evidence pack."
            if watchdog_artifact.get("ok")
            else "Watchdog report was not available for the evidence pack.",
            details=watchdog_artifact,
        )
    )

    task_query = _query_windows_task_for_evidence(config.operator_task_name, enabled=config.query_operator_task)
    _write_json_report(task_status_path, _redact_evidence_value(task_query, secrets_to_redact))
    task_check_status = "pass" if task_query.get("ok") else "warn"
    checks.append(
        _alpha_check(
            "operator_watchdog_task",
            task_check_status,
            "Operator watchdog Scheduled Task was found."
            if task_query.get("ok")
            else "Operator watchdog Scheduled Task could not be confirmed.",
            details=task_query,
        )
    )

    artifacts = {
        "directory": str(out_dir),
        "summary_json": str(summary_path),
        "summary_markdown": str(markdown_path),
        "alpha_status": str(status_path),
        "alpha_remote_proof": str(remote_proof_path),
        "watchdog_report": watchdog_artifact,
        "operator_watchdog_task": str(task_status_path),
    }
    if config.include_inference_proof:
        artifacts["alpha_inference_proof"] = str(inference_proof_path)
    secret_scan_paths = [status_path, remote_proof_path, task_status_path, watchdog_copy_path]
    if config.include_inference_proof:
        secret_scan_paths.append(inference_proof_path)
    secret_scan = _scan_artifacts_for_secrets(
        secret_scan_paths,
        secrets_to_redact,
    )
    checks.append(
        _alpha_check(
            "token_redaction",
            "pass" if not secret_scan["leaks"] else "fail",
            "No raw admission token was found in evidence artifacts."
            if not secret_scan["leaks"]
            else "A raw admission token was found in evidence artifacts.",
            details=secret_scan,
        )
    )

    counts = _check_counts(checks)
    ok = counts["fail"] == 0
    status = "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass")
    report = {
        "schema": ALPHA_EVIDENCE_PACK_SCHEMA,
        "ok": ok,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "counts": counts,
        "config": {
            "home": str(home),
            "invite_path": str(config.invite_path),
            "out_dir": str(out_dir),
            "expected_worker_id": config.expected_worker_id,
            "jobs": config.jobs,
            "min_live_workers": config.min_live_workers,
            "timeout_seconds": config.timeout_seconds,
            "poll_interval": config.poll_interval,
            "status_timeout_seconds": config.status_timeout_seconds,
            "watchdog_report_path": str(watchdog_source),
            "operator_task_name": config.operator_task_name,
            "query_operator_task": config.query_operator_task,
            "include_inference_proof": config.include_inference_proof,
            "inference_mode": config.inference_mode,
            "inference_model": config.inference_model,
            "inference_jobs": config.inference_jobs,
        },
        "invite": invite.public_summary(),
        "checks": checks,
        "artifacts": artifacts,
        "alpha_status": _evidence_status_summary(status_report),
        "remote_proof": _evidence_remote_proof_summary(remote_proof_report),
        "inference_proof": _evidence_inference_proof_summary(inference_proof_report),
        "operator_watchdog_task": task_query,
        "errors": errors,
    }
    report = _redact_evidence_value(report, secrets_to_redact)
    _write_json_report(summary_path, report)
    _write_text_report(markdown_path, _alpha_evidence_markdown(report))
    return report


def run_node_watchdog(config: NodeWatchdogConfig) -> dict[str, Any]:
    _validate_node_watchdog_config(config)
    invite = load_alpha_invite(config.invite_path)
    home = config.home.expanduser().resolve()
    iterations: list[dict[str, Any]] = []
    errors: list[str] = []
    index = 0

    while config.checks == 0 or index < config.checks:
        iteration = _run_watchdog_iteration(
            config=config,
            invite=invite,
            home=home,
            index=index + 1,
        )
        iterations.append(iteration)
        errors.extend(iteration.get("errors", []))
        index += 1
        report = _node_watchdog_report(
            config=config,
            invite=invite,
            home=home,
            iterations=iterations,
            errors=errors,
        )
        if config.report_path is not None:
            _write_json_report(config.report_path, report)
        if config.checks != 0 and index >= config.checks:
            return report
        time.sleep(config.interval_seconds)

    return _node_watchdog_report(
        config=config,
        invite=invite,
        home=home,
        iterations=iterations,
        errors=errors,
    )


def refresh_node_capabilities(config: NodeCapabilityRefreshConfig) -> dict[str, Any]:
    _validate_capability_refresh_config(config)
    started_at = time.time()
    home = config.home.expanduser().resolve()
    profile_path = home / CAPABILITY_PROFILE_NAME
    previous_capabilities = load_node_capabilities(home)
    worker_before = managed_process_status(home=home, role="worker")

    benchmark_report = run_node_benchmark(
        cpu_duration_seconds=config.cpu_duration_seconds,
        ollama_base_url=config.ollama_base_url,
    )
    save_node_benchmark(benchmark_report, profile_path)
    current_capabilities = benchmark_report["capabilities"]

    restart_report = None
    warnings: list[str] = []
    if config.restart_worker:
        assert config.invite_path is not None
        restart_report = run_alpha_join(
            AlphaJoinConfig(
                invite_path=config.invite_path,
                home=home,
                ollama_base_url=config.ollama_base_url,
                worker_interval=config.worker_interval,
                startup_timeout_seconds=config.startup_timeout_seconds,
                cpu_duration_seconds=config.cpu_duration_seconds,
                force=True,
            )
        )
        if restart_report.get("ok"):
            invite = load_alpha_invite(config.invite_path)
            advertisement_report = _wait_for_worker_capability_advertisement(
                invite=invite,
                node_id=restart_report["worker_node_id"],
                expected_capabilities=current_capabilities,
                timeout_seconds=config.startup_timeout_seconds,
            )
        else:
            advertisement_report = None
    elif worker_before.get("alive"):
        advertisement_report = None
        warnings.append("managed worker is running; restart it to advertise refreshed capabilities")
    else:
        advertisement_report = None

    worker_after = managed_process_status(home=home, role="worker")
    ok = restart_report.get("ok", False) if restart_report is not None else True
    if advertisement_report is not None:
        ok = ok and bool(advertisement_report.get("ok"))
    status = "fail" if not ok else ("warn" if warnings else "pass")
    report = {
        "schema": NODE_CAPABILITY_REFRESH_REPORT_SCHEMA,
        "ok": ok,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "home": str(home),
        "profile_path": str(profile_path),
        "config": {
            "invite_path": str(config.invite_path) if config.invite_path else None,
            "report_path": str(config.report_path) if config.report_path else None,
            "restart_worker": config.restart_worker,
            "worker_interval": config.worker_interval,
            "startup_timeout_seconds": config.startup_timeout_seconds,
            "cpu_duration_seconds": config.cpu_duration_seconds,
            "ollama_base_url": config.ollama_base_url,
        },
        "previous_capabilities": _capability_refresh_summary(previous_capabilities),
        "current_capabilities": _capability_refresh_summary(current_capabilities),
        "changes": _capability_refresh_changes(previous_capabilities, current_capabilities),
        "managed_worker_before": worker_before,
        "managed_worker_after": worker_after,
        "restart": restart_report,
        "advertisement": advertisement_report,
        "warnings": warnings,
    }
    if config.report_path is not None:
        _write_json_report(config.report_path, report)
    return report


def run_alpha_join(config: AlphaJoinConfig) -> dict[str, Any]:
    if config.worker_interval <= 0:
        raise ValueError("--worker-interval must be greater than 0")
    if config.startup_timeout_seconds <= 0:
        raise ValueError("--startup-timeout-seconds must be greater than 0")

    invite = load_alpha_invite(config.invite_path)
    home = config.home.expanduser().resolve()
    identity = _load_or_create_worker_identity(home)
    benchmark_report = _ensure_benchmark_profile(config)
    health = _coordinator_health(invite, timeout_seconds=config.startup_timeout_seconds)

    if not health["ok"]:
        return {
            "ok": False,
            "status": "coordinator_unreachable",
            "home": str(home),
            "worker_node_id": identity.node_id,
            "invite": invite.public_summary(),
            "benchmark": benchmark_report,
            "coordinator_health": health,
            "start": None,
            "registration": None,
        }

    start_report = start_managed_process(
        home=home,
        role="worker",
        argv=_worker_loop_argv(config, invite),
        coordinator_url=invite.coordinator,
        force=config.force,
        extra_state={
            "joined_from_invite": str(config.invite_path),
            "worker_interval": config.worker_interval,
            "invite_schema": invite.schema,
        },
    )
    registration = _wait_for_worker_live(
        home=home,
        invite=invite,
        node_id=identity.node_id,
        timeout_seconds=config.startup_timeout_seconds,
    )
    if not registration["ok"] and start_report.get("status") == "started":
        stop_report = stop_managed_process(home=home, role="worker")
        registration["cleanup"] = stop_report

    return {
        "ok": registration["ok"],
        "status": "joined" if registration["ok"] else "needs_attention",
        "home": str(home),
        "worker_node_id": identity.node_id,
        "invite": invite.public_summary(),
        "benchmark": benchmark_report,
        "coordinator_health": health,
        "start": start_report,
        "registration": registration,
    }


def _load_or_create_worker_identity(home: Path) -> NodeIdentity:
    path = home / "worker.identity.json"
    if path.exists():
        return NodeIdentity.load(path)
    identity = NodeIdentity.generate(prefix="worker")
    identity.save(path)
    return identity


def _load_operator_config_for_report(path: Path, checks: list[dict[str, Any]]) -> OperatorConfig | None:
    try:
        operator_config = OperatorConfig.from_file(path)
    except Exception as exc:
        checks.append(
            _alpha_check(
                "operator_config",
                "fail",
                f"Operator config could not be loaded: {type(exc).__name__}: {exc}",
                details={"path": str(path)},
            )
        )
        return None

    checks.append(
        _alpha_check(
            "operator_config",
            "pass",
            "Operator config loaded and validated.",
            details={"path": str(path), "operator": operator_config.public_summary()},
        )
    )
    return operator_config


def _load_invite_for_report(path: Path, checks: list[dict[str, Any]]) -> AlphaInvite | None:
    try:
        invite = load_alpha_invite(path)
    except Exception as exc:
        checks.append(
            _alpha_check(
                "alpha_invite",
                "fail",
                f"Alpha invite could not be loaded: {type(exc).__name__}: {exc}",
                details={"path": str(path)},
            )
        )
        return None

    checks.append(
        _alpha_check(
            "alpha_invite",
            "pass",
            "Alpha invite loaded and validated.",
            details={"path": str(path), "invite": invite.public_summary()},
        )
    )
    return invite


def _ensure_benchmark_profile(config: AlphaJoinConfig) -> dict[str, Any]:
    home = config.home.expanduser().resolve()
    path = home / CAPABILITY_PROFILE_NAME
    if path.exists():
        try:
            capabilities = load_node_capabilities(home)
            if capabilities is not None:
                return {
                    "status": "existing",
                    "path": str(path),
                    "capability_tier": capabilities.get("capability_tier"),
                    "supported_job_types": capabilities.get("supported_job_types", []),
                    "ollama_models": capabilities.get("ollama_models", []),
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    report = run_node_benchmark(
        cpu_duration_seconds=config.cpu_duration_seconds,
        ollama_base_url=config.ollama_base_url,
    )
    save_node_benchmark(report, path)
    capabilities = report["capabilities"]
    return {
        "status": "created",
        "path": str(path),
        "capability_tier": capabilities.get("capability_tier"),
        "supported_job_types": capabilities.get("supported_job_types", []),
        "ollama_models": capabilities.get("ollama_models", []),
    }


def _coordinator_health(invite: AlphaInvite, *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        f"{invite.coordinator.rstrip('/')}/health",
        method="GET",
        headers={"X-ChatP2P-Admission-Token": invite.admission_token},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        return {"ok": False, "url": invite.coordinator, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "url": invite.coordinator, "payload": payload}


def _invite_url_check(invite: AlphaInvite) -> dict[str, Any]:
    parsed = urlparse(invite.coordinator)
    hostname = parsed.hostname or ""
    reachability = _invite_hostname_reachability(hostname)
    if reachability["status"] == "warn":
        return _alpha_check(
            "invite_url_shareable",
            "warn",
            reachability["message"],
            details={
                "coordinator": invite.coordinator,
                "hostname": hostname,
                "reachability": reachability["kind"],
            },
        )
    return _alpha_check(
        "invite_url_shareable",
        "pass",
        "Invite coordinator URL looks shareable.",
        details={
            "coordinator": invite.coordinator,
            "hostname": hostname,
            "reachability": reachability["kind"],
        },
    )


def _invite_hostname_reachability(hostname: str) -> dict[str, str]:
    normalized = hostname.strip().lower()
    if normalized in LOCAL_INVITE_HOSTS:
        return {
            "status": "warn",
            "kind": "local_only",
            "message": "Invite coordinator URL is local-only and will not work for outside contributors.",
        }
    if normalized.endswith(".local"):
        return {
            "status": "warn",
            "kind": "local_name",
            "message": "Invite coordinator URL uses a local network name and may not resolve for outside contributors.",
        }

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return {
            "status": "pass",
            "kind": "dns_name",
            "message": "Invite coordinator URL uses a DNS hostname.",
        }

    if address.is_loopback or address.is_unspecified:
        return {
            "status": "warn",
            "kind": "local_only",
            "message": "Invite coordinator URL is local-only and will not work for outside contributors.",
        }
    if address.version == 4 and address in SHARED_ADDRESS_SPACE:
        return {
            "status": "warn",
            "kind": "shared_network",
            "message": "Invite coordinator URL uses shared address space; outside contributors need the same VPN or tailnet.",
        }
    if address.is_private or address.is_link_local:
        return {
            "status": "warn",
            "kind": "private_network",
            "message": "Invite coordinator URL uses a private network address; outside contributors need the same LAN, VPN, or tunnel.",
        }
    if address.is_reserved or address.is_multicast:
        return {
            "status": "warn",
            "kind": "non_public_ip",
            "message": "Invite coordinator URL does not use a normal public internet address.",
        }
    return {
        "status": "pass",
        "kind": "public_ip",
        "message": "Invite coordinator URL uses a public IP address.",
    }


def _route_probe(
    *,
    invite: AlphaInvite,
    coordinator_url: str,
    timeout_seconds: float,
    tools: dict[str, Any] | None,
) -> dict[str, Any]:
    parsed = urlparse(coordinator_url)
    hostname = parsed.hostname or ""
    reachability = _tailnet_route_reachability(
        hostname,
        _invite_hostname_reachability(hostname),
        tools,
    )
    probe_invite = AlphaInvite(
        coordinator=coordinator_url.strip(),
        admission_token=invite.admission_token,
        created_at=invite.created_at,
        allowed_job_types=invite.allowed_job_types,
        notes=invite.notes,
    ).validated()
    health = _coordinator_health(probe_invite, timeout_seconds=timeout_seconds)
    return {
        "coordinator": probe_invite.coordinator,
        "hostname": hostname,
        "scheme": parsed.scheme,
        "port": parsed.port,
        "reachability": reachability,
        "outside_ready": reachability["status"] == "pass",
        "health": health,
    }


def _remote_route_tooling() -> dict[str, Any]:
    tailscale = _tool_status("tailscale")
    if tailscale["installed"]:
        tailscale["ip4"] = _command_stdout([tailscale["path"], "ip", "-4"], timeout_seconds=3.0)
    cloudflared = _tool_status("cloudflared")
    return {
        "tailscale": tailscale,
        "cloudflared": cloudflared,
    }


def _tool_status(command: str) -> dict[str, Any]:
    path = shutil.which(command)
    source = "path" if path else None
    if path is None:
        for candidate in KNOWN_WINDOWS_TOOL_PATHS.get(command, ()):
            if candidate.exists():
                path = str(candidate)
                source = "known_windows_path"
                break
    return {
        "installed": path is not None,
        "path": path,
        "source": source,
    }


def _tailnet_route_reachability(
    hostname: str,
    reachability: dict[str, str],
    tools: dict[str, Any] | None,
) -> dict[str, str]:
    if reachability["kind"] != "shared_network":
        return reachability
    if hostname not in _tailscale_ipv4_addresses(tools):
        return reachability
    return {
        "status": "pass",
        "kind": "tailnet_self",
        "message": "Invite coordinator URL uses this machine's Tailscale IP; contributors in the same tailnet should be able to reach it.",
    }


def _tailscale_ipv4_addresses(tools: dict[str, Any] | None) -> set[str]:
    tailscale = tools.get("tailscale", {}) if tools else {}
    ip4 = tailscale.get("ip4", {}) if isinstance(tailscale, dict) else {}
    stdout = ip4.get("stdout") if isinstance(ip4, dict) and ip4.get("ok") else None
    if not isinstance(stdout, str):
        return set()
    return {line.strip() for line in stdout.splitlines() if line.strip()}


def _command_stdout(argv: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stdout": None}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip() or None,
        "stderr": completed.stderr.strip() or None,
    }


def _route_recommendations(
    *,
    current_route: dict[str, Any],
    candidate_route: dict[str, Any] | None,
    tools: dict[str, Any] | None,
) -> list[str]:
    recommendations: list[str] = []
    current_reachability = current_route["reachability"]
    route_needs_help = current_reachability["status"] == "warn"
    if current_reachability["status"] == "warn":
        recommendations.append(current_reachability["message"])
        recommendations.append(
            "Choose a reachable route before sending the invite: private VPN/tailnet, HTTPS tunnel, or deliberate public hosting."
        )
        recommendations.append(
            "After choosing a route, regenerate the invite with that reachable URL, restart the coordinator, rerun alpha-preflight, then ask the partner to run node join."
        )

    if candidate_route is not None:
        candidate_reachability = candidate_route["reachability"]
        route_needs_help = route_needs_help or candidate_reachability["status"] == "warn"
        if candidate_reachability["status"] == "warn":
            recommendations.append(f"Candidate URL warning: {candidate_reachability['message']}")
        if not candidate_route["health"].get("ok"):
            route_needs_help = True
            recommendations.append("Candidate URL is not reachable from this machine yet.")

    if tools is not None and route_needs_help:
        if not tools["tailscale"]["installed"] and not tools["cloudflared"]["installed"]:
            recommendations.append(
                "No supported local route tooling was detected on PATH; install/configure a VPN/tailnet or tunnel manually before regenerating the invite."
            )
        elif tools["tailscale"]["installed"] and current_reachability["kind"] in {"local_only", "private_network"}:
            recommendations.append(
                "Tailscale is installed; a private tailnet route is likely the safest first remote test if your partner joins the same tailnet."
            )
        elif tools["cloudflared"]["installed"] and current_reachability["kind"] in {"local_only", "private_network"}:
            recommendations.append(
                "cloudflared is installed; an HTTPS tunnel can provide a public hostname without opening router ports."
            )
    return recommendations


def _coordinator_health_check(health: dict[str, Any]) -> dict[str, Any]:
    return _alpha_check(
        "coordinator_health",
        "pass" if health.get("ok") else "fail",
        "Coordinator health endpoint is reachable."
        if health.get("ok")
        else "Coordinator health endpoint is not reachable.",
        details=health,
    )


def _coordinator_operator_checks(
    health: dict[str, Any],
    operator_config: OperatorConfig | None,
) -> list[dict[str, Any]]:
    if not health.get("ok"):
        return []

    operator = health.get("payload", {}).get("operator", {})
    checks = [
        _alpha_check(
            "coordinator_public_alpha",
            "pass" if operator.get("public_alpha") and operator.get("admission_token_required") else "fail",
            "Coordinator is running in public-alpha mode."
            if operator.get("public_alpha") and operator.get("admission_token_required")
            else "Coordinator is not running in public-alpha mode.",
            details={"operator": operator},
        ),
        _alpha_check(
            "coordinator_token_redacted",
            "pass" if "admission_token" not in operator else "fail",
            "Coordinator public health summary redacts the admission token."
            if "admission_token" not in operator
            else "Coordinator public health summary exposes the admission token.",
            details={"token_exposed": "admission_token" in operator},
        ),
    ]

    if operator_config is not None:
        health_allowed = tuple(operator.get("allowed_job_types", []))
        allowed_match = health_allowed == operator_config.allowed_job_types
        checks.append(
            _alpha_check(
                "coordinator_allowed_job_types_match_config",
                "pass" if allowed_match else "fail",
                "Coordinator allowed job types match the operator config."
                if allowed_match
                else "Coordinator allowed job types do not match the operator config.",
                details={
                    "config_allowed_job_types": list(operator_config.allowed_job_types),
                    "coordinator_allowed_job_types": list(health_allowed),
                },
            )
        )

    return checks


def _managed_processes_for_report(home: Path) -> list[dict[str, Any]]:
    from .node_runtime import managed_processes_status

    return managed_processes_status(home=home)


def _managed_coordinator_check(managed_processes: list[dict[str, Any]]) -> dict[str, Any]:
    coordinator_status = next(
        (process for process in managed_processes if process.get("role") == "coordinator"),
        None,
    )
    if not coordinator_status or not coordinator_status.get("managed"):
        return _alpha_check(
            "managed_coordinator",
            "warn",
            "Coordinator is not managed by chatp2p node up for this home.",
            details={"coordinator": coordinator_status},
        )
    if coordinator_status.get("alive"):
        return _alpha_check(
            "managed_coordinator",
            "pass",
            "Managed coordinator process is alive.",
            details={"coordinator": coordinator_status},
        )
    return _alpha_check(
        "managed_coordinator",
        "fail",
        "Managed coordinator state exists but the process is not alive.",
        details={"coordinator": coordinator_status},
    )


def _validate_alpha_status_config(config: AlphaStatusConfig) -> None:
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.expected_worker_id is not None and not config.expected_worker_id.strip():
        raise ValueError("--expected-worker-id cannot be blank")


def _validate_alpha_evidence_config(config: AlphaEvidenceConfig) -> None:
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.status_timeout_seconds <= 0:
        raise ValueError("--status-timeout-seconds must be greater than 0")
    if config.inference_mode not in {"echo", "auto", "ollama"}:
        raise ValueError("--inference-mode must be echo, auto, or ollama")
    if config.inference_jobs < 1:
        raise ValueError("--inference-jobs must be at least 1")
    if config.inference_mode == "ollama" and not (config.inference_model or "").strip():
        raise ValueError("--inference-model is required when --inference-mode is ollama")
    if config.expected_worker_id is not None and not config.expected_worker_id.strip():
        raise ValueError("--expected-worker-id cannot be blank")
    if config.operator_task_name is not None and not config.operator_task_name.strip():
        raise ValueError("--operator-task-name cannot be blank")


def _snapshot_for_status(invite: AlphaInvite, *, timeout_seconds: float) -> dict[str, Any] | None:
    request = Request(
        f"{invite.coordinator.rstrip('/')}/api/snapshot",
        method="GET",
        headers={"X-ChatP2P-Admission-Token": invite.admission_token},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return None


def _alpha_status_checks(
    *,
    health: dict[str, Any],
    snapshot: dict[str, Any] | None,
    processes: list[dict[str, Any]],
    config: AlphaStatusConfig,
) -> list[dict[str, Any]]:
    checks = [_coordinator_health_check(health), _managed_coordinator_check(processes)]
    checks.append(_managed_worker_check(processes))

    status = snapshot.get("status", {}) if snapshot else {}
    live_workers = int(status.get("live_nodes") or 0)
    disputed_jobs = int(status.get("disputed_jobs") or 0)
    queued_jobs = int(status.get("queued_jobs") or 0)
    pending_jobs = int(status.get("pending_jobs") or 0)
    leased_jobs = int(status.get("leased_jobs") or 0)
    backlog = queued_jobs + pending_jobs + leased_jobs
    checks.append(
        _alpha_check(
            "live_workers",
            "pass" if live_workers >= config.min_live_workers else "fail",
            f"Coordinator sees {live_workers} live worker(s).",
            details={"actual": live_workers, "required": config.min_live_workers},
        )
    )
    checks.append(
        _alpha_check(
            "disputed_jobs",
            "pass" if disputed_jobs == 0 else "fail",
            "No disputed jobs are present." if disputed_jobs == 0 else f"{disputed_jobs} disputed job(s) present.",
            details={"disputed_jobs": disputed_jobs},
        )
    )
    checks.append(
        _alpha_check(
            "work_backlog",
            "pass" if backlog == 0 else "warn",
            "No queued, pending, or leased jobs remain."
            if backlog == 0
            else "Some jobs are still queued, pending verification, or leased.",
            details={"queued_jobs": queued_jobs, "pending_jobs": pending_jobs, "leased_jobs": leased_jobs},
        )
    )
    if config.expected_worker_id:
        node = _snapshot_node(snapshot or {}, config.expected_worker_id)
        live = bool(node is not None and node.get("liveness_status") == "live")
        checks.append(
            _alpha_check(
                "expected_worker_live",
                "pass" if live else "fail",
                "Expected worker is live." if live else "Expected worker is not live.",
                details=_status_expected_worker_summary(snapshot, config.expected_worker_id),
            )
        )
    return checks


def _managed_worker_check(managed_processes: list[dict[str, Any]]) -> dict[str, Any]:
    worker_status = next(
        (process for process in managed_processes if process.get("role") == "worker"),
        None,
    )
    if not worker_status or not worker_status.get("managed"):
        return _alpha_check(
            "managed_worker",
            "warn",
            "Worker is not managed by chatp2p node up or node join for this home.",
            details={"worker": worker_status},
        )
    if worker_status.get("alive"):
        return _alpha_check(
            "managed_worker",
            "pass",
            "Managed worker process is alive.",
            details={"worker": worker_status},
        )
    return _alpha_check(
        "managed_worker",
        "fail",
        "Managed worker state exists but the process is not alive.",
        details={"worker": worker_status},
    )


def _status_expected_worker_summary(
    snapshot: dict[str, Any] | None,
    expected_worker_id: str | None,
) -> dict[str, Any] | None:
    if expected_worker_id is None:
        return None
    node = _snapshot_node(snapshot or {}, expected_worker_id)
    return {
        "node_id": expected_worker_id,
        "present": node is not None,
        "live": bool(node is not None and node.get("liveness_status") == "live"),
        "liveness_status": node.get("liveness_status") if node else None,
        "credits": node.get("credits") if node else None,
        "last_seen_seconds_ago": node.get("last_seen_seconds_ago") if node else None,
        "supported_job_types": node.get("supported_job_types", []) if node else [],
        "ollama_available": _node_ollama_available(node) if node else False,
        "ollama_models": _node_ollama_models(node) if node else [],
    }


def _status_node_summaries(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if snapshot is None:
        return []
    summaries = []
    for node in snapshot.get("nodes", []):
        summaries.append(
            {
                "node_id": node.get("node_id"),
                "liveness_status": node.get("liveness_status"),
                "credits": node.get("credits"),
                "capability_tier": node.get("capability_tier"),
                "supported_job_types": node.get("supported_job_types", []),
                "ollama_available": _node_ollama_available(node),
                "ollama_models": _node_ollama_models(node),
            }
        )
    return summaries


def _node_ollama_available(node: dict[str, Any]) -> bool:
    runtime = node.get("model_runtimes", {}).get("ollama", {})
    return bool(runtime.get("available")) if isinstance(runtime, dict) else False


def _validate_node_watchdog_config(config: NodeWatchdogConfig) -> None:
    if config.role not in {"both", "coordinator", "worker"}:
        raise ValueError("--role must be both, coordinator, or worker")
    if config.checks < 0:
        raise ValueError("--checks cannot be negative")
    if config.interval_seconds <= 0:
        raise ValueError("--interval-seconds must be greater than 0")
    if config.coordinator_port is not None and not 1 <= config.coordinator_port <= 65535:
        raise ValueError("--coordinator-port must be between 1 and 65535")
    if config.lease_timeout_seconds <= 0:
        raise ValueError("--lease-timeout-seconds must be greater than 0")
    if config.node_stale_seconds <= 0:
        raise ValueError("--node-stale-seconds must be greater than 0")
    if config.worker_interval <= 0:
        raise ValueError("--worker-interval must be greater than 0")
    if config.startup_timeout_seconds <= 0:
        raise ValueError("--startup-timeout-seconds must be greater than 0")
    if config.cpu_duration_seconds < 0:
        raise ValueError("--cpu-duration-seconds cannot be negative")


def _validate_capability_refresh_config(config: NodeCapabilityRefreshConfig) -> None:
    if config.cpu_duration_seconds < 0:
        raise ValueError("--cpu-duration-seconds cannot be negative")
    if config.worker_interval <= 0:
        raise ValueError("--worker-interval must be greater than 0")
    if config.startup_timeout_seconds <= 0:
        raise ValueError("--startup-timeout-seconds must be greater than 0")
    if config.restart_worker and config.invite_path is None:
        raise ValueError("--invite is required when --restart-worker is used")


def _capability_refresh_summary(capabilities: dict[str, Any] | None) -> dict[str, Any] | None:
    if capabilities is None:
        return None
    return {
        "capability_tier": capabilities.get("capability_tier"),
        "supported_job_types": capabilities.get("supported_job_types", []),
        "ollama_available": _capability_ollama_available(capabilities),
        "ollama_models": capabilities.get("ollama_models", []),
        "gpu": capabilities.get("gpu", {}),
        "benchmark": capabilities.get("benchmark", {}),
    }


def _capability_refresh_changes(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    previous_supported = set((previous or {}).get("supported_job_types", []))
    current_supported = set(current.get("supported_job_types", []))
    previous_models = set((previous or {}).get("ollama_models", []))
    current_models = set(current.get("ollama_models", []))
    return {
        "supported_job_types_added": sorted(current_supported - previous_supported),
        "supported_job_types_removed": sorted(previous_supported - current_supported),
        "ollama_models_added": sorted(current_models - previous_models),
        "ollama_models_removed": sorted(previous_models - current_models),
        "ollama_available_changed": _capability_ollama_available(previous) != _capability_ollama_available(current),
        "capability_tier_changed": (previous or {}).get("capability_tier") != current.get("capability_tier"),
    }


def _capability_ollama_available(capabilities: dict[str, Any] | None) -> bool:
    if capabilities is None:
        return False
    runtime = capabilities.get("model_runtimes", {}).get("ollama", {})
    return bool(runtime.get("available")) if isinstance(runtime, dict) else False


def _wait_for_worker_capability_advertisement(
    *,
    invite: AlphaInvite,
    node_id: str,
    expected_capabilities: dict[str, Any],
    timeout_seconds: float,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    client = CoordinatorClient(invite.coordinator, admission_token=invite.admission_token)
    expected_supported = set(expected_capabilities.get("supported_job_types", []))
    expected_models = set(expected_capabilities.get("ollama_models", []))
    deadline = time.time() + timeout_seconds
    last_node = None
    last_error = None
    while time.time() <= deadline:
        try:
            snapshot = client.snapshot()
            last_node = _snapshot_node(snapshot, node_id)
            if last_node is not None:
                actual_supported = set(last_node.get("supported_job_types", []))
                actual_models = set(_node_ollama_models(last_node))
                if expected_supported.issubset(actual_supported) and expected_models.issubset(actual_models):
                    return {
                        "ok": True,
                        "node_id": node_id,
                        "supported_job_types": sorted(actual_supported),
                        "ollama_models": sorted(actual_models),
                    }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(poll_interval)
    return {
        "ok": False,
        "node_id": node_id,
        "expected_supported_job_types": sorted(expected_supported),
        "expected_ollama_models": sorted(expected_models),
        "last_node": _capability_advertisement_node_summary(last_node),
        "last_error": last_error,
    }


def _capability_advertisement_node_summary(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return {
        "node_id": node.get("node_id"),
        "liveness_status": node.get("liveness_status"),
        "supported_job_types": node.get("supported_job_types", []),
        "ollama_models": _node_ollama_models(node),
    }


def _run_watchdog_iteration(
    *,
    config: NodeWatchdogConfig,
    invite: AlphaInvite,
    home: Path,
    index: int,
) -> dict[str, Any]:
    started_at = time.time()
    roles = _watchdog_roles(config.role)
    before_processes = _managed_processes_for_report(home)
    before_health = _coordinator_health(invite, timeout_seconds=min(config.startup_timeout_seconds, 5.0))
    before_snapshot = _snapshot_for_status(invite, timeout_seconds=5.0) if before_health.get("ok") else None
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    health = before_health

    if "coordinator" in roles and _watchdog_coordinator_needs_restart(before_processes, before_health):
        if not config.restart:
            actions.append({"role": "coordinator", "action": "restart_skipped", "reason": "restart disabled"})
        elif config.operator_config_path is None:
            errors.append("coordinator needs restart but --operator-config was not provided")
            actions.append(
                {"role": "coordinator", "action": "restart_skipped", "reason": "missing operator config"}
            )
        else:
            start = _start_watchdog_coordinator(config=config, home=home, invite=invite)
            health = _wait_for_invite_health(
                invite=invite,
                timeout_seconds=config.startup_timeout_seconds,
                poll_interval=0.2,
            )
            actions.append({"role": "coordinator", "action": "restart", "start": start, "health": health})
            if not health.get("ok"):
                errors.append("coordinator restart did not become healthy before timeout")

    if "worker" in roles:
        if not health.get("ok"):
            actions.append({"role": "worker", "action": "restart_skipped", "reason": "coordinator unhealthy"})
        elif _watchdog_worker_needs_restart(home=home, processes=before_processes, snapshot=before_snapshot):
            if not config.restart:
                actions.append({"role": "worker", "action": "restart_skipped", "reason": "restart disabled"})
            else:
                join = run_alpha_join(
                    AlphaJoinConfig(
                        invite_path=config.invite_path,
                        home=home,
                        ollama_base_url=config.ollama_base_url,
                        worker_interval=config.worker_interval,
                        startup_timeout_seconds=config.startup_timeout_seconds,
                        cpu_duration_seconds=config.cpu_duration_seconds,
                        force=True,
                    )
                )
                actions.append({"role": "worker", "action": "restart", "join": join})
                if not join.get("ok"):
                    errors.append("worker restart did not register live before timeout")

    after_processes = _managed_processes_for_report(home)
    after_health = _coordinator_health(invite, timeout_seconds=min(config.startup_timeout_seconds, 5.0))
    after_snapshot = _snapshot_for_status(invite, timeout_seconds=5.0) if after_health.get("ok") else None
    return {
        "index": index,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "ok": not errors,
        "before": {
            "health": before_health,
            "processes": before_processes,
            "snapshot_summary": _snapshot_baseline(before_snapshot),
        },
        "actions": actions,
        "after": {
            "health": after_health,
            "processes": after_processes,
            "snapshot_summary": _snapshot_baseline(after_snapshot),
        },
        "errors": errors,
    }


def _node_watchdog_report(
    *,
    config: NodeWatchdogConfig,
    invite: AlphaInvite,
    home: Path,
    iterations: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    ok = not errors and all(iteration.get("ok", False) for iteration in iterations)
    return {
        "schema": NODE_WATCHDOG_REPORT_SCHEMA,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "home": str(home),
            "invite_path": str(config.invite_path),
            "report_path": str(config.report_path) if config.report_path else None,
            "role": config.role,
            "restart": config.restart,
            "checks": config.checks,
            "interval_seconds": config.interval_seconds,
            "operator_config_path": str(config.operator_config_path) if config.operator_config_path else None,
            "coordinator_host": config.coordinator_host,
            "coordinator_port": _watchdog_coordinator_port(config, invite),
            "worker_interval": config.worker_interval,
            "startup_timeout_seconds": config.startup_timeout_seconds,
        },
        "invite": invite.public_summary(),
        "iterations": iterations,
        "errors": errors,
    }


def _watchdog_roles(role: str) -> tuple[str, ...]:
    return ("coordinator", "worker") if role == "both" else (role,)


def _watchdog_coordinator_needs_restart(
    processes: list[dict[str, Any]],
    health: dict[str, Any],
) -> bool:
    coordinator = next((process for process in processes if process.get("role") == "coordinator"), {})
    return not coordinator.get("alive") or not health.get("ok")


def _watchdog_worker_needs_restart(
    *,
    home: Path,
    processes: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
) -> bool:
    worker = next((process for process in processes if process.get("role") == "worker"), {})
    if not worker.get("alive"):
        return True
    identity_path = home / "worker.identity.json"
    if not identity_path.exists() or snapshot is None:
        return False
    try:
        identity = NodeIdentity.load(identity_path)
    except Exception:
        return True
    node = _snapshot_node(snapshot, identity.node_id)
    return node is None or node.get("liveness_status") != "live"


def _watchdog_coordinator_port(config: NodeWatchdogConfig, invite: AlphaInvite) -> int:
    if config.coordinator_port is not None:
        return config.coordinator_port
    parsed = urlparse(invite.coordinator)
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _start_watchdog_coordinator(
    *,
    config: NodeWatchdogConfig,
    home: Path,
    invite: AlphaInvite,
) -> dict[str, Any]:
    assert config.operator_config_path is not None
    port = _watchdog_coordinator_port(config, invite)
    argv = [
        sys.executable,
        "-m",
        "chatp2p.cli",
        "coordinator",
        "serve",
        "--home",
        str(home),
        "--host",
        config.coordinator_host,
        "--port",
        str(port),
        "--lease-timeout-seconds",
        str(config.lease_timeout_seconds),
        "--node-stale-seconds",
        str(config.node_stale_seconds),
        "--operator-config",
        str(config.operator_config_path),
    ]
    return start_managed_process(
        home=home,
        role="coordinator",
        argv=argv,
        coordinator_url=invite.coordinator,
        force=True,
        extra_state={
            "listen": {"host": config.coordinator_host, "port": port},
            "operator_config": str(config.operator_config_path),
            "started_by": "node-watchdog",
        },
    )


def _wait_for_worker_live(
    *,
    home: Path,
    invite: AlphaInvite,
    node_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    client = CoordinatorClient(invite.coordinator, admission_token=invite.admission_token)
    deadline = time.time() + timeout_seconds
    last_error = "worker has not appeared in the coordinator snapshot yet"
    last_node = None
    last_snapshot_status = None

    while time.time() <= deadline:
        try:
            snapshot = client.snapshot()
            last_snapshot_status = snapshot.get("status")
            for node in snapshot.get("nodes", []):
                if node.get("node_id") != node_id:
                    continue
                last_node = node
                if node.get("liveness_status") == "live":
                    return {"ok": True, "node": node, "snapshot_status": last_snapshot_status}
                last_error = f"worker registered but is {node.get('liveness_status', 'unknown')}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        worker_status = managed_process_status(home=home, role="worker")
        if worker_status.get("managed") and not worker_status.get("alive") and last_node is None:
            last_error = "worker process exited before it appeared in the coordinator snapshot"
            break
        time.sleep(0.2)

    return {
        "ok": False,
        "error": last_error,
        "node": last_node,
        "snapshot_status": last_snapshot_status,
        "worker_process": managed_process_status(home=home, role="worker"),
    }


def _validate_drill_config(config: AlphaDrillConfig) -> None:
    if config.simulated_workers < 0:
        raise ValueError("--simulated-workers cannot be negative")
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.worker_interval <= 0:
        raise ValueError("--worker-interval must be greater than 0")
    if config.startup_timeout_seconds <= 0:
        raise ValueError("--startup-timeout-seconds must be greater than 0")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.cpu_duration_seconds < 0:
        raise ValueError("--cpu-duration-seconds cannot be negative")
    if config.coordinator_port is not None and not 1 <= config.coordinator_port <= 65535:
        raise ValueError("--coordinator-port must be between 1 and 65535")
    if config.lease_timeout_seconds <= 0:
        raise ValueError("--lease-timeout-seconds must be greater than 0")
    if config.node_stale_seconds <= 0:
        raise ValueError("--node-stale-seconds must be greater than 0")


def _drill_required_workers(config: AlphaDrillConfig) -> int:
    primary = 1 if config.start_primary_worker else 0
    return max(1, primary + config.simulated_workers)


def _drill_simulated_worker_home(home: Path, index: int) -> Path:
    return home.parent / f"{home.name}-alpha-drill" / f"worker-{index}"


def _drill_coordinator_port(config: AlphaDrillConfig, invite: AlphaInvite) -> int:
    if config.coordinator_port is not None:
        return config.coordinator_port
    parsed = urlparse(invite.coordinator)
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _drill_local_coordinator_url(host: str, port: int) -> str:
    worker_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    if ":" in worker_host and not worker_host.startswith("["):
        worker_host = f"[{worker_host}]"
    return f"http://{worker_host}:{port}"


def _start_drill_coordinator(
    *,
    config: AlphaDrillConfig,
    home: Path,
    invite: AlphaInvite,
) -> dict[str, Any]:
    assert config.config_path is not None
    port = _drill_coordinator_port(config, invite)
    argv = [
        sys.executable,
        "-m",
        "chatp2p.cli",
        "coordinator",
        "serve",
        "--home",
        str(home),
        "--host",
        config.coordinator_host,
        "--port",
        str(port),
        "--lease-timeout-seconds",
        str(config.lease_timeout_seconds),
        "--node-stale-seconds",
        str(config.node_stale_seconds),
        "--operator-config",
        str(config.config_path),
    ]
    return start_managed_process(
        home=home,
        role="coordinator",
        argv=argv,
        coordinator_url=_drill_local_coordinator_url(config.coordinator_host, port),
        force=True,
        extra_state={"listen": {"host": config.coordinator_host, "port": port}, "started_by": "alpha-drill"},
    )


def _wait_for_invite_health(
    *,
    invite: AlphaInvite,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_health = {"ok": False, "url": invite.coordinator, "error": "not attempted"}
    while time.time() <= deadline:
        last_health = _coordinator_health(invite, timeout_seconds=min(timeout_seconds, 5.0))
        if last_health.get("ok"):
            return last_health
        time.sleep(poll_interval)
    return last_health


def _sidecar_report_path(path: Path, label: str) -> Path:
    suffix = path.suffix or ".json"
    stem = path.stem if path.suffix else path.name
    return path.with_name(f"{stem}-{label}{suffix}")


def _validate_smoke_config(config: AlphaSmokeConfig) -> None:
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.min_accepted_results < 0:
        raise ValueError("--min-accepted-results cannot be negative")
    if config.min_verified_jobs < 0:
        raise ValueError("--min-verified-jobs cannot be negative")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")


def _smoke_payload(index: int) -> dict[str, Any]:
    left = index + 11
    right = (index * 3) + 5
    return {
        "task": "arithmetic",
        "operation": "add",
        "operands": [left, right],
        "expected": left + right,
    }


def _result_ids(snapshot: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (
            str(result.get("job_id")),
            str(result.get("node_id")),
            str(result.get("output_hash")),
        )
        for result in snapshot.get("results", [])
    }


def _smoke_criteria(
    snapshot: dict[str, Any],
    created_job_ids: set[str],
    initial_result_ids: set[tuple[str, str, str]],
    config: AlphaSmokeConfig,
) -> dict[str, Any]:
    status = snapshot.get("status", {})
    created_jobs = [
        job for job in snapshot.get("jobs", [])
        if job.get("job_id") in created_job_ids
    ]
    new_created_results = [
        result for result in snapshot.get("results", [])
        if result.get("job_id") in created_job_ids
        and (
            str(result.get("job_id")),
            str(result.get("node_id")),
            str(result.get("output_hash")),
        )
        not in initial_result_ids
    ]
    live_workers = int(status.get("live_nodes") or 0)
    accepted_results = len(new_created_results)
    verified_jobs = sum(1 for job in created_jobs if job.get("status") == "verified")
    disputed_jobs = sum(1 for job in created_jobs if job.get("status") == "disputed")
    return {
        "live_workers": {
            "actual": live_workers,
            "required": config.min_live_workers,
            "passed": live_workers >= config.min_live_workers,
        },
        "accepted_results": {
            "actual": accepted_results,
            "required": config.min_accepted_results,
            "passed": accepted_results >= config.min_accepted_results,
        },
        "verified_jobs": {
            "actual": verified_jobs,
            "required": config.min_verified_jobs,
            "passed": verified_jobs >= config.min_verified_jobs,
        },
        "disputed_jobs": {
            "actual": disputed_jobs,
            "required": 0,
            "passed": disputed_jobs == 0,
        },
    }


def _smoke_report(
    *,
    config: AlphaSmokeConfig,
    invite: AlphaInvite,
    duration_seconds: float,
    created_jobs: list[dict[str, Any]],
    criteria: dict[str, Any],
    final_snapshot: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, Any]:
    ok = not errors and all(item["passed"] for item in criteria.values())
    created_job_ids = {job["job_id"] for job in created_jobs}
    final_created_jobs = (
        [
            job for job in final_snapshot.get("jobs", [])
            if job.get("job_id") in created_job_ids
        ]
        if final_snapshot
        else []
    )
    return {
        "schema": ALPHA_SMOKE_REPORT_SCHEMA,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "invite": invite.public_summary(),
        "parameters": {
            "jobs": config.jobs,
            "min_live_workers": config.min_live_workers,
            "min_accepted_results": config.min_accepted_results,
            "min_verified_jobs": config.min_verified_jobs,
            "timeout_seconds": config.timeout_seconds,
            "poll_interval": config.poll_interval,
        },
        "created_jobs": created_jobs,
        "created_job_statuses": final_created_jobs,
        "criteria": criteria,
        "errors": errors,
        "final_snapshot": final_snapshot,
    }


def _validate_remote_proof_config(config: AlphaRemoteProofConfig) -> None:
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.min_accepted_results is not None and config.min_accepted_results < 0:
        raise ValueError("--min-accepted-results cannot be negative")
    if config.min_verified_jobs is not None and config.min_verified_jobs < 0:
        raise ValueError("--min-verified-jobs cannot be negative")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.expected_worker_id is not None and not config.expected_worker_id.strip():
        raise ValueError("--expected-worker-id cannot be blank")


def _validate_inference_proof_config(config: AlphaInferenceProofConfig) -> None:
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.mode not in {"echo", "ollama", "auto"}:
        raise ValueError("--mode must be echo, ollama, or auto")
    if config.mode == "ollama" and not (config.model and config.model.strip()):
        raise ValueError("--model is required when --mode ollama")
    if config.model is not None and not config.model.strip():
        raise ValueError("--model cannot be blank")
    if not config.prompt.strip():
        raise ValueError("--prompt cannot be blank")
    if config.temperature is not None and not 0 <= config.temperature <= 2:
        raise ValueError("--temperature must be between 0 and 2")
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


def _remote_proof_required_accepted_results(config: AlphaRemoteProofConfig) -> int:
    if config.min_accepted_results is not None:
        return config.min_accepted_results
    return config.jobs * 2


def _remote_proof_required_verified_jobs(config: AlphaRemoteProofConfig) -> int:
    if config.min_verified_jobs is not None:
        return config.min_verified_jobs
    return config.jobs


def _remote_proof_empty_criteria(config: AlphaRemoteProofConfig) -> dict[str, Any]:
    expected_worker_required = 1 if config.expected_worker_id else 0
    return {
        "created_jobs": {"actual": 0, "required": config.jobs, "passed": False},
        "live_workers": {"actual": 0, "required": config.min_live_workers, "passed": False},
        "accepted_results": {
            "actual": 0,
            "required": _remote_proof_required_accepted_results(config),
            "passed": False,
        },
        "verified_jobs": {
            "actual": 0,
            "required": _remote_proof_required_verified_jobs(config),
            "passed": False,
        },
        "all_created_jobs_terminal": {"actual": 0, "required": config.jobs, "passed": False},
        "disputed_jobs": {"actual": 0, "required": 0, "passed": True},
        "expired_jobs": {"actual": 0, "required": 0, "passed": True},
        "incomplete_jobs": {"actual": config.jobs, "required": 0, "passed": False},
        "expected_worker_live": {
            "actual": 0,
            "required": expected_worker_required,
            "passed": expected_worker_required == 0,
        },
        "expected_worker_results": {
            "actual": 0,
            "required": expected_worker_required,
            "passed": expected_worker_required == 0,
        },
    }


def _remote_proof_criteria(
    snapshot: dict[str, Any],
    created_job_ids: set[str],
    initial_result_ids: set[tuple[str, str, str]],
    config: AlphaRemoteProofConfig,
) -> dict[str, Any]:
    status = snapshot.get("status", {})
    created_jobs = _snapshot_jobs_for_ids(snapshot, created_job_ids)
    created_results = _snapshot_results_for_ids(snapshot, created_job_ids, initial_result_ids)
    status_counts = _job_status_counts(created_jobs)
    live_workers = int(status.get("live_nodes") or 0)
    accepted_results = len(created_results)
    verified_jobs = status_counts["verified"]
    disputed_jobs = status_counts["disputed"]
    expired_jobs = status_counts["expired"]
    terminal_jobs = sum(1 for job in created_jobs if job.get("status") in TERMINAL_JOB_STATUSES)
    incomplete_jobs = sum(1 for job in created_jobs if job.get("status") not in TERMINAL_JOB_STATUSES)
    expected_worker_required = 1 if config.expected_worker_id else 0
    expected_worker = _snapshot_node(snapshot, config.expected_worker_id) if config.expected_worker_id else None
    expected_worker_live = int(
        bool(expected_worker is not None and expected_worker.get("liveness_status") == "live")
    )
    expected_worker_results = (
        sum(1 for result in created_results if result.get("node_id") == config.expected_worker_id)
        if config.expected_worker_id
        else 0
    )
    required_verified_jobs = _remote_proof_required_verified_jobs(config)
    required_accepted_results = _remote_proof_required_accepted_results(config)

    return {
        "created_jobs": {
            "actual": len(created_job_ids),
            "required": config.jobs,
            "passed": len(created_job_ids) == config.jobs,
        },
        "live_workers": {
            "actual": live_workers,
            "required": config.min_live_workers,
            "passed": live_workers >= config.min_live_workers,
        },
        "accepted_results": {
            "actual": accepted_results,
            "required": required_accepted_results,
            "passed": accepted_results >= required_accepted_results,
        },
        "verified_jobs": {
            "actual": verified_jobs,
            "required": required_verified_jobs,
            "passed": verified_jobs >= required_verified_jobs,
        },
        "all_created_jobs_terminal": {
            "actual": terminal_jobs,
            "required": len(created_job_ids),
            "passed": bool(created_job_ids) and terminal_jobs == len(created_job_ids),
        },
        "disputed_jobs": {
            "actual": disputed_jobs,
            "required": 0,
            "passed": disputed_jobs == 0,
        },
        "expired_jobs": {
            "actual": expired_jobs,
            "required": 0,
            "passed": expired_jobs == 0,
        },
        "incomplete_jobs": {
            "actual": incomplete_jobs,
            "required": 0,
            "passed": incomplete_jobs == 0,
        },
        "expected_worker_live": {
            "actual": expected_worker_live,
            "required": expected_worker_required,
            "passed": expected_worker_required == 0 or expected_worker_live >= expected_worker_required,
        },
        "expected_worker_results": {
            "actual": expected_worker_results,
            "required": expected_worker_required,
            "passed": expected_worker_required == 0 or expected_worker_results >= expected_worker_required,
        },
    }


def _inference_mode_decision(
    config: AlphaInferenceProofConfig,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    if config.mode == "echo":
        return {
            "status": "pass",
            "mode": config.mode,
            "job_type": "inference.echo.v1",
            "message": "Using echo inference proof.",
            "ollama_capable_live_nodes": [],
        }

    model = config.model.strip() if config.model else None
    capable_nodes = _live_nodes_with_ollama_model(snapshot, model) if model else []
    if config.mode == "auto":
        if model and capable_nodes:
            return {
                "status": "pass",
                "mode": config.mode,
                "job_type": "inference.ollama.v1",
                "message": f"Using Ollama inference proof because {len(capable_nodes)} live node(s) advertise {model}.",
                "model": model,
                "ollama_capable_live_nodes": capable_nodes,
            }
        return {
            "status": "warn",
            "mode": config.mode,
            "job_type": "inference.echo.v1",
            "message": "Falling back to echo inference proof because no live node advertises the requested Ollama model.",
            "model": model,
            "ollama_capable_live_nodes": capable_nodes,
        }

    if not capable_nodes:
        return {
            "status": "fail",
            "mode": config.mode,
            "job_type": "inference.ollama.v1",
            "message": f"No live worker advertises Ollama model {model!r}.",
            "model": model,
            "ollama_capable_live_nodes": capable_nodes,
        }
    return {
        "status": "pass",
        "mode": config.mode,
        "job_type": "inference.ollama.v1",
        "message": f"Using Ollama inference proof with {len(capable_nodes)} capable live node(s).",
        "model": model,
        "ollama_capable_live_nodes": capable_nodes,
    }


def _live_nodes_with_ollama_model(snapshot: dict[str, Any], model: str | None) -> list[dict[str, Any]]:
    if model is None:
        return []
    capable = []
    for node in snapshot.get("nodes", []):
        if node.get("liveness_status") != "live":
            continue
        if "inference.ollama.v1" not in node.get("supported_job_types", []):
            continue
        models = _node_ollama_models(node)
        if model not in models:
            continue
        capable.append(
            {
                "node_id": node.get("node_id"),
                "capability_tier": node.get("capability_tier"),
                "models": models,
            }
        )
    return capable


def _node_ollama_models(node: dict[str, Any]) -> list[str]:
    top_level = node.get("ollama_models")
    if isinstance(top_level, list):
        return [str(model) for model in top_level]
    models = (
        node.get("model_runtimes", {})
        .get("ollama", {})
        .get("models", [])
    )
    return [str(model) for model in models] if isinstance(models, list) else []


def _inference_proof_payload(
    config: AlphaInferenceProofConfig,
    job_type: str,
    index: int,
) -> dict[str, Any]:
    prompt = f"{config.prompt.strip()}\n\nProof job {index + 1} of {config.jobs}."
    if job_type == "inference.ollama.v1":
        payload: dict[str, Any] = {"model": str(config.model).strip(), "prompt": prompt}
        if config.temperature is not None:
            payload["temperature"] = config.temperature
        return payload
    return {"prompt": prompt}


def _inference_required_accepted_results(config: AlphaInferenceProofConfig) -> int:
    if config.min_accepted_results is not None:
        return config.min_accepted_results
    return config.jobs


def _inference_required_verified_jobs(config: AlphaInferenceProofConfig) -> int:
    if config.min_verified_jobs is not None:
        return config.min_verified_jobs
    return config.jobs


def _inference_required_expected_worker_results(config: AlphaInferenceProofConfig) -> int:
    if config.min_expected_worker_results is not None:
        return config.min_expected_worker_results
    return 1 if config.expected_worker_id else 0


def _inference_proof_empty_criteria(config: AlphaInferenceProofConfig) -> dict[str, Any]:
    expected_worker_required = _inference_required_expected_worker_results(config)
    return {
        "created_jobs": {"actual": 0, "required": config.jobs, "passed": False},
        "live_workers": {"actual": 0, "required": config.min_live_workers, "passed": False},
        "accepted_results": {
            "actual": 0,
            "required": _inference_required_accepted_results(config),
            "passed": False,
        },
        "verified_jobs": {
            "actual": 0,
            "required": _inference_required_verified_jobs(config),
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
            "required": expected_worker_required,
            "passed": expected_worker_required == 0,
        },
    }


def _inference_proof_criteria(
    snapshot: dict[str, Any],
    created_job_ids: set[str],
    initial_result_ids: set[tuple[str, str, str]],
    config: AlphaInferenceProofConfig,
) -> dict[str, Any]:
    status = snapshot.get("status", {})
    created_jobs = _snapshot_jobs_for_ids(snapshot, created_job_ids)
    created_results = _snapshot_results_for_ids(snapshot, created_job_ids, initial_result_ids)
    status_counts = _job_status_counts(created_jobs)
    live_workers = int(status.get("live_nodes") or 0)
    accepted_results = len(created_results)
    verified_jobs = status_counts["verified"]
    disputed_jobs = status_counts["disputed"]
    expired_jobs = status_counts["expired"]
    terminal_jobs = sum(1 for job in created_jobs if job.get("status") in TERMINAL_JOB_STATUSES)
    incomplete_jobs = sum(1 for job in created_jobs if job.get("status") not in TERMINAL_JOB_STATUSES)
    expected_worker = _snapshot_node(snapshot, config.expected_worker_id) if config.expected_worker_id else None
    expected_worker_live = int(
        bool(expected_worker is not None and expected_worker.get("liveness_status") == "live")
    )
    expected_worker_results = (
        sum(1 for result in created_results if result.get("node_id") == config.expected_worker_id)
        if config.expected_worker_id
        else 0
    )
    expected_worker_required = _inference_required_expected_worker_results(config)

    return {
        "created_jobs": {
            "actual": len(created_job_ids),
            "required": config.jobs,
            "passed": len(created_job_ids) == config.jobs,
        },
        "live_workers": {
            "actual": live_workers,
            "required": config.min_live_workers,
            "passed": live_workers >= config.min_live_workers,
        },
        "accepted_results": {
            "actual": accepted_results,
            "required": _inference_required_accepted_results(config),
            "passed": accepted_results >= _inference_required_accepted_results(config),
        },
        "verified_jobs": {
            "actual": verified_jobs,
            "required": _inference_required_verified_jobs(config),
            "passed": verified_jobs >= _inference_required_verified_jobs(config),
        },
        "all_created_jobs_terminal": {
            "actual": terminal_jobs,
            "required": len(created_job_ids),
            "passed": bool(created_job_ids) and terminal_jobs == len(created_job_ids),
        },
        "disputed_jobs": {
            "actual": disputed_jobs,
            "required": 0,
            "passed": disputed_jobs == 0,
        },
        "expired_jobs": {
            "actual": expired_jobs,
            "required": 0,
            "passed": expired_jobs == 0,
        },
        "incomplete_jobs": {
            "actual": incomplete_jobs,
            "required": 0,
            "passed": incomplete_jobs == 0,
        },
        "expected_worker_live": {
            "actual": expected_worker_live,
            "required": 1 if config.expected_worker_id else 0,
            "passed": config.expected_worker_id is None or expected_worker_live >= 1,
        },
        "expected_worker_results": {
            "actual": expected_worker_results,
            "required": expected_worker_required,
            "passed": expected_worker_results >= expected_worker_required,
        },
    }


def _remote_proof_report(
    *,
    config: AlphaRemoteProofConfig,
    invite: AlphaInvite,
    duration_seconds: float,
    initial_snapshot: dict[str, Any] | None,
    final_snapshot: dict[str, Any] | None,
    created_jobs: list[dict[str, Any]],
    criteria: dict[str, Any],
    errors: list[str],
    initial_result_ids: set[tuple[str, str, str]],
) -> dict[str, Any]:
    ok = not errors and all(item["passed"] for item in criteria.values())
    created_job_ids = {job["job_id"] for job in created_jobs}
    created_job_statuses = (
        _snapshot_jobs_for_ids(final_snapshot, created_job_ids) if final_snapshot is not None else []
    )
    created_results = (
        _snapshot_results_for_ids(final_snapshot, created_job_ids, initial_result_ids)
        if final_snapshot is not None
        else []
    )
    expected_worker = (
        _remote_proof_expected_worker_summary(final_snapshot, config.expected_worker_id, created_results)
        if config.expected_worker_id
        else None
    )
    return {
        "schema": ALPHA_REMOTE_PROOF_REPORT_SCHEMA,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "invite": invite.public_summary(),
        "parameters": {
            "jobs": config.jobs,
            "expected_worker_id": config.expected_worker_id,
            "min_live_workers": config.min_live_workers,
            "min_accepted_results": _remote_proof_required_accepted_results(config),
            "min_verified_jobs": _remote_proof_required_verified_jobs(config),
            "timeout_seconds": config.timeout_seconds,
            "poll_interval": config.poll_interval,
        },
        "baseline": _snapshot_baseline(initial_snapshot),
        "final_summary": _snapshot_baseline(final_snapshot),
        "created_jobs": created_jobs,
        "created_job_status_counts": _job_status_counts(created_job_statuses),
        "created_job_statuses": created_job_statuses,
        "created_results": created_results,
        "expected_worker": expected_worker,
        "criteria": criteria,
        "errors": errors,
    }


def _inference_proof_report(
    *,
    config: AlphaInferenceProofConfig,
    invite: AlphaInvite,
    duration_seconds: float,
    selected_job_type: str,
    mode_decision: dict[str, Any],
    initial_snapshot: dict[str, Any] | None,
    final_snapshot: dict[str, Any] | None,
    created_jobs: list[dict[str, Any]],
    criteria: dict[str, Any],
    errors: list[str],
    initial_result_ids: set[tuple[str, str, str]],
) -> dict[str, Any]:
    ok = not errors and all(item["passed"] for item in criteria.values())
    created_job_ids = {job["job_id"] for job in created_jobs}
    created_job_statuses = (
        _snapshot_jobs_for_ids(final_snapshot, created_job_ids) if final_snapshot is not None else []
    )
    created_results = (
        _snapshot_results_for_ids(final_snapshot, created_job_ids, initial_result_ids)
        if final_snapshot is not None
        else []
    )
    expected_worker = (
        _remote_proof_expected_worker_summary(final_snapshot, config.expected_worker_id, created_results)
        if config.expected_worker_id
        else None
    )
    return {
        "schema": ALPHA_INFERENCE_PROOF_REPORT_SCHEMA,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "invite": invite.public_summary(),
        "parameters": {
            "jobs": config.jobs,
            "mode": config.mode,
            "selected_job_type": selected_job_type,
            "model": config.model,
            "prompt": config.prompt,
            "temperature": config.temperature,
            "expected_worker_id": config.expected_worker_id,
            "min_live_workers": config.min_live_workers,
            "min_accepted_results": _inference_required_accepted_results(config),
            "min_verified_jobs": _inference_required_verified_jobs(config),
            "min_expected_worker_results": _inference_required_expected_worker_results(config),
            "timeout_seconds": config.timeout_seconds,
            "poll_interval": config.poll_interval,
        },
        "mode_decision": mode_decision,
        "baseline": _snapshot_baseline(initial_snapshot),
        "final_summary": _snapshot_baseline(final_snapshot),
        "created_jobs": created_jobs,
        "created_job_status_counts": _job_status_counts(created_job_statuses),
        "created_job_statuses": created_job_statuses,
        "created_results": _inference_result_summaries(created_results),
        "result_node_counts": _result_node_counts(created_results),
        "expected_worker": expected_worker,
        "criteria": criteria,
        "errors": errors,
    }


def _inference_result_summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for result in results:
        output = result.get("output", {})
        answer = output.get("answer") if isinstance(output, dict) else None
        summaries.append(
            {
                "job_id": result.get("job_id"),
                "job_type": result.get("job_type"),
                "node_id": result.get("node_id"),
                "output_hash": result.get("output_hash"),
                "runtime_seconds": result.get("runtime_seconds"),
                "model": output.get("model") if isinstance(output, dict) else None,
                "confidence": output.get("confidence") if isinstance(output, dict) else None,
                "answer_preview": _preview_text(answer),
            }
        )
    return summaries


def _preview_text(value: Any, *, max_length: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text if len(text) <= max_length else f"{text[:max_length]}..."


def _result_node_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        node_id = str(result.get("node_id"))
        counts[node_id] = counts.get(node_id, 0) + 1
    return dict(sorted(counts.items()))


def _snapshot_baseline(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    status = snapshot.get("status", {})
    return {
        "status": status,
        "job_count": len(snapshot.get("jobs", [])),
        "result_count": len(snapshot.get("results", [])),
        "node_count": len(snapshot.get("nodes", [])),
    }


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


def _snapshot_node(snapshot: dict[str, Any], node_id: str | None) -> dict[str, Any] | None:
    if node_id is None:
        return None
    for node in snapshot.get("nodes", []):
        if node.get("node_id") == node_id:
            return node
    return None


def _job_status_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"queued": 0, "leased": 0, "pending": 0, "verified": 0, "disputed": 0, "expired": 0}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _remote_proof_expected_worker_summary(
    snapshot: dict[str, Any] | None,
    expected_worker_id: str | None,
    created_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if expected_worker_id is None:
        return None
    node = _snapshot_node(snapshot or {}, expected_worker_id)
    result_count = sum(1 for result in created_results if result.get("node_id") == expected_worker_id)
    return {
        "node_id": expected_worker_id,
        "present": node is not None,
        "live": bool(node is not None and node.get("liveness_status") == "live"),
        "liveness_status": node.get("liveness_status") if node else None,
        "credits": node.get("credits") if node else None,
        "result_count": result_count,
    }


def _evidence_status_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    snapshot_status = (report.get("snapshot_summary") or {}).get("status") or {}
    checks = {check.get("id"): check for check in report.get("checks", [])}
    return {
        "ok": report.get("ok"),
        "status": report.get("status"),
        "counts": report.get("counts"),
        "live_workers": snapshot_status.get("live_nodes"),
        "verified_jobs": snapshot_status.get("verified_jobs"),
        "disputed_jobs": snapshot_status.get("disputed_jobs"),
        "queued_jobs": snapshot_status.get("queued_jobs"),
        "pending_jobs": snapshot_status.get("pending_jobs"),
        "leased_jobs": snapshot_status.get("leased_jobs"),
        "expected_worker": report.get("expected_worker"),
        "nodes": report.get("nodes", []),
        "managed_processes": [
            {
                "role": process.get("role"),
                "managed": process.get("managed"),
                "alive": process.get("alive"),
                "pid": process.get("pid"),
                "started_at": process.get("started_at"),
            }
            for process in report.get("managed_processes", [])
        ],
        "check_statuses": {
            check_id: {
                "status": check.get("status"),
                "message": check.get("message"),
                "details": check.get("details"),
            }
            for check_id, check in checks.items()
        },
    }


def _evidence_remote_proof_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    final_status = ((report.get("final_summary") or {}).get("status")) or {}
    return {
        "ok": report.get("ok"),
        "status": report.get("status"),
        "duration_seconds": report.get("duration_seconds"),
        "parameters": report.get("parameters"),
        "created_jobs": len(report.get("created_jobs", [])),
        "created_job_status_counts": report.get("created_job_status_counts"),
        "criteria": report.get("criteria"),
        "expected_worker": report.get("expected_worker"),
        "final_status": {
            "live_nodes": final_status.get("live_nodes"),
            "verified_jobs": final_status.get("verified_jobs"),
            "disputed_jobs": final_status.get("disputed_jobs"),
            "queued_jobs": final_status.get("queued_jobs"),
            "pending_jobs": final_status.get("pending_jobs"),
            "leased_jobs": final_status.get("leased_jobs"),
            "credits": final_status.get("credits"),
        },
        "errors": report.get("errors", []),
    }


def _evidence_inference_proof_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    final_status = ((report.get("final_summary") or {}).get("status")) or {}
    parameters = report.get("parameters") or {}
    criteria = report.get("criteria") or {}
    expected_worker = report.get("expected_worker") or {}
    expected_worker_results = criteria.get("expected_worker_results") or {}
    return {
        "ok": report.get("ok"),
        "status": report.get("status"),
        "duration_seconds": report.get("duration_seconds"),
        "mode": parameters.get("mode"),
        "selected_job_type": parameters.get("selected_job_type"),
        "model": parameters.get("model"),
        "created_jobs": len(report.get("created_jobs", [])),
        "created_job_status_counts": report.get("created_job_status_counts"),
        "accepted_results": (criteria.get("accepted_results") or {}).get("actual"),
        "verified_jobs": (criteria.get("verified_jobs") or {}).get("actual"),
        "disputed_jobs": (criteria.get("disputed_jobs") or {}).get("actual"),
        "result_node_counts": report.get("result_node_counts", {}),
        "expected_worker": expected_worker or None,
        "expected_worker_contributed": bool(expected_worker_results.get("actual", 0) > 0),
        "criteria": criteria,
        "final_status": {
            "live_nodes": final_status.get("live_nodes"),
            "verified_jobs": final_status.get("verified_jobs"),
            "disputed_jobs": final_status.get("disputed_jobs"),
            "queued_jobs": final_status.get("queued_jobs"),
            "pending_jobs": final_status.get("pending_jobs"),
            "leased_jobs": final_status.get("leased_jobs"),
            "credits": final_status.get("credits"),
        },
        "errors": report.get("errors", []),
    }


def _copy_redacted_artifact(
    *,
    source: Path,
    destination: Path,
    secrets_to_redact: tuple[str, ...],
) -> dict[str, Any]:
    if not source.exists():
        return {
            "ok": False,
            "status": "missing",
            "source": str(source),
            "path": str(destination),
        }
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            destination.write_text(_redact_string(raw, secrets_to_redact), encoding="utf-8")
            artifact_format = "text"
        else:
            _write_json_report(destination, _redact_evidence_value(data, secrets_to_redact))
            artifact_format = "json"
    except OSError as exc:
        return {
            "ok": False,
            "status": "error",
            "source": str(source),
            "path": str(destination),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "status": "copied",
        "source": str(source),
        "path": str(destination),
        "format": artifact_format,
        "bytes": destination.stat().st_size,
    }


def _query_windows_task_for_evidence(task_name: str | None, *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"ok": False, "status": "skipped", "task_name": task_name, "reason": "task query disabled"}
    if task_name is None:
        return {"ok": False, "status": "skipped", "task_name": None, "reason": "no task name configured"}
    command = ["schtasks.exe", "/Query", "/TN", task_name, "/FO", "LIST"]
    if os.name != "nt":
        return {
            "ok": False,
            "status": "skipped",
            "task_name": task_name,
            "command": command,
            "reason": "Windows Scheduled Tasks are only available on Windows",
        }
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "status": "error",
            "task_name": task_name,
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }
    parsed = _parse_schtasks_list_output(completed.stdout)
    return {
        "ok": completed.returncode == 0,
        "status": "found" if completed.returncode == 0 else "not_found",
        "task_name": task_name,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip() or None,
        "stderr": completed.stderr.strip() or None,
        "parsed": parsed,
    }


def _parse_schtasks_list_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        normalized = key.strip().lower().replace(" ", "_")
        if normalized:
            parsed[normalized] = value.strip()
    return parsed


def _redact_artifact_file(path: Path, secrets_to_redact: tuple[str, ...]) -> None:
    if not path.exists() or not secrets_to_redact:
        return
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        path.write_text(_redact_string(raw, secrets_to_redact), encoding="utf-8")
        return
    _write_json_report(path, _redact_evidence_value(data, secrets_to_redact))


def _redact_evidence_value(value: Any, secrets_to_redact: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in {"admission_token", "x-chatp2p-admission-token"}:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_evidence_value(item, secrets_to_redact)
        return redacted
    if isinstance(value, list):
        return [_redact_evidence_value(item, secrets_to_redact) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_evidence_value(item, secrets_to_redact) for item in value)
    if isinstance(value, str):
        return _redact_string(value, secrets_to_redact)
    return value


def _redact_string(value: str, secrets_to_redact: tuple[str, ...]) -> str:
    redacted = value
    for secret_value in secrets_to_redact:
        if secret_value:
            redacted = redacted.replace(secret_value, "<redacted>")
    return redacted


def _scan_artifacts_for_secrets(paths: list[Path], secrets_to_redact: tuple[str, ...]) -> dict[str, Any]:
    checked: list[str] = []
    leaks: list[str] = []
    if not secrets_to_redact:
        return {"checked": checked, "leaks": leaks}
    for path in paths:
        if not path.exists():
            continue
        checked.append(str(path))
        try:
            contents = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(secret_value and secret_value in contents for secret_value in secrets_to_redact):
            leaks.append(str(path))
    return {"checked": checked, "leaks": leaks}


def _alpha_evidence_markdown(report: dict[str, Any]) -> str:
    status_summary = report.get("alpha_status") or {}
    proof_summary = report.get("remote_proof") or {}
    inference_summary = report.get("inference_proof") or {}
    expected_worker = status_summary.get("expected_worker") or proof_summary.get("expected_worker") or {}
    task_parsed = (report.get("operator_watchdog_task") or {}).get("parsed") or {}
    lines = [
        "# ChatP2P Alpha Evidence Pack",
        "",
        f"Generated: {report.get('generated_at')}",
        f"Status: {str(report.get('status')).upper()}",
        f"Checks: {report.get('counts')}",
        "",
        "## Current Network",
        "",
        f"- Live workers: {status_summary.get('live_workers')}",
        f"- Verified jobs: {status_summary.get('verified_jobs')}",
        f"- Disputed jobs: {status_summary.get('disputed_jobs')}",
        f"- Queued/pending/leased: {status_summary.get('queued_jobs')}/"
        f"{status_summary.get('pending_jobs')}/{status_summary.get('leased_jobs')}",
        f"- Expected worker: {expected_worker.get('node_id')}",
        f"- Expected worker live: {expected_worker.get('live')}",
        "",
        "## Remote Proof",
        "",
        f"- Proof status: {proof_summary.get('status')}",
        f"- Jobs created: {proof_summary.get('created_jobs')}",
        f"- Job status counts: {proof_summary.get('created_job_status_counts')}",
        f"- Duration seconds: {proof_summary.get('duration_seconds')}",
        "",
    ]
    if inference_summary:
        lines.extend(
            [
                "## Inference Proof",
                "",
                f"- Proof status: {inference_summary.get('status')}",
                f"- Mode: {inference_summary.get('mode')}",
                f"- Selected job type: {inference_summary.get('selected_job_type')}",
                f"- Accepted results: {inference_summary.get('accepted_results')}",
                f"- Verified jobs: {inference_summary.get('verified_jobs')}",
                f"- Disputed jobs: {inference_summary.get('disputed_jobs')}",
                f"- Result node counts: {inference_summary.get('result_node_counts')}",
                f"- Expected worker contributed: {inference_summary.get('expected_worker_contributed')}",
                "",
            ]
        )
    lines.extend(
        [
            "## Watchdog",
            "",
            f"- Operator task status: {task_parsed.get('status')}",
            f"- Operator task logon mode: {task_parsed.get('logon_mode')}",
            f"- Watchdog report copied: {(report.get('artifacts') or {}).get('watchdog_report', {}).get('ok')}",
            "",
            "## Artifacts",
            "",
        ]
    )
    artifacts = report.get("artifacts") or {}
    for key in ("alpha_status", "alpha_remote_proof", "alpha_inference_proof", "operator_watchdog_task", "summary_json"):
        if key not in artifacts:
            continue
        lines.append(f"- {key}: {artifacts.get(key)}")
    watchdog = artifacts.get("watchdog_report") or {}
    lines.append(f"- watchdog_report: {watchdog.get('path')}")
    lines.append("")
    return "\n".join(lines)


def _alpha_report(
    *,
    schema: str,
    config: dict[str, Any],
    checks: list[dict[str, Any]],
    details: dict[str, Any],
) -> dict[str, Any]:
    counts = _check_counts(checks)
    ok = counts["fail"] == 0
    return {
        "schema": schema,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "config": config,
        "checks": checks,
        **details,
    }


def _alpha_check(
    check_id: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check = {"id": check_id, "status": status, "message": message}
    if details is not None:
        check["details"] = details
    return check


def _check_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for check in checks:
        status = check.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _write_json_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def _write_text_report(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _worker_loop_argv(config: AlphaJoinConfig, invite: AlphaInvite) -> list[str]:
    return [
        sys.executable,
        "-m",
        "chatp2p.cli",
        "worker",
        "loop",
        "--home",
        str(config.home.expanduser().resolve()),
        "--coordinator",
        invite.coordinator,
        "--admission-token",
        invite.admission_token,
        "--ollama-base-url",
        config.ollama_base_url,
        "--interval",
        str(config.worker_interval),
    ]


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invite {key} must be a non-empty string")
    return value.strip()

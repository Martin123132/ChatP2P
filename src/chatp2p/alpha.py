"""Public-alpha invite and join helpers."""

from __future__ import annotations

import json
import secrets
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
DEFAULT_ALPHA_NOTES = "ChatP2P public alpha invite. Keep this file private; it contains the admission token."
LOCAL_INVITE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


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
    if hostname.lower() in LOCAL_INVITE_HOSTS:
        return _alpha_check(
            "invite_url_shareable",
            "warn",
            "Invite coordinator URL is local-only and will not work for outside contributors.",
            details={"coordinator": invite.coordinator, "hostname": hostname},
        )
    return _alpha_check(
        "invite_url_shareable",
        "pass",
        "Invite coordinator URL looks shareable.",
        details={"coordinator": invite.coordinator, "hostname": hostname},
    )


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

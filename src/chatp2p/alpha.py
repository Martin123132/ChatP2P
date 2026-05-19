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
DEFAULT_ALPHA_NOTES = "ChatP2P public alpha invite. Keep this file private; it contains the admission token."


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

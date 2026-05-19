"""Readiness checks for a local ChatP2P node."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .benchmark import CAPABILITY_PROFILE_NAME, OLLAMA_JOB_TYPE, load_node_capabilities
from .crypto import NodeIdentity
from .ollama import DEFAULT_OLLAMA_BASE_URL, OllamaError, list_ollama_models


@dataclass(frozen=True)
class NodeDoctorConfig:
    home: Path = Path(".mesh")
    model: str | None = None
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    coordinator_url: str | None = "http://127.0.0.1:8765"
    timeout_seconds: float = 2.0


def run_node_doctor(config: NodeDoctorConfig) -> dict[str, Any]:
    """Return a JSON-serializable readiness report for a local node."""

    checks: list[dict[str, Any]] = []
    model = config.model.strip() if isinstance(config.model, str) else None
    model = model or None

    identity = _check_worker_identity(config.home)
    checks.append(identity)

    capabilities = _load_capabilities(config.home, checks)
    checks.append(_check_base_capabilities(capabilities))
    checks.append(_check_ollama_binary(model_required=model is not None))

    try:
        ollama_models = list_ollama_models(
            base_url=config.ollama_base_url,
            timeout_seconds=config.timeout_seconds,
        )
        checks.append(
            _check(
                "ollama_service",
                "pass",
                f"Ollama is reachable and advertises {len(ollama_models)} model(s).",
                details={"base_url": config.ollama_base_url, "models": ollama_models},
            )
        )
    except OllamaError as exc:
        ollama_models = []
        checks.append(
            _check(
                "ollama_service",
                "fail" if model else "warn",
                f"Ollama is not reachable at {config.ollama_base_url}: {exc}",
                fix="Start Ollama, or pass --ollama-base-url for a different local Ollama endpoint.",
                details={"base_url": config.ollama_base_url},
            )
        )

    checks.append(_check_requested_runtime_model(model, ollama_models))
    checks.append(_check_advertised_model(config.home, model, capabilities))
    checks.append(_check_coordinator(config.coordinator_url, config.timeout_seconds))

    counts = _count_statuses(checks)
    ok = counts["fail"] == 0
    return {
        "ok": ok,
        "status": "ready" if ok else "needs_attention",
        "home": str(config.home),
        "requested_model": model,
        "ollama_base_url": config.ollama_base_url,
        "coordinator_url": config.coordinator_url,
        "counts": counts,
        "checks": checks,
    }


def _load_capabilities(home: Path, checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    profile_path = home / CAPABILITY_PROFILE_NAME
    try:
        capabilities = load_node_capabilities(home)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        checks.append(
            _check(
                "benchmark_profile",
                "fail",
                f"Could not read saved benchmark profile at {profile_path}: {exc}",
                fix="Run chatp2p node benchmark again.",
                details={"path": str(profile_path)},
            )
        )
        return None

    if capabilities is None:
        checks.append(
            _check(
                "benchmark_profile",
                "fail",
                f"No saved benchmark profile found at {profile_path}.",
                fix="Run chatp2p node benchmark before running a worker.",
                details={"path": str(profile_path)},
            )
        )
        return None

    checks.append(
        _check(
            "benchmark_profile",
            "pass",
            f"Saved benchmark profile found with tier {capabilities.get('capability_tier', 'unknown')}.",
            details={
                "path": str(profile_path),
                "capability_tier": capabilities.get("capability_tier"),
                "supported_job_types": capabilities.get("supported_job_types", []),
                "ollama_models": capabilities.get("ollama_models", []),
            },
        )
    )
    return capabilities


def _check_worker_identity(home: Path) -> dict[str, Any]:
    identity_path = home / "worker.identity.json"
    if not identity_path.exists():
        return _check(
            "worker_identity",
            "warn",
            f"No worker identity found at {identity_path}. Worker commands will create one automatically.",
            fix="Run chatp2p init-identity --name worker if you want to create it explicitly.",
            details={"path": str(identity_path)},
        )

    try:
        identity = NodeIdentity.load(identity_path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _check(
            "worker_identity",
            "fail",
            f"Worker identity at {identity_path} could not be loaded: {exc}",
            fix="Move the broken identity aside, then run chatp2p init-identity --name worker.",
            details={"path": str(identity_path)},
        )

    return _check(
        "worker_identity",
        "pass",
        f"Worker identity is present: {identity.node_id}.",
        details={"path": str(identity_path), "node_id": identity.node_id},
    )


def _check_base_capabilities(capabilities: dict[str, Any] | None) -> dict[str, Any]:
    if capabilities is None:
        return _check(
            "base_job_capabilities",
            "skip",
            "Skipped base capability check because no valid benchmark profile was loaded.",
        )

    supported = capabilities.get("supported_job_types", [])
    missing = [
        job_type
        for job_type in ["eval.deterministic.v1", "inference.echo.v1"]
        if job_type not in supported
    ]
    if missing:
        return _check(
            "base_job_capabilities",
            "fail",
            f"Saved capability profile is missing base job types: {', '.join(missing)}.",
            fix="Run chatp2p node benchmark again.",
            details={"supported_job_types": supported},
        )

    return _check(
        "base_job_capabilities",
        "pass",
        "Saved capability profile can run base deterministic and echo jobs.",
        details={"supported_job_types": supported},
    )


def _check_ollama_binary(*, model_required: bool) -> dict[str, Any]:
    path = shutil.which("ollama")
    if path is not None:
        return _check("ollama_binary", "pass", f"Ollama command found at {path}.", details={"path": path})

    return _check(
        "ollama_binary",
        "warn",
        "Ollama command was not found on PATH.",
        fix="Install Ollama or add it to PATH. HTTP checks may still pass if Ollama is already running elsewhere.",
        details={"required_for_requested_model": model_required},
    )


def _check_requested_runtime_model(model: str | None, ollama_models: list[str]) -> dict[str, Any]:
    if model is None:
        return _check(
            "ollama_runtime_model",
            "skip",
            "No --model supplied, so no specific Ollama model was required.",
        )

    if model in ollama_models:
        return _check(
            "ollama_runtime_model",
            "pass",
            f"Ollama runtime has requested model {model}.",
            details={"model": model, "models": ollama_models},
        )

    return _check(
        "ollama_runtime_model",
        "fail",
        f"Ollama runtime does not have requested model {model}.",
        fix=f"Run ollama pull {model}.",
        details={"model": model, "models": ollama_models},
    )


def _check_advertised_model(
    home: Path,
    model: str | None,
    capabilities: dict[str, Any] | None,
) -> dict[str, Any]:
    if model is None:
        return _check(
            "advertised_ollama_model",
            "skip",
            "No --model supplied, so advertised model routing was not checked.",
        )
    if capabilities is None:
        return _check(
            "advertised_ollama_model",
            "skip",
            "Skipped advertised model check because no valid benchmark profile was loaded.",
        )

    advertised_models = capabilities.get("ollama_models", [])
    supported = capabilities.get("supported_job_types", [])
    if model in advertised_models and OLLAMA_JOB_TYPE in supported:
        return _check(
            "advertised_ollama_model",
            "pass",
            f"Saved benchmark profile advertises {model} for Ollama routing.",
            details={"model": model, "ollama_models": advertised_models},
        )

    return _check(
        "advertised_ollama_model",
        "fail",
        f"Saved benchmark profile does not advertise requested model {model}.",
        fix=(
            f"After pulling the model, run chatp2p node benchmark --home {home} "
            "so workers advertise it to the coordinator."
        ),
        details={
            "model": model,
            "ollama_models": advertised_models,
            "supported_job_types": supported,
        },
    )


def _check_coordinator(coordinator_url: str | None, timeout_seconds: float) -> dict[str, Any]:
    if not coordinator_url:
        return _check(
            "coordinator",
            "skip",
            "Coordinator reachability check skipped.",
        )

    request = Request(f"{coordinator_url.rstrip('/')}/health", method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as exc:
        return _check(
            "coordinator",
            "fail",
            f"Coordinator returned HTTP {exc.code} at {coordinator_url}.",
            fix="Start the coordinator with chatp2p coordinator serve, or pass --coordinator for another URL.",
            details={"coordinator_url": coordinator_url},
        )
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return _check(
            "coordinator",
            "fail",
            f"Coordinator is not reachable at {coordinator_url}: {exc}",
            fix="Start the coordinator with chatp2p coordinator serve, or pass --coordinator for another URL.",
            details={"coordinator_url": coordinator_url},
        )

    return _check(
        "coordinator",
        "pass",
        f"Coordinator is reachable at {coordinator_url}.",
        details={
            "coordinator_url": coordinator_url,
            "coordinator_id": data.get("coordinator_id"),
            "known_nodes": data.get("known_nodes"),
            "jobs": data.get("jobs"),
            "verified_jobs": data.get("verified_jobs"),
        },
    )


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    fix: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check = {"id": check_id, "status": status, "message": message}
    if fix:
        check["fix"] = fix
    if details is not None:
        check["details"] = details
    return check


def _count_statuses(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for check in checks:
        status = check.get("status")
        if status in counts:
            counts[status] += 1
    return counts

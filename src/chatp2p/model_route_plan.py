"""Read-only model routing plan for ChatP2P chat requests."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .alpha import load_alpha_invite
from .client import CoordinatorClient
from .jsonio import read_json_file
from .model_governance import MODEL_GOVERNANCE_REGISTRY_SCHEMA, default_model_governance_registry
from .model_registry import MODEL_REGISTRY_SCHEMA, default_model_registry, validate_model_registry
from .model_release import ModelReleaseCheckConfig, run_model_release_check
from .runtime_metadata import software_metadata_public_view


MODEL_ROUTE_PLAN_REPORT_SCHEMA = "chatp2p.model-route-plan-report.v1"
DEFAULT_ROUTE_PLAN_JOB_TYPE = "inference.chat.v1"
DEFAULT_ROUTE_PLAN_RUNTIME = "ollama"
DEFAULT_COORDINATOR_URL = "http://127.0.0.1:8765"

_SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "tailscale_auth_key": re.compile(r"\btskey-[A-Za-z0-9_-]+\b"),
    "github_token": re.compile(r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "alpha_token": re.compile(r"\balpha-token-[A-Za-z0-9_-]{8,}\b"),
    "credit_grant_token": re.compile(r"\bcredit-grant-token-[A-Za-z0-9_-]{8,}\b"),
    "long_admission_token": re.compile(r"""admission_token["']?\s*[:=]\s*["'][^"']{20,}["']"""),
    "long_credit_grant_token": re.compile(r"""credit_grant_token["']?\s*[:=]\s*["'][^"']{20,}["']"""),
}


@dataclass(frozen=True)
class ModelRoutePlanConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    governance_path: Path = Path(".mesh/model-governance.json")
    out_dir: Path = Path(".mesh/model-route-plan")
    preferred_model: str | None = None
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    skip_network_checks: bool = False
    timeout_seconds: float = 5.0
    job_type: str = DEFAULT_ROUTE_PLAN_JOB_TYPE
    runtime: str = DEFAULT_ROUTE_PLAN_RUNTIME


def run_model_route_plan(config: ModelRoutePlanConfig) -> dict[str, Any]:
    """Plan safe model routing without creating jobs or mutating live nodes."""

    _validate_config(config)
    started_at = time.time()
    generated_at = datetime.now(timezone.utc).isoformat()
    registry_path = config.registry_path.expanduser().resolve()
    governance_path = config.governance_path.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    warnings = ["model route plan is read-only and does not create jobs, grant credits, or restart workers"]
    errors: list[str] = []

    registry, registry_status, registry_warnings = _load_registry(registry_path)
    governance, governance_status, governance_warnings = _load_governance(governance_path)
    warnings.extend(registry_warnings)
    warnings.extend(governance_warnings)

    registry_validation = validate_model_registry(registry)
    warnings.extend(f"model registry: {warning}" for warning in registry_validation.get("warnings", []))
    errors.extend(f"model registry: {error}" for error in registry_validation.get("errors", []))

    connection = _resolve_connection(config)
    snapshot_info = _snapshot_info(config=config, connection=connection)
    if snapshot_info["status"] == "skipped":
        warnings.append("network checks were skipped; route readiness cannot be proven")
    elif not snapshot_info["ok"]:
        warnings.append("coordinator snapshot is unavailable; route readiness cannot be proven")

    models = _model_views(
        registry=registry,
        governance_path=governance_path,
        registry_path=registry_path,
        registry_validation=registry_validation,
        snapshot=snapshot_info.get("snapshot"),
        config=config,
    )
    selected = _select_model(models, preferred_model=config.preferred_model)
    summary = _summary(
        models=models,
        selected=selected,
        snapshot_info=snapshot_info,
        preferred_model=config.preferred_model,
        errors=errors,
    )
    if config.preferred_model and selected is None:
        errors.append(f"preferred model is not in the registry: {config.preferred_model}")
        summary["recommended_next_action"] = "choose_registered_model"

    status = "fail" if errors else ("pass" if summary["route_ready"] else "warn")
    report: dict[str, Any] = {
        "schema": MODEL_ROUTE_PLAN_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": generated_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "governance_path": _safe_text(str(governance_path)),
            "out_dir": _safe_text(str(out_dir)),
            "preferred_model": _safe_text(config.preferred_model),
            "coordinator_url": _safe_text(connection.get("coordinator_url")),
            "invite_path": _safe_path(config.invite_path),
            "admission_token_present": bool(connection.get("admission_token")),
            "skip_network_checks": config.skip_network_checks,
            "timeout_seconds": config.timeout_seconds,
            "job_type": _safe_text(config.job_type),
            "runtime": _safe_text(config.runtime),
        },
        "registry_status": registry_status,
        "governance_status": governance_status,
        "registry_validation_summary": _safe_json(registry_validation.get("summary") or {}),
        "coordinator": _coordinator_view(snapshot_info, connection=connection),
        "summary": summary,
        "models": models,
        "warnings": [_safe_text(warning) for warning in warnings],
        "errors": [_safe_text(error) for error in errors],
    }
    json_path = out_dir / "model-route-plan.json"
    markdown_path = out_dir / "model-route-plan.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_route_plan_markdown(report), encoding="utf-8")
    return report


def format_model_route_plan_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model route plan: {str(report.get('status', 'unknown')).upper()}",
        f"Selected model: {summary.get('selected_model_id') or 'none'}",
        f"Route ready: {summary.get('route_ready')}",
        f"Live capable workers: {summary.get('live_model_capable_worker_count')}",
        f"Approved models: {summary.get('approved_model_count')}",
        f"Next: {summary.get('recommended_next_action')}",
    ]
    if report.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    if (report.get("artifacts") or {}).get("json"):
        lines.append(f"Report: {(report.get('artifacts') or {}).get('json')}")
    return "\n".join(lines)


def format_model_route_plan_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    coordinator = report.get("coordinator") or {}
    lines = [
        "# ChatP2P Model Route Plan",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Selected model: `{summary.get('selected_model_id') or 'none'}`",
        f"- Route ready: `{summary.get('route_ready')}`",
        f"- Network checked: `{summary.get('network_checked')}`",
        f"- Coordinator reachable: `{coordinator.get('ok')}`",
        f"- Live capable workers: `{summary.get('live_model_capable_worker_count')}`",
        f"- Approved models: `{summary.get('approved_model_count')}`",
        f"- Routeable models: `{summary.get('routeable_model_count')}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Models",
        "",
        "| Model | Status | Release ready | Approved | Runtime | Live capable workers | Route ready | Next |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for model in report.get("models") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(model.get("model_id")),
                    str(model.get("status")),
                    str(model.get("release_ready")),
                    str(model.get("approved")),
                    str(model.get("runtime_support_status")),
                    str((model.get("routing") or {}).get("live_eligible_node_count")),
                    str(model.get("route_ready")),
                    _markdown_table_text(str(model.get("recommended_next_action") or "")),
                ]
            )
            + " |"
        )
    lines.append("")
    selected = summary.get("selected_model_id")
    if selected:
        chosen = next((model for model in report.get("models") or [] if model.get("model_id") == selected), None)
        blockers = (chosen or {}).get("blockers") or []
        lines.extend(["## Selected Model Blockers", ""])
        if blockers:
            lines.extend(f"- `{blocker}`" for blocker in blockers)
        else:
            lines.append("- `none`")
        lines.append("")
    if report.get("warnings"):
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
        lines.append("")
    if report.get("errors"):
        lines.extend(["## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
        lines.append("")
    return "\n".join(lines)


def _load_registry(path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if not path.exists():
        return (
            default_model_registry(),
            {"source": "builtin_default", "exists": False, "schema": MODEL_REGISTRY_SCHEMA},
            ["model_registry_missing_using_builtin_default"],
        )
    registry = read_json_file(path, description="model registry")
    if not isinstance(registry, dict):
        raise ValueError("model registry must be a JSON object")
    return registry, {"source": "file", "exists": True, "schema": registry.get("schema")}, []


def _load_governance(path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if not path.exists():
        return (
            default_model_governance_registry(),
            {"source": "builtin_default", "exists": False, "schema": MODEL_GOVERNANCE_REGISTRY_SCHEMA},
            ["model_governance_missing_using_builtin_default"],
        )
    governance = read_json_file(path, description="model governance registry")
    if not isinstance(governance, dict):
        raise ValueError("model governance registry must be a JSON object")
    return governance, {"source": "file", "exists": True, "schema": governance.get("schema")}, []


def _resolve_connection(config: ModelRoutePlanConfig) -> dict[str, Any]:
    invite = load_alpha_invite(config.invite_path.expanduser()) if config.invite_path else None
    coordinator_url = config.coordinator_url or (invite.coordinator if invite else DEFAULT_COORDINATOR_URL)
    admission_token = config.admission_token or (invite.admission_token if invite else None)
    return {
        "coordinator_url": str(coordinator_url).rstrip("/"),
        "admission_token": admission_token,
        "invite_summary": invite.public_summary() if invite else None,
    }


def _snapshot_info(*, config: ModelRoutePlanConfig, connection: dict[str, Any]) -> dict[str, Any]:
    if config.skip_network_checks:
        return {"ok": None, "status": "skipped", "snapshot": None, "error": None}
    try:
        client = CoordinatorClient(
            str(connection["coordinator_url"]),
            admission_token=connection.get("admission_token"),
            timeout_seconds=config.timeout_seconds,
        )
        return {"ok": True, "status": "pass", "snapshot": client.snapshot(), "error": None}
    except Exception as exc:
        return {"ok": False, "status": "unreachable", "snapshot": None, "error": _safe_text(f"{type(exc).__name__}: {exc}")}


def _model_views(
    *,
    registry: dict[str, Any],
    governance_path: Path,
    registry_path: Path,
    registry_validation: dict[str, Any],
    snapshot: dict[str, Any] | None,
    config: ModelRoutePlanConfig,
) -> list[dict[str, Any]]:
    readiness_by_id = {
        item.get("id"): item
        for item in registry_validation.get("model_readiness", [])
        if isinstance(item, dict) and item.get("id")
    }
    raw_models = registry.get("models") if isinstance(registry.get("models"), list) else []
    views: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("id") or "")
        release_check = run_model_release_check(
            ModelReleaseCheckConfig(
                registry_path=registry_path,
                governance_path=governance_path,
                model_id=model_id,
            )
        )
        release_summary = release_check.get("summary") if isinstance(release_check.get("summary"), dict) else {}
        routing = _routing_for_model(snapshot=snapshot, model_id=model_id, job_type=config.job_type)
        status = str(raw.get("status") or "unknown")
        runtime_support = _runtime_support_status(raw, runtime=config.runtime)
        approved = status == "approved"
        release_ready = bool(release_summary.get("release_ready"))
        route_ready = approved and release_ready and runtime_support == "verified" and routing["live_eligible_node_count"] > 0
        blockers = _model_blockers(
            approved=approved,
            release_ready=release_ready,
            runtime_support=runtime_support,
            routing=routing,
            snapshot_checked=snapshot is not None,
            release_summary=release_summary,
        )
        readiness = readiness_by_id.get(model_id) or {}
        views.append(
            {
                "model_id": _safe_text(model_id),
                "status": _safe_text(status),
                "approved": approved,
                "release_ready": release_ready,
                "runtime": _safe_text(config.runtime),
                "runtime_support_status": _safe_text(runtime_support),
                "approval_ready": bool(readiness.get("approval_ready")),
                "missing_for_approval": _safe_json(readiness.get("missing_for_approval") or []),
                "blocked_gate_ids": _safe_json(release_summary.get("blocked_gate_ids") or []),
                "route_ready": route_ready,
                "routing": routing,
                "blockers": blockers,
                "recommended_next_action": _model_next_action(
                    approved=approved,
                    release_ready=release_ready,
                    runtime_support=runtime_support,
                    routing=routing,
                    snapshot_checked=snapshot is not None,
                ),
            }
        )
    return views


def _routing_for_model(*, snapshot: dict[str, Any] | None, model_id: str, job_type: str) -> dict[str, Any]:
    nodes = (snapshot or {}).get("nodes") or []
    live_node_count = 0
    eligible_node_count = 0
    live_eligible_node_count = 0
    legacy_node_count = 0
    samples: list[dict[str, Any]] = []
    if not isinstance(nodes, list):
        nodes = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        liveness = str(node.get("liveness_status") or "unknown")
        if liveness == "live":
            live_node_count += 1
        supported = [str(item) for item in node.get("supported_job_types") or [] if isinstance(item, str)]
        advertised_models = [str(item) for item in node.get("ollama_models") or [] if isinstance(item, str)]
        if not supported:
            legacy_node_count += 1
        eligible = job_type in supported and model_id in advertised_models
        if eligible:
            eligible_node_count += 1
            if liveness == "live":
                live_eligible_node_count += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "node_id_redacted": _redact_node_id(node.get("node_id")),
                        "liveness_status": _safe_text(liveness),
                        "software": software_metadata_public_view(node.get("software")),
                    }
                )
    warnings: list[str] = []
    if legacy_node_count:
        warnings.append("legacy_nodes_without_supported_job_types")
    return {
        "live_node_count": live_node_count,
        "eligible_node_count": eligible_node_count,
        "live_eligible_node_count": live_eligible_node_count,
        "legacy_node_count": legacy_node_count,
        "eligible_node_samples": samples,
        "warnings": warnings,
    }


def _summary(
    *,
    models: list[dict[str, Any]],
    selected: dict[str, Any] | None,
    snapshot_info: dict[str, Any],
    preferred_model: str | None,
    errors: list[str],
) -> dict[str, Any]:
    approved = [model for model in models if model.get("approved")]
    routeable = [model for model in models if model.get("route_ready")]
    live_capable = sum((model.get("routing") or {}).get("live_eligible_node_count", 0) for model in models)
    selected_model = (
        selected
        if not (preferred_model and selected is None)
        else None
    )
    if selected_model is None and not preferred_model:
        selected_model = routeable[0] if routeable else (approved[0] if approved else (models[0] if models else None))
    selected_route_ready = bool(selected_model and selected_model.get("route_ready"))
    next_action = _summary_next_action(
        errors=errors,
        selected_model=selected_model,
        routeable=routeable,
        approved=approved,
        network_status=str(snapshot_info.get("status")),
        preferred_model=preferred_model,
    )
    return {
        "selected_model_id": selected_model.get("model_id") if selected_model else None,
        "preferred_model": _safe_text(preferred_model),
        "recommended_chat_model": routeable[0].get("model_id") if routeable else None,
        "route_ready": selected_route_ready,
        "network_checked": snapshot_info.get("status") != "skipped",
        "coordinator_reachable": snapshot_info.get("ok") is True,
        "approved_model_count": len(approved),
        "routeable_model_count": len(routeable),
        "candidate_model_count": len([model for model in models if model.get("status") in {"candidate", "proposal"}]),
        "model_count": len(models),
        "live_model_capable_worker_count": live_capable,
        "recommended_next_action": next_action,
    }


def _select_model(models: list[dict[str, Any]], *, preferred_model: str | None) -> dict[str, Any] | None:
    if preferred_model:
        return next((model for model in models if model.get("model_id") == preferred_model), None)
    routeable = [model for model in models if model.get("route_ready")]
    if routeable:
        return routeable[0]
    approved = [model for model in models if model.get("approved")]
    if approved:
        return approved[0]
    return models[0] if models else None


def _summary_next_action(
    *,
    errors: list[str],
    selected_model: dict[str, Any] | None,
    routeable: list[dict[str, Any]],
    approved: list[dict[str, Any]],
    network_status: str,
    preferred_model: str | None,
) -> str:
    if errors:
        return "fix_model_route_plan_inputs"
    if preferred_model and selected_model is None:
        return "choose_registered_model"
    if routeable:
        return "continue_chat_session_with_route_plan"
    if network_status == "skipped":
        return "rerun_route_plan_with_network_checks"
    if network_status == "unreachable":
        return "start_or_check_local_coordinator"
    if not approved:
        return "approve_release_ready_model_before_routing"
    if selected_model:
        return str(selected_model.get("recommended_next_action") or "inspect_model_route_plan")
    return "inspect_model_route_plan"


def _model_next_action(
    *,
    approved: bool,
    release_ready: bool,
    runtime_support: str,
    routing: dict[str, Any],
    snapshot_checked: bool,
) -> str:
    if not release_ready:
        return "advance_model_release_pipeline"
    if not approved:
        return "review_and_promote_model_before_routing"
    if runtime_support != "verified":
        return "run_model_runtime_check"
    if not snapshot_checked:
        return "rerun_route_plan_with_network_checks"
    if routing["live_eligible_node_count"] < 1:
        return "wait_for_model_capable_worker_or_choose_model"
    return "continue_chat_session_with_route_plan"


def _model_blockers(
    *,
    approved: bool,
    release_ready: bool,
    runtime_support: str,
    routing: dict[str, Any],
    snapshot_checked: bool,
    release_summary: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if not release_ready:
        blockers.extend(str(item) for item in release_summary.get("blocked_gate_ids") or [])
    if not approved:
        blockers.append("model_not_approved")
    if runtime_support != "verified":
        blockers.append("runtime_not_verified")
    if not snapshot_checked:
        blockers.append("network_not_checked")
    elif routing["live_eligible_node_count"] < 1:
        blockers.append("no_live_model_capable_worker")
    return sorted(set(blockers))


def _runtime_support_status(model: dict[str, Any], *, runtime: str) -> str:
    runtimes = model.get("runtimes") if isinstance(model.get("runtimes"), list) else []
    for item in runtimes:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").lower() == runtime.lower():
            return str(item.get("support_status") or "unknown")
    return "unknown"


def _coordinator_view(snapshot_info: dict[str, Any], *, connection: dict[str, Any]) -> dict[str, Any]:
    snapshot = snapshot_info.get("snapshot") if isinstance(snapshot_info.get("snapshot"), dict) else {}
    status = snapshot.get("status") if isinstance(snapshot.get("status"), dict) else {}
    return {
        "checked": snapshot_info.get("status") != "skipped",
        "ok": snapshot_info.get("ok"),
        "status": snapshot_info.get("status"),
        "url": _safe_text(connection.get("coordinator_url")),
        "error": _safe_text(snapshot_info.get("error")),
        "snapshot_status": {
            "coordinator_id_redacted": _redact_node_id(status.get("coordinator_id")),
            "jobs": status.get("jobs"),
            "known_nodes": status.get("known_nodes"),
            "live_nodes": status.get("live_nodes"),
            "pending_jobs": status.get("pending_jobs"),
            "queued_jobs": status.get("queued_jobs"),
        },
        "invite": _safe_json(connection.get("invite_summary")),
    }


def _safe_path(path: Path | None) -> str | None:
    return _safe_text(str(path.expanduser().resolve())) if path is not None else None


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {_safe_text(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text


def _redact_node_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= 12:
        return value
    return f"{value[:10]}...{value[-4:]}"


def _markdown_table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _validate_config(config: ModelRoutePlanConfig) -> None:
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if not str(config.job_type).strip():
        raise ValueError("--job-type is required")
    if not str(config.runtime).strip():
        raise ValueError("--runtime is required")

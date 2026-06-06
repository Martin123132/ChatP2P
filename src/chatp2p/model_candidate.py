"""Structured candidate intake for the ChatP2P base model registry."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .model_registry import (
    MODEL_REGISTRY_RUNTIME_STATUSES,
    MODEL_REGISTRY_SCHEMA,
    default_model_registry,
    validate_model_registry,
)


MODEL_CANDIDATE_INTAKE_REPORT_SCHEMA = "chatp2p.model-candidate-intake-report.v1"
MODEL_CANDIDATE_ALLOWED_STATUSES = {"candidate", "proposal"}
MODEL_CANDIDATE_REQUIRED_EVALS = [
    "domain_eval",
    "regression_eval",
    "safety_eval",
    "license_review",
    "local_smoke",
]

_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
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
class ModelCandidateIntakeConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    model_id: str = ""
    provider: str | None = None
    project: str | None = None
    family: str = "base_chat_model"
    variant: str | None = None
    status: str | None = None
    license: str | None = None
    license_url: str | None = None
    source_url: str | None = None
    parameter_count_b: float | None = None
    architecture: str | None = None
    context_length_tokens: int | None = None
    domains: tuple[str, ...] = ()
    runtimes: tuple[str, ...] = ()
    min_ram_gb: float | None = None
    min_vram_gb: float | None = None
    recommended_capability_tier: str | None = None
    manifest_sha256: str | None = None
    weights_sha256: str | None = None
    quantization: str | None = None
    notes: str | None = None
    out_path: Path | None = None
    write: bool = False
    backup: bool = True


def run_model_candidate_intake(config: ModelCandidateIntakeConfig) -> dict[str, Any]:
    """Preview or write a structured candidate registry update."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    warnings: list[str] = []
    errors: list[str] = []
    input_errors = _validate_input(config)
    errors.extend(input_errors)

    registry, registry_status, load_warnings = _load_registry(registry_path)
    warnings.extend(load_warnings)
    existing_model = _find_model(registry, config.model_id) if config.model_id else None
    if isinstance(existing_model, dict) and existing_model.get("status") == "approved":
        errors.append("approved model entries cannot be modified by candidate intake")

    updated_registry = json.loads(json.dumps(registry))
    updated_model = _find_model(updated_registry, config.model_id) if config.model_id else None
    operation = "update" if isinstance(updated_model, dict) else "add"
    changes: list[dict[str, Any]] = []

    if not errors:
        candidate = _candidate_from_config(config, existing=updated_model)
        if updated_model is None:
            models = updated_registry.setdefault("models", [])
            if not isinstance(models, list):
                errors.append("registry models must be a list")
            else:
                models.append(candidate)
                changes.append({"field": "models", "status": "appended", "model_id": _safe_text(config.model_id)})
        else:
            changes.extend(_merge_candidate(updated_model, candidate))

    before_status = _safe_text(existing_model.get("status")) if isinstance(existing_model, dict) else None
    after_model = _find_model(updated_registry, config.model_id) if config.model_id else None
    after_status = _safe_text(after_model.get("status")) if isinstance(after_model, dict) else None
    if after_status == "approved" or before_status != "approved" and after_status == "approved":
        errors.append("candidate intake must not approve models")

    validation = validate_model_registry(updated_registry if not errors else registry)
    warnings.extend(validation["warnings"])
    if validation["errors"]:
        errors.extend(f"updated registry validation failed: {error}" for error in validation["errors"])

    write_result = {"requested": config.write, "status": "dry_run", "registry_path": str(registry_path)}
    if config.write and not errors:
        if registry_path.exists() and config.backup:
            backup_path = registry_path.with_suffix(registry_path.suffix + ".bak")
            backup_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
            write_result["backup_path"] = str(backup_path)
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps(updated_registry, indent=2, sort_keys=True), encoding="utf-8")
        write_result["status"] = "written"
    elif config.write and errors:
        write_result["status"] = "blocked"

    status = "fail" if errors else ("warn" if warnings else "pass")
    report: dict[str, Any] = {
        "schema": MODEL_CANDIDATE_INTAKE_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "dry_run": not config.write,
        "operation": operation,
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "model_id": _safe_text(config.model_id),
            "out_path": _safe_text(str(config.out_path.expanduser().resolve())) if config.out_path else None,
            "write": config.write,
            "backup": config.backup,
        },
        "registry_status": registry_status,
        "model": {
            "id": _safe_text(config.model_id),
            "status_before": before_status,
            "status_after": after_status,
            "approval_status_changed": before_status != after_status and after_status == "approved",
        },
        "summary": {
            "operation": operation,
            "change_count": len(changes),
            "does_not_approve_model": True,
            "recommended_next_action": _recommended_next_action(
                errors=errors,
                write=config.write,
                validation_summary=validation["summary"],
            ),
        },
        "candidate": _safe_candidate_view(after_model),
        "changes": changes,
        "write": write_result,
        "registry_validation_summary": validation["summary"],
        "warnings": warnings,
        "errors": errors,
    }
    if config.out_path is not None:
        out_path = config.out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report["artifacts"] = {"json": str(out_path)}
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def format_model_candidate_intake_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    model = report.get("model") or {}
    lines = [
        f"Model candidate intake: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {model.get('id')}",
        f"Mode: {'dry-run' if report.get('dry_run') else 'write'}",
        f"Operation: {summary.get('operation')}",
        f"Changes: {summary.get('change_count')}",
        f"Model status: {model.get('status_before')} -> {model.get('status_after')}",
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


def _load_registry(registry_path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if not registry_path.exists():
        return (
            default_model_registry(),
            {"source": "builtin_default", "exists": False, "schema": MODEL_REGISTRY_SCHEMA},
            ["registry_missing_using_builtin_default"],
        )
    registry = read_json_file(registry_path, description="model registry")
    if not isinstance(registry, dict):
        raise ValueError("model registry must be a JSON object")
    return registry, {"source": "file", "exists": True, "schema": registry.get("schema")}, []


def _validate_input(config: ModelCandidateIntakeConfig) -> list[str]:
    errors: list[str] = []
    if not config.model_id or not _SAFE_ID_RE.fullmatch(config.model_id):
        errors.append("model id must start with a letter and contain only letters, digits, _ . : -")
    if config.status is not None and config.status not in MODEL_CANDIDATE_ALLOWED_STATUSES:
        errors.append("candidate status must be candidate or proposal")
    if config.license_url and not config.license_url.startswith(("https://", "http://")):
        errors.append("license_url must be http(s)")
    if config.source_url and not config.source_url.startswith(("https://", "http://")):
        errors.append("source_url must be http(s)")
    if config.manifest_sha256 and not _SHA256_RE.fullmatch(config.manifest_sha256):
        errors.append("manifest_sha256 must be a 64-character sha256 hex string")
    if config.weights_sha256 and not _SHA256_RE.fullmatch(config.weights_sha256):
        errors.append("weights_sha256 must be a 64-character sha256 hex string")
    for runtime in config.runtimes:
        runtime_error = _validate_runtime_spec(runtime)
        if runtime_error:
            errors.append(runtime_error)
    return errors


def _candidate_from_config(config: ModelCandidateIntakeConfig, *, existing: dict[str, Any] | None) -> dict[str, Any]:
    existing = existing if isinstance(existing, dict) else {}
    candidate = {
        "id": config.model_id,
        "status": _value_or_existing(config.status, existing.get("status"), "candidate"),
        "provider": _value_or_existing(config.provider, existing.get("provider"), "to_be_selected"),
        "project": _value_or_existing(config.project, existing.get("project"), "open_weight_base_to_be_selected"),
        "family": _value_or_existing(config.family, existing.get("family"), "base_chat_model"),
        "variant": _value_or_existing(config.variant, existing.get("variant"), "TBD"),
        "license": _value_or_existing(config.license, existing.get("license"), "must_be_confirmed_before_approval"),
        "license_url": _value_or_existing(config.license_url, existing.get("license_url"), None),
        "source_url": _value_or_existing(config.source_url, existing.get("source_url"), None),
        "parameter_count_b": _value_or_existing(config.parameter_count_b, existing.get("parameter_count_b"), None),
        "architecture": _value_or_existing(config.architecture, existing.get("architecture"), "unknown"),
        "context_length_tokens": _value_or_existing(
            config.context_length_tokens,
            existing.get("context_length_tokens"),
            None,
        ),
        "domains": _clean_domains(config.domains or tuple(existing.get("domains", []) or ["general"])),
        "runtimes": _runtime_specs(config.runtimes, existing=existing.get("runtimes")),
        "hardware": {
            "min_ram_gb": _value_or_existing(
                config.min_ram_gb,
                (existing.get("hardware") or {}).get("min_ram_gb") if isinstance(existing.get("hardware"), dict) else None,
                None,
            ),
            "min_vram_gb": _value_or_existing(
                config.min_vram_gb,
                (existing.get("hardware") or {}).get("min_vram_gb") if isinstance(existing.get("hardware"), dict) else None,
                None,
            ),
            "recommended_capability_tier": _value_or_existing(
                config.recommended_capability_tier,
                (existing.get("hardware") or {}).get("recommended_capability_tier")
                if isinstance(existing.get("hardware"), dict)
                else None,
                "unknown",
            ),
        },
        "artifacts": {
            "manifest_sha256": _value_or_existing(
                config.manifest_sha256,
                (existing.get("artifacts") or {}).get("manifest_sha256") if isinstance(existing.get("artifacts"), dict) else None,
                "TBD",
            ),
            "weights_sha256": _value_or_existing(
                config.weights_sha256,
                (existing.get("artifacts") or {}).get("weights_sha256") if isinstance(existing.get("artifacts"), dict) else None,
                "TBD",
            ),
            "quantization": _value_or_existing(
                config.quantization,
                (existing.get("artifacts") or {}).get("quantization") if isinstance(existing.get("artifacts"), dict) else None,
                "TBD",
            ),
        },
        "eval_plan": existing.get("eval_plan") if isinstance(existing.get("eval_plan"), dict) else _default_eval_plan(),
        "governance": existing.get("governance") if isinstance(existing.get("governance"), dict) else _default_governance(),
        "notes": _value_or_existing(config.notes, existing.get("notes"), "Candidate added through structured intake."),
    }
    return candidate


def _merge_candidate(target: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key, value in candidate.items():
        if target.get(key) == value:
            continue
        target[key] = value
        changes.append({"field": key, "status": "updated", "value_status": _value_status(value)})
    return changes


def _find_model(registry: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    for model in registry.get("models", []):
        if isinstance(model, dict) and str(model.get("id") or "") == model_id:
            return model
    return None


def _validate_runtime_spec(spec: str) -> str | None:
    parsed = _parse_runtime_spec(spec)
    if parsed is None:
        return f"runtime spec must be id:status[:notes], got {spec!r}"
    if parsed["support_status"] not in MODEL_REGISTRY_RUNTIME_STATUSES:
        return f"runtime support status must be one of {', '.join(sorted(MODEL_REGISTRY_RUNTIME_STATUSES))}"
    return None


def _runtime_specs(specs: tuple[str, ...], *, existing: Any) -> list[dict[str, Any]]:
    if not specs and isinstance(existing, list) and existing:
        return [runtime for runtime in existing if isinstance(runtime, dict)]
    if not specs:
        return [
            {"id": "ollama", "support_status": "candidate", "notes": "verify local pull and chat smoke"},
            {"id": "llama.cpp", "support_status": "candidate", "notes": "verify quantized runtime support"},
        ]
    runtimes = []
    for spec in specs:
        parsed = _parse_runtime_spec(spec)
        if parsed is not None:
            runtimes.append(parsed)
    return runtimes


def _parse_runtime_spec(spec: str) -> dict[str, str] | None:
    parts = [part.strip() for part in str(spec or "").split(":", 2)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    runtime = {"id": parts[0], "support_status": parts[1]}
    runtime["notes"] = parts[2] if len(parts) > 2 and parts[2] else "runtime evidence pending"
    return runtime


def _default_eval_plan() -> dict[str, Any]:
    return {
        "required_evaluations": list(MODEL_CANDIDATE_REQUIRED_EVALS),
        "success_criteria": {
            "minimum_domain_pass_rate": None,
            "no_known_license_blocker": False,
            "local_chat_smoke_passes": False,
        },
        "completed_evaluations": [],
    }


def _default_governance() -> dict[str, Any]:
    return {
        "proposal_id": None,
        "review_status": "not_submitted",
        "rollback_plan": None,
        "approved_by": [],
    }


def _clean_domains(domains: tuple[str, ...]) -> list[str]:
    cleaned = []
    seen = set()
    for domain in domains:
        text = str(domain or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned or ["general"]


def _value_or_existing(value: Any, existing: Any, default: Any) -> Any:
    if value is not None:
        return value
    if existing is not None:
        return existing
    return default


def _safe_candidate_view(model: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(model, dict):
        return None
    hardware = model.get("hardware") if isinstance(model.get("hardware"), dict) else {}
    artifacts = model.get("artifacts") if isinstance(model.get("artifacts"), dict) else {}
    return {
        "id": _safe_text(model.get("id")),
        "status": _safe_text(model.get("status")),
        "provider": _safe_text(model.get("provider")),
        "project": _safe_text(model.get("project")),
        "family": _safe_text(model.get("family")),
        "variant": _safe_text(model.get("variant")),
        "license_present": bool(model.get("license")),
        "license_url_present": bool(model.get("license_url")),
        "source_url_present": bool(model.get("source_url")),
        "parameter_count_b": model.get("parameter_count_b"),
        "architecture": _safe_text(model.get("architecture")),
        "context_length_tokens": model.get("context_length_tokens"),
        "domains": [_safe_text(domain) for domain in model.get("domains", []) if isinstance(domain, str)],
        "runtime_count": len(model.get("runtimes", [])) if isinstance(model.get("runtimes"), list) else 0,
        "hardware": {
            "min_ram_gb": hardware.get("min_ram_gb"),
            "min_vram_gb": hardware.get("min_vram_gb"),
            "recommended_capability_tier": _safe_text(hardware.get("recommended_capability_tier")),
        },
        "artifacts": {
            "manifest_sha256_present": _hash_present(artifacts.get("manifest_sha256")),
            "weights_sha256_present": _hash_present(artifacts.get("weights_sha256")),
            "quantization": _safe_text(artifacts.get("quantization")),
        },
    }


def _hash_present(value: Any) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value or "")))


def _value_status(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    text = _safe_text(value) or ""
    if text.startswith(("https://", "http://")):
        return "url_present"
    if _SHA256_RE.fullmatch(text):
        return "sha256_present"
    return "text_present" if text else "empty"


def _recommended_next_action(*, errors: list[str], write: bool, validation_summary: dict[str, Any]) -> str:
    if errors:
        return "fix_model_candidate_input"
    if not write:
        return "rerun_candidate_intake_with_write"
    if validation_summary.get("placeholder_hash_count", 0) > 0:
        return "run_model_eval_and_verify_hashes"
    return "run_model_registry_validation"


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text

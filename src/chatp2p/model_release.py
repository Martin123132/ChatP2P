"""Read-only release gate for ChatP2P base model candidates."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .model_governance import (
    MODEL_GOVERNANCE_REGISTRY_SCHEMA,
    default_model_governance_registry,
    validate_model_governance_registry,
)
from .model_registry import (
    MODEL_REGISTRY_REQUIRED_EVALS,
    MODEL_REGISTRY_SCHEMA,
    default_model_registry,
    validate_model_registry,
)


MODEL_RELEASE_CHECK_REPORT_SCHEMA = "chatp2p.model-release-check-report.v1"
MODEL_RELEASE_ALLOWED_STATUSES = {"candidate", "proposal", "approved"}

_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_PLACEHOLDER_VALUES = {"", "TBD", "UNKNOWN", "TO_BE_SELECTED", "MUST_BE_CONFIRMED_BEFORE_APPROVAL"}
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
class ModelReleaseCheckConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    governance_path: Path = Path(".mesh/model-governance.json")
    model_id: str = "chatp2p-base-candidate-v0"
    out_path: Path | None = None


def run_model_release_check(config: ModelReleaseCheckConfig) -> dict[str, Any]:
    """Check whether a model candidate is ready for release without changing files."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    governance_path = config.governance_path.expanduser().resolve()
    warnings: list[str] = []
    errors: list[str] = []

    registry, registry_status, registry_warnings = _load_model_registry(registry_path)
    governance, governance_status, governance_warnings = _load_governance_registry(governance_path)
    warnings.extend(registry_warnings)
    warnings.extend(governance_warnings)

    registry_validation = validate_model_registry(registry)
    governance_validation = validate_model_governance_registry(governance)
    errors.extend(f"model registry: {error}" for error in registry_validation["errors"])
    errors.extend(f"model governance: {error}" for error in governance_validation["errors"])
    warnings.extend(f"model registry: {warning}" for warning in registry_validation["warnings"])
    warnings.extend(f"model governance: {warning}" for warning in governance_validation["warnings"])

    model = _find_model(registry, config.model_id)
    if model is None:
        errors.append(f"model_id not found in registry: {config.model_id}")

    gates = _release_gates(
        model=model,
        registry_validation=registry_validation,
        governance=governance,
        governance_validation=governance_validation,
    )
    blocker_gates = [gate for gate in gates if gate["status"] == "fail"]
    release_ready = model is not None and not errors and not blocker_gates
    status = "fail" if errors else ("pass" if release_ready else "warn")
    report: dict[str, Any] = {
        "schema": MODEL_RELEASE_CHECK_REPORT_SCHEMA,
        "ok": status != "fail",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "governance_path": _safe_text(str(governance_path)),
            "model_id": _safe_text(config.model_id),
            "out_path": _safe_text(str(config.out_path.expanduser().resolve())) if config.out_path else None,
        },
        "registry_status": registry_status,
        "governance_status": governance_status,
        "model": _safe_model_view(model),
        "summary": {
            "release_ready": release_ready,
            "gate_count": len(gates),
            "passed_gate_count": len([gate for gate in gates if gate["status"] == "pass"]),
            "failed_gate_count": len(blocker_gates),
            "blocked_gate_ids": [gate["id"] for gate in blocker_gates],
            "does_not_approve_model": True,
            "recommended_next_action": _recommended_next_action(errors=errors, gates=gates),
        },
        "gates": gates,
        "registry_validation_summary": registry_validation["summary"],
        "governance_validation_summary": governance_validation["summary"],
        "warnings": warnings,
        "errors": errors,
    }
    if config.out_path is not None:
        out_path = config.out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report["artifacts"] = {"json": str(out_path)}
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def format_model_release_check_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    model = report.get("model") or {}
    lines = [
        f"Model release check: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {model.get('id')}",
        f"Release ready: {summary.get('release_ready')}",
        f"Gates: {summary.get('passed_gate_count')}/{summary.get('gate_count')} passed",
        f"Blocked: {', '.join(summary.get('blocked_gate_ids') or []) or 'none'}",
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


def _load_model_registry(path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
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


def _load_governance_registry(path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
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


def _release_gates(
    *,
    model: dict[str, Any] | None,
    registry_validation: dict[str, Any],
    governance: dict[str, Any],
    governance_validation: dict[str, Any],
) -> list[dict[str, Any]]:
    gates = [
        _gate(
            "model_exists",
            model is not None,
            "Model exists in registry",
            "model_id was found",
            "model_id is missing from registry",
        ),
        _gate(
            "registry_valid",
            registry_validation["ok"],
            "Model registry has no validation errors",
            "registry validation passed",
            "registry validation has errors",
        ),
    ]
    if model is None:
        gates.extend(_missing_model_gates())
    else:
        gates.extend(
            [
                _model_status_gate(model),
                _license_gate(model),
                _source_gate(model),
                _shape_gate(model),
                _runtime_gate(model),
                _hardware_gate(model),
                _artifact_gate(model),
                _eval_gate(model),
                _model_governance_gate(model),
            ]
        )
    gates.extend(
        [
            _gate(
                "governance_registry_valid",
                governance_validation["ok"],
                "Governance registry has no validation errors",
                "governance registry validation passed",
                "governance registry validation has errors",
            ),
            _governance_policy_gate(governance),
            _governance_weight_pack_gate(governance, model),
        ]
    )
    return gates


def _missing_model_gates() -> list[dict[str, Any]]:
    return [
        _gate("model_status", False, "Model status is releasable", "", "model is missing"),
        _gate("license", False, "License is confirmed", "", "model is missing"),
        _gate("source", False, "Source URL is present", "", "model is missing"),
        _gate("model_shape", False, "Model shape metadata is complete", "", "model is missing"),
        _gate("runtime", False, "At least one runtime is verified", "", "model is missing"),
        _gate("hardware", False, "Hardware requirements are complete", "", "model is missing"),
        _gate("artifacts", False, "Artifact hashes are present", "", "model is missing"),
        _gate("eval_evidence", False, "Required eval evidence is complete", "", "model is missing"),
        _gate("model_governance_review", False, "Model governance review is approved", "", "model is missing"),
    ]


def _model_status_gate(model: dict[str, Any]) -> dict[str, Any]:
    status = str(model.get("status") or "")
    return _gate(
        "model_status",
        status in MODEL_RELEASE_ALLOWED_STATUSES,
        "Model status is releasable",
        f"status is {status}",
        f"status is {status or '<missing>'}; expected candidate, proposal, or approved",
    )


def _license_gate(model: dict[str, Any]) -> dict[str, Any]:
    license_ready = _field_ready(model.get("license"))
    license_url_ready = _url_ready(model.get("license_url"))
    return _gate(
        "license",
        license_ready and license_url_ready,
        "License is confirmed",
        "license name and URL are present",
        "license name or license URL is missing",
        evidence={"license_present": license_ready, "license_url_present": license_url_ready},
    )


def _source_gate(model: dict[str, Any]) -> dict[str, Any]:
    source_ready = _url_ready(model.get("source_url"))
    return _gate(
        "source",
        source_ready,
        "Source URL is present",
        "source URL is present",
        "source URL is missing or invalid",
    )


def _shape_gate(model: dict[str, Any]) -> dict[str, Any]:
    parameter_ready = _positive_number(model.get("parameter_count_b"))
    context_ready = _positive_int(model.get("context_length_tokens"))
    architecture_ready = _field_ready(model.get("architecture"))
    domains_ready = bool([domain for domain in model.get("domains", []) if isinstance(domain, str) and domain])
    return _gate(
        "model_shape",
        parameter_ready and context_ready and architecture_ready and domains_ready,
        "Model shape metadata is complete",
        "parameter count, context length, architecture, and domains are present",
        "parameter count, context length, architecture, or domains are incomplete",
        evidence={
            "parameter_count_present": parameter_ready,
            "context_length_present": context_ready,
            "architecture_present": architecture_ready,
            "domain_count": len(model.get("domains", [])) if isinstance(model.get("domains"), list) else 0,
        },
    )


def _runtime_gate(model: dict[str, Any]) -> dict[str, Any]:
    runtimes = model.get("runtimes") if isinstance(model.get("runtimes"), list) else []
    verified = [runtime for runtime in runtimes if isinstance(runtime, dict) and runtime.get("support_status") == "verified"]
    return _gate(
        "runtime",
        bool(verified),
        "At least one runtime is verified",
        f"{len(verified)} verified runtime(s)",
        "no verified runtime support is recorded",
        evidence={"verified_runtime_count": len(verified)},
    )


def _hardware_gate(model: dict[str, Any]) -> dict[str, Any]:
    hardware = model.get("hardware") if isinstance(model.get("hardware"), dict) else {}
    ram_ready = _positive_number(hardware.get("min_ram_gb"))
    tier = str(hardware.get("recommended_capability_tier") or "").strip()
    tier_ready = bool(tier and tier != "unknown")
    return _gate(
        "hardware",
        ram_ready and tier_ready,
        "Hardware requirements are complete",
        "minimum RAM and capability tier are present",
        "minimum RAM or capability tier is missing",
        evidence={"min_ram_gb_present": ram_ready, "recommended_capability_tier_present": tier_ready},
    )


def _artifact_gate(model: dict[str, Any]) -> dict[str, Any]:
    artifacts = model.get("artifacts") if isinstance(model.get("artifacts"), dict) else {}
    manifest_ready = _hash_ready(artifacts.get("manifest_sha256"))
    weights_ready = _hash_ready(artifacts.get("weights_sha256"))
    quantization_ready = _field_ready(artifacts.get("quantization"))
    return _gate(
        "artifacts",
        manifest_ready and weights_ready and quantization_ready,
        "Artifact hashes are present",
        "manifest hash, weights hash, and quantization are present",
        "manifest hash, weights hash, or quantization is missing",
        evidence={
            "manifest_sha256_present": manifest_ready,
            "weights_sha256_present": weights_ready,
            "quantization_present": quantization_ready,
        },
    )


def _eval_gate(model: dict[str, Any]) -> dict[str, Any]:
    eval_plan = model.get("eval_plan") if isinstance(model.get("eval_plan"), dict) else {}
    completed = {str(item) for item in eval_plan.get("completed_evaluations", []) if isinstance(item, str)}
    missing = sorted(MODEL_REGISTRY_REQUIRED_EVALS - completed)
    criteria = eval_plan.get("success_criteria") if isinstance(eval_plan.get("success_criteria"), dict) else {}
    criteria_ready = (
        criteria.get("no_known_license_blocker") is True
        and criteria.get("local_chat_smoke_passes") is True
        and _positive_number(criteria.get("minimum_domain_pass_rate"))
    )
    return _gate(
        "eval_evidence",
        not missing and criteria_ready,
        "Required eval evidence is complete",
        "all required evals and success criteria are complete",
        "required evals or success criteria are incomplete",
        evidence={"missing_evaluations": missing, "success_criteria_ready": criteria_ready},
    )


def _model_governance_gate(model: dict[str, Any]) -> dict[str, Any]:
    governance = model.get("governance") if isinstance(model.get("governance"), dict) else {}
    ready = (
        bool(governance.get("proposal_id"))
        and governance.get("review_status") == "approved"
        and bool(governance.get("rollback_plan"))
        and isinstance(governance.get("approved_by"), list)
        and bool(governance.get("approved_by"))
    )
    return _gate(
        "model_governance_review",
        ready,
        "Model governance review is approved",
        "proposal, approval, rollback plan, and approvers are present",
        "model governance review evidence is incomplete",
        evidence={
            "proposal_present": bool(governance.get("proposal_id")),
            "review_status": _safe_text(governance.get("review_status")),
            "rollback_plan_present": bool(governance.get("rollback_plan")),
            "approved_by_count": len(governance.get("approved_by", [])) if isinstance(governance.get("approved_by"), list) else 0,
        },
    )


def _governance_policy_gate(governance: dict[str, Any]) -> dict[str, Any]:
    policy = governance.get("weight_pack_policy") if isinstance(governance.get("weight_pack_policy"), dict) else {}
    ready = policy.get("approved_pack_required") is True and policy.get("core_weight_edits_allowed") is False
    return _gate(
        "governance_policy",
        ready,
        "Governance policy keeps core weights locked",
        "approved packs are required and core weight edits are disabled",
        "approved pack requirement or core-weight lock is missing",
        evidence={
            "approved_pack_required": bool(policy.get("approved_pack_required")),
            "core_weight_edits_allowed": bool(policy.get("core_weight_edits_allowed")),
        },
    )


def _governance_weight_pack_gate(governance: dict[str, Any], model: dict[str, Any] | None) -> dict[str, Any]:
    model_id = str((model or {}).get("id") or "")
    packs = governance.get("weight_packs") if isinstance(governance.get("weight_packs"), list) else []
    approved_matches = [
        pack
        for pack in packs
        if isinstance(pack, dict)
        and pack.get("status") == "approved"
        and (pack.get("base_model") == model_id or pack.get("id") == model_id)
        and _hash_ready(pack.get("manifest_sha256"))
        and _hash_ready(pack.get("weights_sha256"))
        and pack.get("core_weight_editable") is False
    ]
    return _gate(
        "governance_weight_pack",
        bool(approved_matches),
        "Approved governance weight pack exists",
        "an approved, hashed, non-editable governance pack matches the model",
        "no approved, hashed, non-editable governance pack matches the model",
        evidence={"matching_approved_pack_count": len(approved_matches)},
    )


def _gate(
    gate_id: str,
    condition: bool,
    label: str,
    pass_reason: str,
    fail_reason: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "status": "pass" if condition else "fail",
        "reason": pass_reason if condition else fail_reason,
        "evidence": evidence or {},
    }


def _recommended_next_action(*, errors: list[str], gates: list[dict[str, Any]]) -> str:
    if errors:
        return "fix_model_release_inputs"
    failed_ids = [gate["id"] for gate in gates if gate["status"] == "fail"]
    priority = [
        ("model_exists", "add_model_candidate"),
        ("registry_valid", "fix_model_registry"),
        ("license", "confirm_model_license"),
        ("source", "confirm_model_source"),
        ("model_shape", "complete_model_metadata"),
        ("runtime", "verify_local_runtime_support"),
        ("hardware", "complete_hardware_requirements"),
        ("artifacts", "verify_model_hashes"),
        ("eval_evidence", "run_model_eval_and_attach_evidence"),
        ("model_governance_review", "submit_candidate_for_governance_review"),
        ("governance_registry_valid", "fix_model_governance_registry"),
        ("governance_policy", "fix_model_governance_policy"),
        ("governance_weight_pack", "approve_matching_governance_weight_pack"),
    ]
    for gate_id, action in priority:
        if gate_id in failed_ids:
            return action
    return "promote_model_through_governance_release"


def _find_model(registry: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    for model in registry.get("models", []):
        if isinstance(model, dict) and str(model.get("id") or "") == model_id:
            return model
    return None


def _safe_model_view(model: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(model, dict):
        return None
    artifacts = model.get("artifacts") if isinstance(model.get("artifacts"), dict) else {}
    hardware = model.get("hardware") if isinstance(model.get("hardware"), dict) else {}
    return {
        "id": _safe_text(model.get("id")),
        "status": _safe_text(model.get("status")),
        "provider": _safe_text(model.get("provider")),
        "project": _safe_text(model.get("project")),
        "family": _safe_text(model.get("family")),
        "variant": _safe_text(model.get("variant")),
        "license_present": _field_ready(model.get("license")),
        "license_url_present": _url_ready(model.get("license_url")),
        "source_url_present": _url_ready(model.get("source_url")),
        "parameter_count_b": model.get("parameter_count_b"),
        "context_length_tokens": model.get("context_length_tokens"),
        "domains": [_safe_text(domain) for domain in model.get("domains", []) if isinstance(domain, str)],
        "hardware": {
            "min_ram_gb": hardware.get("min_ram_gb"),
            "min_vram_gb": hardware.get("min_vram_gb"),
            "recommended_capability_tier": _safe_text(hardware.get("recommended_capability_tier")),
        },
        "artifacts": {
            "manifest_sha256_present": _hash_ready(artifacts.get("manifest_sha256")),
            "weights_sha256_present": _hash_ready(artifacts.get("weights_sha256")),
            "quantization": _safe_text(artifacts.get("quantization")),
        },
    }


def _field_ready(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.upper() not in _PLACEHOLDER_VALUES and not text.lower().startswith("must_be_confirmed")


def _url_ready(value: Any) -> bool:
    text = str(value or "").strip()
    return text.startswith(("https://", "http://"))


def _hash_ready(value: Any) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value or "")))


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text

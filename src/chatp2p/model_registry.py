"""Base model candidate registry for ChatP2P."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file


MODEL_REGISTRY_SCHEMA = "chatp2p.model-registry.v1"
MODEL_REGISTRY_REPORT_SCHEMA = "chatp2p.model-registry-report.v1"
MODEL_REGISTRY_DEFAULT_ID = "chatp2p-default-base-model-registry-v0"
MODEL_REGISTRY_REQUIRED_EVALS = {
    "domain_eval",
    "regression_eval",
    "safety_eval",
    "license_review",
    "local_smoke",
}
MODEL_REGISTRY_APPROVABLE_STATUSES = {"candidate", "proposal"}
MODEL_REGISTRY_STATUSES = {"candidate", "proposal", "approved", "deprecated", "quarantined", "rejected"}
MODEL_REGISTRY_RUNTIME_STATUSES = {"candidate", "verified", "unsupported"}
_SAFE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_PLACEHOLDER_VALUES = {"", "TBD", "UNKNOWN", "TO_BE_SELECTED", "MUST_BE_CONFIRMED_BEFORE_APPROVAL"}
_SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "tailscale_auth_key": re.compile(r"\btskey-[A-Za-z0-9_-]+\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "alpha_token": re.compile(r"\balpha-token-[A-Za-z0-9_-]{8,}\b"),
    "credit_grant_token": re.compile(r"\bcredit-grant-token-[A-Za-z0-9_-]{8,}\b"),
    "long_admission_token": re.compile(r"""admission_token["']?\s*[:=]\s*["'][^"']{20,}["']"""),
    "long_credit_grant_token": re.compile(r"""credit_grant_token["']?\s*[:=]\s*["'][^"']{20,}["']"""),
}


@dataclass(frozen=True)
class ModelRegistryConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    out_path: Path | None = None
    init: bool = False
    force: bool = False


def default_model_registry() -> dict[str, Any]:
    """Return the built-in starter registry for base model candidate selection."""

    return {
        "schema": MODEL_REGISTRY_SCHEMA,
        "registry_id": MODEL_REGISTRY_DEFAULT_ID,
        "version": "0.1.0",
        "summary": {
            "purpose": "Track candidate open-weight base models before ChatP2P serves them.",
            "policy": "No model is approved without license evidence, hashes, runtime support, and eval plan.",
            "recommended_next_action": "fill_first_candidate_metadata",
        },
        "selection_policy": {
            "approved_model_required_for_default_routing": True,
            "approval_requires": [
                "confirmed_license",
                "weights_sha256",
                "manifest_sha256",
                "runtime_verified",
                "domain_eval",
                "regression_eval",
                "safety_eval",
                "license_review",
                "local_smoke",
                "rollback_plan",
            ],
            "default_preference_order": [
                "fits_standard_gpu",
                "permissive_license",
                "good_local_runtime_support",
                "strong_eval_delta",
                "small_enough_for_contributors",
            ],
        },
        "domains": ["general", "maths", "science", "coding", "philosophy", "safety"],
        "models": [
            {
                "id": "chatp2p-base-candidate-v0",
                "status": "candidate",
                "provider": "to_be_selected",
                "project": "open_weight_base_to_be_selected",
                "family": "base_chat_model",
                "variant": "TBD",
                "license": "must_be_confirmed_before_approval",
                "license_url": None,
                "source_url": None,
                "parameter_count_b": None,
                "architecture": "unknown",
                "context_length_tokens": None,
                "domains": ["general"],
                "runtimes": [
                    {"id": "ollama", "support_status": "candidate", "notes": "verify local pull and chat smoke"},
                    {"id": "llama.cpp", "support_status": "candidate", "notes": "verify quantized runtime support"},
                ],
                "hardware": {
                    "min_ram_gb": None,
                    "min_vram_gb": None,
                    "recommended_capability_tier": "unknown",
                },
                "artifacts": {
                    "manifest_sha256": "TBD",
                    "weights_sha256": "TBD",
                    "quantization": "TBD",
                },
                "eval_plan": {
                    "required_evaluations": [
                        "domain_eval",
                        "regression_eval",
                        "safety_eval",
                        "license_review",
                        "local_smoke",
                    ],
                    "success_criteria": {
                        "minimum_domain_pass_rate": None,
                        "no_known_license_blocker": True,
                        "local_chat_smoke_passes": False,
                    },
                    "completed_evaluations": [],
                },
                "governance": {
                    "proposal_id": None,
                    "review_status": "not_submitted",
                    "rollback_plan": None,
                    "approved_by": [],
                },
                "notes": "Placeholder candidate. Replace with a real open-weight model only after license and runtime checks.",
            }
        ],
    }


def run_model_registry(config: ModelRegistryConfig) -> dict[str, Any]:
    """Inspect or initialize the local base model candidate registry."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    init_result = _maybe_init_registry(config=config, registry_path=registry_path)
    registry, load_status, load_warnings = _load_registry(registry_path=registry_path)
    validation = validate_model_registry(registry)
    warnings = [*init_result["warnings"], *load_warnings, *validation["warnings"]]
    errors = [*init_result["errors"], *validation["errors"]]
    report = {
        "schema": MODEL_REGISTRY_REPORT_SCHEMA,
        "ok": not errors,
        "status": "fail" if errors else ("warn" if warnings else "pass"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "registry_path": str(registry_path),
            "out_path": str(config.out_path.expanduser().resolve()) if config.out_path else None,
            "init": config.init,
            "force": config.force,
        },
        "init": init_result,
        "registry_status": load_status,
        "summary": {
            **validation["summary"],
            "recommended_next_action": _recommended_next_action(
                errors=errors,
                warnings=warnings,
                validation_summary=validation["summary"],
            ),
        },
        "registry": _safe_registry_view(registry),
        "model_readiness": validation["model_readiness"],
        "warnings": warnings,
        "errors": errors,
    }
    if config.out_path is not None:
        out_path = config.out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        report["artifacts"] = {"json": str(out_path)}
    return report


def validate_model_registry(registry: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(registry, dict):
        return {
            "ok": False,
            "summary": _empty_summary(),
            "model_readiness": [],
            "warnings": [],
            "errors": ["registry must be a JSON object"],
        }
    if registry.get("schema") != MODEL_REGISTRY_SCHEMA:
        errors.append(f"schema must be {MODEL_REGISTRY_SCHEMA}")

    sensitive_findings = _sensitive_findings(registry)
    errors.extend(f"sensitive value detected at {finding['path']} ({finding['kind']})" for finding in sensitive_findings)

    models = _list_at(registry, "models")
    model_ids = _unique_ids(models, errors=errors, field_name="models")
    if not models:
        warnings.append("no model candidates are defined")

    readiness = [
        _model_readiness(model, index=index)
        for index, model in enumerate(models)
        if isinstance(model, dict)
    ]
    for item in readiness:
        warnings.extend(item["warnings"])
        errors.extend(item["errors"])

    approved = [item for item in readiness if item["status"] == "approved"]
    approval_ready = [item for item in readiness if item["approval_ready"]]
    candidate = _best_next_candidate(readiness)
    summary = {
        "model_count": len(models),
        "candidate_count": len([item for item in readiness if item["status"] in MODEL_REGISTRY_APPROVABLE_STATUSES]),
        "approved_model_count": len(approved),
        "approval_ready_count": len(approval_ready),
        "placeholder_hash_count": sum(item["placeholder_hash_count"] for item in readiness),
        "sensitive_finding_count": len(sensitive_findings),
        "best_next_candidate": candidate["id"] if candidate else None,
        "model_ids": model_ids,
    }
    if not approved:
        warnings.append("no approved base model exists")
    return {
        "ok": not errors,
        "summary": summary,
        "model_readiness": readiness,
        "warnings": warnings,
        "errors": errors,
    }


def format_model_registry_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model registry: {str(report.get('status', 'unknown')).upper()}",
        f"Registry: {(report.get('config') or {}).get('registry_path')}",
        f"Models: {summary.get('model_count')} approved {summary.get('approved_model_count')}",
        f"Approval-ready: {summary.get('approval_ready_count')}",
        f"Best next candidate: {summary.get('best_next_candidate')}",
        f"Placeholder hashes: {summary.get('placeholder_hash_count')}",
        f"Next: {summary.get('recommended_next_action')}",
    ]
    if (report.get("init") or {}).get("status") != "not_requested":
        lines.append(f"Init: {(report.get('init') or {}).get('status')}")
    if report.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    if (report.get("artifacts") or {}).get("json"):
        lines.append(f"Report: {(report.get('artifacts') or {}).get('json')}")
    return "\n".join(lines)


def _maybe_init_registry(*, config: ModelRegistryConfig, registry_path: Path) -> dict[str, Any]:
    result = {"requested": config.init, "status": "not_requested", "path": str(registry_path), "warnings": [], "errors": []}
    if not config.init:
        return result
    if registry_path.exists() and not config.force:
        result["status"] = "exists"
        result["warnings"].append("registry_exists_use_force_to_replace")
        return result
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(default_model_registry(), indent=2, sort_keys=True), encoding="utf-8")
    result["status"] = "written"
    return result


def _load_registry(*, registry_path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if not registry_path.exists():
        return (
            default_model_registry(),
            {"source": "builtin_default", "exists": False},
            ["registry_missing_using_builtin_default"],
        )
    registry = read_json_file(registry_path, description="model registry")
    if not isinstance(registry, dict):
        raise ValueError("model registry must be a JSON object")
    return registry, {"source": "file", "exists": True}, []


def _model_readiness(model: dict[str, Any], *, index: int) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    model_id = str(model.get("id") or f"model[{index}]")
    status = str(model.get("status") or "")
    if not _SAFE_ID_RE.fullmatch(model_id):
        errors.append(f"{model_id} has invalid model id")
    if status not in MODEL_REGISTRY_STATUSES:
        errors.append(f"{model_id} status must be one of {', '.join(sorted(MODEL_REGISTRY_STATUSES))}")

    license_status = _field_status(model.get("license"))
    license_url_status = _url_status(model.get("license_url"))
    source_url_status = _url_status(model.get("source_url"))
    parameter_status = _positive_number_status(model.get("parameter_count_b"))
    context_status = _positive_int_status(model.get("context_length_tokens"))
    runtime_status = _runtime_status(model.get("runtimes"))
    hardware_status = _hardware_status(model.get("hardware"))
    artifact_status = _artifact_status(model.get("artifacts"))
    eval_status = _eval_status(model.get("eval_plan"))
    governance_status = _governance_status(model.get("governance"))
    domain_status = _domain_status(model.get("domains"))

    checks = {
        "license": license_status,
        "license_url": license_url_status,
        "source_url": source_url_status,
        "parameter_count_b": parameter_status,
        "context_length_tokens": context_status,
        "runtimes": runtime_status["status"],
        "hardware": hardware_status,
        "artifacts": artifact_status["status"],
        "eval_plan": eval_status["status"],
        "governance": governance_status,
        "domains": domain_status,
    }
    missing = [name for name, check_status in checks.items() if check_status != "ready"]
    placeholder_hash_count = artifact_status["placeholder_hash_count"]
    if placeholder_hash_count:
        warnings.append(f"{model_id} has placeholder hashes")
    if license_url_status == "invalid":
        errors.append(f"{model_id} license_url must be http(s)")
    if source_url_status == "invalid":
        errors.append(f"{model_id} source_url must be http(s)")
    if status == "approved":
        for name in missing:
            errors.append(f"{model_id} cannot be approved until {name} is ready")
    elif status in MODEL_REGISTRY_APPROVABLE_STATUSES and not missing:
        warnings.append(f"{model_id} is approval-ready but status is {status}")

    return {
        "id": model_id,
        "status": status or "unknown",
        "approval_ready": not missing,
        "missing_for_approval": missing,
        "checks": checks,
        "runtime_verified_count": runtime_status["verified_count"],
        "required_eval_count": eval_status["required_count"],
        "completed_eval_count": eval_status["completed_count"],
        "placeholder_hash_count": placeholder_hash_count,
        "recommended_next_action": _model_next_action(status=status, missing=missing),
        "warnings": warnings,
        "errors": errors,
    }


def _recommended_next_action(
    *,
    errors: list[str],
    warnings: list[str],
    validation_summary: dict[str, Any],
) -> str:
    if errors:
        return "fix_model_registry"
    if validation_summary.get("approved_model_count", 0) <= 0:
        if validation_summary.get("approval_ready_count", 0) > 0:
            return "submit_candidate_for_governance_approval"
        return "fill_first_candidate_metadata"
    if validation_summary.get("placeholder_hash_count", 0) > 0:
        return "verify_model_hashes"
    if warnings:
        return "review_model_registry_warnings"
    return "publish_model_registry_for_routing"


def _model_next_action(*, status: str, missing: list[str]) -> str:
    if status == "approved" and missing:
        return "fix_approved_model_evidence"
    if "license" in missing or "license_url" in missing:
        return "confirm_model_license"
    if "artifacts" in missing:
        return "verify_model_hashes"
    if "runtimes" in missing:
        return "verify_local_runtime_support"
    if "eval_plan" in missing:
        return "run_model_eval_harness"
    if "governance" in missing:
        return "submit_candidate_for_governance_review"
    if not missing and status != "approved":
        return "submit_candidate_for_governance_approval"
    return "publish_model_for_routing"


def _best_next_candidate(readiness: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [item for item in readiness if item["status"] in MODEL_REGISTRY_APPROVABLE_STATUSES]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (len(item["missing_for_approval"]), item["id"]))[0]


def _safe_registry_view(registry: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": registry.get("schema"),
        "registry_id": _safe_text(registry.get("registry_id")),
        "version": _safe_text(registry.get("version")),
        "selection_policy": _safe_selection_policy(registry.get("selection_policy")),
        "domains": [_safe_text(domain) for domain in _list_at(registry, "domains")],
        "models": [_safe_model(model) for model in _list_at(registry, "models")],
    }


def _safe_selection_policy(policy: Any) -> dict[str, Any]:
    policy = policy if isinstance(policy, dict) else {}
    return {
        "approved_model_required_for_default_routing": bool(
            policy.get("approved_model_required_for_default_routing", False)
        ),
        "approval_requires": [
            _safe_text(item) for item in policy.get("approval_requires", []) if isinstance(item, str)
        ],
        "default_preference_order": [
            _safe_text(item) for item in policy.get("default_preference_order", []) if isinstance(item, str)
        ],
    }


def _safe_model(model: Any) -> dict[str, Any]:
    model = model if isinstance(model, dict) else {}
    artifacts = model.get("artifacts") if isinstance(model.get("artifacts"), dict) else {}
    return {
        "id": _safe_text(model.get("id")),
        "status": _safe_text(model.get("status")),
        "provider": _safe_text(model.get("provider")),
        "project": _safe_text(model.get("project")),
        "family": _safe_text(model.get("family")),
        "variant": _safe_text(model.get("variant")),
        "license": _safe_text(model.get("license")),
        "license_url_present": bool(model.get("license_url")),
        "source_url_present": bool(model.get("source_url")),
        "parameter_count_b": model.get("parameter_count_b"),
        "architecture": _safe_text(model.get("architecture")),
        "context_length_tokens": model.get("context_length_tokens"),
        "domains": [_safe_text(item) for item in model.get("domains", []) if isinstance(item, str)],
        "runtimes": [_safe_runtime(runtime) for runtime in model.get("runtimes", []) if isinstance(runtime, dict)],
        "hardware": model.get("hardware") if isinstance(model.get("hardware"), dict) else {},
        "artifacts": {
            "manifest_sha256_status": _hash_status(_safe_text(artifacts.get("manifest_sha256"))),
            "weights_sha256_status": _hash_status(_safe_text(artifacts.get("weights_sha256"))),
            "quantization": _safe_text(artifacts.get("quantization")),
        },
        "eval_plan": _safe_eval_plan(model.get("eval_plan")),
        "governance": _safe_governance(model.get("governance")),
    }


def _safe_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_text(runtime.get("id")),
        "support_status": _safe_text(runtime.get("support_status")),
        "notes": _safe_text(runtime.get("notes")),
    }


def _safe_eval_plan(eval_plan: Any) -> dict[str, Any]:
    eval_plan = eval_plan if isinstance(eval_plan, dict) else {}
    return {
        "required_evaluations": [
            _safe_text(item) for item in eval_plan.get("required_evaluations", []) if isinstance(item, str)
        ],
        "completed_evaluations": [
            _safe_text(item) for item in eval_plan.get("completed_evaluations", []) if isinstance(item, str)
        ],
        "success_criteria_present": isinstance(eval_plan.get("success_criteria"), dict),
    }


def _safe_governance(governance: Any) -> dict[str, Any]:
    governance = governance if isinstance(governance, dict) else {}
    return {
        "proposal_id": _safe_text(governance.get("proposal_id")),
        "review_status": _safe_text(governance.get("review_status")),
        "rollback_plan_present": bool(governance.get("rollback_plan")),
        "approved_by_count": len(governance.get("approved_by", []))
        if isinstance(governance.get("approved_by"), list)
        else 0,
    }


def _field_status(value: Any) -> str:
    text = str(value or "").strip()
    if text.upper() in _PLACEHOLDER_VALUES or text.lower().startswith("must_be_confirmed"):
        return "missing"
    return "ready"


def _url_status(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.upper() in _PLACEHOLDER_VALUES:
        return "missing"
    if text.startswith(("https://", "http://")):
        return "ready"
    return "invalid"


def _positive_number_status(value: Any) -> str:
    try:
        return "ready" if float(value) > 0 else "missing"
    except (TypeError, ValueError):
        return "missing"


def _positive_int_status(value: Any) -> str:
    try:
        return "ready" if int(value) > 0 else "missing"
    except (TypeError, ValueError):
        return "missing"


def _runtime_status(value: Any) -> dict[str, Any]:
    runtimes = value if isinstance(value, list) else []
    verified = 0
    invalid = 0
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            invalid += 1
            continue
        if str(runtime.get("support_status") or "") not in MODEL_REGISTRY_RUNTIME_STATUSES:
            invalid += 1
        if runtime.get("support_status") == "verified":
            verified += 1
    return {
        "status": "ready" if verified > 0 and invalid == 0 else "missing",
        "verified_count": verified,
        "invalid_count": invalid,
    }


def _hardware_status(value: Any) -> str:
    hardware = value if isinstance(value, dict) else {}
    min_ram = _positive_number_status(hardware.get("min_ram_gb"))
    tier = str(hardware.get("recommended_capability_tier") or "").strip()
    if min_ram == "ready" and tier and tier != "unknown":
        return "ready"
    return "missing"


def _artifact_status(value: Any) -> dict[str, Any]:
    artifacts = value if isinstance(value, dict) else {}
    manifest = _hash_status(_safe_text(artifacts.get("manifest_sha256")))
    weights = _hash_status(_safe_text(artifacts.get("weights_sha256")))
    placeholder_count = len([status for status in (manifest, weights) if status == "placeholder"])
    return {
        "status": "ready" if manifest == "sha256" and weights == "sha256" else "missing",
        "manifest_sha256_status": manifest,
        "weights_sha256_status": weights,
        "placeholder_hash_count": placeholder_count,
    }


def _eval_status(value: Any) -> dict[str, Any]:
    eval_plan = value if isinstance(value, dict) else {}
    required = set(str(item) for item in eval_plan.get("required_evaluations", []) if isinstance(item, str))
    completed = set(str(item) for item in eval_plan.get("completed_evaluations", []) if isinstance(item, str))
    missing_required = MODEL_REGISTRY_REQUIRED_EVALS - required
    incomplete = MODEL_REGISTRY_REQUIRED_EVALS - completed
    criteria = eval_plan.get("success_criteria") if isinstance(eval_plan.get("success_criteria"), dict) else {}
    criteria_ready = (
        criteria.get("no_known_license_blocker") is True
        and criteria.get("local_chat_smoke_passes") is True
        and _positive_number_status(criteria.get("minimum_domain_pass_rate")) == "ready"
    )
    return {
        "status": "ready" if not missing_required and not incomplete and criteria_ready else "missing",
        "required_count": len(required),
        "completed_count": len(completed),
        "missing_required": sorted(missing_required),
        "incomplete": sorted(incomplete),
    }


def _governance_status(value: Any) -> str:
    governance = value if isinstance(value, dict) else {}
    if (
        governance.get("proposal_id")
        and governance.get("review_status") == "approved"
        and governance.get("rollback_plan")
        and isinstance(governance.get("approved_by"), list)
        and governance["approved_by"]
    ):
        return "ready"
    return "missing"


def _domain_status(value: Any) -> str:
    domains = value if isinstance(value, list) else []
    return "ready" if any(isinstance(item, str) and item for item in domains) else "missing"


def _hash_status(value: str | None) -> str:
    text = str(value or "")
    if not text or text == "TBD":
        return "placeholder"
    if _SHA256_RE.fullmatch(text):
        return "sha256"
    return "invalid"


def _unique_ids(items: list[Any], *, errors: list[str], field_name: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            errors.append(f"{field_name} entries must be objects")
            continue
        item_id = str(item.get("id") or "")
        if not item_id:
            errors.append(f"{field_name} entry is missing id")
            continue
        if item_id in seen:
            errors.append(f"{field_name} contains duplicate id: {item_id}")
            continue
        seen.add(item_id)
        ids.append(item_id)
    return ids


def _list_at(value: dict[str, Any], *path: str) -> list[Any]:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return current if isinstance(current, list) else []


def _sensitive_findings(value: Any) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")
        elif isinstance(item, str):
            for kind, pattern in _SENSITIVE_PATTERNS.items():
                if pattern.search(item):
                    findings.append({"path": path, "kind": kind})

    walk(value, "")
    return findings


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text


def _empty_summary() -> dict[str, Any]:
    return {
        "model_count": 0,
        "candidate_count": 0,
        "approved_model_count": 0,
        "approval_ready_count": 0,
        "placeholder_hash_count": 0,
        "sensitive_finding_count": 0,
        "best_next_candidate": None,
        "model_ids": [],
    }

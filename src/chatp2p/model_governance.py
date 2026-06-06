"""Model contribution governance registry for ChatP2P."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file


MODEL_GOVERNANCE_REGISTRY_SCHEMA = "chatp2p.model-governance-registry.v1"
MODEL_GOVERNANCE_REPORT_SCHEMA = "chatp2p.model-governance-report.v1"
MODEL_GOVERNANCE_DEFAULT_REGISTRY_ID = "chatp2p-default-model-governance-v0"
MODEL_GOVERNANCE_REQUIRED_TIERS = {
    "standard_member",
    "verified_compute_member",
    "model_contributor",
    "domain_steward",
    "network_governance_member",
}
MODEL_GOVERNANCE_REQUIRED_EVALS = {"domain_eval", "regression_eval", "safety_eval"}
_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "tailscale_auth_key": re.compile(r"\btskey-[A-Za-z0-9_-]+\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "alpha_token": re.compile(r"\balpha-token-[A-Za-z0-9_-]{8,}\b"),
    "credit_grant_token": re.compile(r"\bcredit-grant-token-[A-Za-z0-9_-]{8,}\b"),
}


@dataclass(frozen=True)
class ModelGovernanceConfig:
    registry_path: Path = Path(".mesh/model-governance.json")
    out_path: Path | None = None
    init: bool = False
    force: bool = False


def default_model_governance_registry() -> dict[str, Any]:
    """Return the built-in starter registry for model contribution governance."""

    return {
        "schema": MODEL_GOVERNANCE_REGISTRY_SCHEMA,
        "registry_id": MODEL_GOVERNANCE_DEFAULT_REGISTRY_ID,
        "version": "0.1.0",
        "summary": {
            "purpose": "Gate model influence by verified contribution, reputation, evals, and review.",
            "core_weight_editing": "disabled_in_v0",
            "recommended_next_action": "choose_first_open_weight_base_model",
        },
        "membership": {
            "credit_metric": "lifetime_verified_credits_earned",
            "reputation_metric": "verified_matches_minus_disputes_and_timeouts",
            "governance_note": "Credits are spendable usage accounting; reputation gates trust.",
            "tiers": [
                {
                    "id": "standard_member",
                    "label": "Standard Member",
                    "min_lifetime_credits_earned": 0,
                    "min_verified_results": 0,
                    "min_reputation_status": "new",
                    "permissions": [
                        "use_network_with_credits",
                        "earn_compute_credits",
                        "run_approved_weight_packs",
                    ],
                    "gated_capabilities": {
                        "submit_adapter": False,
                        "review_domain": False,
                        "vote_on_model_release": False,
                    },
                },
                {
                    "id": "verified_compute_member",
                    "label": "Verified Compute Member",
                    "min_lifetime_credits_earned": 100,
                    "min_verified_results": 25,
                    "min_reputation_status": "ok",
                    "permissions": [
                        "run_approved_weight_packs",
                        "host_higher_value_jobs",
                        "join_compute_leaderboards",
                    ],
                    "gated_capabilities": {
                        "submit_adapter": False,
                        "review_domain": False,
                        "vote_on_model_release": False,
                    },
                },
                {
                    "id": "model_contributor",
                    "label": "Model Contributor",
                    "min_lifetime_credits_earned": 500,
                    "min_verified_results": 100,
                    "min_reputation_status": "ok",
                    "permissions": [
                        "submit_adapter_proposal",
                        "submit_eval_proposal",
                        "submit_training_data_proposal",
                    ],
                    "gated_capabilities": {
                        "submit_adapter": True,
                        "review_domain": False,
                        "vote_on_model_release": False,
                    },
                },
                {
                    "id": "domain_steward",
                    "label": "Domain Steward",
                    "min_lifetime_credits_earned": 1500,
                    "min_verified_results": 250,
                    "min_reputation_status": "trusted",
                    "permissions": [
                        "review_domain_adapter",
                        "review_domain_eval",
                        "flag_domain_regressions",
                    ],
                    "gated_capabilities": {
                        "submit_adapter": True,
                        "review_domain": True,
                        "vote_on_model_release": False,
                    },
                },
                {
                    "id": "network_governance_member",
                    "label": "Network Governance Member",
                    "min_lifetime_credits_earned": 5000,
                    "min_verified_results": 750,
                    "min_reputation_status": "trusted",
                    "permissions": [
                        "vote_on_corpus_policy",
                        "vote_on_model_release",
                        "vote_on_weight_pack_promotion",
                        "vote_on_dispute_policy",
                    ],
                    "gated_capabilities": {
                        "submit_adapter": True,
                        "review_domain": True,
                        "vote_on_model_release": True,
                    },
                },
            ],
        },
        "domains": [
            {"id": "general", "label": "General Chat", "review_tier": "model_contributor"},
            {"id": "maths", "label": "Maths", "review_tier": "domain_steward"},
            {"id": "science", "label": "Science", "review_tier": "domain_steward"},
            {"id": "coding", "label": "Coding", "review_tier": "domain_steward"},
            {"id": "philosophy", "label": "Philosophy", "review_tier": "domain_steward"},
            {"id": "safety", "label": "Safety And Policy", "review_tier": "network_governance_member"},
        ],
        "weight_pack_policy": {
            "approved_pack_required": True,
            "core_weight_edits_allowed": False,
            "core_weight_release_process": "proposal_eval_review_vote_release",
            "tamper_detection": [
                "sha256_manifest",
                "signed_result_challenges",
                "output_mismatch_quarantine",
                "reputation_penalty",
            ],
            "tamper_response": {
                "first_detection": "quarantine_node_pending_review",
                "confirmed_tamper": "revoke_rewards_and_lock_network_access",
                "appeal": "manual_dispute_ticket",
            },
        },
        "weight_packs": [
            {
                "id": "chatp2p-base-placeholder-v0",
                "type": "base_model",
                "status": "proposal",
                "base_model": "open-weight-base-to-be-selected",
                "license": "must_be_confirmed_before_serving",
                "domains": ["general"],
                "allowed_runtimes": ["ollama", "llama.cpp"],
                "manifest_sha256": "TBD",
                "weights_sha256": "TBD",
                "core_weight_editable": False,
                "promotion_gate": "must_pass_eval_and_governance_review",
            }
        ],
        "adapter_policy": {
            "submissions_enabled": True,
            "required_submitter_tier": "model_contributor",
            "required_evaluations": [
                "domain_eval",
                "regression_eval",
                "safety_eval",
                "license_review",
            ],
            "promotion_requires": {
                "minimum_eval_delta": 0.02,
                "domain_steward_review": True,
                "safety_review": True,
                "rollback_plan": True,
                "dataset_license_review": True,
            },
            "direct_core_weight_edits": False,
        },
        "safety_policy": {
            "illegal_use_attempt": "quarantine_and_review",
            "private_data_submission": "reject_and_warn",
            "malicious_adapter": "reject_penalize_and_quarantine",
            "release_rollback": "required_for_every_promoted_pack",
        },
    }


def run_model_governance(config: ModelGovernanceConfig) -> dict[str, Any]:
    """Inspect or initialize the local model governance registry."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    init_result = _maybe_init_registry(config=config, registry_path=registry_path)
    registry, load_status, load_warnings = _load_registry(registry_path=registry_path)
    validation = validate_model_governance_registry(registry)
    warnings = [*init_result["warnings"], *load_warnings, *validation["warnings"]]
    errors = [*init_result["errors"], *validation["errors"]]
    recommended_next_action = _recommended_next_action(errors=errors, warnings=warnings, registry=registry)
    report = {
        "schema": MODEL_GOVERNANCE_REPORT_SCHEMA,
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
            "recommended_next_action": recommended_next_action,
        },
        "registry": _safe_registry_view(registry),
        "warnings": warnings,
        "errors": errors,
    }
    if config.out_path is not None:
        out_path = config.out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        report["artifacts"] = {"json": str(out_path)}
    return report


def validate_model_governance_registry(registry: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(registry, dict):
        return {
            "ok": False,
            "summary": _empty_summary(),
            "warnings": [],
            "errors": ["registry must be a JSON object"],
        }
    if registry.get("schema") != MODEL_GOVERNANCE_REGISTRY_SCHEMA:
        errors.append(f"schema must be {MODEL_GOVERNANCE_REGISTRY_SCHEMA}")

    sensitive_findings = _sensitive_findings(registry)
    errors.extend(f"sensitive value detected at {finding['path']} ({finding['kind']})" for finding in sensitive_findings)

    tiers = _list_at(registry, "membership", "tiers")
    tier_ids = _unique_ids(tiers, errors=errors, field_name="membership.tiers")
    missing_tiers = sorted(MODEL_GOVERNANCE_REQUIRED_TIERS - set(tier_ids))
    if missing_tiers:
        errors.append(f"missing required membership tiers: {', '.join(missing_tiers)}")
    _validate_tiers(tiers=tiers, errors=errors, warnings=warnings)

    domains = _list_at(registry, "domains")
    domain_ids = _unique_ids(domains, errors=errors, field_name="domains")
    if "general" not in domain_ids:
        warnings.append("general domain is missing")

    weight_policy = registry.get("weight_pack_policy") if isinstance(registry.get("weight_pack_policy"), dict) else {}
    core_edits_allowed = bool(weight_policy.get("core_weight_edits_allowed"))
    if core_edits_allowed:
        errors.append("core weight edits must remain disabled in Model Governance V0")
    if not weight_policy.get("approved_pack_required", False):
        errors.append("approved_pack_required must be true")

    weight_packs = _list_at(registry, "weight_packs")
    _unique_ids(weight_packs, errors=errors, field_name="weight_packs")
    placeholder_hash_count = _validate_weight_packs(
        weight_packs=weight_packs,
        domain_ids=set(domain_ids),
        errors=errors,
        warnings=warnings,
    )

    adapter_policy = registry.get("adapter_policy") if isinstance(registry.get("adapter_policy"), dict) else {}
    required_evals = set(str(item) for item in adapter_policy.get("required_evaluations", []) if isinstance(item, str))
    missing_evals = sorted(MODEL_GOVERNANCE_REQUIRED_EVALS - required_evals)
    if missing_evals:
        errors.append(f"adapter_policy.required_evaluations missing: {', '.join(missing_evals)}")
    if adapter_policy.get("direct_core_weight_edits", False):
        errors.append("adapter_policy.direct_core_weight_edits must be false")

    approved_weight_packs = [
        pack for pack in weight_packs if isinstance(pack, dict) and str(pack.get("status") or "") == "approved"
    ]
    summary = {
        "tier_count": len(tiers),
        "domain_count": len(domains),
        "weight_pack_count": len(weight_packs),
        "approved_weight_pack_count": len(approved_weight_packs),
        "placeholder_hash_count": placeholder_hash_count,
        "adapter_submissions_enabled": bool(adapter_policy.get("submissions_enabled", False)),
        "core_weight_edits_allowed": core_edits_allowed,
        "sensitive_finding_count": len(sensitive_findings),
    }
    return {"ok": not errors, "summary": summary, "warnings": warnings, "errors": errors}


def format_model_governance_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model governance: {str(report.get('status', 'unknown')).upper()}",
        f"Registry: {(report.get('config') or {}).get('registry_path')}",
        f"Tiers: {summary.get('tier_count')}",
        f"Domains: {summary.get('domain_count')}",
        f"Weight packs: {summary.get('weight_pack_count')} approved {summary.get('approved_weight_pack_count')}",
        f"Adapter submissions: {summary.get('adapter_submissions_enabled')}",
        f"Core weight edits allowed: {summary.get('core_weight_edits_allowed')}",
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


def _maybe_init_registry(*, config: ModelGovernanceConfig, registry_path: Path) -> dict[str, Any]:
    result = {"requested": config.init, "status": "not_requested", "path": str(registry_path), "warnings": [], "errors": []}
    if not config.init:
        return result
    if registry_path.exists() and not config.force:
        result["status"] = "exists"
        result["warnings"].append("registry_exists_use_force_to_replace")
        return result
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(default_model_governance_registry(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result["status"] = "written"
    return result


def _load_registry(*, registry_path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    if not registry_path.exists():
        return (
            default_model_governance_registry(),
            {"source": "builtin_default", "exists": False},
            ["registry_missing_using_builtin_default"],
        )
    registry = read_json_file(registry_path, description="model governance registry")
    if not isinstance(registry, dict):
        raise ValueError("model governance registry must be a JSON object")
    return registry, {"source": "file", "exists": True}, []


def _recommended_next_action(*, errors: list[str], warnings: list[str], registry: dict[str, Any]) -> str:
    if errors:
        return "fix_model_governance_registry"
    validation = validate_model_governance_registry(registry)
    if validation["summary"].get("approved_weight_pack_count", 0) <= 0:
        return "choose_first_open_weight_base_model"
    if validation["summary"].get("placeholder_hash_count", 0) > 0:
        return "replace_placeholder_weight_hashes_before_serving"
    if warnings:
        return "review_model_governance_warnings"
    return "publish_governance_registry_for_review"


def _safe_registry_view(registry: dict[str, Any]) -> dict[str, Any]:
    membership = registry.get("membership") if isinstance(registry.get("membership"), dict) else {}
    return {
        "schema": registry.get("schema"),
        "registry_id": _safe_text(registry.get("registry_id")),
        "version": _safe_text(registry.get("version")),
        "membership": {
            "credit_metric": _safe_text(membership.get("credit_metric")),
            "reputation_metric": _safe_text(membership.get("reputation_metric")),
            "tiers": [_safe_tier(tier) for tier in _list_at(registry, "membership", "tiers")],
        },
        "domains": [_safe_domain(domain) for domain in _list_at(registry, "domains")],
        "weight_pack_policy": _safe_weight_pack_policy(registry.get("weight_pack_policy")),
        "weight_packs": [_safe_weight_pack(pack) for pack in _list_at(registry, "weight_packs")],
        "adapter_policy": _safe_adapter_policy(registry.get("adapter_policy")),
        "safety_policy": _safe_safety_policy(registry.get("safety_policy")),
    }


def _safe_tier(tier: Any) -> dict[str, Any]:
    tier = tier if isinstance(tier, dict) else {}
    return {
        "id": _safe_text(tier.get("id")),
        "label": _safe_text(tier.get("label")),
        "min_lifetime_credits_earned": tier.get("min_lifetime_credits_earned"),
        "min_verified_results": tier.get("min_verified_results"),
        "min_reputation_status": _safe_text(tier.get("min_reputation_status")),
        "permissions": [_safe_text(item) for item in tier.get("permissions", []) if isinstance(item, str)],
        "gated_capabilities": tier.get("gated_capabilities") if isinstance(tier.get("gated_capabilities"), dict) else {},
    }


def _safe_domain(domain: Any) -> dict[str, Any]:
    domain = domain if isinstance(domain, dict) else {}
    return {
        "id": _safe_text(domain.get("id")),
        "label": _safe_text(domain.get("label")),
        "review_tier": _safe_text(domain.get("review_tier")),
    }


def _safe_weight_pack_policy(policy: Any) -> dict[str, Any]:
    policy = policy if isinstance(policy, dict) else {}
    return {
        "approved_pack_required": bool(policy.get("approved_pack_required", False)),
        "core_weight_edits_allowed": bool(policy.get("core_weight_edits_allowed", False)),
        "core_weight_release_process": _safe_text(policy.get("core_weight_release_process")),
        "tamper_detection": [_safe_text(item) for item in policy.get("tamper_detection", []) if isinstance(item, str)],
    }


def _safe_weight_pack(pack: Any) -> dict[str, Any]:
    pack = pack if isinstance(pack, dict) else {}
    manifest_sha = _safe_text(pack.get("manifest_sha256"))
    weights_sha = _safe_text(pack.get("weights_sha256"))
    return {
        "id": _safe_text(pack.get("id")),
        "type": _safe_text(pack.get("type")),
        "status": _safe_text(pack.get("status")),
        "base_model": _safe_text(pack.get("base_model")),
        "license": _safe_text(pack.get("license")),
        "domains": [_safe_text(item) for item in pack.get("domains", []) if isinstance(item, str)],
        "allowed_runtimes": [_safe_text(item) for item in pack.get("allowed_runtimes", []) if isinstance(item, str)],
        "manifest_sha256_status": _hash_status(manifest_sha),
        "weights_sha256_status": _hash_status(weights_sha),
        "core_weight_editable": bool(pack.get("core_weight_editable", False)),
        "promotion_gate": _safe_text(pack.get("promotion_gate")),
    }


def _safe_adapter_policy(policy: Any) -> dict[str, Any]:
    policy = policy if isinstance(policy, dict) else {}
    return {
        "submissions_enabled": bool(policy.get("submissions_enabled", False)),
        "required_submitter_tier": _safe_text(policy.get("required_submitter_tier")),
        "required_evaluations": [_safe_text(item) for item in policy.get("required_evaluations", []) if isinstance(item, str)],
        "promotion_requires": policy.get("promotion_requires") if isinstance(policy.get("promotion_requires"), dict) else {},
        "direct_core_weight_edits": bool(policy.get("direct_core_weight_edits", False)),
    }


def _safe_safety_policy(policy: Any) -> dict[str, Any]:
    policy = policy if isinstance(policy, dict) else {}
    return {str(key): _safe_text(value) for key, value in policy.items() if isinstance(key, str)}


def _hash_status(value: str | None) -> str:
    if not value or value == "TBD":
        return "placeholder"
    if re.fullmatch(r"[A-Fa-f0-9]{64}", value):
        return "sha256"
    return "nonstandard"


def _validate_tiers(*, tiers: list[Any], errors: list[str], warnings: list[str]) -> None:
    previous_credits = -1
    for tier in tiers:
        if not isinstance(tier, dict):
            errors.append("membership.tiers entries must be objects")
            continue
        tier_id = str(tier.get("id") or "")
        if not _SAFE_ID_RE.fullmatch(tier_id):
            errors.append(f"membership tier id is invalid: {tier_id or '<missing>'}")
        credits = _int_or_none(tier.get("min_lifetime_credits_earned"))
        if credits is None or credits < 0:
            errors.append(f"{tier_id or '<missing>'} min_lifetime_credits_earned must be a non-negative integer")
            continue
        if credits < previous_credits:
            warnings.append("membership tiers are not ordered by min_lifetime_credits_earned")
        previous_credits = credits
        permissions = tier.get("permissions")
        if not isinstance(permissions, list) or not permissions:
            errors.append(f"{tier_id or '<missing>'} must define at least one permission")
            permissions = []
        if tier_id == "network_governance_member":
            if "vote_on_model_release" not in permissions or "vote_on_corpus_policy" not in permissions:
                errors.append("network_governance_member must include model and corpus voting permissions")
            if not tier.get("min_reputation_status"):
                errors.append("network_governance_member must require a reputation status")


def _validate_weight_packs(
    *,
    weight_packs: list[Any],
    domain_ids: set[str],
    errors: list[str],
    warnings: list[str],
) -> int:
    placeholder_hash_count = 0
    for pack in weight_packs:
        if not isinstance(pack, dict):
            errors.append("weight_packs entries must be objects")
            continue
        pack_id = str(pack.get("id") or "")
        if not _SAFE_ID_RE.fullmatch(pack_id.replace("-", "_")):
            errors.append(f"weight pack id is invalid: {pack_id or '<missing>'}")
        if pack.get("core_weight_editable", False):
            errors.append(f"{pack_id or '<missing>'} must not allow direct core weight edits")
        if str(pack.get("status") or "") not in {"proposal", "approved", "deprecated", "quarantined"}:
            errors.append(f"{pack_id or '<missing>'} status must be proposal, approved, deprecated, or quarantined")
        pack_domains = set(str(item) for item in pack.get("domains", []) if isinstance(item, str))
        unknown_domains = sorted(pack_domains - domain_ids)
        if unknown_domains:
            errors.append(f"{pack_id or '<missing>'} references unknown domains: {', '.join(unknown_domains)}")
        for field_name in ("manifest_sha256", "weights_sha256"):
            status = _hash_status(_safe_text(pack.get(field_name)))
            if status == "placeholder":
                placeholder_hash_count += 1
                warnings.append(f"{pack_id or '<missing>'} has placeholder {field_name}")
            elif status != "sha256":
                errors.append(f"{pack_id or '<missing>'} {field_name} must be a sha256 hash or TBD")
    if not weight_packs:
        warnings.append("no weight packs are defined")
    return placeholder_hash_count


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


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _empty_summary() -> dict[str, Any]:
    return {
        "tier_count": 0,
        "domain_count": 0,
        "weight_pack_count": 0,
        "approved_weight_pack_count": 0,
        "placeholder_hash_count": 0,
        "adapter_submissions_enabled": False,
        "core_weight_edits_allowed": False,
        "sensitive_finding_count": 0,
    }

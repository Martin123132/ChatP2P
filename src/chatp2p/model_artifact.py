"""Read-only artifact hash manifest for base model candidates."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .model_registry import MODEL_REGISTRY_SCHEMA, default_model_registry, validate_model_registry


MODEL_ARTIFACT_MANIFEST_REPORT_SCHEMA = "chatp2p.model-artifact-manifest-report.v1"
MODEL_ARTIFACT_ATTACH_REPORT_SCHEMA = "chatp2p.model-artifact-attach-report.v1"

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
class ModelArtifactManifestConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    model_id: str = "chatp2p-base-candidate-v0"
    out_dir: Path = Path(".mesh/model-artifact-manifest")
    manifest_artifact: Path | None = None
    weights_artifact: Path | None = None
    artifact_paths: tuple[Path, ...] = ()
    manifest_sha256: str | None = None
    weights_sha256: str | None = None
    quantization: str | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class ModelArtifactAttachConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    artifact_report_path: Path = Path(".mesh/model-artifact-manifest/model-artifact-manifest.json")
    out_path: Path | None = None
    write: bool = False
    backup: bool = True


def run_model_artifact_manifest(config: ModelArtifactManifestConfig) -> dict[str, Any]:
    """Hash local artifacts or normalize supplied hashes without editing the registry."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    warnings: list[str] = [
        "artifact manifest is read-only and does not download weights, edit registries, or approve candidates"
    ]
    errors: list[str] = []

    registry, registry_status, load_warnings = _load_registry(registry_path)
    warnings.extend(load_warnings)
    validation = validate_model_registry(registry)
    warnings.extend(validation["warnings"])
    errors.extend(validation["errors"])
    model = _find_model(registry, config.model_id)
    if model is None:
        errors.append(f"model_id not found in registry: {config.model_id}")

    artifact_entries: list[dict[str, Any]] = []
    manifest_file = _hash_file_role("manifest", config.manifest_artifact, errors=errors)
    weights_file = _hash_file_role("weights", config.weights_artifact, errors=errors)
    if manifest_file:
        artifact_entries.append(manifest_file)
    if weights_file:
        artifact_entries.append(weights_file)
    for artifact_path in config.artifact_paths:
        artifact = _hash_file_role("auxiliary", artifact_path, errors=errors)
        if artifact:
            artifact_entries.append(artifact)

    manifest_hash = _resolve_hash(
        label="manifest_sha256",
        supplied_hash=config.manifest_sha256,
        file_entry=manifest_file,
        errors=errors,
    )
    weights_hash = _resolve_hash(
        label="weights_sha256",
        supplied_hash=config.weights_sha256,
        file_entry=weights_file,
        errors=errors,
    )
    quantization = _safe_text(config.quantization)
    complete = bool(manifest_hash and weights_hash and quantization)
    status = "fail" if errors else ("pass" if complete else "warn")
    report: dict[str, Any] = {
        "schema": MODEL_ARTIFACT_MANIFEST_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "model_id": _safe_text(config.model_id),
            "out_dir": _safe_text(str(out_dir)),
            "source_url": _safe_text(config.source_url),
        },
        "registry_status": registry_status,
        "registry_validation_summary": validation["summary"],
        "selected_model": _safe_model_view(model),
        "summary": {
            "artifact_hashes_complete": complete,
            "manifest_sha256": _safe_text(manifest_hash),
            "weights_sha256": _safe_text(weights_hash),
            "quantization": quantization,
            "does_not_approve_model": True,
            "registry_write": False,
            "candidate_update_preview": _candidate_update_preview(
                registry_path=registry_path,
                model_id=config.model_id,
                manifest_sha256=manifest_hash,
                weights_sha256=weights_hash,
                quantization=quantization,
            )
            if complete
            else None,
            "recommended_next_action": _recommended_next_action(
                errors=errors,
                manifest_sha256=manifest_hash,
                weights_sha256=weights_hash,
                quantization=quantization,
            ),
        },
        "artifacts_hashed": artifact_entries,
        "warnings": warnings,
        "errors": [_safe_text(error) for error in errors],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "model-artifact-manifest.json"
    markdown_path = out_dir / "model-artifact-manifest.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_artifact_manifest_markdown(report), encoding="utf-8")
    return report


def run_model_artifact_attach(config: ModelArtifactAttachConfig) -> dict[str, Any]:
    """Attach artifact hash evidence to a model registry without approving the model."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    artifact_report_path = config.artifact_report_path.expanduser().resolve()
    warnings: list[str] = []
    errors: list[str] = []

    registry = read_json_file(registry_path, description="model registry")
    if not isinstance(registry, dict):
        raise ValueError("model registry must be a JSON object")
    artifact_report = read_json_file(artifact_report_path, description="model artifact manifest report")
    if not isinstance(artifact_report, dict):
        raise ValueError("model artifact manifest report must be a JSON object")

    if registry.get("schema") != MODEL_REGISTRY_SCHEMA:
        errors.append(f"registry schema must be {MODEL_REGISTRY_SCHEMA}")
    if artifact_report.get("schema") != MODEL_ARTIFACT_MANIFEST_REPORT_SCHEMA:
        errors.append(f"artifact report schema must be {MODEL_ARTIFACT_MANIFEST_REPORT_SCHEMA}")
    if artifact_report.get("status") == "fail" or artifact_report.get("errors"):
        errors.append("artifact manifest report has failures; rerun artifact-manifest before attaching evidence")

    model_id = _artifact_report_model_id(artifact_report)
    if not model_id:
        errors.append("artifact manifest report does not identify a model id")
    model = _find_model(registry, model_id) if model_id else None
    if model_id and model is None:
        errors.append(f"model_id not found in registry: {model_id}")
    if isinstance(model, dict) and model.get("status") == "approved":
        errors.append("approved model entries cannot be modified by attach-artifacts")

    artifact_values = _artifact_values_from_report(artifact_report)
    if not artifact_values["manifest_sha256"]:
        errors.append("artifact manifest report is missing manifest_sha256")
    if not artifact_values["weights_sha256"]:
        errors.append("artifact manifest report is missing weights_sha256")
    if not artifact_values["quantization"]:
        errors.append("artifact manifest report is missing quantization")

    before_status = _safe_text(model.get("status")) if isinstance(model, dict) else None
    updated_registry = json.loads(json.dumps(registry))
    updated_model = _find_model(updated_registry, model_id) if model_id else None
    changes: list[dict[str, Any]] = []

    if not errors and isinstance(updated_model, dict):
        changes = _apply_artifacts_to_model(updated_model, artifact_values)

    after_status = _safe_text(updated_model.get("status")) if isinstance(updated_model, dict) else None
    if before_status != after_status:
        errors.append("internal safety error: attach-artifacts attempted to change model status")

    validation = validate_model_registry(updated_registry if not errors else registry)
    warnings.extend(validation["warnings"])
    if validation["errors"]:
        errors.extend(f"updated registry validation failed: {error}" for error in validation["errors"])

    write_result = {"requested": config.write, "status": "dry_run", "registry_path": str(registry_path)}
    if config.write and not errors:
        if config.backup:
            backup_path = registry_path.with_suffix(registry_path.suffix + ".bak")
            backup_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
            write_result["backup_path"] = str(backup_path)
        registry_path.write_text(json.dumps(updated_registry, indent=2, sort_keys=True), encoding="utf-8")
        write_result["status"] = "written"
    elif config.write and errors:
        write_result["status"] = "blocked"

    status = "fail" if errors else ("warn" if warnings else "pass")
    report: dict[str, Any] = {
        "schema": MODEL_ARTIFACT_ATTACH_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "dry_run": not config.write,
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "artifact_report_path": _safe_text(str(artifact_report_path)),
            "out_path": _safe_text(str(config.out_path.expanduser().resolve())) if config.out_path else None,
            "write": config.write,
            "backup": config.backup,
        },
        "model": {
            "id": _safe_text(model_id),
            "status_before": before_status,
            "status_after": after_status,
            "approval_status_changed": before_status != after_status,
        },
        "summary": {
            "change_count": len(changes),
            "artifacts_complete": bool(
                artifact_values["manifest_sha256"] and artifact_values["weights_sha256"] and artifact_values["quantization"]
            ),
            "does_not_approve_model": True,
            "recommended_next_action": _attach_recommended_next_action(errors=errors, write=config.write),
        },
        "artifact_values": _safe_json(artifact_values),
        "write": write_result,
        "changes": changes,
        "registry_validation_summary": validation["summary"],
        "warnings": warnings,
        "errors": [_safe_text(error) for error in errors],
    }

    if config.out_path is not None:
        out_path = config.out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        report["artifacts"] = {"json": str(out_path)}
    return report


def format_model_artifact_manifest_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model artifact manifest: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {(report.get('selected_model') or {}).get('id')}",
        f"Hashes complete: {summary.get('artifact_hashes_complete')}",
        f"Manifest hash: {_hash_status(summary.get('manifest_sha256'))}",
        f"Weights hash: {_hash_status(summary.get('weights_sha256'))}",
        f"Quantization: {summary.get('quantization')}",
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


def format_model_artifact_attach_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    model = report.get("model") or {}
    write = report.get("write") or {}
    lines = [
        f"Model artifact attach: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {model.get('id')}",
        f"Mode: {'dry-run' if report.get('dry_run') else 'write'}",
        f"Changes: {summary.get('change_count')}",
        f"Artifacts complete: {summary.get('artifacts_complete')}",
        f"Model status: {model.get('status_before')} -> {model.get('status_after')}",
        f"Write: {write.get('status')}",
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


def format_model_artifact_manifest_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Model Artifact Manifest",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Model: `{(report.get('selected_model') or {}).get('id')}`",
        f"- Hashes complete: `{summary.get('artifact_hashes_complete')}`",
        f"- Does not approve model: `{summary.get('does_not_approve_model')}`",
        f"- Registry write: `{summary.get('registry_write')}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Hashes",
        "",
        f"- Manifest SHA256: `{summary.get('manifest_sha256') or 'missing'}`",
        f"- Weights SHA256: `{summary.get('weights_sha256') or 'missing'}`",
        f"- Quantization: `{summary.get('quantization') or 'missing'}`",
        "",
        "## Artifacts",
        "",
    ]
    if report.get("artifacts_hashed"):
        for artifact in report["artifacts_hashed"]:
            lines.append(
                f"- `{artifact.get('role')}` `{artifact.get('name')}` size `{artifact.get('size_bytes')}` "
                f"sha256 `{artifact.get('sha256')}`"
            )
    else:
        lines.append("- No local artifact files were hashed.")
    if summary.get("candidate_update_preview"):
        lines.extend(["", "## Candidate Update Preview", "", "```powershell", summary["candidate_update_preview"], "```"])
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    lines.append("")
    return "\n".join(lines)


def _artifact_report_model_id(report: dict[str, Any]) -> str | None:
    config = report.get("config") if isinstance(report.get("config"), dict) else {}
    model_id = config.get("model_id")
    if isinstance(model_id, str) and model_id.strip():
        return model_id.strip()
    selected = report.get("selected_model") if isinstance(report.get("selected_model"), dict) else {}
    selected_id = selected.get("id")
    return selected_id.strip() if isinstance(selected_id, str) and selected_id.strip() else None


def _artifact_values_from_report(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    manifest_sha = _safe_text(summary.get("manifest_sha256"))
    weights_sha = _safe_text(summary.get("weights_sha256"))
    quantization = _safe_text(summary.get("quantization"))
    return {
        "manifest_sha256": manifest_sha if _hash_ready(manifest_sha) else None,
        "weights_sha256": weights_sha if _hash_ready(weights_sha) else None,
        "quantization": quantization if quantization and quantization != "TBD" else None,
    }


def _apply_artifacts_to_model(model: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = model.setdefault("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}
        model["artifacts"] = artifacts
    changes: list[dict[str, Any]] = []
    for field_name in ("manifest_sha256", "weights_sha256", "quantization"):
        value = values.get(field_name)
        if artifacts.get(field_name) == value:
            continue
        artifacts[field_name] = value
        changes.append({"field": f"artifacts.{field_name}", "status": "updated", "value_status": _value_status(value)})
    return changes


def _attach_recommended_next_action(*, errors: list[str], write: bool) -> str:
    if errors:
        return "fix_artifact_attach_errors"
    if not write:
        return "rerun_attach_artifacts_with_write_after_review"
    return "run_model_release_check"


def _hash_file_role(role: str, path: Path | None, *, errors: list[str]) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        errors.append(f"{role} artifact not found: {resolved}")
        return {
            "role": role,
            "path": _safe_text(str(resolved)),
            "name": _safe_text(resolved.name),
            "exists": False,
        }
    if not resolved.is_file():
        errors.append(f"{role} artifact is not a file: {resolved}")
        return {
            "role": role,
            "path": _safe_text(str(resolved)),
            "name": _safe_text(resolved.name),
            "exists": False,
        }
    return {
        "role": role,
        "path": _safe_text(str(resolved)),
        "name": _safe_text(resolved.name),
        "exists": True,
        "size_bytes": resolved.stat().st_size,
        "sha256": _hash_file(resolved),
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_hash(
    *,
    label: str,
    supplied_hash: str | None,
    file_entry: dict[str, Any] | None,
    errors: list[str],
) -> str | None:
    normalized = str(supplied_hash or "").strip().lower() or None
    if normalized and not _SHA256_RE.fullmatch(normalized):
        errors.append(f"{label} must be a 64-character sha256 hex string")
        normalized = None
    file_hash = str((file_entry or {}).get("sha256") or "").strip().lower() or None
    if normalized and file_hash and normalized != file_hash:
        errors.append(f"{label} does not match hashed {file_entry.get('role')} artifact")
    return normalized or file_hash


def _candidate_update_preview(
    *,
    registry_path: Path,
    model_id: str,
    manifest_sha256: str | None,
    weights_sha256: str | None,
    quantization: str | None,
) -> str:
    return "\n".join(
        [
            "python -m chatp2p.cli model candidate `",
            f"  --registry {registry_path} `",
            f"  --model-id {model_id} `",
            f"  --manifest-sha256 {manifest_sha256} `",
            f"  --weights-sha256 {weights_sha256} `",
            f"  --quantization {quantization} `",
            "  --json",
        ]
    )


def _recommended_next_action(
    *,
    errors: list[str],
    manifest_sha256: str | None,
    weights_sha256: str | None,
    quantization: str | None,
) -> str:
    if errors:
        return "fix_artifact_manifest_errors"
    if not manifest_sha256:
        return "provide_manifest_artifact_or_sha256"
    if not weights_sha256:
        return "provide_weights_artifact_or_sha256"
    if not quantization:
        return "record_quantization"
    return "review_then_update_candidate_artifacts"


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


def _find_model(registry: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    for model in registry.get("models", []):
        if isinstance(model, dict) and str(model.get("id") or "") == model_id:
            return model
    return None


def _safe_model_view(model: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(model, dict):
        return None
    return {
        "id": _safe_text(model.get("id")),
        "status": _safe_text(model.get("status")),
        "provider": _safe_text(model.get("provider")),
        "project": _safe_text(model.get("project")),
        "license": _safe_text(model.get("license")),
        "source_url": _safe_text(model.get("source_url")),
    }


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {_safe_text(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _hash_status(value: Any) -> str:
    return "present" if _SHA256_RE.fullmatch(str(value or "")) else "missing"


def _hash_ready(value: Any) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value or "")))


def _value_status(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    text = _safe_text(value) or ""
    if _hash_ready(text):
        return "sha256"
    return "present" if text else "empty"


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text

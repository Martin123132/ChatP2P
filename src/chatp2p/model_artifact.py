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


def _hash_status(value: Any) -> str:
    return "present" if _SHA256_RE.fullmatch(str(value or "")) else "missing"


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text

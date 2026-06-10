"""Release dossier for ChatP2P base model candidates."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .model_release import ModelReleaseCheckConfig, run_model_release_check


MODEL_RELEASE_BUNDLE_REPORT_SCHEMA = "chatp2p.model-release-bundle-report.v1"

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
class ModelReleaseBundleConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    governance_path: Path = Path(".mesh/model-governance.json")
    model_id: str = "chatp2p-base-candidate-v0"
    out_dir: Path = Path(".mesh/model-release-bundle")
    runtime_report_path: Path | None = None
    artifact_report_path: Path | None = None
    eval_report_path: Path | None = None
    governance_pack_report_path: Path | None = None
    governance_review_report_path: Path | None = None


def run_model_release_bundle(config: ModelReleaseBundleConfig) -> dict[str, Any]:
    """Build a read-only release dossier without approving or editing the model."""

    started_at = time.time()
    out_dir = config.out_dir.expanduser().resolve()
    registry_path = config.registry_path.expanduser().resolve()
    governance_path = config.governance_path.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = [
        "release bundle is read-only and does not approve, promote, or edit model registries"
    ]
    errors: list[str] = []

    release_check_path = out_dir / "model-release-check.json"
    release_check = run_model_release_check(
        ModelReleaseCheckConfig(
            registry_path=registry_path,
            governance_path=governance_path,
            model_id=config.model_id,
            out_path=release_check_path,
        )
    )
    warnings.extend(f"release-check: {warning}" for warning in release_check.get("warnings", []))
    errors.extend(f"release-check: {error}" for error in release_check.get("errors", []))

    evidence = {
        "runtime_check": _evidence_view("runtime_check", config.runtime_report_path),
        "artifact_manifest": _evidence_view("artifact_manifest", config.artifact_report_path),
        "eval_report": _evidence_view("eval_report", config.eval_report_path),
        "governance_pack": _evidence_view("governance_pack", config.governance_pack_report_path),
        "governance_review": _evidence_view("governance_review", config.governance_review_report_path),
    }
    for item in evidence.values():
        if item["status"] in {"missing", "error"}:
            warnings.append(f"{item['id']} evidence {item['status']}")

    release_summary = release_check.get("summary") if isinstance(release_check.get("summary"), dict) else {}
    gates = release_check.get("gates") if isinstance(release_check.get("gates"), list) else []
    release_ready = bool(release_summary.get("release_ready"))
    configured_evidence = [item for item in evidence.values() if item["configured"]]
    evidence_ok = [item for item in configured_evidence if item["status"] == "present" and item.get("ok") is not False]
    missing_or_error = [item["id"] for item in configured_evidence if item["status"] != "present" or item.get("ok") is False]

    if release_check.get("ok") is False:
        errors.append("release-check failed")

    status = "fail" if errors else ("pass" if release_ready and not missing_or_error else "warn")
    promote_dry_run_argv = [
        "python",
        "-m",
        "chatp2p.cli",
        "model",
        "release-promote",
        "--release-report",
        str(release_check_path),
        "--out",
        str(out_dir / "model-release-promote.json"),
    ]
    promote_write_argv = promote_dry_run_argv + ["--write", "--confirm-release-ready"]

    report: dict[str, Any] = {
        "schema": MODEL_RELEASE_BUNDLE_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "governance_path": _safe_text(str(governance_path)),
            "model_id": _safe_text(config.model_id),
            "out_dir": _safe_text(str(out_dir)),
        },
        "model": release_check.get("model"),
        "release_check": {
            "status": _safe_text(release_check.get("status")),
            "ok": bool(release_check.get("ok")),
            "summary": _safe_json(release_summary),
            "artifact": str(release_check_path),
        },
        "gates": [_safe_gate(gate) for gate in gates if isinstance(gate, dict)],
        "evidence": evidence,
        "promotion": {
            "dry_run_argv": [_safe_text(item) for item in promote_dry_run_argv],
            "write_argv": [_safe_text(item) for item in promote_write_argv],
            "requires_explicit_write": True,
            "requires_confirm_release_ready": True,
        },
        "summary": {
            "release_ready": release_ready,
            "gate_count": release_summary.get("gate_count"),
            "passed_gate_count": release_summary.get("passed_gate_count"),
            "failed_gate_count": release_summary.get("failed_gate_count"),
            "blocked_gate_ids": release_summary.get("blocked_gate_ids") or [],
            "configured_evidence_count": len(configured_evidence),
            "evidence_ok_count": len(evidence_ok),
            "missing_or_error_evidence_ids": missing_or_error,
            "recommended_next_action": _bundle_next_action(
                errors=errors,
                release_ready=release_ready,
                missing_or_error=missing_or_error,
            ),
        },
        "warnings": warnings,
        "errors": [_safe_text(error) for error in errors],
    }

    json_path = out_dir / "model-release-bundle.json"
    markdown_path = out_dir / "model-release-bundle.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_release_bundle_markdown(report), encoding="utf-8")
    return report


def format_model_release_bundle_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model release bundle: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {(report.get('model') or {}).get('id')}",
        f"Release ready: {summary.get('release_ready')}",
        f"Gates: {summary.get('passed_gate_count')}/{summary.get('gate_count')} passed",
        f"Evidence: {summary.get('evidence_ok_count')}/{summary.get('configured_evidence_count')} configured ok",
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


def format_model_release_bundle_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    promotion = report.get("promotion") or {}
    lines = [
        "# ChatP2P Model Release Bundle",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Model: `{(report.get('model') or {}).get('id')}`",
        f"- Release ready: `{summary.get('release_ready')}`",
        f"- Gates passed: `{summary.get('passed_gate_count')}/{summary.get('gate_count')}`",
        f"- Evidence ok: `{summary.get('evidence_ok_count')}/{summary.get('configured_evidence_count')}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Gates",
        "",
    ]
    for gate in report.get("gates", []):
        lines.append(f"- `{gate.get('id')}`: `{gate.get('status')}` - {gate.get('reason')}")
    lines.extend(["", "## Evidence", ""])
    for item in (report.get("evidence") or {}).values():
        lines.append(
            f"- `{item.get('id')}`: `{item.get('status')}`"
            f" schema=`{item.get('schema')}` report_status=`{item.get('report_status')}`"
        )
    lines.extend(
        [
            "",
            "## Promotion Preview",
            "",
            "Dry-run:",
            "",
            "```powershell",
            " ".join(str(item) for item in promotion.get("dry_run_argv", [])),
            "```",
            "",
            "Write after review:",
            "",
            "```powershell",
            " ".join(str(item) for item in promotion.get("write_argv", [])),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _evidence_view(evidence_id: str, path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "id": evidence_id,
            "configured": False,
            "status": "not_configured",
            "path": None,
            "schema": None,
            "ok": None,
            "report_status": None,
            "summary": {},
        }
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "id": evidence_id,
            "configured": True,
            "status": "missing",
            "path": _safe_text(str(resolved)),
            "schema": None,
            "ok": None,
            "report_status": None,
            "summary": {},
        }
    try:
        report = read_json_file(resolved, description=f"{evidence_id} report")
    except (OSError, ValueError) as exc:
        return {
            "id": evidence_id,
            "configured": True,
            "status": "error",
            "path": _safe_text(str(resolved)),
            "schema": None,
            "ok": None,
            "report_status": None,
            "summary": {"error": _safe_text(str(exc))},
        }
    if not isinstance(report, dict):
        return {
            "id": evidence_id,
            "configured": True,
            "status": "error",
            "path": _safe_text(str(resolved)),
            "schema": None,
            "ok": None,
            "report_status": None,
            "summary": {"error": "report is not a JSON object"},
        }
    return {
        "id": evidence_id,
        "configured": True,
        "status": "present",
        "path": _safe_text(str(resolved)),
        "schema": _safe_text(report.get("schema")),
        "ok": report.get("ok") if isinstance(report.get("ok"), bool) else None,
        "report_status": _safe_text(report.get("status")),
        "summary": _safe_summary(report.get("summary")),
    }


def _safe_summary(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    allowed_keys = {
        "release_ready",
        "runtime_verified",
        "artifacts_complete",
        "eval_success",
        "review_status",
        "pack_status",
        "does_not_approve_model",
        "recommended_next_action",
        "blocked_gate_ids",
        "change_count",
    }
    return {key: _safe_json(value) for key, value in summary.items() if key in allowed_keys}


def _safe_gate(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_text(gate.get("id")),
        "label": _safe_text(gate.get("label")),
        "status": _safe_text(gate.get("status")),
        "reason": _safe_text(gate.get("reason")),
        "evidence": _safe_json(gate.get("evidence") or {}),
    }


def _bundle_next_action(*, errors: list[str], release_ready: bool, missing_or_error: list[str]) -> str:
    if errors:
        return "fix_model_release_bundle_errors"
    if missing_or_error:
        return f"review_missing_release_evidence_{missing_or_error[0]}"
    if release_ready:
        return "review_release_bundle_then_run_release_promote"
    return "resolve_release_check_blockers"


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

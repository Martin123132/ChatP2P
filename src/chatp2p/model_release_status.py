"""Read-only model release pipeline status for ChatP2P candidates."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .model_release_sequence import ModelReleaseSequenceConfig, run_model_release_sequence


MODEL_RELEASE_STATUS_REPORT_SCHEMA = "chatp2p.model-release-status-report.v1"

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
class ModelReleaseStatusConfig:
    pack_dir: Path = Path(".mesh/model-candidate-pack")
    governance_path: Path = Path(".mesh/model-governance.json")
    out_dir: Path = Path(".mesh/model-release-status")
    model_id: str | None = None
    runtime_report_path: Path | None = None
    artifact_report_path: Path | None = None
    eval_report_path: Path | None = None
    governance_pack_report_path: Path | None = None
    governance_review_report_path: Path | None = None
    bundle_report_path: Path | None = None


def run_model_release_status(config: ModelReleaseStatusConfig) -> dict[str, Any]:
    """Write a read-only status report for the local model release pipeline."""

    started_at = time.time()
    pack_dir = config.pack_dir.expanduser().resolve()
    governance_path = config.governance_path.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    warnings = ["model release status is read-only and does not approve, promote, or edit model registries"]
    errors: list[str] = []

    sequence = run_model_release_sequence(
        ModelReleaseSequenceConfig(
            pack_dir=pack_dir,
            governance_path=governance_path,
            out_dir=out_dir / "sequence",
            model_id=config.model_id,
            runtime_report_path=config.runtime_report_path,
            artifact_report_path=config.artifact_report_path,
            governance_pack_report_path=config.governance_pack_report_path,
            governance_review_report_path=config.governance_review_report_path,
        )
    )
    warnings.extend(f"release-sequence: {warning}" for warning in sequence.get("warnings", []))
    errors.extend(f"release-sequence: {error}" for error in sequence.get("errors", []))

    sequence_summary = sequence.get("summary") if isinstance(sequence.get("summary"), dict) else {}
    release_check_path = _path_from_sequence_release_check(sequence)
    release_check = _read_optional_report(release_check_path)
    release_summary = release_check.get("summary") if isinstance((release_check or {}).get("summary"), dict) else {}
    gates = [_safe_gate(gate) for gate in (release_check or {}).get("gates", []) if isinstance(gate, dict)]

    evidence = {
        "candidate_pack": _evidence_view("candidate_pack", pack_dir / "model-candidate-pack.json"),
        "staging_registry": _evidence_view("staging_registry", pack_dir / "staging-model-registry.json"),
        "release_sequence": _evidence_view("release_sequence", Path(str((sequence.get("artifacts") or {}).get("json")))),
        "release_check": _evidence_view("release_check", release_check_path),
        "runtime_check": _evidence_view("runtime_check", config.runtime_report_path),
        "artifact_manifest": _evidence_view("artifact_manifest", config.artifact_report_path),
        "eval_report": _evidence_view("eval_report", config.eval_report_path or (pack_dir / "eval" / "model-eval-report.json")),
        "eval_attach": _evidence_view("eval_attach", pack_dir / "eval-attach-report.json"),
        "governance_pack": _evidence_view("governance_pack", config.governance_pack_report_path),
        "governance_review": _evidence_view("governance_review", config.governance_review_report_path),
        "release_bundle": _evidence_view("release_bundle", config.bundle_report_path),
    }
    configured = [item for item in evidence.values() if item["configured"]]
    present = [item for item in configured if item["status"] == "present" and item.get("ok") is not False]
    missing_or_error = [
        item["id"]
        for item in configured
        if item["status"] != "present" or item.get("ok") is False
    ]

    if sequence.get("ok") is False:
        errors.append("model release sequence failed")

    release_ready = bool(release_summary.get("release_ready") or sequence_summary.get("release_ready"))
    next_action = sequence.get("next_action") if isinstance(sequence.get("next_action"), dict) else {}
    pipeline_stage = _pipeline_stage(
        evidence=evidence,
        release_ready=release_ready,
        next_action_id=str(next_action.get("id") or ""),
    )

    status = "fail" if errors else ("pass" if release_ready else "warn")
    report: dict[str, Any] = {
        "schema": MODEL_RELEASE_STATUS_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "pack_dir": _safe_text(str(pack_dir)),
            "governance_path": _safe_text(str(governance_path)),
            "out_dir": _safe_text(str(out_dir)),
            "model_id": _safe_text(config.model_id),
            "runtime_report_path": _safe_path(config.runtime_report_path),
            "artifact_report_path": _safe_path(config.artifact_report_path),
            "eval_report_path": _safe_path(config.eval_report_path),
            "governance_pack_report_path": _safe_path(config.governance_pack_report_path),
            "governance_review_report_path": _safe_path(config.governance_review_report_path),
            "bundle_report_path": _safe_path(config.bundle_report_path),
        },
        "summary": {
            "model_id": _safe_text(sequence_summary.get("model_id") or release_summary.get("model_id")),
            "pipeline_stage": pipeline_stage,
            "release_ready": release_ready,
            "release_check_status": _safe_text((release_check or {}).get("status") or sequence_summary.get("release_check_status")),
            "gate_count": release_summary.get("gate_count"),
            "passed_gate_count": release_summary.get("passed_gate_count"),
            "failed_gate_count": release_summary.get("failed_gate_count"),
            "blocked_gate_ids": _safe_json(release_summary.get("blocked_gate_ids") or sequence_summary.get("blocked_gate_ids") or []),
            "evidence_present_count": len(present),
            "evidence_configured_count": len(configured),
            "missing_or_error_evidence_ids": missing_or_error,
            "next_action_id": _safe_text(next_action.get("id")),
            "recommended_next_action": _safe_text(next_action.get("recommendation") or sequence_summary.get("recommended_next_action")),
            "writes_registry": bool(next_action.get("writes_registry")),
            "write_flag_required_after_review": bool(next_action.get("write_flag_required_after_review")),
            "requires_review": bool(next_action.get("requires_review")),
            "ready_for_release_bundle": release_ready,
            "ready_for_promotion_review": release_ready and evidence["release_bundle"]["status"] == "present",
        },
        "sequence": _sequence_view(sequence),
        "release_check": {
            "status": _safe_text((release_check or {}).get("status")),
            "ok": (release_check or {}).get("ok") if isinstance((release_check or {}).get("ok"), bool) else None,
            "artifact": _safe_text(str(release_check_path)) if release_check_path else None,
            "summary": _safe_json(release_summary),
        },
        "gates": gates,
        "evidence": evidence,
        "next_action": _safe_json(next_action),
        "warnings": warnings,
        "errors": [_safe_text(error) for error in errors],
    }

    json_path = out_dir / "model-release-status.json"
    markdown_path = out_dir / "model-release-status.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_release_status_markdown(report), encoding="utf-8")
    return report


def format_model_release_status_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model release status: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {summary.get('model_id')}",
        f"Stage: {summary.get('pipeline_stage')}",
        f"Release ready: {summary.get('release_ready')}",
        f"Gates: {summary.get('passed_gate_count')}/{summary.get('gate_count')} passed",
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


def format_model_release_status_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Model Release Status",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Model: `{summary.get('model_id')}`",
        f"- Pipeline stage: `{summary.get('pipeline_stage')}`",
        f"- Release ready: `{summary.get('release_ready')}`",
        f"- Gates passed: `{summary.get('passed_gate_count')}/{summary.get('gate_count')}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        f"- Registry write needed after review: `{summary.get('write_flag_required_after_review')}`",
        "",
        "## Gates",
        "",
        "| Gate | Status | Reason |",
        "| --- | --- | --- |",
    ]
    for gate in report.get("gates") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(gate.get("id")),
                    str(gate.get("status")),
                    _markdown_table_text(str(gate.get("reason") or "")),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Evidence", "", "| Evidence | Status | Report |", "| --- | --- | --- |"])
    for item in (report.get("evidence") or {}).values():
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("id")),
                    str(item.get("status")),
                    str(item.get("report_status") or item.get("schema") or "-"),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Next Command", "", "```powershell"])
    lines.append(" ".join(str(item) for item in (report.get("next_action") or {}).get("argv", [])))
    lines.extend(["```", ""])
    if summary.get("missing_or_error_evidence_ids"):
        lines.extend(["## Missing Or Error Evidence", ""])
        lines.extend(f"- `{item}`" for item in summary["missing_or_error_evidence_ids"])
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


def _path_from_sequence_release_check(sequence: dict[str, Any]) -> Path | None:
    release_check = sequence.get("release_check") if isinstance(sequence.get("release_check"), dict) else {}
    artifact = release_check.get("artifact")
    return Path(str(artifact)).expanduser().resolve() if artifact else None


def _sequence_view(sequence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": _safe_text(sequence.get("schema")),
        "ok": sequence.get("ok") if isinstance(sequence.get("ok"), bool) else None,
        "status": _safe_text(sequence.get("status")),
        "summary": _safe_json(sequence.get("summary") or {}),
        "artifacts": _safe_json(sequence.get("artifacts") or {}),
    }


def _pipeline_stage(*, evidence: dict[str, dict[str, Any]], release_ready: bool, next_action_id: str) -> str:
    if evidence["candidate_pack"]["status"] != "present" or evidence["staging_registry"]["status"] != "present":
        return "candidate_pack_needed"
    if release_ready and evidence["release_bundle"]["status"] == "present":
        return "promotion_review_ready"
    if release_ready:
        return "release_bundle_needed"
    stage_by_action = {
        "runtime_check": "runtime_check_needed",
        "attach_runtime": "runtime_attach_needed",
        "artifact_manifest": "artifact_manifest_needed",
        "attach_artifacts": "artifact_attach_needed",
        "eval": "eval_needed",
        "attach_eval": "eval_attach_needed",
        "governance_review": "governance_review_needed",
        "governance_pack": "governance_pack_needed",
        "release_check": "release_check_review_needed",
    }
    return stage_by_action.get(next_action_id, "release_evidence_incomplete")


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
    report = _read_optional_report(resolved)
    if report is None:
        return {
            "id": evidence_id,
            "configured": True,
            "status": "present",
            "path": _safe_text(str(resolved)),
            "schema": None,
            "ok": None,
            "report_status": None,
            "summary": {},
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


def _read_optional_report(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        report = read_json_file(path, description=str(path))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


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
        "recommended_next_action",
        "blocked_gate_ids",
        "selected_model_id",
        "model_id",
        "pipeline_stage",
        "next_action_id",
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


def _markdown_table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")

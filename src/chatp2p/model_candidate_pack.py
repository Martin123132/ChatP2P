"""Build an isolated evidence pack for a shortlisted base model candidate."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .model_candidate import ModelCandidateIntakeConfig, run_model_candidate_intake
from .model_eval import ModelEvalAttachConfig, ModelEvalConfig, run_model_eval, run_model_eval_attach
from .model_registry import default_model_registry
from .model_release import ModelReleaseCheckConfig, run_model_release_check
from .model_shortlist import ModelShortlistConfig, run_model_shortlist


MODEL_CANDIDATE_PACK_REPORT_SCHEMA = "chatp2p.model-candidate-pack-report.v1"

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
class ModelCandidatePackConfig:
    out_dir: Path = Path(".mesh/model-candidate-pack")
    registry_path: Path = Path(".mesh/model-registry.json")
    governance_path: Path = Path(".mesh/model-governance.json")
    model_id: str | None = None
    max_parameter_count_b: float = 12.0
    prefer_license: str = "apache-2.0"
    include_noncommercial: bool = False


def run_model_candidate_pack(config: ModelCandidatePackConfig) -> dict[str, Any]:
    """Create a local-only candidate evidence pack from the shortlist workflow."""

    started_at = time.time()
    out_dir = config.out_dir.expanduser().resolve()
    registry_path = config.registry_path.expanduser().resolve()
    governance_path = config.governance_path.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = [
        "candidate pack uses an isolated staging registry and does not modify the live registry",
        "candidate pack evidence does not approve or release a model",
    ]
    errors: list[str] = []
    reports: dict[str, Any] = {}
    artifacts: dict[str, str] = {}

    shortlist_report = run_model_shortlist(
        ModelShortlistConfig(
            out_dir=out_dir / "shortlist",
            max_parameter_count_b=config.max_parameter_count_b,
            prefer_license=config.prefer_license,
            include_noncommercial=config.include_noncommercial,
        )
    )
    reports["shortlist"] = _step_view(shortlist_report)
    artifacts["shortlist_json"] = str(out_dir / "shortlist" / "model-shortlist.json")
    artifacts["shortlist_markdown"] = str(out_dir / "shortlist" / "model-shortlist.md")

    selected = _select_candidate(shortlist_report, config.model_id)
    if selected is None:
        errors.append(f"model_id not found in shortlist: {config.model_id}")
    else:
        warnings.extend(f"selected candidate blocker: {blocker}" for blocker in selected.get("blockers", []))

    staging_registry_path = out_dir / "staging-model-registry.json"
    if selected is not None and not errors:
        _seed_staging_registry(source_path=registry_path, staging_path=staging_registry_path)
        preview_report = run_model_candidate_intake(
            _candidate_config(
                selected,
                registry_path=registry_path,
                out_path=out_dir / "candidate-intake-preview.json",
                write=False,
            )
        )
        staging_report = run_model_candidate_intake(
            _candidate_config(
                selected,
                registry_path=staging_registry_path,
                out_path=out_dir / "candidate-intake-staging-write.json",
                write=True,
            )
        )
        reports["candidate_preview"] = _step_view(preview_report)
        reports["candidate_staging_write"] = _step_view(staging_report)
        artifacts["candidate_preview_json"] = str(out_dir / "candidate-intake-preview.json")
        artifacts["candidate_staging_write_json"] = str(out_dir / "candidate-intake-staging-write.json")
        artifacts["staging_registry_json"] = str(staging_registry_path)
        errors.extend(_step_errors("candidate preview", preview_report))
        errors.extend(_step_errors("candidate staging write", staging_report))

    eval_report: dict[str, Any] | None = None
    attach_report: dict[str, Any] | None = None
    release_report: dict[str, Any] | None = None
    if selected is not None and not errors:
        model_id = str(selected["id"])
        eval_report = run_model_eval(
            ModelEvalConfig(
                registry_path=staging_registry_path,
                model_id=model_id,
                out_dir=out_dir / "eval",
                mode="fake",
            )
        )
        attach_report = run_model_eval_attach(
            ModelEvalAttachConfig(
                registry_path=staging_registry_path,
                eval_report_path=out_dir / "eval" / "model-eval-report.json",
                out_path=out_dir / "eval-attach-report.json",
                write=True,
                backup=False,
            )
        )
        release_report = run_model_release_check(
            ModelReleaseCheckConfig(
                registry_path=staging_registry_path,
                governance_path=governance_path,
                model_id=model_id,
                out_path=out_dir / "release-check.json",
            )
        )
        reports["eval"] = _step_view(eval_report)
        reports["eval_attach"] = _step_view(attach_report)
        reports["release_check"] = _step_view(release_report)
        artifacts["eval_json"] = str(out_dir / "eval" / "model-eval-report.json")
        artifacts["eval_markdown"] = str(out_dir / "eval" / "model-eval-report.md")
        artifacts["eval_attach_json"] = str(out_dir / "eval-attach-report.json")
        artifacts["release_check_json"] = str(out_dir / "release-check.json")
        errors.extend(_step_errors("model eval", eval_report))
        errors.extend(_step_errors("eval attach", attach_report))
        errors.extend(_step_errors("release check", release_report, fail_only=True))

    release_summary = (release_report or {}).get("summary") or {}
    release_ready = bool(release_summary.get("release_ready"))
    status = "fail" if errors else ("pass" if release_ready else "warn")
    report: dict[str, Any] = {
        "schema": MODEL_CANDIDATE_PACK_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "out_dir": _safe_text(str(out_dir)),
            "registry_path": _safe_text(str(registry_path)),
            "governance_path": _safe_text(str(governance_path)),
            "model_id": _safe_text(config.model_id),
            "max_parameter_count_b": config.max_parameter_count_b,
            "prefer_license": _safe_text(config.prefer_license),
            "include_noncommercial": config.include_noncommercial,
        },
        "summary": {
            "selected_model_id": _safe_text(selected.get("id")) if selected else None,
            "release_ready": release_ready,
            "blocked_gate_ids": release_summary.get("blocked_gate_ids") or [],
            "does_not_approve_model": True,
            "live_registry_modified": False,
            "staging_registry_path": _safe_text(str(staging_registry_path)) if selected else None,
            "recommended_next_action": _recommended_next_action(errors=errors, release_summary=release_summary),
        },
        "selected_candidate": selected,
        "reports": reports,
        "artifacts": artifacts,
        "warnings": warnings,
        "errors": errors,
    }
    json_path = out_dir / "model-candidate-pack.json"
    markdown_path = out_dir / "model-candidate-pack.md"
    report["artifacts"]["json"] = str(json_path)
    report["artifacts"]["markdown"] = str(markdown_path)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_candidate_pack_markdown(report), encoding="utf-8")
    return report


def format_model_candidate_pack_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model candidate pack: {str(report.get('status', 'unknown')).upper()}",
        f"Selected: {summary.get('selected_model_id')}",
        f"Release ready: {summary.get('release_ready')}",
        f"Blocked gates: {', '.join(summary.get('blocked_gate_ids') or []) or 'none'}",
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


def format_model_candidate_pack_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Model Candidate Pack",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Selected model id: `{summary.get('selected_model_id')}`",
        f"- Release ready: `{summary.get('release_ready')}`",
        f"- Live registry modified: `{summary.get('live_registry_modified')}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Gates",
        "",
    ]
    blocked = summary.get("blocked_gate_ids") or []
    lines.append(f"- Blocked: `{', '.join(blocked) if blocked else 'none'}`")
    lines.extend(["", "## Reports", ""])
    for name, step in (report.get("reports") or {}).items():
        lines.append(f"- `{name}`: `{step.get('status')}` ok `{step.get('ok')}`")
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    lines.append("")
    return "\n".join(lines)


def _select_candidate(shortlist_report: dict[str, Any], model_id: str | None) -> dict[str, Any] | None:
    candidates = shortlist_report.get("candidates") if isinstance(shortlist_report.get("candidates"), list) else []
    if model_id:
        return next((candidate for candidate in candidates if candidate.get("id") == model_id), None)
    recommended = shortlist_report.get("recommended")
    return recommended if isinstance(recommended, dict) else None


def _seed_staging_registry(*, source_path: Path, staging_path: Path) -> None:
    if source_path.exists():
        registry = read_json_file(source_path, description="model registry")
        if not isinstance(registry, dict):
            raise ValueError("model registry must be a JSON object")
    else:
        registry = default_model_registry()
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


def _candidate_config(
    selected: dict[str, Any],
    *,
    registry_path: Path,
    out_path: Path,
    write: bool,
) -> ModelCandidateIntakeConfig:
    runtime_specs = ["ollama:candidate:local smoke pending"]
    runtime = selected.get("runtime") if isinstance(selected.get("runtime"), dict) else {}
    if runtime.get("llama_cpp"):
        runtime_specs.append("llama.cpp:candidate:quantization pending")
    hardware = selected.get("hardware") if isinstance(selected.get("hardware"), dict) else {}
    license_data = selected.get("license") if isinstance(selected.get("license"), dict) else {}
    return ModelCandidateIntakeConfig(
        registry_path=registry_path,
        model_id=str(selected.get("id") or ""),
        provider=_safe_text(selected.get("provider")),
        project=_safe_text(selected.get("project")),
        variant=_safe_text(selected.get("project")),
        status="candidate",
        license=_safe_text(license_data.get("spdx")),
        license_url=_safe_text(selected.get("model_card_url")),
        source_url=_safe_text(selected.get("source_url")),
        parameter_count_b=float(selected["parameter_count_b"]) if selected.get("parameter_count_b") is not None else None,
        architecture=_safe_text(selected.get("architecture")),
        context_length_tokens=(
            int(selected["context_length_tokens"]) if selected.get("context_length_tokens") is not None else None
        ),
        domains=tuple(domain for domain in selected.get("domains", []) if isinstance(domain, str)),
        runtimes=tuple(runtime_specs),
        min_ram_gb=_float_or_none(hardware.get("min_ram_gb_estimate")),
        min_vram_gb=_float_or_none(hardware.get("min_vram_gb_estimate")),
        recommended_capability_tier="gaming_laptop",
        notes="Generated by model candidate pack; hashes and verified runtime still require confirmation.",
        out_path=out_path,
        write=write,
        backup=False,
    )


def _step_view(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": _safe_text(report.get("schema")),
        "ok": bool(report.get("ok")),
        "status": _safe_text(report.get("status")),
        "summary": _safe_json(report.get("summary") or {}),
        "artifacts": _safe_json(report.get("artifacts") or {}),
        "errors": [_safe_text(error) for error in report.get("errors", []) if isinstance(error, str)],
        "warnings": [_safe_text(warning) for warning in report.get("warnings", []) if isinstance(warning, str)],
    }


def _step_errors(name: str, report: dict[str, Any], *, fail_only: bool = False) -> list[str]:
    if fail_only and report.get("status") != "fail":
        return []
    if report.get("ok"):
        return []
    errors = [error for error in report.get("errors", []) if isinstance(error, str)]
    if errors:
        return [f"{name}: {_safe_text(error)}" for error in errors]
    return [f"{name}: step failed"]


def _recommended_next_action(*, errors: list[str], release_summary: dict[str, Any]) -> str:
    if errors:
        return "fix_candidate_pack_errors"
    blocked = release_summary.get("blocked_gate_ids") if isinstance(release_summary.get("blocked_gate_ids"), list) else []
    if "runtime" in blocked:
        return "verify_local_runtime_for_candidate"
    if "artifacts" in blocked:
        return "download_or_identify_candidate_artifacts_and_record_hashes"
    if "model_governance_review" in blocked:
        return "submit_candidate_for_governance_review"
    if blocked:
        return f"resolve_release_gate_{blocked[0]}"
    if release_summary.get("release_ready"):
        return "promote_model_through_governance_release"
    return "review_candidate_pack"


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


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

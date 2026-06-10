"""Read-only release sequence planner for ChatP2P model candidates."""

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


MODEL_RELEASE_SEQUENCE_REPORT_SCHEMA = "chatp2p.model-release-sequence-report.v1"

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
class ModelReleaseSequenceConfig:
    pack_dir: Path = Path(".mesh/model-candidate-pack")
    governance_path: Path = Path(".mesh/model-governance.json")
    out_dir: Path = Path(".mesh/model-release-sequence")
    model_id: str | None = None
    runtime_report_path: Path | None = None
    artifact_report_path: Path | None = None
    governance_pack_report_path: Path | None = None
    governance_review_report_path: Path | None = None


def run_model_release_sequence(config: ModelReleaseSequenceConfig) -> dict[str, Any]:
    """Plan the next safe command for a candidate-pack release workflow."""

    started_at = time.time()
    pack_dir = config.pack_dir.expanduser().resolve()
    governance_path = config.governance_path.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings = ["release sequence is read-only and only writes planner reports"]
    errors: list[str] = []

    candidate_pack_path = pack_dir / "model-candidate-pack.json"
    candidate_pack = _read_optional_report(candidate_pack_path)
    model_id = config.model_id or _candidate_pack_model_id(candidate_pack)
    staging_registry_path = pack_dir / "staging-model-registry.json"

    release_check: dict[str, Any] | None = None
    release_check_path = out_dir / "model-release-check.json"
    if staging_registry_path.exists() and model_id:
        release_check = run_model_release_check(
            ModelReleaseCheckConfig(
                registry_path=staging_registry_path,
                governance_path=governance_path,
                model_id=model_id,
                out_path=release_check_path,
            )
        )
        warnings.extend(f"release-check: {warning}" for warning in release_check.get("warnings", []))
        errors.extend(f"release-check: {error}" for error in release_check.get("errors", []))
    elif not staging_registry_path.exists():
        warnings.append("staging registry missing; run model candidate-pack first")
    elif not model_id:
        errors.append("model id could not be inferred from candidate pack; pass --model-id")

    evidence = {
        "candidate_pack": _path_status(candidate_pack_path),
        "staging_registry": _path_status(staging_registry_path),
        "eval_report": _path_status(pack_dir / "eval" / "model-eval-report.json"),
        "eval_attach_report": _path_status(pack_dir / "eval-attach-report.json"),
        "runtime_report": _path_status(config.runtime_report_path.expanduser().resolve() if config.runtime_report_path else None),
        "artifact_report": _path_status(config.artifact_report_path.expanduser().resolve() if config.artifact_report_path else None),
        "governance_pack_report": _path_status(
            config.governance_pack_report_path.expanduser().resolve() if config.governance_pack_report_path else None
        ),
        "governance_review_report": _path_status(
            config.governance_review_report_path.expanduser().resolve() if config.governance_review_report_path else None
        ),
        "release_check": _path_status(release_check_path if release_check is not None else None),
    }

    release_summary = release_check.get("summary") if isinstance((release_check or {}).get("summary"), dict) else {}
    blocked_gate_ids = release_summary.get("blocked_gate_ids") if isinstance(release_summary.get("blocked_gate_ids"), list) else []
    release_ready = bool(release_summary.get("release_ready"))
    next_action = _next_action(
        pack_dir=pack_dir,
        out_dir=out_dir,
        governance_path=governance_path,
        model_id=model_id,
        staging_registry_path=staging_registry_path,
        release_check_path=release_check_path,
        release_ready=release_ready,
        blocked_gate_ids=blocked_gate_ids,
        evidence=evidence,
        errors=errors,
    )

    status = "fail" if errors else ("pass" if release_ready else "warn")
    report: dict[str, Any] = {
        "schema": MODEL_RELEASE_SEQUENCE_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "pack_dir": _safe_text(str(pack_dir)),
            "governance_path": _safe_text(str(governance_path)),
            "out_dir": _safe_text(str(out_dir)),
            "model_id": _safe_text(model_id),
        },
        "summary": {
            "model_id": _safe_text(model_id),
            "release_ready": release_ready,
            "blocked_gate_ids": [_safe_text(item) for item in blocked_gate_ids],
            "release_check_status": _safe_text((release_check or {}).get("status")),
            "next_action_id": next_action["id"],
            "recommended_next_action": next_action["recommendation"],
            "writes_registry": next_action["writes_registry"],
            "write_flag_required_after_review": next_action["write_flag_required_after_review"],
            "requires_review": next_action["requires_review"],
        },
        "evidence": evidence,
        "release_check": {
            "status": _safe_text((release_check or {}).get("status")),
            "ok": (release_check or {}).get("ok") if isinstance((release_check or {}).get("ok"), bool) else None,
            "summary": _safe_json(release_summary),
            "artifact": str(release_check_path) if release_check is not None else None,
        },
        "next_action": next_action,
        "warnings": warnings,
        "errors": [_safe_text(error) for error in errors],
    }

    json_path = out_dir / "model-release-sequence.json"
    markdown_path = out_dir / "model-release-sequence.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_release_sequence_markdown(report), encoding="utf-8")
    return report


def format_model_release_sequence_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model release sequence: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {summary.get('model_id')}",
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


def format_model_release_sequence_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    action = report.get("next_action") or {}
    lines = [
        "# ChatP2P Model Release Sequence",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Model: `{summary.get('model_id')}`",
        f"- Release ready: `{summary.get('release_ready')}`",
        f"- Blocked gates: `{', '.join(summary.get('blocked_gate_ids') or []) or 'none'}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Evidence",
        "",
    ]
    for key, item in (report.get("evidence") or {}).items():
        lines.append(f"- `{key}`: `{item.get('status')}` path=`{item.get('path')}`")
    lines.extend(["", "## Next Command", "", "```powershell"])
    lines.append(" ".join(str(item) for item in action.get("argv", [])))
    lines.extend(["```", ""])
    return "\n".join(lines)


def _next_action(
    *,
    pack_dir: Path,
    out_dir: Path,
    governance_path: Path,
    model_id: str | None,
    staging_registry_path: Path,
    release_check_path: Path,
    release_ready: bool,
    blocked_gate_ids: list[Any],
    evidence: dict[str, dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    if errors:
        return _action("fix_sequence_errors", "fix_model_release_sequence_errors", [], writes=False, review=True)
    if evidence["staging_registry"]["status"] != "present":
        return _action(
            "candidate_pack",
            "run_model_candidate_pack",
            ["python", "-m", "chatp2p.cli", "model", "candidate-pack", "--out", str(pack_dir)],
            writes=False,
            review=False,
        )
    if not model_id:
        return _action("choose_model_id", "rerun_release_sequence_with_model_id", [], writes=False, review=True)
    if release_ready:
        return _action(
            "release_bundle",
            "run_release_bundle_then_review_promotion",
            [
                "python",
                "-m",
                "chatp2p.cli",
                "model",
                "release-bundle",
                "--registry",
                str(staging_registry_path),
                "--governance",
                str(governance_path),
                "--model-id",
                model_id,
                "--out",
                str(out_dir / "bundle"),
            ],
            writes=False,
            review=True,
        )

    blocked = [str(item) for item in blocked_gate_ids]
    if "runtime" in blocked:
        if evidence["runtime_report"]["status"] == "present":
            return _action(
                "attach_runtime",
                "review_runtime_report_then_attach_verified_runtime",
                [
                    "python",
                    "-m",
                    "chatp2p.cli",
                    "model",
                    "attach-runtime",
                    "--registry",
                    str(staging_registry_path),
                    "--runtime-report",
                    str(evidence["runtime_report"]["path"]),
                ],
                writes=True,
                review=True,
            )
        return _action(
            "runtime_check",
            "run_model_runtime_check",
            [
                "python",
                "-m",
                "chatp2p.cli",
                "model",
                "runtime-check",
                "--registry",
                str(staging_registry_path),
                "--model-id",
                model_id,
                "--runtime",
                "ollama",
                "--out",
                str(out_dir / "runtime-check"),
            ],
            writes=False,
            review=False,
        )
    if "artifacts" in blocked:
        if evidence["artifact_report"]["status"] == "present":
            return _action(
                "attach_artifacts",
                "review_artifact_report_then_attach_hashes",
                [
                    "python",
                    "-m",
                    "chatp2p.cli",
                    "model",
                    "attach-artifacts",
                    "--registry",
                    str(staging_registry_path),
                    "--artifact-report",
                    str(evidence["artifact_report"]["path"]),
                ],
                writes=True,
                review=True,
            )
        return _action(
            "artifact_manifest",
            "create_artifact_manifest_from_reviewed_weight_files",
            [
                "python",
                "-m",
                "chatp2p.cli",
                "model",
                "artifact-manifest",
                "--registry",
                str(staging_registry_path),
                "--model-id",
                model_id,
                "--manifest-artifact",
                "<path-to-reviewed-manifest>",
                "--weights-artifact",
                "<path-to-reviewed-weights>",
                "--quantization",
                "<quantization>",
                "--out",
                str(out_dir / "artifact-manifest"),
            ],
            writes=False,
            review=True,
        )
    if "eval_evidence" in blocked:
        if evidence["eval_report"]["status"] == "present":
            return _action(
                "attach_eval",
                "review_eval_report_then_attach_evidence",
                [
                    "python",
                    "-m",
                    "chatp2p.cli",
                    "model",
                    "attach-eval",
                    "--registry",
                    str(staging_registry_path),
                    "--eval-report",
                    str(pack_dir / "eval" / "model-eval-report.json"),
                ],
                writes=True,
                review=True,
            )
        return _action(
            "eval",
            "run_model_eval",
            [
                "python",
                "-m",
                "chatp2p.cli",
                "model",
                "eval",
                "--registry",
                str(staging_registry_path),
                "--model-id",
                model_id,
                "--out",
                str(pack_dir / "eval"),
                "--mode",
                "fake",
            ],
            writes=False,
            review=False,
        )
    if "model_governance_review" in blocked:
        return _action(
            "governance_review",
            "review_candidate_then_record_governance_review",
            [
                "python",
                "-m",
                "chatp2p.cli",
                "model",
                "governance-review",
                "--registry",
                str(staging_registry_path),
                "--model-id",
                model_id,
                "--review-status",
                "approved",
                "--rollback-plan",
                "<rollback-plan>",
                "--approved-by",
                "<approver-id-or-role>",
            ],
            writes=True,
            review=True,
        )
    if "governance_weight_pack" in blocked:
        return _action(
            "governance_pack",
            "review_candidate_hashes_then_create_governance_pack",
            [
                "python",
                "-m",
                "chatp2p.cli",
                "model",
                "governance-pack",
                "--governance",
                str(governance_path),
                "--registry",
                str(staging_registry_path),
                "--model-id",
                model_id,
                "--status",
                "approved",
            ],
            writes=True,
            review=True,
        )
    return _action(
        "release_check",
        "review_release_check_blockers",
        [
            "python",
            "-m",
            "chatp2p.cli",
            "model",
            "release-check",
            "--registry",
            str(staging_registry_path),
            "--governance",
            str(governance_path),
            "--model-id",
            model_id,
            "--out",
            str(release_check_path),
        ],
        writes=False,
        review=True,
    )


def _action(action_id: str, recommendation: str, argv: list[str], *, writes: bool, review: bool) -> dict[str, Any]:
    return {
        "id": action_id,
        "recommendation": recommendation,
        "argv": [_safe_text(item) for item in argv],
        "writes_registry": False,
        "write_flag_required_after_review": writes,
        "requires_review": review,
    }


def _read_optional_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    report = read_json_file(path, description=str(path))
    return report if isinstance(report, dict) else None


def _candidate_pack_model_id(report: dict[str, Any] | None) -> str | None:
    if not isinstance(report, dict):
        return None
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    selected = report.get("selected_candidate") if isinstance(report.get("selected_candidate"), dict) else {}
    return _safe_text(summary.get("selected_model_id") or selected.get("id"))


def _path_status(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"configured": False, "status": "not_configured", "path": None}
    return {
        "configured": True,
        "status": "present" if path.exists() else "missing",
        "path": _safe_text(str(path)),
    }


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

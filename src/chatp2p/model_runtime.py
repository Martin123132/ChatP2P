"""Read-only runtime verification for base model candidates."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .model_registry import MODEL_REGISTRY_SCHEMA, default_model_registry, validate_model_registry
from .ollama import DEFAULT_OLLAMA_BASE_URL, OllamaError, generate_ollama, list_ollama_models


MODEL_RUNTIME_CHECK_REPORT_SCHEMA = "chatp2p.model-runtime-check-report.v1"
MODEL_RUNTIME_ATTACH_REPORT_SCHEMA = "chatp2p.model-runtime-attach-report.v1"
MODEL_RUNTIME_SUPPORTED_RUNTIMES = {"ollama"}

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
class ModelRuntimeCheckConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    model_id: str = "chatp2p-base-candidate-v0"
    runtime: str = "ollama"
    out_dir: Path = Path(".mesh/model-runtime-check")
    ollama_model: str | None = None
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_timeout_seconds: float = 30.0
    prompt: str = "Reply with exactly: ok"
    expected_text: str = "ok"


@dataclass(frozen=True)
class ModelRuntimeAttachConfig:
    registry_path: Path = Path(".mesh/model-registry.json")
    runtime_report_path: Path = Path(".mesh/model-runtime-check/model-runtime-check.json")
    out_path: Path | None = None
    write: bool = False
    backup: bool = True


def run_model_runtime_check(config: ModelRuntimeCheckConfig) -> dict[str, Any]:
    """Verify a candidate against a local runtime without mutating registry state."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    runtime = str(config.runtime or "").strip().lower()
    warnings: list[str] = [
        "runtime check is read-only and does not pull models, edit registries, or approve candidates"
    ]
    errors: list[str] = []
    checks: list[dict[str, Any]] = []

    registry, registry_status, load_warnings = _load_registry(registry_path)
    warnings.extend(load_warnings)
    validation = validate_model_registry(registry)
    warnings.extend(validation["warnings"])
    errors.extend(validation["errors"])

    model = _find_model(registry, config.model_id)
    if model is None:
        errors.append(f"model_id not found in registry: {config.model_id}")
    checks.append(
        _check(
            "registry_model",
            "pass" if model is not None else "fail",
            "Model exists in registry",
            "model_id was found" if model is not None else "model_id is missing from registry",
        )
    )

    if runtime not in MODEL_RUNTIME_SUPPORTED_RUNTIMES:
        errors.append(f"runtime check only supports: {', '.join(sorted(MODEL_RUNTIME_SUPPORTED_RUNTIMES))}")

    runtime_entry = _runtime_entry(model, runtime) if model else None
    checks.append(
        _check(
            "registry_runtime_declared",
            "pass" if runtime_entry else "warn",
            f"Registry declares runtime {runtime}",
            "runtime entry exists" if runtime_entry else "runtime entry is missing or unsupported",
        )
    )

    selected_ollama_model = config.ollama_model or _guess_ollama_model(model)
    runtime_result = _run_ollama_runtime_checks(
        config=config,
        enabled=(not errors and model is not None and runtime == "ollama"),
        ollama_model=selected_ollama_model,
    )
    checks.extend(runtime_result["checks"])

    runtime_verified = (
        not errors
        and runtime_result["reachable"]
        and runtime_result["model_present"]
        and runtime_result["smoke_passed"]
    )
    status = "fail" if errors else ("pass" if runtime_verified else "warn")
    report: dict[str, Any] = {
        "schema": MODEL_RUNTIME_CHECK_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "model_id": _safe_text(config.model_id),
            "runtime": _safe_text(runtime),
            "out_dir": _safe_text(str(out_dir)),
            "ollama_model": _safe_text(selected_ollama_model),
            "ollama_base_url": _safe_text(config.ollama_base_url),
            "ollama_timeout_seconds": config.ollama_timeout_seconds,
            "prompt_preview": _preview(config.prompt),
            "expected_text": _safe_text(config.expected_text),
        },
        "registry_status": registry_status,
        "registry_validation_summary": validation["summary"],
        "selected_model": _safe_model_view(model),
        "runtime": {
            "id": _safe_text(runtime),
            "registry_entry": _safe_json(runtime_entry) if runtime_entry else None,
            "ollama_model": _safe_text(selected_ollama_model),
            "available_models": [_safe_text(model_name) for model_name in runtime_result["available_models"]],
            "available_model_count": len(runtime_result["available_models"]),
            "answer_preview": _safe_text(runtime_result.get("answer_preview")),
            "error": _safe_text(runtime_result.get("error")),
        },
        "summary": {
            "runtime_verified": runtime_verified,
            "coordinator_or_partner_required": False,
            "does_not_approve_model": True,
            "registry_write": False,
            "reachable": runtime_result["reachable"],
            "model_present": runtime_result["model_present"],
            "smoke_passed": runtime_result["smoke_passed"],
            "recommended_next_action": _recommended_next_action(
                errors=errors,
                reachable=runtime_result["reachable"],
                model_present=runtime_result["model_present"],
                smoke_passed=runtime_result["smoke_passed"],
                runtime_entry=runtime_entry,
            ),
        },
        "checks": checks,
        "warnings": warnings,
        "errors": [_safe_text(error) for error in errors],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "model-runtime-check.json"
    markdown_path = out_dir / "model-runtime-check.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_runtime_check_markdown(report), encoding="utf-8")
    return report


def run_model_runtime_attach(config: ModelRuntimeAttachConfig) -> dict[str, Any]:
    """Attach a passing runtime-check report to a model registry entry."""

    started_at = time.time()
    registry_path = config.registry_path.expanduser().resolve()
    runtime_report_path = config.runtime_report_path.expanduser().resolve()
    warnings: list[str] = []
    errors: list[str] = []

    registry, registry_status, load_warnings = _load_registry(registry_path)
    warnings.extend(load_warnings)
    runtime_report = read_json_file(runtime_report_path, description="model runtime-check report")
    if not isinstance(runtime_report, dict):
        raise ValueError("model runtime-check report must be a JSON object")

    if runtime_report.get("schema") != MODEL_RUNTIME_CHECK_REPORT_SCHEMA:
        errors.append("runtime report schema is not chatp2p.model-runtime-check-report.v1")
    report_summary = runtime_report.get("summary") if isinstance(runtime_report.get("summary"), dict) else {}
    report_runtime = runtime_report.get("runtime") if isinstance(runtime_report.get("runtime"), dict) else {}
    report_config = runtime_report.get("config") if isinstance(runtime_report.get("config"), dict) else {}
    model_id = _safe_text(report_config.get("model_id") or (runtime_report.get("selected_model") or {}).get("id"))
    runtime_id = _safe_text(report_runtime.get("id") or report_config.get("runtime"))
    if not model_id:
        errors.append("runtime report is missing model id")
    if not runtime_id:
        errors.append("runtime report is missing runtime id")
    if runtime_report.get("ok") is not True or runtime_report.get("status") != "pass":
        errors.append("runtime report must have ok=true and status=pass before attach")
    if report_summary.get("runtime_verified") is not True:
        errors.append("runtime report must have summary.runtime_verified=true before attach")
    if report_summary.get("model_present") is not True or report_summary.get("smoke_passed") is not True:
        errors.append("runtime report must show model_present=true and smoke_passed=true before attach")

    validation_before = validate_model_registry(registry)
    warnings.extend(f"model registry: {warning}" for warning in validation_before["warnings"])
    errors.extend(f"model registry: {error}" for error in validation_before["errors"])

    model = _find_model(registry, model_id or "")
    if model_id and model is None:
        errors.append(f"model_id not found in registry: {model_id}")
    if isinstance(model, dict) and model.get("status") == "approved":
        errors.append("approved model entries cannot be modified by attach-runtime")

    updated_registry = json.loads(json.dumps(registry))
    updated_model = _find_model(updated_registry, model_id or "")
    before_status = _safe_text(model.get("status")) if isinstance(model, dict) else None
    runtime_before = _runtime_entry(model, runtime_id or "") if isinstance(model, dict) and runtime_id else None
    changes: list[dict[str, Any]] = []
    if not errors and isinstance(updated_model, dict) and runtime_id:
        changes.extend(
            _apply_runtime_evidence(
                updated_model,
                runtime_id=runtime_id,
                runtime_report=runtime_report,
            )
        )

    after_model = _find_model(updated_registry, model_id or "")
    after_status = _safe_text(after_model.get("status")) if isinstance(after_model, dict) else None
    runtime_after = _runtime_entry(after_model, runtime_id or "") if isinstance(after_model, dict) and runtime_id else None
    if before_status != after_status:
        errors.append("attach-runtime must not change model status")

    validation_after = validate_model_registry(updated_registry if not errors else registry)
    warnings.extend(f"updated model registry: {warning}" for warning in validation_after["warnings"])
    if validation_after["errors"]:
        errors.extend(f"updated model registry validation failed: {error}" for error in validation_after["errors"])

    write_result = {"requested": config.write, "status": "dry_run", "registry_path": str(registry_path)}
    if config.write and not errors:
        if config.backup and registry_path.exists():
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
        "schema": MODEL_RUNTIME_ATTACH_REPORT_SCHEMA,
        "ok": not errors,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "dry_run": not config.write,
        "config": {
            "registry_path": _safe_text(str(registry_path)),
            "runtime_report_path": _safe_text(str(runtime_report_path)),
            "out_path": _safe_text(str(config.out_path.expanduser().resolve())) if config.out_path else None,
            "write": config.write,
            "backup": config.backup,
        },
        "registry_status": registry_status,
        "model": {
            "id": model_id,
            "status_before": before_status,
            "status_after": after_status,
            "approval_status_changed": before_status != after_status and after_status == "approved",
        },
        "runtime": {
            "id": runtime_id,
            "ollama_model": _safe_text(report_runtime.get("ollama_model")),
            "support_status_before": _safe_text((runtime_before or {}).get("support_status")),
            "support_status_after": _safe_text((runtime_after or {}).get("support_status")),
            "verified_at": _safe_text((runtime_after or {}).get("verified_at")),
        },
        "summary": {
            "change_count": len(changes),
            "runtime_verified_attached": (runtime_after or {}).get("support_status") == "verified",
            "does_not_approve_model": True,
            "model_status_unchanged": before_status == after_status,
            "recommended_next_action": _attach_runtime_next_action(errors=errors, write=config.write),
        },
        "write": write_result,
        "changes": changes,
        "registry_validation_summary": validation_after["summary"],
        "warnings": warnings,
        "errors": [_safe_text(error) for error in errors],
    }
    if config.out_path is not None:
        out_path = config.out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        report["artifacts"] = {"json": str(out_path)}
    return report


def format_model_runtime_check_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Model runtime check: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {(report.get('selected_model') or {}).get('id')}",
        f"Runtime verified: {summary.get('runtime_verified')}",
        f"Reachable: {summary.get('reachable')}",
        f"Model present: {summary.get('model_present')}",
        f"Smoke passed: {summary.get('smoke_passed')}",
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


def format_model_runtime_attach_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    model = report.get("model") or {}
    runtime = report.get("runtime") or {}
    write = report.get("write") or {}
    lines = [
        f"Model runtime attach: {str(report.get('status', 'unknown')).upper()}",
        f"Model: {model.get('id')}",
        f"Runtime: {runtime.get('id')}",
        f"Mode: {'dry-run' if report.get('dry_run') else 'write'}",
        f"Changes: {summary.get('change_count')}",
        f"Runtime status: {runtime.get('support_status_before')} -> {runtime.get('support_status_after')}",
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


def format_model_runtime_check_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    runtime = report.get("runtime") or {}
    lines = [
        "# ChatP2P Model Runtime Check",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Model: `{(report.get('selected_model') or {}).get('id')}`",
        f"- Runtime: `{runtime.get('id')}`",
        f"- Ollama model: `{runtime.get('ollama_model')}`",
        f"- Runtime verified: `{summary.get('runtime_verified')}`",
        f"- Does not approve model: `{summary.get('does_not_approve_model')}`",
        f"- Registry write: `{summary.get('registry_write')}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Checks",
        "",
    ]
    for check in report.get("checks") or []:
        lines.append(f"- `{check.get('id')}`: `{check.get('status')}` - {check.get('message')}")
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    lines.append("")
    return "\n".join(lines)


def _run_ollama_runtime_checks(
    *,
    config: ModelRuntimeCheckConfig,
    enabled: bool,
    ollama_model: str,
) -> dict[str, Any]:
    if not enabled:
        return {
            "reachable": False,
            "model_present": False,
            "smoke_passed": False,
            "available_models": [],
            "checks": [
                _check("ollama_reachable", "skipped", "Ollama is reachable", "skipped because earlier checks failed"),
                _check("ollama_model_present", "skipped", "Ollama model is available locally", "skipped"),
                _check("ollama_smoke", "skipped", "Ollama smoke prompt passes", "skipped"),
            ],
        }

    checks: list[dict[str, Any]] = []
    try:
        available_models = list_ollama_models(
            base_url=config.ollama_base_url,
            timeout_seconds=min(config.ollama_timeout_seconds, 5.0),
        )
    except OllamaError as exc:
        error = str(exc)
        return {
            "reachable": False,
            "model_present": False,
            "smoke_passed": False,
            "available_models": [],
            "error": error,
            "checks": [
                _check("ollama_reachable", "warn", "Ollama is reachable", error),
                _check("ollama_model_present", "skipped", "Ollama model is available locally", "skipped"),
                _check("ollama_smoke", "skipped", "Ollama smoke prompt passes", "skipped"),
            ],
        }

    checks.append(_check("ollama_reachable", "pass", "Ollama is reachable", "tags endpoint responded"))
    model_present = ollama_model in available_models
    checks.append(
        _check(
            "ollama_model_present",
            "pass" if model_present else "warn",
            "Ollama model is available locally",
            f"{ollama_model} is present" if model_present else f"{ollama_model} is not present locally",
        )
    )
    if not model_present:
        checks.append(_check("ollama_smoke", "skipped", "Ollama smoke prompt passes", "model is not present"))
        return {
            "reachable": True,
            "model_present": False,
            "smoke_passed": False,
            "available_models": available_models,
            "checks": checks,
        }

    try:
        response = generate_ollama(
            model=ollama_model,
            prompt=config.prompt,
            temperature=0.0,
            base_url=config.ollama_base_url,
            timeout_seconds=config.ollama_timeout_seconds,
        )
    except OllamaError as exc:
        error = str(exc)
        checks.append(_check("ollama_smoke", "warn", "Ollama smoke prompt passes", error))
        return {
            "reachable": True,
            "model_present": True,
            "smoke_passed": False,
            "available_models": available_models,
            "error": error,
            "checks": checks,
        }

    answer = str(response.get("answer") or "")
    smoke_passed = str(config.expected_text or "").lower() in answer.lower()
    checks.append(
        _check(
            "ollama_smoke",
            "pass" if smoke_passed else "warn",
            "Ollama smoke prompt passes",
            "expected text found in response" if smoke_passed else "expected text missing from response",
        )
    )
    return {
        "reachable": True,
        "model_present": True,
        "smoke_passed": smoke_passed,
        "available_models": available_models,
        "answer_preview": _preview(answer),
        "checks": checks,
    }


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
        if isinstance(model, dict) and model.get("id") == model_id:
            return model
    return None


def _runtime_entry(model: dict[str, Any] | None, runtime: str) -> dict[str, Any] | None:
    if not isinstance(model, dict):
        return None
    for item in model.get("runtimes", []):
        if isinstance(item, dict) and str(item.get("id") or "").lower() in {runtime, "llama.cpp" if runtime == "llama_cpp" else runtime}:
            return item
    return None


def _guess_ollama_model(model: dict[str, Any] | None) -> str:
    model_id = str((model or {}).get("id") or "").lower()
    known = {
        "qwen2.5-7b-instruct": "qwen2.5:7b-instruct",
        "mistral-nemo-instruct-2407": "mistral-nemo:12b-instruct-2407",
        "llama-3.2-3b-instruct": "llama3.2:3b-instruct",
        "gemma-4-e4b-it": "gemma3:4b-it",
    }
    if model_id in known:
        return known[model_id]
    if not model_id:
        return "unknown"
    return model_id.replace("-instruct", ":instruct").replace("-", ".")


def _recommended_next_action(
    *,
    errors: list[str],
    reachable: bool,
    model_present: bool,
    smoke_passed: bool,
    runtime_entry: dict[str, Any] | None,
) -> str:
    if errors:
        return "fix_runtime_check_errors"
    if not reachable:
        return "start_or_install_ollama"
    if not model_present:
        return "pull_or_choose_ollama_model"
    if not smoke_passed:
        return "inspect_runtime_smoke_failure"
    if (runtime_entry or {}).get("support_status") != "verified":
        return "attach_runtime_evidence_to_candidate_registry"
    return "runtime_verified_continue_release_gates"


def _attach_runtime_next_action(*, errors: list[str], write: bool) -> str:
    if errors:
        return "fix_runtime_attach_errors"
    if not write:
        return "rerun_attach_runtime_with_write_after_review"
    return "run_model_release_check"


def _apply_runtime_evidence(
    model: dict[str, Any],
    *,
    runtime_id: str,
    runtime_report: dict[str, Any],
) -> list[dict[str, Any]]:
    runtimes = model.get("runtimes") if isinstance(model.get("runtimes"), list) else []
    model["runtimes"] = runtimes
    runtime = _runtime_entry(model, runtime_id)
    changes: list[dict[str, Any]] = []
    if runtime is None:
        runtime = {"id": runtime_id}
        runtimes.append(runtime)
        changes.append({"field": "runtimes", "status": "appended", "runtime_id": _safe_text(runtime_id)})

    report_runtime = runtime_report.get("runtime") if isinstance(runtime_report.get("runtime"), dict) else {}
    report_summary = runtime_report.get("summary") if isinstance(runtime_report.get("summary"), dict) else {}
    values = {
        "support_status": "verified",
        "notes": "verified by model-runtime-check",
        "verified_at": _safe_text(runtime_report.get("generated_at")),
        "ollama_model": _safe_text(report_runtime.get("ollama_model")),
        "evidence": {
            "report_schema": MODEL_RUNTIME_CHECK_REPORT_SCHEMA,
            "runtime_verified": bool(report_summary.get("runtime_verified")),
            "model_present": bool(report_summary.get("model_present")),
            "smoke_passed": bool(report_summary.get("smoke_passed")),
            "status": _safe_text(runtime_report.get("status")),
        },
    }
    for key, value in values.items():
        if runtime.get(key) == value:
            continue
        runtime[key] = value
        changes.append({"field": f"runtimes.{runtime_id}.{key}", "status": "updated", "value_status": _value_status(value)})
    return changes


def _value_status(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return f"object[{len(value)}]"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    text = _safe_text(value) or ""
    return "present" if text else "empty"


def _check(check_id: str, status: str, title: str, message: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "title": title,
        "message": _safe_text(message),
    }


def _safe_model_view(model: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(model, dict):
        return None
    return {
        "id": _safe_text(model.get("id")),
        "status": _safe_text(model.get("status")),
        "provider": _safe_text(model.get("provider")),
        "project": _safe_text(model.get("project")),
        "license": _safe_text(model.get("license")),
        "parameter_count_b": model.get("parameter_count_b"),
        "architecture": _safe_text(model.get("architecture")),
        "context_length_tokens": model.get("context_length_tokens"),
    }


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {_safe_text(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _preview(value: Any, *, limit: int = 180) -> str | None:
    if value is None:
        return None
    text = _safe_text(value) or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text

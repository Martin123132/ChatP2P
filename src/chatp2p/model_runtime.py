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

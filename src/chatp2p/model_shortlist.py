"""Read-only shortlist for choosing first ChatP2P base model candidates."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODEL_SHORTLIST_REPORT_SCHEMA = "chatp2p.model-shortlist-report.v1"

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
class ModelShortlistConfig:
    out_dir: Path = Path(".mesh/model-shortlist")
    max_parameter_count_b: float = 12.0
    prefer_license: str = "apache-2.0"
    include_noncommercial: bool = False


def run_model_shortlist(config: ModelShortlistConfig) -> dict[str, Any]:
    """Build a conservative, read-only candidate shortlist report."""

    started_at = time.time()
    out_dir = config.out_dir.expanduser().resolve()
    entries = [_score_entry(entry, config=config) for entry in _default_entries()]
    visible_entries = [
        entry
        for entry in entries
        if config.include_noncommercial or entry["license"]["commercial_use"] != "noncommercial_only"
    ]
    ranked_entries = sorted(visible_entries, key=lambda item: (-item["score"], item["id"]))
    recommended = _recommended_entry(ranked_entries)
    report: dict[str, Any] = {
        "schema": MODEL_SHORTLIST_REPORT_SCHEMA,
        "ok": True,
        "status": "pass" if recommended else "warn",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "out_dir": _safe_text(str(out_dir)),
            "max_parameter_count_b": config.max_parameter_count_b,
            "prefer_license": _safe_text(config.prefer_license),
            "include_noncommercial": config.include_noncommercial,
        },
        "summary": {
            "candidate_count": len(ranked_entries),
            "recommended_model_id": recommended["id"] if recommended else None,
            "recommended_next_action": "review_shortlist_then_run_model_candidate" if recommended else "add_candidate_sources",
            "does_not_approve_model": True,
            "selection_note": "Shortlist evidence is not release approval; run candidate, eval, attach-eval, and release-check next.",
        },
        "recommended": recommended,
        "candidates": ranked_entries,
        "warnings": _warnings(ranked_entries),
        "errors": [],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "model-shortlist.json"
    markdown_path = out_dir / "model-shortlist.md"
    report["artifacts"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_model_shortlist_markdown(report), encoding="utf-8")
    return report


def format_model_shortlist_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    recommended = report.get("recommended") or {}
    lines = [
        f"Model shortlist: {str(report.get('status', 'unknown')).upper()}",
        f"Candidates: {summary.get('candidate_count')}",
        f"Recommended: {recommended.get('id')}",
        f"Score: {recommended.get('score')}",
        f"Next: {summary.get('recommended_next_action')}",
    ]
    if report.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if (report.get("artifacts") or {}).get("json"):
        lines.append(f"Report: {(report.get('artifacts') or {}).get('json')}")
    return "\n".join(lines)


def format_model_shortlist_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    recommended = report.get("recommended") or {}
    lines = [
        "# ChatP2P Model Shortlist",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Candidates: `{summary.get('candidate_count')}`",
        f"- Recommended model id: `{summary.get('recommended_model_id')}`",
        f"- Does not approve model: `{summary.get('does_not_approve_model')}`",
        f"- Next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Recommended",
        "",
    ]
    if recommended:
        lines.extend(
            [
                f"- `{recommended.get('id')}` score `{recommended.get('score')}`",
                f"- Project: `{recommended.get('project')}`",
                f"- License: `{(recommended.get('license') or {}).get('spdx')}`",
                f"- Source: {recommended.get('source_url')}",
                "",
                "Candidate command preview:",
                "",
                "```powershell",
                recommended.get("candidate_command", ""),
                "```",
                "",
            ]
        )
    lines.extend(["## Candidates", ""])
    for entry in report.get("candidates") or []:
        blockers = ", ".join(entry.get("blockers") or []) or "none"
        lines.append(
            f"- `{entry.get('id')}` score `{entry.get('score')}`; "
            f"license `{(entry.get('license') or {}).get('spdx')}`; blockers `{blockers}`"
        )
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    lines.append("")
    return "\n".join(lines)


def _default_entries() -> list[dict[str, Any]]:
    return [
        {
            "id": "qwen2.5-7b-instruct",
            "provider": "Qwen",
            "project": "Qwen2.5-7B-Instruct",
            "source_url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
            "model_card_url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
            "license": {
                "spdx": "Apache-2.0",
                "commercial_use": "allowed_by_reported_license",
                "review_status": "must_confirm_before_candidate_intake",
            },
            "parameter_count_b": 7.61,
            "context_length_tokens": 131072,
            "architecture": "transformer",
            "domains": ["general", "coding", "maths", "multilingual"],
            "runtime": {
                "ollama": "quantization_available_to_verify",
                "llama_cpp": "quantization_available_to_verify",
                "vllm": "model_card_quickstart",
            },
            "hardware": {"min_ram_gb_estimate": 16, "min_vram_gb_estimate": 8, "fit": "good_standard_gpu"},
            "why": "Strong first candidate: permissive reported license, 7B-class size, long context, coding/math signal.",
            "cautions": ["confirm license file", "verify hashes", "run local Ollama smoke"],
        },
        {
            "id": "gemma-4-e4b-it",
            "provider": "Google DeepMind",
            "project": "Gemma 4 E4B IT",
            "source_url": "https://ai.google.dev/gemma/docs/core/model_card_4",
            "model_card_url": "https://ai.google.dev/gemma/docs/core/model_card_4",
            "license": {
                "spdx": "Apache-2.0",
                "commercial_use": "allowed_by_reported_license",
                "review_status": "must_confirm_before_candidate_intake",
            },
            "parameter_count_b": 4.5,
            "context_length_tokens": 128000,
            "architecture": "dense_or_moe_open_model",
            "domains": ["general", "coding", "reasoning", "multimodal"],
            "runtime": {
                "ollama": "needs_local_runtime_verification",
                "llama_cpp": "needs_quantization_verification",
            },
            "hardware": {"min_ram_gb_estimate": 12, "min_vram_gb_estimate": 6, "fit": "good_standard_gpu"},
            "why": "Small modern open model family with permissive reported license and local-device focus.",
            "cautions": ["confirm exact E4B artifact", "multimodal path is out of scope for text-only V0"],
        },
        {
            "id": "mistral-nemo-instruct-2407",
            "provider": "Mistral AI / NVIDIA",
            "project": "Mistral-Nemo-Instruct-2407",
            "source_url": "https://huggingface.co/mistralai/Mistral-Nemo-Instruct-2407",
            "model_card_url": "https://huggingface.co/mistralai/Mistral-Nemo-Instruct-2407",
            "license": {
                "spdx": "Apache-2.0",
                "commercial_use": "allowed_by_reported_license",
                "review_status": "must_confirm_before_candidate_intake",
            },
            "parameter_count_b": 12,
            "context_length_tokens": 128000,
            "architecture": "transformer",
            "domains": ["general", "coding", "multilingual"],
            "runtime": {
                "ollama": "quantization_available_to_verify",
                "llama_cpp": "quantization_available_to_verify",
                "mistral_inference": "model_card_quickstart",
            },
            "hardware": {"min_ram_gb_estimate": 24, "min_vram_gb_estimate": 12, "fit": "upper_standard_gpu"},
            "why": "Apache-reported 12B option with long context and multilingual/code focus.",
            "cautions": ["heavier than 7B class", "model card notes no moderation mechanisms"],
        },
        {
            "id": "llama-3.2-3b-instruct",
            "provider": "Meta",
            "project": "Llama-3.2-3B-Instruct",
            "source_url": "https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct",
            "model_card_url": "https://www.llama.com/docs/model-cards-and-prompt-formats/llama3_2/",
            "license": {
                "spdx": "LicenseRef-Llama-3.2",
                "commercial_use": "custom_terms_review_required",
                "review_status": "must_confirm_before_candidate_intake",
            },
            "parameter_count_b": 3,
            "context_length_tokens": 128000,
            "architecture": "transformer",
            "domains": ["general", "summarization", "multilingual"],
            "runtime": {
                "ollama": "quantization_available_to_verify",
                "llama_cpp": "quantization_available_to_verify",
            },
            "hardware": {"min_ram_gb_estimate": 8, "min_vram_gb_estimate": 4, "fit": "excellent_low_end"},
            "why": "Very practical small text model for low-end contributors.",
            "cautions": ["custom Llama license requires policy review before default routing"],
        },
    ]


def _score_entry(entry: dict[str, Any], *, config: ModelShortlistConfig) -> dict[str, Any]:
    scored = json.loads(json.dumps(entry))
    blockers: list[str] = []
    score = 0
    license_spdx = str((entry.get("license") or {}).get("spdx") or "").lower()
    commercial_use = str((entry.get("license") or {}).get("commercial_use") or "")
    parameter_count = float(entry.get("parameter_count_b") or 0)
    context_length = int(entry.get("context_length_tokens") or 0)
    runtime = entry.get("runtime") if isinstance(entry.get("runtime"), dict) else {}

    if license_spdx == config.prefer_license.lower():
        score += 35
    elif "custom" in commercial_use:
        score += 10
        blockers.append("custom_license_review")
    elif commercial_use == "noncommercial_only":
        blockers.append("noncommercial_license")

    if parameter_count <= config.max_parameter_count_b:
        score += 20
    else:
        blockers.append("too_large_for_default_threshold")
    if 4 <= parameter_count <= 8:
        score += 10
    if context_length >= 32000:
        score += 10
    if runtime.get("ollama"):
        score += 15
    if runtime.get("llama_cpp"):
        score += 10
    if "multimodal" in entry.get("domains", []):
        blockers.append("multimodal_scope_review")
    if not blockers:
        score += 10

    scored["score"] = score
    scored["blockers"] = blockers
    scored["candidate_command"] = _candidate_command(scored)
    return _safe_entry(scored)


def _recommended_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None
    nonblocked = [entry for entry in entries if not entry.get("blockers")]
    return (nonblocked or entries)[0]


def _candidate_command(entry: dict[str, Any]) -> str:
    lines = [
        "python -m chatp2p.cli model candidate `",
        "  --registry D:\\ChatP2PData\\model-registry.json `",
        f"  --model-id {entry.get('id')} `",
        f"  --provider \"{entry.get('provider')}\" `",
        f"  --project \"{entry.get('project')}\" `",
        f"  --license \"{(entry.get('license') or {}).get('spdx')}\" `",
        f"  --license-url {entry.get('model_card_url')} `",
        f"  --source-url {entry.get('source_url')} `",
        f"  --parameter-count-b {entry.get('parameter_count_b')} `",
        f"  --architecture {entry.get('architecture')} `",
        f"  --context-length-tokens {entry.get('context_length_tokens')} `",
        "  --runtime \"ollama:candidate:local smoke pending\" `",
    ]
    for domain in entry.get("domains", [])[:3]:
        lines.append(f"  --domain {domain} `")
    lines.append("  --json")
    return "\n".join(lines)


def _warnings(entries: list[dict[str, Any]]) -> list[str]:
    warnings = [
        "shortlist entries are starting research evidence, not release approval",
        "confirm license files, hashes, and local runtime behavior before candidate intake write",
    ]
    if any("custom_license_review" in entry.get("blockers", []) for entry in entries):
        warnings.append("one or more entries use custom license terms requiring review")
    if any("multimodal_scope_review" in entry.get("blockers", []) for entry in entries):
        warnings.append("one or more entries include multimodal scope beyond text-only V0")
    return warnings


def _safe_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_text(entry.get("id")),
        "provider": _safe_text(entry.get("provider")),
        "project": _safe_text(entry.get("project")),
        "source_url": _safe_text(entry.get("source_url")),
        "model_card_url": _safe_text(entry.get("model_card_url")),
        "license": {
            "spdx": _safe_text((entry.get("license") or {}).get("spdx")),
            "commercial_use": _safe_text((entry.get("license") or {}).get("commercial_use")),
            "review_status": _safe_text((entry.get("license") or {}).get("review_status")),
        },
        "parameter_count_b": entry.get("parameter_count_b"),
        "context_length_tokens": entry.get("context_length_tokens"),
        "architecture": _safe_text(entry.get("architecture")),
        "domains": [_safe_text(domain) for domain in entry.get("domains", []) if isinstance(domain, str)],
        "runtime": entry.get("runtime") if isinstance(entry.get("runtime"), dict) else {},
        "hardware": entry.get("hardware") if isinstance(entry.get("hardware"), dict) else {},
        "why": _safe_text(entry.get("why")),
        "cautions": [_safe_text(caution) for caution in entry.get("cautions", []) if isinstance(caution, str)],
        "score": entry.get("score"),
        "blockers": [_safe_text(blocker) for blocker in entry.get("blockers", []) if isinstance(blocker, str)],
        "candidate_command": _safe_text(entry.get("candidate_command")),
    }


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    for pattern in _SENSITIVE_PATTERNS.values():
        text = pattern.sub("<redacted>", text)
    return text

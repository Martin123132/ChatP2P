"""Public repository privacy scanner."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PUBLIC_PRIVACY_SCAN_SCHEMA = "chatp2p.public-privacy-scan.v1"

_DOC_PRIVACY_PATTERNS: dict[str, re.Pattern[str]] = {
    "exact_worker_id": re.compile(r"\bworker_[0-9a-f]{16}\b"),
    "private_partner_repo_path": re.compile(r"E:\\ChatP2P-private-version(?:--main|-autopilot|-)?"),
    "partner_specific_invite": re.compile(r"backup-alpha-invite-glyn", re.IGNORECASE),
    "partner_name": re.compile(r"\bGlyn\b", re.IGNORECASE),
    "windows_hostname": re.compile(r"\bDESKTOP-[A-Z0-9]+\b"),
    "live_tailnet_address": re.compile(r"\b(?:100\.85\.112\.121|100\.86\.22\.29)\b"),
}

_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "google_oauth_token": re.compile(r"\bya29\.[A-Za-z0-9_-]+\b"),
    "tailscale_auth_key": re.compile(r"\btskey-[A-Za-z0-9_-]+\b"),
    "long_admission_token": re.compile(r"""admission_token["']?\s*[:=]\s*["'][^"']{20,}["']"""),
}

_PRIVATE_FILENAME_PATTERNS = [
    re.compile(r"(^|/).*alpha-invite.*\.json$", re.IGNORECASE),
    re.compile(r"(^|/).*operator-config.*\.json$", re.IGNORECASE),
]

_FALLBACK_EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}


@dataclass(frozen=True)
class PrivacyScanConfig:
    root: Path = Path(".")
    report_path: Path | None = None
    include_provider_config_filenames: bool = False


def run_public_privacy_scan(config: PrivacyScanConfig) -> dict[str, Any]:
    started_at = time.time()
    root = config.root.expanduser().resolve()
    files = _tracked_or_fallback_files(root)
    findings: list[dict[str, Any]] = []

    for relative_path in files:
        normalized_path = str(relative_path).replace("\\", "/")
        findings.extend(_scan_private_filenames(normalized_path, config))
        absolute_path = root / relative_path
        text = _read_text_or_none(absolute_path)
        if text is None:
            continue
        findings.extend(_scan_text_patterns(normalized_path, text, _SECRET_PATTERNS, "credential"))
        if _is_public_doc_path(normalized_path):
            findings.extend(_scan_text_patterns(normalized_path, text, _DOC_PRIVACY_PATTERNS, "public_doc"))

    report = {
        "ok": not findings,
        "status": "pass" if not findings else "fail",
        "schema": PUBLIC_PRIVACY_SCAN_SCHEMA,
        "root": str(root),
        "scanned_files": len(files),
        "findings": findings,
        "duration_seconds": round(time.time() - started_at, 3),
    }
    if config.report_path is not None:
        report_path = config.report_path.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        report["report_path"] = str(report_path)
    return report


def _tracked_or_fallback_files(root: Path) -> list[Path]:
    git_files = _git_ls_files(root)
    if git_files is not None:
        return git_files
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _FALLBACK_EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        files.append(relative)
    return sorted(files)


def _git_ls_files(root: Path) -> list[Path] | None:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    raw_paths = completed.stdout.decode("utf-8", errors="replace").split("\0")
    return [Path(path) for path in raw_paths if path]


def _scan_private_filenames(normalized_path: str, config: PrivacyScanConfig) -> list[dict[str, Any]]:
    patterns = list(_PRIVATE_FILENAME_PATTERNS)
    if config.include_provider_config_filenames:
        patterns.append(re.compile(r"(^|/).*provider-config.*\.json$", re.IGNORECASE))
    findings = []
    for pattern in patterns:
        if pattern.search(normalized_path):
            findings.append(
                {
                    "scope": "filename",
                    "path": normalized_path,
                    "line": None,
                    "pattern": "private_runtime_filename",
                    "match": normalized_path,
                }
            )
    return findings


def _scan_text_patterns(
    normalized_path: str,
    text: str,
    patterns: dict[str, re.Pattern[str]],
    scope: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in patterns.items():
            for match in pattern.finditer(line):
                findings.append(
                    {
                        "scope": scope,
                        "path": normalized_path,
                        "line": line_number,
                        "pattern": label,
                        "match": _safe_match(match.group(0), scope),
                    }
                )
    return findings


def _safe_match(value: str, scope: str) -> str:
    if scope == "credential":
        return "<redacted>"
    return value


def _read_text_or_none(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_public_doc_path(normalized_path: str) -> bool:
    return normalized_path == "README.md" or (
        normalized_path.startswith("docs/") and normalized_path.endswith(".md")
    )

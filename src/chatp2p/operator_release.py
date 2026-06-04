"""Read-only release readiness report for ChatP2P operators."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .privacy import PrivacyScanConfig, run_public_privacy_scan
from .runtime_metadata import collect_software_metadata, redact_remote_url, software_metadata_public_view


OPERATOR_RELEASE_CHECK_REPORT_SCHEMA = "chatp2p.operator-release-check-report.v1"


@dataclass(frozen=True)
class OperatorReleaseCheckConfig:
    repo: Path
    out_dir: Path
    console_report_path: Path | None = None
    sync_status_report_path: Path | None = None
    include_provider_config_filenames: bool = True


def run_operator_release_check(config: OperatorReleaseCheckConfig) -> dict[str, Any]:
    """Write a static local release readiness report."""

    started_at = time.time()
    now = datetime.now(timezone.utc)
    repo = config.repo.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    git = _git_summary(repo)
    privacy = run_public_privacy_scan(
        PrivacyScanConfig(
            root=repo,
            include_provider_config_filenames=config.include_provider_config_filenames,
        )
    )
    console = _optional_report_summary(config.console_report_path, expected_schema="chatp2p.operator-console-report.v1")
    sync_status = _optional_report_summary(
        config.sync_status_report_path,
        expected_schema="chatp2p.operator-sync-status-report.v1",
    )
    summary = _release_summary(git=git, privacy=privacy, console=console, sync_status=sync_status)
    json_path = out_dir / "release-check.json"
    markdown_path = out_dir / "release-check.md"
    report = {
        "schema": OPERATOR_RELEASE_CHECK_REPORT_SCHEMA,
        "ok": summary["status"] != "fail",
        "status": summary["status"],
        "generated_at": now.isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "repo": str(repo),
            "out_dir": str(out_dir),
            "console_report_path": str(config.console_report_path) if config.console_report_path else None,
            "sync_status_report_path": str(config.sync_status_report_path) if config.sync_status_report_path else None,
            "include_provider_config_filenames": config.include_provider_config_filenames,
            "read_only": True,
        },
        "summary": summary,
        "git": git,
        "privacy_scan": _privacy_public_summary(privacy),
        "operator_console": console,
        "sync_status": sync_status,
        "suggested_commands": _suggested_commands(summary, repo=repo),
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
        },
    }
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_operator_release_check_markdown(report), encoding="utf-8")
    return report


def format_operator_release_check_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    git = report.get("git") or {}
    return "\n".join(
        [
            f"ChatP2P release check: {str(report.get('status', 'unknown')).upper()}",
            f"Publish state: {summary.get('publish_state', 'unknown')}",
            f"Recommended next action: {summary.get('recommended_next_action', 'unknown')}",
            f"Branch: {git.get('branch', 'unknown')}",
            f"Local revision: {_short_revision(git.get('local_revision'))}",
            f"Origin/main revision: {_short_revision(git.get('origin_main_revision'))}",
            f"Ahead/behind: {git.get('ahead_count')} / {git.get('behind_count')}",
            f"Privacy findings: {(report.get('privacy_scan') or {}).get('finding_count', 0)}",
            f"Report: {(report.get('artifacts') or {}).get('json')}",
        ]
    )


def format_operator_release_check_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    git = report.get("git") or {}
    privacy = report.get("privacy_scan") or {}
    console = report.get("operator_console") or {}
    sync_status = report.get("sync_status") or {}
    lines = [
        "# ChatP2P Release Check",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Publish state: `{summary.get('publish_state', 'unknown')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action', 'unknown')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        "",
        "## Git",
        "",
        f"- Branch: `{git.get('branch') or 'unknown'}`",
        f"- Local revision: `{_short_revision(git.get('local_revision'))}`",
        f"- Origin/main revision: `{_short_revision(git.get('origin_main_revision'))}`",
        f"- Ahead count: `{git.get('ahead_count')}`",
        f"- Behind count: `{git.get('behind_count')}`",
        f"- Dirty: `{git.get('dirty')}`",
        f"- Remote: `{git.get('remote_url_redacted') or 'unknown'}`",
        "",
        "## Gates",
        "",
        f"- Privacy scan: `{privacy.get('status', 'unknown')}` with `{privacy.get('finding_count', 0)}` finding(s)",
        f"- Operator Console: `{console.get('status', 'not_configured')}`",
        f"- Sync status: `{sync_status.get('status', 'not_configured')}`",
    ]
    if summary.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in summary["errors"])
    if summary.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    commands = report.get("suggested_commands") or []
    if commands:
        lines.extend(["", "## Suggested Commands", ""])
        for command in commands:
            lines.extend(
                [
                    f"{command.get('label', 'Command')}:",
                    "",
                    "```powershell",
                    str(command.get("command", "")),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines)


def _git_summary(repo: Path) -> dict[str, Any]:
    local = software_metadata_public_view(collect_software_metadata(repo))
    git = shutil.which("git")
    if git is None:
        return {
            "ok": False,
            "status": "git_unavailable",
            "branch": local.get("source_branch"),
            "local_revision": local.get("source_revision"),
            "origin_main_revision": None,
            "origin_head_revision": None,
            "ahead_count": None,
            "behind_count": None,
            "dirty": local.get("source_dirty"),
            "remote_url_redacted": local.get("source_remote_url_redacted"),
            "errors": ["git executable not found"],
        }

    origin_main = _git_output(git, repo, "rev-parse", "--verify", "origin/main")
    origin_head = _git_output(git, repo, "rev-parse", "--verify", "origin/HEAD")
    ahead = None
    behind = None
    if origin_main and local.get("source_revision"):
        counts = _git_output(git, repo, "rev-list", "--left-right", "--count", "origin/main...HEAD")
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                behind = _int_or_none(parts[0])
                ahead = _int_or_none(parts[1])
    status = "pass"
    errors: list[str] = []
    if local.get("source_status") != "git":
        status = "not_git"
        errors.append("repo is not a git checkout")
    elif not origin_main:
        status = "origin_unknown"
        errors.append("origin/main revision is unknown")
    return {
        "ok": status == "pass",
        "status": status,
        "branch": local.get("source_branch"),
        "local_revision": local.get("source_revision"),
        "origin_main_revision": origin_main,
        "origin_head_revision": origin_head,
        "ahead_count": ahead,
        "behind_count": behind,
        "dirty": local.get("source_dirty"),
        "remote_url_redacted": local.get("source_remote_url_redacted") or redact_remote_url(
            _git_output(git, repo, "remote", "get-url", "origin")
        ),
        "errors": errors,
    }


def _release_summary(
    *,
    git: dict[str, Any],
    privacy: dict[str, Any],
    console: dict[str, Any],
    sync_status: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    publish_state = "ready_to_push"
    recommended = "push_origin_main"
    privacy_failed = False
    git_state_failed = False
    dirty = False
    behind = False

    if not privacy.get("ok"):
        errors.append("public privacy scan has findings")
        privacy_failed = True
    if git.get("status") != "pass":
        errors.extend(str(item) for item in git.get("errors") or [])
        git_state_failed = True
    if git.get("dirty") is True:
        errors.append("working tree has uncommitted changes")
        dirty = True
    if _positive(git.get("behind_count")):
        errors.append("local branch is behind origin/main")
        behind = True

    if errors:
        publish_state = "blocked"
        if privacy_failed:
            recommended = "fix_public_privacy_findings"
        elif dirty:
            recommended = "commit_or_stash_changes"
        elif behind:
            recommended = "sync_with_origin_main"
        elif git_state_failed:
            recommended = "repair_git_release_state"

    if not errors:
        if _positive(git.get("ahead_count")):
            publish_state = "ready_to_push"
            recommended = "push_origin_main"
        else:
            publish_state = "already_published"
            recommended = "continue_development"

    if console.get("configured") and console.get("status") == "fail":
        warnings.append("latest Operator Console report is failing")
    if sync_status.get("configured") and sync_status.get("summary", {}).get("sync_state") in {
        "waiting_for_autopull",
        "unknown_old_worker",
    }:
        warnings.append("live node revision sync is not confirmed yet")

    status = "fail" if errors else ("warn" if warnings else "pass")
    return {
        "status": status,
        "publish_state": publish_state,
        "recommended_next_action": recommended,
        "can_push": publish_state == "ready_to_push",
        "already_published": publish_state == "already_published",
        "warnings": warnings,
        "errors": errors,
    }


def _optional_report_summary(path: Path | None, *, expected_schema: str) -> dict[str, Any]:
    if path is None:
        return {
            "configured": False,
            "exists": False,
            "status": "not_configured",
            "path": None,
            "summary": {},
        }
    try:
        report = read_json_file(path, description="operator report")
    except (OSError, ValueError) as exc:
        return {
            "configured": True,
            "exists": False,
            "status": "missing_or_invalid",
            "path": str(path),
            "summary": {},
            "error": str(exc),
        }
    if not isinstance(report, dict):
        return {
            "configured": True,
            "exists": True,
            "status": "invalid",
            "path": str(path),
            "summary": {},
            "error": "report must be a JSON object",
        }
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    status = str(report.get("status") or "unknown")
    if report.get("schema") != expected_schema:
        status = "schema_mismatch"
    return {
        "configured": True,
        "exists": True,
        "status": status,
        "path": str(path),
        "generated_at": report.get("generated_at"),
        "summary": {
            "recommended_next_action": summary.get("recommended_next_action"),
            "can_continue_without_partner": summary.get("can_continue_without_partner"),
            "sync_state": summary.get("sync_state"),
        },
    }


def _privacy_public_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": report.get("schema"),
        "ok": report.get("ok"),
        "status": report.get("status"),
        "finding_count": len(report.get("findings") or []),
        "scanned_files": report.get("scanned_files"),
    }


def _suggested_commands(summary: dict[str, Any], *, repo: Path) -> list[dict[str, str]]:
    action = summary.get("recommended_next_action")
    if action == "push_origin_main":
        return [_command("Push current branch", "git push origin main")]
    if action == "fix_public_privacy_findings":
        return [
            _command(
                "Run public privacy scan",
                f"python -m chatp2p.cli operator privacy-scan --root {_ps(repo)} --include-provider-config-filenames",
            )
        ]
    if action == "sync_with_origin_main":
        return [_command("Review incoming changes", "git fetch origin")]
    if action == "commit_or_stash_changes":
        return [_command("Review working tree", "git status --short --branch")]
    return []


def _command(label: str, command: str) -> dict[str, str]:
    return {
        "label": label,
        "shell": "powershell",
        "command": command,
    }


def _git_output(git: str, cwd: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            [git, *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None


def _positive(value: Any) -> bool:
    parsed = _int_or_none(value)
    return parsed is not None and parsed > 0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _short_revision(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "unknown"
    return value[:12]


def _ps(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"

"""Daily operator check for ChatP2P workstations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .alpha import AlphaReliabilityPackConfig, run_alpha_reliability_pack
from .operator_actions import build_operator_action_queue, write_operator_action_queue
from .operator_console import OperatorConsoleConfig, run_operator_console
from .privacy import PrivacyScanConfig, run_public_privacy_scan


OPERATOR_DAILY_CHECK_SCHEMA = "chatp2p.operator-daily-check-report.v1"


@dataclass(frozen=True)
class OperatorDailyCheckConfig:
    repo: Path
    home: Path
    primary_invite_path: Path
    out_dir: Path
    backup_invite_path: Path | None = None
    reliability_dir: Path | None = None
    console_out_dir: Path | None = None
    partner_report_paths: tuple[Path, ...] = ()
    expected_primary_worker_id: str | None = None
    expected_backup_worker_id: str | None = None
    skip_network_checks: bool = False
    refresh_reliability_pack: bool = False
    include_deterministic_smoke: bool = False
    timeout_seconds: float = 90.0
    status_timeout_seconds: float = 5.0
    poll_interval: float = 0.5
    inference_jobs: int = 4
    smoke_jobs: int = 4
    min_live_workers: int = 1
    freshness_seconds: float = 3600.0
    history_limit: int = 20
    stale_report_root: Path | None = None
    stale_report_days: float = 2.0
    stale_report_max_items: int = 50


def run_operator_daily_check(config: OperatorDailyCheckConfig) -> dict[str, Any]:
    _validate_daily_check_config(config)
    started_at = time.time()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    console_out_dir = (
        config.console_out_dir.expanduser().resolve()
        if config.console_out_dir is not None
        else out_dir / "operator-console"
    )
    now = datetime.now(timezone.utc)

    privacy_report_path = out_dir / "daily-privacy-scan.json"
    privacy = run_public_privacy_scan(
        PrivacyScanConfig(root=config.repo, report_path=privacy_report_path)
    )
    reliability_refresh = _run_reliability_refresh(config, out_dir=out_dir)
    console = run_operator_console(
        OperatorConsoleConfig(
            repo=config.repo,
            home=config.home,
            primary_invite_path=config.primary_invite_path,
            backup_invite_path=config.backup_invite_path,
            reliability_dir=config.reliability_dir,
            out_dir=console_out_dir,
            partner_report_paths=config.partner_report_paths,
            expected_primary_worker_id=config.expected_primary_worker_id,
            expected_backup_worker_id=config.expected_backup_worker_id,
            skip_network_checks=config.skip_network_checks,
            timeout_seconds=config.status_timeout_seconds,
            freshness_seconds=config.freshness_seconds,
            history_limit=config.history_limit,
            stale_report_root=config.stale_report_root,
            stale_report_days=config.stale_report_days,
            stale_report_max_items=config.stale_report_max_items,
            daily_check_dir=out_dir,
        )
    )
    summary = _daily_summary(
        privacy=privacy,
        reliability_refresh=reliability_refresh,
        console=console,
    )
    self_heal = console.get("self_heal") if isinstance(console.get("self_heal"), dict) else {}

    json_path = out_dir / "daily-check.json"
    markdown_path = out_dir / "daily-check.md"
    action_queue_json_path = out_dir / "action-queue.json"
    action_queue_markdown_path = out_dir / "action-queue.md"
    report = {
        "schema": OPERATOR_DAILY_CHECK_SCHEMA,
        "ok": summary["status"] != "fail",
        "status": summary["status"],
        "generated_at": now.isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "repo": str(config.repo.expanduser().resolve()),
            "home": str(config.home.expanduser().resolve()),
            "primary_invite_path": str(config.primary_invite_path),
            "backup_invite_path": str(config.backup_invite_path) if config.backup_invite_path else None,
            "reliability_dir": str(config.reliability_dir) if config.reliability_dir else None,
            "out_dir": str(out_dir),
            "console_out_dir": str(console_out_dir),
            "refresh_reliability_pack": config.refresh_reliability_pack,
            "include_deterministic_smoke": config.include_deterministic_smoke,
            "skip_network_checks": config.skip_network_checks,
        },
        "summary": summary,
        "steps": {
            "privacy_scan": _privacy_step_summary(privacy),
            "reliability_refresh": reliability_refresh,
            "operator_console": _console_step_summary(console),
            "self_heal": _self_heal_step_summary(self_heal),
        },
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
            "privacy_scan": str(privacy_report_path),
            "operator_console_json": (console.get("artifacts") or {}).get("json"),
            "operator_console_markdown": (console.get("artifacts") or {}).get("markdown"),
            "operator_console_html": (console.get("artifacts") or {}).get("html"),
            "operator_self_heal": (console.get("artifacts") or {}).get("self_heal_report"),
            "action_queue_json": str(action_queue_json_path),
            "action_queue_markdown": str(action_queue_markdown_path),
        },
    }
    action_queue = build_operator_action_queue(report)
    report["action_queue"] = action_queue
    write_operator_action_queue(out_dir, action_queue)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_operator_daily_check_markdown(report), encoding="utf-8")
    return report


def format_operator_daily_check_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    artifacts = report.get("artifacts", {})
    return "\n".join(
        [
            f"ChatP2P daily check: {str(report.get('status', 'unknown')).upper()}",
            f"Can continue without partner: {_yes_no(summary.get('can_continue_without_partner'))}",
            f"Next action: {summary.get('recommended_next_action', 'unknown')}",
            f"Top queued action: {((report.get('action_queue') or {}).get('next_action') or {}).get('action_id', 'none')}",
            f"Privacy scan: {str((report.get('steps') or {}).get('privacy_scan', {}).get('status', 'unknown')).upper()}",
            f"Operator console: {str((report.get('steps') or {}).get('operator_console', {}).get('status', 'unknown')).upper()}",
            f"Self-heal: {str((report.get('steps') or {}).get('self_heal', {}).get('status', 'unknown')).upper()}",
            f"Open: {artifacts.get('operator_console_html')}",
        ]
    )


def format_operator_daily_check_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    steps = report.get("steps", {})
    artifacts = report.get("artifacts", {})
    action_queue = report.get("action_queue") or {}
    lines = [
        "# ChatP2P Daily Check",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Can continue without partner: **{_yes_no(summary.get('can_continue_without_partner'))}**",
        f"- Recommended next action: `{summary.get('recommended_next_action', 'unknown')}`",
        f"- Top queued action: `{(action_queue.get('next_action') or {}).get('action_id', 'none')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        "",
        "## Steps",
        "",
        "| Step | Status | Notes |",
        "| --- | --- | --- |",
    ]
    for key, label in (
        ("privacy_scan", "Privacy scan"),
        ("reliability_refresh", "Reliability refresh"),
        ("operator_console", "Operator console"),
        ("self_heal", "Self-heal"),
    ):
        step = steps.get(key, {})
        lines.append(f"| {label} | {step.get('status', 'unknown')} | {step.get('message', '')} |")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            f"- Daily JSON: `{artifacts.get('json')}`",
            f"- Daily Markdown: `{artifacts.get('markdown')}`",
            f"- Operator Console HTML: `{artifacts.get('operator_console_html')}`",
            f"- Self-Heal report: `{artifacts.get('operator_self_heal')}`",
            f"- Action Queue JSON: `{artifacts.get('action_queue_json')}`",
            f"- Action Queue Markdown: `{artifacts.get('action_queue_markdown')}`",
        ]
    )
    actions = action_queue.get("actions") or []
    if actions:
        lines.extend(
            [
                "",
                "## Action Queue",
                "",
                "| Rank | Severity | Action | Partner required |",
                "| --- | --- | --- | --- |",
            ]
        )
        for action in actions[:6]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(action.get("rank")),
                        str(action.get("severity")),
                        f"`{action.get('action_id')}`",
                        _yes_no(action.get("partner_required")),
                    ]
                )
                + " |"
            )
    if summary.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    if summary.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in summary["errors"])
    lines.append("")
    return "\n".join(lines)


def _run_reliability_refresh(config: OperatorDailyCheckConfig, *, out_dir: Path) -> dict[str, Any]:
    if not config.refresh_reliability_pack:
        return {
            "ok": True,
            "status": "skipped",
            "message": "Reliability pack refresh was not requested.",
        }
    if config.backup_invite_path is None or config.reliability_dir is None:
        return {
            "ok": False,
            "status": "fail",
            "message": "--backup-invite and --reliability-dir are required when --refresh-reliability-pack is used.",
        }
    try:
        report = run_alpha_reliability_pack(
            AlphaReliabilityPackConfig(
                primary_invite_path=config.primary_invite_path,
                backup_invite_path=config.backup_invite_path,
                out_dir=config.reliability_dir,
                expected_primary_worker_id=config.expected_primary_worker_id,
                expected_backup_worker_id=config.expected_backup_worker_id,
                include_deterministic_smoke=config.include_deterministic_smoke,
                smoke_jobs=config.smoke_jobs,
                inference_jobs=config.inference_jobs,
                min_live_workers=config.min_live_workers,
                status_timeout_seconds=config.status_timeout_seconds,
                timeout_seconds=config.timeout_seconds,
                poll_interval=config.poll_interval,
            )
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "fail",
            "message": f"Reliability pack refresh failed: {type(exc).__name__}: {exc}",
        }
    refresh_copy_path = out_dir / "daily-reliability-refresh.json"
    refresh_copy_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": bool(report.get("ok")),
        "status": report.get("status", "unknown"),
        "message": "Reliability pack refreshed.",
        "report_path": str(refresh_copy_path),
        "summary_path": (report.get("artifacts") or {}).get("summary_json"),
        "can_continue_without_partner": (report.get("summary") or {}).get("can_continue_without_partner"),
        "recommended_mode": (report.get("summary") or {}).get("recommended_mode"),
    }


def _daily_summary(
    *,
    privacy: dict[str, Any],
    reliability_refresh: dict[str, Any],
    console: dict[str, Any],
) -> dict[str, Any]:
    console_summary = console.get("summary") or {}
    warnings: list[str] = []
    errors: list[str] = []
    if not privacy.get("ok"):
        errors.append("public privacy scan has findings")
    if reliability_refresh.get("status") == "fail":
        errors.append("reliability pack refresh failed")
    if console.get("status") == "fail":
        errors.append("operator console is failing")
    elif console.get("status") == "warn":
        warnings.append("operator console has warnings")

    if errors:
        status = "fail"
    elif warnings:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "can_continue_without_partner": bool(privacy.get("ok")) and bool(console_summary.get("can_continue_without_partner")),
        "recommended_next_action": _daily_recommended_action(
            privacy=privacy,
            reliability_refresh=reliability_refresh,
            console=console,
        ),
        "warnings": warnings,
        "errors": errors,
    }


def _daily_recommended_action(
    *,
    privacy: dict[str, Any],
    reliability_refresh: dict[str, Any],
    console: dict[str, Any],
) -> str:
    if not privacy.get("ok"):
        return "fix_public_privacy_findings"
    if reliability_refresh.get("status") == "fail":
        return "repair_reliability_pack"
    console_summary = console.get("summary") or {}
    return str(console_summary.get("recommended_next_action") or "continue_development")


def _privacy_step_summary(report: dict[str, Any]) -> dict[str, Any]:
    findings = report.get("findings", [])
    return {
        "ok": bool(report.get("ok")),
        "status": report.get("status"),
        "message": f"{len(findings) if isinstance(findings, list) else 0} finding(s).",
        "finding_count": len(findings) if isinstance(findings, list) else 0,
        "report_path": report.get("report_path"),
    }


def _console_step_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") or {}
    return {
        "ok": bool(report.get("ok")),
        "status": report.get("status"),
        "message": str(summary.get("recommended_next_action") or ""),
        "can_continue_without_partner": summary.get("can_continue_without_partner"),
        "html": (report.get("artifacts") or {}).get("html"),
    }


def _self_heal_step_summary(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {
            "ok": True,
            "status": "missing",
            "message": "Self-heal report has not been generated yet.",
            "repairable_issue_count": None,
            "top_self_heal_action": None,
            "report_path": None,
        }
    return {
        "ok": report.get("status") != "fail",
        "status": report.get("status"),
        "message": f"{report.get('repairable_issue_count', 0)} repairable issue(s).",
        "repairable_issue_count": report.get("repairable_issue_count"),
        "top_self_heal_action": report.get("top_self_heal_action"),
        "report_path": report.get("path"),
    }


def _yes_no(value: Any) -> str:
    if value is None:
        return "unknown"
    return "yes" if bool(value) else "no"


def _validate_daily_check_config(config: OperatorDailyCheckConfig) -> None:
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.status_timeout_seconds <= 0:
        raise ValueError("--status-timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.inference_jobs < 1:
        raise ValueError("--inference-jobs must be at least 1")
    if config.smoke_jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.history_limit < 1:
        raise ValueError("--history-limit must be at least 1")
    if config.stale_report_days <= 0:
        raise ValueError("--stale-report-days must be greater than 0")
    if config.stale_report_max_items < 0:
        raise ValueError("--stale-report-max-items cannot be negative")

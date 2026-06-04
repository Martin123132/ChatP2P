"""Operator action queue generation for ChatP2P daily reports."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OPERATOR_ACTION_QUEUE_SCHEMA = "chatp2p.operator-action-queue.v1"
OPERATOR_ACTION_RUN_REPORT_SCHEMA = "chatp2p.operator-action-run-report.v1"
ALLOWED_RUN_ACTION_OPERATOR_COMMANDS = {
    "action-queue",
    "console",
    "daily-check",
    "privacy-scan",
    "reliability-pack",
    "sync-status",
}


_RECOMMENDED_ACTIONS: dict[str, dict[str, Any]] = {
    "fix_public_privacy_findings": {
        "priority": 10,
        "severity": "blocker",
        "category": "privacy",
        "title": "Fix public privacy findings",
        "detail": "Do not push until the public privacy scan is clean.",
        "partner_required": False,
    },
    "restore_primary_or_backup_coordinator": {
        "priority": 20,
        "severity": "blocker",
        "category": "network",
        "title": "Restore a working coordinator lane",
        "detail": "Primary and backup lanes are unavailable; restart or repair one lane before continuing network work.",
        "partner_required": False,
    },
    "rerun_console_with_network_checks": {
        "priority": 30,
        "severity": "warning",
        "category": "operator",
        "title": "Rerun console with network checks",
        "detail": "The current report was built offline and does not prove live coordinator health.",
        "partner_required": False,
    },
    "continue_on_backup_lane": {
        "priority": 40,
        "severity": "info",
        "category": "network",
        "title": "Continue on backup lane",
        "detail": "Primary is not ready but the backup lane is usable, so local work can continue.",
        "partner_required": False,
    },
    "run_reliability_pack_when_ready": {
        "priority": 50,
        "severity": "warning",
        "category": "evidence",
        "title": "Run reliability pack when ready",
        "detail": "The operator can continue, but reliability evidence is missing.",
        "partner_required": False,
    },
    "refresh_reliability_pack": {
        "priority": 55,
        "severity": "warning",
        "category": "evidence",
        "title": "Refresh reliability pack",
        "detail": "Existing reliability evidence is stale; refresh it when the lanes are available.",
        "partner_required": False,
    },
    "repair_lane_then_rerun_reliability_pack": {
        "priority": 60,
        "severity": "warning",
        "category": "evidence",
        "title": "Repair lane then rerun reliability pack",
        "detail": "Reliability evidence exists but is failing.",
        "partner_required": False,
    },
    "repair_reliability_pack": {
        "priority": 12,
        "severity": "blocker",
        "category": "evidence",
        "title": "Repair reliability pack",
        "detail": "The optional reliability refresh failed; inspect the refresh report before using it as evidence.",
        "partner_required": False,
    },
    "regenerate_operator_console": {
        "priority": 94,
        "severity": "warning",
        "category": "operator",
        "title": "Regenerate Operator Console",
        "detail": "The Operator Console report is missing or stale; regenerate the static local report.",
        "partner_required": False,
    },
    "refresh_daily_check_report": {
        "priority": 96,
        "severity": "warning",
        "category": "operator",
        "title": "Refresh daily check report",
        "detail": "The daily-check report is missing or stale; run the read-only daily operator gate.",
        "partner_required": False,
    },
    "refresh_action_queue": {
        "priority": 97,
        "severity": "warning",
        "category": "operator",
        "title": "Refresh action queue",
        "detail": "The action queue is missing or stale; rebuild it from the latest daily-check report.",
        "partner_required": False,
    },
    "create_action_run_report": {
        "priority": 98,
        "severity": "info",
        "category": "operator",
        "title": "Create action-run report",
        "detail": "No action-run report exists yet; dry-run or execute an allowlisted local operator action to create one.",
        "partner_required": False,
    },
    "continue_development": {
        "priority": 90,
        "severity": "info",
        "category": "development",
        "title": "Continue development",
        "detail": "The operator gate is clear and no partner action is required.",
        "partner_required": False,
    },
    "wait_for_partner_autopull": {
        "priority": 58,
        "severity": "warning",
        "category": "sync",
        "title": "Wait for partner autopull",
        "detail": "A live node is advertising an older or different public revision; wait for autopull, then confirm sync status.",
        "partner_required": False,
    },
    "partner_synced_continue": {
        "priority": 88,
        "severity": "info",
        "category": "sync",
        "title": "Partner synced, continue",
        "detail": "Live nodes that advertise revision metadata are synced with the expected public revision.",
        "partner_required": False,
    },
}


def build_operator_action_queue(daily_report: dict[str, Any]) -> dict[str, Any]:
    """Build a ranked queue from an operator daily-check or console report."""

    summary = daily_report.get("summary") or {}
    steps = _normalised_steps(daily_report)
    artifacts = daily_report.get("artifacts") or {}
    actions: list[dict[str, Any]] = []

    privacy = steps.get("privacy_scan") or {}
    reliability_refresh = steps.get("reliability_refresh") or {}
    operator_console = steps.get("operator_console") or {}

    if not privacy.get("ok"):
        _add_catalog_action(
            actions,
            "fix_public_privacy_findings",
            source="privacy_scan",
            artifacts={"privacy_scan": privacy.get("report_path")},
        )
    if reliability_refresh.get("status") == "fail":
        _add_catalog_action(
            actions,
            "repair_reliability_pack",
            source="reliability_refresh",
            artifacts={
                "reliability_refresh_report": reliability_refresh.get("report_path"),
                "reliability_summary": reliability_refresh.get("summary_path"),
            },
        )
    if operator_console.get("status") == "fail":
        _add_action(
            actions,
            action_id="repair_operator_console",
            priority=25,
            severity="blocker",
            category="operator",
            title="Repair operator console",
            detail="The static operator console is failing; inspect its JSON report before relying on next-step guidance.",
            source="operator_console",
            partner_required=False,
            artifacts={"operator_console_html": operator_console.get("html")},
        )
    _add_self_heal_actions(actions, daily_report)

    recommended = str(summary.get("recommended_next_action") or "continue_development")
    _add_catalog_action(
        actions,
        recommended,
        source="operator_summary",
        artifacts={
            "report_json": artifacts.get("json"),
            "report_markdown": artifacts.get("markdown"),
            "operator_console_html": artifacts.get("operator_console_html") or artifacts.get("html"),
        },
    )

    for warning in summary.get("warnings") or []:
        _add_action(
            actions,
            action_id=f"review_warning_{_slug(str(warning))}",
            priority=95,
            severity="warning",
            category="operator",
            title="Review operator warning",
            detail=str(warning),
            source="operator_summary",
            partner_required="partner" in str(warning).lower(),
        )
    for error in summary.get("errors") or []:
        if str(error) in {"public privacy scan has findings", "reliability pack refresh failed"}:
            continue
        _add_action(
            actions,
            action_id=f"resolve_error_{_slug(str(error))}",
            priority=15,
            severity="blocker",
            category="operator",
            title="Resolve operator error",
            detail=str(error),
            source="operator_summary",
            partner_required=False,
        )

    actions = _ranked_unique(actions)
    _attach_suggested_commands(actions, daily_report)
    return {
        "schema": OPERATOR_ACTION_QUEUE_SCHEMA,
        "generated_at": daily_report.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "status": _queue_status(actions),
        "can_continue_without_partner": bool(summary.get("can_continue_without_partner")),
        "recommended_next_action": recommended,
        "next_action": actions[0] if actions else None,
        "counts": _severity_counts(actions),
        "actions": actions,
    }


def _normalised_steps(report: dict[str, Any]) -> dict[str, Any]:
    steps = report.get("steps")
    if isinstance(steps, dict) and steps:
        return steps

    summary = report.get("summary") or {}
    privacy = report.get("privacy_scan") or {}
    artifacts = report.get("artifacts") or {}
    return {
        "privacy_scan": {
            "ok": bool(privacy.get("ok")),
            "status": privacy.get("status"),
            "finding_count": privacy.get("finding_count"),
        },
        "reliability_refresh": {
            "ok": True,
            "status": "skipped",
            "message": "Console reports do not refresh reliability evidence.",
        },
        "operator_console": {
            "ok": True,
            "status": "pass",
            "message": str(summary.get("recommended_next_action") or ""),
            "can_continue_without_partner": summary.get("can_continue_without_partner"),
            "html": artifacts.get("html"),
        },
    }


def write_operator_action_queue(out_dir: Path, queue: dict[str, Any]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "action-queue.json"
    markdown_path = out_dir / "action-queue.md"
    json_path.write_text(json.dumps(queue, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_operator_action_queue_markdown(queue), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def format_operator_action_queue_summary(queue: dict[str, Any]) -> str:
    next_action = queue.get("next_action") or {}
    return "\n".join(
        [
            f"ChatP2P action queue: {str(queue.get('status', 'unknown')).upper()}",
            f"Can continue without partner: {_yes_no(queue.get('can_continue_without_partner'))}",
            f"Next action: {next_action.get('action_id', 'none')}",
            f"Partner required: {_yes_no(next_action.get('partner_required'))}",
        ]
    )


def format_operator_action_queue_markdown(queue: dict[str, Any]) -> str:
    lines = [
        "# ChatP2P Operator Action Queue",
        "",
        f"- Status: **{str(queue.get('status', 'unknown')).upper()}**",
        f"- Can continue without partner: **{_yes_no(queue.get('can_continue_without_partner'))}**",
        f"- Recommended next action: `{queue.get('recommended_next_action', 'unknown')}`",
        f"- Generated at: `{queue.get('generated_at')}`",
        "",
        "## Actions",
        "",
        "| Rank | Severity | Action | Partner required | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for action in queue.get("actions") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(action.get("rank")),
                    str(action.get("severity")),
                    f"`{action.get('action_id')}`",
                    _yes_no(action.get("partner_required")),
                    _escape_table_text(str(action.get("detail", ""))),
                ]
            )
            + " |"
        )
    command_sections = _action_command_sections(queue)
    if command_sections:
        lines.extend(["", "## Suggested Commands", ""])
        lines.extend(command_sections)
    lines.append("")
    return "\n".join(lines)


def _add_catalog_action(
    actions: list[dict[str, Any]],
    action_id: str,
    *,
    source: str,
    artifacts: dict[str, Any] | None = None,
) -> None:
    spec = _RECOMMENDED_ACTIONS.get(action_id)
    if spec is None:
        _add_action(
            actions,
            action_id=action_id,
            priority=70,
            severity="warning",
            category="operator",
            title=action_id.replace("_", " ").capitalize(),
            detail="Review the operator console recommendation.",
            source=source,
            partner_required=False,
            artifacts=artifacts,
        )
        return
    _add_action(actions, action_id=action_id, source=source, artifacts=artifacts, **spec)


def _add_action(
    actions: list[dict[str, Any]],
    *,
    action_id: str,
    priority: int,
    severity: str,
    category: str,
    title: str,
    detail: str,
    source: str,
    partner_required: bool,
    artifacts: dict[str, Any] | None = None,
) -> None:
    actions.append(
        {
            "action_id": action_id,
            "priority": priority,
            "severity": severity,
            "category": category,
            "title": title,
            "detail": detail,
            "source": source,
            "partner_required": bool(partner_required),
            "can_run_without_partner": not bool(partner_required),
            "artifacts": {key: value for key, value in (artifacts or {}).items() if value},
        }
    )


def _ranked_unique(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for action in actions:
        existing = by_id.get(str(action.get("action_id")))
        if existing is None or int(action.get("priority", 999)) < int(existing.get("priority", 999)):
            by_id[str(action.get("action_id"))] = action
    ranked = sorted(by_id.values(), key=lambda item: (int(item.get("priority", 999)), str(item.get("action_id"))))
    for index, action in enumerate(ranked, start=1):
        action["rank"] = index
    return ranked


def _attach_suggested_commands(actions: list[dict[str, Any]], report: dict[str, Any]) -> None:
    for action in actions:
        commands = _suggested_commands_for_action(str(action.get("action_id") or ""), report)
        if commands:
            action["suggested_commands"] = commands


def _suggested_commands_for_action(action_id: str, report: dict[str, Any]) -> list[dict[str, str]]:
    commands: list[dict[str, str]] = []
    if action_id == "fix_public_privacy_findings":
        command = _privacy_scan_command(report)
        argv = _privacy_scan_argv(report)
        if command:
            commands.append(_command("Run public privacy scan", command, argv=argv))
    elif action_id in {
        "run_reliability_pack_when_ready",
        "refresh_reliability_pack",
        "repair_lane_then_rerun_reliability_pack",
        "repair_reliability_pack",
    }:
        command = _reliability_pack_command(report)
        argv = _reliability_pack_argv(report)
        if command:
            commands.append(_command("Refresh reliability pack", command, argv=argv))
    elif action_id in {"rerun_console_with_network_checks", "repair_operator_console"}:
        command = _operator_console_command(report, include_skip_network_checks=False)
        argv = _operator_console_argv(report, include_skip_network_checks=False)
        if command:
            commands.append(_command("Regenerate Operator Console", command, argv=argv))
    elif action_id == "regenerate_operator_console":
        command = _operator_console_command(report, include_skip_network_checks=True)
        argv = _operator_console_argv(report, include_skip_network_checks=True)
        if command:
            commands.append(_command("Regenerate Operator Console", command, argv=argv))
    elif action_id == "refresh_daily_check_report":
        command = _daily_check_command(report)
        argv = _daily_check_argv(report)
        if command:
            commands.append(_command("Run daily check now", command, argv=argv))
    elif action_id == "refresh_action_queue":
        command = _action_queue_command(report)
        argv = _action_queue_argv(report)
        if command:
            commands.append(_command("Rebuild action queue", command, argv=argv))
    elif action_id.startswith("review_warning_daily_check_automation"):
        command = _daily_check_command(report)
        argv = _daily_check_argv(report)
        if command:
            commands.append(_command("Run daily check now", command, argv=argv))
    elif action_id == "create_action_run_report":
        command = _privacy_scan_command(report)
        argv = _privacy_scan_argv(report)
        if command:
            commands.append(_command("Create action-run report with privacy scan", command, argv=argv))
    elif action_id == "wait_for_partner_autopull":
        command = _sync_status_command(report)
        argv = _sync_status_argv(report)
        if command:
            commands.append(_command("Confirm partner autopull status", command, argv=argv))
        command = _operator_console_command(report, include_skip_network_checks=False)
        argv = _operator_console_argv(report, include_skip_network_checks=False)
        if command:
            commands.append(_command("Refresh Operator Console with network checks", command, argv=argv))
    elif action_id in {"continue_development", "partner_synced_continue"}:
        command = _privacy_scan_command(report)
        argv = _privacy_scan_argv(report)
        if command:
            commands.append(_command("Recheck privacy before committing", command, argv=argv))
    return commands


def _command(label: str, command: str, *, argv: list[str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "label": label,
        "shell": "powershell",
        "command": command,
    }
    if argv:
        spec["argv"] = [str(item) for item in argv]
    return spec


def _add_self_heal_actions(actions: list[dict[str, Any]], report: dict[str, Any]) -> None:
    daily_check = report.get("daily_check_automation") if isinstance(report.get("daily_check_automation"), dict) else {}
    daily_report = daily_check.get("report") if isinstance(daily_check.get("report"), dict) else {}
    daily_report_status = str(daily_report.get("status") or "")
    if daily_report_status in {"missing", "stale"} or daily_report.get("fresh") is False:
        _add_catalog_action(
            actions,
            "refresh_daily_check_report",
            source="daily_check_automation",
            artifacts={"daily_check_report": daily_report.get("path")},
        )

    reliability = report.get("reliability") if isinstance(report.get("reliability"), dict) else {}
    reliability_status = str(reliability.get("status") or "")
    if reliability_status in {"missing", "not_configured"}:
        _add_catalog_action(
            actions,
            "run_reliability_pack_when_ready",
            source="reliability_summary",
            artifacts={"reliability_summary": reliability.get("summary_path") or reliability.get("path")},
        )
    elif reliability.get("exists") and reliability.get("fresh") is False:
        _add_catalog_action(
            actions,
            "refresh_reliability_pack",
            source="reliability_summary",
            artifacts={"reliability_summary": reliability.get("summary_path") or reliability.get("path")},
        )

    action_runner = report.get("action_runner") if isinstance(report.get("action_runner"), dict) else {}
    last_run = action_runner.get("last_run") if isinstance(action_runner.get("last_run"), dict) else {}
    if str(last_run.get("status") or "") in {"missing", "stale"}:
        _add_catalog_action(
            actions,
            "create_action_run_report",
            source="action_runner",
            artifacts={"action_run_report": last_run.get("path")},
        )

    steps = _normalised_steps(report)
    operator_console = steps.get("operator_console") or {}
    if str(operator_console.get("status") or "") in {"missing", "stale"}:
        _add_catalog_action(
            actions,
            "regenerate_operator_console",
            source="operator_console",
            artifacts={"operator_console_html": operator_console.get("html")},
        )


def _privacy_scan_command(report: dict[str, Any]) -> str | None:
    repo = _config_value(report, "repo")
    if not repo:
        return None
    return f"python -m chatp2p.cli operator privacy-scan --root {_ps(repo)}"


def _privacy_scan_argv(report: dict[str, Any]) -> list[str] | None:
    repo = _config_value(report, "repo")
    if not repo:
        return None
    return ["python", "-m", "chatp2p.cli", "operator", "privacy-scan", "--root", str(repo)]


def _reliability_pack_command(report: dict[str, Any]) -> str | None:
    primary_invite = _config_value(report, "primary_invite_path")
    backup_invite = _config_value(report, "backup_invite_path")
    reliability_dir = _config_value(report, "reliability_dir")
    if not primary_invite or not backup_invite or not reliability_dir:
        return None
    lines = [
        "python -m chatp2p.cli operator reliability-pack `",
        f"  --primary-invite {_ps(primary_invite)} `",
        f"  --backup-invite {_ps(backup_invite)} `",
    ]
    primary_worker = _config_value(report, "expected_primary_worker_id")
    backup_worker = _config_value(report, "expected_backup_worker_id")
    if primary_worker:
        lines.append(f"  --expected-primary-worker-id {_ps(primary_worker)} `")
    if backup_worker:
        lines.append(f"  --expected-backup-worker-id {_ps(backup_worker)} `")
    lines.append(f"  --out {_ps(reliability_dir)}")
    return "\n".join(lines)


def _reliability_pack_argv(report: dict[str, Any]) -> list[str] | None:
    primary_invite = _config_value(report, "primary_invite_path")
    backup_invite = _config_value(report, "backup_invite_path")
    reliability_dir = _config_value(report, "reliability_dir")
    if not primary_invite or not backup_invite or not reliability_dir:
        return None
    argv = [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "reliability-pack",
        "--primary-invite",
        str(primary_invite),
        "--backup-invite",
        str(backup_invite),
    ]
    primary_worker = _config_value(report, "expected_primary_worker_id")
    backup_worker = _config_value(report, "expected_backup_worker_id")
    if primary_worker:
        argv.extend(["--expected-primary-worker-id", str(primary_worker)])
    if backup_worker:
        argv.extend(["--expected-backup-worker-id", str(backup_worker)])
    argv.extend(["--out", str(reliability_dir)])
    return argv


def _operator_console_command(report: dict[str, Any], *, include_skip_network_checks: bool) -> str | None:
    repo = _config_value(report, "repo")
    home = _config_value(report, "home")
    primary_invite = _config_value(report, "primary_invite_path")
    out_dir = _config_value(report, "console_out_dir") or _config_value(report, "out_dir")
    if not repo or not home or not primary_invite or not out_dir:
        return None
    lines = [
        "python -m chatp2p.cli operator console `",
        f"  --repo {_ps(repo)} `",
        f"  --home {_ps(home)} `",
        f"  --primary-invite {_ps(primary_invite)} `",
    ]
    backup_invite = _config_value(report, "backup_invite_path")
    reliability_dir = _config_value(report, "reliability_dir")
    daily_check_dir = _config_value(report, "daily_check_dir") or _daily_out_dir(report)
    primary_worker = _config_value(report, "expected_primary_worker_id")
    backup_worker = _config_value(report, "expected_backup_worker_id")
    expected_revision = _config_value(report, "expected_public_revision")
    if backup_invite:
        lines.append(f"  --backup-invite {_ps(backup_invite)} `")
    if primary_worker:
        lines.append(f"  --expected-primary-worker-id {_ps(primary_worker)} `")
    if backup_worker:
        lines.append(f"  --expected-backup-worker-id {_ps(backup_worker)} `")
    if expected_revision:
        lines.append(f"  --expected-public-revision {_ps(expected_revision)} `")
    if reliability_dir:
        lines.append(f"  --reliability-dir {_ps(reliability_dir)} `")
    if daily_check_dir:
        lines.append(f"  --daily-check-dir {_ps(daily_check_dir)} `")
    if include_skip_network_checks and _config_value(report, "skip_network_checks"):
        lines.append("  --skip-network-checks `")
    lines.append(f"  --out {_ps(out_dir)}")
    return "\n".join(lines)


def _operator_console_argv(report: dict[str, Any], *, include_skip_network_checks: bool) -> list[str] | None:
    repo = _config_value(report, "repo")
    home = _config_value(report, "home")
    primary_invite = _config_value(report, "primary_invite_path")
    out_dir = _config_value(report, "console_out_dir") or _config_value(report, "out_dir")
    if not repo or not home or not primary_invite or not out_dir:
        return None
    argv = [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "console",
        "--repo",
        str(repo),
        "--home",
        str(home),
        "--primary-invite",
        str(primary_invite),
    ]
    backup_invite = _config_value(report, "backup_invite_path")
    reliability_dir = _config_value(report, "reliability_dir")
    daily_check_dir = _config_value(report, "daily_check_dir") or _daily_out_dir(report)
    primary_worker = _config_value(report, "expected_primary_worker_id")
    backup_worker = _config_value(report, "expected_backup_worker_id")
    expected_revision = _config_value(report, "expected_public_revision")
    if backup_invite:
        argv.extend(["--backup-invite", str(backup_invite)])
    if primary_worker:
        argv.extend(["--expected-primary-worker-id", str(primary_worker)])
    if backup_worker:
        argv.extend(["--expected-backup-worker-id", str(backup_worker)])
    if expected_revision:
        argv.extend(["--expected-public-revision", str(expected_revision)])
    if reliability_dir:
        argv.extend(["--reliability-dir", str(reliability_dir)])
    if daily_check_dir:
        argv.extend(["--daily-check-dir", str(daily_check_dir)])
    if include_skip_network_checks and _config_value(report, "skip_network_checks"):
        argv.append("--skip-network-checks")
    argv.extend(["--out", str(out_dir)])
    return argv


def _daily_check_command(report: dict[str, Any]) -> str | None:
    repo = _config_value(report, "repo")
    home = _config_value(report, "home")
    primary_invite = _config_value(report, "primary_invite_path")
    out_dir = _daily_out_dir(report)
    if not repo or not home or not primary_invite or not out_dir:
        return None
    lines = [
        "python -m chatp2p.cli operator daily-check `",
        f"  --repo {_ps(repo)} `",
        f"  --home {_ps(home)} `",
        f"  --primary-invite {_ps(primary_invite)} `",
    ]
    backup_invite = _config_value(report, "backup_invite_path")
    reliability_dir = _config_value(report, "reliability_dir")
    expected_revision = _config_value(report, "expected_public_revision")
    console_out = _config_value(report, "console_out_dir") or (
        _config_value(report, "out_dir") if report.get("schema") == "chatp2p.operator-console-report.v1" else None
    )
    if backup_invite:
        lines.append(f"  --backup-invite {_ps(backup_invite)} `")
    if reliability_dir:
        lines.append(f"  --reliability-dir {_ps(reliability_dir)} `")
    if expected_revision:
        lines.append(f"  --expected-public-revision {_ps(expected_revision)} `")
    lines.append(f"  --out {_ps(out_dir)} `")
    if console_out:
        lines.append(f"  --console-out {_ps(console_out)}")
    else:
        lines[-1] = lines[-1].rstrip(" `")
    return "\n".join(lines)


def _daily_check_argv(report: dict[str, Any]) -> list[str] | None:
    repo = _config_value(report, "repo")
    home = _config_value(report, "home")
    primary_invite = _config_value(report, "primary_invite_path")
    out_dir = _daily_out_dir(report)
    if not repo or not home or not primary_invite or not out_dir:
        return None
    argv = [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "daily-check",
        "--repo",
        str(repo),
        "--home",
        str(home),
        "--primary-invite",
        str(primary_invite),
    ]
    backup_invite = _config_value(report, "backup_invite_path")
    reliability_dir = _config_value(report, "reliability_dir")
    expected_revision = _config_value(report, "expected_public_revision")
    console_out = _config_value(report, "console_out_dir") or (
        _config_value(report, "out_dir") if report.get("schema") == "chatp2p.operator-console-report.v1" else None
    )
    if backup_invite:
        argv.extend(["--backup-invite", str(backup_invite)])
    if reliability_dir:
        argv.extend(["--reliability-dir", str(reliability_dir)])
    if expected_revision:
        argv.extend(["--expected-public-revision", str(expected_revision)])
    argv.extend(["--out", str(out_dir)])
    if console_out:
        argv.extend(["--console-out", str(console_out)])
    return argv


def _action_queue_command(report: dict[str, Any]) -> str | None:
    daily_report = _daily_report_path(report)
    out_dir = _action_queue_out_dir(report)
    if not daily_report or not out_dir:
        return None
    return "\n".join(
        [
            "python -m chatp2p.cli operator action-queue `",
            f"  --daily-report {_ps(daily_report)} `",
            f"  --out {_ps(out_dir)}",
        ]
    )


def _action_queue_argv(report: dict[str, Any]) -> list[str] | None:
    daily_report = _daily_report_path(report)
    out_dir = _action_queue_out_dir(report)
    if not daily_report or not out_dir:
        return None
    return [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "action-queue",
        "--daily-report",
        str(daily_report),
        "--out",
        str(out_dir),
    ]


def _sync_status_command(report: dict[str, Any]) -> str | None:
    repo = _config_value(report, "repo")
    console_report = _console_report_path(report)
    out_dir = _sync_status_out_dir(report)
    if not repo or not console_report or not out_dir:
        return None
    lines = [
        "python -m chatp2p.cli operator sync-status `",
        f"  --repo {_ps(repo)} `",
        f"  --console-report {_ps(console_report)} `",
    ]
    expected_revision = _config_value(report, "expected_public_revision")
    if expected_revision:
        lines.append(f"  --expected-public-revision {_ps(expected_revision)} `")
    lines.append(f"  --out {_ps(out_dir)}")
    return "\n".join(lines)


def _sync_status_argv(report: dict[str, Any]) -> list[str] | None:
    repo = _config_value(report, "repo")
    console_report = _console_report_path(report)
    out_dir = _sync_status_out_dir(report)
    if not repo or not console_report or not out_dir:
        return None
    argv = [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "sync-status",
        "--repo",
        str(repo),
        "--console-report",
        str(console_report),
    ]
    expected_revision = _config_value(report, "expected_public_revision")
    if expected_revision:
        argv.extend(["--expected-public-revision", str(expected_revision)])
    argv.extend(["--out", str(out_dir)])
    return argv


def _console_report_path(report: dict[str, Any]) -> Any:
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    if report.get("schema") == "chatp2p.operator-console-report.v1":
        if artifacts.get("json"):
            return artifacts.get("json")
        out_dir = _config_value(report, "out_dir")
        return str(Path(str(out_dir)) / "operator-console.json") if out_dir else None
    if artifacts.get("operator_console_json"):
        return artifacts.get("operator_console_json")
    console_out = _config_value(report, "console_out_dir")
    if console_out:
        return str(Path(str(console_out)) / "operator-console.json")
    operator_console = (report.get("steps") or {}).get("operator_console") if isinstance(report.get("steps"), dict) else {}
    html_path = operator_console.get("html") or artifacts.get("operator_console_html")
    if html_path:
        return str(Path(str(html_path)).with_name("operator-console.json"))
    return None


def _sync_status_out_dir(report: dict[str, Any]) -> Any:
    console_report = _console_report_path(report)
    if console_report:
        return str(Path(str(console_report)).parent / "sync-status")
    return None


def _daily_report_path(report: dict[str, Any]) -> Any:
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    if report.get("schema") == "chatp2p.operator-daily-check-report.v1":
        if artifacts.get("json"):
            return artifacts.get("json")
        out_dir = _config_value(report, "out_dir")
        return str(Path(str(out_dir)) / "daily-check.json") if out_dir else None
    daily_check = report.get("daily_check_automation") if isinstance(report.get("daily_check_automation"), dict) else {}
    daily_report = daily_check.get("report") if isinstance(daily_check.get("report"), dict) else {}
    return daily_report.get("path") or artifacts.get("daily_check_json")


def _action_queue_out_dir(report: dict[str, Any]) -> Any:
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    queue_path = artifacts.get("action_queue_json")
    if queue_path:
        return str(Path(str(queue_path)).parent)
    return _config_value(report, "console_out_dir") or _config_value(report, "out_dir")


def run_operator_action(
    queue: dict[str, Any],
    *,
    queue_path: Path,
    action_id: str | None = None,
    command_index: int = 0,
    dry_run: bool = True,
    out_path: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    started_at = time.time()
    selected_action = _select_action(queue, action_id)
    selected_command = _select_command(selected_action, command_index)
    argv = _validated_action_argv(selected_command)
    command_preview = selected_command.get("command")
    report_path = out_path or queue_path.expanduser().resolve().parent / "operator-action-run-report.json"
    run_cwd = cwd.expanduser().resolve() if cwd is not None else Path.cwd()
    execution: dict[str, Any] = {
        "attempted": False,
        "dry_run": dry_run,
    }

    errors: list[str] = []
    status = "dry_run"
    ok = True
    if not dry_run:
        execution = _execute_action_argv(argv, cwd=run_cwd)
        ok = bool(execution.get("ok"))
        status = "pass" if ok else "fail"
        if not ok:
            errors.append(f"command returned {execution.get('returncode')}")

    report = {
        "schema": OPERATOR_ACTION_RUN_REPORT_SCHEMA,
        "ok": ok,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "queue_path": str(queue_path.expanduser().resolve()),
        "action": {
            "action_id": selected_action.get("action_id"),
            "rank": selected_action.get("rank"),
            "severity": selected_action.get("severity"),
            "partner_required": selected_action.get("partner_required"),
            "can_run_without_partner": selected_action.get("can_run_without_partner"),
            "title": selected_action.get("title"),
        },
        "command": {
            "index": command_index,
            "label": selected_command.get("label"),
            "shell": selected_command.get("shell"),
            "preview": command_preview,
            "argv": argv,
            "allowlist": sorted(ALLOWED_RUN_ACTION_OPERATOR_COMMANDS),
        },
        "execution": execution,
        "errors": errors,
        "artifacts": {
            "json": str(report_path.expanduser().resolve()),
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def format_operator_action_run_summary(report: dict[str, Any]) -> str:
    action = report.get("action") or {}
    command = report.get("command") or {}
    execution = report.get("execution") or {}
    return "\n".join(
        [
            f"ChatP2P action run: {str(report.get('status', 'unknown')).upper()}",
            f"Action: {action.get('action_id', 'unknown')}",
            f"Dry run: {_yes_no(execution.get('dry_run'))}",
            f"Command: {command.get('label', 'unknown')}",
            f"Attempted: {_yes_no(execution.get('attempted'))}",
            f"Report: {(report.get('artifacts') or {}).get('json')}",
        ]
    )


def _select_action(queue: dict[str, Any], action_id: str | None) -> dict[str, Any]:
    actions = queue.get("actions")
    if not isinstance(actions, list):
        raise ValueError("action queue must contain an actions list")
    selected_id = action_id
    if not selected_id:
        next_action = queue.get("next_action") if isinstance(queue.get("next_action"), dict) else {}
        selected_id = next_action.get("action_id")
    for action in actions:
        if isinstance(action, dict) and action.get("action_id") == selected_id:
            if action.get("partner_required"):
                raise ValueError(f"action requires partner involvement and cannot be run locally: {selected_id}")
            return action
    raise ValueError(f"action not found in queue: {selected_id}")


def _select_command(action: dict[str, Any], command_index: int) -> dict[str, Any]:
    if command_index < 0:
        raise ValueError("--command-index cannot be negative")
    commands = action.get("suggested_commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError(f"action has no suggested commands: {action.get('action_id')}")
    if command_index >= len(commands):
        raise ValueError(f"action has no suggested command at index {command_index}")
    command = commands[command_index]
    if not isinstance(command, dict):
        raise ValueError(f"suggested command at index {command_index} is not an object")
    return command


def _validated_action_argv(command: dict[str, Any]) -> list[str]:
    raw_argv = command.get("argv")
    if not isinstance(raw_argv, list) or not raw_argv:
        raise ValueError("suggested command is missing structured argv and will not be executed")
    argv = [str(part) for part in raw_argv]
    executable_name = Path(argv[0]).name.lower()
    if not (executable_name in {"py", "py.exe", "python", "python.exe"} or executable_name.startswith("python")):
        raise ValueError(f"suggested command must use a Python executable, got: {Path(argv[0]).name}")
    if len(argv) < 5 or argv[1:4] != ["-m", "chatp2p.cli", "operator"]:
        raise ValueError("suggested command is not a chatp2p operator command")
    operator_command = argv[4]
    if operator_command not in ALLOWED_RUN_ACTION_OPERATOR_COMMANDS:
        raise ValueError(f"operator command is not allowlisted: {operator_command}")
    risky_flags = {"--admission-token", "--operator-config", "--public-alpha"}
    present_risky_flags = sorted(flag for flag in risky_flags if flag in argv)
    if present_risky_flags:
        raise ValueError(f"suggested command contains disallowed sensitive flag(s): {', '.join(present_risky_flags)}")
    return argv


def _execute_action_argv(argv: list[str], *, cwd: Path) -> dict[str, Any]:
    started_at = time.time()
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        timeout=None,
    )
    return {
        "attempted": True,
        "dry_run": False,
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "ok": completed.returncode == 0,
        "duration_seconds": round(time.time() - started_at, 3),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _daily_out_dir(report: dict[str, Any]) -> Any:
    daily_check = report.get("daily_check_automation") if isinstance(report.get("daily_check_automation"), dict) else {}
    return _config_value(report, "daily_check_dir") or daily_check.get("daily_check_dir") or (
        _config_value(report, "out_dir") if report.get("schema") == "chatp2p.operator-daily-check-report.v1" else None
    )


def _config_value(report: dict[str, Any], key: str) -> Any:
    config = report.get("config")
    if not isinstance(config, dict):
        return None
    value = config.get(key)
    if value in ("", None):
        return None
    return value


def _ps(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _action_command_sections(queue: dict[str, Any]) -> list[str]:
    sections: list[str] = []
    for action in queue.get("actions") or []:
        commands = action.get("suggested_commands") or []
        if not commands:
            continue
        sections.append(f"### `{action.get('action_id')}`")
        sections.append("")
        for command in commands:
            sections.append(f"{command.get('label', 'Run command')}:")
            sections.append("")
            sections.append("```powershell")
            sections.append(str(command.get("command", "")))
            sections.append("```")
            sections.append("")
    return sections


def _queue_status(actions: list[dict[str, Any]]) -> str:
    severities = {str(action.get("severity")) for action in actions}
    if "blocker" in severities:
        return "fail"
    if "warning" in severities:
        return "warn"
    return "pass"


def _severity_counts(actions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"blocker": 0, "warning": 0, "info": 0}
    for action in actions:
        severity = str(action.get("severity"))
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _slug(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "_" for character in value).strip("_")
    return "_".join(part for part in slug.split("_") if part)[:64] or "item"


def _escape_table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _yes_no(value: Any) -> str:
    if value is None:
        return "unknown"
    return "yes" if bool(value) else "no"

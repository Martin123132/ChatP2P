"""Operator action queue generation for ChatP2P daily reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OPERATOR_ACTION_QUEUE_SCHEMA = "chatp2p.operator-action-queue.v1"


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
    "continue_development": {
        "priority": 90,
        "severity": "info",
        "category": "development",
        "title": "Continue development",
        "detail": "The operator gate is clear and no partner action is required.",
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
        if command:
            commands.append(_command("Run public privacy scan", command))
    elif action_id in {
        "run_reliability_pack_when_ready",
        "refresh_reliability_pack",
        "repair_lane_then_rerun_reliability_pack",
        "repair_reliability_pack",
    }:
        command = _reliability_pack_command(report)
        if command:
            commands.append(_command("Refresh reliability pack", command))
    elif action_id in {"rerun_console_with_network_checks", "repair_operator_console"}:
        command = _operator_console_command(report, include_skip_network_checks=False)
        if command:
            commands.append(_command("Regenerate Operator Console", command))
    elif action_id.startswith("review_warning_daily_check_automation"):
        command = _daily_check_command(report)
        if command:
            commands.append(_command("Run daily check now", command))
    elif action_id == "continue_development":
        command = _privacy_scan_command(report)
        if command:
            commands.append(_command("Recheck privacy before committing", command))
    return commands


def _command(label: str, command: str) -> dict[str, str]:
    return {
        "label": label,
        "shell": "powershell",
        "command": command,
    }


def _privacy_scan_command(report: dict[str, Any]) -> str | None:
    repo = _config_value(report, "repo")
    if not repo:
        return None
    return f"python -m chatp2p.cli operator privacy-scan --root {_ps(repo)}"


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
    if backup_invite:
        lines.append(f"  --backup-invite {_ps(backup_invite)} `")
    if primary_worker:
        lines.append(f"  --expected-primary-worker-id {_ps(primary_worker)} `")
    if backup_worker:
        lines.append(f"  --expected-backup-worker-id {_ps(backup_worker)} `")
    if reliability_dir:
        lines.append(f"  --reliability-dir {_ps(reliability_dir)} `")
    if daily_check_dir:
        lines.append(f"  --daily-check-dir {_ps(daily_check_dir)} `")
    if include_skip_network_checks and _config_value(report, "skip_network_checks"):
        lines.append("  --skip-network-checks `")
    lines.append(f"  --out {_ps(out_dir)}")
    return "\n".join(lines)


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
    console_out = _config_value(report, "console_out_dir") or (
        _config_value(report, "out_dir") if report.get("schema") == "chatp2p.operator-console-report.v1" else None
    )
    if backup_invite:
        lines.append(f"  --backup-invite {_ps(backup_invite)} `")
    if reliability_dir:
        lines.append(f"  --reliability-dir {_ps(reliability_dir)} `")
    lines.append(f"  --out {_ps(out_dir)} `")
    if console_out:
        lines.append(f"  --console-out {_ps(console_out)}")
    else:
        lines[-1] = lines[-1].rstrip(" `")
    return "\n".join(lines)


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

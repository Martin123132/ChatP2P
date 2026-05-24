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
    """Build a ranked queue from an operator daily-check report."""

    summary = daily_report.get("summary") or {}
    steps = daily_report.get("steps") or {}
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
        source="daily_summary",
        artifacts={
            "daily_check": artifacts.get("json"),
            "daily_markdown": artifacts.get("markdown"),
            "operator_console_html": artifacts.get("operator_console_html"),
        },
    )

    for warning in summary.get("warnings") or []:
        _add_action(
            actions,
            action_id=f"review_warning_{_slug(str(warning))}",
            priority=75,
            severity="warning",
            category="operator",
            title="Review operator warning",
            detail=str(warning),
            source="daily_summary",
            partner_required="partner" in str(warning).lower(),
        )
    for error in summary.get("errors") or []:
        _add_action(
            actions,
            action_id=f"resolve_error_{_slug(str(error))}",
            priority=15,
            severity="blocker",
            category="operator",
            title="Resolve operator error",
            detail=str(error),
            source="daily_summary",
            partner_required=False,
        )

    actions = _ranked_unique(actions)
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

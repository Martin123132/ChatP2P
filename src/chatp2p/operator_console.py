"""Static operator console report for local ChatP2P workstations."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .alpha import load_alpha_invite
from .client import CoordinatorClient
from .jsonio import read_json_file
from .node_runtime import managed_processes_status
from .operator_actions import build_operator_action_queue, write_operator_action_queue
from .operator_self_heal import latest_self_heal_summary
from .privacy import PrivacyScanConfig, run_public_privacy_scan
from .runtime_metadata import collect_software_metadata, software_metadata_public_view
from .windows_task import DEFAULT_DAILY_CHECK_TASK_NAME


OPERATOR_CONSOLE_REPORT_SCHEMA = "chatp2p.operator-console-report.v1"
OPERATOR_CONSOLE_HISTORY_SCHEMA = "chatp2p.operator-console-history.v1"
DEFAULT_OPERATOR_CONSOLE_FRESHNESS_SECONDS = 3600.0
DEFAULT_OPERATOR_CONSOLE_HISTORY_LIMIT = 20
DEFAULT_STALE_REPORT_DAYS = 2.0
DEFAULT_STALE_REPORT_MAX_ITEMS = 50
DEFAULT_SNAPSHOT_SAMPLE_LIMIT = 5
_REPORT_SUFFIXES = {".json", ".md", ".zip", ".log", ".txt"}
_REPORT_NAME_HINTS = (
    "report",
    "proof",
    "smoke",
    "soak",
    "evidence",
    "ops-pack",
    "status",
    "reliability",
    "autopilot",
    "route",
)

_PATH_PRIVACY_PATTERNS = (
    (re.compile(r"ChatP2P-[^\\/\\s]*(?:private|partner)[^\\/\\s]*", re.IGNORECASE), "ChatP2P-<redacted>"),
    (re.compile(r"backup-alpha-invite-[A-Za-z0-9_-]+", re.IGNORECASE), "backup-alpha-invite-<partner>"),
)


@dataclass(frozen=True)
class OperatorConsoleConfig:
    repo: Path
    home: Path
    primary_invite_path: Path
    out_dir: Path
    backup_invite_path: Path | None = None
    reliability_dir: Path | None = None
    partner_report_paths: tuple[Path, ...] = ()
    expected_primary_worker_id: str | None = None
    expected_backup_worker_id: str | None = None
    expected_public_revision: str | None = None
    skip_network_checks: bool = False
    timeout_seconds: float = 5.0
    freshness_seconds: float = DEFAULT_OPERATOR_CONSOLE_FRESHNESS_SECONDS
    history_limit: int = DEFAULT_OPERATOR_CONSOLE_HISTORY_LIMIT
    stale_report_root: Path | None = None
    stale_report_days: float = DEFAULT_STALE_REPORT_DAYS
    stale_report_max_items: int = DEFAULT_STALE_REPORT_MAX_ITEMS
    daily_check_dir: Path | None = None
    daily_check_task_name: str | None = DEFAULT_DAILY_CHECK_TASK_NAME
    query_daily_check_task: bool = True
    model_release_status_path: Path | None = None
    model_route_plan_path: Path | None = None


def run_operator_console(config: OperatorConsoleConfig) -> dict[str, Any]:
    _validate_operator_console_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc)
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    primary_invite = _load_invite_for_redaction(config.primary_invite_path)
    backup_invite = (
        _load_invite_for_redaction(config.backup_invite_path)
        if config.backup_invite_path is not None
        else None
    )
    secrets_to_redact = tuple(
        value
        for value in (
            primary_invite.get("admission_token") if primary_invite else None,
            backup_invite.get("admission_token") if backup_invite else None,
        )
        if isinstance(value, str) and value
    )

    primary = _lane_status(
        label="primary",
        invite_path=config.primary_invite_path,
        expected_worker_id=config.expected_primary_worker_id,
        skip_network_checks=config.skip_network_checks,
        timeout_seconds=config.timeout_seconds,
    )
    backup = (
        _lane_status(
            label="backup",
            invite_path=config.backup_invite_path,
            expected_worker_id=config.expected_backup_worker_id,
            skip_network_checks=config.skip_network_checks,
            timeout_seconds=config.timeout_seconds,
        )
        if config.backup_invite_path is not None
        else _unconfigured_lane("backup")
    )
    local = _local_status(config.home)
    reliability = _reliability_summary(config.reliability_dir, now=now, freshness_seconds=config.freshness_seconds)
    privacy_scan = run_public_privacy_scan(PrivacyScanConfig(root=config.repo))
    partner_autopilot = _partner_autopilot_summaries(
        home=config.home,
        configured_paths=config.partner_report_paths,
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    daily_check = _daily_check_automation_summary(
        daily_check_dir=config.daily_check_dir,
        default_root=config.home.expanduser().resolve().parent,
        task_name=config.daily_check_task_name,
        query_task=config.query_daily_check_task,
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    software = _software_visibility_summary(
        repo=config.repo,
        expected_public_revision=config.expected_public_revision,
        primary=primary,
        backup=backup,
    )
    model_release = _model_release_status_summary(
        config.model_release_status_path,
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    model_route_plan = _model_route_plan_summary(
        config.model_route_plan_path,
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    summary = _operator_summary(
        primary=primary,
        backup=backup,
        reliability=reliability,
        privacy_scan=privacy_scan,
        partner_autopilot=partner_autopilot,
        daily_check=daily_check,
        software=software,
        model_release=model_release,
        model_route_plan=model_route_plan,
        skip_network_checks=config.skip_network_checks,
    )

    json_path = out_dir / "operator-console.json"
    markdown_path = out_dir / "operator-console.md"
    html_path = out_dir / "operator-console.html"
    action_queue_json_path = out_dir / "action-queue.json"
    action_queue_markdown_path = out_dir / "action-queue.md"
    action_run_report_path = out_dir / "operator-action-run-report.json"
    self_heal_report_path = out_dir / "operator-self-heal-report.json"
    history_path = out_dir / "operator-console-history.json"
    cleanup_plan_path = out_dir / "operator-console-cleanup-plan.ps1"
    action_run = _action_run_report_summary(
        action_run_report_path,
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    self_heal = latest_self_heal_summary(
        self_heal_report_path,
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    stale_reports = _stale_report_inventory(
        root=config.stale_report_root or config.home.expanduser().resolve().parent,
        out_dir=out_dir,
        now=now,
        stale_days=config.stale_report_days,
        max_items=config.stale_report_max_items,
    )
    _write_cleanup_plan(cleanup_plan_path, stale_reports)
    report = {
        "schema": OPERATOR_CONSOLE_REPORT_SCHEMA,
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
            "expected_primary_worker_id": config.expected_primary_worker_id,
            "expected_backup_worker_id": config.expected_backup_worker_id,
            "expected_public_revision": config.expected_public_revision,
            "skip_network_checks": config.skip_network_checks,
            "timeout_seconds": config.timeout_seconds,
            "freshness_seconds": config.freshness_seconds,
            "history_limit": config.history_limit,
            "stale_report_root": str(config.stale_report_root) if config.stale_report_root else None,
            "stale_report_days": config.stale_report_days,
            "stale_report_max_items": config.stale_report_max_items,
            "daily_check_dir": str(config.daily_check_dir) if config.daily_check_dir else None,
            "daily_check_task_name": config.daily_check_task_name,
            "query_daily_check_task": config.query_daily_check_task,
            "model_release_status_path": str(config.model_release_status_path) if config.model_release_status_path else None,
            "model_route_plan_path": str(config.model_route_plan_path) if config.model_route_plan_path else None,
        },
        "summary": summary,
        "local": local,
        "lanes": {
            "primary": primary,
            "backup": backup,
        },
        "reliability": reliability,
        "privacy_scan": _privacy_summary(privacy_scan),
        "software": software,
        "model_release": model_release,
        "model_route_plan": model_route_plan,
        "partner_autopilot": partner_autopilot,
        "daily_check_automation": daily_check,
        "action_runner": {
            "last_run": action_run,
        },
        "self_heal": self_heal,
        "stale_reports": stale_reports,
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
            "html": str(html_path),
            "action_queue_json": str(action_queue_json_path),
            "action_queue_markdown": str(action_queue_markdown_path),
            "action_run_report": str(action_run_report_path),
            "self_heal_report": str(self_heal_report_path),
            "history": str(history_path),
            "cleanup_plan": str(cleanup_plan_path),
        },
    }
    report = _redact_sensitive_report(report, secrets_to_redact)
    history_summary = _update_console_history(history_path, report, limit=config.history_limit)
    report["history"] = _redact_sensitive_report(history_summary, secrets_to_redact)
    action_queue = build_operator_action_queue(report)
    report["action_queue"] = _redact_sensitive_report(action_queue, secrets_to_redact)
    report["action_runner"]["next_action"] = _next_action_run_summary(
        report["action_queue"],
        queue_path=action_queue_json_path,
    )
    write_operator_action_queue(out_dir, report["action_queue"])

    markdown = format_operator_console_markdown(report)
    html_report = format_operator_console_html(report, markdown)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_report, encoding="utf-8")
    return report


def format_operator_console_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    artifacts = report.get("artifacts", {})
    stale_reports = report.get("stale_reports", {})
    history = report.get("history", {})
    top_action = ((report.get("action_queue") or {}).get("next_action") or {})
    daily_check = report.get("daily_check_automation") or {}
    self_heal = report.get("self_heal") or {}
    software = report.get("software") or {}
    model_release = report.get("model_release") or {}
    model_route_plan = report.get("model_route_plan") or {}
    model_route_plan = report.get("model_route_plan") or {}
    lines = [
        f"ChatP2P operator console: {str(report.get('status', 'unknown')).upper()}",
        f"Can continue without partner: {_yes_no(summary.get('can_continue_without_partner'))}",
        f"Recommended next action: {summary.get('recommended_next_action', 'unknown')}",
        f"Top queued action: {top_action.get('action_id', 'none')}",
        f"Partner required: {_yes_no(top_action.get('partner_required'))}",
        f"Daily check automation: {daily_check.get('status', 'unknown')}",
        f"Self-heal: {self_heal.get('status', 'missing')} issues={self_heal.get('repairable_issue_count', 0)}",
        f"Software sync: {software.get('status', 'unknown')} expected={_short_revision(software.get('expected_public_revision'))}",
        f"Model release: {model_release.get('status', 'not_configured')} stage={model_release.get('pipeline_stage', 'unknown')}",
        f"Model route plan: {model_route_plan.get('status', 'not_configured')} ready={_yes_no(model_route_plan.get('route_ready'))}",
        f"Primary lane: {_lane_brief((report.get('lanes') or {}).get('primary', {}))}",
        f"Backup lane: {_lane_brief((report.get('lanes') or {}).get('backup', {}))}",
        f"Privacy scan: {str((report.get('privacy_scan') or {}).get('status', 'unknown')).upper()}",
        f"History changes: {len(history.get('changes', []))}",
        f"Stale report candidates: {stale_reports.get('candidate_count', 0)}",
    ]
    html_path = artifacts.get("html")
    if html_path:
        lines.append(f"HTML report: {html_path}")
    return "\n".join(lines)


def format_operator_console_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lanes = report.get("lanes", {})
    reliability = report.get("reliability", {})
    privacy = report.get("privacy_scan", {})
    partner_reports = report.get("partner_autopilot", {}).get("reports", [])
    local_processes = report.get("local", {}).get("managed_processes", [])
    history = report.get("history", {})
    stale_reports = report.get("stale_reports", {})
    action_queue = report.get("action_queue", {})
    daily_check = report.get("daily_check_automation") or {}
    action_runner = report.get("action_runner") or {}
    self_heal = report.get("self_heal") or {}
    software = report.get("software") or {}
    model_release = report.get("model_release") or {}
    model_route_plan = report.get("model_route_plan") or {}
    top_action = action_queue.get("next_action") or {}
    next_runner = action_runner.get("next_action") or {}
    last_action_run = action_runner.get("last_run") or {}

    lines = [
        "# ChatP2P Operator Console",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Can continue without partner: **{_yes_no(summary.get('can_continue_without_partner'))}**",
        f"- Recommended next action: `{summary.get('recommended_next_action', 'unknown')}`",
        f"- Top queued action: `{top_action.get('action_id', 'none')}`",
        f"- Partner required: **{_yes_no(top_action.get('partner_required'))}**",
        f"- Daily check automation: `{daily_check.get('status', 'unknown')}`",
        f"- Self-heal: `{self_heal.get('status', 'missing')}` ({self_heal.get('repairable_issue_count', 0)} repairable issue(s))",
        f"- Software sync: `{software.get('status', 'unknown')}`",
        f"- Model release: `{model_release.get('status', 'not_configured')}` stage=`{model_release.get('pipeline_stage', 'unknown')}`",
        f"- Model route plan: `{model_route_plan.get('status', 'not_configured')}` route_ready=`{model_route_plan.get('route_ready')}`",
        f"- Last action run: `{last_action_run.get('status', 'missing')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        "",
        "## Run Next Action",
        "",
        f"- Action: `{next_runner.get('action_id', 'none')}`",
        f"- Last run status: `{last_action_run.get('status', 'missing')}`",
        f"- Last run generated at: `{last_action_run.get('generated_at')}`",
        f"- Last run report: `{last_action_run.get('path')}`",
        "",
        "Dry run:",
        "",
        "```powershell",
        str(next_runner.get("dry_run_command") or "No local command is available for the next action."),
        "```",
        "",
        "Execute:",
        "",
        "```powershell",
        str(next_runner.get("execute_command") or "No local command is available for the next action."),
        "```",
        "",
        "## Self-Heal",
        "",
        f"- Latest report status: `{self_heal.get('status', 'missing')}`",
        f"- Latest report fresh: `{self_heal.get('fresh')}`",
        f"- Repairable issue count: `{self_heal.get('repairable_issue_count', 0)}`",
        f"- Top self-heal action: `{self_heal.get('top_self_heal_action') or 'none'}`",
        f"- Report path: `{self_heal.get('path')}`",
        "",
        "Self-heal dry run:",
        "",
        "```powershell",
        str(self_heal.get("top_dry_run_command") or _operator_self_heal_command(report)),
        "```",
        "",
        "Self-heal execute:",
        "",
        "```powershell",
        str(self_heal.get("top_execute_command") or "Run operator self-heal first; V1 does not auto-execute repairs."),
        "```",
        "",
        "## Action Queue",
        "",
        "| Rank | Severity | Action | Partner required | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for action in action_queue.get("actions") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(action.get("rank")),
                    str(action.get("severity")),
                    f"`{action.get('action_id')}`",
                    _yes_no(action.get("partner_required")),
                    _markdown_table_text(str(action.get("detail", ""))),
                ]
            )
            + " |"
        )
    lines.extend(_software_sync_markdown_section(software))
    lines.extend(_model_release_markdown_section(model_release))
    lines.extend(_model_route_plan_markdown_section(model_route_plan))
    lines.extend(
        [
            "",
            "## Lanes",
            "",
            "| Lane | Ready | Health | Live workers | Revision sync | Expected worker | Disputes |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for label in ("primary", "backup"):
        lane = lanes.get(label, {})
        expected = lane.get("expected_worker") or {}
        expected_text = "not set"
        if expected:
            expected_text = f"{_yes_no(expected.get('live'))} ({expected.get('node_id')})"
        lines.append(
            "| "
            + " | ".join(
                [
                    label,
                    _yes_no(lane.get("ready")),
                    str(lane.get("status", "unknown")),
                    str((lane.get("snapshot_summary") or {}).get("live_nodes", "-")),
                    str(((software.get("lanes") or {}).get(label) or {}).get("status", "unknown")),
                    expected_text,
                    str((lane.get("snapshot_summary") or {}).get("disputed_jobs", "-")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Scheduled Automation",
            "",
            f"- Status: `{daily_check.get('status', 'unknown')}`",
            f"- Task query: `{(daily_check.get('task') or {}).get('status', 'unknown')}`",
            f"- Task state: `{(daily_check.get('task') or {}).get('task_status') or '-'}`",
            f"- Last daily report: `{(daily_check.get('report') or {}).get('status', 'unknown')}`",
            f"- Report fresh: `{(daily_check.get('report') or {}).get('fresh')}`",
            f"- Report path: `{(daily_check.get('report') or {}).get('path')}`",
        ]
    )

    lines.extend(
        [
            "",
            "## Local Processes",
            "",
            "| Role | Managed | Alive | PID |",
            "| --- | --- | --- | --- |",
        ]
    )
    for process in local_processes:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(process.get("role")),
                    _yes_no(process.get("managed")),
                    _yes_no(process.get("alive")),
                    str(process.get("pid", "-")),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Reliability",
            "",
            f"- Status: `{reliability.get('status', 'not_configured')}`",
            f"- Can continue without partner: `{reliability.get('can_continue_without_partner')}`",
            f"- Recommended mode: `{reliability.get('recommended_mode')}`",
            f"- Fresh: `{reliability.get('fresh')}`",
            "",
            "## Privacy",
            "",
            f"- Status: `{privacy.get('status', 'unknown')}`",
            f"- Findings: `{privacy.get('finding_count', 0)}`",
            "",
            "## Action History",
            "",
            f"- History file: `{history.get('path')}`",
            f"- Entries kept: `{history.get('entries_kept')}`",
            f"- Previous generated at: `{(history.get('previous_entry') or {}).get('generated_at')}`",
        ]
    )
    if history.get("changes"):
        lines.extend(["", "Changes since previous run:", ""])
        lines.extend(f"- {change}" for change in history["changes"])
    else:
        lines.extend(["", "Changes since previous run: none"])
    lines.extend(
        [
            "",
            "## Stale Report Cleanup",
            "",
            f"- Candidate count: `{stale_reports.get('candidate_count', 0)}`",
            f"- Cleanup plan: `{stale_reports.get('cleanup_plan_path')}`",
            f"- Automatic deletion: `false`",
        ]
    )
    candidates = stale_reports.get("candidates", [])
    if candidates:
        lines.extend(["", "| Path | Age days | Size bytes |", "| --- | --- | --- |"])
        for candidate in candidates[:10]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(candidate.get("path")),
                        str(candidate.get("age_days")),
                        str(candidate.get("size_bytes")),
                    ]
                )
                + " |"
            )
    if partner_reports:
        lines.extend(["", "## Partner Autopilot", "", "| Report | Status | Fresh | Finished |", "| --- | --- | --- | --- |"])
        for partner in partner_reports:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(partner.get("name")),
                        str(partner.get("status")),
                        str(partner.get("fresh")),
                        str(partner.get("finished_at") or partner.get("generated_at") or "-"),
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


def format_operator_console_html(report: dict[str, Any], markdown: str | None = None) -> str:
    summary = report.get("summary", {})
    lanes = report.get("lanes", {})
    action_queue = report.get("action_queue", {})
    daily_check = report.get("daily_check_automation") or {}
    action_runner = report.get("action_runner") or {}
    self_heal = report.get("self_heal") or {}
    software = report.get("software") or {}
    model_release = report.get("model_release") or {}
    model_route_plan = report.get("model_route_plan") or {}
    top_action = action_queue.get("next_action") or {}
    status = str(report.get("status", "unknown"))
    status_class = "ok" if status == "pass" else ("warn" if status == "warn" else "fail")
    lane_cards = "\n".join(_lane_card(label, lanes.get(label, {})) for label in ("primary", "backup"))
    action_rows = _action_queue_rows(action_queue)
    run_next_section = _run_next_action_section(action_runner)
    self_heal_section = _self_heal_section(self_heal, report)
    markdown = markdown or format_operator_console_markdown(report)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ChatP2P Operator Console</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --line: #d9dee8;
      --ink: #18202f;
      --muted: #5d687a;
      --ok: #147a50;
      --warn: #9a6400;
      --fail: #b3261e;
      --accent: #2458c8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Segoe UI, Arial, sans-serif;
      line-height: 1.45;
    }}
    main {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 28px 18px 40px;
    }}
    header {{
      display: grid;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
      margin-bottom: 18px;
    }}
    h1 {{ margin: 0; font-size: 30px; letter-spacing: 0; }}
    h2 {{ margin: 28px 0 12px; font-size: 18px; letter-spacing: 0; }}
    .status {{
      display: inline-flex;
      width: fit-content;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 5px 9px;
      font-weight: 700;
      background: var(--panel);
    }}
    .status.ok {{ color: var(--ok); }}
    .status.warn {{ color: var(--warn); }}
    .status.fail {{ color: var(--fail); }}
    .severity {{
      display: inline-flex;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
    }}
    .severity.info {{ color: var(--accent); }}
    .severity.warning {{ color: var(--warn); }}
    .severity.blocker {{ color: var(--fail); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .label {{ color: var(--muted); font-size: 13px; margin: 0 0 4px; }}
    .value {{ margin: 0; font-size: 17px; font-weight: 650; overflow-wrap: anywhere; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      text-align: left;
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-size: 13px; }}
    tr:last-child td {{ border-bottom: 0; }}
    code, pre {{
      font-family: Consolas, monospace;
      font-size: 13px;
    }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #101828;
      color: #f4f7fb;
      padding: 14px;
      border-radius: 8px;
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>ChatP2P Operator Console</h1>
      <span class="status {status_class}">{html.escape(status.upper())}</span>
      <div class="grid">
        <div class="panel">
          <p class="label">Can continue without partner</p>
          <p class="value">{html.escape(_yes_no(summary.get("can_continue_without_partner")))}</p>
        </div>
        <div class="panel">
          <p class="label">Recommended next action</p>
          <p class="value">{html.escape(str(summary.get("recommended_next_action", "unknown")))}</p>
        </div>
        <div class="panel">
          <p class="label">Top queued action</p>
          <p class="value">{html.escape(str(top_action.get("action_id", "none")))}</p>
        </div>
        <div class="panel">
          <p class="label">Partner required</p>
          <p class="value">{html.escape(_yes_no(top_action.get("partner_required")))}</p>
        </div>
        <div class="panel">
          <p class="label">Daily check automation</p>
          <p class="value">{html.escape(str(daily_check.get("status", "unknown")).upper())}</p>
        </div>
        <div class="panel">
          <p class="label">Self-heal</p>
          <p class="value">{html.escape(str(self_heal.get("status", "missing")).upper())}</p>
        </div>
        <div class="panel">
          <p class="label">Software sync</p>
          <p class="value">{html.escape(str(software.get("status", "unknown")).upper())}</p>
        </div>
        <div class="panel">
          <p class="label">Model release</p>
          <p class="value">{html.escape(str(model_release.get("status", "not_configured")).upper())}</p>
        </div>
        <div class="panel">
          <p class="label">Model route plan</p>
          <p class="value">{html.escape(str(model_route_plan.get("status", "not_configured")).upper())}</p>
        </div>
        <div class="panel">
      <p class="label">Generated</p>
          <p class="value">{html.escape(str(report.get("generated_at", "-")))}</p>
        </div>
      </div>
    </header>
    {run_next_section}
    {self_heal_section}
    <section>
      <h2>Action Queue</h2>
      <table>
        <thead>
          <tr>
            <th>Rank</th>
            <th>Severity</th>
            <th>Action</th>
            <th>Partner</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>{action_rows}</tbody>
      </table>
    </section>
    <section>
      <h2>Scheduled Automation</h2>
      {_daily_check_automation_table(daily_check)}
    </section>
    <section>
      <h2>Software Sync</h2>
      {_software_sync_table(software)}
    </section>
    <section>
      <h2>Model Release</h2>
      {_model_release_table(model_release)}
    </section>
    <section>
      <h2>Model Route Plan</h2>
      {_model_route_plan_table(model_route_plan)}
    </section>
    <section>
      <h2>Lanes</h2>
      <div class="grid">{lane_cards}</div>
    </section>
    <section>
      <h2>Full Report</h2>
      <pre>{html.escape(markdown)}</pre>
    </section>
  </main>
</body>
</html>
"""


def _lane_status(
    *,
    label: str,
    invite_path: Path,
    expected_worker_id: str | None,
    skip_network_checks: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    lane: dict[str, Any] = {
        "label": label,
        "configured": True,
        "invite_path": str(invite_path),
        "expected_worker_id": expected_worker_id,
        "network_checked": not skip_network_checks,
        "ready": False,
        "status": "skipped" if skip_network_checks else "unknown",
        "errors": [],
        "warnings": [],
    }
    try:
        invite = load_alpha_invite(invite_path)
        lane["invite"] = invite.public_summary()
        lane["coordinator"] = invite.coordinator
    except Exception as exc:
        lane["status"] = "fail"
        lane["errors"].append(f"invite_error: {_error_message(exc)}")
        return lane

    if skip_network_checks:
        lane["warnings"].append("network checks skipped")
        return lane

    client = CoordinatorClient(invite.coordinator, admission_token=invite.admission_token, timeout_seconds=timeout_seconds)
    health = _client_call(lambda: client.health(), url=invite.coordinator)
    lane["health"] = health
    if not health.get("ok"):
        lane["status"] = "fail"
        lane["errors"].append("coordinator health unreachable")
        return lane

    snapshot_result = _client_call(lambda: client.snapshot(), url=f"{invite.coordinator.rstrip('/')}/api/snapshot")
    snapshot = snapshot_result.get("payload") if snapshot_result.get("ok") else None
    lane["snapshot"] = _bounded_snapshot_result(snapshot_result)
    if not snapshot_result.get("ok"):
        lane["status"] = "fail"
        lane["errors"].append("coordinator snapshot unreachable")
        return lane

    snapshot_summary = _snapshot_summary(snapshot)
    expected_worker = _expected_worker_summary(snapshot, expected_worker_id)
    criteria = _lane_criteria(snapshot_summary, expected_worker, expected_worker_id)
    lane.update(
        {
            "snapshot_summary": snapshot_summary,
            "expected_worker": expected_worker,
            "software_nodes": _software_nodes_from_snapshot(snapshot),
            "criteria": criteria,
            "ready": all(item["passed"] for item in criteria.values()),
            "status": "pass" if all(item["passed"] for item in criteria.values()) else "fail",
        }
    )
    if not lane["ready"]:
        lane["errors"].extend(
            name for name, item in criteria.items() if not item["passed"]
        )
    return lane


def _unconfigured_lane(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "configured": False,
        "network_checked": False,
        "ready": False,
        "status": "not_configured",
        "software_nodes": [],
        "errors": [],
        "warnings": [],
    }


def _client_call(call: Any, *, url: str) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "status": "pass",
            "url": url,
            "payload": call(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "fail",
            "url": url,
            "error": _error_message(exc),
        }


def _snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    status = snapshot.get("status", {}) if isinstance(snapshot, dict) else {}
    return {
        "known_nodes": _int_or_none(status.get("known_nodes")),
        "live_nodes": _int_or_zero(status.get("live_nodes")),
        "stale_nodes": _int_or_zero(status.get("stale_nodes")),
        "offline_nodes": _int_or_zero(status.get("offline_nodes")),
        "jobs": _int_or_zero(status.get("jobs")),
        "verified_jobs": _int_or_zero(status.get("verified_jobs")),
        "disputed_jobs": _int_or_zero(status.get("disputed_jobs")),
        "expired_jobs": _int_or_zero(status.get("expired_jobs")),
        "queued_jobs": _int_or_zero(status.get("queued_jobs")),
        "pending_jobs": _int_or_zero(status.get("pending_jobs")),
        "leased_jobs": _int_or_zero(status.get("leased_jobs")),
    }


def _bounded_snapshot_result(
    snapshot_result: dict[str, Any],
    *,
    sample_limit: int = DEFAULT_SNAPSHOT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    if not snapshot_result.get("ok") or not isinstance(snapshot_result.get("payload"), dict):
        return snapshot_result
    bounded = {
        key: value
        for key, value in snapshot_result.items()
        if key != "payload"
    }
    bounded["payload"] = _bounded_snapshot_payload(snapshot_result["payload"], sample_limit=sample_limit)
    return bounded


def _bounded_snapshot_payload(snapshot: dict[str, Any], *, sample_limit: int) -> dict[str, Any]:
    nodes = [item for item in snapshot.get("nodes", []) if isinstance(item, dict)]
    jobs = [item for item in snapshot.get("jobs", []) if isinstance(item, dict)]
    results = [item for item in snapshot.get("results", []) if isinstance(item, dict)]
    reputation = [item for item in snapshot.get("reputation", []) if isinstance(item, dict)]
    return {
        "status": snapshot.get("status", {}),
        "provider": snapshot.get("provider", {}),
        "counts": {
            "nodes": len(nodes),
            "jobs": len(jobs),
            "results": len(results),
            "reputation": len(reputation),
        },
        "nodes": [_node_snapshot_digest(item) for item in _sample_nodes(nodes, sample_limit)],
        "jobs": [_job_snapshot_digest(item) for item in _tail_sample(jobs, sample_limit)],
        "results": [_result_snapshot_digest(item) for item in _tail_sample(results, sample_limit)],
        "truncated": {
            "nodes": len(nodes) > sample_limit,
            "jobs": len(jobs) > sample_limit,
            "results": len(results) > sample_limit,
            "reputation": len(reputation) > 0,
        },
        "sample_limit": sample_limit,
    }


def _sample_nodes(nodes: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    return sorted(
        nodes,
        key=lambda item: (
            0 if item.get("liveness_status") == "live" else 1,
            str(item.get("node_id", "")),
        ),
    )[:sample_limit]


def _tail_sample(items: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    if sample_limit <= 0:
        return []
    return items[-sample_limit:]


def _node_snapshot_digest(node: dict[str, Any]) -> dict[str, Any]:
    model_runtimes = node.get("model_runtimes") if isinstance(node.get("model_runtimes"), dict) else {}
    ollama = model_runtimes.get("ollama") if isinstance(model_runtimes.get("ollama"), dict) else {}
    reputation = node.get("reputation") if isinstance(node.get("reputation"), dict) else {}
    return {
        "node_id": node.get("node_id"),
        "node_role": node.get("node_role"),
        "provider_id": node.get("provider_id"),
        "subscriber_id": node.get("subscriber_id"),
        "capability_tier": node.get("capability_tier"),
        "liveness_status": node.get("liveness_status"),
        "last_seen_seconds_ago": node.get("last_seen_seconds_ago"),
        "credits": node.get("credits"),
        "supported_job_types": node.get("supported_job_types", []),
        "ollama_available": ollama.get("available"),
        "ollama_models": node.get("ollama_models", []),
        "reputation_status": reputation.get("status"),
        "software": software_metadata_public_view(node.get("software")),
    }


def _job_snapshot_digest(job: dict[str, Any]) -> dict[str, Any]:
    routing = job.get("routing") if isinstance(job.get("routing"), dict) else {}
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "model_id": job.get("model_id"),
        "required_results": job.get("required_results"),
        "max_results": job.get("max_results"),
        "result_count": job.get("result_count"),
        "lease_count": job.get("lease_count"),
        "leased_to": job.get("leased_to", []),
        "verification_strategy": job.get("verification_strategy"),
        "routing": {
            "eligible_node_count": routing.get("eligible_node_count"),
            "live_eligible_node_count": routing.get("live_eligible_node_count"),
            "policy": routing.get("policy"),
            "required_ollama_model": routing.get("required_ollama_model"),
        },
    }


def _result_snapshot_digest(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": result.get("job_id"),
        "job_type": result.get("job_type"),
        "node_id": result.get("node_id"),
        "created_at": result.get("created_at"),
        "runtime_seconds": result.get("runtime_seconds"),
    }


def _software_nodes_from_snapshot(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    nodes = snapshot.get("nodes", []) if isinstance(snapshot, dict) else []
    result = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        software = software_metadata_public_view(node.get("software"))
        result.append(
            {
                "node_id": node.get("node_id"),
                "liveness_status": node.get("liveness_status"),
                "software": software,
            }
        )
    return result


def _software_visibility_summary(
    *,
    repo: Path,
    expected_public_revision: str | None,
    primary: dict[str, Any],
    backup: dict[str, Any],
) -> dict[str, Any]:
    local = software_metadata_public_view(collect_software_metadata(repo))
    expected = (expected_public_revision or local.get("source_revision") or "").strip() or None
    lanes = {
        "primary": _lane_software_sync(primary, expected_revision=expected),
        "backup": _lane_software_sync(backup, expected_revision=expected),
    }
    live_node_count = sum(int(lane.get("live_node_count", 0)) for lane in lanes.values())
    synced = sum(int(lane.get("synced_live_nodes", 0)) for lane in lanes.values())
    behind = sum(int(lane.get("behind_live_nodes", 0)) for lane in lanes.values())
    unknown = sum(int(lane.get("unknown_live_nodes", 0)) for lane in lanes.values())
    dirty = sum(int(lane.get("dirty_live_nodes", 0)) for lane in lanes.values())
    local_dirty = local.get("source_dirty") is True
    if not expected:
        status = "unknown"
    elif behind or unknown or dirty or local_dirty:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "expected_public_revision": expected,
        "local": local,
        "lanes": lanes,
        "live_node_count": live_node_count,
        "synced_live_nodes": synced,
        "behind_live_nodes": behind,
        "unknown_live_nodes": unknown,
        "dirty_live_nodes": dirty,
        "has_local_dirty_checkout": local_dirty,
        "has_unsynced_live_nodes": behind > 0,
        "has_unknown_live_nodes": unknown > 0,
        "has_dirty_live_nodes": dirty > 0,
        "all_live_nodes_synced": live_node_count > 0 and synced == live_node_count and behind == 0 and unknown == 0 and dirty == 0,
    }


def _lane_software_sync(lane: dict[str, Any], *, expected_revision: str | None) -> dict[str, Any]:
    nodes = lane.get("software_nodes") if isinstance(lane.get("software_nodes"), list) else []
    live_nodes = [
        _node_software_sync(node, expected_revision=expected_revision)
        for node in nodes
        if isinstance(node, dict) and node.get("liveness_status") == "live"
    ]
    synced = sum(1 for node in live_nodes if node.get("revision_status") == "synced")
    behind = sum(1 for node in live_nodes if node.get("revision_status") == "behind")
    unknown = sum(1 for node in live_nodes if node.get("revision_status") == "unknown")
    dirty = sum(1 for node in live_nodes if node.get("source_dirty") is True)
    if not expected_revision:
        status = "unknown"
    elif behind or unknown or dirty:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "expected_public_revision": expected_revision,
        "live_node_count": len(live_nodes),
        "synced_live_nodes": synced,
        "behind_live_nodes": behind,
        "unknown_live_nodes": unknown,
        "dirty_live_nodes": dirty,
        "nodes": live_nodes,
    }


def _node_software_sync(node: dict[str, Any], *, expected_revision: str | None) -> dict[str, Any]:
    software = software_metadata_public_view(node.get("software"))
    revision = software.get("source_revision")
    dirty = bool(software.get("source_dirty")) if software.get("source_dirty") is not None else None
    if not revision:
        revision_status = "unknown"
    elif expected_revision and revision != expected_revision:
        revision_status = "behind"
    elif dirty is True:
        revision_status = "dirty"
    else:
        revision_status = "synced"
    return {
        "node_id": node.get("node_id"),
        "revision_status": revision_status,
        "source_revision": revision,
        "source_revision_short": _short_revision(revision),
        "source_branch": software.get("source_branch"),
        "source_dirty": dirty,
        "chatp2p_version": software.get("chatp2p_version"),
        "collected_at": software.get("collected_at"),
    }


def _expected_worker_summary(snapshot: dict[str, Any] | None, expected_worker_id: str | None) -> dict[str, Any] | None:
    if not expected_worker_id:
        return None
    node = None
    if isinstance(snapshot, dict):
        node = next(
            (
                item
                for item in snapshot.get("nodes", [])
                if isinstance(item, dict) and item.get("node_id") == expected_worker_id
            ),
            None,
        )
    return {
        "node_id": expected_worker_id,
        "present": node is not None,
        "live": bool(node and node.get("liveness_status") == "live"),
        "liveness_status": node.get("liveness_status") if node else None,
        "credits": node.get("credits") if node else None,
        "last_seen_seconds_ago": node.get("last_seen_seconds_ago") if node else None,
    }


def _lane_criteria(
    snapshot_summary: dict[str, Any],
    expected_worker: dict[str, Any] | None,
    expected_worker_id: str | None,
) -> dict[str, dict[str, Any]]:
    live_nodes = _int_or_zero(snapshot_summary.get("live_nodes"))
    disputed_jobs = _int_or_zero(snapshot_summary.get("disputed_jobs"))
    criteria = {
        "coordinator_has_live_worker": {
            "actual": live_nodes,
            "required": 1,
            "passed": live_nodes >= 1,
        },
        "no_disputed_jobs": {
            "actual": disputed_jobs,
            "required": 0,
            "passed": disputed_jobs == 0,
        },
    }
    if expected_worker_id:
        criteria["expected_worker_live"] = {
            "actual": bool(expected_worker and expected_worker.get("live")),
            "required": True,
            "passed": bool(expected_worker and expected_worker.get("live")),
        }
    return criteria


def _local_status(home: Path) -> dict[str, Any]:
    resolved = home.expanduser().resolve()
    return {
        "home": str(resolved),
        "managed_processes": managed_processes_status(home=resolved),
    }


def _reliability_summary(
    reliability_dir: Path | None,
    *,
    now: datetime,
    freshness_seconds: float,
) -> dict[str, Any]:
    if reliability_dir is None:
        return {"configured": False, "exists": False, "status": "not_configured"}
    path = reliability_dir.expanduser().resolve() / "reliability-summary.json"
    if not path.exists():
        return {
            "configured": True,
            "exists": False,
            "status": "missing",
            "path": str(path),
        }
    try:
        report = read_json_file(path, description="reliability summary")
    except (OSError, ValueError) as exc:
        return {
            "configured": True,
            "exists": True,
            "status": "fail",
            "path": str(path),
            "error": _error_message(exc),
        }
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    generated_at = report.get("generated_at") if isinstance(report, dict) else None
    age_seconds = _age_seconds(generated_at, now=now)
    return {
        "configured": True,
        "exists": True,
        "path": str(path),
        "schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "status": report.get("status"),
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "can_continue_without_partner": bool(summary.get("can_continue_without_partner")),
        "recommended_mode": summary.get("recommended_mode"),
        "primary_lane_ready": bool(summary.get("primary_lane_ready")),
        "backup_lane_ready": bool(summary.get("backup_lane_ready")),
        "disputed_jobs": summary.get("disputed_jobs"),
        "token_redaction_ok": ((report.get("criteria") or {}).get("token_redaction") or {}).get("passed"),
    }


def _partner_autopilot_summaries(
    *,
    home: Path,
    configured_paths: tuple[Path, ...],
    now: datetime,
    freshness_seconds: float,
) -> dict[str, Any]:
    paths: list[Path] = []
    runtime_dir = home.expanduser().resolve().parent
    if runtime_dir.exists():
        paths.extend(sorted(runtime_dir.glob("*autopilot-report.json")))
    paths.extend(configured_paths)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in seen:
            deduped.append(resolved)
            seen.add(resolved)

    reports = [_partner_autopilot_summary(path, now=now, freshness_seconds=freshness_seconds) for path in deduped]
    return {
        "configured": bool(deduped),
        "reports": reports,
        "ok": all(report.get("ok", True) for report in reports),
        "fresh": all(report.get("fresh", True) for report in reports),
    }


def _partner_autopilot_summary(path: Path, *, now: datetime, freshness_seconds: float) -> dict[str, Any]:
    base = {
        "name": path.name,
        "exists": path.exists(),
    }
    if not path.exists():
        return {**base, "ok": False, "status": "missing", "fresh": False}
    try:
        report = read_json_file(path, description="partner autopilot report")
    except (OSError, ValueError) as exc:
        return {**base, "ok": False, "status": "fail", "fresh": False, "error": _error_message(exc)}
    finished_at = report.get("finished_at") or report.get("generated_at")
    age_seconds = _age_seconds(finished_at, now=now)
    steps = [
        {
            "name": step.get("name"),
            "ok": step.get("ok"),
            "status": step.get("status"),
        }
        for step in report.get("steps", [])
        if isinstance(step, dict)
    ]
    return {
        **base,
        "ok": bool(report.get("ok")),
        "status": report.get("status"),
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "generated_at": report.get("generated_at"),
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "steps": steps,
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
    }


def _daily_check_automation_summary(
    *,
    daily_check_dir: Path | None,
    default_root: Path,
    task_name: str | None,
    query_task: bool,
    now: datetime,
    freshness_seconds: float,
) -> dict[str, Any]:
    resolved_dir = daily_check_dir.expanduser().resolve() if daily_check_dir is not None else default_root / "daily-check"
    report_summary = _daily_check_report_summary(
        resolved_dir / "daily-check.json",
        now=now,
        freshness_seconds=freshness_seconds,
    )
    task_summary = _query_daily_check_task(task_name, enabled=query_task)
    status = _daily_check_automation_status(report_summary, task_summary)
    return {
        "configured": True,
        "status": status,
        "ok": status == "pass",
        "daily_check_dir": str(resolved_dir),
        "report": report_summary,
        "task": task_summary,
    }


def _daily_check_report_summary(path: Path, *, now: datetime, freshness_seconds: float) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "path": str(resolved),
            "exists": False,
            "status": "missing",
            "ok": False,
            "fresh": False,
        }
    try:
        data = read_json_file(resolved, description="daily check report")
    except (OSError, ValueError) as exc:
        return {
            "path": str(resolved),
            "exists": True,
            "status": "fail",
            "ok": False,
            "fresh": False,
            "error": _error_message(exc),
        }
    generated_at = data.get("generated_at") if isinstance(data, dict) else None
    age_seconds = _age_seconds(generated_at, now=now)
    summary = data.get("summary") if isinstance(data, dict) else {}
    action_queue = data.get("action_queue") if isinstance(data, dict) else {}
    return {
        "path": str(resolved),
        "exists": True,
        "schema": data.get("schema") if isinstance(data, dict) else None,
        "ok": bool(data.get("ok")) if isinstance(data, dict) else False,
        "status": data.get("status", "unknown") if isinstance(data, dict) else "unknown",
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "can_continue_without_partner": (summary or {}).get("can_continue_without_partner"),
        "recommended_next_action": (summary or {}).get("recommended_next_action"),
        "top_queued_action": ((action_queue or {}).get("next_action") or {}).get("action_id"),
    }


def _query_daily_check_task(task_name: str | None, *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {
            "configured": bool(task_name),
            "task_name": task_name,
            "queried": False,
            "status": "skipped",
            "ok": None,
        }
    if not task_name:
        return {
            "configured": False,
            "task_name": None,
            "queried": False,
            "status": "not_configured",
            "ok": None,
        }
    if os.name != "nt":
        return {
            "configured": True,
            "task_name": task_name,
            "queried": False,
            "status": "unsupported",
            "ok": None,
            "message": "Windows Scheduled Tasks are only available on Windows.",
        }
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except OSError as exc:
        return {
            "configured": True,
            "task_name": task_name,
            "queried": True,
            "status": "fail",
            "ok": False,
            "error": _error_message(exc),
        }
    fields = _parse_schtasks_list(completed.stdout)
    if completed.returncode != 0:
        return {
            "configured": True,
            "task_name": task_name,
            "queried": True,
            "status": "missing",
            "ok": False,
            "returncode": completed.returncode,
            "stderr": completed.stderr.strip(),
        }
    last_result = fields.get("Last Result") or fields.get("Last Run Result")
    return {
        "configured": True,
        "task_name": task_name,
        "queried": True,
        "status": "pass",
        "ok": True,
        "returncode": completed.returncode,
        "task_status": fields.get("Status"),
        "next_run_time": fields.get("Next Run Time"),
        "last_run_time": fields.get("Last Run Time"),
        "last_result": last_result,
    }


def _parse_schtasks_list(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key:
            fields[key] = value.strip()
    return fields


def _daily_check_automation_status(report: dict[str, Any], task: dict[str, Any]) -> str:
    if report.get("exists") and report.get("status") == "fail":
        return "fail"
    if not report.get("exists"):
        return "missing"
    if not report.get("fresh"):
        return "stale"
    if task.get("ok") is False:
        return "warn"
    if report.get("ok"):
        return "pass"
    return "warn"


def _model_release_status_summary(
    path: Path | None,
    *,
    now: datetime,
    freshness_seconds: float,
) -> dict[str, Any]:
    if path is None:
        return {
            "configured": False,
            "status": "not_configured",
            "ok": None,
            "path": None,
            "fresh": None,
            "pipeline_stage": None,
            "recommended_next_action": None,
        }
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "configured": True,
            "status": "missing",
            "ok": False,
            "path": str(resolved),
            "exists": False,
            "fresh": False,
            "pipeline_stage": None,
            "recommended_next_action": None,
        }
    try:
        data = read_json_file(resolved, description="model release status report")
    except (OSError, ValueError) as exc:
        return {
            "configured": True,
            "status": "fail",
            "ok": False,
            "path": str(resolved),
            "exists": True,
            "fresh": False,
            "error": _error_message(exc),
            "pipeline_stage": None,
            "recommended_next_action": None,
        }
    if not isinstance(data, dict):
        return {
            "configured": True,
            "status": "fail",
            "ok": False,
            "path": str(resolved),
            "exists": True,
            "fresh": False,
            "error": "model release status report is not a JSON object",
            "pipeline_stage": None,
            "recommended_next_action": None,
        }
    generated_at = data.get("generated_at")
    age_seconds = _age_seconds(generated_at, now=now)
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return {
        "configured": True,
        "status": data.get("status", "unknown"),
        "ok": data.get("ok") if isinstance(data.get("ok"), bool) else None,
        "path": str(resolved),
        "exists": True,
        "schema": data.get("schema"),
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "model_id": summary.get("model_id"),
        "pipeline_stage": summary.get("pipeline_stage"),
        "release_ready": summary.get("release_ready"),
        "blocked_gate_ids": summary.get("blocked_gate_ids") if isinstance(summary.get("blocked_gate_ids"), list) else [],
        "next_action_id": summary.get("next_action_id"),
        "recommended_next_action": summary.get("recommended_next_action"),
        "write_flag_required_after_review": summary.get("write_flag_required_after_review"),
        "ready_for_promotion_review": summary.get("ready_for_promotion_review"),
    }


def _model_route_plan_summary(
    path: Path | None,
    *,
    now: datetime,
    freshness_seconds: float,
) -> dict[str, Any]:
    if path is None:
        return {
            "configured": False,
            "status": "not_configured",
            "ok": None,
            "path": None,
            "fresh": None,
            "route_ready": None,
            "recommended_next_action": None,
        }
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "configured": True,
            "status": "missing",
            "ok": False,
            "path": str(resolved),
            "exists": False,
            "fresh": False,
            "route_ready": None,
            "recommended_next_action": None,
        }
    try:
        data = read_json_file(resolved, description="model route plan report")
    except (OSError, ValueError) as exc:
        return {
            "configured": True,
            "status": "fail",
            "ok": False,
            "path": str(resolved),
            "exists": True,
            "fresh": False,
            "error": _error_message(exc),
            "route_ready": None,
            "recommended_next_action": None,
        }
    if not isinstance(data, dict):
        return {
            "configured": True,
            "status": "fail",
            "ok": False,
            "path": str(resolved),
            "exists": True,
            "fresh": False,
            "error": "model route plan report is not a JSON object",
            "route_ready": None,
            "recommended_next_action": None,
        }
    generated_at = data.get("generated_at")
    age_seconds = _age_seconds(generated_at, now=now)
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return {
        "configured": True,
        "status": data.get("status", "unknown"),
        "ok": data.get("ok") if isinstance(data.get("ok"), bool) else None,
        "path": str(resolved),
        "exists": True,
        "schema": data.get("schema"),
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "selected_model_id": summary.get("selected_model_id"),
        "recommended_chat_model": summary.get("recommended_chat_model"),
        "route_ready": summary.get("route_ready"),
        "network_checked": summary.get("network_checked"),
        "coordinator_reachable": summary.get("coordinator_reachable"),
        "live_model_capable_worker_count": summary.get("live_model_capable_worker_count"),
        "approved_model_count": summary.get("approved_model_count"),
        "routeable_model_count": summary.get("routeable_model_count"),
        "recommended_next_action": summary.get("recommended_next_action"),
    }


def _action_run_report_summary(path: Path, *, now: datetime, freshness_seconds: float) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "path": str(resolved),
            "exists": False,
            "status": "missing",
            "ok": None,
            "fresh": False,
        }
    try:
        data = read_json_file(resolved, description="operator action run report")
    except (OSError, ValueError) as exc:
        return {
            "path": str(resolved),
            "exists": True,
            "status": "fail",
            "ok": False,
            "fresh": False,
            "error": _error_message(exc),
        }
    if not isinstance(data, dict):
        return {
            "path": str(resolved),
            "exists": True,
            "status": "fail",
            "ok": False,
            "fresh": False,
            "error": "operator action run report is not a JSON object",
        }
    generated_at = data.get("generated_at")
    age_seconds = _age_seconds(generated_at, now=now)
    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    command = data.get("command") if isinstance(data.get("command"), dict) else {}
    execution = data.get("execution") if isinstance(data.get("execution"), dict) else {}
    return {
        "path": str(resolved),
        "exists": True,
        "schema": data.get("schema"),
        "ok": data.get("ok"),
        "status": data.get("status", "unknown"),
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "action_id": action.get("action_id"),
        "command_label": command.get("label"),
        "dry_run": execution.get("dry_run"),
        "attempted": execution.get("attempted"),
        "returncode": execution.get("returncode"),
    }


def _next_action_run_summary(action_queue: dict[str, Any], *, queue_path: Path) -> dict[str, Any]:
    action = action_queue.get("next_action") if isinstance(action_queue.get("next_action"), dict) else {}
    action_id = action.get("action_id")
    if not action_id:
        return {
            "status": "not_available",
            "action_id": None,
            "queue_path": str(queue_path.expanduser().resolve()),
            "reason": "action queue has no next action",
        }
    if action.get("partner_required"):
        return {
            "status": "partner_required",
            "action_id": action_id,
            "partner_required": True,
            "queue_path": str(queue_path.expanduser().resolve()),
            "reason": "next action requires partner involvement",
        }
    if not action.get("suggested_commands"):
        return {
            "status": "not_available",
            "action_id": action_id,
            "partner_required": False,
            "queue_path": str(queue_path.expanduser().resolve()),
            "reason": "next action has no suggested local command",
        }
    return {
        "status": "available",
        "action_id": action_id,
        "partner_required": False,
        "queue_path": str(queue_path.expanduser().resolve()),
        "dry_run_command": _operator_run_action_command(queue_path, str(action_id), execute=False),
        "execute_command": _operator_run_action_command(queue_path, str(action_id), execute=True),
    }


def _operator_run_action_command(queue_path: Path, action_id: str, *, execute: bool) -> str:
    mode_flag = "--execute" if execute else "--dry-run"
    return "\n".join(
        [
            "python -m chatp2p.cli operator run-action `",
            f"  --queue {_ps_arg(queue_path.expanduser().resolve())} `",
            f"  --action {_ps_arg(action_id)} `",
            f"  {mode_flag}",
        ]
    )


def _operator_self_heal_command(report: dict[str, Any]) -> str:
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    json_path = artifacts.get("json") or "operator-console.json"
    daily_check = report.get("daily_check_automation") if isinstance(report.get("daily_check_automation"), dict) else {}
    daily_report = daily_check.get("report") if isinstance(daily_check.get("report"), dict) else {}
    daily_path = daily_report.get("path")
    if not daily_path:
        config = report.get("config") if isinstance(report.get("config"), dict) else {}
        daily_dir = config.get("daily_check_dir") or str(Path(str(config.get("home", "."))).parent / "daily-check")
        daily_path = str(Path(str(daily_dir)) / "daily-check.json")
    action_queue_path = artifacts.get("action_queue_json") or str(Path(str(json_path)).parent / "action-queue.json")
    out_dir = Path(str(artifacts.get("self_heal_report") or json_path)).parent
    return "\n".join(
        [
            "python -m chatp2p.cli operator self-heal `",
            f"  --console-report {_ps_arg(json_path)} `",
            f"  --daily-report {_ps_arg(daily_path)} `",
            f"  --action-queue {_ps_arg(action_queue_path)} `",
            f"  --out {_ps_arg(out_dir)}",
        ]
    )


def _ps_arg(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _update_console_history(history_path: Path, report: dict[str, Any], *, limit: int) -> dict[str, Any]:
    previous_entries = _read_history_entries(history_path)
    previous_entry = previous_entries[0] if previous_entries else None
    current_entry = _history_entry(report)
    entries = [current_entry, *previous_entries]
    entries = entries[:limit]
    history_document = {
        "schema": OPERATOR_CONSOLE_HISTORY_SCHEMA,
        "updated_at": report.get("generated_at"),
        "entries": entries,
    }
    history_path.write_text(json.dumps(history_document, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "schema": OPERATOR_CONSOLE_HISTORY_SCHEMA,
        "path": str(history_path),
        "entries_kept": len(entries),
        "current_entry": current_entry,
        "previous_entry": previous_entry,
        "changes": _history_changes(previous_entry, current_entry),
    }


def _read_history_entries(history_path: Path) -> list[dict[str, Any]]:
    if not history_path.exists():
        return []
    try:
        data = read_json_file(history_path, description="operator console history")
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _history_entry(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") or {}
    lanes = report.get("lanes") or {}
    privacy = report.get("privacy_scan") or {}
    stale_reports = report.get("stale_reports") or {}
    daily_check = report.get("daily_check_automation") or {}
    action_runner = report.get("action_runner") or {}
    last_action_run = action_runner.get("last_run") or {}
    model_release = report.get("model_release") or {}
    model_route_plan = report.get("model_route_plan") or {}
    return {
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "can_continue_without_partner": summary.get("can_continue_without_partner"),
        "recommended_next_action": summary.get("recommended_next_action"),
        "primary_ready": (lanes.get("primary") or {}).get("ready"),
        "backup_ready": (lanes.get("backup") or {}).get("ready"),
        "privacy_status": privacy.get("status"),
        "privacy_findings": privacy.get("finding_count"),
        "daily_check_status": daily_check.get("status"),
        "daily_check_report_status": (daily_check.get("report") or {}).get("status"),
        "model_release_status": model_release.get("status"),
        "model_release_pipeline_stage": model_release.get("pipeline_stage"),
        "model_release_next_action": model_release.get("recommended_next_action"),
        "model_route_plan_status": model_route_plan.get("status"),
        "model_route_plan_route_ready": model_route_plan.get("route_ready"),
        "model_route_plan_next_action": model_route_plan.get("recommended_next_action"),
        "action_run_status": last_action_run.get("status"),
        "action_run_action_id": last_action_run.get("action_id"),
        "stale_report_candidates": stale_reports.get("candidate_count"),
    }


def _history_changes(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    if previous is None:
        return ["first_console_run"]
    labels = {
        "status": "status",
        "can_continue_without_partner": "can_continue_without_partner",
        "recommended_next_action": "recommended_next_action",
        "primary_ready": "primary_ready",
        "backup_ready": "backup_ready",
        "privacy_status": "privacy_status",
        "privacy_findings": "privacy_findings",
        "daily_check_status": "daily_check_status",
        "daily_check_report_status": "daily_check_report_status",
        "model_release_status": "model_release_status",
        "model_release_pipeline_stage": "model_release_pipeline_stage",
        "model_release_next_action": "model_release_next_action",
        "model_route_plan_status": "model_route_plan_status",
        "model_route_plan_route_ready": "model_route_plan_route_ready",
        "model_route_plan_next_action": "model_route_plan_next_action",
        "action_run_status": "action_run_status",
        "action_run_action_id": "action_run_action_id",
        "stale_report_candidates": "stale_report_candidates",
    }
    changes = []
    for key, label in labels.items():
        old = previous.get(key)
        new = current.get(key)
        if old != new:
            changes.append(f"{label}: {old} -> {new}")
    return changes


def _stale_report_inventory(
    *,
    root: Path,
    out_dir: Path,
    now: datetime,
    stale_days: float,
    max_items: int,
) -> dict[str, Any]:
    resolved_root = root.expanduser().resolve()
    cleanup_plan_path = out_dir / "operator-console-cleanup-plan.ps1"
    if not resolved_root.exists():
        return {
            "configured": True,
            "root": str(resolved_root),
            "exists": False,
            "status": "missing",
            "candidate_count": 0,
            "candidates": [],
            "cleanup_plan_path": str(cleanup_plan_path),
            "automatic_deletion": False,
        }
    stale_seconds = stale_days * 24 * 60 * 60
    candidates: list[dict[str, Any]] = []
    for path in resolved_root.rglob("*"):
        if len(candidates) >= max_items:
            break
        if not path.is_file():
            continue
        if _is_path_inside(path, out_dir):
            continue
        if any(part.startswith(".mesh") for part in path.relative_to(resolved_root).parts):
            continue
        if not _looks_like_report_artifact(path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        age_seconds = max(0.0, now.timestamp() - stat.st_mtime)
        if age_seconds < stale_seconds:
            continue
        candidates.append(
            {
                "path": str(path),
                "relative_path": str(path.relative_to(resolved_root)),
                "age_days": round(age_seconds / 86400, 2),
                "size_bytes": stat.st_size,
                "last_write_time": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "suggested_action": "review_then_archive_or_delete",
            }
        )
    return {
        "configured": True,
        "root": str(resolved_root),
        "exists": True,
        "status": "pass",
        "stale_after_days": stale_days,
        "candidate_count": len(candidates),
        "max_items": max_items,
        "truncated": len(candidates) >= max_items,
        "candidates": candidates,
        "cleanup_plan_path": str(cleanup_plan_path),
        "automatic_deletion": False,
    }


def _write_cleanup_plan(path: Path, stale_reports: dict[str, Any]) -> None:
    candidates = stale_reports.get("candidates", [])
    lines = [
        "# ChatP2P operator console cleanup plan",
        "# This file is generated for review only. It does not run automatically.",
        "# Uncomment individual commands only after checking the paths.",
        "",
    ]
    if not candidates:
        lines.append("# No stale report artifacts were found.")
    else:
        archive_root = path.parent / "archive"
        lines.append(f"# New-Item -ItemType Directory -Force -Path {json.dumps(str(archive_root))} | Out-Null")
        for candidate in candidates:
            source = str(candidate.get("path", ""))
            target = str(archive_root / Path(source).name)
            lines.append(f"# Move-Item -LiteralPath {json.dumps(source)} -Destination {json.dumps(target)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _looks_like_report_artifact(path: Path) -> bool:
    name = path.name.lower()
    if path.suffix.lower() not in _REPORT_SUFFIXES:
        return False
    return any(hint in name for hint in _REPORT_NAME_HINTS)


def _is_path_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _operator_summary(
    *,
    primary: dict[str, Any],
    backup: dict[str, Any],
    reliability: dict[str, Any],
    privacy_scan: dict[str, Any],
    partner_autopilot: dict[str, Any],
    daily_check: dict[str, Any],
    software: dict[str, Any],
    model_release: dict[str, Any],
    model_route_plan: dict[str, Any],
    skip_network_checks: bool,
) -> dict[str, Any]:
    primary_ready = bool(primary.get("ready"))
    backup_ready = bool(backup.get("ready"))
    reliability_can_continue = bool(reliability.get("can_continue_without_partner"))
    privacy_ok = bool(privacy_scan.get("ok"))
    can_continue = privacy_ok and (primary_ready or backup_ready or reliability_can_continue)

    warnings: list[str] = []
    errors: list[str] = []
    if skip_network_checks:
        warnings.append("network checks were skipped")
    if reliability.get("status") in {"not_configured", "missing"}:
        warnings.append("reliability summary is not available yet")
    elif reliability.get("exists") and not reliability.get("fresh"):
        warnings.append("reliability summary is stale")
    if partner_autopilot.get("configured") and not partner_autopilot.get("fresh"):
        warnings.append("partner autopilot report is stale or missing")
    if partner_autopilot.get("configured") and not partner_autopilot.get("ok"):
        warnings.append("partner autopilot report is failing")
    if daily_check.get("status") in {"missing", "stale"}:
        warnings.append(f"daily check automation is {daily_check.get('status')}")
    elif daily_check.get("status") in {"warn", "fail"}:
        warnings.append("daily check automation is not healthy")
    if software.get("has_unsynced_live_nodes"):
        warnings.append("one or more live nodes are advertising an older or different public revision")
    if software.get("has_unknown_live_nodes"):
        warnings.append("one or more live nodes have not advertised software revision metadata yet")
    if software.get("has_dirty_live_nodes"):
        warnings.append("one or more live nodes report a dirty source checkout")
    if software.get("has_local_dirty_checkout"):
        warnings.append("local public repo checkout has uncommitted changes")
    if model_release.get("configured") and model_release.get("status") in {"missing", "fail"}:
        warnings.append(f"model release status report is {model_release.get('status')}")
    elif model_release.get("configured") and model_release.get("fresh") is False:
        warnings.append("model release status report is stale")
    if model_route_plan.get("configured") and model_route_plan.get("status") in {"missing", "fail"}:
        warnings.append(f"model route plan report is {model_route_plan.get('status')}")
    elif model_route_plan.get("configured") and model_route_plan.get("fresh") is False:
        warnings.append("model route plan report is stale")
    elif model_route_plan.get("configured") and model_route_plan.get("route_ready") is False:
        warnings.append("model route plan is not route ready")
    if not backup.get("configured"):
        warnings.append("backup invite is not configured")
    if not privacy_ok:
        errors.append("public privacy scan has findings")
    if not can_continue and privacy_ok:
        errors.append("no healthy primary or backup lane is available")

    if errors:
        status = "fail"
    elif warnings:
        status = "warn"
    else:
        status = "pass"
    return {
        "status": status,
        "primary_ready": primary_ready,
        "backup_ready": backup_ready,
        "privacy_ok": privacy_ok,
        "model_release_status": model_release.get("status"),
        "model_release_pipeline_stage": model_release.get("pipeline_stage"),
        "model_release_next_action": model_release.get("recommended_next_action"),
        "model_route_plan_status": model_route_plan.get("status"),
        "model_route_plan_route_ready": model_route_plan.get("route_ready"),
        "model_route_plan_next_action": model_route_plan.get("recommended_next_action"),
        "reliability_can_continue_without_partner": reliability_can_continue,
        "can_continue_without_partner": can_continue,
        "recommended_next_action": _recommended_next_action(
            privacy_ok=privacy_ok,
            can_continue=can_continue,
            primary_ready=primary_ready,
            backup_ready=backup_ready,
            reliability=reliability,
            software=software,
            skip_network_checks=skip_network_checks,
        ),
        "warnings": warnings,
        "errors": errors,
    }


def _recommended_next_action(
    *,
    privacy_ok: bool,
    can_continue: bool,
    primary_ready: bool,
    backup_ready: bool,
    reliability: dict[str, Any],
    software: dict[str, Any],
    skip_network_checks: bool,
) -> str:
    if not privacy_ok:
        return "fix_public_privacy_findings"
    if skip_network_checks and not reliability.get("can_continue_without_partner"):
        return "rerun_console_with_network_checks"
    if not can_continue:
        return "restore_primary_or_backup_coordinator"
    if backup_ready and not primary_ready:
        return "continue_on_backup_lane"
    if reliability.get("status") in {"not_configured", "missing"}:
        return "run_reliability_pack_when_ready"
    if reliability.get("exists") and not reliability.get("fresh"):
        return "refresh_reliability_pack"
    if reliability.get("exists") and not reliability.get("ok"):
        return "repair_lane_then_rerun_reliability_pack"
    if software.get("has_unsynced_live_nodes"):
        return "wait_for_partner_autopull"
    if software.get("all_live_nodes_synced"):
        return "partner_synced_continue"
    return "continue_development"


def _privacy_summary(report: dict[str, Any]) -> dict[str, Any]:
    findings = report.get("findings", [])
    return {
        "schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "status": report.get("status"),
        "scanned_files": report.get("scanned_files"),
        "finding_count": len(findings) if isinstance(findings, list) else 0,
        "findings": findings,
        "duration_seconds": report.get("duration_seconds"),
    }


def _load_invite_for_redaction(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = read_json_file(path, description="alpha invite")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _redact_sensitive_report(value: Any, secrets_to_redact: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_sensitive_report(item, secrets_to_redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive_report(item, secrets_to_redact) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets_to_redact:
            redacted = redacted.replace(secret, "<redacted>")
        for pattern, replacement in _PATH_PRIVACY_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    return value


def _lane_card(label: str, lane: dict[str, Any]) -> str:
    summary = lane.get("snapshot_summary") or {}
    expected = lane.get("expected_worker") or {}
    expected_text = "not set"
    if expected:
        expected_text = f"{_yes_no(expected.get('live'))} ({expected.get('node_id')})"
    return f"""
        <div class="panel">
          <p class="label">{html.escape(label.title())} lane</p>
          <p class="value">{html.escape(str(lane.get("status", "unknown")).upper())}</p>
          <p>Ready: {html.escape(_yes_no(lane.get("ready")))}</p>
          <p>Live workers: {html.escape(str(summary.get("live_nodes", "-")))}</p>
          <p>Expected worker: {html.escape(expected_text)}</p>
        </div>
    """


def _action_queue_rows(action_queue: dict[str, Any]) -> str:
    rows = []
    for action in action_queue.get("actions") or []:
        severity = str(action.get("severity", "unknown"))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(action.get('rank', '-')))}</td>"
            f'<td><span class="severity {html.escape(severity)}">{html.escape(severity)}</span></td>'
            f"<td><code>{html.escape(str(action.get('action_id', 'unknown')))}</code></td>"
            f"<td>{html.escape(_yes_no(action.get('partner_required')))}</td>"
            f"<td>{html.escape(str(action.get('detail', '')))}</td>"
            "</tr>"
        )
    if rows:
        return "\n".join(rows)
    return '<tr><td colspan="5">No queued actions.</td></tr>'


def _software_sync_markdown_section(software: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## Software Sync",
        "",
        f"- Expected public revision: `{_short_revision(software.get('expected_public_revision'))}`",
        f"- Local repo revision: `{_short_revision(((software.get('local') or {}).get('source_revision')))}`",
        f"- Local branch: `{(software.get('local') or {}).get('source_branch') or 'unknown'}`",
        f"- Local dirty: `{(software.get('local') or {}).get('source_dirty')}`",
        f"- Live nodes: `{software.get('live_node_count', 0)}`",
        f"- Synced live nodes: `{software.get('synced_live_nodes', 0)}`",
        f"- Behind/different live nodes: `{software.get('behind_live_nodes', 0)}`",
        f"- Unknown live nodes: `{software.get('unknown_live_nodes', 0)}`",
        f"- Dirty live nodes: `{software.get('dirty_live_nodes', 0)}`",
        "",
        "| Lane | Status | Live | Synced | Behind/different | Unknown | Dirty |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label in ("primary", "backup"):
        sync = ((software.get("lanes") or {}).get(label) or {})
        lines.append(
            "| "
            + " | ".join(
                [
                    label,
                    str(sync.get("status", "unknown")),
                    str(sync.get("live_node_count", 0)),
                    str(sync.get("synced_live_nodes", 0)),
                    str(sync.get("behind_live_nodes", 0)),
                    str(sync.get("unknown_live_nodes", 0)),
                    str(sync.get("dirty_live_nodes", 0)),
                ]
            )
            + " |"
        )
    return lines


def _software_sync_table(software: dict[str, Any]) -> str:
    rows = [
        ("Status", software.get("status", "unknown")),
        ("Expected revision", _short_revision(software.get("expected_public_revision"))),
        ("Local revision", _short_revision(((software.get("local") or {}).get("source_revision")))),
        ("Local branch", (software.get("local") or {}).get("source_branch") or "unknown"),
        ("Live nodes", software.get("live_node_count", 0)),
        ("Synced live nodes", software.get("synced_live_nodes", 0)),
        ("Behind/different live nodes", software.get("behind_live_nodes", 0)),
        ("Unknown live nodes", software.get("unknown_live_nodes", 0)),
        ("Dirty live nodes", software.get("dirty_live_nodes", 0)),
    ]
    body = "\n".join(
        "<tr>"
        f"<th>{html.escape(str(label))}</th>"
        f"<td>{html.escape(str(value))}</td>"
        "</tr>"
        for label, value in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def _model_release_markdown_section(model_release: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## Model Release",
        "",
        f"- Status: `{model_release.get('status', 'not_configured')}`",
        f"- Model: `{model_release.get('model_id') or 'unknown'}`",
        f"- Pipeline stage: `{model_release.get('pipeline_stage') or 'unknown'}`",
        f"- Release ready: `{model_release.get('release_ready')}`",
        f"- Next action: `{model_release.get('recommended_next_action') or 'not_configured'}`",
        f"- Report fresh: `{model_release.get('fresh')}`",
        f"- Report path: `{model_release.get('path')}`",
    ]
    blocked = model_release.get("blocked_gate_ids") if isinstance(model_release.get("blocked_gate_ids"), list) else []
    if blocked:
        lines.append(f"- Blocked gates: `{', '.join(str(item) for item in blocked)}`")
    return lines


def _model_release_table(model_release: dict[str, Any]) -> str:
    blocked = model_release.get("blocked_gate_ids") if isinstance(model_release.get("blocked_gate_ids"), list) else []
    rows = [
        ("Status", model_release.get("status", "not_configured")),
        ("Model", model_release.get("model_id") or "unknown"),
        ("Pipeline stage", model_release.get("pipeline_stage") or "unknown"),
        ("Release ready", _yes_no(model_release.get("release_ready"))),
        ("Next action", model_release.get("recommended_next_action") or "not_configured"),
        ("Blocked gates", ", ".join(str(item) for item in blocked) or "none"),
        ("Report fresh", _yes_no(model_release.get("fresh"))),
        ("Report path", model_release.get("path") or "-"),
    ]
    body = "\n".join(
        "<tr>"
        f"<th>{html.escape(str(label))}</th>"
        f"<td>{html.escape(str(value))}</td>"
        "</tr>"
        for label, value in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def _model_route_plan_markdown_section(model_route_plan: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## Model Route Plan",
        "",
        f"- Status: `{model_route_plan.get('status', 'not_configured')}`",
        f"- Selected model: `{model_route_plan.get('selected_model_id') or 'unknown'}`",
        f"- Recommended chat model: `{model_route_plan.get('recommended_chat_model') or 'none'}`",
        f"- Route ready: `{model_route_plan.get('route_ready')}`",
        f"- Network checked: `{model_route_plan.get('network_checked')}`",
        f"- Coordinator reachable: `{model_route_plan.get('coordinator_reachable')}`",
        f"- Live capable workers: `{model_route_plan.get('live_model_capable_worker_count')}`",
        f"- Next action: `{model_route_plan.get('recommended_next_action') or 'not_configured'}`",
        f"- Report fresh: `{model_route_plan.get('fresh')}`",
        f"- Report path: `{model_route_plan.get('path')}`",
    ]
    return lines


def _model_route_plan_table(model_route_plan: dict[str, Any]) -> str:
    rows = [
        ("Status", model_route_plan.get("status", "not_configured")),
        ("Selected model", model_route_plan.get("selected_model_id") or "unknown"),
        ("Recommended chat model", model_route_plan.get("recommended_chat_model") or "none"),
        ("Route ready", _yes_no(model_route_plan.get("route_ready"))),
        ("Network checked", _yes_no(model_route_plan.get("network_checked"))),
        ("Coordinator reachable", _yes_no(model_route_plan.get("coordinator_reachable"))),
        ("Live capable workers", model_route_plan.get("live_model_capable_worker_count")),
        ("Next action", model_route_plan.get("recommended_next_action") or "not_configured"),
        ("Report fresh", _yes_no(model_route_plan.get("fresh"))),
        ("Report path", model_route_plan.get("path") or "-"),
    ]
    body = "\n".join(
        "<tr>"
        f"<th>{html.escape(str(label))}</th>"
        f"<td>{html.escape(str(value))}</td>"
        "</tr>"
        for label, value in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def _daily_check_automation_table(daily_check: dict[str, Any]) -> str:
    report = daily_check.get("report") or {}
    task = daily_check.get("task") or {}
    rows = [
        ("Overall", daily_check.get("status", "unknown")),
        ("Task query", task.get("status", "unknown")),
        ("Task state", task.get("task_status") or "-"),
        ("Next run", task.get("next_run_time") or "-"),
        ("Last run", task.get("last_run_time") or "-"),
        ("Last result", task.get("last_result") or "-"),
        ("Daily report", report.get("status", "unknown")),
        ("Report fresh", _yes_no(report.get("fresh"))),
        ("Report generated", report.get("generated_at") or "-"),
        ("Report path", report.get("path") or "-"),
    ]
    body = "\n".join(
        "<tr>"
        f"<th>{html.escape(str(label))}</th>"
        f"<td>{html.escape(str(value))}</td>"
        "</tr>"
        for label, value in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def _run_next_action_section(action_runner: dict[str, Any]) -> str:
    next_action = action_runner.get("next_action") or {}
    last_run = action_runner.get("last_run") or {}
    dry_run_command = next_action.get("dry_run_command")
    execute_command = next_action.get("execute_command")
    rows = [
        ("Next action", next_action.get("action_id") or "-"),
        ("Run status", next_action.get("status") or "unknown"),
        ("Partner required", _yes_no(next_action.get("partner_required"))),
        ("Last run status", last_run.get("status") or "missing"),
        ("Last run action", last_run.get("action_id") or "-"),
        ("Last run generated", last_run.get("generated_at") or "-"),
        ("Last run report", last_run.get("path") or "-"),
    ]
    body = "\n".join(
        "<tr>"
        f"<th>{html.escape(str(label))}</th>"
        f"<td>{html.escape(str(value))}</td>"
        "</tr>"
        for label, value in rows
    )
    if not dry_run_command or not execute_command:
        reason = next_action.get("reason") or "No local command is available for the next action."
        commands_html = f"<p>{html.escape(str(reason))}</p>"
    else:
        commands_html = (
            "<h3>Dry Run</h3>"
            f"<pre>{html.escape(str(dry_run_command))}</pre>"
            "<h3>Execute</h3>"
            f"<pre>{html.escape(str(execute_command))}</pre>"
        )
    return f"""
    <section>
      <h2>Run Next Action</h2>
      <table><tbody>{body}</tbody></table>
      {commands_html}
    </section>
    """


def _self_heal_section(self_heal: dict[str, Any], report: dict[str, Any]) -> str:
    dry_run_command = self_heal.get("top_dry_run_command") or _operator_self_heal_command(report)
    execute_command = self_heal.get("top_execute_command") or "Run operator self-heal first; V1 does not auto-execute repairs."
    rows = [
        ("Latest report status", self_heal.get("status") or "missing"),
        ("Report fresh", _yes_no(self_heal.get("fresh"))),
        ("Repairable issues", self_heal.get("repairable_issue_count") or 0),
        ("Top self-heal action", self_heal.get("top_self_heal_action") or "-"),
        ("Report path", self_heal.get("path") or "-"),
    ]
    body = "\n".join(
        "<tr>"
        f"<th>{html.escape(str(label))}</th>"
        f"<td>{html.escape(str(value))}</td>"
        "</tr>"
        for label, value in rows
    )
    return f"""
    <section>
      <h2>Self-Heal</h2>
      <table><tbody>{body}</tbody></table>
      <h3>Dry Run</h3>
      <pre>{html.escape(str(dry_run_command))}</pre>
      <h3>Execute</h3>
      <pre>{html.escape(str(execute_command))}</pre>
    </section>
    """


def _lane_brief(lane: dict[str, Any]) -> str:
    if not lane:
        return "unknown"
    return f"{lane.get('status', 'unknown')} ready={_yes_no(lane.get('ready'))}"


def _markdown_table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _age_seconds(timestamp: Any, *, now: datetime) -> float | None:
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - parsed).total_seconds()), 3)


def _int_or_zero(value: Any) -> int:
    parsed = _int_or_none(value)
    return 0 if parsed is None else parsed


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _yes_no(value: Any) -> str:
    if value is None:
        return "unknown"
    return "yes" if bool(value) else "no"


def _short_revision(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    return value.strip()[:12]


def _error_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _validate_operator_console_config(config: OperatorConsoleConfig) -> None:
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.freshness_seconds <= 0:
        raise ValueError("--freshness-seconds must be greater than 0")
    if config.history_limit < 1:
        raise ValueError("--history-limit must be at least 1")
    if config.stale_report_days <= 0:
        raise ValueError("--stale-report-days must be greater than 0")
    if config.stale_report_max_items < 0:
        raise ValueError("--stale-report-max-items cannot be negative")
    if not str(config.primary_invite_path).strip():
        raise ValueError("--primary-invite is required")

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
from .privacy import PrivacyScanConfig, run_public_privacy_scan
from .windows_task import DEFAULT_DAILY_CHECK_TASK_NAME


OPERATOR_CONSOLE_REPORT_SCHEMA = "chatp2p.operator-console-report.v1"
OPERATOR_CONSOLE_HISTORY_SCHEMA = "chatp2p.operator-console-history.v1"
DEFAULT_OPERATOR_CONSOLE_FRESHNESS_SECONDS = 3600.0
DEFAULT_OPERATOR_CONSOLE_HISTORY_LIMIT = 20
DEFAULT_STALE_REPORT_DAYS = 2.0
DEFAULT_STALE_REPORT_MAX_ITEMS = 50
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
    summary = _operator_summary(
        primary=primary,
        backup=backup,
        reliability=reliability,
        privacy_scan=privacy_scan,
        partner_autopilot=partner_autopilot,
        daily_check=daily_check,
        skip_network_checks=config.skip_network_checks,
    )

    json_path = out_dir / "operator-console.json"
    markdown_path = out_dir / "operator-console.md"
    html_path = out_dir / "operator-console.html"
    action_queue_json_path = out_dir / "action-queue.json"
    action_queue_markdown_path = out_dir / "action-queue.md"
    history_path = out_dir / "operator-console-history.json"
    cleanup_plan_path = out_dir / "operator-console-cleanup-plan.ps1"
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
        },
        "summary": summary,
        "local": local,
        "lanes": {
            "primary": primary,
            "backup": backup,
        },
        "reliability": reliability,
        "privacy_scan": _privacy_summary(privacy_scan),
        "partner_autopilot": partner_autopilot,
        "daily_check_automation": daily_check,
        "stale_reports": stale_reports,
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
            "html": str(html_path),
            "action_queue_json": str(action_queue_json_path),
            "action_queue_markdown": str(action_queue_markdown_path),
            "history": str(history_path),
            "cleanup_plan": str(cleanup_plan_path),
        },
    }
    report = _redact_sensitive_report(report, secrets_to_redact)
    history_summary = _update_console_history(history_path, report, limit=config.history_limit)
    report["history"] = _redact_sensitive_report(history_summary, secrets_to_redact)
    action_queue = build_operator_action_queue(report)
    report["action_queue"] = _redact_sensitive_report(action_queue, secrets_to_redact)
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
    lines = [
        f"ChatP2P operator console: {str(report.get('status', 'unknown')).upper()}",
        f"Can continue without partner: {_yes_no(summary.get('can_continue_without_partner'))}",
        f"Recommended next action: {summary.get('recommended_next_action', 'unknown')}",
        f"Top queued action: {top_action.get('action_id', 'none')}",
        f"Partner required: {_yes_no(top_action.get('partner_required'))}",
        f"Daily check automation: {daily_check.get('status', 'unknown')}",
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
    top_action = action_queue.get("next_action") or {}

    lines = [
        "# ChatP2P Operator Console",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Can continue without partner: **{_yes_no(summary.get('can_continue_without_partner'))}**",
        f"- Recommended next action: `{summary.get('recommended_next_action', 'unknown')}`",
        f"- Top queued action: `{top_action.get('action_id', 'none')}`",
        f"- Partner required: **{_yes_no(top_action.get('partner_required'))}**",
        f"- Daily check automation: `{daily_check.get('status', 'unknown')}`",
        f"- Generated at: `{report.get('generated_at')}`",
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
    lines.extend(
        [
            "",
        "## Lanes",
        "",
        "| Lane | Ready | Health | Live workers | Expected worker | Disputes |",
        "| --- | --- | --- | --- | --- | --- |",
        ]
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
                    expected_text,
                    str((lane.get("snapshot_summary") or {}).get("disputed_jobs", "-")),
                ]
            )
            + " |"
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
    top_action = action_queue.get("next_action") or {}
    status = str(report.get("status", "unknown"))
    status_class = "ok" if status == "pass" else ("warn" if status == "warn" else "fail")
    lane_cards = "\n".join(_lane_card(label, lanes.get(label, {})) for label in ("primary", "backup"))
    action_rows = _action_queue_rows(action_queue)
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
          <p class="label">Generated</p>
          <p class="value">{html.escape(str(report.get("generated_at", "-")))}</p>
        </div>
      </div>
    </header>
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
    lane["snapshot"] = snapshot_result
    snapshot = snapshot_result.get("payload") if snapshot_result.get("ok") else None
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
        "reliability_can_continue_without_partner": reliability_can_continue,
        "can_continue_without_partner": can_continue,
        "recommended_next_action": _recommended_next_action(
            privacy_ok=privacy_ok,
            can_continue=can_continue,
            primary_ready=primary_ready,
            backup_ready=backup_ready,
            reliability=reliability,
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

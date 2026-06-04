"""Read-only autopull health report for ChatP2P operators."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file


OPERATOR_AUTOPULL_HEALTH_REPORT_SCHEMA = "chatp2p.operator-autopull-health-report.v1"
DEFAULT_AUTOPULL_HEALTH_FRESHNESS_SECONDS = 3600.0

_TOKEN_PATTERNS = (
    re.compile(r"(admission_token\s*[=:]\s*)['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"tskey-[A-Za-z0-9_-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)
_PRIVATE_PATH_PATTERNS = (
    (re.compile(r"ChatP2P-[^\\/\\s]*(?:private|partner)[^\\/\\s]*", re.IGNORECASE), "ChatP2P-<redacted>"),
    (re.compile(r"backup-alpha-invite-[A-Za-z0-9_-]+", re.IGNORECASE), "backup-alpha-invite-<partner>"),
)


@dataclass(frozen=True)
class OperatorAutopullHealthConfig:
    repo: Path
    out_dir: Path
    console_report_path: Path | None = None
    sync_status_report_path: Path | None = None
    partner_report_paths: tuple[Path, ...] = ()
    freshness_seconds: float = DEFAULT_AUTOPULL_HEALTH_FRESHNESS_SECONDS


def run_operator_autopull_health(config: OperatorAutopullHealthConfig) -> dict[str, Any]:
    """Write a static report showing whether partner autopull is healthy."""

    _validate_autopull_health_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc)
    repo = config.repo.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    console = _optional_report_summary(
        config.console_report_path,
        expected_schema="chatp2p.operator-console-report.v1",
        now=now,
        freshness_seconds=config.freshness_seconds,
        kind="operator_console",
    )
    sync_status = _optional_report_summary(
        config.sync_status_report_path,
        expected_schema="chatp2p.operator-sync-status-report.v1",
        now=now,
        freshness_seconds=config.freshness_seconds,
        kind="sync_status",
    )
    partner_reports = [
        _partner_report_summary(path, now=now, freshness_seconds=config.freshness_seconds)
        for path in config.partner_report_paths
    ]
    nodes = _node_summaries(sync_status=sync_status, console=console)
    summary = _autopull_summary(
        console=console,
        sync_status=sync_status,
        partner_reports=partner_reports,
        nodes=nodes,
    )

    json_path = out_dir / "autopull-health.json"
    markdown_path = out_dir / "autopull-health.md"
    report = {
        "schema": OPERATOR_AUTOPULL_HEALTH_REPORT_SCHEMA,
        "ok": summary["status"] != "fail",
        "status": summary["status"],
        "generated_at": now.isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "repo": str(repo),
            "out_dir": str(out_dir),
            "console_report_path": str(config.console_report_path) if config.console_report_path else None,
            "sync_status_report_path": str(config.sync_status_report_path) if config.sync_status_report_path else None,
            "partner_report_paths": [str(path) for path in config.partner_report_paths],
            "freshness_seconds": config.freshness_seconds,
            "read_only": True,
        },
        "summary": summary,
        "operator_console": console,
        "sync_status": sync_status,
        "partner_autopilot": {
            "configured": bool(partner_reports),
            "report_count": len(partner_reports),
            "reports": partner_reports,
        },
        "nodes": nodes,
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
        },
    }
    report = _redact_sensitive(report)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_operator_autopull_health_markdown(report), encoding="utf-8")
    return report


def format_operator_autopull_health_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    return "\n".join(
        [
            f"ChatP2P autopull health: {str(report.get('status', 'unknown')).upper()}",
            f"Autopull state: {summary.get('autopull_state', 'unknown')}",
            f"Recommended next action: {summary.get('recommended_next_action', 'unknown')}",
            f"Partner required: {_yes_no(summary.get('partner_required'))}",
            f"Sync state: {summary.get('sync_state', 'unknown')}",
            f"Live nodes: {summary.get('live_node_count', 0)}",
            f"Partner reports healthy/fresh: {_yes_no(summary.get('partner_reports_healthy'))}/{_yes_no(summary.get('partner_reports_fresh'))}",
            f"Report: {(report.get('artifacts') or {}).get('json')}",
        ]
    )


def format_operator_autopull_health_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    partner = report.get("partner_autopilot") or {}
    lines = [
        "# ChatP2P Autopull Health",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Autopull state: `{summary.get('autopull_state', 'unknown')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action', 'unknown')}`",
        f"- Can continue without partner: **{_yes_no(summary.get('can_continue_without_partner'))}**",
        f"- Partner required: **{_yes_no(summary.get('partner_required'))}**",
        f"- Sync state: `{summary.get('sync_state', 'unknown')}`",
        f"- Live nodes: `{summary.get('live_node_count', 0)}`",
        f"- Partner reports configured: **{_yes_no(partner.get('configured'))}**",
        f"- Generated at: `{report.get('generated_at')}`",
        "",
        "## Partner Reports",
        "",
        "| Report | Status | Fresh | Age Seconds | Errors |",
        "| --- | --- | --- | --- | --- |",
    ]
    reports = partner.get("reports") or []
    if not reports:
        lines.append("| - | not_configured | - | - | - |")
    for item in reports:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("name") or "unknown"),
                    str(item.get("status") or "unknown"),
                    _yes_no(item.get("fresh")),
                    str(item.get("age_seconds") if item.get("age_seconds") is not None else "-"),
                    str(item.get("error_count", 0)),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Live Nodes", "", "| Lane | Node | Status | Revision | Branch | Dirty |", "| --- | --- | --- | --- | --- | --- |"])
    nodes = report.get("nodes") or []
    if not nodes:
        lines.append("| - | - | none | - | - | - |")
    for node in nodes:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(node.get("lane", "unknown")),
                    f"`{node.get('node_id') or 'unknown'}`",
                    str(node.get("revision_status", "unknown")),
                    f"`{node.get('source_revision_short') or 'unknown'}`",
                    str(node.get("source_branch") or "unknown"),
                    str(node.get("source_dirty")),
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


def _validate_autopull_health_config(config: OperatorAutopullHealthConfig) -> None:
    if config.freshness_seconds <= 0:
        raise ValueError("--freshness-seconds must be greater than zero")


def _optional_report_summary(
    path: Path | None,
    *,
    expected_schema: str,
    now: datetime,
    freshness_seconds: float,
    kind: str,
) -> dict[str, Any]:
    if path is None:
        return {
            "configured": False,
            "exists": False,
            "kind": kind,
            "status": "not_configured",
            "summary": {},
        }
    resolved = path.expanduser().resolve()
    try:
        report = read_json_file(resolved, description=f"{kind} report")
    except (OSError, ValueError) as exc:
        return {
            "configured": True,
            "exists": False,
            "kind": kind,
            "path": str(resolved),
            "status": "missing_or_invalid",
            "fresh": False,
            "summary": {},
            "error": str(exc),
        }
    if not isinstance(report, dict):
        return {
            "configured": True,
            "exists": True,
            "kind": kind,
            "path": str(resolved),
            "status": "invalid",
            "fresh": False,
            "summary": {},
            "error": "report must be a JSON object",
        }
    generated_at = report.get("generated_at")
    age_seconds = _age_seconds(generated_at, now=now)
    status = str(report.get("status") or "unknown")
    if report.get("schema") != expected_schema:
        status = "schema_mismatch"
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return {
        "configured": True,
        "exists": True,
        "kind": kind,
        "path": str(resolved),
        "schema": report.get("schema"),
        "ok": bool(report.get("ok", status != "fail")),
        "status": status,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "summary": _safe_summary(summary),
        "software": _safe_software(report.get("software") if isinstance(report.get("software"), dict) else {}),
        "nodes": _safe_nodes(report.get("nodes") if isinstance(report.get("nodes"), list) else []),
    }


def _partner_report_summary(path: Path, *, now: datetime, freshness_seconds: float) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    base = {
        "name": resolved.name,
        "path": str(resolved),
        "exists": resolved.exists(),
    }
    if not resolved.exists():
        return {**base, "ok": False, "status": "missing", "fresh": False, "error_count": 1}
    try:
        report = read_json_file(resolved, description="partner autopilot report")
    except (OSError, ValueError) as exc:
        return {**base, "ok": False, "status": "missing_or_invalid", "fresh": False, "error": str(exc), "error_count": 1}
    if not isinstance(report, dict):
        return {**base, "ok": False, "status": "invalid", "fresh": False, "error": "report must be a JSON object", "error_count": 1}
    finished_at = report.get("finished_at") or report.get("generated_at") or report.get("started_at")
    age_seconds = _age_seconds(finished_at, now=now)
    steps = [
        {
            "name": step.get("name"),
            "ok": step.get("ok"),
            "status": step.get("status"),
        }
        for step in (report.get("steps") if isinstance(report.get("steps"), list) else [])
        if isinstance(step, dict)
    ]
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    return {
        **base,
        "ok": bool(report.get("ok")),
        "status": str(report.get("status") or "unknown"),
        "schema": report.get("schema"),
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "generated_at": report.get("generated_at"),
        "age_seconds": age_seconds,
        "fresh": age_seconds is not None and age_seconds <= freshness_seconds,
        "step_count": len(steps),
        "failed_steps": [step for step in steps if step.get("ok") is False or step.get("status") == "fail"],
        "error_count": len(errors),
        "warning_count": len(warnings),
    }


def _autopull_summary(
    *,
    console: dict[str, Any],
    sync_status: dict[str, Any],
    partner_reports: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    sync_summary = sync_status.get("summary") if isinstance(sync_status.get("summary"), dict) else {}
    console_summary = console.get("summary") if isinstance(console.get("summary"), dict) else {}
    sync_state = _sync_state_from_reports(sync_status=sync_status, console=console)
    live_node_count = _int_or_zero(sync_summary.get("live_node_count") or (console.get("software") or {}).get("live_node_count") or len(nodes))
    partner_configured = bool(partner_reports)
    partner_healthy = all(item.get("ok") is True and item.get("status") != "fail" for item in partner_reports) if partner_configured else None
    partner_fresh = all(item.get("fresh") is True for item in partner_reports) if partner_configured else None

    if console.get("configured") and console.get("status") in {"missing_or_invalid", "invalid", "schema_mismatch", "fail"}:
        warnings.append("operator console report is missing, invalid, or failing")
    if sync_status.get("configured") and sync_status.get("status") in {"missing_or_invalid", "invalid", "schema_mismatch", "fail"}:
        warnings.append("sync-status report is missing, invalid, or failing")
    if partner_configured and not partner_healthy:
        warnings.append("one or more partner autopilot reports are missing or failing")
    if partner_configured and not partner_fresh:
        warnings.append("one or more partner autopilot reports are stale")

    if sync_state == "synced":
        autopull_state = "autopull_working"
    elif partner_configured and (not partner_healthy or not partner_fresh):
        autopull_state = "autopull_stale"
    elif sync_state == "waiting_for_autopull":
        autopull_state = "autopull_pending"
    elif sync_state == "unknown_old_worker":
        autopull_state = "autopull_pending" if partner_healthy or not partner_configured else "autopull_stale"
    elif sync_state == "blocked" and live_node_count == 0 and (sync_status.get("configured") or console.get("configured")):
        autopull_state = "partner_offline"
    else:
        autopull_state = "unknown"

    status = "pass" if autopull_state == "autopull_working" and not warnings else "warn"
    return {
        "status": status,
        "autopull_state": autopull_state,
        "sync_state": sync_state,
        "recommended_next_action": _recommended_next_action(autopull_state),
        "can_continue_without_partner": True,
        "partner_required": False,
        "live_node_count": live_node_count,
        "expected_public_revision": sync_summary.get("expected_public_revision") or console_summary.get("expected_public_revision"),
        "expected_public_revision_short": sync_summary.get("expected_public_revision_short") or _short_revision(sync_summary.get("expected_public_revision")),
        "synced_live_nodes": _int_or_zero(sync_summary.get("synced_live_nodes") or (console.get("software") or {}).get("synced_live_nodes")),
        "behind_live_nodes": _int_or_zero(sync_summary.get("behind_live_nodes") or (console.get("software") or {}).get("behind_live_nodes")),
        "unknown_live_nodes": _int_or_zero(sync_summary.get("unknown_live_nodes") or (console.get("software") or {}).get("unknown_live_nodes")),
        "dirty_live_nodes": _int_or_zero(sync_summary.get("dirty_live_nodes") or (console.get("software") or {}).get("dirty_live_nodes")),
        "partner_reports_configured": partner_configured,
        "partner_reports_healthy": partner_healthy,
        "partner_reports_fresh": partner_fresh,
        "warnings": warnings,
        "errors": errors,
    }


def _sync_state_from_reports(*, sync_status: dict[str, Any], console: dict[str, Any]) -> str:
    summary = sync_status.get("summary") if isinstance(sync_status.get("summary"), dict) else {}
    sync_state = summary.get("sync_state")
    if isinstance(sync_state, str) and sync_state:
        return sync_state
    software = console.get("software") if isinstance(console.get("software"), dict) else {}
    live = _int_or_zero(software.get("live_node_count"))
    dirty = _int_or_zero(software.get("dirty_live_nodes"))
    behind = _int_or_zero(software.get("behind_live_nodes"))
    unknown = _int_or_zero(software.get("unknown_live_nodes"))
    synced = _int_or_zero(software.get("synced_live_nodes"))
    if dirty:
        return "blocked"
    if behind:
        return "waiting_for_autopull"
    if unknown:
        return "unknown_old_worker"
    if live > 0 and synced == live:
        return "synced"
    if live == 0:
        return "blocked"
    return "unknown"


def _node_summaries(*, sync_status: dict[str, Any], console: dict[str, Any]) -> list[dict[str, Any]]:
    sync_nodes = _safe_nodes(sync_status.get("nodes") if isinstance(sync_status.get("nodes"), list) else [])
    if sync_nodes:
        return sync_nodes
    console_nodes = _safe_nodes(console.get("nodes") if isinstance(console.get("nodes"), list) else [])
    if console_nodes:
        return console_nodes
    software = console.get("software") if isinstance(console.get("software"), dict) else {}
    nodes: list[dict[str, Any]] = []
    lanes = software.get("lanes") if isinstance(software.get("lanes"), dict) else {}
    for label in ("primary", "backup"):
        lane = lanes.get(label) if isinstance(lanes.get(label), dict) else {}
        for node in lane.get("nodes") if isinstance(lane.get("nodes"), list) else []:
            if isinstance(node, dict):
                item = _safe_node(node)
                item["lane"] = label
                nodes.append(item)
    return nodes


def _safe_summary(summary: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "sync_state",
        "recommended_next_action",
        "can_continue_without_partner",
        "expected_public_revision",
        "expected_public_revision_short",
        "local_revision",
        "local_revision_short",
        "origin_revision",
        "origin_revision_short",
        "console_status",
        "console_generated_at",
        "console_age_seconds",
        "live_node_count",
        "synced_live_nodes",
        "behind_live_nodes",
        "unknown_live_nodes",
        "dirty_live_nodes",
        "can_confirm_partner_synced",
        "warnings",
        "errors",
    }
    return {key: summary.get(key) for key in keys if key in summary}


def _safe_software(software: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "status",
        "expected_public_revision",
        "expected_public_revision_short",
        "live_node_count",
        "synced_live_nodes",
        "behind_live_nodes",
        "unknown_live_nodes",
        "dirty_live_nodes",
    }
    result = {key: software.get(key) for key in keys if key in software}
    lanes = software.get("lanes") if isinstance(software.get("lanes"), dict) else {}
    result["lanes"] = {}
    for label in ("primary", "backup"):
        lane = lanes.get(label) if isinstance(lanes.get(label), dict) else {}
        result["lanes"][label] = {
            "status": lane.get("status", "unknown"),
            "live_node_count": _int_or_zero(lane.get("live_node_count")),
            "synced_live_nodes": _int_or_zero(lane.get("synced_live_nodes")),
            "behind_live_nodes": _int_or_zero(lane.get("behind_live_nodes")),
            "unknown_live_nodes": _int_or_zero(lane.get("unknown_live_nodes")),
            "dirty_live_nodes": _int_or_zero(lane.get("dirty_live_nodes")),
            "nodes": _safe_nodes(lane.get("nodes") if isinstance(lane.get("nodes"), list) else []),
        }
    return result


def _safe_nodes(nodes: list[Any]) -> list[dict[str, Any]]:
    return [_safe_node(node) for node in nodes if isinstance(node, dict)]


def _safe_node(node: dict[str, Any]) -> dict[str, Any]:
    revision = node.get("source_revision")
    return {
        "lane": node.get("lane"),
        "node_id": node.get("node_id"),
        "revision_status": node.get("revision_status") or "unknown",
        "source_revision": revision,
        "source_revision_short": node.get("source_revision_short") or _short_revision(revision),
        "source_branch": node.get("source_branch"),
        "source_dirty": node.get("source_dirty"),
        "chatp2p_version": node.get("chatp2p_version"),
        "collected_at": node.get("collected_at"),
    }


def _recommended_next_action(autopull_state: str) -> str:
    if autopull_state == "autopull_working":
        return "partner_synced_continue"
    if autopull_state == "autopull_pending":
        return "wait_for_partner_autopull"
    if autopull_state == "autopull_stale":
        return "refresh_operator_console_or_wait"
    if autopull_state == "partner_offline":
        return "continue_offline_or_wait_for_partner"
    return "run_operator_maintenance"


def _age_seconds(value: Any, *, now: datetime) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds()), 3)


def _short_revision(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:12]


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_sensitive(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item) for item in value)
    if isinstance(value, str):
        text = value
        for pattern in _TOKEN_PATTERNS:
            if pattern.pattern.startswith("(admission_token"):
                text = pattern.sub(r"\1'<redacted>'", text)
            else:
                text = pattern.sub("<redacted>", text)
        for pattern, replacement in _PRIVATE_PATH_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    return value

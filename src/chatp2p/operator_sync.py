"""Read-only public revision sync status for ChatP2P operators."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file
from .runtime_metadata import collect_software_metadata, software_metadata_public_view


OPERATOR_SYNC_STATUS_REPORT_SCHEMA = "chatp2p.operator-sync-status-report.v1"
DEFAULT_AUTOPULL_STALE_MINUTES = 45.0

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
class OperatorSyncStatusConfig:
    repo: Path
    console_report_path: Path
    out_dir: Path
    expected_public_revision: str | None = None
    autopull_stale_minutes: float = DEFAULT_AUTOPULL_STALE_MINUTES


def run_operator_sync_status(config: OperatorSyncStatusConfig) -> dict[str, Any]:
    """Write a static answer for whether live nodes advertise the expected public revision."""

    _validate_sync_status_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc)
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    console_input = _load_console_report(config.console_report_path)
    console_report = console_input.get("report") if isinstance(console_input.get("report"), dict) else {}
    console_software = console_report.get("software") if isinstance(console_report.get("software"), dict) else {}
    local = software_metadata_public_view(collect_software_metadata(config.repo))
    origin_revision = _local_tracking_revision(config.repo)
    expected = _select_expected_revision(
        requested=config.expected_public_revision,
        console_software=console_software,
        local=local,
    )
    lanes = _lanes_from_software(console_software)
    nodes = _flatten_nodes(lanes)
    console_generated_at = console_report.get("generated_at") if isinstance(console_report, dict) else None
    console_age_seconds = _age_seconds(console_generated_at, now=now)
    warnings: list[str] = []
    errors: list[str] = []

    if console_input.get("status") != "loaded":
        errors.append(str(console_input.get("message") or "operator console report could not be loaded"))
    if console_report and console_report.get("status") == "fail":
        errors.append("operator console report status is fail")
    if not expected:
        errors.append("expected public revision is unknown")
    if not nodes:
        errors.append("no live node revision metadata is available in the console report")
    if local.get("source_dirty") is True:
        warnings.append("local public repo checkout is dirty")

    synced = sum(1 for node in nodes if node.get("revision_status") == "synced")
    behind = sum(1 for node in nodes if node.get("revision_status") == "behind")
    unknown = sum(1 for node in nodes if node.get("revision_status") == "unknown")
    dirty = sum(1 for node in nodes if node.get("source_dirty") is True or node.get("revision_status") == "dirty")
    if dirty:
        errors.append("one or more live nodes advertise a dirty checkout")

    sync_state = _sync_state(
        expected_revision=expected,
        live_node_count=len(nodes),
        synced_live_nodes=synced,
        behind_live_nodes=behind,
        unknown_live_nodes=unknown,
        dirty_live_nodes=dirty,
        errors=errors,
    )
    if sync_state in {"waiting_for_autopull", "unknown_old_worker"} and _is_stale(console_age_seconds, config):
        warnings.append("operator console report is older than the autopull freshness window")
    if sync_state == "waiting_for_autopull":
        warnings.append("one or more live nodes advertise an older or different public revision")
    elif sync_state == "unknown_old_worker":
        warnings.append("one or more live nodes have not advertised software revision metadata yet")

    status = _report_status(sync_state, warnings=warnings)
    json_path = out_dir / "sync-status.json"
    markdown_path = out_dir / "sync-status.md"
    report = {
        "schema": OPERATOR_SYNC_STATUS_REPORT_SCHEMA,
        "ok": status != "fail",
        "status": status,
        "generated_at": now.isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "repo": str(config.repo.expanduser().resolve()),
            "console_report_path": str(config.console_report_path.expanduser().resolve()),
            "out_dir": str(out_dir),
            "expected_public_revision": config.expected_public_revision,
            "autopull_stale_minutes": config.autopull_stale_minutes,
            "read_only": True,
        },
        "summary": {
            "sync_state": sync_state,
            "expected_public_revision": expected,
            "expected_public_revision_short": _short_revision(expected),
            "local_revision": local.get("source_revision"),
            "local_revision_short": _short_revision(local.get("source_revision")),
            "local_dirty": local.get("source_dirty"),
            "origin_revision": origin_revision,
            "origin_revision_short": _short_revision(origin_revision),
            "console_status": console_report.get("status") if isinstance(console_report, dict) else None,
            "console_generated_at": console_generated_at,
            "console_age_seconds": console_age_seconds,
            "live_node_count": len(nodes),
            "synced_live_nodes": synced,
            "behind_live_nodes": behind,
            "unknown_live_nodes": unknown,
            "dirty_live_nodes": dirty,
            "can_confirm_partner_synced": sync_state == "synced",
            "can_continue_without_partner": sync_state in {"synced", "waiting_for_autopull", "unknown_old_worker"},
            "recommended_next_action": _recommended_next_action(sync_state),
            "warnings": warnings,
            "errors": errors,
        },
        "local": local,
        "lanes": lanes,
        "nodes": nodes,
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
        },
    }
    report = _redact_sensitive(report)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_operator_sync_status_markdown(report), encoding="utf-8")
    return report


def format_operator_sync_status_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    return "\n".join(
        [
            f"ChatP2P sync status: {str(report.get('status', 'unknown')).upper()}",
            f"Sync state: {summary.get('sync_state', 'unknown')}",
            f"Expected revision: {summary.get('expected_public_revision_short', 'unknown')}",
            f"Live nodes: {summary.get('live_node_count', 0)}",
            f"Synced/behind/unknown/dirty: {summary.get('synced_live_nodes', 0)}/{summary.get('behind_live_nodes', 0)}/{summary.get('unknown_live_nodes', 0)}/{summary.get('dirty_live_nodes', 0)}",
            f"Recommended next action: {summary.get('recommended_next_action', 'unknown')}",
            f"Report: {(report.get('artifacts') or {}).get('json')}",
        ]
    )


def format_operator_sync_status_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Sync Status",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Sync state: `{summary.get('sync_state', 'unknown')}`",
        f"- Expected public revision: `{summary.get('expected_public_revision_short') or 'unknown'}`",
        f"- Local revision: `{summary.get('local_revision_short') or 'unknown'}`",
        f"- Origin tracking revision: `{summary.get('origin_revision_short') or 'unknown'}`",
        f"- Local dirty: `{summary.get('local_dirty')}`",
        f"- Live nodes: `{summary.get('live_node_count', 0)}`",
        f"- Synced live nodes: `{summary.get('synced_live_nodes', 0)}`",
        f"- Behind/different live nodes: `{summary.get('behind_live_nodes', 0)}`",
        f"- Unknown live nodes: `{summary.get('unknown_live_nodes', 0)}`",
        f"- Dirty live nodes: `{summary.get('dirty_live_nodes', 0)}`",
        f"- Can confirm partner synced: **{_yes_no(summary.get('can_confirm_partner_synced'))}**",
        f"- Recommended next action: `{summary.get('recommended_next_action', 'unknown')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        "",
        "## Lanes",
        "",
        "| Lane | Status | Live | Synced | Behind | Unknown | Dirty |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label in ("primary", "backup"):
        lane = ((report.get("lanes") or {}).get(label) or {})
        lines.append(
            "| "
            + " | ".join(
                [
                    label,
                    str(lane.get("status", "unknown")),
                    str(lane.get("live_node_count", 0)),
                    str(lane.get("synced_live_nodes", 0)),
                    str(lane.get("behind_live_nodes", 0)),
                    str(lane.get("unknown_live_nodes", 0)),
                    str(lane.get("dirty_live_nodes", 0)),
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


def _validate_sync_status_config(config: OperatorSyncStatusConfig) -> None:
    if config.autopull_stale_minutes <= 0:
        raise ValueError("--autopull-stale-minutes must be greater than zero")


def _load_console_report(path: Path) -> dict[str, Any]:
    try:
        report = read_json_file(path, description="operator console report")
    except (OSError, ValueError) as exc:
        return {
            "status": "missing_or_invalid",
            "ok": False,
            "message": str(exc),
            "report": None,
        }
    if not isinstance(report, dict):
        return {
            "status": "invalid",
            "ok": False,
            "message": "operator console report must be a JSON object",
            "report": None,
        }
    return {
        "status": "loaded",
        "ok": True,
        "message": None,
        "report": report,
    }


def _select_expected_revision(
    *,
    requested: str | None,
    console_software: dict[str, Any],
    local: dict[str, Any],
) -> str | None:
    for value in (
        requested,
        console_software.get("expected_public_revision"),
        local.get("source_revision"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _lanes_from_software(software: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_lanes = software.get("lanes") if isinstance(software.get("lanes"), dict) else {}
    return {
        "primary": _lane_public_summary(raw_lanes.get("primary")),
        "backup": _lane_public_summary(raw_lanes.get("backup")),
    }


def _lane_public_summary(value: Any) -> dict[str, Any]:
    lane = value if isinstance(value, dict) else {}
    nodes = [
        _node_public_summary(node)
        for node in (lane.get("nodes") if isinstance(lane.get("nodes"), list) else [])
        if isinstance(node, dict)
    ]
    return {
        "status": lane.get("status", "unknown"),
        "expected_public_revision": lane.get("expected_public_revision"),
        "live_node_count": _int_or_zero(lane.get("live_node_count")),
        "synced_live_nodes": _int_or_zero(lane.get("synced_live_nodes")),
        "behind_live_nodes": _int_or_zero(lane.get("behind_live_nodes")),
        "unknown_live_nodes": _int_or_zero(lane.get("unknown_live_nodes")),
        "dirty_live_nodes": _int_or_zero(lane.get("dirty_live_nodes")),
        "nodes": nodes,
    }


def _node_public_summary(node: dict[str, Any]) -> dict[str, Any]:
    revision = node.get("source_revision")
    return {
        "node_id": node.get("node_id"),
        "revision_status": node.get("revision_status") or "unknown",
        "source_revision": revision,
        "source_revision_short": node.get("source_revision_short") or _short_revision(revision),
        "source_branch": node.get("source_branch"),
        "source_dirty": node.get("source_dirty"),
        "chatp2p_version": node.get("chatp2p_version"),
        "collected_at": node.get("collected_at"),
    }


def _flatten_nodes(lanes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for label in ("primary", "backup"):
        for node in (lanes.get(label) or {}).get("nodes") or []:
            item = dict(node)
            item["lane"] = label
            nodes.append(item)
    return nodes


def _sync_state(
    *,
    expected_revision: str | None,
    live_node_count: int,
    synced_live_nodes: int,
    behind_live_nodes: int,
    unknown_live_nodes: int,
    dirty_live_nodes: int,
    errors: list[str],
) -> str:
    blocking_errors = [error for error in errors if "older or different" not in error]
    if not expected_revision or dirty_live_nodes or blocking_errors:
        return "blocked"
    if behind_live_nodes:
        return "waiting_for_autopull"
    if unknown_live_nodes:
        return "unknown_old_worker"
    if live_node_count > 0 and synced_live_nodes == live_node_count:
        return "synced"
    return "blocked"


def _report_status(sync_state: str, *, warnings: list[str]) -> str:
    if sync_state == "synced":
        return "warn" if warnings else "pass"
    if sync_state in {"waiting_for_autopull", "unknown_old_worker"}:
        return "warn"
    return "fail"


def _recommended_next_action(sync_state: str) -> str:
    if sync_state == "synced":
        return "partner_synced_continue"
    if sync_state in {"waiting_for_autopull", "unknown_old_worker"}:
        return "wait_for_partner_autopull"
    return "rerun_console_with_network_checks"


def _local_tracking_revision(repo: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    root = repo.expanduser().resolve()
    for ref in ("origin/main", "origin/HEAD"):
        try:
            completed = subprocess.run(
                [git, "rev-parse", "--verify", ref],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        value = completed.stdout.strip()
        if value:
            return value
    return None


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


def _is_stale(console_age_seconds: float | None, config: OperatorSyncStatusConfig) -> bool:
    if console_age_seconds is None:
        return True
    return console_age_seconds > config.autopull_stale_minutes * 60.0


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

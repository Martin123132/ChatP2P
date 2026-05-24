"""Read-only operator self-heal report for local ChatP2P workstations."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import read_json_file


OPERATOR_SELF_HEAL_REPORT_SCHEMA = "chatp2p.operator-self-heal-report.v1"
DEFAULT_OPERATOR_SELF_HEAL_FRESHNESS_SECONDS = 3600.0

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
class OperatorSelfHealConfig:
    console_report_path: Path
    daily_report_path: Path
    action_queue_path: Path
    out_dir: Path
    freshness_seconds: float = DEFAULT_OPERATOR_SELF_HEAL_FRESHNESS_SECONDS


def run_operator_self_heal(config: OperatorSelfHealConfig) -> dict[str, Any]:
    _validate_self_heal_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc)
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    console_input = _load_report_input(
        config.console_report_path,
        label="operator_console",
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    daily_input = _load_report_input(
        config.daily_report_path,
        label="daily_check",
        now=now,
        freshness_seconds=config.freshness_seconds,
    )
    action_queue_input = _load_report_input(
        config.action_queue_path,
        label="action_queue",
        now=now,
        freshness_seconds=config.freshness_seconds,
    )

    console = console_input.get("report") if isinstance(console_input.get("report"), dict) else {}
    daily = daily_input.get("report") if isinstance(daily_input.get("report"), dict) else {}
    action_queue = _select_action_queue(action_queue_input, console, daily)
    queue_path = _select_queue_path(config.action_queue_path, action_queue_input, console, daily)

    issues = _collect_self_heal_issues(
        console_input=console_input,
        daily_input=daily_input,
        action_queue_input=action_queue_input,
        console=console,
        daily=daily,
    )
    actions = _actions_for_issues(issues, action_queue=action_queue, queue_path=queue_path, source_report=console or daily)
    status = _self_heal_status(issues)
    json_path = out_dir / "operator-self-heal-report.json"
    markdown_path = out_dir / "operator-self-heal-report.md"
    report = {
        "schema": OPERATOR_SELF_HEAL_REPORT_SCHEMA,
        "ok": status != "fail",
        "status": status,
        "generated_at": now.isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "console_report_path": str(config.console_report_path),
            "daily_report_path": str(config.daily_report_path),
            "action_queue_path": str(config.action_queue_path),
            "out_dir": str(out_dir),
            "freshness_seconds": config.freshness_seconds,
            "execute_supported": False,
            "automatic_repairs": False,
        },
        "inputs": {
            "operator_console": _input_public_summary(console_input),
            "daily_check": _input_public_summary(daily_input),
            "action_queue": _input_public_summary(action_queue_input),
        },
        "summary": {
            "repairable_issue_count": len(issues),
            "selected_action_ids": [action["action_id"] for action in actions],
            "top_self_heal_action": actions[0]["action_id"] if actions else None,
            "top_dry_run_command": ((actions[0].get("run_action") or {}).get("dry_run_command") if actions else None),
            "top_execute_command": ((actions[0].get("run_action") or {}).get("execute_command") if actions else None),
            "partner_required": False,
            "can_continue_without_partner": _can_continue_without_partner(status=status, issues=issues, console=console),
            "recommended_next_action": actions[0]["action_id"] if actions else "continue_development",
        },
        "issues": issues,
        "actions": actions,
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
        },
    }
    report = _redact_sensitive(report)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_operator_self_heal_markdown(report), encoding="utf-8")
    return report


def format_operator_self_heal_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    return "\n".join(
        [
            f"ChatP2P self-heal: {str(report.get('status', 'unknown')).upper()}",
            f"Repairable issues: {summary.get('repairable_issue_count', 0)}",
            f"Top action: {summary.get('top_self_heal_action') or 'none'}",
            f"Partner required: {_yes_no(summary.get('partner_required'))}",
            f"Report: {(report.get('artifacts') or {}).get('json')}",
        ]
    )


def format_operator_self_heal_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Operator Self-Heal",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Repairable issues: **{summary.get('repairable_issue_count', 0)}**",
        f"- Top self-heal action: `{summary.get('top_self_heal_action') or 'none'}`",
        f"- Partner required: **{_yes_no(summary.get('partner_required'))}**",
        f"- Automatic repairs: **{_yes_no(False)}**",
        f"- Generated at: `{report.get('generated_at')}`",
        "",
        "## Inputs",
        "",
        "| Input | Status | Fresh | Path |",
        "| --- | --- | --- | --- |",
    ]
    for key, item in (report.get("inputs") or {}).items():
        lines.append(
            "| "
            + " | ".join(
                [
                    key,
                    str(item.get("status", "unknown")),
                    _yes_no(item.get("fresh")),
                    f"`{item.get('path')}`",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Issues", ""])
    issues = report.get("issues") or []
    if not issues:
        lines.append("- No self-heal issues found.")
    else:
        for issue in issues:
            lines.append(
                f"- `{issue.get('issue_id')}` ({issue.get('severity')}): {issue.get('message')}"
            )
    lines.extend(["", "## Actions", ""])
    actions = report.get("actions") or []
    if not actions:
        lines.append("- No self-heal actions selected.")
    else:
        for action in actions:
            run_action = action.get("run_action") or {}
            lines.extend(
                [
                    f"### `{action.get('action_id')}`",
                    "",
                    f"- Partner required: **{_yes_no(action.get('partner_required'))}**",
                    f"- Source issue: `{action.get('issue_id')}`",
                    "",
                    "Dry run:",
                    "",
                    "```powershell",
                    str(run_action.get("dry_run_command") or action.get("direct_command") or "No command available."),
                    "```",
                    "",
                    "Execute:",
                    "",
                    "```powershell",
                    str(run_action.get("execute_command") or "No execute command is available for this action."),
                    "```",
                    "",
                ]
            )
    lines.append("")
    return "\n".join(lines)


def latest_self_heal_summary(path: Path, *, now: datetime, freshness_seconds: float) -> dict[str, Any]:
    loaded = _load_report_input(path, label="self_heal", now=now, freshness_seconds=freshness_seconds)
    summary = loaded.get("report", {}).get("summary") if isinstance(loaded.get("report"), dict) else {}
    return {
        "path": str(path.expanduser().resolve()),
        "exists": loaded.get("exists"),
        "ok": loaded.get("ok"),
        "status": loaded.get("status"),
        "fresh": loaded.get("fresh"),
        "generated_at": loaded.get("generated_at"),
        "repairable_issue_count": summary.get("repairable_issue_count", 0) if isinstance(summary, dict) else 0,
        "top_self_heal_action": summary.get("top_self_heal_action") if isinstance(summary, dict) else None,
        "top_dry_run_command": summary.get("top_dry_run_command") if isinstance(summary, dict) else None,
        "top_execute_command": summary.get("top_execute_command") if isinstance(summary, dict) else None,
    }


def _collect_self_heal_issues(
    *,
    console_input: dict[str, Any],
    daily_input: dict[str, Any],
    action_queue_input: dict[str, Any],
    console: dict[str, Any],
    daily: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    privacy_findings = _privacy_finding_count(console, daily)
    if privacy_findings > 0:
        issues.append(
            _issue(
                "public_privacy_findings",
                action_id="fix_public_privacy_findings",
                priority=10,
                severity="blocker",
                source="privacy_scan",
                message=f"Public privacy scan has {privacy_findings} finding(s).",
            )
        )

    if _input_needs_refresh(daily_input):
        issues.append(
            _issue(
                "daily_check_report_missing_or_stale",
                action_id="refresh_daily_check_report",
                priority=30,
                severity="warning",
                source="daily_check",
                message=f"Daily-check report is {daily_input.get('status')}.",
            )
        )
    if _input_needs_refresh(console_input):
        issues.append(
            _issue(
                "operator_console_report_missing_or_stale",
                action_id="regenerate_operator_console",
                priority=32,
                severity="warning",
                source="operator_console",
                message=f"Operator Console report is {console_input.get('status')}.",
            )
        )

    reliability = console.get("reliability") if isinstance(console.get("reliability"), dict) else {}
    reliability_status = str(reliability.get("status") or "")
    if reliability_status in {"missing", "not_configured"}:
        issues.append(
            _issue(
                "reliability_pack_missing",
                action_id="run_reliability_pack_when_ready",
                priority=40,
                severity="warning",
                source="reliability",
                message="Reliability pack summary is missing.",
            )
        )
    elif reliability.get("exists") and reliability.get("fresh") is False:
        issues.append(
            _issue(
                "reliability_pack_stale",
                action_id="refresh_reliability_pack",
                priority=42,
                severity="warning",
                source="reliability",
                message="Reliability pack summary is stale.",
            )
        )

    action_run = ((console.get("action_runner") or {}).get("last_run") or {}) if isinstance(console, dict) else {}
    if str(action_run.get("status") or "") in {"missing", "stale"}:
        issues.append(
            _issue(
                "action_run_report_missing_or_stale",
                action_id="create_action_run_report",
                priority=80,
                severity="info",
                source="action_runner",
                message=f"Action-run report is {action_run.get('status')}.",
            )
        )

    if _input_needs_refresh(action_queue_input):
        issues.append(
            _issue(
                "action_queue_report_missing_or_stale",
                action_id="refresh_action_queue",
                priority=82,
                severity="info",
                source="action_queue",
                message=f"Action queue report is {action_queue_input.get('status')}.",
            )
        )

    by_id: dict[str, dict[str, Any]] = {}
    for issue in issues:
        existing = by_id.get(str(issue["issue_id"]))
        if existing is None or int(issue["priority"]) < int(existing["priority"]):
            by_id[str(issue["issue_id"])] = issue
    return sorted(by_id.values(), key=lambda item: (int(item["priority"]), str(item["issue_id"])))


def _actions_for_issues(
    issues: list[dict[str, Any]],
    *,
    action_queue: dict[str, Any],
    queue_path: Path | None,
    source_report: dict[str, Any],
) -> list[dict[str, Any]]:
    queued_actions = _queued_actions_by_id(action_queue)
    actions = []
    for issue in issues:
        action_id = str(issue["action_id"])
        queued = queued_actions.get(action_id) or _find_compatible_queued_action(action_id, queued_actions)
        command = _first_suggested_command(queued)
        action = {
            "issue_id": issue["issue_id"],
            "action_id": action_id,
            "severity": issue["severity"],
            "priority": issue["priority"],
            "partner_required": False,
            "can_run_without_partner": True,
            "source": issue["source"],
            "queued": queued is not None,
            "queue_path": str(queue_path) if queue_path is not None else None,
            "direct_command": command.get("command") if command else _fallback_command(action_id, source_report),
        }
        if queue_path is not None and queued is not None:
            action["run_action"] = {
                "dry_run_command": _operator_run_action_command(queue_path, action_id, execute=False),
                "execute_command": _operator_run_action_command(queue_path, action_id, execute=True),
            }
        else:
            action["run_action"] = {
                "dry_run_command": None,
                "execute_command": None,
                "reason": "Action was not available in the loaded action queue.",
            }
        actions.append(action)
    return actions


def _load_report_input(path: Path, *, label: str, now: datetime, freshness_seconds: float) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "label": label,
            "path": str(resolved),
            "exists": False,
            "ok": False,
            "status": "missing",
            "fresh": False,
            "generated_at": None,
            "age_seconds": None,
            "report": None,
            "error": None,
        }
    try:
        report = read_json_file(resolved, description=f"{label} report")
    except (OSError, ValueError) as exc:
        return {
            "label": label,
            "path": str(resolved),
            "exists": True,
            "ok": False,
            "status": "unreadable",
            "fresh": False,
            "generated_at": None,
            "age_seconds": None,
            "report": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(report, dict):
        return {
            "label": label,
            "path": str(resolved),
            "exists": True,
            "ok": False,
            "status": "invalid",
            "fresh": False,
            "generated_at": None,
            "age_seconds": None,
            "report": None,
            "error": "report JSON root is not an object",
        }
    generated_at = report.get("generated_at") or report.get("updated_at")
    age_seconds = _age_seconds(generated_at, now=now)
    fresh = age_seconds is None or age_seconds <= freshness_seconds
    status = "pass" if fresh else "stale"
    return {
        "label": label,
        "path": str(resolved),
        "exists": True,
        "ok": True,
        "status": status,
        "fresh": fresh,
        "generated_at": generated_at,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "report": report,
        "error": None,
    }


def _select_action_queue(
    action_queue_input: dict[str, Any],
    console: dict[str, Any],
    daily: dict[str, Any],
) -> dict[str, Any]:
    loaded = action_queue_input.get("report")
    if isinstance(loaded, dict):
        return loaded
    embedded_console = console.get("action_queue") if isinstance(console.get("action_queue"), dict) else None
    if embedded_console:
        return embedded_console
    embedded_daily = daily.get("action_queue") if isinstance(daily.get("action_queue"), dict) else None
    return embedded_daily or {}


def _select_queue_path(
    requested_path: Path,
    action_queue_input: dict[str, Any],
    console: dict[str, Any],
    daily: dict[str, Any],
) -> Path | None:
    if action_queue_input.get("exists"):
        return requested_path.expanduser().resolve()
    for report in (console, daily):
        artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
        queue_path = artifacts.get("action_queue_json")
        if queue_path:
            return Path(str(queue_path)).expanduser().resolve()
    return None


def _input_public_summary(input_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": input_report.get("path"),
        "exists": input_report.get("exists"),
        "ok": input_report.get("ok"),
        "status": input_report.get("status"),
        "fresh": input_report.get("fresh"),
        "generated_at": input_report.get("generated_at"),
        "age_seconds": input_report.get("age_seconds"),
        "error": input_report.get("error"),
    }


def _issue(issue_id: str, *, action_id: str, priority: int, severity: str, source: str, message: str) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "action_id": action_id,
        "priority": priority,
        "severity": severity,
        "source": source,
        "partner_required": False,
        "repairable": True,
        "message": message,
    }


def _input_needs_refresh(input_report: dict[str, Any]) -> bool:
    return str(input_report.get("status") or "") in {"missing", "stale", "unreadable", "invalid"}


def _privacy_finding_count(console: dict[str, Any], daily: dict[str, Any]) -> int:
    counts: list[int] = []
    privacy = console.get("privacy_scan") if isinstance(console.get("privacy_scan"), dict) else {}
    if privacy:
        counts.append(int(privacy.get("finding_count") or 0))
        if privacy.get("ok") is False and not counts[-1]:
            counts[-1] = 1
    daily_privacy = ((daily.get("steps") or {}).get("privacy_scan") or {}) if isinstance(daily, dict) else {}
    if daily_privacy:
        counts.append(int(daily_privacy.get("finding_count") or 0))
        if daily_privacy.get("ok") is False and not counts[-1]:
            counts[-1] = 1
    return max(counts) if counts else 0


def _queued_actions_by_id(queue: dict[str, Any]) -> dict[str, dict[str, Any]]:
    actions = queue.get("actions") if isinstance(queue.get("actions"), list) else []
    return {str(action.get("action_id")): action for action in actions if isinstance(action, dict)}


def _find_compatible_queued_action(action_id: str, actions: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if action_id == "refresh_daily_check_report":
        for key, action in actions.items():
            if key.startswith("review_warning_daily_check_automation"):
                return action
    return None


def _first_suggested_command(action: dict[str, Any] | None) -> dict[str, Any] | None:
    commands = action.get("suggested_commands") if isinstance(action, dict) else None
    if isinstance(commands, list) and commands and isinstance(commands[0], dict):
        return commands[0]
    return None


def _fallback_command(action_id: str, report: dict[str, Any]) -> str | None:
    config = report.get("config") if isinstance(report.get("config"), dict) else {}
    if action_id == "fix_public_privacy_findings" and config.get("repo"):
        return f"python -m chatp2p.cli operator privacy-scan --root {_ps(config['repo'])}"
    if action_id == "regenerate_operator_console":
        return _console_command(config)
    if action_id == "refresh_daily_check_report":
        return _daily_command(config)
    if action_id in {"refresh_reliability_pack", "run_reliability_pack_when_ready"}:
        return _reliability_command(config)
    if action_id == "refresh_action_queue":
        out_dir = config.get("out_dir")
        if out_dir:
            return "\n".join(
                [
                    "python -m chatp2p.cli operator action-queue `",
                    f"  --daily-report {_ps(Path(str(out_dir)) / 'daily-check.json')} `",
                    f"  --out {_ps(out_dir)}",
                ]
            )
    return None


def _console_command(config: dict[str, Any]) -> str | None:
    required = ("repo", "home", "primary_invite_path", "out_dir")
    if any(not config.get(key) for key in required):
        return None
    lines = [
        "python -m chatp2p.cli operator console `",
        f"  --repo {_ps(config['repo'])} `",
        f"  --home {_ps(config['home'])} `",
        f"  --primary-invite {_ps(config['primary_invite_path'])} `",
    ]
    if config.get("backup_invite_path"):
        lines.append(f"  --backup-invite {_ps(config['backup_invite_path'])} `")
    if config.get("reliability_dir"):
        lines.append(f"  --reliability-dir {_ps(config['reliability_dir'])} `")
    if config.get("daily_check_dir"):
        lines.append(f"  --daily-check-dir {_ps(config['daily_check_dir'])} `")
    if config.get("skip_network_checks"):
        lines.append("  --skip-network-checks `")
    lines.append(f"  --out {_ps(config['out_dir'])}")
    return "\n".join(lines)


def _daily_command(config: dict[str, Any]) -> str | None:
    required = ("repo", "home", "primary_invite_path")
    if any(not config.get(key) for key in required):
        return None
    out_dir = config.get("daily_check_dir") or config.get("out_dir")
    if not out_dir:
        return None
    lines = [
        "python -m chatp2p.cli operator daily-check `",
        f"  --repo {_ps(config['repo'])} `",
        f"  --home {_ps(config['home'])} `",
        f"  --primary-invite {_ps(config['primary_invite_path'])} `",
    ]
    if config.get("backup_invite_path"):
        lines.append(f"  --backup-invite {_ps(config['backup_invite_path'])} `")
    if config.get("reliability_dir"):
        lines.append(f"  --reliability-dir {_ps(config['reliability_dir'])} `")
    lines.append(f"  --out {_ps(out_dir)}")
    return "\n".join(lines)


def _reliability_command(config: dict[str, Any]) -> str | None:
    required = ("primary_invite_path", "backup_invite_path", "reliability_dir")
    if any(not config.get(key) for key in required):
        return None
    return "\n".join(
        [
            "python -m chatp2p.cli operator reliability-pack `",
            f"  --primary-invite {_ps(config['primary_invite_path'])} `",
            f"  --backup-invite {_ps(config['backup_invite_path'])} `",
            f"  --out {_ps(config['reliability_dir'])}",
        ]
    )


def _operator_run_action_command(queue_path: Path, action_id: str, *, execute: bool) -> str:
    mode_flag = "--execute" if execute else "--dry-run"
    return "\n".join(
        [
            "python -m chatp2p.cli operator run-action `",
            f"  --queue {_ps(queue_path)} `",
            f"  --action {_ps(action_id)} `",
            f"  {mode_flag}",
        ]
    )


def _self_heal_status(issues: list[dict[str, Any]]) -> str:
    severities = {str(issue.get("severity")) for issue in issues}
    if "blocker" in severities:
        return "fail"
    if issues:
        return "warn"
    return "pass"


def _can_continue_without_partner(*, status: str, issues: list[dict[str, Any]], console: dict[str, Any]) -> bool:
    if any(issue.get("severity") == "blocker" for issue in issues):
        return False
    summary = console.get("summary") if isinstance(console.get("summary"), dict) else {}
    if "can_continue_without_partner" in summary:
        return bool(summary.get("can_continue_without_partner"))
    return status != "fail"


def _age_seconds(generated_at: Any, *, now: datetime) -> float | None:
    if not isinstance(generated_at, str) or not generated_at:
        return None
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("token", "secret", "private_key", "auth_key")):
                redacted[key] = "<redacted>" if item else item
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _TOKEN_PATTERNS:
            redacted = pattern.sub(lambda match: _redacted_token_match(match), redacted)
        for pattern, replacement in _PRIVATE_PATH_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    return value


def _redacted_token_match(match: re.Match[str]) -> str:
    if match.lastindex:
        return f"{match.group(1)}<redacted>"
    return "<redacted>"


def _validate_self_heal_config(config: OperatorSelfHealConfig) -> None:
    if config.freshness_seconds <= 0:
        raise ValueError("--freshness-seconds must be greater than 0")


def _ps(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _yes_no(value: Any) -> str:
    if value is None:
        return "unknown"
    return "yes" if bool(value) else "no"

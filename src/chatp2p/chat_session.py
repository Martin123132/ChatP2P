"""Persistent requester chat sessions backed by funded ChatP2P jobs."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .alpha import load_alpha_invite
from .chat_request import ChatAskConfig, run_chat_ask
from .client import CoordinatorClient
from .jsonio import read_json_file


CHAT_SESSION_REPORT_SCHEMA = "chatp2p.chat-session-report.v1"
CHAT_SESSION_STATUS_REPORT_SCHEMA = "chatp2p.chat-session-status-report.v1"
CHAT_SESSION_RESUME_REPORT_SCHEMA = "chatp2p.chat-session-resume-report.v1"
CHAT_SESSION_SYNC_REPORT_SCHEMA = "chatp2p.chat-session-sync-report.v1"
CHAT_SESSION_CONTINUE_REPORT_SCHEMA = "chatp2p.chat-session-continue-report.v1"
DEFAULT_SESSION_ID = "default"
DEFAULT_MAX_CONTEXT_TURNS = 8
DEFAULT_COORDINATOR_URL = "http://127.0.0.1:8765"


@dataclass(frozen=True)
class ChatSessionConfig:
    out_dir: Path = Path(".mesh/chat-session")
    session_id: str = DEFAULT_SESSION_ID
    title: str | None = None
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    model: str = "tiny-test-model"
    prompt: str = "Explain ChatP2P in one sentence."
    system: str | None = "Be concise."
    requester_account_id: str = "requester_demo"
    job_cost: int = 1
    reward: int = 1
    temperature: float | None = 0.2
    max_tokens: int | None = 256
    ttl_seconds: int = 300
    timeout_seconds: float = 60.0
    poll_interval: float = 0.5
    no_wait: bool = False
    client_timeout_seconds: float = 10.0
    max_context_turns: int = DEFAULT_MAX_CONTEXT_TURNS


@dataclass(frozen=True)
class ChatSessionStatusConfig:
    out_dir: Path = Path(".mesh/chat-session")
    session_id: str = DEFAULT_SESSION_ID


@dataclass(frozen=True)
class ChatSessionResumeConfig:
    out_dir: Path = Path(".mesh/chat-session")
    session_id: str = DEFAULT_SESSION_ID
    turn_id: str | None = None
    include_submitted: bool = False
    dry_run: bool = False
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    model: str | None = None
    system: str | None = None
    requester_account_id: str | None = None
    job_cost: int | None = None
    reward: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    ttl_seconds: int | None = None
    timeout_seconds: float | None = None
    poll_interval: float | None = None
    no_wait: bool = False
    client_timeout_seconds: float | None = None
    max_context_turns: int | None = None


@dataclass(frozen=True)
class ChatSessionSyncConfig:
    out_dir: Path = Path(".mesh/chat-session")
    session_id: str = DEFAULT_SESSION_ID
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    dry_run: bool = False
    client_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ChatSessionContinueConfig:
    out_dir: Path = Path(".mesh/chat-session")
    session_id: str = DEFAULT_SESSION_ID
    title: str | None = None
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    model: str = "tiny-test-model"
    prompt: str = "Explain ChatP2P in one sentence."
    system: str | None = "Be concise."
    requester_account_id: str = "requester_demo"
    job_cost: int = 1
    reward: int = 1
    temperature: float | None = 0.2
    max_tokens: int | None = 256
    ttl_seconds: int = 300
    timeout_seconds: float = 60.0
    poll_interval: float = 0.5
    no_wait: bool = False
    client_timeout_seconds: float = 10.0
    max_context_turns: int = DEFAULT_MAX_CONTEXT_TURNS


def run_chat_session(config: ChatSessionConfig) -> dict[str, Any]:
    """Append one funded user turn to a persistent local chat session."""

    _validate_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    session_path = out_dir / "chat-session.json"
    markdown_path = out_dir / "chat-session.md"
    existing = _load_existing_session(session_path)
    session = _base_session(config=config, existing=existing, now=now)
    previous_turns = list(session.get("turns") or [])
    turn_index = len(previous_turns) + 1
    turn_id = f"turn-{turn_index:04d}"
    context_messages = _context_messages(previous_turns, max_turns=config.max_context_turns)

    ask_report = run_chat_ask(
        ChatAskConfig(
            out_dir=out_dir / turn_id,
            coordinator_url=config.coordinator_url,
            invite_path=config.invite_path,
            admission_token=config.admission_token,
            model=config.model,
            prompt=config.prompt,
            system=config.system,
            context_messages=tuple(context_messages),
            requester_account_id=config.requester_account_id,
            job_cost=config.job_cost,
            reward=config.reward,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            ttl_seconds=config.ttl_seconds,
            timeout_seconds=config.timeout_seconds,
            poll_interval=config.poll_interval,
            no_wait=config.no_wait,
            client_timeout_seconds=config.client_timeout_seconds,
        )
    )
    turn = _turn_from_ask_report(
        turn_id=turn_id,
        turn_index=turn_index,
        prompt=config.prompt,
        context_message_count=len(context_messages),
        ask_report=ask_report,
    )
    turns = [*previous_turns, turn]
    session["updated_at"] = now
    session["turns"] = turns
    summary = _session_summary(turns=turns, latest_turn=turn)
    session["summary"] = summary
    session["status"] = summary["status"]
    session["ok"] = summary["status"] in {"pass", "submitted"}
    session["errors"] = list(turn.get("errors") or [])
    session["artifacts"] = {
        "json": str(session_path),
        "markdown": str(markdown_path),
        "latest_turn_json": (ask_report.get("artifacts") or {}).get("json"),
        "latest_turn_markdown": (ask_report.get("artifacts") or {}).get("markdown"),
    }
    session["duration_seconds"] = round(time.time() - started_at, 3)

    session_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(format_chat_session_markdown(session), encoding="utf-8")
    return session


def run_chat_session_status(config: ChatSessionStatusConfig) -> dict[str, Any]:
    """Write a read-only status report for an existing chat session."""

    _validate_status_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    session_path, _, status_path, status_markdown_path, _, _, _, _, _, _ = _session_paths(out_dir)
    session = _require_session(session_path=session_path, session_id=config.session_id)
    turns = list(session.get("turns") or [])
    turn_reports = [_turn_report_summary(turn) for turn in turns]
    summary = _status_report_summary(turns=turns, turn_reports=turn_reports)
    report = {
        "schema": CHAT_SESSION_STATUS_REPORT_SCHEMA,
        "ok": True,
        "status": summary["status"],
        "generated_at": now,
        "duration_seconds": round(time.time() - started_at, 3),
        "session_id": config.session_id,
        "session_path": str(session_path),
        "summary": summary,
        "turns": _status_turns(turns=turns, turn_reports=turn_reports),
        "artifacts": {
            "json": str(status_path),
            "markdown": str(status_markdown_path),
            "session_json": str(session_path),
        },
    }
    status_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    status_markdown_path.write_text(format_chat_session_status_markdown(report), encoding="utf-8")
    return report


def run_chat_session_sync(config: ChatSessionSyncConfig) -> dict[str, Any]:
    """Reconcile existing session turns against coordinator snapshot evidence."""

    _validate_sync_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    (
        session_path,
        markdown_path,
        _,
        _,
        _,
        _,
        sync_path,
        sync_markdown_path,
        _,
        _,
    ) = _session_paths(out_dir)
    session = _require_session(session_path=session_path, session_id=config.session_id)
    turns = list(session.get("turns") or [])
    connection = _resolve_sync_connection(config=config, session=session)
    client = CoordinatorClient(
        connection["coordinator_url"],
        admission_token=connection["token"],
        timeout_seconds=config.client_timeout_seconds,
    )

    errors: list[str] = []
    snapshot: dict[str, Any] | None = None
    try:
        snapshot = client.snapshot()
    except Exception as exc:
        errors.append(_format_sync_exception(exc))

    updates: list[dict[str, Any]] = []
    changed = False
    if snapshot is not None:
        synced_turns = []
        requester_account_id = str((session.get("config") or {}).get("requester_account_id") or "")
        for turn in turns:
            updated_turn, update = _sync_turn_from_snapshot(
                turn=turn,
                snapshot=snapshot,
                requester_account_id=requester_account_id,
            )
            synced_turns.append(updated_turn)
            if update is not None:
                updates.append(update)
                changed = changed or update["changed"]
        if changed and not config.dry_run:
            session["updated_at"] = now
            session["turns"] = synced_turns
            latest_turn = synced_turns[-1] if synced_turns else {}
            session["summary"] = _session_summary(turns=synced_turns, latest_turn=latest_turn)
            session["status"] = session["summary"]["status"]
            session["ok"] = session["status"] in {"pass", "submitted"}
            session["errors"] = list(latest_turn.get("errors") or [])
            session["artifacts"] = {
                "json": str(session_path),
                "markdown": str(markdown_path),
            }
            session_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
            markdown_path.write_text(format_chat_session_markdown(session), encoding="utf-8")

    unresolved = _sync_unresolved_turns(turns if config.dry_run or not changed else session.get("turns") or [])
    status = _sync_status(errors=errors, updates=updates, unresolved=unresolved, dry_run=config.dry_run)
    report = {
        "schema": CHAT_SESSION_SYNC_REPORT_SCHEMA,
        "ok": status in {"pass", "warn", "dry_run"},
        "status": status,
        "dry_run": config.dry_run,
        "generated_at": now,
        "duration_seconds": round(time.time() - started_at, 3),
        "session_id": config.session_id,
        "config": {
            "out_dir": str(out_dir),
            "coordinator": connection["coordinator_url"],
            "invite_path": str(config.invite_path.expanduser().resolve()) if config.invite_path else None,
            "auth": {"token_present": bool(connection["token"])},
            "remote_side_effect": "read_coordinator_snapshot_only",
        },
        "summary": {
            "status": status,
            "turn_count": len(turns),
            "checked_turns": len([turn for turn in turns if turn.get("job_id")]),
            "updated_turns": len([update for update in updates if update["changed"]]),
            "unresolved_turns": len(unresolved),
            "snapshot_available": snapshot is not None,
            "recommended_next_action": _sync_recommended_next_action(
                status=status,
                updates=updates,
                unresolved=unresolved,
                errors=errors,
                dry_run=config.dry_run,
            ),
        },
        "updates": updates,
        "unresolved_turns": unresolved,
        "snapshot_status": (snapshot or {}).get("status"),
        "errors": errors,
        "artifacts": {
            "json": str(sync_path),
            "markdown": str(sync_markdown_path),
            "session_json": str(session_path),
            "session_markdown": str(markdown_path),
        },
    }
    sync_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    sync_markdown_path.write_text(format_chat_session_sync_markdown(report), encoding="utf-8")
    return report


def run_chat_session_continue(config: ChatSessionContinueConfig) -> dict[str, Any]:
    """Safely append a new chat turn after status/sync preflight checks."""

    session_config = _continue_session_config(config)
    _validate_config(session_config)
    started_at = time.time()
    now = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (
        session_path,
        _,
        _,
        _,
        _,
        _,
        _,
        _,
        continue_path,
        continue_markdown_path,
    ) = _session_paths(out_dir)

    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    status_before: dict[str, Any] | None = None
    status_after_sync: dict[str, Any] | None = None
    sync_report: dict[str, Any] | None = None
    session_report: dict[str, Any] | None = None
    blocked_reason: str | None = None

    if session_path.exists():
        status_before = run_chat_session_status(
            ChatSessionStatusConfig(out_dir=out_dir, session_id=config.session_id)
        )
        steps.append(_continue_step("session_status", status_before["status"], status_before.get("summary")))
        status_for_decision = status_before

        if _continue_status_needs_sync(status_before):
            sync_report = run_chat_session_sync(
                ChatSessionSyncConfig(
                    out_dir=out_dir,
                    session_id=config.session_id,
                    coordinator_url=config.coordinator_url,
                    invite_path=config.invite_path,
                    admission_token=config.admission_token,
                    dry_run=False,
                    client_timeout_seconds=config.client_timeout_seconds,
                )
            )
            steps.append(_continue_step("session_sync", sync_report["status"], sync_report.get("summary")))
            if sync_report["status"] == "fail":
                blocked_reason = "sync_failed"
                errors.extend(sync_report.get("errors") or [])
            else:
                status_after_sync = run_chat_session_status(
                    ChatSessionStatusConfig(out_dir=out_dir, session_id=config.session_id)
                )
                steps.append(
                    _continue_step(
                        "session_status_after_sync",
                        status_after_sync["status"],
                        status_after_sync.get("summary"),
                    )
                )
                status_for_decision = status_after_sync

        unresolved = _continue_unresolved_turns(status_for_decision)
        if unresolved and blocked_reason is None:
            blocked_reason = "unresolved_session_turns"
            warnings.append("A failed or submitted turn is still unresolved; no new funded turn was created.")
        if blocked_reason is None:
            session_report = run_chat_session(session_config)
            steps.append(_continue_step("append_turn", session_report["status"], session_report.get("summary")))
    else:
        steps.append(_continue_step("session_status", "skipped", {"reason": "new_session"}))
        session_report = run_chat_session(session_config)
        steps.append(_continue_step("append_turn", session_report["status"], session_report.get("summary")))

    report_status = _continue_report_status(
        blocked_reason=blocked_reason,
        session_report=session_report,
        errors=errors,
    )
    report = {
        "schema": CHAT_SESSION_CONTINUE_REPORT_SCHEMA,
        "ok": report_status in {"pass", "submitted"},
        "status": report_status,
        "generated_at": now,
        "duration_seconds": round(time.time() - started_at, 3),
        "session_id": config.session_id,
        "config": _safe_config(session_config),
        "summary": {
            "status": report_status,
            "blocked_reason": blocked_reason,
            "turn_created": session_report is not None,
            "turn_count": ((session_report or {}).get("summary") or {}).get("turn_count"),
            "latest_turn": ((session_report or {}).get("summary") or {}).get("latest_turn"),
            "recommended_next_action": _continue_recommended_next_action(
                blocked_reason=blocked_reason,
                session_report=session_report,
                status_report=status_after_sync or status_before,
                sync_report=sync_report,
                errors=errors,
            ),
        },
        "steps": steps,
        "status_before": _continue_embedded_report(status_before),
        "sync": _continue_embedded_report(sync_report),
        "status_after_sync": _continue_embedded_report(status_after_sync),
        "session": _continue_embedded_report(session_report),
        "warnings": warnings,
        "errors": errors,
        "artifacts": {
            "json": str(continue_path),
            "markdown": str(continue_markdown_path),
            "session_json": str(session_path),
            "latest_turn_json": ((session_report or {}).get("artifacts") or {}).get("latest_turn_json"),
        },
    }
    continue_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    continue_markdown_path.write_text(format_chat_session_continue_markdown(report), encoding="utf-8")
    return report


def run_chat_session_resume(config: ChatSessionResumeConfig) -> dict[str, Any]:
    """Append a retry turn for the latest failed/submitted session turn."""

    _validate_resume_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    session_path, markdown_path, _, _, resume_path, resume_markdown_path, _, _, _, _ = _session_paths(out_dir)
    session = _require_session(session_path=session_path, session_id=config.session_id)
    turns = list(session.get("turns") or [])
    target_turn = _select_resume_turn(
        turns=turns,
        turn_id=config.turn_id,
        include_submitted=config.include_submitted,
    )
    if target_turn is None:
        report = _resume_noop_report(
            config=config,
            session_path=session_path,
            resume_path=resume_path,
            resume_markdown_path=resume_markdown_path,
            started_at=started_at,
            generated_at=now,
        )
        _write_resume_report(report, resume_path=resume_path, resume_markdown_path=resume_markdown_path)
        return report

    effective = _effective_resume_config(
        config=config,
        session=session,
        prompt=str(target_turn["prompt"]),
    )
    _validate_config(effective)
    context_source_turns = turns[: max(0, int(target_turn.get("turn_index", len(turns))) - 1)]
    context_messages = _context_messages(context_source_turns, max_turns=effective.max_context_turns)
    new_turn_index = len(turns) + 1
    new_turn_id = f"turn-{new_turn_index:04d}"
    retry_attempt = _retry_attempt(turns=turns, target_turn_id=str(target_turn["turn_id"]))
    warnings = []
    if target_turn.get("status") == "submitted":
        warnings.append("Retrying a submitted turn may create a duplicate credit spend.")

    ask_report: dict[str, Any] | None = None
    appended_turn: dict[str, Any] | None = None
    if not config.dry_run:
        ask_report = run_chat_ask(
            ChatAskConfig(
                out_dir=out_dir / new_turn_id,
                coordinator_url=effective.coordinator_url,
                invite_path=effective.invite_path,
                admission_token=effective.admission_token,
                model=effective.model,
                prompt=str(target_turn["prompt"]),
                system=effective.system,
                context_messages=tuple(context_messages),
                requester_account_id=effective.requester_account_id,
                job_cost=effective.job_cost,
                reward=effective.reward,
                temperature=effective.temperature,
                max_tokens=effective.max_tokens,
                ttl_seconds=effective.ttl_seconds,
                timeout_seconds=effective.timeout_seconds,
                poll_interval=effective.poll_interval,
                no_wait=effective.no_wait,
                client_timeout_seconds=effective.client_timeout_seconds,
            )
        )
        appended_turn = _turn_from_ask_report(
            turn_id=new_turn_id,
            turn_index=new_turn_index,
            prompt=str(target_turn["prompt"]),
            context_message_count=len(context_messages),
            ask_report=ask_report,
        )
        appended_turn["retry_of_turn_id"] = target_turn["turn_id"]
        appended_turn["retry_attempt"] = retry_attempt
        turns = [*turns, appended_turn]
        session["updated_at"] = now
        session["turns"] = turns
        summary = _session_summary(turns=turns, latest_turn=appended_turn)
        session["summary"] = summary
        session["status"] = summary["status"]
        session["ok"] = summary["status"] in {"pass", "submitted"}
        session["errors"] = list(appended_turn.get("errors") or [])
        session["artifacts"] = {
            "json": str(session_path),
            "markdown": str(markdown_path),
            "latest_turn_json": (ask_report.get("artifacts") or {}).get("json"),
            "latest_turn_markdown": (ask_report.get("artifacts") or {}).get("markdown"),
        }
        session["duration_seconds"] = round(time.time() - started_at, 3)
        session_path.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
        markdown_path.write_text(format_chat_session_markdown(session), encoding="utf-8")

    report_status = "dry_run" if config.dry_run else (session.get("status") if appended_turn else "pass")
    report = {
        "schema": CHAT_SESSION_RESUME_REPORT_SCHEMA,
        "ok": report_status in {"pass", "submitted", "dry_run"},
        "status": report_status,
        "dry_run": config.dry_run,
        "generated_at": now,
        "duration_seconds": round(time.time() - started_at, 3),
        "session_id": config.session_id,
        "target_turn": _resume_target_summary(target_turn),
        "retry": {
            "created": appended_turn is not None,
            "turn_id": new_turn_id,
            "retry_attempt": retry_attempt,
            "context_message_count": len(context_messages),
            "turn": appended_turn,
        },
        "summary": _resume_summary(
            status=report_status,
            target_turn=target_turn,
            appended_turn=appended_turn,
            dry_run=config.dry_run,
        ),
        "warnings": warnings,
        "errors": list((appended_turn or {}).get("errors") or []),
        "artifacts": {
            "json": str(resume_path),
            "markdown": str(resume_markdown_path),
            "session_json": str(session_path),
            "session_markdown": str(markdown_path),
            "latest_turn_json": (ask_report or {}).get("artifacts", {}).get("json") if ask_report else None,
        },
    }
    _write_resume_report(report, resume_path=resume_path, resume_markdown_path=resume_markdown_path)
    return report


def format_chat_session_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    latest = summary.get("latest_turn") or {}
    lines = [
        f"Chat session: {str(summary.get('status', 'unknown')).upper()}",
        f"Session: {report.get('session_id')}",
        f"Turns: {summary.get('turn_count')}",
        f"Latest job: {latest.get('job_id')}",
        f"Requester balance: {latest.get('requester_balance_after')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    if latest.get("answer"):
        lines.insert(5, f"Answer: {latest.get('answer')}")
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_chat_session_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    config = report.get("config") or {}
    lines = [
        "# ChatP2P Chat Session",
        "",
        f"- Status: **{str(summary.get('status', 'unknown')).upper()}**",
        f"- Session: `{report.get('session_id')}`",
        f"- Model: `{config.get('model')}`",
        f"- Requester account: `{config.get('requester_account_id')}`",
        f"- Turns: `{summary.get('turn_count')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Transcript",
        "",
    ]
    for turn in report.get("turns") or []:
        lines.extend(
            [
                f"### Turn {turn.get('turn_index')}",
                "",
                f"**User:** {turn.get('prompt')}",
                "",
            ]
        )
        answer = turn.get("answer")
        if answer:
            lines.extend([f"**Assistant:** {answer}", ""])
        else:
            lines.extend([f"**Assistant status:** `{turn.get('status')}`", ""])
    return "\n".join(lines)


def format_chat_session_status_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    latest = summary.get("latest_turn") or {}
    lines = [
        f"Chat session status: {str(report.get('status', 'unknown')).upper()}",
        f"Session: {report.get('session_id')}",
        f"Turns: {summary.get('turn_count')}",
        f"Failed: {summary.get('failed_turns')}",
        f"Submitted: {summary.get('submitted_turns')}",
        f"Latest: {latest.get('turn_id')} {latest.get('status')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    return "\n".join(lines)


def format_chat_session_resume_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    target = report.get("target_turn") or {}
    retry = report.get("retry") or {}
    lines = [
        f"Chat session resume: {str(report.get('status', 'unknown')).upper()}",
        f"Session: {report.get('session_id')}",
        f"Target: {target.get('turn_id')} {target.get('status')}",
        f"Retry turn: {retry.get('turn_id')}",
        f"Retry created: {retry.get('created')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    if report.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_chat_session_sync_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Chat session sync: {str(report.get('status', 'unknown')).upper()}",
        f"Session: {report.get('session_id')}",
        f"Checked turns: {summary.get('checked_turns')}",
        f"Updated turns: {summary.get('updated_turns')}",
        f"Unresolved turns: {summary.get('unresolved_turns')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_chat_session_continue_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    latest = summary.get("latest_turn") or {}
    lines = [
        f"Chat session continue: {str(report.get('status', 'unknown')).upper()}",
        f"Session: {report.get('session_id')}",
        f"Turn created: {summary.get('turn_created')}",
        f"Turns: {summary.get('turn_count')}",
        f"Latest job: {latest.get('job_id')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    if latest.get("answer"):
        lines.insert(5, f"Answer: {latest.get('answer')}")
    if summary.get("blocked_reason"):
        lines.insert(3, f"Blocked: {summary.get('blocked_reason')}")
    if report.get("warnings"):
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_chat_session_status_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Chat Session Status",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Session: `{report.get('session_id')}`",
        f"- Turns: `{summary.get('turn_count')}`",
        f"- Failed turns: `{summary.get('failed_turns')}`",
        f"- Submitted turns: `{summary.get('submitted_turns')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Turns",
        "",
    ]
    for turn in report.get("turns") or []:
        lines.append(
            f"- `{turn.get('turn_id')}` status `{turn.get('status')}` "
            f"job `{turn.get('job_id')}` artifact `{turn.get('artifact_status')}`"
        )
    return "\n".join(lines)


def format_chat_session_continue_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    latest = summary.get("latest_turn") or {}
    lines = [
        "# ChatP2P Chat Session Continue",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Session: `{report.get('session_id')}`",
        f"- Turn created: `{summary.get('turn_created')}`",
        f"- Blocked reason: `{summary.get('blocked_reason')}`",
        f"- Turns: `{summary.get('turn_count')}`",
        f"- Latest job: `{latest.get('job_id')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Steps",
        "",
    ]
    for step in report.get("steps") or []:
        lines.append(f"- `{step.get('name')}`: `{step.get('status')}`")
    if latest.get("answer"):
        lines.extend(["", "## Answer", "", str(latest["answer"])])
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_chat_session_sync_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Chat Session Sync",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Session: `{report.get('session_id')}`",
        f"- Checked turns: `{summary.get('checked_turns')}`",
        f"- Updated turns: `{summary.get('updated_turns')}`",
        f"- Unresolved turns: `{summary.get('unresolved_turns')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Updates",
        "",
    ]
    updates = report.get("updates") or []
    if updates:
        for update in updates:
            lines.append(
                f"- `{update.get('turn_id')}` job `{update.get('job_id')}` "
                f"{update.get('previous_status')} -> {update.get('synced_status')} "
                f"changed `{update.get('changed')}`"
            )
    else:
        lines.append("- No local turn updates were available from the snapshot.")
    if report.get("unresolved_turns"):
        lines.extend(["", "## Unresolved Turns", ""])
        for turn in report["unresolved_turns"]:
            lines.append(
                f"- `{turn.get('turn_id')}` status `{turn.get('status')}` "
                f"job `{turn.get('job_id')}`"
            )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_chat_session_resume_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    target = report.get("target_turn") or {}
    retry = report.get("retry") or {}
    lines = [
        "# ChatP2P Chat Session Resume",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Session: `{report.get('session_id')}`",
        f"- Target turn: `{target.get('turn_id')}`",
        f"- Retry turn: `{retry.get('turn_id')}`",
        f"- Retry created: `{retry.get('created')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
    ]
    if retry.get("turn"):
        turn = retry["turn"]
        lines.extend(
            [
                "## Retry",
                "",
                f"- Status: `{turn.get('status')}`",
                f"- Job id: `{turn.get('job_id')}`",
                f"- Requester balance after: `{turn.get('requester_balance_after')}`",
                "",
            ]
        )
    if report.get("warnings"):
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def _validate_config(config: ChatSessionConfig) -> None:
    if not config.session_id.strip():
        raise ValueError("--session-id must be non-empty")
    if _safe_session_id(config.session_id) != config.session_id:
        raise ValueError("--session-id may contain only letters, numbers, dot, underscore, and dash")
    if config.coordinator_url is not None and not config.coordinator_url.strip():
        raise ValueError("--coordinator must be non-empty")
    if not config.model.strip():
        raise ValueError("--model must be non-empty")
    if not config.prompt.strip():
        raise ValueError("--prompt must be non-empty")
    if not config.requester_account_id.strip():
        raise ValueError("--requester-account-id must be non-empty")
    if config.job_cost < 1:
        raise ValueError("--job-cost must be at least 1")
    if config.reward < 1:
        raise ValueError("--reward must be at least 1")
    if config.ttl_seconds < 1:
        raise ValueError("--ttl-seconds must be at least 1")
    if config.max_tokens is not None and config.max_tokens < 1:
        raise ValueError("--max-tokens must be at least 1")
    if config.temperature is not None and not 0 <= config.temperature <= 2:
        raise ValueError("--temperature must be between 0 and 2")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.client_timeout_seconds <= 0:
        raise ValueError("--client-timeout-seconds must be greater than 0")
    if config.max_context_turns < 0:
        raise ValueError("--max-context-turns must be at least 0")


def _validate_status_config(config: ChatSessionStatusConfig) -> None:
    if not config.session_id.strip():
        raise ValueError("--session-id must be non-empty")
    if _safe_session_id(config.session_id) != config.session_id:
        raise ValueError("--session-id may contain only letters, numbers, dot, underscore, and dash")


def _validate_resume_config(config: ChatSessionResumeConfig) -> None:
    if not config.session_id.strip():
        raise ValueError("--session-id must be non-empty")
    if _safe_session_id(config.session_id) != config.session_id:
        raise ValueError("--session-id may contain only letters, numbers, dot, underscore, and dash")
    if config.turn_id is not None and not config.turn_id.strip():
        raise ValueError("--turn-id must be non-empty")
    if config.coordinator_url is not None and not config.coordinator_url.strip():
        raise ValueError("--coordinator must be non-empty")
    if config.model is not None and not config.model.strip():
        raise ValueError("--model must be non-empty")
    if config.requester_account_id is not None and not config.requester_account_id.strip():
        raise ValueError("--requester-account-id must be non-empty")
    if config.job_cost is not None and config.job_cost < 1:
        raise ValueError("--job-cost must be at least 1")
    if config.reward is not None and config.reward < 1:
        raise ValueError("--reward must be at least 1")
    if config.ttl_seconds is not None and config.ttl_seconds < 1:
        raise ValueError("--ttl-seconds must be at least 1")
    if config.max_tokens is not None and config.max_tokens < 1:
        raise ValueError("--max-tokens must be at least 1")
    if config.temperature is not None and not 0 <= config.temperature <= 2:
        raise ValueError("--temperature must be between 0 and 2")
    if config.timeout_seconds is not None and config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval is not None and config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.client_timeout_seconds is not None and config.client_timeout_seconds <= 0:
        raise ValueError("--client-timeout-seconds must be greater than 0")
    if config.max_context_turns is not None and config.max_context_turns < 0:
        raise ValueError("--max-context-turns must be at least 0")


def _validate_sync_config(config: ChatSessionSyncConfig) -> None:
    if not config.session_id.strip():
        raise ValueError("--session-id must be non-empty")
    if _safe_session_id(config.session_id) != config.session_id:
        raise ValueError("--session-id may contain only letters, numbers, dot, underscore, and dash")
    if config.coordinator_url is not None and not config.coordinator_url.strip():
        raise ValueError("--coordinator must be non-empty")
    if config.client_timeout_seconds <= 0:
        raise ValueError("--client-timeout-seconds must be greater than 0")


def _load_existing_session(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = read_json_file(path, description="chat session file")
    if not isinstance(data, dict):
        raise ValueError("chat session file must be a JSON object")
    if data.get("schema") != CHAT_SESSION_REPORT_SCHEMA:
        raise ValueError(f"chat session schema must be {CHAT_SESSION_REPORT_SCHEMA!r}")
    return data


def _require_session(*, session_path: Path, session_id: str) -> dict[str, Any]:
    session = _load_existing_session(session_path)
    if session is None:
        raise ValueError(f"chat session does not exist at {session_path}")
    if session.get("session_id") != session_id:
        raise ValueError("existing chat session id does not match --session-id")
    return session


def _session_paths(out_dir: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path, Path, Path, Path]:
    return (
        out_dir / "chat-session.json",
        out_dir / "chat-session.md",
        out_dir / "chat-session-status.json",
        out_dir / "chat-session-status.md",
        out_dir / "chat-session-resume.json",
        out_dir / "chat-session-resume.md",
        out_dir / "chat-session-sync.json",
        out_dir / "chat-session-sync.md",
        out_dir / "chat-session-continue.json",
        out_dir / "chat-session-continue.md",
    )


def _base_session(*, config: ChatSessionConfig, existing: dict[str, Any] | None, now: str) -> dict[str, Any]:
    if existing is not None:
        if existing.get("session_id") != config.session_id:
            raise ValueError("existing chat session id does not match --session-id")
        session = dict(existing)
        session["config"] = _safe_config(config)
        return session
    return {
        "schema": CHAT_SESSION_REPORT_SCHEMA,
        "ok": True,
        "status": "pass",
        "session_id": config.session_id,
        "title": config.title or config.session_id,
        "created_at": now,
        "updated_at": now,
        "config": _safe_config(config),
        "turns": [],
        "summary": {},
        "errors": [],
    }


def _safe_config(config: ChatSessionConfig) -> dict[str, Any]:
    return {
        "out_dir": str(config.out_dir.expanduser().resolve()),
        "session_id": config.session_id,
        "coordinator": config.coordinator_url,
        "invite_path": str(config.invite_path.expanduser().resolve()) if config.invite_path else None,
        "auth": {"admission_token_present": bool(config.admission_token)},
        "model": config.model,
        "system": config.system,
        "requester_account_id": config.requester_account_id,
        "job_cost": config.job_cost,
        "reward": config.reward,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "ttl_seconds": config.ttl_seconds,
        "timeout_seconds": config.timeout_seconds,
        "poll_interval": config.poll_interval,
        "no_wait": config.no_wait,
        "max_context_turns": config.max_context_turns,
        "remote_side_effect": "create_funded_chat_job",
    }


def _context_messages(turns: list[dict[str, Any]], *, max_turns: int) -> list[dict[str, str]]:
    if max_turns == 0:
        return []
    verified_turns = [
        turn for turn in turns
        if turn.get("status") == "pass" and turn.get("prompt") and turn.get("answer")
    ]
    messages: list[dict[str, str]] = []
    for turn in verified_turns[-max_turns:]:
        messages.append({"role": "user", "content": str(turn["prompt"])})
        messages.append({"role": "assistant", "content": str(turn["answer"])})
    return messages


def _turn_from_ask_report(
    *,
    turn_id: str,
    turn_index: int,
    prompt: str,
    context_message_count: int,
    ask_report: dict[str, Any],
) -> dict[str, Any]:
    summary = ask_report.get("summary") or {}
    return {
        "turn_id": turn_id,
        "turn_index": turn_index,
        "created_at": ask_report.get("generated_at"),
        "ok": bool(ask_report.get("ok")),
        "status": ask_report.get("status"),
        "prompt": prompt,
        "answer": summary.get("answer"),
        "model": summary.get("model"),
        "job_id": summary.get("job_id"),
        "job_status": summary.get("job_status"),
        "worker_node_id": summary.get("worker_node_id"),
        "requester_balance_after": summary.get("requester_balance_after"),
        "worker_balance_after": summary.get("worker_balance_after"),
        "recommended_next_action": summary.get("recommended_next_action"),
        "context_message_count": context_message_count,
        "artifacts": ask_report.get("artifacts") or {},
        "errors": ask_report.get("errors") or [],
    }


def _session_summary(*, turns: list[dict[str, Any]], latest_turn: dict[str, Any]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for turn in turns:
        status = str(turn.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    latest_status = str(latest_turn.get("status") or "unknown")
    status = latest_status if latest_status in {"pass", "submitted"} else "fail"
    return {
        "status": status,
        "turn_count": len(turns),
        "completed_turns": status_counts.get("pass", 0),
        "submitted_turns": status_counts.get("submitted", 0),
        "failed_turns": status_counts.get("fail", 0),
        "status_counts": status_counts,
        "latest_turn": {
            "turn_id": latest_turn.get("turn_id"),
            "status": latest_turn.get("status"),
            "job_id": latest_turn.get("job_id"),
            "answer": latest_turn.get("answer"),
            "requester_balance_after": latest_turn.get("requester_balance_after"),
            "recommended_next_action": latest_turn.get("recommended_next_action"),
        },
        "recommended_next_action": _recommended_next_action(latest_turn),
    }


def _recommended_next_action(latest_turn: dict[str, Any]) -> str:
    latest_action = str(latest_turn.get("recommended_next_action") or "")
    if latest_turn.get("status") == "pass":
        return "continue_chat_session"
    if latest_turn.get("status") == "submitted":
        return "wait_for_worker_result"
    return latest_action or "inspect_chat_session_report"


def _turn_report_summary(turn: dict[str, Any]) -> dict[str, Any]:
    raw_path = (turn.get("artifacts") or {}).get("json")
    if not raw_path:
        return {"exists": False, "status": "missing", "path": None}
    path = Path(str(raw_path))
    if not path.exists():
        return {"exists": False, "status": "missing", "path": str(path)}
    try:
        data = read_json_file(path, description="chat ask turn report")
    except (OSError, ValueError) as exc:
        return {"exists": True, "status": "unreadable", "path": str(path), "error": str(exc)}
    if not isinstance(data, dict):
        return {"exists": True, "status": "invalid", "path": str(path)}
    summary = data.get("summary") or {}
    return {
        "exists": True,
        "status": str(data.get("status") or "unknown"),
        "path": str(path),
        "job_id": summary.get("job_id"),
        "job_status": summary.get("job_status"),
        "answer_present": bool(summary.get("answer")),
    }


def _status_report_summary(
    *,
    turns: list[dict[str, Any]],
    turn_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for turn in turns:
        status = str(turn.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    failed_turns = [turn for turn in turns if turn.get("status") == "fail"]
    submitted_turns = [turn for turn in turns if turn.get("status") == "submitted"]
    latest_turn = turns[-1] if turns else {}
    artifact_mismatches = [
        {
            "turn_id": turn.get("turn_id"),
            "session_status": turn.get("status"),
            "artifact_status": report.get("status"),
        }
        for turn, report in zip(turns, turn_reports, strict=False)
        if report.get("exists") and report.get("status") not in {turn.get("status"), "unknown"}
    ]
    status = "warn" if failed_turns or submitted_turns or artifact_mismatches else "pass"
    return {
        "status": status,
        "turn_count": len(turns),
        "completed_turns": status_counts.get("pass", 0),
        "submitted_turns": len(submitted_turns),
        "failed_turns": len(failed_turns),
        "status_counts": status_counts,
        "retryable_turn_count": len(failed_turns),
        "unresolved_turn_count": len(failed_turns) + len(submitted_turns),
        "artifact_mismatch_count": len(artifact_mismatches),
        "artifact_mismatches": artifact_mismatches,
        "latest_turn": {
            "turn_id": latest_turn.get("turn_id"),
            "status": latest_turn.get("status"),
            "job_id": latest_turn.get("job_id"),
            "recommended_next_action": latest_turn.get("recommended_next_action"),
        },
        "recommended_next_action": _status_recommended_action(
            failed_turns=failed_turns,
            submitted_turns=submitted_turns,
            artifact_mismatches=artifact_mismatches,
        ),
    }


def _status_turns(
    *,
    turns: list[dict[str, Any]],
    turn_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for turn, report in zip(turns, turn_reports, strict=False):
        prompt = str(turn.get("prompt") or "")
        rows.append(
            {
                "turn_id": turn.get("turn_id"),
                "turn_index": turn.get("turn_index"),
                "status": turn.get("status"),
                "job_id": turn.get("job_id"),
                "prompt_preview": prompt[:120],
                "artifact_status": report.get("status"),
                "artifact_path": report.get("path"),
                "retry_of_turn_id": turn.get("retry_of_turn_id"),
                "retry_attempt": turn.get("retry_attempt"),
            }
        )
    return rows


def _status_recommended_action(
    *,
    failed_turns: list[dict[str, Any]],
    submitted_turns: list[dict[str, Any]],
    artifact_mismatches: list[dict[str, Any]],
) -> str:
    if artifact_mismatches:
        return "inspect_turn_artifacts"
    if any(turn.get("job_id") for turn in failed_turns):
        return "sync_session_then_resume_failed_turn"
    if any(turn.get("job_id") for turn in submitted_turns):
        return "sync_session"
    if failed_turns:
        return "resume_failed_turn"
    if submitted_turns:
        return "wait_or_resume_submitted_turn"
    return "continue_chat_session"


def _continue_session_config(config: ChatSessionContinueConfig) -> ChatSessionConfig:
    return ChatSessionConfig(
        out_dir=config.out_dir,
        session_id=config.session_id,
        title=config.title,
        coordinator_url=config.coordinator_url,
        invite_path=config.invite_path,
        admission_token=config.admission_token,
        model=config.model,
        prompt=config.prompt,
        system=config.system,
        requester_account_id=config.requester_account_id,
        job_cost=config.job_cost,
        reward=config.reward,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        ttl_seconds=config.ttl_seconds,
        timeout_seconds=config.timeout_seconds,
        poll_interval=config.poll_interval,
        no_wait=config.no_wait,
        client_timeout_seconds=config.client_timeout_seconds,
        max_context_turns=config.max_context_turns,
    )


def _continue_status_needs_sync(report: dict[str, Any]) -> bool:
    return any(
        turn.get("job_id") and turn.get("status") in {"fail", "submitted"}
        for turn in report.get("turns") or []
    )


def _continue_unresolved_turns(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        {
            "turn_id": turn.get("turn_id"),
            "status": turn.get("status"),
            "job_id": turn.get("job_id"),
            "recommended_next_action": turn.get("recommended_next_action"),
        }
        for turn in (report or {}).get("turns", [])
        if turn.get("status") in {"fail", "submitted"}
    ]


def _continue_report_status(
    *,
    blocked_reason: str | None,
    session_report: dict[str, Any] | None,
    errors: list[str],
) -> str:
    if blocked_reason:
        return "blocked"
    if errors:
        return "fail"
    return str((session_report or {}).get("status") or "fail")


def _continue_recommended_next_action(
    *,
    blocked_reason: str | None,
    session_report: dict[str, Any] | None,
    status_report: dict[str, Any] | None,
    sync_report: dict[str, Any] | None,
    errors: list[str],
) -> str:
    if blocked_reason == "sync_failed":
        return "check_coordinator_reachability"
    if blocked_reason == "unresolved_session_turns":
        summary = (status_report or {}).get("summary") or {}
        action = str(summary.get("recommended_next_action") or "")
        if action == "sync_session_then_resume_failed_turn":
            return "run_session_sync_then_resume_dry_run"
        if action == "resume_failed_turn":
            return "run_session_resume_dry_run"
        if action in {"sync_session", "wait_or_resume_submitted_turn"}:
            return "wait_for_worker_result"
        return action or "inspect_chat_session_status"
    if errors:
        return "inspect_chat_session_continue_report"
    if sync_report and ((sync_report.get("summary") or {}).get("recommended_next_action") == "wait_for_worker_result"):
        return "wait_for_worker_result"
    return ((session_report or {}).get("summary") or {}).get("recommended_next_action") or "continue_chat_session"


def _continue_embedded_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "schema": report.get("schema"),
        "ok": report.get("ok"),
        "status": report.get("status"),
        "summary": report.get("summary"),
        "errors": report.get("errors") or [],
        "artifacts": report.get("artifacts") or {},
    }


def _continue_step(name: str, status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "ok": status in {"pass", "submitted", "skipped"}, "status": status, "details": details or {}}


def _resolve_sync_connection(*, config: ChatSessionSyncConfig, session: dict[str, Any]) -> dict[str, Any]:
    invite = load_alpha_invite(config.invite_path.expanduser()) if config.invite_path else None
    stored = session.get("config") or {}
    coordinator_url = (
        config.coordinator_url
        or (invite.coordinator if invite else None)
        or stored.get("coordinator")
        or DEFAULT_COORDINATOR_URL
    )
    token = config.admission_token or (invite.admission_token if invite else None)
    return {
        "coordinator_url": str(coordinator_url).rstrip("/"),
        "token": token,
    }


def _sync_turn_from_snapshot(
    *,
    turn: dict[str, Any],
    snapshot: dict[str, Any],
    requester_account_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    job_id = turn.get("job_id")
    if not job_id:
        return dict(turn), None
    job = _snapshot_job(snapshot, str(job_id))
    result = _snapshot_result(snapshot, str(job_id))
    previous_status = str(turn.get("status") or "unknown")
    previous_job_status = turn.get("job_status")
    new_turn = dict(turn)
    job_status = str((job or {}).get("status") or previous_job_status or "unknown")

    if result and (job is None or job_status == "verified"):
        output = result.get("output") or {}
        balances = ((snapshot.get("credit_ledger") or {}).get("summary") or {}).get("balances") or {}
        worker_id = result.get("node_id")
        new_turn.update(
            {
                "ok": True,
                "status": "pass",
                "answer": output.get("answer"),
                "model": output.get("model") or turn.get("model"),
                "job_status": "verified",
                "worker_node_id": worker_id,
                "requester_balance_after": balances.get(requester_account_id) if requester_account_id else None,
                "worker_balance_after": balances.get(worker_id) if worker_id else None,
                "recommended_next_action": "continue_chat_session",
                "errors": [],
            }
        )
    elif job_status in {"queued", "leased", "pending"}:
        new_turn.update(
            {
                "ok": True,
                "status": "submitted",
                "job_status": job_status,
                "recommended_next_action": "wait_for_worker_result",
                "errors": [],
            }
        )
    elif job_status in {"expired", "disputed"}:
        new_turn.update(
            {
                "ok": False,
                "status": "fail",
                "job_status": job_status,
                "recommended_next_action": "resume_failed_turn",
                "errors": [f"chat job is {job_status}"],
            }
        )

    changed = _sync_turn_changed(turn, new_turn)
    return new_turn, {
        "turn_id": turn.get("turn_id"),
        "job_id": job_id,
        "previous_status": previous_status,
        "previous_job_status": previous_job_status,
        "synced_status": new_turn.get("status"),
        "synced_job_status": new_turn.get("job_status"),
        "result_found": result is not None,
        "job_found": job is not None,
        "changed": changed,
    }


def _sync_turn_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    fields = {
        "ok",
        "status",
        "answer",
        "model",
        "job_status",
        "worker_node_id",
        "requester_balance_after",
        "worker_balance_after",
        "recommended_next_action",
        "errors",
    }
    return any(before.get(field) != after.get(field) for field in fields)


def _sync_unresolved_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "turn_id": turn.get("turn_id"),
            "status": turn.get("status"),
            "job_id": turn.get("job_id"),
            "job_status": turn.get("job_status"),
            "recommended_next_action": turn.get("recommended_next_action"),
        }
        for turn in turns
        if turn.get("status") in {"fail", "submitted"}
    ]


def _sync_status(
    *,
    errors: list[str],
    updates: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    dry_run: bool,
) -> str:
    if errors:
        return "fail"
    if dry_run and any(update["changed"] for update in updates):
        return "dry_run"
    if unresolved:
        return "warn"
    return "pass"


def _sync_recommended_next_action(
    *,
    status: str,
    updates: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    errors: list[str],
    dry_run: bool,
) -> str:
    if errors:
        return "check_coordinator_reachability"
    if dry_run and any(update["changed"] for update in updates):
        return "rerun_session_sync_without_dry_run"
    if any(turn.get("status") == "fail" for turn in unresolved):
        return "resume_failed_turn"
    if any(turn.get("status") == "submitted" for turn in unresolved):
        return "wait_for_worker_result"
    if any(update["changed"] for update in updates) or status == "pass":
        return "continue_chat_session"
    return "inspect_chat_session_sync_report"


def _snapshot_job(snapshot: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    return next((job for job in snapshot.get("jobs", []) if job.get("job_id") == job_id), None)


def _snapshot_result(snapshot: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    return next((result for result in snapshot.get("results", []) if result.get("job_id") == job_id), None)


def _format_sync_exception(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _select_resume_turn(
    *,
    turns: list[dict[str, Any]],
    turn_id: str | None,
    include_submitted: bool,
) -> dict[str, Any] | None:
    allowed_statuses = {"fail"}
    if include_submitted:
        allowed_statuses.add("submitted")
    if turn_id is not None:
        target = next((turn for turn in turns if turn.get("turn_id") == turn_id), None)
        if target is None:
            raise ValueError(f"turn not found: {turn_id}")
        if target.get("status") not in allowed_statuses:
            raise ValueError(
                f"turn {turn_id} has status {target.get('status')!r}; "
                "resume supports failed turns by default and submitted turns with --include-submitted"
            )
        return target
    return next(
        (
            turn for turn in reversed(turns)
            if turn.get("status") in allowed_statuses and isinstance(turn.get("prompt"), str)
        ),
        None,
    )


def _effective_resume_config(
    *,
    config: ChatSessionResumeConfig,
    session: dict[str, Any],
    prompt: str,
) -> ChatSessionConfig:
    stored = session.get("config") or {}
    return ChatSessionConfig(
        out_dir=config.out_dir,
        session_id=config.session_id,
        title=session.get("title"),
        coordinator_url=config.coordinator_url if config.coordinator_url is not None else stored.get("coordinator"),
        invite_path=(
            config.invite_path
            if config.invite_path is not None
            else _optional_path(stored.get("invite_path"))
        ),
        admission_token=config.admission_token,
        model=config.model or str(stored.get("model") or "tiny-test-model"),
        prompt=prompt,
        system=config.system if config.system is not None else stored.get("system"),
        requester_account_id=(
            config.requester_account_id
            or str(stored.get("requester_account_id") or "requester_demo")
        ),
        job_cost=config.job_cost if config.job_cost is not None else int(stored.get("job_cost") or 1),
        reward=config.reward if config.reward is not None else int(stored.get("reward") or 1),
        temperature=(
            config.temperature
            if config.temperature is not None
            else _optional_float(stored.get("temperature"))
        ),
        max_tokens=config.max_tokens if config.max_tokens is not None else _optional_int(stored.get("max_tokens")),
        ttl_seconds=config.ttl_seconds if config.ttl_seconds is not None else int(stored.get("ttl_seconds") or 300),
        timeout_seconds=(
            config.timeout_seconds
            if config.timeout_seconds is not None
            else float(stored.get("timeout_seconds") or 60.0)
        ),
        poll_interval=(
            config.poll_interval
            if config.poll_interval is not None
            else float(stored.get("poll_interval") or 0.5)
        ),
        no_wait=config.no_wait,
        client_timeout_seconds=(
            config.client_timeout_seconds
            if config.client_timeout_seconds is not None
            else 10.0
        ),
        max_context_turns=(
            config.max_context_turns
            if config.max_context_turns is not None
            else int(stored.get("max_context_turns") or DEFAULT_MAX_CONTEXT_TURNS)
        ),
    )


def _retry_attempt(*, turns: list[dict[str, Any]], target_turn_id: str) -> int:
    return 1 + sum(1 for turn in turns if turn.get("retry_of_turn_id") == target_turn_id)


def _resume_target_summary(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": turn.get("turn_id"),
        "turn_index": turn.get("turn_index"),
        "status": turn.get("status"),
        "job_id": turn.get("job_id"),
        "prompt_preview": str(turn.get("prompt") or "")[:120],
        "recommended_next_action": turn.get("recommended_next_action"),
    }


def _resume_summary(
    *,
    status: str,
    target_turn: dict[str, Any],
    appended_turn: dict[str, Any] | None,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        recommended = "rerun_session_resume_without_dry_run"
    elif appended_turn is None:
        recommended = "continue_chat_session"
    elif appended_turn.get("status") == "pass":
        recommended = "continue_chat_session"
    elif appended_turn.get("status") == "submitted":
        recommended = "wait_for_worker_result"
    else:
        recommended = appended_turn.get("recommended_next_action") or "inspect_chat_session_resume_report"
    return {
        "status": status,
        "target_turn_id": target_turn.get("turn_id"),
        "retry_turn_id": (appended_turn or {}).get("turn_id"),
        "retry_created": appended_turn is not None,
        "recommended_next_action": recommended,
    }


def _resume_noop_report(
    *,
    config: ChatSessionResumeConfig,
    session_path: Path,
    resume_path: Path,
    resume_markdown_path: Path,
    started_at: float,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema": CHAT_SESSION_RESUME_REPORT_SCHEMA,
        "ok": True,
        "status": "pass",
        "dry_run": config.dry_run,
        "generated_at": generated_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "session_id": config.session_id,
        "target_turn": None,
        "retry": {"created": False, "turn_id": None, "retry_attempt": None, "turn": None},
        "summary": {
            "status": "pass",
            "target_turn_id": None,
            "retry_turn_id": None,
            "retry_created": False,
            "recommended_next_action": "continue_chat_session",
        },
        "warnings": [],
        "errors": [],
        "artifacts": {
            "json": str(resume_path),
            "markdown": str(resume_markdown_path),
            "session_json": str(session_path),
        },
    }


def _write_resume_report(report: dict[str, Any], *, resume_path: Path, resume_markdown_path: Path) -> None:
    resume_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    resume_markdown_path.write_text(format_chat_session_resume_markdown(report), encoding="utf-8")


def _optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text) if text else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _safe_session_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value.strip())

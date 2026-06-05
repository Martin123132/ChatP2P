"""Persistent requester chat sessions backed by funded ChatP2P jobs."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chat_request import ChatAskConfig, run_chat_ask
from .jsonio import read_json_file


CHAT_SESSION_REPORT_SCHEMA = "chatp2p.chat-session-report.v1"
CHAT_SESSION_STATUS_REPORT_SCHEMA = "chatp2p.chat-session-status-report.v1"
CHAT_SESSION_RESUME_REPORT_SCHEMA = "chatp2p.chat-session-resume-report.v1"
DEFAULT_SESSION_ID = "default"
DEFAULT_MAX_CONTEXT_TURNS = 8


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
    session_path, _, status_path, status_markdown_path, _, _ = _session_paths(out_dir)
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


def run_chat_session_resume(config: ChatSessionResumeConfig) -> dict[str, Any]:
    """Append a retry turn for the latest failed/submitted session turn."""

    _validate_resume_config(config)
    started_at = time.time()
    now = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    session_path, markdown_path, _, _, resume_path, resume_markdown_path = _session_paths(out_dir)
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


def _session_paths(out_dir: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    return (
        out_dir / "chat-session.json",
        out_dir / "chat-session.md",
        out_dir / "chat-session-status.json",
        out_dir / "chat-session-status.md",
        out_dir / "chat-session-resume.json",
        out_dir / "chat-session-resume.md",
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
    if failed_turns:
        return "resume_failed_turn"
    if submitted_turns:
        return "wait_or_resume_submitted_turn"
    return "continue_chat_session"


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

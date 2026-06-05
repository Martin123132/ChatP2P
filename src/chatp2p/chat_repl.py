"""Interactive terminal chat loop backed by safe ChatP2P session commands."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .chat_session import (
    DEFAULT_MAX_CONTEXT_TURNS,
    ChatSessionContinueConfig,
    ChatSessionResumeConfig,
    ChatSessionStatusConfig,
    ChatSessionSyncConfig,
    format_chat_session_resume_summary,
    format_chat_session_status_summary,
    format_chat_session_sync_summary,
    run_chat_session_continue,
    run_chat_session_resume,
    run_chat_session_status,
    run_chat_session_sync,
)


CHAT_REPL_REPORT_SCHEMA = "chatp2p.chat-repl-report.v1"
InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


@dataclass(frozen=True)
class ChatReplConfig:
    out_dir: Path = Path(".mesh/chat-session")
    session_id: str = "default"
    title: str | None = None
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    model: str = "tiny-test-model"
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
    prompt_label: str = "you> "


def run_chat_repl(
    config: ChatReplConfig,
    *,
    input_func: InputFunc = input,
    output_func: OutputFunc = print,
) -> dict[str, Any]:
    """Run an interactive prompt where each message uses safe chat continue."""

    _validate_config(config)
    started_at = time.time()
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "chat-repl.json"
    markdown_path = out_dir / "chat-repl.md"
    events: list[dict[str, Any]] = []

    output_func("ChatP2P REPL. Type /help for commands, /quit to exit.")
    while True:
        try:
            raw = input_func(config.prompt_label)
        except EOFError:
            events.append(_event("exit", status="pass", reason="eof"))
            break
        except KeyboardInterrupt:
            output_func("")
            events.append(_event("exit", status="pass", reason="keyboard_interrupt"))
            break

        text = raw.strip()
        if not text:
            continue
        lower = text.lower()
        if lower in {"/quit", "/exit"}:
            events.append(_event("exit", status="pass", command=lower, reason="user_exit"))
            _write_report(
                _report(
                    config=config,
                    events=events,
                    started_at=started_at,
                    generated_at=generated_at,
                    report_path=report_path,
                    markdown_path=markdown_path,
                )
            )
            output_func("bye")
            break
        if lower == "/help":
            event = _event("command", status="pass", command=lower, summary={"recommended_next_action": "continue_chat"})
            events.append(event)
            _write_report(
                _report(
                    config=config,
                    events=events,
                    started_at=started_at,
                    generated_at=generated_at,
                    report_path=report_path,
                    markdown_path=markdown_path,
                )
            )
            output_func("commands: /status, /sync, /resume-dry-run, /quit")
            continue

        event = _handle_input(config=config, text=text, output_func=output_func)
        events.append(event)
        _write_report(
            _report(
                config=config,
                events=events,
                started_at=started_at,
                generated_at=generated_at,
                report_path=report_path,
                markdown_path=markdown_path,
            )
        )

    report = _report(
        config=config,
        events=events,
        started_at=started_at,
        generated_at=generated_at,
        report_path=report_path,
        markdown_path=markdown_path,
    )
    _write_report(report)
    return report


def format_chat_repl_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    latest = summary.get("latest_event") or {}
    lines = [
        f"Chat REPL: {str(report.get('status', 'unknown')).upper()}",
        f"Session: {report.get('session_id')}",
        f"Messages: {summary.get('messages')}",
        f"Commands: {summary.get('commands')}",
        f"Blocked: {summary.get('blocked_messages')}",
        f"Latest: {latest.get('kind')} {latest.get('status')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    return "\n".join(lines)


def format_chat_repl_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P REPL",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Session: `{report.get('session_id')}`",
        f"- Messages: `{summary.get('messages')}`",
        f"- Commands: `{summary.get('commands')}`",
        f"- Blocked messages: `{summary.get('blocked_messages')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Events",
        "",
    ]
    for event in report.get("events") or []:
        lines.append(
            f"- `{event.get('event_id')}` `{event.get('kind')}` "
            f"status `{event.get('status')}` next `{((event.get('summary') or {}).get('recommended_next_action'))}`"
        )
    return "\n".join(lines)


def _handle_input(*, config: ChatReplConfig, text: str, output_func: OutputFunc) -> dict[str, Any]:
    lower = text.lower()
    if lower == "/status":
        return _run_status_command(config=config, output_func=output_func)
    if lower == "/sync":
        return _run_sync_command(config=config, output_func=output_func)
    if lower == "/resume-dry-run":
        return _run_resume_dry_run_command(config=config, output_func=output_func)
    if lower.startswith("/"):
        output_func(f"unknown command: {text}")
        return _event(
            "command",
            status="warn",
            command=text,
            warnings=[f"unknown command: {text}"],
            summary={"recommended_next_action": "type_help"},
        )
    return _run_message(config=config, prompt=text, output_func=output_func)


def _run_message(*, config: ChatReplConfig, prompt: str, output_func: OutputFunc) -> dict[str, Any]:
    try:
        report = run_chat_session_continue(_continue_config(config, prompt=prompt))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        output_func(f"error> {error}")
        return _event("message", status="fail", prompt_preview=prompt[:120], errors=[error])

    summary = report.get("summary") or {}
    latest = summary.get("latest_turn") or {}
    answer = latest.get("answer")
    if answer:
        output_func(f"assistant> {answer}")
    else:
        output_func(f"status> {report.get('status')} next={summary.get('recommended_next_action')}")
    return _event_from_report(
        kind="message",
        command=None,
        prompt_preview=prompt[:120],
        report=report,
    )


def _run_status_command(*, config: ChatReplConfig, output_func: OutputFunc) -> dict[str, Any]:
    try:
        report = run_chat_session_status(ChatSessionStatusConfig(out_dir=config.out_dir, session_id=config.session_id))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        output_func(f"status> {error}")
        return _event("command", status="fail", command="/status", errors=[error])
    output_func(format_chat_session_status_summary(report))
    return _event_from_report(kind="command", command="/status", prompt_preview=None, report=report)


def _run_sync_command(*, config: ChatReplConfig, output_func: OutputFunc) -> dict[str, Any]:
    try:
        report = run_chat_session_sync(
            ChatSessionSyncConfig(
                out_dir=config.out_dir,
                session_id=config.session_id,
                coordinator_url=config.coordinator_url,
                invite_path=config.invite_path,
                admission_token=config.admission_token,
                client_timeout_seconds=config.client_timeout_seconds,
            )
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        output_func(f"sync> {error}")
        return _event("command", status="fail", command="/sync", errors=[error])
    output_func(format_chat_session_sync_summary(report))
    return _event_from_report(kind="command", command="/sync", prompt_preview=None, report=report)


def _run_resume_dry_run_command(*, config: ChatReplConfig, output_func: OutputFunc) -> dict[str, Any]:
    try:
        report = run_chat_session_resume(
            ChatSessionResumeConfig(
                out_dir=config.out_dir,
                session_id=config.session_id,
                dry_run=True,
                coordinator_url=config.coordinator_url,
                invite_path=config.invite_path,
                admission_token=config.admission_token,
                model=config.model,
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
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        output_func(f"resume> {error}")
        return _event("command", status="fail", command="/resume-dry-run", errors=[error])
    output_func(format_chat_session_resume_summary(report))
    return _event_from_report(kind="command", command="/resume-dry-run", prompt_preview=None, report=report)


def _continue_config(config: ChatReplConfig, *, prompt: str) -> ChatSessionContinueConfig:
    return ChatSessionContinueConfig(
        out_dir=config.out_dir,
        session_id=config.session_id,
        title=config.title,
        coordinator_url=config.coordinator_url,
        invite_path=config.invite_path,
        admission_token=config.admission_token,
        model=config.model,
        prompt=prompt,
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


def _report(
    *,
    config: ChatReplConfig,
    events: list[dict[str, Any]],
    started_at: float,
    generated_at: str,
    report_path: Path,
    markdown_path: Path,
) -> dict[str, Any]:
    status = _report_status(events)
    summary = _report_summary(events)
    return {
        "schema": CHAT_REPL_REPORT_SCHEMA,
        "ok": status in {"pass", "warn"},
        "status": status,
        "generated_at": generated_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "session_id": config.session_id,
        "config": _safe_config(config),
        "summary": summary,
        "events": events,
        "warnings": [
            warning
            for event in events
            for warning in (event.get("warnings") or [])
        ],
        "errors": [
            error
            for event in events
            for error in (event.get("errors") or [])
        ],
        "artifacts": {
            "json": str(report_path),
            "markdown": str(markdown_path),
            "session_json": str(config.out_dir.expanduser().resolve() / "chat-session.json"),
        },
    }


def _report_status(events: list[dict[str, Any]]) -> str:
    if any(event.get("status") == "fail" for event in events):
        return "fail"
    if any(event.get("status") in {"blocked", "submitted", "warn"} for event in events):
        return "warn"
    return "pass"


def _report_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    messages = [event for event in events if event.get("kind") == "message"]
    commands = [event for event in events if event.get("kind") == "command"]
    blocked = [event for event in messages if event.get("status") == "blocked"]
    submitted = [event for event in messages if event.get("status") == "submitted"]
    failed = [event for event in events if event.get("status") == "fail"]
    latest = events[-1] if events else {}
    latest_summary = latest.get("summary") or {}
    exit_events = [event for event in events if event.get("kind") == "exit"]
    return {
        "events": len(events),
        "messages": len(messages),
        "commands": len(commands),
        "blocked_messages": len(blocked),
        "submitted_messages": len(submitted),
        "failed_events": len(failed),
        "exit_reason": (exit_events[-1].get("reason") if exit_events else None),
        "latest_event": {
            "event_id": latest.get("event_id"),
            "kind": latest.get("kind"),
            "status": latest.get("status"),
        },
        "recommended_next_action": latest_summary.get("recommended_next_action") or "continue_chat",
    }


def _event_from_report(
    *,
    kind: str,
    command: str | None,
    prompt_preview: str | None,
    report: dict[str, Any],
) -> dict[str, Any]:
    summary = report.get("summary") or {}
    return _event(
        kind,
        status=str(report.get("status") or "unknown"),
        command=command,
        prompt_preview=prompt_preview,
        schema=report.get("schema"),
        summary=summary,
        artifacts=report.get("artifacts") or {},
        errors=report.get("errors") or [],
        warnings=report.get("warnings") or [],
    )


def _event(kind: str, *, status: str, **fields: Any) -> dict[str, Any]:
    event = {
        "event_id": "",
        "kind": kind,
        "status": status,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    event.update(fields)
    return event


def _write_report(report: dict[str, Any]) -> None:
    for index, event in enumerate(report.get("events") or [], start=1):
        if not event.get("event_id"):
            event["event_id"] = f"event-{index:04d}"
    artifacts = report["artifacts"]
    Path(artifacts["json"]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    Path(artifacts["markdown"]).write_text(format_chat_repl_markdown(report), encoding="utf-8")


def _safe_config(config: ChatReplConfig) -> dict[str, Any]:
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
        "remote_side_effect": "interactive_safe_chat_continue",
    }


def _validate_config(config: ChatReplConfig) -> None:
    if not config.session_id.strip():
        raise ValueError("--session-id must be non-empty")
    if not config.model.strip():
        raise ValueError("--model must be non-empty")
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

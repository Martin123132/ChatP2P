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


def _load_existing_session(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = read_json_file(path, description="chat session file")
    if not isinstance(data, dict):
        raise ValueError("chat session file must be a JSON object")
    if data.get("schema") != CHAT_SESSION_REPORT_SCHEMA:
        raise ValueError(f"chat session schema must be {CHAT_SESSION_REPORT_SCHEMA!r}")
    return data


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


def _safe_session_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value.strip())

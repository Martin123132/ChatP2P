"""Requester-side chat command for funded ChatP2P inference jobs."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from .alpha import load_alpha_invite
from .client import CoordinatorClient

CHAT_ASK_REPORT_SCHEMA = "chatp2p.chat-ask-report.v1"
DEFAULT_COORDINATOR_URL = "http://127.0.0.1:8765"


@dataclass(frozen=True)
class ChatAskConfig:
    out_dir: Path = Path(".mesh/chat-ask")
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    model: str = "tiny-test-model"
    prompt: str = "Explain ChatP2P in one sentence."
    system: str | None = "Be concise."
    context_messages: tuple[dict[str, str], ...] = ()
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


def run_chat_ask(config: ChatAskConfig) -> dict[str, Any]:
    """Create a funded chat job and optionally wait for the accepted answer."""

    _validate_config(config)
    started_at = time.time()
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    connection = _resolve_connection(config)
    client = CoordinatorClient(
        connection["coordinator_url"],
        admission_token=connection["token"],
        timeout_seconds=config.client_timeout_seconds,
    )
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    job_summary: dict[str, Any] | None = None
    result_summary: dict[str, Any] | None = None
    final_snapshot: dict[str, Any] | None = None
    created_job_id: str | None = None

    try:
        job = client.create_chat_job(
            model=config.model,
            messages=_messages_from_config(config),
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reward=config.reward,
            ttl_seconds=config.ttl_seconds,
            requester_account_id=config.requester_account_id,
            job_cost=config.job_cost,
        )
        created_job_id = job.job_id
        steps.append(
            _step(
                "create_funded_chat_job",
                "pass",
                {
                    "job_id": job.job_id,
                    "coordinator": connection["coordinator_url"],
                    "requester_account_id": config.requester_account_id,
                    "job_cost": config.job_cost,
                    "reward": config.reward,
                },
            )
        )

        if config.no_wait:
            final_snapshot = _safe_snapshot(client, errors=errors)
            job_summary = _find_job(final_snapshot, job.job_id) if final_snapshot else None
            steps.append(_step("wait_for_result", "skipped", {"reason": "--no-wait"}))
        else:
            job_summary, result_summary, final_snapshot = _wait_for_result(
                client,
                job_id=job.job_id,
                timeout_seconds=config.timeout_seconds,
                poll_interval=config.poll_interval,
            )
            steps.append(
                _step(
                    "wait_for_result",
                    "pass",
                    {
                        "job_id": job.job_id,
                        "job_status": job_summary.get("status"),
                        "node_id": result_summary.get("node_id"),
                    },
                )
            )
    except Exception as exc:
        errors.append(_format_exception(exc))
        steps.append(_step("chat_ask_error", "fail", {"error": errors[-1]}))
        final_snapshot = _safe_snapshot(client, errors=errors)
        if created_job_id and final_snapshot:
            job_summary = _find_job(final_snapshot, created_job_id)
            result_summary = _find_result(final_snapshot, created_job_id)

    status = _status(errors=errors, no_wait=config.no_wait, job_summary=job_summary, result_summary=result_summary)
    report = {
        "schema": CHAT_ASK_REPORT_SCHEMA,
        "ok": status in {"pass", "submitted"},
        "status": status,
        "generated_at": generated_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "out_dir": str(out_dir),
            "coordinator": connection["coordinator_url"],
            "invite_path": str(config.invite_path.expanduser().resolve()) if config.invite_path else None,
            "auth": {"token_present": bool(connection["token"])},
            "model": config.model,
            "context_message_count": len(config.context_messages),
            "requester_account_id": config.requester_account_id,
            "job_cost": config.job_cost,
            "reward": config.reward,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "ttl_seconds": config.ttl_seconds,
            "timeout_seconds": config.timeout_seconds,
            "poll_interval": config.poll_interval,
            "no_wait": config.no_wait,
            "remote_side_effect": "create_funded_chat_job",
        },
        "invite": connection["invite_summary"],
        "summary": _summary(
            status=status,
            config=config,
            job_summary=job_summary,
            result_summary=result_summary,
            snapshot=final_snapshot,
            errors=errors,
        ),
        "steps": steps,
        "job": job_summary,
        "result": result_summary,
        "final_status": (final_snapshot or {}).get("status"),
        "errors": errors,
        "artifacts": {
            "json": str(out_dir / "chat-ask.json"),
            "markdown": str(out_dir / "chat-ask.md"),
        },
    }
    _write_report(report)
    return report


def format_chat_ask_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Chat ask: {str(report.get('status', 'unknown')).upper()}",
        f"Coordinator: {(report.get('config') or {}).get('coordinator')}",
        f"Job: {summary.get('job_id')}",
        f"Job status: {summary.get('job_status')}",
        f"Requester balance: {summary.get('requester_balance_after')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    answer = summary.get("answer")
    if answer:
        lines.insert(5, f"Answer: {answer}")
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_chat_ask_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    config = report.get("config") or {}
    lines = [
        "# ChatP2P Chat Ask",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Coordinator: `{config.get('coordinator')}`",
        f"- Model: `{config.get('model')}`",
        f"- Requester account: `{config.get('requester_account_id')}`",
        f"- Job id: `{summary.get('job_id')}`",
        f"- Job status: `{summary.get('job_status')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Steps",
        "",
    ]
    for step in report.get("steps") or []:
        lines.append(f"- `{step.get('name')}`: `{step.get('status')}`")
    if summary.get("answer"):
        lines.extend(["", "## Answer", "", str(summary["answer"])])
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def _validate_config(config: ChatAskConfig) -> None:
    if config.coordinator_url is not None and not config.coordinator_url.strip():
        raise ValueError("--coordinator must be non-empty")
    if not config.model.strip():
        raise ValueError("--model must be non-empty")
    if not config.prompt.strip():
        raise ValueError("--prompt must be non-empty")
    for message in config.context_messages:
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("context message role must be system, user, or assistant")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("context message content must be non-empty")
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


def _resolve_connection(config: ChatAskConfig) -> dict[str, Any]:
    invite = load_alpha_invite(config.invite_path.expanduser()) if config.invite_path else None
    coordinator_url = config.coordinator_url or (invite.coordinator if invite else DEFAULT_COORDINATOR_URL)
    token = config.admission_token or (invite.admission_token if invite else None)
    return {
        "coordinator_url": coordinator_url.rstrip("/"),
        "token": token,
        "invite_summary": invite.public_summary() if invite else None,
    }


def _messages_from_config(config: ChatAskConfig) -> list[dict[str, str]]:
    messages = []
    if config.system and config.system.strip():
        messages.append({"role": "system", "content": config.system.strip()})
    messages.extend(
        {"role": message["role"].strip(), "content": message["content"].strip()}
        for message in config.context_messages
    )
    messages.append({"role": "user", "content": config.prompt.strip()})
    return messages


def _wait_for_result(
    client: CoordinatorClient,
    *,
    job_id: str,
    timeout_seconds: float,
    poll_interval: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    deadline = time.time() + timeout_seconds
    last_job: dict[str, Any] | None = None
    while time.time() <= deadline:
        snapshot = client.snapshot()
        last_job = _find_job(snapshot, job_id)
        result = _find_result(snapshot, job_id)
        if last_job and last_job.get("status") == "verified" and result:
            return last_job, result, snapshot
        if last_job and last_job.get("status") in {"disputed", "expired"}:
            raise RuntimeError(f"chat job became {last_job.get('status')}")
        time.sleep(poll_interval)
    raise TimeoutError(f"chat job did not verify before timeout: {job_id}; last status={last_job}")


def _safe_snapshot(client: CoordinatorClient, *, errors: list[str]) -> dict[str, Any] | None:
    try:
        return client.snapshot()
    except Exception as exc:
        errors.append(f"snapshot_after_error: {_format_exception(exc)}")
        return None


def _status(
    *,
    errors: list[str],
    no_wait: bool,
    job_summary: dict[str, Any] | None,
    result_summary: dict[str, Any] | None,
) -> str:
    if errors:
        return "fail"
    if no_wait and job_summary:
        return "submitted"
    if job_summary and job_summary.get("status") == "verified" and result_summary:
        return "pass"
    return "fail"


def _summary(
    *,
    status: str,
    config: ChatAskConfig,
    job_summary: dict[str, Any] | None,
    result_summary: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, Any]:
    balances = ((snapshot or {}).get("credit_ledger") or {}).get("summary", {}).get("balances", {})
    worker_id = (result_summary or {}).get("node_id")
    output = (result_summary or {}).get("output") or {}
    return {
        "status": status,
        "job_id": (job_summary or {}).get("job_id"),
        "job_status": (job_summary or {}).get("status"),
        "answer": output.get("answer"),
        "model": output.get("model") or config.model,
        "worker_node_id": worker_id,
        "requester_balance_after": balances.get(config.requester_account_id),
        "worker_balance_after": balances.get(worker_id) if worker_id else None,
        "recommended_next_action": _recommended_next_action(status=status, errors=errors),
        "suggested_commands": _suggested_commands(config=config, status=status, errors=errors),
    }


def _recommended_next_action(*, status: str, errors: list[str]) -> str:
    if status == "pass":
        return "continue_chat_session"
    if status == "submitted":
        return "wait_for_worker_result"
    joined = "\n".join(errors).lower()
    if "negative" in joined or "credit" in joined:
        return "grant_requester_credits"
    if "timeout" in joined or "did not verify" in joined:
        return "check_live_ollama_workers"
    if "403" in joined or "forbidden" in joined:
        return "check_invite_or_admission_token"
    if "connection" in joined or "refused" in joined or "reachable" in joined:
        return "check_coordinator_reachability"
    return "inspect_chat_ask_report"


def _suggested_commands(*, config: ChatAskConfig, status: str, errors: list[str]) -> list[str]:
    if _recommended_next_action(status=status, errors=errors) != "grant_requester_credits":
        return []
    coordinator_arg = (
        f"--coordinator {config.coordinator_url}"
        if config.coordinator_url
        else "--invite <alpha-invite.json>"
    )
    return [
        "python -m chatp2p.cli operator grant-requester-credits "
        f"{coordinator_arg} "
        "--operator-config <operator-config.json> "
        f"--requester-account-id {config.requester_account_id} "
        f"--credits {config.job_cost}"
    ]


def _find_job(snapshot: dict[str, Any] | None, job_id: str) -> dict[str, Any] | None:
    return next((job for job in (snapshot or {}).get("jobs", []) if job.get("job_id") == job_id), None)


def _find_result(snapshot: dict[str, Any] | None, job_id: str) -> dict[str, Any] | None:
    return next((result for result in (snapshot or {}).get("results", []) if result.get("job_id") == job_id), None)


def _format_exception(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        return f"HTTPError {exc.code}{suffix}"
    if isinstance(exc, URLError):
        return f"URLError: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def _write_report(report: dict[str, Any]) -> None:
    artifacts = report["artifacts"]
    Path(artifacts["json"]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    Path(artifacts["markdown"]).write_text(format_chat_ask_markdown(report), encoding="utf-8")


def _step(name: str, status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "ok": status in {"pass", "skipped"}, "status": status, "details": details or {}}

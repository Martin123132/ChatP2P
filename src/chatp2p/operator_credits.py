"""Operator credit reports and guarded requester credit grants."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit

from .alpha import load_alpha_invite
from .chat_request import DEFAULT_COORDINATOR_URL
from .client import CoordinatorClient
from .operator_config import OperatorConfig


OPERATOR_CREDITS_REPORT_SCHEMA = "chatp2p.operator-credits-report.v1"
OPERATOR_GRANT_REQUESTER_CREDITS_REPORT_SCHEMA = "chatp2p.operator-grant-requester-credits-report.v1"
SAFE_GRANT_REASONS = ("operator_credit_grant", "requester_credit_topup", "dev_credit_grant")


@dataclass(frozen=True)
class OperatorCreditsConfig:
    out_dir: Path = Path(".mesh/operator-credits")
    coordinator_url: str | None = None
    invite_path: Path | None = None
    admission_token: str | None = None
    requester_account_id: str | None = None
    min_requester_balance: int = 1
    client_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class OperatorGrantRequesterCreditsConfig:
    requester_account_id: str
    credits: int
    out_dir: Path = Path(".mesh/operator-credit-grant")
    coordinator_url: str | None = None
    invite_path: Path | None = None
    operator_config_path: Path | None = None
    credit_grant_token: str | None = None
    reason: str = "operator_credit_grant"
    transaction_id: str | None = None
    dry_run: bool = False
    client_timeout_seconds: float = 10.0


def run_operator_credits(config: OperatorCreditsConfig) -> dict[str, Any]:
    """Inspect requester/worker balances from a coordinator ledger."""

    _validate_credits_config(config)
    started_at = time.time()
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "json": str(out_dir / "operator-credits.json"),
        "markdown": str(out_dir / "operator-credits.md"),
    }

    connection = _resolve_connection(
        coordinator_url=config.coordinator_url,
        invite_path=config.invite_path,
        admission_token=config.admission_token,
    )
    ledger: dict[str, Any] | None = None
    errors: list[str] = []
    steps: list[dict[str, Any]] = []

    try:
        client = CoordinatorClient(
            connection["coordinator_url"],
            admission_token=connection["token"],
            timeout_seconds=config.client_timeout_seconds,
        )
        ledger = client.ledger()["credit_ledger"]
        steps.append(_step("fetch_credit_ledger", "pass", {"coordinator": connection["coordinator_url"]}))
    except Exception as exc:
        errors.append(_format_exception(exc))
        steps.append(_step("fetch_credit_ledger", "fail", {"error": errors[-1]}))

    status = "pass" if not errors else "fail"
    report = {
        "schema": OPERATOR_CREDITS_REPORT_SCHEMA,
        "ok": status == "pass",
        "status": status,
        "generated_at": generated_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "out_dir": str(out_dir),
            "coordinator": connection["coordinator_url"],
            "invite_path": _path_or_none(config.invite_path),
            "auth": {"admission_token_present": bool(connection["token"])},
            "requester_account_id": config.requester_account_id,
            "min_requester_balance": config.min_requester_balance,
        },
        "invite": connection["invite_summary"],
        "summary": _credits_summary(
            status=status,
            ledger=ledger,
            requester_account_id=config.requester_account_id,
            min_requester_balance=config.min_requester_balance,
            errors=errors,
        ),
        "ledger": ledger,
        "steps": steps,
        "errors": errors,
        "artifacts": artifacts,
    }
    _write_credits_report(report)
    return report


def run_operator_grant_requester_credits(config: OperatorGrantRequesterCreditsConfig) -> dict[str, Any]:
    """Grant requester credits through the guarded operator-only endpoint."""

    _validate_grant_config(config)
    started_at = time.time()
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "json": str(out_dir / "grant-requester-credits.json"),
        "markdown": str(out_dir / "grant-requester-credits.md"),
    }

    connection = _resolve_connection(
        coordinator_url=config.coordinator_url,
        invite_path=config.invite_path,
        admission_token=None,
    )
    grant_token = _resolve_credit_grant_token(config)
    errors: list[str] = []
    steps: list[dict[str, Any]] = []
    grant_response: dict[str, Any] | None = None

    if not grant_token:
        errors.append("credit_grant_token is required; pass --credit-grant-token or --operator-config")
        steps.append(_step("resolve_credit_grant_token", "fail", {"token_present": False}))
    else:
        steps.append(_step("resolve_credit_grant_token", "pass", {"token_present": True}))

    if config.dry_run:
        steps.append(
            _step(
                "grant_requester_credits",
                "skipped",
                {"reason": "--dry-run", "account_id": config.requester_account_id, "credits": config.credits},
            )
        )
    elif grant_token:
        try:
            client = CoordinatorClient(
                connection["coordinator_url"],
                timeout_seconds=config.client_timeout_seconds,
            )
            grant_response = client.grant_requester_credits(
                credit_grant_token=grant_token,
                account_id=config.requester_account_id,
                credits=config.credits,
                reason=config.reason,
                transaction_id=config.transaction_id,
            )
            steps.append(
                _step(
                    "grant_requester_credits",
                    "pass",
                    {
                        "account_id": config.requester_account_id,
                        "credits": config.credits,
                        "balance": grant_response.get("balance"),
                    },
                )
            )
        except Exception as exc:
            errors.append(_format_exception(exc))
            steps.append(_step("grant_requester_credits", "fail", {"error": errors[-1]}))

    status = "dry_run" if config.dry_run and not errors else ("pass" if not errors else "fail")
    report = {
        "schema": OPERATOR_GRANT_REQUESTER_CREDITS_REPORT_SCHEMA,
        "ok": status in {"pass", "dry_run"},
        "status": status,
        "dry_run": config.dry_run,
        "generated_at": generated_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "out_dir": str(out_dir),
            "coordinator": connection["coordinator_url"],
            "invite_path": _path_or_none(config.invite_path),
            "operator_config_path": _path_or_none(config.operator_config_path),
            "auth": {"credit_grant_token_present": bool(grant_token)},
            "requester_account_id": config.requester_account_id,
            "credits": config.credits,
            "reason": config.reason,
            "transaction_id": config.transaction_id,
        },
        "invite": connection["invite_summary"],
        "summary": _grant_summary(status=status, config=config, response=grant_response, errors=errors),
        "grant": grant_response,
        "steps": steps,
        "errors": errors,
        "artifacts": artifacts,
    }
    _write_grant_report(report)
    return report


def format_operator_credits_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Operator credits: {str(report.get('status', 'unknown')).upper()}",
        f"Coordinator: {(report.get('config') or {}).get('coordinator')}",
        f"Ledger entries: {summary.get('ledger_entries')}",
        f"Requester accounts: {summary.get('requester_account_count')}",
        f"Worker accounts: {summary.get('worker_account_count')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    requester = summary.get("selected_requester")
    if requester:
        lines.insert(4, f"Requester balance: {requester.get('balance')}")
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_operator_grant_requester_credits_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Grant requester credits: {str(report.get('status', 'unknown')).upper()}",
        f"Coordinator: {(report.get('config') or {}).get('coordinator')}",
        f"Requester: {summary.get('requester_account_id')}",
        f"Credits: {summary.get('credits')}",
        f"Balance after: {summary.get('balance_after')}",
        f"Next: {summary.get('recommended_next_action')}",
        f"Report: {(report.get('artifacts') or {}).get('json')}",
    ]
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_operator_credits_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Operator Credits",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Coordinator: `{(report.get('config') or {}).get('coordinator')}`",
        f"- Ledger entries: `{summary.get('ledger_entries')}`",
        f"- Positive credits: `{summary.get('positive_credits')}`",
        f"- Negative credits: `{summary.get('negative_credits')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Requesters",
        "",
    ]
    requesters = summary.get("requester_accounts") or []
    if requesters:
        lines.extend(
            f"- `{item.get('account_id')}` balance `{item.get('balance')}`"
            for item in requesters
        )
    else:
        lines.append("- none observed")
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def format_operator_grant_requester_credits_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Grant Requester Credits",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Coordinator: `{(report.get('config') or {}).get('coordinator')}`",
        f"- Requester account: `{summary.get('requester_account_id')}`",
        f"- Credits: `{summary.get('credits')}`",
        f"- Balance after: `{summary.get('balance_after')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Steps",
        "",
    ]
    for step in report.get("steps") or []:
        lines.append(f"- `{step.get('name')}`: `{step.get('status')}`")
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    return "\n".join(lines)


def _validate_credits_config(config: OperatorCreditsConfig) -> None:
    if config.coordinator_url is not None and not config.coordinator_url.strip():
        raise ValueError("--coordinator must be non-empty")
    if config.requester_account_id is not None and not config.requester_account_id.strip():
        raise ValueError("--requester-account-id must be non-empty")
    if config.min_requester_balance < 0:
        raise ValueError("--min-requester-balance must be at least 0")
    if config.client_timeout_seconds <= 0:
        raise ValueError("--client-timeout-seconds must be greater than 0")


def _validate_grant_config(config: OperatorGrantRequesterCreditsConfig) -> None:
    if not config.requester_account_id.strip():
        raise ValueError("--requester-account-id must be non-empty")
    if config.credits < 1:
        raise ValueError("--credits must be at least 1")
    if config.reason not in SAFE_GRANT_REASONS:
        raise ValueError(f"--reason must be one of: {', '.join(SAFE_GRANT_REASONS)}")
    if config.coordinator_url is not None and not config.coordinator_url.strip():
        raise ValueError("--coordinator must be non-empty")
    if config.client_timeout_seconds <= 0:
        raise ValueError("--client-timeout-seconds must be greater than 0")


def _resolve_connection(
    *,
    coordinator_url: str | None,
    invite_path: Path | None,
    admission_token: str | None,
) -> dict[str, Any]:
    invite = load_alpha_invite(invite_path.expanduser()) if invite_path else None
    resolved_url = coordinator_url or (invite.coordinator if invite else DEFAULT_COORDINATOR_URL)
    token = admission_token or (invite.admission_token if invite else None)
    return {
        "coordinator_url": _safe_url(resolved_url.rstrip("/")),
        "token": token,
        "invite_summary": invite.public_summary() if invite else None,
    }


def _resolve_credit_grant_token(config: OperatorGrantRequesterCreditsConfig) -> str | None:
    if config.credit_grant_token:
        return config.credit_grant_token.strip()
    if not config.operator_config_path:
        return None
    operator_config = OperatorConfig.from_file(config.operator_config_path.expanduser())
    return operator_config.credit_grant_token


def _credits_summary(
    *,
    status: str,
    ledger: dict[str, Any] | None,
    requester_account_id: str | None,
    min_requester_balance: int,
    errors: list[str],
) -> dict[str, Any]:
    ledger_summary = (ledger or {}).get("summary") or {}
    recent_entries = (ledger or {}).get("recent_entries") or []
    balances = ledger_summary.get("balances") or {}
    requester_accounts = _accounts_by_type(
        entries=recent_entries,
        balances=balances,
        account_type="requester",
        selected_account_id=requester_account_id,
    )
    worker_accounts = _accounts_by_type(entries=recent_entries, balances=balances, account_type="node")
    selected_requester = (
        next((item for item in requester_accounts if item["account_id"] == requester_account_id), None)
        if requester_account_id
        else None
    )
    return {
        "status": status,
        "ledger_entries": ledger_summary.get("entries", 0),
        "positive_credits": ledger_summary.get("positive_credits", 0),
        "negative_credits": ledger_summary.get("negative_credits", 0),
        "net_credits": ledger_summary.get("net_credits", 0),
        "by_reason": ledger_summary.get("by_reason") or {},
        "requester_account_count": len(requester_accounts),
        "worker_account_count": len(worker_accounts),
        "requester_accounts": requester_accounts,
        "worker_accounts": worker_accounts,
        "selected_requester": selected_requester,
        "recommended_next_action": _credits_recommended_action(
            status=status,
            requester=selected_requester,
            min_requester_balance=min_requester_balance,
            errors=errors,
        ),
    }


def _grant_summary(
    *,
    status: str,
    config: OperatorGrantRequesterCreditsConfig,
    response: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, Any]:
    entry = (response or {}).get("entry") or {}
    return {
        "status": status,
        "requester_account_id": config.requester_account_id,
        "credits": config.credits,
        "reason": config.reason,
        "transaction_id": entry.get("transaction_id") or config.transaction_id,
        "balance_after": (response or {}).get("balance") or entry.get("balance_after"),
        "recommended_next_action": _grant_recommended_action(status=status, errors=errors),
    }


def _accounts_by_type(
    *,
    entries: list[dict[str, Any]],
    balances: dict[str, Any],
    account_type: str,
    selected_account_id: str | None = None,
) -> list[dict[str, Any]]:
    account_ids = {
        str(entry["account_id"])
        for entry in entries
        if entry.get("account_type") == account_type and isinstance(entry.get("account_id"), str)
    }
    if selected_account_id:
        account_ids.add(selected_account_id)
    accounts = []
    for account_id in sorted(account_ids):
        account_entries = [entry for entry in entries if entry.get("account_id") == account_id]
        accounts.append(
            {
                "account_id": account_id,
                "balance": balances.get(account_id),
                "recent_entries": len(account_entries),
                "recent_net_delta": sum(int(entry.get("delta", 0)) for entry in account_entries),
            }
        )
    return accounts


def _credits_recommended_action(
    *,
    status: str,
    requester: dict[str, Any] | None,
    min_requester_balance: int,
    errors: list[str],
) -> str:
    if status != "pass":
        joined = "\n".join(errors).lower()
        if "connection" in joined or "refused" in joined or "urlerror" in joined:
            return "check_coordinator_reachability"
        return "inspect_operator_credits_report"
    if requester is not None:
        balance = requester.get("balance")
        if balance is None or int(balance) < min_requester_balance:
            return "grant_requester_credits"
    return "continue_chat_ask"


def _grant_recommended_action(*, status: str, errors: list[str]) -> str:
    if status == "pass":
        return "run_chat_ask"
    if status == "dry_run":
        return "rerun_grant_without_dry_run"
    joined = "\n".join(errors).lower()
    if "403" in joined or "credit grant token" in joined:
        return "check_operator_credit_grant_token"
    if "connection" in joined or "refused" in joined or "urlerror" in joined:
        return "check_coordinator_reachability"
    return "inspect_operator_grant_report"


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return url


def _path_or_none(path: Path | None) -> str | None:
    return str(path.expanduser().resolve()) if path is not None else None


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


def _write_credits_report(report: dict[str, Any]) -> None:
    artifacts = report["artifacts"]
    Path(artifacts["json"]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    Path(artifacts["markdown"]).write_text(format_operator_credits_markdown(report), encoding="utf-8")


def _write_grant_report(report: dict[str, Any]) -> None:
    artifacts = report["artifacts"]
    Path(artifacts["json"]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    Path(artifacts["markdown"]).write_text(format_operator_grant_requester_credits_markdown(report), encoding="utf-8")


def _step(name: str, status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "ok": status in {"pass", "skipped"}, "status": status, "details": details or {}}

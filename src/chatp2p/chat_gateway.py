"""Local-only HTTP gateway for safe ChatP2P chat sessions."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from .alpha import load_alpha_invite
from .chat_session import (
    DEFAULT_COORDINATOR_URL,
    DEFAULT_MAX_CONTEXT_TURNS,
    ChatSessionContinueConfig,
    ChatSessionResumeConfig,
    ChatSessionStatusConfig,
    ChatSessionSyncConfig,
    run_chat_session_continue,
    run_chat_session_resume,
    run_chat_session_status,
    run_chat_session_sync,
)
from .client import CoordinatorClient
from .runtime_metadata import collect_software_metadata, software_metadata_public_view


CHAT_GATEWAY_REPORT_SCHEMA = "chatp2p.chat-gateway-report.v1"
CHAT_GATEWAY_TRANSCRIPT_SCHEMA = "chatp2p.chat-gateway-transcript.v1"
CHAT_GATEWAY_READINESS_SCHEMA = "chatp2p.chat-gateway-readiness.v1"
CHAT_GATEWAY_MODEL_CATALOG_SCHEMA = "chatp2p.chat-gateway-model-catalog.v1"
CHAT_GATEWAY_SESSION_CONTROL_SCHEMA = "chatp2p.chat-gateway-session-control.v1"
DEFAULT_CHAT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_CHAT_GATEWAY_PORT = 8787
DEFAULT_CHAT_GATEWAY_MAX_REQUEST_BYTES = 16_384
CHAT_GATEWAY_MAX_MODEL_LENGTH = 128
CHAT_GATEWAY_MODEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-/")
GATEWAY_ERROR_CATEGORIES = {
    "coordinator_unreachable",
    "insufficient_credits",
    "no_model_worker",
    "unresolved_session",
    "invalid_model",
    "request_timeout",
}
CHAT_GATEWAY_SESSION_FILES = (
    "chat-session.json",
    "chat-session.md",
    "chat-session-status.json",
    "chat-session-status.md",
    "chat-session-resume.json",
    "chat-session-resume.md",
    "chat-session-sync.json",
    "chat-session-sync.md",
    "chat-session-continue.json",
    "chat-session-continue.md",
)


@dataclass(frozen=True)
class ChatGatewayConfig:
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
    host: str = DEFAULT_CHAT_GATEWAY_HOST
    port: int = DEFAULT_CHAT_GATEWAY_PORT
    max_request_bytes: int = DEFAULT_CHAT_GATEWAY_MAX_REQUEST_BYTES
    source_root: Path | None = None


def create_chat_gateway_server(config: ChatGatewayConfig) -> ThreadingHTTPServer:
    """Create a localhost chat gateway server without starting its loop."""

    _validate_config(config)
    safe_config = _safe_config(config)
    software = software_metadata_public_view(collect_software_metadata(config.source_root))

    class ChatGatewayHandler(BaseHTTPRequestHandler):
        server_version = "ChatP2PGateway/0"

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _html_response(self, 200, _render_gateway_html(config))
                return
            if parsed.path == "/health":
                _json_response(self, 200, _health_payload(config=safe_config, software=software))
                return
            if parsed.path == "/api/session/status":
                _json_response(self, 200, _status_payload(config))
                return
            if parsed.path == "/api/session/transcript":
                _json_response(self, 200, _transcript_payload(config))
                return
            if parsed.path == "/api/chat/readiness":
                request_config, error = _config_from_query_model(config, parsed.query)
                if error:
                    _json_response(self, 400, _bad_model_payload(error))
                    return
                _json_response(self, 200, _readiness_payload(request_config))
                return
            if parsed.path == "/api/chat/models":
                request_config, error = _config_from_query_model(config, parsed.query)
                if error:
                    _json_response(self, 400, _bad_model_payload(error))
                    return
                _json_response(self, 200, _model_catalog_payload(request_config))
                return
            _json_response(self, 404, {"ok": False, "status": "fail", "error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/session/sync":
                _json_response(self, 200, _sync_payload(config))
                return
            if parsed.path == "/api/session/resume-dry-run":
                _json_response(self, 200, _resume_dry_run_payload(config))
                return
            if parsed.path == "/api/session/reset-dry-run":
                _json_response(self, 200, _session_dry_run_payload(config, action="reset"))
                return
            if parsed.path == "/api/session/archive-dry-run":
                _json_response(self, 200, _session_dry_run_payload(config, action="archive"))
                return
            if parsed.path == "/api/chat/continue":
                request = _read_json_request(self, max_bytes=config.max_request_bytes)
                if request is None:
                    return
                prompt = request.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    _json_response(
                        self,
                        400,
                        _request_error_payload(
                            "prompt must be a non-empty string",
                            recommended_next_action="enter_prompt",
                        ),
                    )
                    return
                request_config, error = _config_from_model_override(config, request.get("model"))
                if error:
                    _json_response(self, 400, _bad_model_payload(error))
                    return
                _json_response(self, 200, _continue_payload(request_config, prompt=prompt))
                return
            _json_response(self, 404, {"ok": False, "status": "fail", "error": "not found"})

    return ThreadingHTTPServer((config.host, config.port), ChatGatewayHandler)


def run_chat_gateway(config: ChatGatewayConfig) -> None:
    """Run the blocking local chat gateway server."""

    server = create_chat_gateway_server(config)
    host, port = server.server_address
    print(f"chat gateway: http://{host}:{port}")
    print(f"session: {config.session_id}")
    print(f"out: {config.out_dir.expanduser().resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down chat gateway")
    finally:
        server.server_close()


def _health_payload(*, config: dict[str, Any], software: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": CHAT_GATEWAY_REPORT_SCHEMA,
        "ok": True,
        "status": "pass",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "service": "chat_gateway",
            "recommended_next_action": "open_chat_gateway",
        },
        "config": config,
        "software": software,
        "endpoints": {
            "health": "/health",
            "session_status": "/api/session/status",
            "session_transcript": "/api/session/transcript",
            "chat_readiness": "/api/chat/readiness",
            "chat_models": "/api/chat/models",
            "session_sync": "/api/session/sync",
            "session_resume_dry_run": "/api/session/resume-dry-run",
            "session_reset_dry_run": "/api/session/reset-dry-run",
            "session_archive_dry_run": "/api/session/archive-dry-run",
            "chat_continue": "/api/chat/continue",
        },
    }


def _status_payload(config: ChatGatewayConfig) -> dict[str, Any]:
    try:
        return _redact_report(
            run_chat_session_status(
                ChatSessionStatusConfig(out_dir=config.out_dir, session_id=config.session_id)
            ),
            config,
        )
    except ValueError as exc:
        message = str(exc)
        if "does not exist" not in message:
            return _error_payload(message)
        session_path = config.out_dir.expanduser().resolve() / "chat-session.json"
        return {
            "schema": "chatp2p.chat-session-status-report.v1",
            "ok": True,
            "status": "no_session",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "session_id": config.session_id,
            "session_path": str(session_path),
            "summary": {
                "status": "no_session",
                "turn_count": 0,
                "completed_turns": 0,
                "submitted_turns": 0,
                "failed_turns": 0,
                "recommended_next_action": "continue_chat_session",
            },
            "turns": [],
            "artifacts": {"session_json": str(session_path)},
        }
    except Exception as exc:
        return _error_payload(f"{type(exc).__name__}: {exc}")


def _transcript_payload(config: ChatGatewayConfig) -> dict[str, Any]:
    session_path = config.out_dir.expanduser().resolve() / "chat-session.json"
    generated_at = datetime.now(timezone.utc).isoformat()
    if not session_path.exists():
        return {
            "schema": CHAT_GATEWAY_TRANSCRIPT_SCHEMA,
            "ok": True,
            "status": "no_session",
            "generated_at": generated_at,
            "session_id": config.session_id,
            "summary": {
                "status": "no_session",
                "turn_count": 0,
                "completed_turns": 0,
                "submitted_turns": 0,
                "failed_turns": 0,
                "recommended_next_action": "continue_chat_session",
            },
            "turns": [],
            "artifacts": {"session_json": str(session_path)},
        }
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _error_payload(f"{type(exc).__name__}: {exc}")
    if session.get("session_id") != config.session_id:
        return _error_payload("existing chat session id does not match --session-id")

    turns = [_transcript_turn(turn) for turn in session.get("turns") or []]
    summary = dict(session.get("summary") or {})
    summary.setdefault("status", session.get("status"))
    summary["turn_count"] = len(turns)
    summary.setdefault("completed_turns", len([turn for turn in turns if turn.get("status") == "pass"]))
    summary.setdefault("submitted_turns", len([turn for turn in turns if turn.get("status") == "submitted"]))
    summary.setdefault("failed_turns", len([turn for turn in turns if turn.get("status") == "fail"]))
    summary.setdefault("recommended_next_action", "continue_chat_session")
    return {
        "schema": CHAT_GATEWAY_TRANSCRIPT_SCHEMA,
        "ok": bool(session.get("ok", True)),
        "status": str(session.get("status") or summary.get("status") or "unknown"),
        "generated_at": generated_at,
        "session_id": config.session_id,
        "title": session.get("title"),
        "summary": summary,
        "turns": turns,
        "artifacts": {"session_json": str(session_path)},
    }


def _sync_payload(config: ChatGatewayConfig) -> dict[str, Any]:
    try:
        return _redact_report(
            run_chat_session_sync(
                ChatSessionSyncConfig(
                    out_dir=config.out_dir,
                    session_id=config.session_id,
                    coordinator_url=config.coordinator_url,
                    invite_path=config.invite_path,
                    admission_token=config.admission_token,
                    client_timeout_seconds=config.client_timeout_seconds,
                )
            ),
            config,
        )
    except Exception as exc:
        return _error_payload(f"{type(exc).__name__}: {exc}")


def _resume_dry_run_payload(config: ChatGatewayConfig) -> dict[str, Any]:
    try:
        return _redact_report(
            run_chat_session_resume(
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
            ),
            config,
        )
    except Exception as exc:
        return _error_payload(f"{type(exc).__name__}: {exc}")


def _session_dry_run_payload(config: ChatGatewayConfig, *, action: str) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    out_dir = config.out_dir.expanduser().resolve()
    inventory = _session_control_inventory(out_dir)
    archive_target = out_dir / "archive" / f"{config.session_id}-{generated_at.strftime('%Y%m%dT%H%M%SZ')}"
    session_exists = any(item["name"] == "chat-session.json" and item["exists"] for item in inventory["files"])
    if action == "reset":
        action_id = "reset_session_dry_run"
        description = "Preview archiving the current local session so the next prompt can start a clean session."
        next_action = "review_reset_plan_then_archive_manually"
    elif action == "archive":
        action_id = "archive_session_dry_run"
        description = "Preview moving local session files and turn folders into an archive directory."
        next_action = "review_archive_plan"
    else:
        return _error_payload(f"unknown session control action: {action}")

    status = "pass" if session_exists else "no_session"
    report = {
        "schema": CHAT_GATEWAY_SESSION_CONTROL_SCHEMA,
        "ok": True,
        "status": status,
        "generated_at": generated_at.isoformat(),
        "session_id": config.session_id,
        "dry_run": True,
        "action": {
            "id": action_id,
            "description": description,
            "local_only": True,
            "partner_required": False,
            "credit_spend": False,
            "will_modify": False,
        },
        "summary": {
            "status": status,
            "session_exists": session_exists,
            "file_count": len([item for item in inventory["files"] if item["exists"]]),
            "turn_dir_count": len(inventory["turn_directories"]),
            "archive_target": str(archive_target),
            "recommended_next_action": next_action if session_exists else "continue_chat_session",
        },
        "plan": {
            "archive_target": str(archive_target),
            "files_to_archive": [item for item in inventory["files"] if item["exists"]],
            "turn_directories_to_archive": inventory["turn_directories"],
            "session_json_after_archive": str(out_dir / "chat-session.json"),
        },
        "warnings": [] if session_exists else ["no_session_to_archive_or_reset"],
        "errors": [],
    }
    return _redact_report(report, config)


def _session_control_inventory(out_dir: Path) -> dict[str, Any]:
    files = []
    for name in CHAT_GATEWAY_SESSION_FILES:
        path = out_dir / name
        files.append(
            {
                "name": name,
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": path.stat().st_size if path.exists() else None,
            }
        )
    turn_directories = []
    if out_dir.exists():
        for path in sorted(out_dir.glob("turn-*")):
            if path.is_dir():
                turn_directories.append({"name": path.name, "path": str(path)})
    return {"files": files, "turn_directories": turn_directories}


def _continue_payload(config: ChatGatewayConfig, *, prompt: str) -> dict[str, Any]:
    try:
        report = run_chat_session_continue(
            ChatSessionContinueConfig(
                out_dir=config.out_dir,
                session_id=config.session_id,
                title=config.title,
                coordinator_url=config.coordinator_url,
                invite_path=config.invite_path,
                admission_token=config.admission_token,
                model=config.model,
                prompt=prompt.strip(),
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
        return _redact_report(_with_gateway_error_clarity(report, config), config)
    except Exception as exc:
        return _error_payload(f"{type(exc).__name__}: {exc}", config=config)


def _readiness_payload(config: ChatGatewayConfig) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []
    connection = _resolve_gateway_connection(config)
    transcript = _transcript_payload(config)
    session = _readiness_session(transcript)
    snapshot_info = _fetch_snapshot(connection=connection, timeout_seconds=config.client_timeout_seconds)
    snapshot = snapshot_info["snapshot"]
    catalog = _model_catalog_from_snapshot(snapshot=snapshot, selected_model=config.model)

    requester = _readiness_requester(snapshot=snapshot, account_id=config.requester_account_id, job_cost=config.job_cost)
    routing = _model_routing_from_catalog(catalog=catalog, model=config.model)
    warnings.extend(routing["warnings"])

    can_send = (
        not session["blocked"]
        and snapshot is not None
        and requester["credits_sufficient"]
        and routing["live_eligible_node_count"] > 0
    )
    recommended_next_action = _readiness_recommended_next_action(
        session=session,
        coordinator_ok=snapshot_info["ok"],
        credits_sufficient=requester["credits_sufficient"],
        live_eligible_node_count=routing["live_eligible_node_count"],
    )
    error_category = _readiness_error_category(
        session=session,
        snapshot_info=snapshot_info,
        requester=requester,
        routing=routing,
    )
    action_hint = _readiness_action_hint(
        config=config,
        connection=connection,
        action_id=recommended_next_action,
        requester_balance=requester["balance"],
        error_category=error_category,
    )
    status = "pass" if can_send else "blocked"
    return _redact_report(
        {
            "schema": CHAT_GATEWAY_READINESS_SCHEMA,
            "ok": can_send,
            "status": status,
            "generated_at": generated_at,
            "summary": {
                "status": status,
                "can_send": can_send,
                "coordinator_reachable": snapshot_info["ok"],
                "requester_balance": requester["balance"],
                "job_cost": config.job_cost,
                "credits_sufficient": requester["credits_sufficient"],
                "model": config.model,
                "live_eligible_node_count": routing["live_eligible_node_count"],
                "session_blocked": session["blocked"],
                "recommended_next_action": recommended_next_action,
                "suggested_command": action_hint["primary_command"],
                "error_category": error_category,
            },
            "error_category": error_category,
            "ui_message": _gateway_ui_message(error_category, recommended_next_action),
            "action_hint": action_hint,
            "coordinator": {
                "ok": snapshot_info["ok"],
                "url": connection["coordinator_url"],
                "error": snapshot_info["error"],
                "status": _snapshot_status_summary(snapshot),
            },
            "requester": requester,
            "model_routing": routing,
            "model_catalog": catalog["summary"],
            "session": session,
            "invite": connection["invite_summary"],
            "warnings": warnings,
            "errors": list(snapshot_info["errors"]),
        },
        config,
    )


def _model_catalog_payload(config: ChatGatewayConfig) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    connection = _resolve_gateway_connection(config)
    snapshot_info = _fetch_snapshot(connection=connection, timeout_seconds=config.client_timeout_seconds)
    catalog = _model_catalog_from_snapshot(snapshot=snapshot_info["snapshot"], selected_model=config.model)
    status = "pass" if snapshot_info["ok"] and catalog["summary"]["available_model_count"] > 0 else "warn"
    return _redact_report(
        {
            "schema": CHAT_GATEWAY_MODEL_CATALOG_SCHEMA,
            "ok": snapshot_info["ok"],
            "status": status,
            "generated_at": generated_at,
            "summary": {
                **catalog["summary"],
                "status": status,
                "recommended_next_action": _model_catalog_recommended_next_action(
                    coordinator_ok=snapshot_info["ok"],
                    selected_model_sendable=catalog["summary"]["selected_model_sendable"],
                    recommended_model=catalog["summary"]["recommended_model"],
                ),
            },
            "coordinator": {
                "ok": snapshot_info["ok"],
                "url": connection["coordinator_url"],
                "error": snapshot_info["error"],
                "status": _snapshot_status_summary(snapshot_info["snapshot"]),
            },
            "models": catalog["models"],
            "warnings": catalog["warnings"],
            "errors": list(snapshot_info["errors"]),
        },
        config,
    )


def _config_from_query_model(config: ChatGatewayConfig, query: str) -> tuple[ChatGatewayConfig, str | None]:
    values = parse_qs(query, keep_blank_values=True).get("model") or []
    if not values:
        return config, None
    return _config_from_model_override(config, values[-1])


def _config_from_model_override(config: ChatGatewayConfig, value: Any) -> tuple[ChatGatewayConfig, str | None]:
    if value is None:
        return config, None
    model, error = _validate_model_override(value)
    if error:
        return config, error
    return replace(config, model=model), None


def _validate_model_override(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, str):
        return "", "model must be a string"
    model = value.strip()
    if not model:
        return "", "model must be a non-empty string"
    if model != value:
        return "", "model must not contain leading or trailing whitespace"
    if len(model) > CHAT_GATEWAY_MAX_MODEL_LENGTH:
        return "", f"model must be at most {CHAT_GATEWAY_MAX_MODEL_LENGTH} characters"
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in model):
        return "", "model must not contain whitespace or control characters"
    if any(char not in CHAT_GATEWAY_MODEL_CHARS for char in model):
        return "", "model may contain only letters, numbers, '.', '_', ':', '-', and '/'"
    return model, None


def _bad_model_payload(error: str) -> dict[str, Any]:
    return {
        "schema": CHAT_GATEWAY_REPORT_SCHEMA,
        "ok": False,
        "status": "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error_category": "invalid_model",
        "error": error,
        "errors": [error],
        "summary": {
            "recommended_next_action": "choose_valid_model",
            "error_category": "invalid_model",
        },
        "ui_message": _gateway_ui_message("invalid_model", "choose_valid_model"),
        "action_hint": _static_action_hint("choose_valid_model"),
    }


def _render_gateway_html(config: ChatGatewayConfig) -> str:
    title = html.escape(config.session_id)
    model = html.escape(config.model)
    model_json = json.dumps(config.model)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ChatP2P {title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --ink: #151922;
      --muted: #626d7c;
      --line: #d6dde6;
      --accent: #0f766e;
      --warn: #9a3412;
      --bad: #b91c1c;
      --wait: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Segoe UI, system-ui, sans-serif; background: var(--bg); color: var(--ink); }}
    main {{ min-height: 100vh; display: grid; grid-template-rows: auto 1fr auto; }}
    header {{ padding: 16px 20px; border-bottom: 1px solid var(--line); background: var(--panel); }}
    .bar {{ max-width: 1040px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
    h1 {{ font-size: 20px; margin: 0; font-weight: 700; }}
    .sub {{ color: var(--muted); font-size: 13px; margin-top: 3px; }}
    button, textarea, select {{ font: inherit; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    button {{ border: 1px solid #cbd1d8; background: #fff; color: #111827; border-radius: 6px; padding: 8px 12px; cursor: pointer; min-height: 38px; }}
    button.primary {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    select {{ border: 1px solid #cbd1d8; background: #fff; color: #111827; border-radius: 6px; padding: 6px 8px; min-height: 32px; max-width: 220px; }}
    #status {{ display: grid; gap: 8px; max-width: 1040px; margin: 0 auto; padding: 14px 20px 0; }}
    .status-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--muted); font-size: 14px; }}
    .badge {{ display: inline-flex; align-items: center; min-height: 24px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); background: #fff; color: var(--muted); font-size: 12px; font-weight: 650; }}
    .badge.pass {{ color: #047857; border-color: #a7f3d0; background: #ecfdf5; }}
    .badge.fail, .badge.blocked {{ color: var(--bad); border-color: #fecaca; background: #fef2f2; }}
    .badge.submitted {{ color: var(--wait); border-color: #bfdbfe; background: #eff6ff; }}
    .banner {{ display: none; border: 1px solid #fed7aa; background: #fff7ed; color: var(--warn); border-radius: 8px; padding: 10px 12px; }}
    .banner.visible {{ display: block; }}
    .banner.error {{ border-color: #fecaca; background: #fef2f2; color: var(--bad); }}
    .command {{ display: none; border: 1px solid #d1d5db; background: #111827; color: #f9fafb; border-radius: 8px; padding: 10px 12px; font-family: Consolas, ui-monospace, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    .command.visible {{ display: block; }}
    .models {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 13px; }}
    #turns {{ max-width: 1040px; width: 100%; margin: 0 auto; padding: 16px 20px 120px; display: grid; gap: 14px; align-content: start; }}
    .empty {{ border: 1px dashed #cbd5e1; border-radius: 8px; padding: 18px; color: var(--muted); background: rgba(255,255,255,.72); }}
    .turn {{ display: grid; gap: 8px; max-width: min(760px, 92%); }}
    .turn.user {{ justify-self: end; }}
    .turn.assistant {{ justify-self: start; }}
    .bubble {{ border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 12px 14px; white-space: pre-wrap; line-height: 1.45; overflow-wrap: anywhere; }}
    .turn.user .bubble {{ background: #e8f5f3; border-color: #b7dfd9; }}
    .turn.assistant .bubble {{ background: #fff; }}
    .meta {{ color: var(--muted); font-size: 12px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    footer {{ position: fixed; left: 0; right: 0; bottom: 0; border-top: 1px solid var(--line); background: rgba(245,247,250,.96); backdrop-filter: blur(8px); }}
    form {{ max-width: 1040px; margin: 0 auto; padding: 12px 20px; display: grid; grid-template-columns: 1fr auto; gap: 10px; }}
    textarea {{ min-height: 50px; max-height: 180px; resize: vertical; border: 1px solid #cbd1d8; border-radius: 8px; padding: 10px; background: #fff; }}
    @media (max-width: 720px) {{
      .bar {{ align-items: flex-start; flex-direction: column; }}
      form {{ grid-template-columns: 1fr; }}
      .turn {{ max-width: 100%; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="bar">
        <div>
          <h1>ChatP2P</h1>
          <div class="sub">Session {title} - {model}</div>
        </div>
        <div class="actions">
          <button id="statusButton" type="button">Refresh</button>
          <button id="safeActionButton" type="button" hidden>Run Safe Action</button>
          <button id="syncButton" type="button">Sync</button>
          <button id="resumeButton" type="button">Resume Dry Run</button>
          <button id="resetButton" type="button">New Session Dry Run</button>
          <button id="archiveButton" type="button">Archive Dry Run</button>
        </div>
      </div>
    </header>
    <section id="status">
      <div class="status-row">
        <span id="stateBadge" class="badge">loading</span>
        <span id="readinessBadge" class="badge">readiness</span>
        <select id="modelSelect" aria-label="Model"></select>
        <span id="balance"></span>
        <span id="modelRoute"></span>
        <span id="coordinatorState"></span>
        <span id="nextAction"></span>
      </div>
      <div id="modelCatalog" class="models"></div>
      <div id="apiErrorBanner" class="banner error"></div>
      <div id="blockedBanner" class="banner"></div>
      <div id="commandHint" class="command"></div>
    </section>
    <section id="turns"></section>
  </main>
  <footer>
    <form id="chatForm">
      <textarea id="prompt" name="prompt" autocomplete="off"></textarea>
      <button id="sendButton" class="primary" type="submit">Send</button>
    </form>
  </footer>
  <script>
    const stateBadge = document.querySelector("#stateBadge");
    const readinessBadge = document.querySelector("#readinessBadge");
    const balanceEl = document.querySelector("#balance");
    const modelRouteEl = document.querySelector("#modelRoute");
    const coordinatorStateEl = document.querySelector("#coordinatorState");
    const nextActionEl = document.querySelector("#nextAction");
    const modelCatalogEl = document.querySelector("#modelCatalog");
    const modelSelectEl = document.querySelector("#modelSelect");
    const apiErrorBanner = document.querySelector("#apiErrorBanner");
    const blockedBanner = document.querySelector("#blockedBanner");
    const commandHintEl = document.querySelector("#commandHint");
    const turnsEl = document.querySelector("#turns");
    const promptEl = document.querySelector("#prompt");
    const sendButton = document.querySelector("#sendButton");
    const safeActionButton = document.querySelector("#safeActionButton");
    const buttons = Array.from(document.querySelectorAll("button"));
    const defaultModel = {model_json};
    const blockedActions = new Set(["run_session_resume_dry_run", "run_session_sync_then_resume_dry_run", "wait_for_worker_result", "resume_failed_turn", "sync_session_then_resume_failed_turn"]);
    const safeActionEndpoints = {{
      run_session_sync: "/api/session/sync",
      wait_for_worker_result: "/api/session/sync",
      run_session_resume_dry_run: "/api/session/resume-dry-run",
      resume_failed_turn: "/api/session/resume-dry-run",
      sync_session_then_resume_failed_turn: "/api/session/sync",
      run_session_sync_then_resume_dry_run: "/api/session/sync"
    }};
    let currentSafeAction = "";
    function selectedModel() {{
      return modelSelectEl.value || defaultModel;
    }}
    function modelQuery() {{
      return `?model=${{encodeURIComponent(selectedModel())}}`;
    }}
    function setBusy(value) {{
      buttons.forEach((button) => button.disabled = value);
      sendButton.textContent = value ? "Sending" : "Send";
      if (!value) updateSafeActionButton();
    }}
    function updateSafeActionButton() {{
      const enabled = Boolean(safeActionEndpoints[currentSafeAction]);
      safeActionButton.hidden = !enabled;
      safeActionButton.disabled = !enabled;
    }}
    function badgeClass(status) {{
      return `badge ${{status || ""}}`;
    }}
    function formatBalance(turns) {{
      for (let index = turns.length - 1; index >= 0; index -= 1) {{
        const balance = turns[index].requester_balance_after;
        if (balance !== null && balance !== undefined) return `Balance ${{balance}}`;
      }}
      return "";
    }}
    function renderApiError(report, statusCode) {{
      const summary = report.summary || {{}};
      const category = report.error_category || summary.error_category || "";
      const message = report.ui_message || report.error || "";
      if (category || statusCode >= 400) {{
        apiErrorBanner.className = "banner error visible";
        apiErrorBanner.textContent = [category, message].filter(Boolean).join(": ");
      }} else if (report.status === "pass" || report.status === "no_session" || report.status === "submitted") {{
        apiErrorBanner.className = "banner error";
        apiErrorBanner.textContent = "";
      }}
    }}
    function renderModelCatalog(report) {{
      const summary = report.summary || {{}};
      const models = report.models || [];
      const sendable = (report.models || []).filter((item) => item.sendable);
      const names = sendable.map((item) => `${{item.model}} (${{item.live_worker_count}})`);
      const selected = summary.selected_model || defaultModel;
      const recommendedModel = summary.recommended_model || "";
      renderModelOptions(models, selected, recommendedModel);
      modelCatalogEl.textContent = `Model ${{selected}} - Recommended ${{recommendedModel || "none"}} - Available ${{names.join(", ") || "none"}}`;
    }}
    function renderModelOptions(models, selected, recommended) {{
      const current = selectedModel();
      const choices = [];
      function addChoice(value) {{
        if (value && !choices.includes(value)) choices.push(value);
      }}
      addChoice(current);
      addChoice(selected);
      addChoice(recommended);
      (models || []).filter((item) => item.sendable).forEach((item) => addChoice(item.model));
      (models || []).forEach((item) => addChoice(item.model));
      addChoice(defaultModel);
      modelSelectEl.replaceChildren();
      choices.forEach((name) => {{
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        modelSelectEl.append(option);
      }});
      modelSelectEl.value = choices.includes(current) ? current : selected || recommended || defaultModel;
    }}
    function renderReadiness(report) {{
      const summary = report.summary || {{}};
      const routing = report.model_routing || {{}};
      const coordinator = report.coordinator || {{}};
      const actionHint = report.action_hint || {{}};
      const command = actionHint.primary_command || "";
      currentSafeAction = actionHint.id || summary.recommended_next_action || "";
      updateSafeActionButton();
      readinessBadge.className = badgeClass(report.status || "unknown");
      readinessBadge.textContent = summary.can_send ? "ready" : "not ready";
      if (summary.requester_balance !== null && summary.requester_balance !== undefined) {{
        balanceEl.textContent = `Balance ${{summary.requester_balance}}`;
      }}
      modelRouteEl.textContent = `Model workers ${{routing.live_eligible_node_count ?? 0}}`;
      coordinatorStateEl.textContent = coordinator.ok ? "Coordinator online" : "Coordinator offline";
      if (!summary.can_send && summary.recommended_next_action) {{
        blockedBanner.className = "banner visible";
        blockedBanner.textContent = `Safe action: ${{summary.recommended_next_action}}`;
      }} else {{
        blockedBanner.className = "banner";
        blockedBanner.textContent = "";
      }}
      commandHintEl.className = command ? "command visible" : "command";
      commandHintEl.textContent = command;
    }}
    function render(report) {{
      const summary = report.summary || {{}};
      const turns = report.turns || [];
      const status = report.status || "unknown";
      const nextAction = summary.recommended_next_action || "";
      stateBadge.className = badgeClass(status);
      stateBadge.textContent = status;
      if (!balanceEl.textContent) balanceEl.textContent = formatBalance(turns);
      nextActionEl.textContent = nextAction ? `Next ${{nextAction}}` : "";
      const blocked = status === "blocked" || status === "fail" || blockedActions.has(nextAction);
      if (blocked) {{
        blockedBanner.className = "banner visible";
        blockedBanner.textContent = `Safe action: ${{nextAction || "inspect_chat_session_status"}}`;
      }} else if (!blockedBanner.textContent) {{
        blockedBanner.className = "banner";
      }}
      if (blocked && !commandHintEl.textContent) {{
        commandHintEl.className = "command";
      }}
      turnsEl.replaceChildren();
      if (!turns.length) {{
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No turns yet.";
        turnsEl.append(empty);
        return;
      }}
      turns.forEach((turn) => {{
        const userNode = document.createElement("article");
        userNode.className = `turn user status-${{turn.status || "unknown"}}`;
        const userMeta = document.createElement("div");
        userMeta.className = "meta";
        const turnBadge = document.createElement("span");
        turnBadge.className = `badge ${{turn.status || ""}}`;
        turnBadge.textContent = turn.status || "unknown";
        const turnId = document.createElement("span");
        turnId.textContent = turn.turn_id || "";
        const jobStatus = document.createElement("span");
        jobStatus.textContent = turn.job_status || "";
        userMeta.append(turnBadge, turnId, jobStatus);
        const userBubble = document.createElement("div");
        userBubble.className = "bubble";
        userBubble.textContent = turn.prompt || turn.prompt_preview || "";
        userNode.append(userMeta, userBubble);
        turnsEl.append(userNode);
        if (turn.answer || turn.errors?.length) {{
          const assistantNode = document.createElement("article");
          assistantNode.className = `turn assistant status-${{turn.status || "unknown"}}`;
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = [turn.model, turn.worker_node_id_redacted].filter(Boolean).join(" - ");
          const bubble = document.createElement("div");
          bubble.className = "bubble";
          bubble.textContent = turn.answer || turn.errors.join("\\n");
          assistantNode.append(meta, bubble);
          turnsEl.append(assistantNode);
        }} else {{
          const pendingNode = document.createElement("article");
          pendingNode.className = `turn assistant status-${{turn.status || "unknown"}}`;
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = turn.model || "";
          const bubble = document.createElement("div");
          bubble.className = "bubble";
          bubble.textContent = turn.status === "submitted" ? "Waiting for result." : "";
          pendingNode.append(meta, bubble);
          turnsEl.append(pendingNode);
        }}
      }});
      turnsEl.scrollTop = turnsEl.scrollHeight;
    }}
    async function refreshReadiness() {{
      try {{
        const [readinessResponse, modelsResponse] = await Promise.all([
          fetch(`/api/chat/readiness${{modelQuery()}}`),
          fetch(`/api/chat/models${{modelQuery()}}`)
        ]);
        renderReadiness(await readinessResponse.json());
        renderModelCatalog(await modelsResponse.json());
      }} catch (_error) {{
        readinessBadge.className = badgeClass("fail");
        readinessBadge.textContent = "readiness failed";
      }}
    }}
    async function request(path, options = {{}}) {{
      setBusy(true);
      try {{
        const response = await fetch(path, options);
        const report = await response.json();
        renderApiError(report, response.status);
        render(report);
        await refreshReadiness();
      }} finally {{
        setBusy(false);
      }}
    }}
    async function refreshTranscript() {{ await request("/api/session/transcript"); }}
    document.querySelector("#statusButton").onclick = refreshTranscript;
    safeActionButton.onclick = async () => {{
      const path = safeActionEndpoints[currentSafeAction];
      if (!path) return;
      await request(path, {{ method: "POST" }});
      await refreshTranscript();
    }};
    document.querySelector("#syncButton").onclick = async () => {{ await request("/api/session/sync", {{ method: "POST" }}); await refreshTranscript(); }};
    document.querySelector("#resumeButton").onclick = () => request("/api/session/resume-dry-run", {{ method: "POST" }});
    document.querySelector("#resetButton").onclick = () => request("/api/session/reset-dry-run", {{ method: "POST" }});
    document.querySelector("#archiveButton").onclick = () => request("/api/session/archive-dry-run", {{ method: "POST" }});
    modelSelectEl.onchange = refreshReadiness;
    document.querySelector("#chatForm").onsubmit = async (event) => {{
      event.preventDefault();
      const prompt = promptEl.value.trim();
      if (!prompt) return;
      promptEl.value = "";
      await request("/api/chat/continue", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ prompt, model: selectedModel() }})
      }});
      await refreshTranscript();
    }};
    refreshReadiness();
    refreshTranscript();
  </script>
</body>
</html>
"""


def _read_json_request(handler: BaseHTTPRequestHandler, *, max_bytes: int) -> dict[str, Any] | None:
    try:
        content_length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        _json_response(handler, 400, {"ok": False, "status": "fail", "error": "invalid content length"})
        return None
    if content_length == 0:
        return {}
    if content_length > max_bytes:
        _json_response(
            handler,
            413,
            {
                "ok": False,
                "status": "fail",
                "error": f"request body exceeds max_request_bytes ({max_bytes})",
            },
        )
        return None
    try:
        return json.loads(handler.rfile.read(content_length).decode("utf-8"))
    except json.JSONDecodeError as exc:
        _json_response(handler, 400, {"ok": False, "status": "fail", "error": f"invalid JSON: {exc.msg}"})
        return None


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, status: int, markup: str) -> None:
    body = markup.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _request_error_payload(
    error: str,
    *,
    recommended_next_action: str = "inspect_chat_gateway_response",
) -> dict[str, Any]:
    return {
        "schema": CHAT_GATEWAY_REPORT_SCHEMA,
        "ok": False,
        "status": "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "errors": [error],
        "summary": {"recommended_next_action": recommended_next_action},
        "action_hint": _static_action_hint(recommended_next_action),
    }


def _error_payload(error: str, *, config: ChatGatewayConfig | None = None) -> dict[str, Any]:
    category = _category_from_error_text(error)
    action_id = _action_id_for_category(category) if category else "inspect_chat_gateway_response"
    action_hint = (
        _gateway_action_hint(config=config, action_id=action_id, category=category, requester_balance=None)
        if config is not None
        else _static_action_hint(action_id)
    )
    return {
        "schema": CHAT_GATEWAY_REPORT_SCHEMA,
        "ok": False,
        "status": "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error_category": category,
        "error": error,
        "errors": [error],
        "summary": {
            "recommended_next_action": action_id,
            "error_category": category,
        },
        "ui_message": _gateway_ui_message(category, action_id),
        "action_hint": action_hint,
    }


def _with_gateway_error_clarity(report: dict[str, Any], config: ChatGatewayConfig) -> dict[str, Any]:
    summary = dict(report.get("summary") or {})
    if str(report.get("status") or "") not in {"blocked", "fail"} and str(summary.get("status") or "") not in {
        "blocked",
        "fail",
    }:
        return report
    category = _continue_error_category(report)
    action_id = _action_id_for_category(category, fallback=str(summary.get("recommended_next_action") or ""))
    enriched = dict(report)
    summary["error_category"] = category
    summary["recommended_next_action"] = action_id
    enriched["summary"] = summary
    enriched["error_category"] = category
    enriched["ui_message"] = _gateway_ui_message(category, action_id)
    enriched["action_hint"] = _gateway_action_hint(
        config=config,
        action_id=action_id,
        category=category,
        requester_balance=None,
    )
    return enriched


def _continue_error_category(report: dict[str, Any]) -> str | None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    if summary.get("blocked_reason") == "unresolved_session_turns":
        return "unresolved_session"
    if summary.get("blocked_reason") == "sync_failed":
        return _category_from_error_text(_joined_report_errors(report)) or "coordinator_unreachable"
    text_category = _category_from_error_text(_joined_report_errors(report))
    if text_category:
        return text_category
    action = str(summary.get("recommended_next_action") or "")
    if action in {"run_session_resume_dry_run", "run_session_sync_then_resume_dry_run", "wait_for_worker_result"}:
        return "unresolved_session"
    if action == "grant_requester_credits":
        return "insufficient_credits"
    if action == "check_coordinator_reachability":
        return "coordinator_unreachable"
    if action == "check_live_ollama_workers":
        return "no_model_worker"
    return None


def _readiness_error_category(
    *,
    session: dict[str, Any],
    snapshot_info: dict[str, Any],
    requester: dict[str, Any],
    routing: dict[str, Any],
) -> str | None:
    if session["blocked"]:
        return "unresolved_session"
    if not snapshot_info["ok"]:
        return "coordinator_unreachable"
    if not requester["credits_sufficient"]:
        return "insufficient_credits"
    if routing["live_eligible_node_count"] <= 0:
        return "no_model_worker"
    return None


def _category_from_error_text(error_text: str) -> str | None:
    text = error_text.lower()
    if not text:
        return None
    if "timed out" in text or "timeout" in text or "did not verify" in text:
        return "request_timeout"
    if "negative" in text or "credit" in text:
        return "insufficient_credits"
    if "connection" in text or "refused" in text or "unreachable" in text or "urlerror" in text:
        return "coordinator_unreachable"
    if "ollama" in text or "worker" in text or "model" in text:
        return "no_model_worker"
    return None


def _joined_report_errors(report: dict[str, Any]) -> str:
    chunks: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            errors = value.get("errors")
            if isinstance(errors, list):
                chunks.extend(str(error) for error in errors)
            for key in ("summary", "session", "sync", "status_before", "status_after_sync"):
                collect(value.get(key))
            latest_turn = value.get("latest_turn")
            collect(latest_turn)
            return
        if isinstance(value, list):
            for item in value:
                collect(item)

    collect(report)
    return "\n".join(chunks)


def _action_id_for_category(category: str | None, fallback: str = "") -> str:
    if category == "unresolved_session":
        if fallback in {"run_session_sync_then_resume_dry_run", "wait_for_worker_result", "run_session_sync"}:
            return fallback
        return "run_session_resume_dry_run"
    if category == "coordinator_unreachable":
        return "start_or_check_local_coordinator"
    if category == "insufficient_credits":
        return "grant_requester_credits"
    if category == "no_model_worker":
        return "wait_for_model_capable_worker_or_change_model"
    if category == "request_timeout":
        return "run_session_sync"
    if category == "invalid_model":
        return "choose_valid_model"
    return fallback or "inspect_chat_gateway_response"


def _gateway_action_hint(
    *,
    config: ChatGatewayConfig,
    action_id: str,
    category: str | None,
    requester_balance: int | None,
) -> dict[str, Any]:
    if action_id == "choose_valid_model":
        return _static_action_hint(action_id, category=category)
    connection = _resolve_gateway_connection(config)
    commands = _readiness_commands(
        config=config,
        connection=connection,
        action_id=action_id,
        requester_balance=requester_balance,
    )
    primary_command = commands[0]["command"] if commands else None
    return {
        "id": action_id,
        "error_category": category,
        "partner_required": False,
        "safe": True,
        "primary_command": primary_command,
        "commands": commands,
    }


def _static_action_hint(action_id: str, *, category: str | None = None) -> dict[str, Any]:
    return {
        "id": action_id,
        "error_category": category,
        "partner_required": False,
        "safe": True,
        "primary_command": None,
        "commands": [],
    }


def _gateway_ui_message(category: str | None, action_id: str) -> str | None:
    if category == "unresolved_session":
        return "A previous turn is unresolved. Sync or preview resume before spending more credits."
    if category == "coordinator_unreachable":
        return "The coordinator is not reachable from this gateway."
    if category == "insufficient_credits":
        return "The requester account does not have enough credits for this turn."
    if category == "no_model_worker":
        return "No live worker currently advertises the selected model."
    if category == "invalid_model":
        return "Choose a valid model name from the catalog."
    if category == "request_timeout":
        return "The request timed out before a verified answer arrived."
    if action_id and action_id != "continue_chat_session":
        return f"Safe action: {action_id}"
    return None


def _redact_report(report: dict[str, Any], config: ChatGatewayConfig) -> dict[str, Any]:
    secrets = [value for value in [config.admission_token] if value]
    return _redact_value(report, secrets=secrets)


def _redact_value(value: Any, *, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets=secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "<redacted>")
        return redacted
    return value


def _resolve_gateway_connection(config: ChatGatewayConfig) -> dict[str, Any]:
    invite = load_alpha_invite(config.invite_path.expanduser()) if config.invite_path else None
    resolved_url = config.coordinator_url or (invite.coordinator if invite else DEFAULT_COORDINATOR_URL)
    token = config.admission_token or (invite.admission_token if invite else None)
    return {
        "coordinator_url": _safe_url(resolved_url.rstrip("/")),
        "token": token,
        "invite_summary": invite.public_summary() if invite else None,
    }


def _fetch_snapshot(*, connection: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    try:
        client = CoordinatorClient(
            connection["coordinator_url"],
            admission_token=connection["token"],
            timeout_seconds=timeout_seconds,
        )
        return {"ok": True, "snapshot": client.snapshot(), "error": None, "errors": []}
    except Exception as exc:
        error = _format_gateway_exception(exc)
        return {"ok": False, "snapshot": None, "error": error, "errors": [error]}


def _readiness_session(transcript: dict[str, Any]) -> dict[str, Any]:
    summary = transcript.get("summary") if isinstance(transcript.get("summary"), dict) else {}
    status = str(transcript.get("status") or summary.get("status") or "unknown")
    submitted = _int_or_zero(summary.get("submitted_turns"))
    failed = _int_or_zero(summary.get("failed_turns"))
    recommended = str(summary.get("recommended_next_action") or "")
    blocked = status in {"blocked", "fail"} or submitted > 0 or failed > 0
    return {
        "ok": bool(transcript.get("ok", status == "no_session")),
        "status": status,
        "blocked": blocked,
        "turn_count": _int_or_zero(summary.get("turn_count")),
        "completed_turns": _int_or_zero(summary.get("completed_turns")),
        "submitted_turns": submitted,
        "failed_turns": failed,
        "recommended_next_action": recommended or "continue_chat_session",
    }


def _readiness_requester(*, snapshot: dict[str, Any] | None, account_id: str, job_cost: int) -> dict[str, Any]:
    balances = (((snapshot or {}).get("credit_ledger") or {}).get("summary") or {}).get("balances") or {}
    balance = _int_or_none(balances.get(account_id))
    if snapshot is not None and balance is None:
        balance = 0
    effective_balance = balance if balance is not None else 0
    return {
        "account_id": account_id,
        "balance": balance,
        "job_cost": job_cost,
        "credits_sufficient": effective_balance >= job_cost,
    }


def _model_catalog_from_snapshot(*, snapshot: dict[str, Any] | None, selected_model: str) -> dict[str, Any]:
    nodes = (snapshot or {}).get("nodes") or []
    live_node_count = 0
    legacy_node_count = 0
    models: dict[str, dict[str, Any]] = {}

    node_items = nodes if isinstance(nodes, list) else []
    for node in node_items:
        if not isinstance(node, dict):
            continue
        liveness = str(node.get("liveness_status") or "unknown")
        if liveness == "live":
            live_node_count += 1
        supported = [str(item) for item in node.get("supported_job_types") or [] if isinstance(item, str)]
        advertised_models = [str(item) for item in node.get("ollama_models") or [] if isinstance(item, str)]
        if not supported:
            legacy_node_count += 1
        if "inference.chat.v1" not in supported:
            continue
        for model in advertised_models:
            entry = models.setdefault(
                model,
                {
                    "model": model,
                    "worker_count": 0,
                    "live_worker_count": 0,
                    "sendable": False,
                    "node_samples": [],
                },
            )
            entry["worker_count"] += 1
            if liveness == "live":
                entry["live_worker_count"] += 1
                entry["sendable"] = True
            if len(entry["node_samples"]) < 5:
                entry["node_samples"].append(
                    {
                        "node_id_redacted": _redact_node_id(node.get("node_id")),
                        "liveness_status": liveness,
                        "software": software_metadata_public_view(node.get("software")),
                    }
                )

    warnings = []
    if legacy_node_count:
        warnings.append("legacy_nodes_without_supported_job_types")
    model_list = sorted(models.values(), key=lambda item: (not item["sendable"], item["model"]))
    selected = next((item for item in model_list if item["model"] == selected_model), None)
    recommended_model = (
        selected_model
        if selected and selected["sendable"]
        else next((item["model"] for item in model_list if item["sendable"]), None)
    )
    return {
        "summary": {
            "selected_model": selected_model,
            "selected_model_sendable": bool(selected and selected["sendable"]),
            "recommended_model": recommended_model,
            "available_model_count": len(model_list),
            "sendable_model_count": sum(1 for item in model_list if item["sendable"]),
            "live_node_count": live_node_count,
            "legacy_node_count": legacy_node_count,
        },
        "models": model_list,
        "warnings": warnings,
    }


def _model_routing_from_catalog(*, catalog: dict[str, Any], model: str) -> dict[str, Any]:
    model_entry = next((item for item in catalog["models"] if item["model"] == model), None)
    return {
        "model": model,
        "live_node_count": catalog["summary"]["live_node_count"],
        "eligible_node_count": _int_or_zero((model_entry or {}).get("worker_count")),
        "live_eligible_node_count": _int_or_zero((model_entry or {}).get("live_worker_count")),
        "legacy_node_count": catalog["summary"]["legacy_node_count"],
        "eligible_node_samples": (model_entry or {}).get("node_samples") or [],
        "warnings": list(catalog["warnings"]),
    }


def _model_catalog_recommended_next_action(
    *,
    coordinator_ok: bool,
    selected_model_sendable: bool,
    recommended_model: str | None,
) -> str:
    if not coordinator_ok:
        return "start_or_check_local_coordinator"
    if selected_model_sendable:
        return "continue_chat_session"
    if recommended_model is not None:
        return "change_model_to_recommended_model"
    return "wait_for_model_capable_worker_or_change_model"


def _snapshot_status_summary(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    status = snapshot.get("status") if isinstance(snapshot.get("status"), dict) else {}
    return {
        "coordinator_id_redacted": _redact_node_id(status.get("coordinator_id")),
        "jobs": status.get("jobs"),
        "live_nodes": status.get("live_nodes"),
        "known_nodes": status.get("known_nodes"),
        "pending_jobs": status.get("pending_jobs"),
        "queued_jobs": status.get("queued_jobs"),
    }


def _readiness_recommended_next_action(
    *,
    session: dict[str, Any],
    coordinator_ok: bool,
    credits_sufficient: bool,
    live_eligible_node_count: int,
) -> str:
    if session["blocked"]:
        if session["submitted_turns"] > 0:
            return "run_session_sync"
        if session["failed_turns"] > 0:
            return "run_session_resume_dry_run"
        return session.get("recommended_next_action") or "inspect_chat_session_status"
    if not coordinator_ok:
        return "start_or_check_local_coordinator"
    if not credits_sufficient:
        return "grant_requester_credits"
    if live_eligible_node_count <= 0:
        return "wait_for_model_capable_worker_or_change_model"
    return "continue_chat_session"


def _readiness_action_hint(
    *,
    config: ChatGatewayConfig,
    connection: dict[str, Any],
    action_id: str,
    requester_balance: int | None,
    error_category: str | None = None,
) -> dict[str, Any]:
    commands = _readiness_commands(
        config=config,
        connection=connection,
        action_id=action_id,
        requester_balance=requester_balance,
    )
    primary_command = commands[0]["command"] if commands else None
    return {
        "id": action_id,
        "error_category": error_category,
        "partner_required": False,
        "safe": True,
        "primary_command": primary_command,
        "commands": commands,
    }


def _readiness_commands(
    *,
    config: ChatGatewayConfig,
    connection: dict[str, Any],
    action_id: str,
    requester_balance: int | None,
) -> list[dict[str, Any]]:
    if action_id in {"run_session_sync", "wait_for_worker_result"}:
        return [
            _command(
                "sync_session",
                "read_only_remote_updates_local_session",
                _base_chat_session_argv(config, "session-sync", connection) + ["--json"],
            )
        ]
    if action_id == "run_session_sync_then_resume_dry_run":
        return [
            _command(
                "sync_session",
                "read_only_remote_updates_local_session",
                _base_chat_session_argv(config, "session-sync", connection) + ["--json"],
            ),
            _command(
                "preview_resume",
                "dry_run_no_credit_spend",
                _base_chat_session_argv(config, "session-resume", connection)
                + _chat_continue_options(config)
                + ["--dry-run", "--json"],
            ),
        ]
    if action_id == "run_session_resume_dry_run":
        return [
            _command(
                "preview_resume",
                "dry_run_no_credit_spend",
                _base_chat_session_argv(config, "session-resume", connection)
                + _chat_continue_options(config)
                + ["--dry-run", "--json"],
            )
        ]
    if action_id == "start_or_check_local_coordinator":
        return [
            _command(
                "check_coordinator",
                "read_only_network_check",
                _base_node_status_argv(config, connection),
            )
        ]
    if action_id == "grant_requester_credits":
        needed = max(1, config.job_cost - (requester_balance or 0))
        commands = [
            _command(
                "inspect_requester_credits",
                "read_only_credit_report",
                _base_operator_credits_argv(config, connection) + ["--json"],
            )
        ]
        grant_argv = _base_operator_grant_argv(config, connection, credits=needed)
        commands.append(
            _command(
                "preview_credit_grant",
                "dry_run_requires_private_operator_config",
                grant_argv + ["--operator-config", "<private-operator-config.json>", "--dry-run", "--json"],
            )
        )
        return commands
    if action_id == "wait_for_model_capable_worker_or_change_model":
        return [
            _command(
                "check_node_status",
                "read_only_model_route_check",
                _base_node_status_argv(config, connection),
            )
        ]
    return []


def _base_chat_session_argv(config: ChatGatewayConfig, subcommand: str, connection: dict[str, Any]) -> list[str]:
    return [
        "python",
        "-m",
        "chatp2p.cli",
        "chat",
        subcommand,
        "--out",
        str(config.out_dir.expanduser().resolve()),
        "--session-id",
        config.session_id,
        *_connection_argv(config, connection),
        "--client-timeout-seconds",
        str(config.client_timeout_seconds),
    ]


def _chat_continue_options(config: ChatGatewayConfig) -> list[str]:
    argv = [
        "--model",
        config.model,
        "--requester-account-id",
        config.requester_account_id,
        "--job-cost",
        str(config.job_cost),
        "--reward",
        str(config.reward),
        "--ttl-seconds",
        str(config.ttl_seconds),
        "--timeout-seconds",
        str(config.timeout_seconds),
        "--poll-interval",
        str(config.poll_interval),
        "--max-context-turns",
        str(config.max_context_turns),
    ]
    if config.system is not None:
        argv.extend(["--system", config.system])
    if config.temperature is not None:
        argv.extend(["--temperature", str(config.temperature)])
    if config.max_tokens is not None:
        argv.extend(["--max-tokens", str(config.max_tokens)])
    if config.no_wait:
        argv.append("--no-wait")
    return argv


def _base_node_status_argv(config: ChatGatewayConfig, connection: dict[str, Any]) -> list[str]:
    return [
        "python",
        "-m",
        "chatp2p.cli",
        "node",
        "status",
        *_connection_argv(config, connection),
    ]


def _base_operator_credits_argv(config: ChatGatewayConfig, connection: dict[str, Any]) -> list[str]:
    return [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "credits",
        "--out",
        str((config.out_dir.expanduser().resolve().parent / "operator-credits")),
        "--requester-account-id",
        config.requester_account_id,
        "--min-requester-balance",
        str(config.job_cost),
        *_connection_argv(config, connection),
        "--client-timeout-seconds",
        str(config.client_timeout_seconds),
    ]


def _base_operator_grant_argv(
    config: ChatGatewayConfig,
    connection: dict[str, Any],
    *,
    credits: int,
) -> list[str]:
    return [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "grant-requester-credits",
        "--out",
        str((config.out_dir.expanduser().resolve().parent / "operator-credit-grant")),
        "--requester-account-id",
        config.requester_account_id,
        "--credits",
        str(credits),
        *_connection_argv(config, connection),
        "--client-timeout-seconds",
        str(config.client_timeout_seconds),
    ]


def _connection_argv(config: ChatGatewayConfig, connection: dict[str, Any]) -> list[str]:
    if config.invite_path is not None:
        return ["--invite", str(config.invite_path.expanduser().resolve())]
    return ["--coordinator", str(connection["coordinator_url"])]


def _command(command_id: str, side_effect: str, argv: list[str]) -> dict[str, Any]:
    return {
        "id": command_id,
        "side_effect": side_effect,
        "argv": argv,
        "command": _command_text(argv),
    }


def _command_text(argv: list[str]) -> str:
    return " ".join(_powershell_quote(part) for part in argv)


def _powershell_quote(value: str) -> str:
    if value and all(char.isalnum() or char in ".:/\\-_=" for char in value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return url


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0


def _format_gateway_exception(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _transcript_turn(turn: dict[str, Any]) -> dict[str, Any]:
    worker_node_id = turn.get("worker_node_id")
    return {
        "turn_id": turn.get("turn_id"),
        "turn_index": turn.get("turn_index"),
        "created_at": turn.get("created_at"),
        "status": turn.get("status"),
        "prompt": turn.get("prompt"),
        "answer": turn.get("answer"),
        "model": turn.get("model"),
        "job_id": turn.get("job_id"),
        "job_status": turn.get("job_status"),
        "worker_node_id_redacted": _redact_node_id(worker_node_id),
        "requester_balance_after": turn.get("requester_balance_after"),
        "recommended_next_action": turn.get("recommended_next_action"),
        "retry_of_turn_id": turn.get("retry_of_turn_id"),
        "retry_attempt": turn.get("retry_attempt"),
        "errors": list(turn.get("errors") or []),
    }


def _redact_node_id(node_id: Any) -> str | None:
    if not isinstance(node_id, str) or not node_id:
        return None
    if len(node_id) <= 12:
        return node_id
    return f"{node_id[:12]}..."


def _safe_config(config: ChatGatewayConfig) -> dict[str, Any]:
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
        "host": config.host,
        "port": config.port,
        "max_request_bytes": config.max_request_bytes,
        "remote_side_effect": "localhost_http_safe_chat_continue",
    }


def _validate_config(config: ChatGatewayConfig) -> None:
    if not config.session_id.strip():
        raise ValueError("--session-id must be non-empty")
    if not config.model.strip():
        raise ValueError("--model must be non-empty")
    if not config.requester_account_id.strip():
        raise ValueError("--requester-account-id must be non-empty")
    if config.host != DEFAULT_CHAT_GATEWAY_HOST:
        raise ValueError("Chat Gateway V0 only supports --host 127.0.0.1")
    if not 0 <= config.port <= 65535:
        raise ValueError("--port must be between 0 and 65535")
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
    if config.max_request_bytes < 1:
        raise ValueError("--max-request-bytes must be at least 1")

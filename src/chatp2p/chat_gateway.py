"""Local-only HTTP gateway for safe ChatP2P chat sessions."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .chat_session import (
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
from .runtime_metadata import collect_software_metadata, software_metadata_public_view


CHAT_GATEWAY_REPORT_SCHEMA = "chatp2p.chat-gateway-report.v1"
DEFAULT_CHAT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_CHAT_GATEWAY_PORT = 8787
DEFAULT_CHAT_GATEWAY_MAX_REQUEST_BYTES = 16_384


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
            _json_response(self, 404, {"ok": False, "status": "fail", "error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/session/sync":
                _json_response(self, 200, _sync_payload(config))
                return
            if parsed.path == "/api/session/resume-dry-run":
                _json_response(self, 200, _resume_dry_run_payload(config))
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
                        {
                            "ok": False,
                            "status": "fail",
                            "error": "prompt must be a non-empty string",
                        },
                    )
                    return
                _json_response(self, 200, _continue_payload(config, prompt=prompt))
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
            "session_sync": "/api/session/sync",
            "session_resume_dry_run": "/api/session/resume-dry-run",
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


def _continue_payload(config: ChatGatewayConfig, *, prompt: str) -> dict[str, Any]:
    try:
        return _redact_report(
            run_chat_session_continue(
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
            ),
            config,
        )
    except Exception as exc:
        return _error_payload(f"{type(exc).__name__}: {exc}")


def _render_gateway_html(config: ChatGatewayConfig) -> str:
    title = html.escape(config.session_id)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ChatP2P {title}</title>
  <style>
    body {{ margin: 0; font-family: Segoe UI, system-ui, sans-serif; background: #f6f7f9; color: #17191c; }}
    main {{ max-width: 920px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
    h1 {{ font-size: 22px; margin: 0; font-weight: 650; }}
    button, textarea {{ font: inherit; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    button {{ border: 1px solid #cbd1d8; background: #fff; color: #111827; border-radius: 6px; padding: 8px 12px; cursor: pointer; }}
    button.primary {{ background: #0f766e; border-color: #0f766e; color: #fff; }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    #status {{ border: 1px solid #d7dde5; background: #fff; border-radius: 8px; padding: 12px; min-height: 56px; }}
    #turns {{ display: grid; gap: 10px; }}
    .turn {{ border: 1px solid #d7dde5; background: #fff; border-radius: 8px; padding: 12px; white-space: pre-wrap; }}
    .meta {{ color: #5b6573; font-size: 13px; margin-bottom: 4px; }}
    form {{ display: grid; gap: 10px; }}
    textarea {{ min-height: 96px; resize: vertical; border: 1px solid #cbd1d8; border-radius: 8px; padding: 10px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>ChatP2P</h1>
      <div class="actions">
        <button id="statusButton" type="button">Status</button>
        <button id="syncButton" type="button">Sync</button>
        <button id="resumeButton" type="button">Resume Dry Run</button>
      </div>
    </header>
    <section id="status"></section>
    <section id="turns"></section>
    <form id="chatForm">
      <textarea id="prompt" name="prompt" autocomplete="off"></textarea>
      <button class="primary" type="submit">Send</button>
    </form>
  </main>
  <script>
    const statusEl = document.querySelector("#status");
    const turnsEl = document.querySelector("#turns");
    const promptEl = document.querySelector("#prompt");
    const buttons = Array.from(document.querySelectorAll("button"));
    function setBusy(value) {{ buttons.forEach((button) => button.disabled = value); }}
    function render(report) {{
      const summary = report.summary || {{}};
      statusEl.textContent = `${{report.status || "unknown"}} | ${{summary.recommended_next_action || ""}}`;
      turnsEl.innerHTML = "";
      (report.turns || []).forEach((turn) => {{
        const node = document.createElement("article");
        node.className = "turn";
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `${{turn.turn_id || ""}} | ${{turn.status || ""}}`;
        const body = document.createElement("div");
        body.textContent = turn.prompt_preview || turn.prompt || "";
        node.append(meta, body);
        turnsEl.append(node);
      }});
    }}
    async function request(path, options = {{}}) {{
      setBusy(true);
      try {{
        const response = await fetch(path, options);
        render(await response.json());
      }} finally {{
        setBusy(false);
      }}
    }}
    document.querySelector("#statusButton").onclick = () => request("/api/session/status");
    document.querySelector("#syncButton").onclick = () => request("/api/session/sync", {{ method: "POST" }});
    document.querySelector("#resumeButton").onclick = () => request("/api/session/resume-dry-run", {{ method: "POST" }});
    document.querySelector("#chatForm").onsubmit = async (event) => {{
      event.preventDefault();
      const prompt = promptEl.value.trim();
      if (!prompt) return;
      promptEl.value = "";
      await request("/api/chat/continue", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ prompt }})
      }});
    }};
    request("/api/session/status");
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


def _error_payload(error: str) -> dict[str, Any]:
    return {
        "schema": CHAT_GATEWAY_REPORT_SCHEMA,
        "ok": False,
        "status": "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "errors": [error],
        "summary": {"recommended_next_action": "inspect_chat_gateway_response"},
    }


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

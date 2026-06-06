"""Local-only HTTP gateway for safe ChatP2P chat sessions."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

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
            if parsed.path == "/api/session/transcript":
                _json_response(self, 200, _transcript_payload(config))
                return
            if parsed.path == "/api/chat/readiness":
                _json_response(self, 200, _readiness_payload(config))
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
            "session_transcript": "/api/session/transcript",
            "chat_readiness": "/api/chat/readiness",
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


def _readiness_payload(config: ChatGatewayConfig) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    errors: list[str] = []
    warnings: list[str] = []
    connection = _resolve_gateway_connection(config)
    transcript = _transcript_payload(config)
    session = _readiness_session(transcript)

    snapshot: dict[str, Any] | None = None
    coordinator_error: str | None = None
    try:
        client = CoordinatorClient(
            connection["coordinator_url"],
            admission_token=connection["token"],
            timeout_seconds=config.client_timeout_seconds,
        )
        snapshot = client.snapshot()
    except Exception as exc:
        coordinator_error = _format_gateway_exception(exc)
        errors.append(coordinator_error)

    requester = _readiness_requester(snapshot=snapshot, account_id=config.requester_account_id, job_cost=config.job_cost)
    routing = _readiness_model_routing(snapshot=snapshot, model=config.model)
    warnings.extend(routing["warnings"])

    can_send = (
        not session["blocked"]
        and snapshot is not None
        and requester["credits_sufficient"]
        and routing["live_eligible_node_count"] > 0
    )
    recommended_next_action = _readiness_recommended_next_action(
        session=session,
        coordinator_ok=snapshot is not None,
        credits_sufficient=requester["credits_sufficient"],
        live_eligible_node_count=routing["live_eligible_node_count"],
    )
    action_hint = _readiness_action_hint(
        config=config,
        connection=connection,
        action_id=recommended_next_action,
        requester_balance=requester["balance"],
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
                "coordinator_reachable": snapshot is not None,
                "requester_balance": requester["balance"],
                "job_cost": config.job_cost,
                "credits_sufficient": requester["credits_sufficient"],
                "model": config.model,
                "live_eligible_node_count": routing["live_eligible_node_count"],
                "session_blocked": session["blocked"],
                "recommended_next_action": recommended_next_action,
                "suggested_command": action_hint["primary_command"],
            },
            "action_hint": action_hint,
            "coordinator": {
                "ok": snapshot is not None,
                "url": connection["coordinator_url"],
                "error": coordinator_error,
                "status": _snapshot_status_summary(snapshot),
            },
            "requester": requester,
            "model_routing": routing,
            "session": session,
            "invite": connection["invite_summary"],
            "warnings": warnings,
            "errors": errors,
        },
        config,
    )


def _render_gateway_html(config: ChatGatewayConfig) -> str:
    title = html.escape(config.session_id)
    model = html.escape(config.model)
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
    button, textarea {{ font: inherit; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    button {{ border: 1px solid #cbd1d8; background: #fff; color: #111827; border-radius: 6px; padding: 8px 12px; cursor: pointer; min-height: 38px; }}
    button.primary {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    #status {{ display: grid; gap: 8px; max-width: 1040px; margin: 0 auto; padding: 14px 20px 0; }}
    .status-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--muted); font-size: 14px; }}
    .badge {{ display: inline-flex; align-items: center; min-height: 24px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); background: #fff; color: var(--muted); font-size: 12px; font-weight: 650; }}
    .badge.pass {{ color: #047857; border-color: #a7f3d0; background: #ecfdf5; }}
    .badge.fail, .badge.blocked {{ color: var(--bad); border-color: #fecaca; background: #fef2f2; }}
    .badge.submitted {{ color: var(--wait); border-color: #bfdbfe; background: #eff6ff; }}
    .banner {{ display: none; border: 1px solid #fed7aa; background: #fff7ed; color: var(--warn); border-radius: 8px; padding: 10px 12px; }}
    .banner.visible {{ display: block; }}
    .command {{ display: none; border: 1px solid #d1d5db; background: #111827; color: #f9fafb; border-radius: 8px; padding: 10px 12px; font-family: Consolas, ui-monospace, monospace; font-size: 12px; overflow-wrap: anywhere; }}
    .command.visible {{ display: block; }}
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
          <button id="syncButton" type="button">Sync</button>
          <button id="resumeButton" type="button">Resume Dry Run</button>
        </div>
      </div>
    </header>
    <section id="status">
      <div class="status-row">
        <span id="stateBadge" class="badge">loading</span>
        <span id="readinessBadge" class="badge">readiness</span>
        <span id="balance"></span>
        <span id="modelRoute"></span>
        <span id="coordinatorState"></span>
        <span id="nextAction"></span>
      </div>
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
    const blockedBanner = document.querySelector("#blockedBanner");
    const commandHintEl = document.querySelector("#commandHint");
    const turnsEl = document.querySelector("#turns");
    const promptEl = document.querySelector("#prompt");
    const sendButton = document.querySelector("#sendButton");
    const buttons = Array.from(document.querySelectorAll("button"));
    const blockedActions = new Set(["run_session_resume_dry_run", "run_session_sync_then_resume_dry_run", "wait_for_worker_result", "resume_failed_turn", "sync_session_then_resume_failed_turn"]);
    function setBusy(value) {{
      buttons.forEach((button) => button.disabled = value);
      sendButton.textContent = value ? "Sending" : "Send";
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
    function renderReadiness(report) {{
      const summary = report.summary || {{}};
      const routing = report.model_routing || {{}};
      const coordinator = report.coordinator || {{}};
      const actionHint = report.action_hint || {{}};
      const command = actionHint.primary_command || "";
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
        const response = await fetch("/api/chat/readiness");
        renderReadiness(await response.json());
      }} catch (_error) {{
        readinessBadge.className = badgeClass("fail");
        readinessBadge.textContent = "readiness failed";
      }}
    }}
    async function request(path, options = {{}}) {{
      setBusy(true);
      try {{
        const response = await fetch(path, options);
        render(await response.json());
        await refreshReadiness();
      }} finally {{
        setBusy(false);
      }}
    }}
    async function refreshTranscript() {{ await request("/api/session/transcript"); }}
    document.querySelector("#statusButton").onclick = refreshTranscript;
    document.querySelector("#syncButton").onclick = async () => {{ await request("/api/session/sync", {{ method: "POST" }}); await refreshTranscript(); }};
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


def _resolve_gateway_connection(config: ChatGatewayConfig) -> dict[str, Any]:
    invite = load_alpha_invite(config.invite_path.expanduser()) if config.invite_path else None
    resolved_url = config.coordinator_url or (invite.coordinator if invite else DEFAULT_COORDINATOR_URL)
    token = config.admission_token or (invite.admission_token if invite else None)
    return {
        "coordinator_url": _safe_url(resolved_url.rstrip("/")),
        "token": token,
        "invite_summary": invite.public_summary() if invite else None,
    }


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


def _readiness_model_routing(*, snapshot: dict[str, Any] | None, model: str) -> dict[str, Any]:
    nodes = (snapshot or {}).get("nodes") or []
    eligible_node_count = 0
    live_eligible_node_count = 0
    live_node_count = 0
    legacy_node_count = 0
    eligible_samples: list[dict[str, Any]] = []

    node_items = nodes if isinstance(nodes, list) else []
    for node in node_items:
        if not isinstance(node, dict):
            continue
        liveness = str(node.get("liveness_status") or "unknown")
        if liveness == "live":
            live_node_count += 1
        supported = [str(item) for item in node.get("supported_job_types") or [] if isinstance(item, str)]
        models = [str(item) for item in node.get("ollama_models") or [] if isinstance(item, str)]
        if not supported:
            legacy_node_count += 1
        eligible = "inference.chat.v1" in supported and model in models
        if not eligible:
            continue
        eligible_node_count += 1
        if liveness == "live":
            live_eligible_node_count += 1
        if len(eligible_samples) < 5:
            eligible_samples.append(
                {
                    "node_id_redacted": _redact_node_id(node.get("node_id")),
                    "liveness_status": liveness,
                    "ollama_models": models,
                    "software": node.get("software") if isinstance(node.get("software"), dict) else None,
                }
            )

    warnings = []
    if legacy_node_count:
        warnings.append("legacy_nodes_without_supported_job_types")
    return {
        "model": model,
        "live_node_count": live_node_count,
        "eligible_node_count": eligible_node_count,
        "live_eligible_node_count": live_eligible_node_count,
        "legacy_node_count": legacy_node_count,
        "eligible_node_samples": eligible_samples,
        "warnings": warnings,
    }


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
    if action_id == "run_session_sync":
        return [
            _command(
                "sync_session",
                "read_only_remote_updates_local_session",
                _base_chat_session_argv(config, "session-sync", connection) + ["--json"],
            )
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

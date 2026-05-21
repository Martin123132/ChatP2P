"""HTTP transport for the first networked coordinator prototype."""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .coordinator import Coordinator
from .operator_config import OperatorConfig
from .packets import (
    JobLeaseAcknowledgement,
    JobLeaseRenewal,
    JobLeaseRequest,
    JobResult,
    NodeHeartbeat,
    NodeRegistration,
)


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


class HTTPRequestRejected(ValueError):
    def __init__(self, status: int, payload: dict[str, Any]):
        super().__init__(payload.get("error", "request rejected"))
        self.status = status
        self.payload = payload


def _read_json(handler: BaseHTTPRequestHandler, *, max_bytes: int) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length == 0:
        return {}
    if content_length > max_bytes:
        raise HTTPRequestRejected(
            413,
            {
                "accepted": False,
                "error": f"request body exceeds max_request_bytes ({max_bytes})",
                "max_request_bytes": max_bytes,
            },
        )
    raw = handler.rfile.read(content_length)
    return json.loads(raw.decode("utf-8"))


def _short(value: str, keep: int = 12) -> str:
    if len(value) <= keep:
        return value
    return f"{value[:keep]}..."


def _json_snippet(value: Any) -> str:
    return html.escape(json.dumps(value, sort_keys=True, ensure_ascii=True))


def _render_dashboard(snapshot: dict[str, Any]) -> str:
    status = snapshot["status"]
    nodes = snapshot["nodes"]
    jobs = snapshot["jobs"]
    results = snapshot["results"]

    metric_labels = [
        ("Nodes", "known_nodes"),
        ("Jobs", "jobs"),
        ("Queued", "queued_jobs"),
        ("Pending", "pending_jobs"),
        ("Active Leases", "leased_jobs"),
        ("Expired Leases", "expired_leases"),
        ("Verified", "verified_jobs"),
        ("Disputed", "disputed_jobs"),
        ("Expired Jobs", "expired_jobs"),
    ]
    metrics = "\n".join(
        f"""
        <section class="metric">
          <span>{html.escape(label)}</span>
          <strong>{html.escape(str(status[key]))}</strong>
        </section>
        """
        for label, key in metric_labels
    )

    node_rows = "\n".join(
        f"""
        <tr>
          <td><code>{html.escape(_short(node["node_id"], 18))}</code></td>
          <td><span class="live-state {html.escape(node["liveness_status"])}">{html.escape(node["liveness_status"])}</span></td>
          <td>{html.escape(str(node["credits"]))}</td>
          <td>{html.escape("" if node["last_seen_seconds_ago"] is None else f'{node["last_seen_seconds_ago"]}s')}</td>
          <td>{html.escape(str(node["active_leases"]))}/{html.escape(str(node["expired_leases"]))}</td>
          <td>{html.escape(node.get("capability_tier", "light"))}</td>
          <td>{html.escape(", ".join(node["supported_job_types"]) or "none")}</td>
          <td>{html.escape(node["hardware"].get("system", "unknown"))}</td>
        </tr>
        """
        for node in nodes
    ) or """<tr><td colspan="8" class="empty">No nodes registered yet.</td></tr>"""

    job_rows = "\n".join(
        f"""
        <tr>
          <td><code>{html.escape(_short(job["job_id"], 18))}</code></td>
          <td>{html.escape(job["job_type"])}</td>
          <td><span class="status {html.escape(job["status"])}">{html.escape(job["status"])}</span></td>
          <td><code>{html.escape(", ".join(_short(node_id, 18) for node_id in job["active_leases"]))}</code></td>
          <td>{html.escape(str(job["acknowledged_lease_count"]))}</td>
          <td>{html.escape(str(job["expired_lease_count"]))}</td>
          <td>{html.escape(str(job["reward"]))}</td>
          <td>{html.escape(str(job["result_count"]))}/{html.escape(str(job["required_results"]))}</td>
          <td><code>{_json_snippet(job["payload"])}</code></td>
        </tr>
        """
        for job in jobs
    ) or """<tr><td colspan="9" class="empty">No jobs queued yet.</td></tr>"""

    reputation_rows = "\n".join(
        f"""
        <tr>
          <td><code>{html.escape(_short(entry["node_id"], 18))}</code></td>
          <td><span class="rep {html.escape(entry["status"])}">{html.escape(entry["status"])}</span></td>
          <td>{html.escape(str(entry["score"]))}</td>
          <td>{html.escape("" if entry["reliability"] is None else str(entry["reliability"]))}</td>
          <td>{html.escape(str(entry["verified_matches"]))}</td>
          <td>{html.escape(str(entry["mismatches"]))}</td>
          <td>{html.escape(str(entry["disputed_results"]))}</td>
          <td>{html.escape(str(entry["timeouts"]))}</td>
        </tr>
        """
        for entry in snapshot["reputation"]
    ) or """<tr><td colspan="8" class="empty">No reputation history yet.</td></tr>"""

    result_rows = "\n".join(
        f"""
        <tr>
          <td><code>{html.escape(_short(result["job_id"], 18))}</code></td>
          <td><code>{html.escape(_short(result["node_id"], 18))}</code></td>
          <td>{html.escape(str(result["output"].get("passed", "")))}</td>
          <td>{html.escape(str(result["runtime_seconds"]))}</td>
          <td><code>{_json_snippet(result["output"])}</code></td>
        </tr>
        """
        for result in reversed(results[-12:])
    ) or """<tr><td colspan="5" class="empty">No completed results yet.</td></tr>"""

    coordinator_id = html.escape(status["coordinator_id"])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>ChatP2P Coordinator</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f4;
      --surface: #ffffff;
      --text: #20231f;
      --muted: #666d62;
      --line: #deded6;
      --green: #1f7a4d;
      --amber: #9a6518;
      --red: #a23b32;
      --blue: #315f8c;
      --gray: #687076;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }}
    header {{
      padding: 24px 28px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }}
    h1, h2 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 24px; }}
    h2 {{ font-size: 16px; }}
    .subhead {{
      margin-top: 6px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    main {{
      width: min(1400px, 100%);
      margin: 0 auto;
      padding: 20px 24px 32px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric {{
      min-height: 78px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }}
    .metric strong {{
      display: block;
      margin-top: 6px;
      font-size: 28px;
      line-height: 1;
    }}
    section.table-block {{
      margin-top: 16px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      margin-top: 10px;
    }}
    th, td {{
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }}
    code {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 12px;
    }}
    .status {{
      display: inline-block;
      min-width: 74px;
      padding: 3px 7px;
      border-radius: 999px;
      color: #fff;
      text-align: center;
      font-size: 12px;
    }}
    .queued {{ background: var(--amber); }}
    .leased {{ background: var(--blue); }}
    .pending {{ background: var(--gray); }}
    .verified {{ background: var(--green); }}
    .disputed {{ background: var(--red); }}
    .expired {{ background: var(--red); }}
    .rep {{
      display: inline-block;
      min-width: 66px;
      padding: 3px 7px;
      border-radius: 999px;
      color: #fff;
      text-align: center;
      font-size: 12px;
    }}
    .new {{ background: var(--gray); }}
    .ok {{ background: var(--blue); }}
    .trusted {{ background: var(--green); }}
    .watch {{ background: var(--amber); }}
    .flagged {{ background: var(--red); }}
    .live-state {{
      display: inline-block;
      min-width: 58px;
      padding: 3px 7px;
      border-radius: 999px;
      color: #fff;
      text-align: center;
      font-size: 12px;
    }}
    .live {{ background: var(--green); }}
    .stale {{ background: var(--amber); }}
    .offline {{ background: var(--red); }}
    .empty {{ color: var(--muted); }}
    .api {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 10px;
    }}
    .api a {{
      color: var(--blue);
      text-decoration: none;
      border-bottom: 1px solid transparent;
    }}
    .api a:hover {{ border-color: currentColor; }}
    @media (max-width: 720px) {{
      header {{ padding: 18px; }}
      main {{ padding: 14px; }}
      table {{ min-width: 760px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>ChatP2P Coordinator</h1>
    <div class="subhead">Coordinator <code>{coordinator_id}</code>. Auto-refreshes every 5 seconds.</div>
    <nav class="api">
      <a href="/api/snapshot">snapshot</a>
      <a href="/api/nodes">nodes</a>
      <a href="/api/jobs">jobs</a>
      <a href="/api/results">results</a>
      <a href="/api/reputation">reputation</a>
    </nav>
  </header>
  <main>
    <div class="metrics">{metrics}</div>
    <section class="table-block">
      <h2>Nodes</h2>
      <table>
        <thead><tr><th>Node</th><th>Live</th><th>Credits</th><th>Seen</th><th>Leases</th><th>Tier</th><th>Capabilities</th><th>System</th></tr></thead>
        <tbody>{node_rows}</tbody>
      </table>
    </section>
    <section class="table-block">
      <h2>Jobs</h2>
      <table>
        <thead><tr><th>Job</th><th>Type</th><th>Status</th><th>Active</th><th>Acked</th><th>Expired</th><th>Reward</th><th>Results</th><th>Payload</th></tr></thead>
        <tbody>{job_rows}</tbody>
      </table>
    </section>
    <section class="table-block">
      <h2>Reputation</h2>
      <table>
        <thead><tr><th>Node</th><th>Status</th><th>Score</th><th>Reliability</th><th>Verified</th><th>Mismatches</th><th>Disputed</th><th>Timeouts</th></tr></thead>
        <tbody>{reputation_rows}</tbody>
      </table>
    </section>
    <section class="table-block">
      <h2>Recent Results</h2>
      <table>
        <thead><tr><th>Job</th><th>Node</th><th>Passed</th><th>Runtime</th><th>Output</th></tr></thead>
        <tbody>{result_rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def create_coordinator_http_server(
    coordinator: Coordinator,
    host: str = "127.0.0.1",
    port: int = 8765,
    operator_config: OperatorConfig | None = None,
) -> ThreadingHTTPServer:
    lock = threading.Lock()
    config = operator_config or OperatorConfig.default()

    class CoordinatorHandler(BaseHTTPRequestHandler):
        server_version = "ChatP2PHTTP/0.1"

        def _read_json_or_reject(self) -> dict[str, Any] | None:
            try:
                return _read_json(self, max_bytes=config.max_request_bytes)
            except HTTPRequestRejected as exc:
                _json_response(self, exc.status, exc.payload)
            except json.JSONDecodeError as exc:
                _json_response(self, 400, {"accepted": False, "error": str(exc)})
            return None

        def _admission_token(self) -> str | None:
            token = self.headers.get("X-ChatP2P-Admission-Token")
            if token:
                return token.strip()
            authorization = self.headers.get("Authorization")
            if authorization and authorization.lower().startswith("bearer "):
                return authorization[7:].strip()
            return None

        def _admission_allowed(self, path: str) -> bool:
            if not config.admission_required_for(path):
                return True
            if config.token_matches(self._admission_token()):
                return True
            _json_response(
                self,
                403,
                {
                    "accepted": False,
                    "error": "admission token required",
                    "public_alpha": config.public_summary(),
                },
            )
            return False

        def _validate_job_creation(self, request: dict[str, Any]) -> bool:
            job_type = request.get("job_type")
            if job_type not in config.allowed_job_types:
                _json_response(
                    self,
                    403,
                    {
                        "created": False,
                        "error": f"job_type {job_type!r} is not allowed by operator policy",
                        "allowed_job_types": list(config.allowed_job_types),
                    },
                )
                return False

            payload = request.get("payload")
            payload_size = len(json.dumps(payload, sort_keys=True).encode("utf-8"))
            if payload_size > config.max_job_payload_bytes:
                _json_response(
                    self,
                    413,
                    {
                        "created": False,
                        "error": (
                            f"job payload exceeds max_job_payload_bytes "
                            f"({config.max_job_payload_bytes})"
                        ),
                        "payload_bytes": payload_size,
                        "max_job_payload_bytes": config.max_job_payload_bytes,
                    },
                )
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path in {"/", "/dashboard"}:
                with lock:
                    snapshot = coordinator.snapshot()
                _html_response(self, 200, _render_dashboard(snapshot))
                return

            if parsed.path == "/health":
                with lock:
                    status = coordinator.status()
                    status["operator"] = config.public_summary()
                    _json_response(self, 200, status)
                return

            if parsed.path == "/api/status":
                with lock:
                    status = coordinator.status()
                    status["operator"] = config.public_summary()
                    _json_response(self, 200, status)
                return

            if parsed.path == "/api/nodes":
                with lock:
                    _json_response(self, 200, {"nodes": coordinator.node_summaries()})
                return

            if parsed.path == "/api/jobs":
                with lock:
                    _json_response(self, 200, {"jobs": coordinator.job_summaries()})
                return

            if parsed.path == "/api/results":
                with lock:
                    _json_response(self, 200, {"results": coordinator.result_summaries()})
                return

            if parsed.path == "/api/reputation":
                with lock:
                    _json_response(self, 200, {"reputation": list(coordinator.reputation_summaries().values())})
                return

            if parsed.path == "/api/snapshot":
                with lock:
                    _json_response(self, 200, coordinator.snapshot())
                return

            if parsed.path == "/jobs/next":
                _json_response(self, 405, {"error": "signed POST /jobs/next is required"})
                return

            _json_response(self, 404, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/nodes/register":
                if not self._admission_allowed(parsed.path):
                    return
                request = self._read_json_or_reject()
                if request is None:
                    return
                try:
                    registration = NodeRegistration.from_dict(request)
                except (KeyError, TypeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    accepted = coordinator.register_signed_node(registration)
                    credits = coordinator.credits.get(registration.node_id, 0)
                status = 200 if accepted else 403
                _json_response(self, status, {"accepted": accepted, "node_id": registration.node_id, "credits": credits})
                return

            if parsed.path == "/nodes/heartbeat":
                request = self._read_json_or_reject()
                if request is None:
                    return
                try:
                    heartbeat = NodeHeartbeat.from_dict(request)
                except (KeyError, TypeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    accepted = coordinator.record_signed_heartbeat(heartbeat)
                    last_seen_at = coordinator.node_last_seen.get(heartbeat.node_id)
                status = 200 if accepted else 403
                _json_response(
                    self,
                    status,
                    {"accepted": accepted, "node_id": heartbeat.node_id, "last_seen_at": last_seen_at},
                )
                return

            if parsed.path == "/jobs/next":
                request_body = self._read_json_or_reject()
                if request_body is None:
                    return
                try:
                    lease_request = JobLeaseRequest.from_dict(request_body)
                except (KeyError, TypeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    rejection_reason = coordinator.signed_node_packet_rejection_reason(lease_request)
                    if rejection_reason is not None:
                        _json_response(
                            self,
                            403,
                            {"accepted": False, "job": None, "lease": None, "error": rejection_reason},
                        )
                        return
                    leased = coordinator.lease_next_signed_job(lease_request)
                if leased is None:
                    _json_response(self, 200, {"accepted": True, "job": None, "lease": None})
                    return
                job, lease = leased
                _json_response(self, 200, {"accepted": True, "job": job.to_dict(), "lease": lease})
                return

            if parsed.path == "/jobs/lease/ack":
                request = self._read_json_or_reject()
                if request is None:
                    return
                try:
                    acknowledgement = JobLeaseAcknowledgement.from_dict(request)
                except (KeyError, TypeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    accepted = coordinator.acknowledge_lease(acknowledgement)
                    lease = (
                        coordinator.lease_metadata(acknowledgement.job_id, acknowledgement.node_id)
                        if accepted
                        else None
                    )
                status = 200 if accepted else 403
                _json_response(
                    self,
                    status,
                    {
                        "accepted": accepted,
                        "job_id": acknowledgement.job_id,
                        "node_id": acknowledgement.node_id,
                        "lease": lease,
                    },
                )
                return

            if parsed.path == "/jobs/lease/renew":
                request = self._read_json_or_reject()
                if request is None:
                    return
                try:
                    renewal = JobLeaseRenewal.from_dict(request)
                except (KeyError, TypeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    lease = coordinator.renew_lease(renewal)
                accepted = lease is not None
                status = 200 if accepted else 403
                _json_response(
                    self,
                    status,
                    {
                        "accepted": accepted,
                        "job_id": renewal.job_id,
                        "node_id": renewal.node_id,
                        "lease": lease,
                    },
                )
                return

            if parsed.path == "/jobs/result":
                request = self._read_json_or_reject()
                if request is None:
                    return
                try:
                    result = JobResult.from_dict(request)
                except (KeyError, TypeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    accepted = coordinator.submit_result(result)
                    credits = coordinator.credits.get(result.node_id, 0)
                status = 200 if accepted else 403
                _json_response(self, status, {"accepted": accepted, "node_id": result.node_id, "credits": credits})
                return

            if parsed.path == "/jobs":
                if not self._admission_allowed(parsed.path):
                    return
                request = self._read_json_or_reject()
                if request is None:
                    return
                if not self._validate_job_creation(request):
                    return
                try:
                    with lock:
                        job = coordinator.create_job(
                            job_type=request["job_type"],
                            payload=request["payload"],
                            model_id=request.get("model_id"),
                            resource_requirements=request.get("resource_requirements"),
                            expected_output_schema=request.get("expected_output_schema"),
                            verification_strategy=request.get("verification_strategy"),
                            reward=int(request.get("reward", 1)),
                            ttl_seconds=int(request.get("ttl_seconds", 300)),
                        )
                except (KeyError, TypeError, ValueError) as exc:
                    _json_response(self, 400, {"created": False, "error": str(exc)})
                    return
                _json_response(self, 201, {"created": True, "job": job.to_dict()})
                return

            if parsed.path == "/jobs/demo-math":
                if not self._admission_allowed(parsed.path):
                    return
                with lock:
                    job = coordinator.create_math_eval_job()
                _json_response(self, 201, {"job": job.to_dict()})
                return

            if parsed.path == "/jobs/demo-suite":
                if not self._admission_allowed(parsed.path):
                    return
                with lock:
                    jobs = coordinator.create_deterministic_eval_jobs()
                _json_response(self, 201, {"jobs": [job.to_dict() for job in jobs]})
                return

            _json_response(self, 404, {"error": "not found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), CoordinatorHandler)

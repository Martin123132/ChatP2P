"""HTTP transport for the first networked coordinator prototype."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .coordinator import Coordinator
from .packets import JobResult, NodeRegistration


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length == 0:
        return {}
    raw = handler.rfile.read(content_length)
    return json.loads(raw.decode("utf-8"))


def create_coordinator_http_server(
    coordinator: Coordinator,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    lock = threading.Lock()

    class CoordinatorHandler(BaseHTTPRequestHandler):
        server_version = "ChatP2PHTTP/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/health":
                with lock:
                    _json_response(self, 200, coordinator.status())
                return

            if parsed.path == "/jobs/next":
                query = parse_qs(parsed.query)
                node_id = query.get("node_id", [None])[0]
                if node_id is None:
                    _json_response(self, 400, {"error": "node_id query parameter is required"})
                    return
                with lock:
                    job = coordinator.lease_next_job(node_id)
                _json_response(self, 200, {"job": job.to_dict() if job else None})
                return

            _json_response(self, 404, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/nodes/register":
                try:
                    registration = NodeRegistration.from_dict(_read_json(self))
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    accepted = coordinator.register_signed_node(registration)
                    credits = coordinator.credits.get(registration.node_id, 0)
                status = 200 if accepted else 403
                _json_response(self, status, {"accepted": accepted, "node_id": registration.node_id, "credits": credits})
                return

            if parsed.path == "/jobs/result":
                try:
                    result = JobResult.from_dict(_read_json(self))
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    _json_response(self, 400, {"accepted": False, "error": str(exc)})
                    return
                with lock:
                    accepted = coordinator.submit_result(result)
                    credits = coordinator.credits.get(result.node_id, 0)
                status = 200 if accepted else 403
                _json_response(self, status, {"accepted": accepted, "node_id": result.node_id, "credits": credits})
                return

            if parsed.path == "/jobs/demo-math":
                with lock:
                    job = coordinator.create_math_eval_job()
                _json_response(self, 201, {"job": job.to_dict()})
                return

            if parsed.path == "/jobs/demo-suite":
                with lock:
                    jobs = coordinator.create_deterministic_eval_jobs()
                _json_response(self, 201, {"jobs": [job.to_dict() for job in jobs]})
                return

            _json_response(self, 404, {"error": "not found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), CoordinatorHandler)

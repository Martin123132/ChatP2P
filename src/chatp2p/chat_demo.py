"""Local all-in-one chat demo runtime."""

from __future__ import annotations

import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chat_gateway import (
    DEFAULT_CHAT_GATEWAY_HOST,
    DEFAULT_CHAT_GATEWAY_MAX_REQUEST_BYTES,
    DEFAULT_CHAT_GATEWAY_PORT,
    ChatGatewayConfig,
    create_chat_gateway_server,
)
from .chat_smoke import _ollama_capabilities, _start_fake_ollama
from .client import CoordinatorClient
from .coordinator import Coordinator
from .crypto import NodeIdentity
from .http_api import create_coordinator_http_server
from .ollama import DEFAULT_OLLAMA_BASE_URL, list_ollama_models
from .packets import NodeRegistration
from .worker import WorkerNode


CHAT_DEMO_RUNTIME_SCHEMA = "chatp2p.chat-demo-runtime.v1"


@dataclass(frozen=True)
class ChatDemoConfig:
    out_dir: Path = Path(".mesh/chat-demo")
    session_id: str = "demo"
    title: str | None = "Local Chat Demo"
    mode: str = "fake"
    model: str = "tiny-test-model"
    system: str | None = "Be concise."
    requester_account_id: str = "requester_demo"
    starting_credits: int = 10
    job_cost: int = 1
    reward: int = 1
    temperature: float | None = 0.2
    max_tokens: int | None = 256
    ttl_seconds: int = 300
    timeout_seconds: float = 60.0
    poll_interval: float = 0.2
    client_timeout_seconds: float = 10.0
    max_context_turns: int = 8
    fake_answer: str = "ChatP2P demo answer from a local fake model worker."
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_timeout_seconds: float = 30.0
    host: str = DEFAULT_CHAT_GATEWAY_HOST
    port: int = DEFAULT_CHAT_GATEWAY_PORT
    coordinator_host: str = "127.0.0.1"
    coordinator_port: int = 0
    worker_poll_interval: float = 0.05
    max_request_bytes: int = DEFAULT_CHAT_GATEWAY_MAX_REQUEST_BYTES
    open_browser: bool = False
    source_root: Path | None = None


@dataclass
class ChatDemoRuntime:
    config: ChatDemoConfig
    coordinator: Coordinator
    coordinator_server: Any
    coordinator_thread: threading.Thread
    model_runtime: Any
    worker: WorkerNode
    worker_thread: threading.Thread
    worker_stop: threading.Event
    gateway_server: Any
    coordinator_url: str
    gateway_url: str
    started_at: str
    worker_errors: list[str] = field(default_factory=list)
    gateway_thread: threading.Thread | None = None

    def start_gateway_thread(self) -> None:
        if self.gateway_thread is not None:
            return
        self.gateway_thread = threading.Thread(target=self.gateway_server.serve_forever, daemon=True)
        self.gateway_thread.start()

    def serve_gateway_forever(self) -> None:
        print(format_chat_demo_summary(self.report()))
        if self.config.open_browser:
            webbrowser.open(self.gateway_url)
        try:
            self.gateway_server.serve_forever()
        except KeyboardInterrupt:
            print("shutting down chat demo")
        finally:
            self.close()

    def report(self) -> dict[str, Any]:
        return {
            "schema": CHAT_DEMO_RUNTIME_SCHEMA,
            "ok": not self.worker_errors,
            "status": "pass" if not self.worker_errors else "warn",
            "started_at": self.started_at,
            "urls": {
                "gateway": self.gateway_url,
                "coordinator": self.coordinator_url,
                "model_runtime": self.model_runtime.base_url,
            },
            "config": {
                "out_dir": str(self.config.out_dir.expanduser().resolve()),
                "session_id": self.config.session_id,
                "mode": self.config.mode,
                "model": self.config.model,
                "requester_account_id": self.config.requester_account_id,
                "starting_credits": self.config.starting_credits,
                "job_cost": self.config.job_cost,
                "reward": self.config.reward,
                "host": self.config.host,
                "port": self.gateway_server.server_address[1],
            },
            "worker": {
                "node_id_redacted": _redact_node_id(self.worker.identity.node_id),
                "supported_job_types": self.worker.capabilities().get("supported_job_types", []),
                "ollama_models": self.worker.capabilities().get("ollama_models", []),
                "errors": list(self.worker_errors),
            },
            "summary": {
                "recommended_next_action": "open_chat_demo_gateway",
                "gateway_url": self.gateway_url,
            },
        }

    def close(self) -> None:
        self.worker_stop.set()
        if self.gateway_thread is not None:
            self.gateway_server.shutdown()
            self.gateway_thread.join(timeout=2)
        self.gateway_server.server_close()
        self.coordinator_server.shutdown()
        self.coordinator_server.server_close()
        self.coordinator_thread.join(timeout=2)
        self.model_runtime.stop()
        self.worker_thread.join(timeout=2)


def create_chat_demo_runtime(config: ChatDemoConfig) -> ChatDemoRuntime:
    """Start local coordinator, fake model, worker loop, and gateway server."""

    _validate_config(config)
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    model_runtime = _start_model_runtime(config)
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    coordinator_server = create_coordinator_http_server(
        coordinator,
        host=config.coordinator_host,
        port=config.coordinator_port,
    )
    coordinator_thread = threading.Thread(target=coordinator_server.serve_forever, daemon=True)
    coordinator_thread.start()
    coordinator_host, coordinator_port = coordinator_server.server_address
    coordinator_url = f"http://{coordinator_host}:{coordinator_port}"

    if config.starting_credits > 0:
        coordinator.apply_credit_delta(
            account_id=config.requester_account_id,
            account_type="requester",
            delta=config.starting_credits,
            reason="operator_credit_grant",
            metadata={"source": "chat_demo"},
        )

    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(
        identity=worker_identity,
        capability_profile=_ollama_capabilities(models=[config.model], base_url=model_runtime.base_url),
        ollama_base_url=model_runtime.base_url,
        ollama_timeout_seconds=config.ollama_timeout_seconds,
    )
    client = CoordinatorClient(coordinator_url, timeout_seconds=config.client_timeout_seconds)
    registration = NodeRegistration.create(node=worker.identity, capabilities=worker.capabilities())
    client.register(registration)

    worker_stop = threading.Event()
    worker_errors: list[str] = []
    worker_thread = threading.Thread(
        target=_worker_loop,
        args=(client, worker, worker_stop, worker_errors, config.worker_poll_interval),
        daemon=True,
    )
    worker_thread.start()

    gateway_server = create_chat_gateway_server(
        ChatGatewayConfig(
            out_dir=out_dir,
            session_id=config.session_id,
            title=config.title,
            coordinator_url=coordinator_url,
            invite_path=None,
            admission_token=None,
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
            no_wait=False,
            client_timeout_seconds=config.client_timeout_seconds,
            max_context_turns=config.max_context_turns,
            host=config.host,
            port=config.port,
            max_request_bytes=config.max_request_bytes,
            source_root=config.source_root,
        )
    )
    gateway_host, gateway_port = gateway_server.server_address
    return ChatDemoRuntime(
        config=config,
        coordinator=coordinator,
        coordinator_server=coordinator_server,
        coordinator_thread=coordinator_thread,
        model_runtime=model_runtime,
        worker=worker,
        worker_thread=worker_thread,
        worker_stop=worker_stop,
        worker_errors=worker_errors,
        gateway_server=gateway_server,
        coordinator_url=coordinator_url,
        gateway_url=f"http://{gateway_host}:{gateway_port}",
        started_at=started_at,
    )


def run_chat_demo(config: ChatDemoConfig) -> None:
    runtime = create_chat_demo_runtime(config)
    runtime.serve_gateway_forever()


def format_chat_demo_summary(report: dict[str, Any]) -> str:
    urls = report.get("urls") or {}
    config = report.get("config") or {}
    worker = report.get("worker") or {}
    return "\n".join(
        [
            "Chat demo: READY",
            f"Gateway: {urls.get('gateway')}",
            f"Coordinator: {urls.get('coordinator')}",
            f"Model runtime: {urls.get('model_runtime')}",
            f"Session: {config.get('session_id')}",
            f"Mode: {config.get('mode')}",
            f"Model: {config.get('model')}",
            f"Requester: {config.get('requester_account_id')} ({config.get('starting_credits')} credits)",
            f"Worker: {worker.get('node_id_redacted')}",
            "Press Ctrl+C to stop the demo.",
        ]
    )


def _worker_loop(
    client: CoordinatorClient,
    worker: WorkerNode,
    stop: threading.Event,
    errors: list[str],
    interval_seconds: float,
) -> None:
    while not stop.is_set():
        try:
            job = client.next_job(worker.identity)
            if job is None:
                time.sleep(interval_seconds)
                continue
            result = worker.run_job(job)
            response = client.submit_result(result)
            if not response.get("accepted"):
                errors.append(f"result rejected for {job.job_id}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(interval_seconds)


@dataclass
class _ExternalModelRuntime:
    base_url: str

    def stop(self) -> None:
        return


def _start_model_runtime(config: ChatDemoConfig) -> Any:
    if config.mode == "fake":
        return _start_fake_ollama(model=config.model, answer=config.fake_answer)
    if config.mode == "ollama":
        advertised_models = list_ollama_models(
            base_url=config.ollama_base_url,
            timeout_seconds=min(config.ollama_timeout_seconds, 10.0),
        )
        if config.model not in advertised_models:
            raise ValueError(
                f"Ollama model {config.model!r} is not advertised by {config.ollama_base_url}. "
                f"Advertised models: {advertised_models}"
            )
        return _ExternalModelRuntime(base_url=config.ollama_base_url.rstrip("/"))
    raise ValueError("--mode must be fake or ollama")


def _validate_config(config: ChatDemoConfig) -> None:
    if not config.session_id.strip():
        raise ValueError("--session-id must be non-empty")
    if not config.model.strip():
        raise ValueError("--model must be non-empty")
    if not config.requester_account_id.strip():
        raise ValueError("--requester-account-id must be non-empty")
    if config.mode not in {"fake", "ollama"}:
        raise ValueError("--mode must be fake or ollama")
    if config.host != DEFAULT_CHAT_GATEWAY_HOST:
        raise ValueError("Chat Demo V0 only supports --host 127.0.0.1")
    if not 0 <= config.port <= 65535:
        raise ValueError("--port must be between 0 and 65535")
    if not 0 <= config.coordinator_port <= 65535:
        raise ValueError("--coordinator-port must be between 0 and 65535")
    if config.starting_credits < 0:
        raise ValueError("--starting-credits must be at least 0")
    if config.job_cost < 1:
        raise ValueError("--job-cost must be at least 1")
    if config.reward < 1:
        raise ValueError("--reward must be at least 1")
    if config.ttl_seconds < 1:
        raise ValueError("--ttl-seconds must be at least 1")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.client_timeout_seconds <= 0:
        raise ValueError("--client-timeout-seconds must be greater than 0")
    if config.worker_poll_interval <= 0:
        raise ValueError("--worker-poll-interval must be greater than 0")
    if config.ollama_timeout_seconds <= 0:
        raise ValueError("--ollama-timeout-seconds must be greater than 0")
    if config.max_context_turns < 0:
        raise ValueError("--max-context-turns must be at least 0")
    if config.max_request_bytes < 1:
        raise ValueError("--max-request-bytes must be at least 1")
    if config.temperature is not None and not 0 <= config.temperature <= 2:
        raise ValueError("--temperature must be between 0 and 2")
    if config.max_tokens is not None and config.max_tokens < 1:
        raise ValueError("--max-tokens must be at least 1")


def _redact_node_id(node_id: Any) -> str | None:
    if not isinstance(node_id, str) or not node_id:
        return None
    if len(node_id) <= 12:
        return node_id
    return f"{node_id[:12]}..."

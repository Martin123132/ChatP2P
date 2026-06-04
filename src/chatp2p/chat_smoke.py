"""Local funded chat smoke proof for the credit-backed inference loop."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .benchmark import capabilities_from_benchmark
from .coordinator import Coordinator
from .crypto import NodeIdentity
from .ollama import DEFAULT_OLLAMA_BASE_URL, list_ollama_models
from .worker import WorkerNode

FUNDED_CHAT_SMOKE_REPORT_SCHEMA = "chatp2p.funded-chat-smoke-report.v1"


@dataclass(frozen=True)
class FundedChatSmokeConfig:
    out_dir: Path = Path(".mesh/chat-smoke")
    model: str = "tiny-test-model"
    prompt: str = "Explain ChatP2P in one sentence."
    system: str | None = "Be concise."
    requester_account_id: str = "requester_demo"
    starting_credits: int = 3
    job_cost: int = 2
    reward: int = 1
    temperature: float | None = 0.2
    max_tokens: int | None = 96
    ttl_seconds: int = 300
    mode: str = "fake"
    fake_answer: str = "ChatP2P lets contributors earn credits by running signed AI jobs for requesters."
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL
    ollama_timeout_seconds: float = 30.0


@dataclass
class _FakeOllamaRuntime:
    server: ThreadingHTTPServer
    thread: threading.Thread
    base_url: str
    requests: list[dict[str, Any]]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def run_funded_chat_smoke(config: FundedChatSmokeConfig) -> dict[str, Any]:
    """Run one local requester-funded chat job and write JSON/Markdown evidence."""

    _validate_config(config)
    started_at = time.time()
    generated_at = datetime.now(timezone.utc).isoformat()
    out_dir = config.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    coordinator: Coordinator | None = None
    fake_runtime: _FakeOllamaRuntime | None = None
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    job_summary: dict[str, Any] | None = None
    result_summary: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    runtime_base_url = config.ollama_base_url
    advertised_models: list[str] = [config.model]

    try:
        if config.mode == "fake":
            fake_runtime = _start_fake_ollama(model=config.model, answer=config.fake_answer)
            runtime_base_url = fake_runtime.base_url
            steps.append(_step("start_fake_ollama", "pass", {"base_url": runtime_base_url, "model": config.model}))
        else:
            advertised_models = list_ollama_models(
                base_url=config.ollama_base_url,
                timeout_seconds=min(config.ollama_timeout_seconds, 10.0),
            )
            if config.model not in advertised_models:
                raise RuntimeError(f"Ollama model {config.model!r} is not advertised by {config.ollama_base_url}")
            steps.append(
                _step(
                    "check_ollama",
                    "pass",
                    {"base_url": config.ollama_base_url, "models": advertised_models},
                )
            )

        coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(
            identity=worker_identity,
            capability_profile=_ollama_capabilities(models=advertised_models, base_url=runtime_base_url),
            ollama_base_url=runtime_base_url,
            ollama_timeout_seconds=config.ollama_timeout_seconds,
        )
        coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
        steps.append(
            _step(
                "register_worker",
                "pass",
                {
                    "node_id": worker_identity.node_id,
                    "supported_job_types": worker.capabilities().get("supported_job_types", []),
                    "ollama_models": worker.capabilities().get("ollama_models", []),
                },
            )
        )

        if config.starting_credits > 0:
            grant = coordinator.apply_credit_delta(
                account_id=config.requester_account_id,
                account_type="requester",
                delta=config.starting_credits,
                reason="operator_credit_grant",
                metadata={"source": "funded_chat_smoke"},
            )
            steps.append(
                _step(
                    "grant_requester_credits",
                    "pass",
                    {
                        "account_id": grant.account_id,
                        "delta": grant.delta,
                        "balance_after": grant.balance_after,
                    },
                )
            )
        else:
            steps.append(
                _step(
                    "grant_requester_credits",
                    "skipped",
                    {"account_id": config.requester_account_id, "starting_credits": 0},
                )
            )

        job = coordinator.create_chat_inference_job(
            model=config.model,
            messages=_messages_from_config(config),
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reward=config.reward,
            ttl_seconds=config.ttl_seconds,
            requester_account_id=config.requester_account_id,
            job_cost=config.job_cost,
        )
        steps.append(
            _step(
                "create_funded_chat_job",
                "pass",
                {
                    "job_id": job.job_id,
                    "requester_account_id": config.requester_account_id,
                    "job_cost": config.job_cost,
                    "reward": config.reward,
                },
            )
        )

        leased = coordinator.lease_next_job(worker_identity.node_id)
        if leased is None:
            raise RuntimeError("no eligible worker leased the funded chat job")
        steps.append(_step("lease_job", "pass", {"job_id": leased.job_id, "node_id": worker_identity.node_id}))

        result = worker.run_job(leased)
        steps.append(
            _step(
                "run_chat_inference",
                "pass",
                {
                    "job_id": result.job_id,
                    "node_id": result.node_id,
                    "output_hash": result.output_hash,
                    "runtime_seconds": result.runtime_seconds,
                },
            )
        )

        if not coordinator.submit_result(result):
            raise RuntimeError("coordinator rejected the worker result")
        snapshot = coordinator.snapshot()
        job_summary = _first_matching(snapshot.get("jobs", []), "job_id", job.job_id)
        result_summary = _first_matching(snapshot.get("results", []), "job_id", job.job_id)
        steps.append(
            _step(
                "submit_result",
                "pass",
                {
                    "job_id": job.job_id,
                    "accepted": True,
                    "job_status": (job_summary or {}).get("status"),
                },
            )
        )
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        steps.append(_step("smoke_error", "fail", {"error": errors[-1]}))
        if coordinator is not None:
            snapshot = coordinator.snapshot()
    finally:
        fake_requests = _fake_request_summaries(fake_runtime.requests if fake_runtime is not None else [])
        if fake_runtime is not None:
            fake_runtime.stop()

    ledger = coordinator.credit_ledger_snapshot(limit=20) if coordinator is not None else {"summary": {}, "recent_entries": []}
    status = "pass" if not errors and (job_summary or {}).get("status") == "verified" and result_summary else "fail"
    report = {
        "schema": FUNDED_CHAT_SMOKE_REPORT_SCHEMA,
        "ok": status == "pass",
        "status": status,
        "generated_at": generated_at,
        "duration_seconds": round(time.time() - started_at, 3),
        "config": {
            "out_dir": str(out_dir),
            "mode": config.mode,
            "model": config.model,
            "requester_account_id": config.requester_account_id,
            "starting_credits": config.starting_credits,
            "job_cost": config.job_cost,
            "reward": config.reward,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "ttl_seconds": config.ttl_seconds,
            "ollama_base_url": runtime_base_url if config.mode == "fake" else config.ollama_base_url,
            "read_only_remote": True,
        },
        "summary": _summary(
            status=status,
            config=config,
            ledger=ledger,
            result_summary=result_summary,
            errors=errors,
        ),
        "steps": steps,
        "job": job_summary,
        "result": result_summary,
        "ledger": ledger,
        "final_status": (snapshot or {}).get("status"),
        "fake_ollama": {
            "enabled": config.mode == "fake",
            "request_count": len(fake_requests),
            "requests": fake_requests,
        },
        "errors": errors,
        "artifacts": {
            "json": str(out_dir / "funded-chat-smoke.json"),
            "markdown": str(out_dir / "funded-chat-smoke.md"),
        },
    }
    _write_report(report)
    return report


def format_funded_chat_smoke_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        f"Funded chat smoke: {str(report.get('status', 'unknown')).upper()}",
        f"Mode: {(report.get('config') or {}).get('mode', 'unknown')}",
        f"Requester balance: {summary.get('requester_balance_after')}",
        f"Worker balance: {summary.get('worker_balance_after')}",
        f"Ledger entries: {summary.get('ledger_entries')}",
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


def format_funded_chat_smoke_markdown(report: dict[str, Any]) -> str:
    config = report.get("config") or {}
    summary = report.get("summary") or {}
    lines = [
        "# ChatP2P Funded Chat Smoke",
        "",
        f"- Status: **{str(report.get('status', 'unknown')).upper()}**",
        f"- Mode: `{config.get('mode', 'unknown')}`",
        f"- Model: `{config.get('model', 'unknown')}`",
        f"- Requester account: `{config.get('requester_account_id', 'unknown')}`",
        f"- Requester balance after: `{summary.get('requester_balance_after')}`",
        f"- Worker balance after: `{summary.get('worker_balance_after')}`",
        f"- Ledger entries: `{summary.get('ledger_entries')}`",
        f"- Recommended next action: `{summary.get('recommended_next_action')}`",
        "",
        "## Product Loop",
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


def _validate_config(config: FundedChatSmokeConfig) -> None:
    if config.mode not in {"fake", "ollama"}:
        raise ValueError("--mode must be fake or ollama")
    if not config.model.strip():
        raise ValueError("--model must be non-empty")
    if not config.prompt.strip():
        raise ValueError("--prompt must be non-empty")
    if not config.requester_account_id.strip():
        raise ValueError("--requester-account-id must be non-empty")
    if config.starting_credits < 0:
        raise ValueError("--starting-credits must be at least 0")
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
    if config.ollama_timeout_seconds <= 0:
        raise ValueError("--ollama-timeout-seconds must be greater than 0")


def _messages_from_config(config: FundedChatSmokeConfig) -> list[dict[str, str]]:
    messages = []
    if config.system and config.system.strip():
        messages.append({"role": "system", "content": config.system.strip()})
    messages.append({"role": "user", "content": config.prompt.strip()})
    return messages


def _ollama_capabilities(*, models: list[str], base_url: str) -> dict[str, Any]:
    unique_models = sorted(set(model for model in models if model.strip()))
    return capabilities_from_benchmark(
        {
            "hardware": {
                "cpu_count": 8,
                "ram_total_mb": 16_000,
                "system": "LocalSmoke",
            },
            "gpu": {"available": False, "provider": None, "devices": [], "total_vram_mb": None},
            "benchmark": {"cpu_iterations_per_second": 10_000},
            "model_runtimes": {
                "ollama": {
                    "available": True,
                    "path": "funded-chat-smoke",
                    "base_url": base_url,
                    "models": unique_models,
                    "model_discovery_error": None,
                }
            },
        }
    )


def _start_fake_ollama(*, model: str, answer: str) -> _FakeOllamaRuntime:
    requests: list[dict[str, Any]] = []

    class FakeOllamaHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/api/tags":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"models": [{"name": model}]}).encode("utf-8"))

        def do_POST(self) -> None:
            if self.path != "/api/generate":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw) if raw else {}
            requests.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "model": payload.get("model") or model,
                        "response": answer,
                        "done": True,
                        "eval_count": max(1, len(answer.split())),
                        "total_duration": 1,
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return _FakeOllamaRuntime(server=server, thread=thread, base_url=f"http://{host}:{port}", requests=requests)


def _fake_request_summaries(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for request in requests:
        options = request.get("options") if isinstance(request.get("options"), dict) else {}
        summaries.append(
            {
                "model": request.get("model"),
                "stream": request.get("stream"),
                "temperature": options.get("temperature"),
                "prompt_preview": _preview(str(request.get("prompt", ""))),
            }
        )
    return summaries


def _summary(
    *,
    status: str,
    config: FundedChatSmokeConfig,
    ledger: dict[str, Any],
    result_summary: dict[str, Any] | None,
    errors: list[str],
) -> dict[str, Any]:
    balances = (ledger.get("summary") or {}).get("balances") or {}
    worker_id = (result_summary or {}).get("node_id")
    output = (result_summary or {}).get("output") or {}
    return {
        "status": status,
        "requester_balance_after": balances.get(config.requester_account_id),
        "worker_node_id": worker_id,
        "worker_balance_after": balances.get(worker_id) if worker_id else None,
        "ledger_entries": (ledger.get("summary") or {}).get("entries", 0),
        "answer": output.get("answer"),
        "job_verified": status == "pass",
        "recommended_next_action": _recommended_next_action(status=status, errors=errors),
    }


def _recommended_next_action(*, status: str, errors: list[str]) -> str:
    if status == "pass":
        return "continue_to_chat_ui_mvp"
    joined = "\n".join(errors).lower()
    if "negative" in joined or "credit" in joined:
        return "increase_requester_credits"
    if "ollama" in joined:
        return "start_ollama_or_use_fake_mode"
    if "lease" in joined or "eligible worker" in joined:
        return "check_worker_capabilities"
    return "inspect_funded_chat_smoke_report"


def _write_report(report: dict[str, Any]) -> None:
    artifacts = report["artifacts"]
    Path(artifacts["json"]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    Path(artifacts["markdown"]).write_text(format_funded_chat_smoke_markdown(report), encoding="utf-8")


def _step(name: str, status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "ok": status in {"pass", "skipped"}, "status": status, "details": details or {}}


def _first_matching(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get(key) == value), None)


def _preview(value: str, limit: int = 240) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."

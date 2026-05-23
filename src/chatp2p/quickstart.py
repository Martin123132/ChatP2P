"""One-command local product loop for ChatP2P."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import CoordinatorClient
from .node_runtime import default_coordinator_url, start_managed_process, stop_managed_process
from .ollama import DEFAULT_OLLAMA_BASE_URL

QUICKSTART_REPORT_SCHEMA = "chatp2p.quickstart-report.v1"


@dataclass(frozen=True)
class QuickstartConfig:
    home: Path = Path(".mesh/quickstart")
    host: str = "127.0.0.1"
    port: int = 8766
    prompt: str = "ChatP2P quickstart: echo this signed job."
    timeout_seconds: float = 45.0
    poll_interval: float = 0.25
    worker_interval: float = 0.5
    force: bool = False
    stop_after_job: bool = False
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL


def run_quickstart(config: QuickstartConfig) -> dict[str, Any]:
    _validate_quickstart_config(config)
    started_at = time.time()
    home = config.home.expanduser().resolve()
    coordinator_url = default_coordinator_url(config.host, config.port)
    steps: list[dict[str, Any]] = []
    errors: list[str] = []
    job_summary: dict[str, Any] | None = None
    result_summary: dict[str, Any] | None = None
    final_snapshot: dict[str, Any] | None = None

    coordinator_result = start_managed_process(
        home=home,
        role="coordinator",
        argv=_coordinator_argv(config, home),
        coordinator_url=coordinator_url,
        force=config.force,
        extra_state={"listen": {"host": config.host, "port": config.port}, "started_by": "quickstart"},
    )
    steps.append({"step": "start_coordinator", **_managed_step_summary(coordinator_result)})

    try:
        client = CoordinatorClient(coordinator_url, timeout_seconds=5.0)
        health = _wait_for_health(client, timeout_seconds=config.timeout_seconds, poll_interval=config.poll_interval)
        steps.append({"step": "coordinator_health", "ok": True, "status": "pass", "details": health})

        worker_result = start_managed_process(
            home=home,
            role="worker",
            argv=_worker_argv(config, home, coordinator_url),
            coordinator_url=coordinator_url,
            force=config.force,
            extra_state={"worker_interval": config.worker_interval, "started_by": "quickstart"},
        )
        steps.append({"step": "connect_worker", **_managed_step_summary(worker_result)})

        worker = _wait_for_live_worker(
            client,
            timeout_seconds=config.timeout_seconds,
            poll_interval=config.poll_interval,
        )
        steps.append({"step": "worker_live", "ok": True, "status": "pass", "details": worker})

        job = client.create_job(
            job_type="inference.echo.v1",
            payload={"prompt": config.prompt},
            ttl_seconds=120,
        )
        steps.append({"step": "run_job", "ok": True, "status": "created", "job_id": job.job_id})

        job_summary, result_summary, final_snapshot = _wait_for_job_result(
            client,
            job_id=job.job_id,
            timeout_seconds=config.timeout_seconds,
            poll_interval=config.poll_interval,
        )
        steps.append({"step": "see_result", "ok": True, "status": "pass", "details": result_summary})
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if config.stop_after_job:
            steps.append({"step": "stop_worker", **stop_managed_process(home=home, role="worker")})
            steps.append({"step": "stop_coordinator", **stop_managed_process(home=home, role="coordinator")})

    ok = not errors and bool(job_summary and job_summary.get("status") == "verified") and result_summary is not None
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "schema": QUICKSTART_REPORT_SCHEMA,
        "duration_seconds": round(time.time() - started_at, 3),
        "home": str(home),
        "coordinator": coordinator_url,
        "dashboard": f"{coordinator_url}/dashboard",
        "steps": steps,
        "job": job_summary,
        "result": result_summary,
        "final_status": (final_snapshot or {}).get("status"),
        "errors": errors,
    }


def format_quickstart_report(report: dict[str, Any]) -> str:
    lines = [
        f"ChatP2P quickstart: {report['status']}",
        f"Dashboard: {report['dashboard']}",
        f"Home: {report['home']}",
    ]
    job = report.get("job") or {}
    result = report.get("result") or {}
    if job:
        lines.append(f"Job: {job.get('job_id')} ({job.get('status')})")
    if result:
        output = result.get("output") or {}
        lines.append(f"Worker: {result.get('node_id')}")
        lines.append(f"Result: {output.get('answer')}")
    if report.get("errors"):
        lines.append("Errors:")
        lines.extend(f"- {error}" for error in report["errors"])
    if report.get("ok"):
        lines.append("Repeat: run the same command again to create another job.")
    return "\n".join(lines)


def _validate_quickstart_config(config: QuickstartConfig) -> None:
    if not config.host.strip():
        raise ValueError("--host must be non-empty")
    if config.port < 1 or config.port > 65535:
        raise ValueError("--port must be between 1 and 65535")
    if not config.prompt.strip():
        raise ValueError("--prompt must be non-empty")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.worker_interval <= 0:
        raise ValueError("--worker-interval must be greater than 0")


def _coordinator_argv(config: QuickstartConfig, home: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "chatp2p.cli",
        "coordinator",
        "serve",
        "--home",
        str(home),
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--lease-timeout-seconds",
        "30",
        "--node-stale-seconds",
        "60",
    ]


def _worker_argv(config: QuickstartConfig, home: Path, coordinator_url: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "chatp2p.cli",
        "worker",
        "loop",
        "--home",
        str(home),
        "--coordinator",
        coordinator_url,
        "--ollama-base-url",
        config.ollama_base_url,
        "--interval",
        str(config.worker_interval),
    ]


def _managed_step_summary(result: dict[str, Any]) -> dict[str, Any]:
    state = result.get("state") or {}
    return {
        "ok": result.get("status") in {"started", "already_running"},
        "status": result.get("status"),
        "pid": state.get("pid"),
        "alive": state.get("alive"),
    }


def _wait_for_health(
    client: CoordinatorClient,
    *,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_error = "not attempted"
    while time.time() <= deadline:
        try:
            return client.health()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(poll_interval)
    raise TimeoutError(f"coordinator did not become healthy: {last_error}")


def _wait_for_live_worker(
    client: CoordinatorClient,
    *,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() <= deadline:
        snapshot = client.snapshot()
        live_nodes = [
            node for node in snapshot.get("nodes", [])
            if node.get("liveness_status") == "live"
        ]
        if live_nodes:
            return {
                "live_workers": len(live_nodes),
                "node_id": live_nodes[0].get("node_id"),
                "supported_job_types": live_nodes[0].get("supported_job_types", []),
            }
        time.sleep(poll_interval)
    raise TimeoutError("worker did not appear live in the coordinator snapshot")


def _wait_for_job_result(
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
        last_job = next((job for job in snapshot.get("jobs", []) if job.get("job_id") == job_id), None)
        result = next((item for item in snapshot.get("results", []) if item.get("job_id") == job_id), None)
        if last_job and last_job.get("status") == "verified" and result:
            return last_job, result, snapshot
        if last_job and last_job.get("status") in {"disputed", "expired"}:
            raise RuntimeError(f"job became {last_job.get('status')}")
        time.sleep(poll_interval)
    raise TimeoutError(f"job did not verify before timeout: {job_id}; last status={last_job}")

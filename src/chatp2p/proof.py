"""Local reliability proof harness for the signed coordinator protocol."""

from __future__ import annotations

import json
import multiprocessing
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import CoordinatorClient
from .coordinator import Coordinator
from .crypto import NodeIdentity
from .http_api import create_coordinator_http_server
from .packets import NodeRegistration
from .storage import SQLiteCoordinatorStore
from .worker import WorkerNode


@dataclass(frozen=True)
class SwarmProofConfig:
    workers: int = 25
    jobs: int = 100
    work_dir: Path = Path(".mesh/proof")
    report_path: Path = Path(".mesh/proof/reliability-report.json")
    timeout_seconds: float = 120.0
    lease_timeout_seconds: float = 10.0
    poll_interval: float = 0.5
    worker_interval: float = 0.1
    fault_timeout_workers: int = 0


def run_swarm_proof(config: SwarmProofConfig) -> dict[str, Any]:
    """Run a one-machine multi-process swarm proof and write a JSON report."""

    _validate_config(config)
    run_id = f"proof_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_dir = config.work_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config.report_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(
        identity=coordinator_identity,
        store=SQLiteCoordinatorStore(run_dir / "coordinator.sqlite3"),
        lease_timeout_seconds=config.lease_timeout_seconds,
        node_stale_seconds=max(config.lease_timeout_seconds * 4, 10.0),
    )
    for index in range(config.jobs):
        coordinator.create_job(
            job_type="eval.deterministic.v1",
            payload=_deterministic_payload(index),
            ttl_seconds=max(300, int(config.timeout_seconds + 60)),
        )

    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    result_queue = context.Queue()
    processes: list[multiprocessing.Process] = []
    worker_reports: dict[int, dict[str, Any]] = {}

    try:
        fault_count = config.fault_timeout_workers
        if fault_count:
            processes.extend(
                _start_worker_processes(
                    context=context,
                    base_url=base_url,
                    run_dir=run_dir,
                    stop_event=stop_event,
                    result_queue=result_queue,
                    indexes=range(fault_count),
                    interval=config.worker_interval,
                    fault_once=True,
                )
            )
            deadline = time.time() + min(15.0, max(5.0, config.lease_timeout_seconds * 3))
            while len(worker_reports) < fault_count and time.time() < deadline:
                worker_reports.update(_drain_worker_reports(result_queue))
                time.sleep(0.05)

        processes.extend(
            _start_worker_processes(
                context=context,
                base_url=base_url,
                run_dir=run_dir,
                stop_event=stop_event,
                result_queue=result_queue,
                indexes=range(fault_count, config.workers),
                interval=config.worker_interval,
                fault_once=False,
            )
        )

        client = CoordinatorClient(base_url)
        final_snapshot = client.snapshot()
        terminal = False
        timed_out = False
        deadline = started_at + config.timeout_seconds
        while True:
            worker_reports.update(_drain_worker_reports(result_queue))
            final_snapshot = client.snapshot()
            status = final_snapshot["status"]
            terminal = status["verified_jobs"] + status["disputed_jobs"] >= config.jobs
            if terminal:
                break
            if time.time() >= deadline:
                timed_out = True
                break
            time.sleep(config.poll_interval)
    finally:
        stop_event.set()
        for process in processes:
            process.join(timeout=5)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        worker_reports.update(_drain_worker_reports(result_queue))
        try:
            final_snapshot = CoordinatorClient(base_url).snapshot()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    finished_at = time.time()
    for process in processes:
        if process.pid is None:
            continue
        report = worker_reports.setdefault(
            _worker_index_from_name(process.name),
            {"worker_index": _worker_index_from_name(process.name), "errors": []},
        )
        report["process_exitcode"] = process.exitcode

    report = _build_report(
        config=config,
        run_id=run_id,
        run_dir=run_dir,
        coordinator_url=base_url,
        started_at=started_at,
        finished_at=finished_at,
        final_snapshot=final_snapshot,
        worker_reports=worker_reports,
        timed_out=timed_out,
        terminal=terminal,
    )
    config.report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def proof_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Return a small CLI-friendly summary for a full proof report."""

    return {
        "passed": report["passed"],
        "run_id": report["run_id"],
        "report": report["report_path"],
        "duration_seconds": report["duration_seconds"],
        "workers_registered": report["workers_registered"],
        "jobs_created": report["jobs_created"],
        "verified_jobs": report["verified_jobs"],
        "disputed_jobs": report["disputed_jobs"],
        "pending_jobs": report["pending_jobs"],
        "queued_jobs": report["queued_jobs"],
        "leased_jobs": report["leased_jobs"],
        "expired_leases": report["expired_leases"],
        "accepted_results": report["accepted_results"],
        "capability_tiers": report["capability_tiers"],
        "worker_error_count": len(report["worker_errors"]),
    }


def _validate_config(config: SwarmProofConfig) -> None:
    if config.workers < 1:
        raise ValueError("--workers must be at least 1")
    if config.jobs < 1:
        raise ValueError("--jobs must be at least 1")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.lease_timeout_seconds <= 0:
        raise ValueError("--lease-timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.worker_interval <= 0:
        raise ValueError("--worker-interval must be greater than 0")
    if config.fault_timeout_workers < 0:
        raise ValueError("--fault-timeout-workers cannot be negative")
    if config.fault_timeout_workers >= config.workers:
        raise ValueError("--fault-timeout-workers must leave at least one normal worker")


def _start_worker_processes(
    *,
    context: multiprocessing.context.BaseContext,
    base_url: str,
    run_dir: Path,
    stop_event: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.Queue,
    indexes: range,
    interval: float,
    fault_once: bool,
) -> list[multiprocessing.Process]:
    processes = []
    for index in indexes:
        process = context.Process(
            name=f"chatp2p-proof-worker-{index}",
            target=_worker_process,
            args=(
                index,
                base_url,
                str(run_dir / "workers" / f"worker-{index}"),
                stop_event,
                result_queue,
                interval,
                fault_once,
            ),
        )
        process.start()
        processes.append(process)
    return processes


def _worker_process(
    worker_index: int,
    base_url: str,
    home: str,
    stop_event: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.Queue,
    interval: float,
    fault_once: bool,
) -> None:
    report: dict[str, Any] = {
        "worker_index": worker_index,
        "role": "fault-timeout" if fault_once else "worker",
        "registered": False,
        "node_id": None,
        "completed_jobs": 0,
        "accepted_results": 0,
        "rejected_results": 0,
        "idle_polls": 0,
        "leased_without_submit": False,
        "leased_job_id": None,
        "errors": [],
    }
    try:
        identity = _load_or_create_identity(Path(home), "worker")
        worker = WorkerNode(identity=identity)
        client = CoordinatorClient(base_url)
        report["node_id"] = identity.node_id
        registration = NodeRegistration.create(node=identity, capabilities=worker.capabilities())
        register_response = client.register(registration)
        report["registered"] = bool(register_response.get("accepted"))
        if not report["registered"]:
            report["errors"].append(f"registration rejected: {register_response}")
            result_queue.put(report)
            return

        if fault_once:
            job = client.next_job(identity)
            if job is None:
                report["errors"].append("fault worker did not receive a lease")
            else:
                report["leased_without_submit"] = True
                report["leased_job_id"] = job.job_id
            result_queue.put(report)
            return

        while not stop_event.is_set():
            try:
                job = client.next_job(identity)
                if job is None:
                    report["idle_polls"] += 1
                    time.sleep(interval)
                    continue

                result = worker.run_job(job)
                submit_response = client.submit_result(result)
                if submit_response.get("accepted"):
                    report["accepted_results"] += 1
                else:
                    report["rejected_results"] += 1
                    report["errors"].append(f"result rejected: {submit_response}")
                report["completed_jobs"] += 1
            except Exception as exc:  # pragma: no cover - retained in report for diagnosis.
                report["errors"].append(f"{type(exc).__name__}: {exc}")
                time.sleep(interval)
    except Exception as exc:  # pragma: no cover - process boundary failure path.
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        result_queue.put(report)


def _load_or_create_identity(home: Path, name: str) -> NodeIdentity:
    path = home / f"{name}.identity.json"
    if path.exists():
        return NodeIdentity.load(path)
    identity = NodeIdentity.generate(prefix=name)
    identity.save(path)
    return identity


def _deterministic_payload(index: int) -> dict[str, Any]:
    variant = index % 4
    if variant == 0:
        left = index + 7
        right = (index * 3) + 11
        return {
            "task": "arithmetic",
            "operation": "add",
            "operands": [left, right],
            "expected": left + right,
        }
    if variant == 1:
        left = (index % 12) + 2
        right = (index % 7) + 3
        return {
            "task": "arithmetic",
            "operation": "multiply",
            "operands": [left, right],
            "expected": left * right,
        }
    if variant == 2:
        value = [97, 101, 103, 107, 109, 113, 127][(index // 4) % 7]
        return {
            "task": "number_theory",
            "check": "is_prime",
            "value": value,
            "expected": True,
        }
    value = f"open     compute\nmesh proof   {index}"
    return {
        "task": "text",
        "operation": "normalize_whitespace",
        "value": value,
        "expected": " ".join(value.split()),
    }


def _drain_worker_reports(result_queue: multiprocessing.Queue) -> dict[int, dict[str, Any]]:
    reports = {}
    while True:
        try:
            report = result_queue.get_nowait()
        except queue.Empty:
            return reports
        reports[int(report["worker_index"])] = report


def _build_report(
    *,
    config: SwarmProofConfig,
    run_id: str,
    run_dir: Path,
    coordinator_url: str,
    started_at: float,
    finished_at: float,
    final_snapshot: dict[str, Any],
    worker_reports: dict[int, dict[str, Any]],
    timed_out: bool,
    terminal: bool,
) -> dict[str, Any]:
    status = final_snapshot["status"]
    duration = round(finished_at - started_at, 3)
    accepted_results = len(final_snapshot["results"])
    capability_tiers: dict[str, int] = {}
    for node in final_snapshot["nodes"]:
        tier = node.get("capability_tier", "light")
        capability_tiers[tier] = capability_tiers.get(tier, 0) + 1
    worker_summaries = [worker_reports[index] for index in sorted(worker_reports)]
    worker_errors = [
        {
            "worker_index": summary.get("worker_index"),
            "node_id": summary.get("node_id"),
            "errors": summary.get("errors", []),
            "process_exitcode": summary.get("process_exitcode"),
        }
        for summary in worker_summaries
        if summary.get("errors") or summary.get("process_exitcode") not in {0, None}
    ]
    passed = (
        not timed_out
        and terminal
        and status["verified_jobs"] == config.jobs
        and status["disputed_jobs"] == 0
        and status["queued_jobs"] == 0
        and status["pending_jobs"] == 0
        and status["leased_jobs"] == 0
        and status["known_nodes"] == config.workers
        and not worker_errors
    )
    return {
        "run_id": run_id,
        "started_at": round(started_at, 3),
        "finished_at": round(finished_at, 3),
        "duration_seconds": duration,
        "timed_out": timed_out,
        "terminal": terminal,
        "passed": passed,
        "coordinator_url": coordinator_url,
        "run_dir": str(run_dir),
        "report_path": str(config.report_path),
        "parameters": {
            "workers": config.workers,
            "jobs": config.jobs,
            "timeout_seconds": config.timeout_seconds,
            "lease_timeout_seconds": config.lease_timeout_seconds,
            "poll_interval": config.poll_interval,
            "worker_interval": config.worker_interval,
            "fault_timeout_workers": config.fault_timeout_workers,
        },
        "workers_requested": config.workers,
        "workers_registered": status["known_nodes"],
        "jobs_created": config.jobs,
        "verified_jobs": status["verified_jobs"],
        "disputed_jobs": status["disputed_jobs"],
        "pending_jobs": status["pending_jobs"],
        "queued_jobs": status["queued_jobs"],
        "leased_jobs": status["leased_jobs"],
        "expired_leases": status["expired_leases"],
        "accepted_results": accepted_results,
        "capability_tiers": capability_tiers,
        "throughput_jobs_per_second": round(status["verified_jobs"] / duration, 3) if duration else None,
        "throughput_results_per_second": round(accepted_results / duration, 3) if duration else None,
        "per_worker_credits": dict(status["credits"]),
        "worker_summaries": worker_summaries,
        "worker_errors": worker_errors,
        "final_snapshot": final_snapshot,
    }


def _worker_index_from_name(name: str | None) -> int:
    if not name:
        return -1
    try:
        return int(name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return -1

"""Command line interface for the ChatP2P prototype."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from .alpha import (
    AlphaDrillConfig,
    AlphaJoinConfig,
    AlphaPreflightConfig,
    AlphaRemoteProofConfig,
    AlphaRouteConfig,
    AlphaSmokeConfig,
    AlphaStatusConfig,
    DEFAULT_ALPHA_NOTES,
    NodeWatchdogConfig,
    bootstrap_alpha,
    run_alpha_drill,
    run_alpha_join,
    run_alpha_preflight,
    run_alpha_remote_proof,
    run_alpha_route,
    run_alpha_smoke,
    run_alpha_status,
    run_node_watchdog,
)
from .benchmark import CAPABILITY_PROFILE_NAME, load_node_capabilities, run_node_benchmark, save_node_benchmark
from .client import CoordinatorClient
from .coordinator import Coordinator
from .crypto import NodeIdentity
from .doctor import NodeDoctorConfig, run_node_doctor
from .http_api import create_coordinator_http_server
from .node_runtime import (
    MANAGED_ROLES,
    default_coordinator_url,
    managed_processes_status,
    start_managed_process,
    stop_managed_process,
)
from .ollama import DEFAULT_OLLAMA_BASE_URL
from .operator_config import OperatorConfig, write_operator_config
from .packets import NodeRegistration
from .proof import OllamaProofConfig, SwarmProofConfig, proof_summary, run_ollama_proof, run_swarm_proof
from .storage import SQLiteCoordinatorStore
from .worker import WorkerNode
from .windows_task import (
    DEFAULT_TASK_NAME,
    WatchdogTaskConfig,
    install_watchdog_task,
    uninstall_watchdog_task,
)


def _identity_path(home: Path, name: str) -> Path:
    return home / f"{name}.identity.json"


def _capabilities_path(home: Path) -> Path:
    return home / CAPABILITY_PROFILE_NAME


def _load_or_create_identity(home: Path, name: str) -> NodeIdentity:
    path = _identity_path(home, name)
    if path.exists():
        return NodeIdentity.load(path)
    identity = NodeIdentity.generate(prefix=name)
    identity.save(path)
    return identity


def _load_worker(home: Path, ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL) -> WorkerNode:
    identity = _load_or_create_identity(home, "worker")
    return WorkerNode(
        identity=identity,
        capability_profile=load_node_capabilities(home),
        ollama_base_url=ollama_base_url,
    )


def _coordinator_client(args: argparse.Namespace) -> CoordinatorClient:
    return CoordinatorClient(
        args.coordinator,
        admission_token=getattr(args, "admission_token", None),
    )


def _selected_managed_roles(role: str) -> tuple[str, ...]:
    return MANAGED_ROLES if role == "both" else (role,)


def _append_optional_arg(argv: list[str], flag: str, value: Any) -> None:
    if value is not None:
        argv.extend([flag, str(value)])


def _append_repeated_arg(argv: list[str], flag: str, values: list[str] | None) -> None:
    for value in values or []:
        argv.extend([flag, value])


def _admission_token_for_worker(args: argparse.Namespace) -> str | None:
    if args.admission_token:
        return args.admission_token
    if not args.operator_config:
        return None
    try:
        return OperatorConfig.from_file(Path(args.operator_config)).admission_token
    except (OSError, ValueError) as exc:
        raise SystemExit(f"could not read operator config token: {exc}") from exc


def _coordinator_url_from_node_args(args: argparse.Namespace) -> str:
    return args.coordinator or default_coordinator_url(args.host, args.port)


def _operator_config_from_args(args: argparse.Namespace) -> OperatorConfig:
    config = (
        OperatorConfig.from_file(Path(args.operator_config))
        if args.operator_config
        else OperatorConfig.default()
    )
    public_alpha = True if args.public_alpha or args.admission_token else None
    return config.with_overrides(
        public_alpha=public_alpha,
        admission_token=args.admission_token,
        max_request_bytes=args.max_request_bytes,
        max_job_payload_bytes=args.max_job_payload_bytes,
        allowed_job_types=args.allowed_job_type,
    )


def _parse_json_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_number(raw: str) -> int | float:
    value = json.loads(raw)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SystemExit(f"Expected a number, got {raw!r}")
    return value


def _is_prime(value: int) -> bool:
    if value < 2:
        return False
    if value == 2:
        return True
    if value % 2 == 0:
        return False
    divisor = 3
    while divisor * divisor <= value:
        if value % divisor == 0:
            return False
        divisor += 2
    return True


def _default_expected(payload: dict):
    task = payload["task"]
    if task == "arithmetic":
        left, right = payload["operands"]
        operation = payload["operation"]
        if operation == "add":
            return left + right
        if operation == "subtract":
            return left - right
        if operation == "multiply":
            return left * right
        if operation == "divide":
            return left / right
    if task == "number_theory":
        return _is_prime(payload["value"])
    if task == "text":
        return " ".join(payload["value"].split())
    raise SystemExit(f"Cannot infer expected value for task {task!r}")


def _build_deterministic_payload(args: argparse.Namespace) -> dict:
    if args.task == "arithmetic":
        if args.operation is None:
            raise SystemExit("--operation is required for arithmetic jobs")
        if args.operands is None:
            raise SystemExit("--operands LEFT RIGHT is required for arithmetic jobs")
        payload = {
            "task": "arithmetic",
            "operation": args.operation,
            "operands": [_parse_number(args.operands[0]), _parse_number(args.operands[1])],
        }
    elif args.task == "number_theory":
        if args.value is None:
            raise SystemExit("--value is required for number_theory jobs")
        parsed_value = _parse_number(args.value)
        if not isinstance(parsed_value, int):
            raise SystemExit("--value must be an integer for number_theory jobs")
        payload = {
            "task": "number_theory",
            "check": "is_prime",
            "value": parsed_value,
        }
    elif args.task == "text":
        if args.value is None:
            raise SystemExit("--value is required for text jobs")
        payload = {
            "task": "text",
            "operation": "normalize_whitespace",
            "value": args.value,
        }
    else:
        raise SystemExit(f"Unsupported deterministic task: {args.task}")

    payload["expected"] = _parse_json_value(args.expected) if args.expected is not None else _default_expected(payload)
    return payload


def init_identity(args: argparse.Namespace) -> None:
    home = Path(args.home)
    path = _identity_path(home, args.name)
    if path.exists() and not args.force:
        raise SystemExit(f"Identity already exists at {path}. Use --force to replace it.")

    identity = NodeIdentity.generate(prefix=args.name)
    identity.save(path)
    print(f"created identity: {identity.node_id}")
    print(f"path: {path}")


def run_demo(args: argparse.Namespace) -> None:
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    worker_identity = NodeIdentity.generate(prefix="worker")

    coordinator = Coordinator(identity=coordinator_identity)
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public())

    job = coordinator.create_math_eval_job()
    result = worker.run_job(job)
    accepted = coordinator.submit_result(result)

    report = {
        "coordinator": coordinator_identity.node_id,
        "worker": worker_identity.node_id,
        "job_id": job.job_id,
        "job_signature_valid": job.verify_signature(),
        "result_signature_valid": result.verify_signature(),
        "result_accepted": accepted,
        "worker_credits": coordinator.credits[worker_identity.node_id],
        "output": result.output,
    }

    print(json.dumps(report, indent=2, sort_keys=True))


def serve_coordinator(args: argparse.Namespace) -> None:
    home = Path(args.home)
    identity = _load_or_create_identity(home, "coordinator")
    db_path = Path(args.db) if args.db else home / "coordinator.sqlite3"
    try:
        operator_config = _operator_config_from_args(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    coordinator = Coordinator(
        identity=identity,
        store=SQLiteCoordinatorStore(db_path),
        lease_timeout_seconds=args.lease_timeout_seconds,
        node_stale_seconds=args.node_stale_seconds,
    )
    if args.seed_math_job:
        coordinator.create_math_eval_job()
    if args.seed_eval_suite:
        coordinator.create_deterministic_eval_jobs()

    server = create_coordinator_http_server(
        coordinator,
        host=args.host,
        port=args.port,
        operator_config=operator_config,
    )
    print(f"coordinator: {identity.node_id}")
    print(f"listening: http://{args.host}:{args.port}")
    print(f"database: {db_path}")
    print(f"operator: {json.dumps(operator_config.public_summary(), sort_keys=True)}")
    if args.seed_math_job:
        print("seeded: eval.math.v1")
    if args.seed_eval_suite:
        print("seeded: eval.deterministic.v1 suite")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down coordinator")
    finally:
        server.server_close()


def run_worker_once(args: argparse.Namespace) -> None:
    worker = _load_worker(Path(args.home), ollama_base_url=args.ollama_base_url)
    client = _coordinator_client(args)

    _register_worker(client, worker)
    result = _run_one_remote_job(client, worker)
    print(json.dumps(result, indent=2, sort_keys=True))


def _register_worker(client: CoordinatorClient, worker: WorkerNode) -> None:
    registration = NodeRegistration.create(node=worker.identity, capabilities=worker.capabilities())
    register_response = client.register(registration)
    if not register_response.get("accepted"):
        raise SystemExit(f"registration rejected: {register_response}")


def _run_one_remote_job(client: CoordinatorClient, worker: WorkerNode) -> dict:
    job = client.next_job(worker.identity)
    if job is None:
        return {"worker": worker.identity.node_id, "job": None, "status": "idle"}

    result = worker.run_job(job)
    submit_response = client.submit_result(result)
    return {
        "worker": worker.identity.node_id,
        "job_id": job.job_id,
        "job_type": job.job_type,
        "result_accepted": submit_response.get("accepted"),
        "credits": submit_response.get("credits"),
        "output": result.output,
        "status": "submitted" if submit_response.get("accepted") else "rejected",
    }


def run_worker_loop(args: argparse.Namespace) -> None:
    worker = _load_worker(Path(args.home), ollama_base_url=args.ollama_base_url)
    identity = worker.identity
    client = _coordinator_client(args)
    _register_worker(client, worker)

    completed = 0
    while True:
        timestamp = time.strftime("%H:%M:%S")
        try:
            result = _run_one_remote_job(client, worker)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(
                f"[{timestamp}] {identity.node_id} transient-error "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(args.interval)
            continue
        if result["status"] == "idle":
            print(f"[{timestamp}] {identity.node_id} idle")
            if args.stop_when_idle:
                return
        else:
            completed += 1
            print(
                f"[{timestamp}] {identity.node_id} {result['status']} "
                f"{result['job_type']} {result['job_id']} credits={result['credits']}"
            )
            if args.max_jobs is not None and completed >= args.max_jobs:
                return
        time.sleep(args.interval)


def create_generic_job(args: argparse.Namespace) -> None:
    payload = json.loads(args.payload_json)
    client = _coordinator_client(args)
    job = client.create_job(
        job_type=args.job_type,
        payload=payload,
        model_id=args.model_id,
        reward=args.reward,
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps({"created": True, "job": job.to_dict()}, indent=2, sort_keys=True))


def create_echo_job(args: argparse.Namespace) -> None:
    client = _coordinator_client(args)
    job = client.create_job(
        job_type="inference.echo.v1",
        payload={"prompt": args.prompt},
        reward=args.reward,
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps({"created": True, "job": job.to_dict()}, indent=2, sort_keys=True))


def create_ollama_job(args: argparse.Namespace) -> None:
    client = _coordinator_client(args)
    payload: dict[str, Any] = {"model": args.model, "prompt": args.prompt}
    if args.temperature is not None:
        payload["temperature"] = args.temperature
    job = client.create_job(
        job_type="inference.ollama.v1",
        payload=payload,
        reward=args.reward,
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps({"created": True, "job": job.to_dict()}, indent=2, sort_keys=True))


def create_deterministic_job(args: argparse.Namespace) -> None:
    client = _coordinator_client(args)
    payload = _build_deterministic_payload(args)
    job = client.create_job(
        job_type="eval.deterministic.v1",
        payload=payload,
        reward=args.reward,
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps({"created": True, "job": job.to_dict()}, indent=2, sort_keys=True))


def create_demo_suite(args: argparse.Namespace) -> None:
    client = _coordinator_client(args)
    jobs = client.create_demo_suite()
    print(
        json.dumps(
            {"created": True, "jobs": [job.to_dict() for job in jobs]},
            indent=2,
            sort_keys=True,
        )
    )


def list_jobs(args: argparse.Namespace) -> None:
    client = _coordinator_client(args)
    print(json.dumps(client.jobs(), indent=2, sort_keys=True))


def show_snapshot(args: argparse.Namespace) -> None:
    client = _coordinator_client(args)
    print(json.dumps(client.snapshot(), indent=2, sort_keys=True))


def show_reputation(args: argparse.Namespace) -> None:
    client = _coordinator_client(args)
    print(json.dumps(client.reputation(), indent=2, sort_keys=True))


def write_operator_config_command(args: argparse.Namespace) -> None:
    config = OperatorConfig(
        public_alpha=True,
        admission_token=args.admission_token,
        max_request_bytes=args.max_request_bytes,
        max_job_payload_bytes=args.max_job_payload_bytes,
        allowed_job_types=tuple(args.allowed_job_type or OperatorConfig.default().allowed_job_types),
    )
    try:
        config.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    path = Path(args.output)
    if path.exists() and not args.force:
        raise SystemExit(f"Operator config already exists at {path}. Use --force to replace it.")
    write_operator_config(path, config)
    print(json.dumps({"saved": str(path), "operator": config.public_summary()}, indent=2, sort_keys=True))


def bootstrap_alpha_command(args: argparse.Namespace) -> None:
    try:
        report = bootstrap_alpha(
            config_path=Path(args.config),
            invite_path=Path(args.invite),
            coordinator_url=args.coordinator_url,
            admission_token=args.admission_token,
            max_request_bytes=args.max_request_bytes,
            max_job_payload_bytes=args.max_job_payload_bytes,
            allowed_job_types=tuple(args.allowed_job_type or OperatorConfig.default().allowed_job_types),
            notes=args.notes,
            force=args.force,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))


def alpha_preflight_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_preflight(
            AlphaPreflightConfig(
                config_path=Path(args.config),
                invite_path=Path(args.invite),
                home=Path(args.home),
                report_path=Path(args.report),
                timeout_seconds=args.timeout_seconds,
            )
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_smoke_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_smoke(
            AlphaSmokeConfig(
                invite_path=Path(args.invite),
                report_path=Path(args.report),
                jobs=args.jobs,
                min_live_workers=args.min_live_workers,
                min_accepted_results=args.min_accepted_results,
                min_verified_jobs=args.min_verified_jobs,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_remote_proof_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_remote_proof(
            AlphaRemoteProofConfig(
                invite_path=Path(args.invite),
                report_path=Path(args.report),
                jobs=args.jobs,
                expected_worker_id=args.expected_worker_id,
                min_live_workers=args.min_live_workers,
                min_accepted_results=args.min_accepted_results,
                min_verified_jobs=args.min_verified_jobs,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_status_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    invite = Path(args.invite) if args.invite else home.parent / "alpha-invite.json"
    report_path = Path(args.report) if args.report else None
    try:
        report = run_alpha_status(
            AlphaStatusConfig(
                home=home,
                invite_path=invite,
                report_path=report_path,
                expected_worker_id=args.expected_worker_id,
                min_live_workers=args.min_live_workers,
                timeout_seconds=args.timeout_seconds,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_drill_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    invite = Path(args.invite) if args.invite else home.parent / "alpha-invite.json"
    config = Path(args.config) if args.config else home.parent / "operator-config.json"
    config_path = config if args.config or config.exists() else None
    report = Path(args.report) if args.report else home.parent / "alpha-drill-report.json"
    try:
        drill_report = run_alpha_drill(
            AlphaDrillConfig(
                home=home,
                invite_path=invite,
                config_path=config_path,
                report_path=report,
                simulated_workers=args.simulated_workers,
                jobs=args.jobs,
                worker_interval=args.worker_interval,
                startup_timeout_seconds=args.startup_timeout_seconds,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
                cpu_duration_seconds=args.cpu_duration_seconds,
                ollama_base_url=args.ollama_base_url,
                start_coordinator=not args.no_start_coordinator,
                coordinator_host=args.coordinator_host,
                coordinator_port=args.coordinator_port,
                lease_timeout_seconds=args.lease_timeout_seconds,
                node_stale_seconds=args.node_stale_seconds,
                start_primary_worker=not args.no_primary_worker,
                force_workers=args.force_workers,
                keep_simulated_workers=not args.cleanup_simulated_workers,
                run_preflight=not args.no_preflight,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(drill_report, indent=2, sort_keys=True))
    if not drill_report["ok"]:
        raise SystemExit(1)


def alpha_route_command(args: argparse.Namespace) -> None:
    home = Path(args.home) if args.home else None
    invite = Path(args.invite) if args.invite else (home.parent / "alpha-invite.json" if home else Path("alpha-invite.json"))
    report = Path(args.report) if args.report else (
        home.parent / "alpha-route-report.json" if home else Path("alpha-route-report.json")
    )
    try:
        route_report = run_alpha_route(
            AlphaRouteConfig(
                invite_path=invite,
                report_path=report,
                home=home,
                candidate_url=args.candidate_url,
                timeout_seconds=args.timeout_seconds,
                detect_tools=not args.no_tool_detection,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(route_report, indent=2, sort_keys=True))
    if route_report["status"] == "fail":
        raise SystemExit(1)


def run_proof_swarm(args: argparse.Namespace) -> None:
    config = SwarmProofConfig(
        workers=args.workers,
        jobs=args.jobs,
        work_dir=Path(args.work_dir),
        report_path=Path(args.report),
        timeout_seconds=args.timeout_seconds,
        lease_timeout_seconds=args.lease_timeout_seconds,
        poll_interval=args.poll_interval,
        worker_interval=args.worker_interval,
        fault_timeout_workers=args.fault_timeout_workers,
    )
    try:
        report = run_swarm_proof(config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(proof_summary(report), indent=2, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


def run_proof_ollama(args: argparse.Namespace) -> None:
    config = OllamaProofConfig(
        workers=args.workers,
        jobs=args.jobs,
        model=args.model,
        prompt=args.prompt,
        work_dir=Path(args.work_dir),
        report_path=Path(args.report),
        timeout_seconds=args.timeout_seconds,
        lease_timeout_seconds=args.lease_timeout_seconds,
        poll_interval=args.poll_interval,
        worker_interval=args.worker_interval,
        ollama_base_url=args.ollama_base_url,
        temperature=args.temperature,
        mismatched_workers=args.mismatched_workers,
    )
    try:
        report = run_ollama_proof(config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(proof_summary(report), indent=2, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


def run_node_benchmark_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    report = run_node_benchmark(
        cpu_duration_seconds=args.cpu_duration_seconds,
        ollama_base_url=args.ollama_base_url,
    )
    output = Path(args.output) if args.output else _capabilities_path(home)
    save_node_benchmark(report, output)
    print(json.dumps({"saved": str(output), **report}, indent=2, sort_keys=True))


def run_node_doctor_command(args: argparse.Namespace) -> None:
    coordinator_url = None if args.skip_coordinator else args.coordinator
    report = run_node_doctor(
        NodeDoctorConfig(
            home=Path(args.home),
            model=args.model,
            ollama_base_url=args.ollama_base_url,
            coordinator_url=coordinator_url,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def run_node_join_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_join(
            AlphaJoinConfig(
                invite_path=Path(args.invite),
                home=Path(args.home),
                ollama_base_url=args.ollama_base_url,
                worker_interval=args.worker_interval,
                startup_timeout_seconds=args.startup_timeout_seconds,
                cpu_duration_seconds=args.cpu_duration_seconds,
                force=args.force,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def run_node_up_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    roles = _selected_managed_roles(args.role)
    coordinator_url = _coordinator_url_from_node_args(args)
    results: list[dict[str, Any]] = []
    health: dict[str, Any] | None = None

    if "coordinator" in roles:
        coordinator_argv = _build_managed_coordinator_argv(args)
        results.append(
            start_managed_process(
                home=home,
                role="coordinator",
                argv=coordinator_argv,
                coordinator_url=coordinator_url,
                force=args.force,
                extra_state={"listen": {"host": args.host, "port": args.port}},
            )
        )

    health = _wait_for_coordinator_health(
        coordinator_url=coordinator_url,
        admission_token=args.admission_token,
        timeout_seconds=args.startup_timeout_seconds,
        poll_interval=0.2,
    )
    if not health["ok"]:
        report = {
            "ok": False,
            "home": str(home.expanduser().resolve()),
            "role": args.role,
            "coordinator": coordinator_url,
            "results": results,
            "coordinator_health": health,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(1)

    if "worker" in roles:
        worker_argv = _build_managed_worker_argv(args, coordinator_url)
        results.append(
            start_managed_process(
                home=home,
                role="worker",
                argv=worker_argv,
                coordinator_url=coordinator_url,
                force=args.force,
                extra_state={"worker_interval": args.worker_interval},
            )
        )

    report = {
        "ok": True,
        "home": str(home.expanduser().resolve()),
        "role": args.role,
        "coordinator": coordinator_url,
        "results": results,
        "coordinator_health": health,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


def run_node_down_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    results = [
        stop_managed_process(home=home, role=role, timeout_seconds=args.timeout_seconds)
        for role in _selected_managed_roles(args.role)
    ]
    print(
        json.dumps(
            {
                "ok": all(result["status"] in {"stopped", "not_managed"} for result in results),
                "home": str(home.expanduser().resolve()),
                "role": args.role,
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if any(result["status"] == "stop_timeout" for result in results):
        raise SystemExit(1)


def run_node_status_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    coordinator_url = _coordinator_url_from_node_args(args)
    health = None
    if not args.skip_health:
        health = _coordinator_health(coordinator_url=coordinator_url, admission_token=args.admission_token)
    processes = managed_processes_status(home=home)
    print(
        json.dumps(
            {
                "ok": all(process["alive"] for process in processes if process["managed"]),
                "home": str(home.expanduser().resolve()),
                "coordinator": coordinator_url,
                "processes": processes,
                "coordinator_health": health,
            },
            indent=2,
            sort_keys=True,
        )
    )


def run_node_watchdog_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    invite = Path(args.invite) if args.invite else home.parent / "alpha-invite.json"
    report_path = Path(args.report) if args.report else None
    operator_config_path = Path(args.operator_config) if args.operator_config else None
    try:
        report = run_node_watchdog(
            NodeWatchdogConfig(
                home=home,
                invite_path=invite,
                report_path=report_path,
                role=args.role,
                restart=not args.no_restart,
                checks=args.checks,
                interval_seconds=args.interval_seconds,
                operator_config_path=operator_config_path,
                coordinator_host=args.coordinator_host,
                coordinator_port=args.coordinator_port,
                lease_timeout_seconds=args.lease_timeout_seconds,
                node_stale_seconds=args.node_stale_seconds,
                worker_interval=args.worker_interval,
                startup_timeout_seconds=args.startup_timeout_seconds,
                cpu_duration_seconds=args.cpu_duration_seconds,
                ollama_base_url=args.ollama_base_url,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def run_node_install_task_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    invite = Path(args.invite) if args.invite else home.parent / "alpha-invite.json"
    report_path = Path(args.report) if args.report else None
    operator_config_path = Path(args.operator_config) if args.operator_config else None
    try:
        report = install_watchdog_task(
            WatchdogTaskConfig(
                home=home,
                invite_path=invite,
                task_name=args.task_name,
                report_path=report_path,
                role=args.role,
                operator_config_path=operator_config_path,
                schedule=args.schedule,
                force=not args.no_force,
                startup_fallback=args.allow_startup_folder_fallback,
                restart=not args.no_restart,
                checks=args.checks,
                interval_seconds=args.interval_seconds,
                coordinator_host=args.coordinator_host,
                coordinator_port=args.coordinator_port,
                lease_timeout_seconds=args.lease_timeout_seconds,
                node_stale_seconds=args.node_stale_seconds,
                worker_interval=args.worker_interval,
                startup_timeout_seconds=args.startup_timeout_seconds,
                cpu_duration_seconds=args.cpu_duration_seconds,
                ollama_base_url=args.ollama_base_url,
                work_dir=Path(args.work_dir) if args.work_dir else None,
                launcher_path=Path(args.launcher) if args.launcher else None,
            ),
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def run_node_uninstall_task_command(args: argparse.Namespace) -> None:
    try:
        report = uninstall_watchdog_task(
            task_name=args.task_name,
            home=Path(args.home) if args.home else None,
            launcher_path=Path(args.launcher) if args.launcher else None,
            delete_launcher=not args.keep_launcher,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def _build_managed_coordinator_argv(args: argparse.Namespace) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "chatp2p.cli",
        "coordinator",
        "serve",
        "--home",
        str(Path(args.home)),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--lease-timeout-seconds",
        str(args.lease_timeout_seconds),
        "--node-stale-seconds",
        str(args.node_stale_seconds),
    ]
    _append_optional_arg(argv, "--operator-config", args.operator_config)
    if args.public_alpha:
        argv.append("--public-alpha")
    _append_optional_arg(argv, "--admission-token", args.admission_token)
    _append_optional_arg(argv, "--max-request-bytes", args.max_request_bytes)
    _append_optional_arg(argv, "--max-job-payload-bytes", args.max_job_payload_bytes)
    _append_repeated_arg(argv, "--allowed-job-type", args.allowed_job_type)
    if args.seed_math_job:
        argv.append("--seed-math-job")
    if args.seed_eval_suite:
        argv.append("--seed-eval-suite")
    return argv


def _build_managed_worker_argv(args: argparse.Namespace, coordinator_url: str) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "chatp2p.cli",
        "worker",
        "loop",
        "--home",
        str(Path(args.home)),
        "--coordinator",
        coordinator_url,
        "--ollama-base-url",
        args.ollama_base_url,
        "--interval",
        str(args.worker_interval),
    ]
    _append_optional_arg(argv, "--admission-token", _admission_token_for_worker(args))
    return argv


def _wait_for_coordinator_health(
    *,
    coordinator_url: str,
    admission_token: str | None,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.time() + max(timeout_seconds, 0)
    last_error = "not attempted"
    while time.time() <= deadline:
        health = _coordinator_health(coordinator_url=coordinator_url, admission_token=admission_token)
        if health["ok"]:
            return health
        last_error = health["error"]
        time.sleep(poll_interval)
    return {"ok": False, "url": coordinator_url, "error": last_error}


def _coordinator_health(*, coordinator_url: str, admission_token: str | None) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "url": coordinator_url,
            "payload": CoordinatorClient(coordinator_url, admission_token=admission_token).health(),
        }
    except Exception as exc:
        return {"ok": False, "url": coordinator_url, "error": f"{type(exc).__name__}: {exc}"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chatp2p", description="ChatP2P prototype")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init-identity", help="Create a node identity keypair")
    init_parser.add_argument("--home", default=".mesh", help="Directory to store identity files")
    init_parser.add_argument("--name", default="node", help="Identity prefix, such as worker or coordinator")
    init_parser.add_argument("--force", action="store_true", help="Replace an existing identity")
    init_parser.set_defaults(func=init_identity)

    demo_parser = subcommands.add_parser("demo", help="Run a local signed job demo")
    demo_parser.set_defaults(func=run_demo)

    node_parser = subcommands.add_parser("node", help="Local node commands")
    node_subcommands = node_parser.add_subparsers(dest="node_command", required=True)

    join_parser = node_subcommands.add_parser("join", help="Join a public-alpha coordinator from an invite file")
    join_parser.add_argument("--invite", required=True, help="Path to a chatp2p.alpha-invite.v1 JSON invite")
    join_parser.add_argument("--home", default=".mesh", help="Directory for node identity, capabilities, run state, and logs")
    join_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for inference.ollama.v1 jobs",
    )
    join_parser.add_argument("--worker-interval", default=5.0, type=float, help="Seconds between worker polling attempts")
    join_parser.add_argument(
        "--startup-timeout-seconds",
        default=15.0,
        type=float,
        help="Seconds to wait for the worker to register and become live",
    )
    join_parser.add_argument(
        "--cpu-duration-seconds",
        default=0.25,
        type=float,
        help="Seconds to spend benchmarking if this node has no saved benchmark profile",
    )
    join_parser.add_argument("--force", action="store_true", help="Replace an existing managed worker process")
    join_parser.set_defaults(func=run_node_join_command)

    up_parser = node_subcommands.add_parser("up", help="Start managed background coordinator and worker processes")
    up_parser.add_argument("--home", default=".mesh", help="Directory for node identity, database, run state, and logs")
    up_parser.add_argument(
        "--role",
        default="both",
        choices=["both", "coordinator", "worker"],
        help="Managed process role to start",
    )
    up_parser.add_argument("--host", default="127.0.0.1", help="Coordinator host to bind")
    up_parser.add_argument("--port", default=8765, type=int, help="Coordinator port to bind")
    up_parser.add_argument(
        "--coordinator",
        default=None,
        help="Coordinator URL for the worker. Defaults to the local host/port",
    )
    up_parser.add_argument("--operator-config", default=None, help="Operator config JSON path")
    up_parser.add_argument(
        "--public-alpha",
        action="store_true",
        help="Require admission token for node registration and job creation",
    )
    up_parser.add_argument("--admission-token", default=None, help="Shared admission token for public alpha")
    up_parser.add_argument(
        "--max-request-bytes",
        default=None,
        type=int,
        help="Override maximum JSON request body size",
    )
    up_parser.add_argument(
        "--max-job-payload-bytes",
        default=None,
        type=int,
        help="Override maximum public job payload JSON size",
    )
    up_parser.add_argument(
        "--allowed-job-type",
        action="append",
        default=None,
        help="Override allowed public job type. Can be passed more than once",
    )
    up_parser.add_argument(
        "--lease-timeout-seconds",
        default=30.0,
        type=float,
        help="Seconds before an unfinished lease is released for another worker",
    )
    up_parser.add_argument(
        "--node-stale-seconds",
        default=60.0,
        type=float,
        help="Seconds after last activity before a node is marked stale",
    )
    up_parser.add_argument("--seed-math-job", action="store_true", help="Create one math eval job on coordinator startup")
    up_parser.add_argument(
        "--seed-eval-suite",
        action="store_true",
        help="Create deterministic eval jobs on coordinator startup",
    )
    up_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for inference.ollama.v1 jobs",
    )
    up_parser.add_argument("--worker-interval", default=5.0, type=float, help="Seconds between worker polling attempts")
    up_parser.add_argument(
        "--startup-timeout-seconds",
        default=10.0,
        type=float,
        help="Seconds to wait for coordinator health before starting a worker",
    )
    up_parser.add_argument("--force", action="store_true", help="Stop and replace existing managed processes")
    up_parser.set_defaults(func=run_node_up_command)

    down_parser = node_subcommands.add_parser("down", help="Stop managed background node processes")
    down_parser.add_argument("--home", default=".mesh", help="Directory for node run state")
    down_parser.add_argument(
        "--role",
        default="both",
        choices=["both", "coordinator", "worker"],
        help="Managed process role to stop",
    )
    down_parser.add_argument("--timeout-seconds", default=5.0, type=float, help="Seconds to wait for process exit")
    down_parser.set_defaults(func=run_node_down_command)

    status_parser = node_subcommands.add_parser("status", help="Show managed background node status")
    status_parser.add_argument("--home", default=".mesh", help="Directory for node run state")
    status_parser.add_argument("--host", default="127.0.0.1", help="Coordinator host used when deriving a URL")
    status_parser.add_argument("--port", default=8765, type=int, help="Coordinator port used when deriving a URL")
    status_parser.add_argument(
        "--coordinator",
        default=None,
        help="Coordinator base URL to check. Defaults to the local host/port",
    )
    status_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    status_parser.add_argument("--skip-health", action="store_true", help="Skip coordinator health check")
    status_parser.set_defaults(func=run_node_status_command)

    watchdog_parser = node_subcommands.add_parser(
        "watchdog",
        help="Check managed node processes and optionally restart unhealthy alpha roles",
    )
    watchdog_parser.add_argument("--home", default=".mesh", help="Directory for node run state")
    watchdog_parser.add_argument(
        "--invite",
        default=None,
        help="Path to alpha invite JSON. Defaults to HOME parent/alpha-invite.json",
    )
    watchdog_parser.add_argument(
        "--report",
        default=None,
        help="Optional path for watchdog JSON report",
    )
    watchdog_parser.add_argument(
        "--role",
        default="worker",
        choices=["both", "coordinator", "worker"],
        help="Managed role to check",
    )
    watchdog_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Only report unhealthy processes; do not restart them",
    )
    watchdog_parser.add_argument(
        "--checks",
        default=1,
        type=int,
        help="Number of checks to run. Use 0 to run until interrupted",
    )
    watchdog_parser.add_argument(
        "--interval-seconds",
        default=30.0,
        type=float,
        help="Seconds between checks when --checks is greater than 1 or 0",
    )
    watchdog_parser.add_argument(
        "--operator-config",
        default=None,
        help="Operator config JSON path required when restarting the coordinator",
    )
    watchdog_parser.add_argument(
        "--coordinator-host",
        default="0.0.0.0",
        help="Host to bind if the watchdog restarts the coordinator",
    )
    watchdog_parser.add_argument(
        "--coordinator-port",
        default=None,
        type=int,
        help="Port to bind if the watchdog restarts the coordinator. Defaults to invite URL port",
    )
    watchdog_parser.add_argument(
        "--lease-timeout-seconds",
        default=30.0,
        type=float,
        help="Coordinator lease timeout when the watchdog restarts the coordinator",
    )
    watchdog_parser.add_argument(
        "--node-stale-seconds",
        default=60.0,
        type=float,
        help="Coordinator node stale timeout when the watchdog restarts the coordinator",
    )
    watchdog_parser.add_argument(
        "--worker-interval",
        default=0.5,
        type=float,
        help="Seconds between worker polling attempts after a watchdog restart",
    )
    watchdog_parser.add_argument(
        "--startup-timeout-seconds",
        default=15.0,
        type=float,
        help="Seconds to wait for restarted roles to become healthy",
    )
    watchdog_parser.add_argument(
        "--cpu-duration-seconds",
        default=0.25,
        type=float,
        help="Seconds to spend benchmarking a worker that has no saved profile",
    )
    watchdog_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for inference.ollama.v1 capability discovery",
    )
    watchdog_parser.set_defaults(func=run_node_watchdog_command)

    install_task_parser = node_subcommands.add_parser(
        "install-task",
        help="Install a Windows Scheduled Task that runs the ChatP2P watchdog",
    )
    install_task_parser.add_argument("--home", default=".mesh", help="Directory for node run state")
    install_task_parser.add_argument(
        "--invite",
        default=None,
        help="Path to alpha invite JSON. Defaults to HOME parent/alpha-invite.json",
    )
    install_task_parser.add_argument(
        "--task-name",
        default=DEFAULT_TASK_NAME,
        help="Windows Scheduled Task name",
    )
    install_task_parser.add_argument(
        "--report",
        default=None,
        help="Watchdog report path written by the scheduled task. Defaults to HOME/run/watchdog-task-report.json",
    )
    install_task_parser.add_argument(
        "--role",
        default="worker",
        choices=["both", "coordinator", "worker"],
        help="Managed role the watchdog task should check",
    )
    install_task_parser.add_argument(
        "--operator-config",
        default=None,
        help="Operator config JSON path required when the task may restart the coordinator",
    )
    install_task_parser.add_argument(
        "--schedule",
        default="onlogon",
        choices=["onlogon", "onstart"],
        help="Windows task trigger",
    )
    install_task_parser.add_argument(
        "--checks",
        default=0,
        type=int,
        help="Watchdog checks per task run. Use 0 to keep it running until stopped",
    )
    install_task_parser.add_argument(
        "--interval-seconds",
        default=30.0,
        type=float,
        help="Seconds between watchdog checks",
    )
    install_task_parser.add_argument(
        "--coordinator-host",
        default="0.0.0.0",
        help="Host to bind if the task restarts the coordinator",
    )
    install_task_parser.add_argument(
        "--coordinator-port",
        default=None,
        type=int,
        help="Port to bind if the task restarts the coordinator. Defaults to invite URL port",
    )
    install_task_parser.add_argument(
        "--lease-timeout-seconds",
        default=30.0,
        type=float,
        help="Coordinator lease timeout when the task restarts the coordinator",
    )
    install_task_parser.add_argument(
        "--node-stale-seconds",
        default=60.0,
        type=float,
        help="Coordinator node stale timeout when the task restarts the coordinator",
    )
    install_task_parser.add_argument(
        "--worker-interval",
        default=0.5,
        type=float,
        help="Seconds between worker polling attempts after a watchdog restart",
    )
    install_task_parser.add_argument(
        "--startup-timeout-seconds",
        default=15.0,
        type=float,
        help="Seconds to wait for restarted roles to become healthy",
    )
    install_task_parser.add_argument(
        "--cpu-duration-seconds",
        default=0.25,
        type=float,
        help="Seconds to spend benchmarking a worker that has no saved profile",
    )
    install_task_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for inference.ollama.v1 capability discovery",
    )
    install_task_parser.add_argument(
        "--work-dir",
        default=None,
        help="Working directory for the generated launcher. Defaults to the ChatP2P source root parent",
    )
    install_task_parser.add_argument(
        "--launcher",
        default=None,
        help="Path for generated .cmd launcher. Defaults to HOME/run/<task-name>.cmd",
    )
    install_task_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Install a reporting-only watchdog task that does not restart unhealthy roles",
    )
    install_task_parser.add_argument(
        "--no-force",
        action="store_true",
        help="Do not replace an existing Scheduled Task of the same name",
    )
    install_task_parser.add_argument(
        "--allow-startup-folder-fallback",
        action="store_true",
        help="If Scheduled Task creation is denied, install a per-user Startup folder launcher under APPDATA",
    )
    install_task_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the task plan without writing the launcher or creating a Scheduled Task",
    )
    install_task_parser.set_defaults(func=run_node_install_task_command)

    uninstall_task_parser = node_subcommands.add_parser(
        "uninstall-task",
        help="Remove a ChatP2P Windows Scheduled Task",
    )
    uninstall_task_parser.add_argument(
        "--task-name",
        default=DEFAULT_TASK_NAME,
        help="Windows Scheduled Task name",
    )
    uninstall_task_parser.add_argument(
        "--home",
        default=None,
        help="Optional home directory used to locate the generated launcher for deletion",
    )
    uninstall_task_parser.add_argument(
        "--launcher",
        default=None,
        help="Optional generated launcher path to delete",
    )
    uninstall_task_parser.add_argument(
        "--keep-launcher",
        action="store_true",
        help="Leave the generated launcher file in place",
    )
    uninstall_task_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the uninstall plan without deleting the task or launcher",
    )
    uninstall_task_parser.set_defaults(func=run_node_uninstall_task_command)

    benchmark_parser = node_subcommands.add_parser(
        "benchmark",
        help="Benchmark this machine and save worker capabilities",
    )
    benchmark_parser.add_argument("--home", default=".mesh", help="Directory for node identity and capabilities")
    benchmark_parser.add_argument(
        "--output",
        default=None,
        help=f"Output path. Defaults to HOME/{CAPABILITY_PROFILE_NAME}",
    )
    benchmark_parser.add_argument(
        "--cpu-duration-seconds",
        default=0.25,
        type=float,
        help="Seconds to spend on the tiny CPU benchmark",
    )
    benchmark_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for model discovery",
    )
    benchmark_parser.set_defaults(func=run_node_benchmark_command)

    doctor_parser = node_subcommands.add_parser(
        "doctor",
        help="Check whether this machine is ready to run as a ChatP2P node",
    )
    doctor_parser.add_argument("--home", default=".mesh", help="Directory for node identity and capabilities")
    doctor_parser.add_argument("--model", default=None, help="Optional Ollama model that must be locally available")
    doctor_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for model discovery",
    )
    doctor_parser.add_argument(
        "--coordinator",
        default="http://127.0.0.1:8765",
        help="Coordinator base URL to check",
    )
    doctor_parser.add_argument(
        "--skip-coordinator",
        action="store_true",
        help="Skip coordinator reachability check",
    )
    doctor_parser.add_argument(
        "--timeout-seconds",
        default=2.0,
        type=float,
        help="Timeout for local HTTP checks",
    )
    doctor_parser.set_defaults(func=run_node_doctor_command)

    operator_parser = subcommands.add_parser("operator", help="Operator config commands")
    operator_subcommands = operator_parser.add_subparsers(dest="operator_command", required=True)
    operator_config_parser = operator_subcommands.add_parser(
        "write-config",
        help="Write a public-alpha operator config file",
    )
    operator_config_parser.add_argument("--output", required=True, help="Path for operator config JSON")
    operator_config_parser.add_argument("--admission-token", required=True, help="Shared admission token")
    operator_config_parser.add_argument(
        "--max-request-bytes",
        default=256 * 1024,
        type=int,
        help="Maximum JSON request body size accepted by the coordinator",
    )
    operator_config_parser.add_argument(
        "--max-job-payload-bytes",
        default=16 * 1024,
        type=int,
        help="Maximum job payload JSON size accepted by public job creation",
    )
    operator_config_parser.add_argument(
        "--allowed-job-type",
        action="append",
        default=None,
        help="Allowed public job type. Can be passed more than once",
    )
    operator_config_parser.add_argument("--force", action="store_true", help="Replace an existing config")
    operator_config_parser.set_defaults(func=write_operator_config_command)

    bootstrap_alpha_parser = operator_subcommands.add_parser(
        "bootstrap-alpha",
        help="Write public-alpha operator config and invite files",
    )
    bootstrap_alpha_parser.add_argument("--config", required=True, help="Path for operator config JSON")
    bootstrap_alpha_parser.add_argument("--invite", required=True, help="Path for alpha invite JSON")
    bootstrap_alpha_parser.add_argument(
        "--coordinator-url",
        required=True,
        help="Public URL contributors should use to reach this coordinator",
    )
    bootstrap_alpha_parser.add_argument(
        "--admission-token",
        default=None,
        help="Shared admission token. Generated when omitted",
    )
    bootstrap_alpha_parser.add_argument(
        "--max-request-bytes",
        default=256 * 1024,
        type=int,
        help="Maximum JSON request body size accepted by the coordinator",
    )
    bootstrap_alpha_parser.add_argument(
        "--max-job-payload-bytes",
        default=16 * 1024,
        type=int,
        help="Maximum job payload JSON size accepted by public job creation",
    )
    bootstrap_alpha_parser.add_argument(
        "--allowed-job-type",
        action="append",
        default=None,
        help="Allowed public job type. Can be passed more than once",
    )
    bootstrap_alpha_parser.add_argument(
        "--notes",
        default=DEFAULT_ALPHA_NOTES,
        help="Notes stored in the invite for contributors",
    )
    bootstrap_alpha_parser.add_argument("--force", action="store_true", help="Replace existing config/invite files")
    bootstrap_alpha_parser.set_defaults(func=bootstrap_alpha_command)

    alpha_preflight_parser = operator_subcommands.add_parser(
        "alpha-preflight",
        help="Validate public-alpha config, invite, coordinator, and managed state",
    )
    alpha_preflight_parser.add_argument("--config", required=True, help="Path to operator config JSON")
    alpha_preflight_parser.add_argument("--invite", required=True, help="Path to alpha invite JSON")
    alpha_preflight_parser.add_argument("--home", required=True, help="Coordinator home directory")
    alpha_preflight_parser.add_argument("--report", required=True, help="Path for preflight JSON report")
    alpha_preflight_parser.add_argument(
        "--timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for coordinator health checks",
    )
    alpha_preflight_parser.set_defaults(func=alpha_preflight_command)

    alpha_status_parser = operator_subcommands.add_parser(
        "alpha-status",
        help="Show a redacted operator status report for a running alpha",
    )
    alpha_status_parser.add_argument("--home", required=True, help="Coordinator and primary worker home directory")
    alpha_status_parser.add_argument(
        "--invite",
        default=None,
        help="Path to alpha invite JSON. Defaults to HOME parent/alpha-invite.json",
    )
    alpha_status_parser.add_argument(
        "--report",
        default=None,
        help="Optional path for status JSON report",
    )
    alpha_status_parser.add_argument(
        "--expected-worker-id",
        default=None,
        help="Worker node ID that should be present and live",
    )
    alpha_status_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required for pass",
    )
    alpha_status_parser.add_argument(
        "--timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for coordinator health and snapshot checks",
    )
    alpha_status_parser.set_defaults(func=alpha_status_command)

    alpha_route_parser = operator_subcommands.add_parser(
        "alpha-route",
        help="Report whether an alpha invite URL is ready for remote contributors",
    )
    alpha_route_parser.add_argument(
        "--invite",
        default=None,
        help="Path to alpha invite JSON. Defaults to HOME parent/alpha-invite.json or ./alpha-invite.json",
    )
    alpha_route_parser.add_argument(
        "--home",
        default=None,
        help="Optional coordinator home directory for managed process status",
    )
    alpha_route_parser.add_argument(
        "--candidate-url",
        default=None,
        help="Optional future coordinator URL to classify and health-check without rewriting the invite",
    )
    alpha_route_parser.add_argument(
        "--report",
        default=None,
        help="Path for route JSON report. Defaults to HOME parent/alpha-route-report.json or ./alpha-route-report.json",
    )
    alpha_route_parser.add_argument(
        "--timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for coordinator health checks",
    )
    alpha_route_parser.add_argument(
        "--no-tool-detection",
        action="store_true",
        help="Skip non-mutating local checks for route tools such as tailscale or cloudflared",
    )
    alpha_route_parser.set_defaults(func=alpha_route_command)

    alpha_smoke_parser = operator_subcommands.add_parser(
        "alpha-smoke",
        help="Create deterministic jobs and prove live workers can return accepted results",
    )
    alpha_smoke_parser.add_argument("--invite", required=True, help="Path to alpha invite JSON")
    alpha_smoke_parser.add_argument("--jobs", default=4, type=int, help="Deterministic eval jobs to create")
    alpha_smoke_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required for pass",
    )
    alpha_smoke_parser.add_argument(
        "--min-accepted-results",
        default=1,
        type=int,
        help="Minimum accepted results on smoke-created jobs required for pass",
    )
    alpha_smoke_parser.add_argument(
        "--min-verified-jobs",
        default=0,
        type=int,
        help="Minimum verified smoke-created jobs required for pass",
    )
    alpha_smoke_parser.add_argument(
        "--timeout-seconds",
        default=90.0,
        type=float,
        help="Maximum time to wait for smoke thresholds",
    )
    alpha_smoke_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls",
    )
    alpha_smoke_parser.add_argument("--report", required=True, help="Path for smoke JSON report")
    alpha_smoke_parser.set_defaults(func=alpha_smoke_command)

    alpha_remote_proof_parser = operator_subcommands.add_parser(
        "alpha-remote-proof",
        help="Prove a named external worker can complete verified signed work",
    )
    alpha_remote_proof_parser.add_argument("--invite", required=True, help="Path to alpha invite JSON")
    alpha_remote_proof_parser.add_argument("--jobs", default=4, type=int, help="Deterministic eval jobs to create")
    alpha_remote_proof_parser.add_argument(
        "--expected-worker-id",
        default=None,
        help="Worker node ID that must be live and must return at least one accepted result",
    )
    alpha_remote_proof_parser.add_argument(
        "--min-live-workers",
        default=2,
        type=int,
        help="Minimum live workers required for pass",
    )
    alpha_remote_proof_parser.add_argument(
        "--min-accepted-results",
        default=None,
        type=int,
        help="Minimum accepted results on proof-created jobs. Defaults to jobs * 2",
    )
    alpha_remote_proof_parser.add_argument(
        "--min-verified-jobs",
        default=None,
        type=int,
        help="Minimum verified proof-created jobs. Defaults to jobs",
    )
    alpha_remote_proof_parser.add_argument(
        "--timeout-seconds",
        default=180.0,
        type=float,
        help="Maximum time to wait for all proof-created jobs to finish",
    )
    alpha_remote_proof_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls",
    )
    alpha_remote_proof_parser.add_argument("--report", required=True, help="Path for remote proof JSON report")
    alpha_remote_proof_parser.set_defaults(func=alpha_remote_proof_command)

    alpha_drill_parser = operator_subcommands.add_parser(
        "alpha-drill",
        help="Run an operator rehearsal with optional simulated workers and a quorum smoke proof",
    )
    alpha_drill_parser.add_argument("--home", required=True, help="Coordinator and primary worker home directory")
    alpha_drill_parser.add_argument(
        "--invite",
        default=None,
        help="Path to alpha invite JSON. Defaults to HOME parent/alpha-invite.json",
    )
    alpha_drill_parser.add_argument(
        "--config",
        default=None,
        help="Path to operator config JSON. Defaults to HOME parent/operator-config.json when present",
    )
    alpha_drill_parser.add_argument(
        "--report",
        default=None,
        help="Path for drill JSON report. Defaults to HOME parent/alpha-drill-report.json",
    )
    alpha_drill_parser.add_argument(
        "--simulated-workers",
        default=1,
        type=int,
        help="Extra isolated local workers to start for the drill",
    )
    alpha_drill_parser.add_argument("--jobs", default=4, type=int, help="Deterministic eval jobs to create")
    alpha_drill_parser.add_argument(
        "--worker-interval",
        default=0.5,
        type=float,
        help="Seconds between worker polling attempts",
    )
    alpha_drill_parser.add_argument(
        "--startup-timeout-seconds",
        default=15.0,
        type=float,
        help="Seconds to wait for coordinator and workers to become live",
    )
    alpha_drill_parser.add_argument(
        "--timeout-seconds",
        default=90.0,
        type=float,
        help="Maximum time to wait for smoke thresholds",
    )
    alpha_drill_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls during smoke proof",
    )
    alpha_drill_parser.add_argument(
        "--cpu-duration-seconds",
        default=0.25,
        type=float,
        help="Seconds to spend benchmarking workers that have no saved profile",
    )
    alpha_drill_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for inference.ollama.v1 capability discovery",
    )
    alpha_drill_parser.add_argument(
        "--no-start-coordinator",
        action="store_true",
        help="Only check the coordinator; do not start it when unreachable",
    )
    alpha_drill_parser.add_argument(
        "--coordinator-host",
        default="0.0.0.0",
        help="Host to bind if the drill needs to start the coordinator",
    )
    alpha_drill_parser.add_argument(
        "--coordinator-port",
        default=None,
        type=int,
        help="Port to bind if the drill needs to start the coordinator. Defaults to invite URL port",
    )
    alpha_drill_parser.add_argument(
        "--lease-timeout-seconds",
        default=30.0,
        type=float,
        help="Coordinator lease timeout when the drill starts the coordinator",
    )
    alpha_drill_parser.add_argument(
        "--node-stale-seconds",
        default=60.0,
        type=float,
        help="Coordinator node stale timeout when the drill starts the coordinator",
    )
    alpha_drill_parser.add_argument(
        "--no-primary-worker",
        action="store_true",
        help="Do not start or check a primary worker under --home",
    )
    alpha_drill_parser.add_argument(
        "--force-workers",
        action="store_true",
        help="Replace existing managed workers used by the drill",
    )
    alpha_drill_parser.add_argument(
        "--cleanup-simulated-workers",
        action="store_true",
        help="Stop simulated workers after writing the report",
    )
    alpha_drill_parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip the config/invite/coordinator preflight sidecar report",
    )
    alpha_drill_parser.set_defaults(func=alpha_drill_command)

    coordinator_parser = subcommands.add_parser("coordinator", help="Coordinator commands")
    coordinator_subcommands = coordinator_parser.add_subparsers(dest="coordinator_command", required=True)
    serve_parser = coordinator_subcommands.add_parser("serve", help="Run a local HTTP coordinator")
    serve_parser.add_argument("--home", default=".mesh", help="Directory for coordinator identity")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    serve_parser.add_argument("--port", default=8765, type=int, help="Port to bind")
    serve_parser.add_argument("--db", default=None, help="SQLite database path")
    serve_parser.add_argument("--operator-config", default=None, help="Operator config JSON path")
    serve_parser.add_argument(
        "--public-alpha",
        action="store_true",
        help="Require admission token for node registration and job creation",
    )
    serve_parser.add_argument("--admission-token", default=None, help="Shared admission token for public alpha")
    serve_parser.add_argument(
        "--max-request-bytes",
        default=None,
        type=int,
        help="Override maximum JSON request body size",
    )
    serve_parser.add_argument(
        "--max-job-payload-bytes",
        default=None,
        type=int,
        help="Override maximum public job payload JSON size",
    )
    serve_parser.add_argument(
        "--allowed-job-type",
        action="append",
        default=None,
        help="Override allowed public job type. Can be passed more than once",
    )
    serve_parser.add_argument(
        "--lease-timeout-seconds",
        default=30.0,
        type=float,
        help="Seconds before an unfinished lease is released for another worker",
    )
    serve_parser.add_argument(
        "--node-stale-seconds",
        default=60.0,
        type=float,
        help="Seconds after last activity before a node is marked stale",
    )
    serve_parser.add_argument("--seed-math-job", action="store_true", help="Create one math eval job on startup")
    serve_parser.add_argument("--seed-eval-suite", action="store_true", help="Create deterministic eval jobs on startup")
    serve_parser.set_defaults(func=serve_coordinator)

    worker_parser = subcommands.add_parser("worker", help="Worker commands")
    worker_subcommands = worker_parser.add_subparsers(dest="worker_command", required=True)
    once_parser = worker_subcommands.add_parser("run-once", help="Register, lease one job, run it, submit result")
    once_parser.add_argument("--home", default=".mesh", help="Directory for worker identity")
    once_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    once_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    once_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for inference.ollama.v1 jobs",
    )
    once_parser.set_defaults(func=run_worker_once)

    loop_parser = worker_subcommands.add_parser("loop", help="Continuously poll for jobs")
    loop_parser.add_argument("--home", default=".mesh", help="Directory for worker identity")
    loop_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    loop_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    loop_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for inference.ollama.v1 jobs",
    )
    loop_parser.add_argument("--interval", default=5.0, type=float, help="Seconds between polling attempts")
    loop_parser.add_argument("--max-jobs", default=None, type=int, help="Stop after completing this many jobs")
    loop_parser.add_argument("--stop-when-idle", action="store_true", help="Stop once the coordinator has no job")
    loop_parser.set_defaults(func=run_worker_loop)

    job_parser = subcommands.add_parser("job", help="Create and inspect coordinator jobs")
    job_subcommands = job_parser.add_subparsers(dest="job_command", required=True)

    generic_parser = job_subcommands.add_parser("create", help="Create a job from a JSON payload")
    generic_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    generic_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    generic_parser.add_argument("--job-type", required=True, help="Job type, such as eval.deterministic.v1")
    generic_parser.add_argument("--payload-json", required=True, help="JSON object payload")
    generic_parser.add_argument("--model-id", default=None, help="Optional model id override")
    generic_parser.add_argument("--reward", default=1, type=int, help="Credits awarded for accepted result")
    generic_parser.add_argument("--ttl-seconds", default=300, type=int, help="Job lifetime in seconds")
    generic_parser.set_defaults(func=create_generic_job)

    echo_parser = job_subcommands.add_parser("create-echo", help="Create an echo inference smoke-test job")
    echo_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    echo_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    echo_parser.add_argument("--prompt", required=True, help="Prompt to echo")
    echo_parser.add_argument("--reward", default=1, type=int, help="Credits awarded for accepted result")
    echo_parser.add_argument("--ttl-seconds", default=300, type=int, help="Job lifetime in seconds")
    echo_parser.set_defaults(func=create_echo_job)

    ollama_parser = job_subcommands.add_parser("create-ollama", help="Create a local Ollama inference job")
    ollama_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    ollama_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    ollama_parser.add_argument("--model", required=True, help="Ollama model name, such as llama3.2:3b")
    ollama_parser.add_argument("--prompt", required=True, help="Prompt to send to Ollama")
    ollama_parser.add_argument("--temperature", default=None, type=float, help="Optional Ollama temperature")
    ollama_parser.add_argument("--reward", default=1, type=int, help="Credits awarded for accepted result")
    ollama_parser.add_argument("--ttl-seconds", default=300, type=int, help="Job lifetime in seconds")
    ollama_parser.set_defaults(func=create_ollama_job)

    deterministic_parser = job_subcommands.add_parser(
        "create-deterministic",
        help="Create a deterministic eval job",
    )
    deterministic_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    deterministic_parser.add_argument(
        "--admission-token",
        default=None,
        help="Admission token for public alpha coordinators",
    )
    deterministic_parser.add_argument("--task", required=True, choices=["arithmetic", "number_theory", "text"])
    deterministic_parser.add_argument("--operation", choices=["add", "subtract", "multiply", "divide"])
    deterministic_parser.add_argument("--operands", nargs=2, metavar=("LEFT", "RIGHT"), help="Arithmetic operands")
    deterministic_parser.add_argument("--value", help="Number theory integer or text value")
    deterministic_parser.add_argument("--expected", help="Expected answer as JSON; inferred when omitted")
    deterministic_parser.add_argument("--reward", default=1, type=int, help="Credits awarded for accepted result")
    deterministic_parser.add_argument("--ttl-seconds", default=300, type=int, help="Job lifetime in seconds")
    deterministic_parser.set_defaults(func=create_deterministic_job)

    suite_parser = job_subcommands.add_parser("create-suite", help="Create the deterministic demo eval suite")
    suite_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    suite_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    suite_parser.set_defaults(func=create_demo_suite)

    list_parser = job_subcommands.add_parser("list", help="List jobs")
    list_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    list_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    list_parser.set_defaults(func=list_jobs)

    snapshot_parser = job_subcommands.add_parser("snapshot", help="Show coordinator snapshot")
    snapshot_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    snapshot_parser.add_argument("--admission-token", default=None, help="Admission token for public alpha coordinators")
    snapshot_parser.set_defaults(func=show_snapshot)

    reputation_parser = job_subcommands.add_parser("reputation", help="Show node reputation")
    reputation_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    reputation_parser.add_argument(
        "--admission-token",
        default=None,
        help="Admission token for public alpha coordinators",
    )
    reputation_parser.set_defaults(func=show_reputation)

    proof_parser = subcommands.add_parser("proof", help="Run local proof harnesses")
    proof_subcommands = proof_parser.add_subparsers(dest="proof_command", required=True)

    swarm_parser = proof_subcommands.add_parser("swarm", help="Run a local multi-process reliability proof")
    swarm_parser.add_argument("--workers", default=25, type=int, help="Number of worker processes to launch")
    swarm_parser.add_argument("--jobs", default=100, type=int, help="Number of deterministic eval jobs to create")
    swarm_parser.add_argument("--work-dir", default=".mesh/proof", help="Directory for proof run state")
    swarm_parser.add_argument(
        "--report",
        default=".mesh/proof/reliability-report.json",
        help="Path for the JSON proof report",
    )
    swarm_parser.add_argument(
        "--timeout-seconds",
        default=120.0,
        type=float,
        help="Maximum proof runtime before marking the run failed",
    )
    swarm_parser.add_argument(
        "--lease-timeout-seconds",
        default=10.0,
        type=float,
        help="Seconds before unfinished leases are released to other workers",
    )
    swarm_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between parent snapshot polls",
    )
    swarm_parser.add_argument(
        "--worker-interval",
        default=0.1,
        type=float,
        help="Seconds idle workers wait between job polls",
    )
    swarm_parser.add_argument(
        "--fault-timeout-workers",
        default=0,
        type=int,
        help="Workers that acknowledge one lease and disappear to prove lease recovery",
    )
    swarm_parser.set_defaults(func=run_proof_swarm)

    ollama_proof_parser = proof_subcommands.add_parser(
        "ollama",
        help="Run a local multi-process Ollama inference proof",
    )
    ollama_proof_parser.add_argument("--workers", default=4, type=int, help="Number of worker processes to launch")
    ollama_proof_parser.add_argument("--jobs", default=8, type=int, help="Number of Ollama inference jobs to create")
    ollama_proof_parser.add_argument("--model", required=True, help="Required local Ollama model name")
    ollama_proof_parser.add_argument(
        "--prompt",
        default="Explain peer-to-peer AI in one concise sentence.",
        help="Base prompt to send to Ollama for each proof job",
    )
    ollama_proof_parser.add_argument("--temperature", default=None, type=float, help="Optional Ollama temperature")
    ollama_proof_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL",
    )
    ollama_proof_parser.add_argument("--work-dir", default=".mesh/proof", help="Directory for proof run state")
    ollama_proof_parser.add_argument(
        "--report",
        default=".mesh/proof/ollama-report.json",
        help="Path for the JSON proof report",
    )
    ollama_proof_parser.add_argument(
        "--timeout-seconds",
        default=180.0,
        type=float,
        help="Maximum proof runtime before marking the run failed",
    )
    ollama_proof_parser.add_argument(
        "--lease-timeout-seconds",
        default=60.0,
        type=float,
        help="Seconds before unfinished leases are released to other workers",
    )
    ollama_proof_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between parent snapshot polls",
    )
    ollama_proof_parser.add_argument(
        "--worker-interval",
        default=0.25,
        type=float,
        help="Seconds idle workers wait between job polls",
    )
    ollama_proof_parser.add_argument(
        "--mismatched-workers",
        default=0,
        type=int,
        help="Workers that advertise a different Ollama model to prove model-aware routing",
    )
    ollama_proof_parser.set_defaults(func=run_proof_ollama)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

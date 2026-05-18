"""Command line interface for the ChatP2P prototype."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .client import CoordinatorClient
from .coordinator import Coordinator
from .crypto import NodeIdentity
from .http_api import create_coordinator_http_server
from .packets import NodeRegistration
from .storage import SQLiteCoordinatorStore
from .worker import WorkerNode


def _identity_path(home: Path, name: str) -> Path:
    return home / f"{name}.identity.json"


def _load_or_create_identity(home: Path, name: str) -> NodeIdentity:
    path = _identity_path(home, name)
    if path.exists():
        return NodeIdentity.load(path)
    identity = NodeIdentity.generate(prefix=name)
    identity.save(path)
    return identity


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
    coordinator = Coordinator(identity=identity, store=SQLiteCoordinatorStore(db_path))
    if args.seed_math_job:
        coordinator.create_math_eval_job()
    if args.seed_eval_suite:
        coordinator.create_deterministic_eval_jobs()

    server = create_coordinator_http_server(coordinator, host=args.host, port=args.port)
    print(f"coordinator: {identity.node_id}")
    print(f"listening: http://{args.host}:{args.port}")
    print(f"database: {db_path}")
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
    identity = _load_or_create_identity(Path(args.home), "worker")
    worker = WorkerNode(identity=identity)
    client = CoordinatorClient(args.coordinator)

    _register_worker(client, worker)
    result = _run_one_remote_job(client, worker)
    print(json.dumps(result, indent=2, sort_keys=True))


def _register_worker(client: CoordinatorClient, worker: WorkerNode) -> None:
    registration = NodeRegistration.create(node=worker.identity, capabilities=worker.capabilities())
    register_response = client.register(registration)
    if not register_response.get("accepted"):
        raise SystemExit(f"registration rejected: {register_response}")


def _run_one_remote_job(client: CoordinatorClient, worker: WorkerNode) -> dict:
    job = client.next_job(worker.identity.node_id)
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
    identity = _load_or_create_identity(Path(args.home), "worker")
    worker = WorkerNode(identity=identity)
    client = CoordinatorClient(args.coordinator)
    _register_worker(client, worker)

    completed = 0
    while True:
        result = _run_one_remote_job(client, worker)
        timestamp = time.strftime("%H:%M:%S")
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
    client = CoordinatorClient(args.coordinator)
    job = client.create_job(
        job_type=args.job_type,
        payload=payload,
        model_id=args.model_id,
        reward=args.reward,
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps({"created": True, "job": job.to_dict()}, indent=2, sort_keys=True))


def create_echo_job(args: argparse.Namespace) -> None:
    client = CoordinatorClient(args.coordinator)
    job = client.create_job(
        job_type="inference.echo.v1",
        payload={"prompt": args.prompt},
        reward=args.reward,
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps({"created": True, "job": job.to_dict()}, indent=2, sort_keys=True))


def create_deterministic_job(args: argparse.Namespace) -> None:
    client = CoordinatorClient(args.coordinator)
    payload = _build_deterministic_payload(args)
    job = client.create_job(
        job_type="eval.deterministic.v1",
        payload=payload,
        reward=args.reward,
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps({"created": True, "job": job.to_dict()}, indent=2, sort_keys=True))


def create_demo_suite(args: argparse.Namespace) -> None:
    client = CoordinatorClient(args.coordinator)
    jobs = client.create_demo_suite()
    print(
        json.dumps(
            {"created": True, "jobs": [job.to_dict() for job in jobs]},
            indent=2,
            sort_keys=True,
        )
    )


def list_jobs(args: argparse.Namespace) -> None:
    client = CoordinatorClient(args.coordinator)
    print(json.dumps(client.jobs(), indent=2, sort_keys=True))


def show_snapshot(args: argparse.Namespace) -> None:
    client = CoordinatorClient(args.coordinator)
    print(json.dumps(client.snapshot(), indent=2, sort_keys=True))


def show_reputation(args: argparse.Namespace) -> None:
    client = CoordinatorClient(args.coordinator)
    print(json.dumps(client.reputation(), indent=2, sort_keys=True))


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

    coordinator_parser = subcommands.add_parser("coordinator", help="Coordinator commands")
    coordinator_subcommands = coordinator_parser.add_subparsers(dest="coordinator_command", required=True)
    serve_parser = coordinator_subcommands.add_parser("serve", help="Run a local HTTP coordinator")
    serve_parser.add_argument("--home", default=".mesh", help="Directory for coordinator identity")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    serve_parser.add_argument("--port", default=8765, type=int, help="Port to bind")
    serve_parser.add_argument("--db", default=None, help="SQLite database path")
    serve_parser.add_argument("--seed-math-job", action="store_true", help="Create one math eval job on startup")
    serve_parser.add_argument("--seed-eval-suite", action="store_true", help="Create deterministic eval jobs on startup")
    serve_parser.set_defaults(func=serve_coordinator)

    worker_parser = subcommands.add_parser("worker", help="Worker commands")
    worker_subcommands = worker_parser.add_subparsers(dest="worker_command", required=True)
    once_parser = worker_subcommands.add_parser("run-once", help="Register, lease one job, run it, submit result")
    once_parser.add_argument("--home", default=".mesh", help="Directory for worker identity")
    once_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    once_parser.set_defaults(func=run_worker_once)

    loop_parser = worker_subcommands.add_parser("loop", help="Continuously poll for jobs")
    loop_parser.add_argument("--home", default=".mesh", help="Directory for worker identity")
    loop_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    loop_parser.add_argument("--interval", default=5.0, type=float, help="Seconds between polling attempts")
    loop_parser.add_argument("--max-jobs", default=None, type=int, help="Stop after completing this many jobs")
    loop_parser.add_argument("--stop-when-idle", action="store_true", help="Stop once the coordinator has no job")
    loop_parser.set_defaults(func=run_worker_loop)

    job_parser = subcommands.add_parser("job", help="Create and inspect coordinator jobs")
    job_subcommands = job_parser.add_subparsers(dest="job_command", required=True)

    generic_parser = job_subcommands.add_parser("create", help="Create a job from a JSON payload")
    generic_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    generic_parser.add_argument("--job-type", required=True, help="Job type, such as eval.deterministic.v1")
    generic_parser.add_argument("--payload-json", required=True, help="JSON object payload")
    generic_parser.add_argument("--model-id", default=None, help="Optional model id override")
    generic_parser.add_argument("--reward", default=1, type=int, help="Credits awarded for accepted result")
    generic_parser.add_argument("--ttl-seconds", default=300, type=int, help="Job lifetime in seconds")
    generic_parser.set_defaults(func=create_generic_job)

    echo_parser = job_subcommands.add_parser("create-echo", help="Create an echo inference smoke-test job")
    echo_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    echo_parser.add_argument("--prompt", required=True, help="Prompt to echo")
    echo_parser.add_argument("--reward", default=1, type=int, help="Credits awarded for accepted result")
    echo_parser.add_argument("--ttl-seconds", default=300, type=int, help="Job lifetime in seconds")
    echo_parser.set_defaults(func=create_echo_job)

    deterministic_parser = job_subcommands.add_parser(
        "create-deterministic",
        help="Create a deterministic eval job",
    )
    deterministic_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
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
    suite_parser.set_defaults(func=create_demo_suite)

    list_parser = job_subcommands.add_parser("list", help="List jobs")
    list_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    list_parser.set_defaults(func=list_jobs)

    snapshot_parser = job_subcommands.add_parser("snapshot", help="Show coordinator snapshot")
    snapshot_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    snapshot_parser.set_defaults(func=show_snapshot)

    reputation_parser = job_subcommands.add_parser("reputation", help="Show node reputation")
    reputation_parser.add_argument("--coordinator", default="http://127.0.0.1:8765", help="Coordinator base URL")
    reputation_parser.set_defaults(func=show_reputation)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

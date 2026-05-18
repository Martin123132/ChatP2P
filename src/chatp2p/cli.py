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

    registration = NodeRegistration.create(node=identity, capabilities=worker.capabilities())
    register_response = client.register(registration)
    if not register_response.get("accepted"):
        raise SystemExit(f"registration rejected: {register_response}")

    job = client.next_job(identity.node_id)
    if job is None:
        print(json.dumps({"worker": identity.node_id, "job": None}, indent=2, sort_keys=True))
        return

    result = worker.run_job(job)
    submit_response = client.submit_result(result)
    print(
        json.dumps(
            {
                "worker": identity.node_id,
                "job_id": job.job_id,
                "job_type": job.job_type,
                "result_accepted": submit_response.get("accepted"),
                "credits": submit_response.get("credits"),
                "output": result.output,
            },
            indent=2,
            sort_keys=True,
        )
    )


def run_worker_loop(args: argparse.Namespace) -> None:
    while True:
        run_worker_once(args)
        time.sleep(args.interval)


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
    loop_parser.set_defaults(func=run_worker_loop)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

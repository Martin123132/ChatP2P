"""Command line interface for the ChatP2P prototype."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from .alpha import (
    AlphaDrillConfig,
    AlphaEvidenceConfig,
    AlphaFailoverSmokeConfig,
    AlphaInferenceProofConfig,
    AlphaJoinConfig,
    AlphaNetworkStatusConfig,
    AlphaOpsPackConfig,
    AlphaReliabilityPackConfig,
    NodeCapabilityRefreshConfig,
    AlphaPreflightConfig,
    AlphaRemoteProofConfig,
    AlphaRouteConfig,
    AlphaSmokeConfig,
    AlphaSoakConfig,
    AlphaStatusConfig,
    DEFAULT_ALPHA_NOTES,
    DEFAULT_INFERENCE_PROOF_PROMPT,
    DEFAULT_OPERATOR_TASK_NAME,
    NodeWatchdogConfig,
    bootstrap_alpha,
    run_alpha_drill,
    run_alpha_evidence,
    run_alpha_failover_smoke,
    run_alpha_inference_proof,
    run_alpha_join,
    run_alpha_network_status,
    run_alpha_ops_pack,
    run_alpha_preflight,
    run_alpha_reliability_pack,
    run_alpha_remote_proof,
    run_alpha_route,
    run_alpha_smoke,
    run_alpha_soak,
    run_alpha_status,
    load_alpha_invite,
    refresh_node_capabilities,
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
from .jsonio import read_json_file
from .operator_config import OperatorConfig, write_operator_config
from .operator_actions import (
    build_operator_action_queue,
    format_operator_action_queue_summary,
    format_operator_action_run_summary,
    run_operator_action,
    write_operator_action_queue,
)
from .operator_console import OperatorConsoleConfig, format_operator_console_summary, run_operator_console
from .operator_daily import OperatorDailyCheckConfig, format_operator_daily_check_summary, run_operator_daily_check
from .operator_self_heal import (
    OperatorSelfHealConfig,
    format_operator_self_heal_summary,
    run_operator_self_heal,
)
from .packets import JobLeaseRenewal, NodeRegistration
from .proof import OllamaProofConfig, SwarmProofConfig, proof_summary, run_ollama_proof, run_swarm_proof
from .privacy import PrivacyScanConfig, run_public_privacy_scan
from .quickstart import QuickstartConfig, format_quickstart_report, run_quickstart
from .provider import (
    ProviderEdgeProofConfig,
    ProviderOpsPackConfig,
    ProviderRemoteProofConfig,
    ProviderStatusConfig,
    add_provider_subscriber,
    bootstrap_provider_config,
    join_provider_node,
    run_provider_edge_proof,
    run_provider_ops_pack,
    run_provider_remote_proof,
    run_provider_status,
)
from .storage import SQLiteCoordinatorStore
from .worker import WorkerNode
from .windows_task import (
    DEFAULT_DAILY_CHECK_TASK_NAME,
    DEFAULT_TASK_NAME,
    DEFAULT_RELIABILITY_TASK_NAME,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    DailyCheckTaskConfig,
    ReliabilityTaskConfig,
    WatchdogTaskConfig,
    install_daily_check_task,
    install_reliability_task,
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


def _load_worker(
    home: Path,
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL,
    ollama_timeout_seconds: float = 300.0,
) -> WorkerNode:
    identity = _load_or_create_identity(home, "worker")
    return WorkerNode(
        identity=identity,
        capability_profile=load_node_capabilities(home),
        ollama_base_url=ollama_base_url,
        ollama_timeout_seconds=ollama_timeout_seconds,
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


def _node_status_connection_from_args(
    args: argparse.Namespace,
) -> tuple[str, str | None, dict[str, Any] | None]:
    invite = load_alpha_invite(Path(args.invite)) if getattr(args, "invite", None) else None
    coordinator_url = args.coordinator or (invite.coordinator if invite else default_coordinator_url(args.host, args.port))
    admission_token = args.admission_token or (invite.admission_token if invite else None)
    invite_summary = invite.public_summary() if invite else None
    return coordinator_url, admission_token, invite_summary


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


def run_quickstart_command(args: argparse.Namespace) -> None:
    try:
        report = run_quickstart(
            QuickstartConfig(
                home=Path(args.home),
                host=args.host,
                port=args.port,
                prompt=args.prompt,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
                worker_interval=args.worker_interval,
                force=args.force,
                stop_after_job=args.stop_after_job,
                ollama_base_url=args.ollama_base_url,
            )
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_quickstart_report(report))
    if not report["ok"]:
        raise SystemExit(1)


def operator_privacy_scan_command(args: argparse.Namespace) -> None:
    report = run_public_privacy_scan(
        PrivacyScanConfig(
            root=Path(args.root),
            report_path=Path(args.report) if args.report else None,
            include_provider_config_filenames=args.include_provider_config_filenames,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def operator_console_command(args: argparse.Namespace) -> None:
    try:
        report = run_operator_console(
            OperatorConsoleConfig(
                repo=Path(args.repo),
                home=Path(args.home),
                primary_invite_path=Path(args.primary_invite),
                backup_invite_path=Path(args.backup_invite) if args.backup_invite else None,
                reliability_dir=Path(args.reliability_dir) if args.reliability_dir else None,
                out_dir=Path(args.out),
                partner_report_paths=tuple(Path(path) for path in (args.partner_report or [])),
                expected_primary_worker_id=args.expected_primary_worker_id,
                expected_backup_worker_id=args.expected_backup_worker_id,
                skip_network_checks=args.skip_network_checks,
                timeout_seconds=args.timeout_seconds,
                freshness_seconds=args.freshness_seconds,
                history_limit=args.history_limit,
                stale_report_root=Path(args.stale_report_root) if args.stale_report_root else None,
                stale_report_days=args.stale_report_days,
                stale_report_max_items=args.stale_report_max_items,
                daily_check_dir=Path(args.daily_check_dir) if args.daily_check_dir else None,
                daily_check_task_name=args.daily_check_task_name,
                query_daily_check_task=not args.skip_daily_check_task_query,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_operator_console_summary(report))
    if report["status"] == "fail":
        raise SystemExit(1)


def operator_daily_check_command(args: argparse.Namespace) -> None:
    try:
        report = run_operator_daily_check(
            OperatorDailyCheckConfig(
                repo=Path(args.repo),
                home=Path(args.home),
                primary_invite_path=Path(args.primary_invite),
                backup_invite_path=Path(args.backup_invite) if args.backup_invite else None,
                reliability_dir=Path(args.reliability_dir) if args.reliability_dir else None,
                out_dir=Path(args.out),
                console_out_dir=Path(args.console_out) if args.console_out else None,
                partner_report_paths=tuple(Path(path) for path in (args.partner_report or [])),
                expected_primary_worker_id=args.expected_primary_worker_id,
                expected_backup_worker_id=args.expected_backup_worker_id,
                skip_network_checks=args.skip_network_checks,
                refresh_reliability_pack=args.refresh_reliability_pack,
                include_deterministic_smoke=args.include_deterministic_smoke,
                timeout_seconds=args.timeout_seconds,
                status_timeout_seconds=args.status_timeout_seconds,
                poll_interval=args.poll_interval,
                inference_jobs=args.inference_jobs,
                smoke_jobs=args.jobs,
                min_live_workers=args.min_live_workers,
                freshness_seconds=args.freshness_seconds,
                history_limit=args.history_limit,
                stale_report_root=Path(args.stale_report_root) if args.stale_report_root else None,
                stale_report_days=args.stale_report_days,
                stale_report_max_items=args.stale_report_max_items,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_operator_daily_check_summary(report))
    if report["status"] == "fail":
        raise SystemExit(1)


def operator_action_queue_command(args: argparse.Namespace) -> None:
    try:
        daily_report = read_json_file(Path(args.daily_report), description="daily check report")
        if not isinstance(daily_report, dict):
            raise ValueError("daily check report must be a JSON object")
        queue = build_operator_action_queue(daily_report)
        artifacts = write_operator_action_queue(Path(args.out), queue)
        queue["artifacts"] = artifacts
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(queue, indent=2, sort_keys=True))
    else:
        print(format_operator_action_queue_summary(queue))
    if queue["status"] == "fail":
        raise SystemExit(1)


def operator_run_action_command(args: argparse.Namespace) -> None:
    if args.execute and args.dry_run:
        raise SystemExit("--execute and --dry-run cannot be used together")
    queue_path = Path(args.queue)
    try:
        queue = read_json_file(queue_path, description="operator action queue")
        if not isinstance(queue, dict):
            raise ValueError("operator action queue must be a JSON object")
        report = run_operator_action(
            queue,
            queue_path=queue_path,
            action_id=args.action,
            command_index=args.command_index,
            dry_run=not args.execute,
            out_path=Path(args.out) if args.out else None,
            cwd=Path(args.cwd) if args.cwd else None,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_operator_action_run_summary(report))
    if report["status"] == "fail":
        raise SystemExit(1)


def operator_self_heal_command(args: argparse.Namespace) -> None:
    try:
        report = run_operator_self_heal(
            OperatorSelfHealConfig(
                console_report_path=Path(args.console_report),
                daily_report_path=Path(args.daily_report),
                action_queue_path=Path(args.action_queue),
                out_dir=Path(args.out),
                freshness_seconds=args.freshness_seconds,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_operator_self_heal_summary(report))
    if report["status"] == "fail":
        raise SystemExit(1)


def _run_operator_maintenance_command(
    command: list[str],
    *,
    label: str,
    cwd: Path,
) -> None:
    result = subprocess.run(command, check=False, text=True, capture_output=False, cwd=str(cwd))
    if result.returncode:
        raise SystemExit(f"{label} failed with exit code {result.returncode}")


def _run_operator_maintenance_fallback(args: argparse.Namespace, repo_root: Path) -> None:
    home = str(Path(args.home).resolve()) if args.home else str((repo_root / ".mesh").resolve())
    out_root = Path(args.out).resolve()
    daily_dir = out_root / "daily-check"
    console_dir = out_root / "operator-console"
    self_heal_dir = out_root / "operator-self-heal"
    daily_check_path = daily_dir / "daily-check.json"
    console_json = console_dir / "operator-console.json"
    action_queue_json = daily_dir / "action-queue.json"
    self_heal_json = self_heal_dir / "operator-self-heal-report.json"
    action_run_json = out_root / "operator-action-run-report.json"
    maintenance_json = out_root / "operator-maintenance-report.json"

    out_root.mkdir(parents=True, exist_ok=True)
    daily_dir.mkdir(parents=True, exist_ok=True)
    console_dir.mkdir(parents=True, exist_ok=True)
    self_heal_dir.mkdir(parents=True, exist_ok=True)

    exe = sys.executable
    console_command = [
        exe,
        "-m",
        "chatp2p.cli",
        "operator",
        "console",
        "--repo",
        str(repo_root),
        "--home",
        home,
        "--primary-invite",
        str(args.primary_invite),
        "--out",
        str(console_dir),
        "--daily-check-dir",
        str(daily_dir),
        "--json",
    ]
    if args.backup_invite:
        console_command.extend(["--backup-invite", str(args.backup_invite)])
    if args.expected_primary_worker_id:
        console_command.extend(["--expected-primary-worker-id", str(args.expected_primary_worker_id)])
    if args.expected_backup_worker_id:
        console_command.extend(["--expected-backup-worker-id", str(args.expected_backup_worker_id)])
    if args.reliability_dir is not None:
        console_command.extend(["--reliability-dir", str(args.reliability_dir)])
    if args.skip_network_checks:
        console_command.append("--skip-network-checks")
    for partner_report in args.partner_report or []:
        console_command.extend(["--partner-report", str(partner_report)])

    daily_command = [
        exe,
        "-m",
        "chatp2p.cli",
        "operator",
        "daily-check",
        "--repo",
        str(repo_root),
        "--home",
        home,
        "--primary-invite",
        str(args.primary_invite),
        "--out",
        str(daily_dir),
        "--console-out",
        str(console_dir),
        "--json",
    ]
    if args.backup_invite:
        daily_command.extend(["--backup-invite", str(args.backup_invite)])
    if args.expected_primary_worker_id:
        daily_command.extend(["--expected-primary-worker-id", str(args.expected_primary_worker_id)])
    if args.expected_backup_worker_id:
        daily_command.extend(["--expected-backup-worker-id", str(args.expected_backup_worker_id)])
    if args.reliability_dir is not None:
        daily_command.extend(["--reliability-dir", str(args.reliability_dir)])
    if args.skip_network_checks:
        daily_command.append("--skip-network-checks")

    action_queue_command = [
        exe,
        "-m",
        "chatp2p.cli",
        "operator",
        "action-queue",
        "--daily-report",
        str(daily_check_path),
        "--out",
        str(daily_dir),
        "--json",
    ]

    self_heal_command = [
        exe,
        "-m",
        "chatp2p.cli",
        "operator",
        "self-heal",
        "--console-report",
        str(console_json),
        "--daily-report",
        str(daily_check_path),
        "--action-queue",
        str(action_queue_json),
        "--out",
        str(self_heal_dir),
        "--json",
    ]

    steps: list[tuple[str, list[str]]] = [
        ("operator console", console_command),
        ("operator daily-check", daily_command),
        ("operator action-queue", action_queue_command),
        ("operator self-heal", self_heal_command),
    ]

    maintenance_report: dict[str, Any] = {
        "schema": "chatp2p.operator-maintenance-report.v1",
        "status": "pass",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "repo": str(repo_root),
            "home": home,
            "primary_invite": str(args.primary_invite),
            "backup_invite": str(args.backup_invite) if args.backup_invite else None,
            "out_dir": str(out_root),
            "reliability_dir": str(args.reliability_dir) if args.reliability_dir is not None else None,
            "expected_primary_worker_id": args.expected_primary_worker_id,
            "expected_backup_worker_id": args.expected_backup_worker_id,
            "skip_network_checks": bool(args.skip_network_checks),
            "partner_report": [str(path) for path in args.partner_report or []],
        },
        "artifacts": {
            "daily_check_json": str(daily_check_path),
            "console_json": str(console_json),
            "action_queue_json": str(action_queue_json),
            "self_heal_json": str(self_heal_json),
            "action_run_json": str(action_run_json),
            "maintenance_json": str(maintenance_json),
        },
        "steps": [],
    }

    for index, (label, command) in enumerate(steps, start=1):
        print(f"[{index}/{len(steps)}] {label}...")
        step_report = {
            "label": label,
            "command": [str(piece) for piece in command],
            "returncode": 0,
            "status": "pass",
        }
        maintenance_report["steps"].append(step_report)
        try:
            _run_operator_maintenance_command(command, label=label, cwd=repo_root)
        except SystemExit as exc:
            step_report["status"] = "fail"
            step_report["returncode"] = 1
            step_report["error"] = str(exc)
            maintenance_report["status"] = "fail"
            maintenance_json.write_text(
                json.dumps(maintenance_report, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            if args.json:
                print(json.dumps(maintenance_report, indent=2, sort_keys=True))
            else:
                print(f"\nOperator maintenance failed during {label}: {str(exc)}")
            raise

    console_report = read_json_file(console_json, description="operator console report")
    self_heal_report = read_json_file(self_heal_json, description="operator self-heal report")
    action_queue = read_json_file(action_queue_json, description="operator action queue")
    next_action = action_queue.get("next_action") if isinstance(action_queue, dict) else None

    console_summary = console_report.get("summary", {})
    top_action = next_action if isinstance(next_action, dict) else None
    top_action_status = "none"
    top_action_can_run_without_partner = False
    if top_action:
        has_local_commands = bool(top_action.get("suggested_commands"))
        if "can_run_without_partner" in top_action:
            top_action_can_run_without_partner = bool(top_action.get("can_run_without_partner"))
        else:
            # Legacy compatibility: if a queue entry omits this field, derive from partner_required.
            top_action_can_run_without_partner = not bool(top_action.get("partner_required"))

        if top_action.get("partner_required"):
            top_action_status = "partner_required"
        elif top_action_can_run_without_partner and has_local_commands:
            top_action_status = "safe_local"
        else:
            top_action_status = "not_local_executable"

    print("\nOperator maintenance complete.")
    print(f"Can continue without partner: {console_summary.get('can_continue_without_partner')}")
    print(f"Recommended next action:  {console_summary.get('recommended_next_action')}")
    print(
        f"Self-heal summary:        {self_heal_report.get('summary', {}).get('repairable_issue_count')} repairable "
        f"issue(s)"
    )

    if isinstance(next_action, dict):
        safe_action_message = (
            "safe to dry-run locally" if top_action_can_run_without_partner else "requires partner to act"
        )
        print(
            f"Top queue action:         {next_action.get('action_id')} (partner_required={next_action.get('partner_required')})"
        )
        print(f"Run preview:              {safe_action_message}")
        print(f"Top action status:        {top_action_status}")

    maintenance_report["summary"] = {
        "can_continue_without_partner": console_summary.get("can_continue_without_partner"),
        "recommended_next_action": console_summary.get("recommended_next_action"),
        "top_action": top_action,
        "top_action_status": top_action_status,
        "top_action_partner_required": top_action.get("partner_required") if top_action else None,
        "repairable_issue_count": self_heal_report.get("summary", {}).get("repairable_issue_count"),
    }

    if args.preview_top_action and top_action_status == "safe_local":
        print("\nPreparing preview...")
        preview_command = [
            exe,
            "-m",
            "chatp2p.cli",
            "operator",
            "run-action",
            "--queue",
            str(action_queue_json),
            "--out",
            str(action_run_json),
            "--json",
        ]
        if next_action:
            preview_command.extend(["--action", str(next_action.get("action_id"))])
        _run_operator_maintenance_command(preview_command, label="operator run-action (dry-run)", cwd=repo_root)
    elif args.preview_top_action:
        if top_action:
            print("Skipping preview because top action cannot be run locally.")
        else:
            raise SystemExit("run-top-action preview requested but no executable top action is available.")

    if args.run_top_action and next_action:
        if args.allow_execute:
            if top_action_status == "safe_local":
                print("\nRunning top local action now (allowed in operator V1)...")
                execute_command = [
                    exe,
                    "-m",
                    "chatp2p.cli",
                    "operator",
                    "run-action",
                    "--queue",
                    str(action_queue_json),
                    "--out",
                    str(action_run_json),
                    "--execute",
                    "--json",
                ]
                if next_action.get("action_id"):
                    execute_command.extend(["--action", str(next_action.get("action_id"))])
                _run_operator_maintenance_command(execute_command, label="operator run-action (execute)", cwd=repo_root)
            else:
                raise SystemExit(
                    "run-top-action requested, but top action is not safe for local execute. "
                    "Regenerate the queue and resolve partner-required items first."
                )
        else:
            print("run-top-action requires allow-execute to run local action")

    maintenance_json.write_text(json.dumps(maintenance_report, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(maintenance_report, indent=2, sort_keys=True))


def operator_maintenance_command(args: argparse.Namespace) -> None:
    if args.run_top_action and not args.allow_execute:
        raise SystemExit("--run-top-action requires --allow-execute")

    script_path = (Path(args.repo) / "scripts" / "operator-maintenance.ps1").resolve()
    if not script_path.exists():
        return _run_operator_maintenance_fallback(args, Path(args.repo).resolve())

    repo_root = Path(args.repo).resolve()
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-Root",
        str(repo_root),
        "-PrimaryInvite",
        str(args.primary_invite),
        "-OutRoot",
        str(args.out),
    ]

    if args.backup_invite:
        command.extend(["-BackupInvite", str(args.backup_invite)])
    if args.reliability_dir is not None:
        command.extend(["-ReliabilityDir", str(args.reliability_dir)])
    if args.home:
        command.extend(["-MeshHome", str(args.home)])
    if args.skip_network_checks:
        command.append("-SkipNetworkChecks")
    if args.expected_primary_worker_id:
        command.extend(["-ExpectedPrimaryWorkerId", str(args.expected_primary_worker_id)])
    if args.expected_backup_worker_id:
        command.extend(["-ExpectedBackupWorkerId", str(args.expected_backup_worker_id)])
    if args.partner_report:
        for partner_report in args.partner_report:
            command.extend(["-PartnerReport", str(partner_report)])
    if args.preview_top_action:
        command.append("-PreviewTopAction")
    if args.run_top_action:
        command.append("-RunTopAction")
    if args.allow_execute:
        command.append("-AllowExecute")

    result = subprocess.run(command, check=False, text=True, capture_output=False)
    if result.returncode:
        raise SystemExit(f"operator maintenance failed with exit code {result.returncode}")


def operator_install_daily_check_task_command(args: argparse.Namespace) -> None:
    try:
        report = install_daily_check_task(
            DailyCheckTaskConfig(
                repo=Path(args.repo),
                home=Path(args.home),
                primary_invite_path=Path(args.primary_invite),
                backup_invite_path=Path(args.backup_invite) if args.backup_invite else None,
                reliability_dir=Path(args.reliability_dir) if args.reliability_dir else None,
                out_dir=Path(args.out),
                console_out_dir=Path(args.console_out) if args.console_out else None,
                task_name=args.task_name,
                interval_minutes=args.interval_minutes,
                force=not args.no_force,
                startup_fallback=args.allow_startup_folder_fallback,
                partner_report_paths=tuple(Path(path) for path in (args.partner_report or [])),
                expected_primary_worker_id=args.expected_primary_worker_id,
                expected_backup_worker_id=args.expected_backup_worker_id,
                skip_network_checks=args.skip_network_checks,
                refresh_reliability_pack=args.refresh_reliability_pack,
                include_deterministic_smoke=args.include_deterministic_smoke,
                jobs=args.jobs,
                inference_jobs=args.inference_jobs,
                min_live_workers=args.min_live_workers,
                status_timeout_seconds=args.status_timeout_seconds,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
                freshness_seconds=args.freshness_seconds,
                history_limit=args.history_limit,
                stale_report_root=Path(args.stale_report_root) if args.stale_report_root else None,
                stale_report_days=args.stale_report_days,
                stale_report_max_items=args.stale_report_max_items,
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
    worker = _load_worker(
        Path(args.home),
        ollama_base_url=args.ollama_base_url,
        ollama_timeout_seconds=getattr(args, "ollama_timeout_seconds", 300.0),
    )
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
    leased = client.next_job_with_lease(worker.identity)
    if leased is None:
        return {"worker": worker.identity.node_id, "job": None, "status": "idle"}
    job, lease = leased

    renewal_tracker = _start_lease_renewal_loop(client, worker.identity, lease)
    try:
        result = worker.run_job(job)
    finally:
        renewal_tracker["stop"].set()
        thread = renewal_tracker.get("thread")
        if thread is not None:
            thread.join(timeout=2.0)
    submit_response = client.submit_result(result)
    return {
        "worker": worker.identity.node_id,
        "job_id": job.job_id,
        "job_type": job.job_type,
        "result_accepted": submit_response.get("accepted"),
        "credits": submit_response.get("credits"),
        "output": result.output,
        "lease_renewals": renewal_tracker["renewals"],
        "lease_renewal_errors": renewal_tracker["errors"],
        "status": "submitted" if submit_response.get("accepted") else "rejected",
    }


def _start_lease_renewal_loop(
    client: CoordinatorClient,
    identity: NodeIdentity,
    lease: dict[str, Any] | None,
) -> dict[str, Any]:
    tracker: dict[str, Any] = {"stop": threading.Event(), "thread": None, "renewals": [], "errors": []}
    if not lease or not lease.get("lease_id") or not lease.get("grant_hash"):
        return tracker

    stop_event: threading.Event = tracker["stop"]

    def renew_until_stopped() -> None:
        while True:
            wait_seconds = _lease_renewal_wait_seconds(lease)
            if stop_event.wait(wait_seconds):
                return
            try:
                renewal = JobLeaseRenewal.create(
                    node=identity,
                    lease_id=str(lease["lease_id"]),
                    job_id=str(lease["job_id"]),
                    grant_hash=str(lease["grant_hash"]),
                )
                response = client.renew_lease(renewal)
                if not response.get("accepted"):
                    tracker["errors"].append(response)
                    return
                renewed_lease = response.get("lease") or {}
                lease.update(renewed_lease)
                tracker["renewals"].append(
                    {
                        "renewal_id": renewal.renewal_id,
                        "expires_at": renewed_lease.get("expires_at"),
                    }
                )
            except Exception as exc:
                tracker["errors"].append({"error": f"{type(exc).__name__}: {exc}"})
                return

    thread = threading.Thread(target=renew_until_stopped, daemon=True)
    tracker["thread"] = thread
    thread.start()
    return tracker


def _lease_renewal_wait_seconds(lease: dict[str, Any]) -> float:
    expires_at = lease.get("expires_at")
    try:
        remaining = float(expires_at) - time.time()
    except (TypeError, ValueError):
        return 10.0
    return max(0.1, min(10.0, remaining / 3.0))


def run_worker_loop(args: argparse.Namespace) -> None:
    worker = _load_worker(
        Path(args.home),
        ollama_base_url=args.ollama_base_url,
        ollama_timeout_seconds=getattr(args, "ollama_timeout_seconds", 300.0),
    )
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
                f"{result['job_type']} {result['job_id']} credits={result['credits']} "
                f"renewals={len(result.get('lease_renewals', []))}"
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


def bootstrap_provider_command(args: argparse.Namespace) -> None:
    try:
        report = bootstrap_provider_config(
            config_path=Path(args.config),
            provider_name=args.provider_name,
            region=args.region,
            provider_id=args.provider_id,
            force=args.force,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))


def provider_ops_pack_command(args: argparse.Namespace) -> None:
    try:
        report = run_provider_ops_pack(
            ProviderOpsPackConfig(
                provider_config_path=Path(args.provider_config),
                out_dir=Path(args.out),
                subscribers=args.subscribers,
                edge_workers=args.edge_workers,
                peer_workers=args.peer_workers,
                verifier_workers=args.verifier_workers,
                jobs=args.jobs,
                timeout_seconds=args.timeout_seconds,
                create_zip=not args.no_zip,
                zip_path=Path(args.zip) if args.zip else None,
            )
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "status": report["status"],
                "schema": report["schema"],
                "provider": report["provider"],
                "proof": {
                    "jobs_created": report["proof"]["jobs_created"],
                    "jobs_verified": report["proof"]["jobs_verified"],
                    "jobs_disputed": report["proof"]["jobs_disputed"],
                    "jobs_expired": report["proof"]["jobs_expired"],
                    "route_counts": report["proof"]["route_counts"],
                },
                "artifacts": report["artifacts"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not report["ok"]:
        raise SystemExit(1)


def provider_remote_proof_command(args: argparse.Namespace) -> None:
    try:
        invite = load_alpha_invite(Path(args.invite))
        report = run_provider_remote_proof(
            ProviderRemoteProofConfig(
                provider_config_path=Path(args.provider_config),
                coordinator_url=invite.coordinator,
                admission_token=invite.admission_token,
                expected_worker_id=args.expected_worker_id,
                subscriber_id=args.subscriber_id,
                jobs=args.jobs,
                min_live_workers=args.min_live_workers,
                min_accepted_results=args.min_accepted_results,
                min_verified_jobs=args.min_verified_jobs,
                min_expected_worker_results=args.min_expected_worker_results,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
                report_path=Path(args.report),
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "status": report["status"],
                "schema": report["schema"],
                "provider": report["provider"],
                "coordinator": report["coordinator"],
                "created_jobs": len(report["created_jobs"]),
                "created_job_status_counts": report["created_job_status_counts"],
                "created_result_count": report["created_result_count"],
                "result_node_counts": report["result_node_counts"],
                "requested_route_counts": report["requested_route_counts"],
                "actual_result_route_counts": report["actual_result_route_counts"],
                "expected_worker": report["expected_worker"],
                "criteria": report["criteria"],
                "errors": report["errors"],
                "report": str(Path(args.report).expanduser().resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not report["ok"]:
        raise SystemExit(1)


def provider_status_command(args: argparse.Namespace) -> None:
    try:
        invite = load_alpha_invite(Path(args.invite))
        report = run_provider_status(
            ProviderStatusConfig(
                provider_config_path=Path(args.provider_config),
                coordinator_url=invite.coordinator,
                admission_token=invite.admission_token,
                expected_worker_id=args.expected_worker_id,
                timeout_seconds=args.timeout_seconds,
                report_path=Path(args.report) if args.report else None,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "status": report["status"],
                "schema": report["schema"],
                "provider": report["provider"],
                "coordinator": report["coordinator"],
                "summary": report["summary"],
                "expected_worker": report["expected_worker"],
                "nodes": report["nodes"],
                "job_status_counts": report["job_status_counts"],
                "result_node_counts": report["result_node_counts"],
                "result_route_counts": report["result_route_counts"],
                "criteria": report["criteria"],
                "errors": report["errors"],
                "report": str(Path(args.report).expanduser().resolve()) if args.report else None,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not report["ok"]:
        raise SystemExit(1)


def provider_create_subscriber_command(args: argparse.Namespace) -> None:
    try:
        report = add_provider_subscriber(
            config_path=Path(args.config),
            subscriber_id=args.subscriber_id,
            plan=args.plan,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))


def node_join_provider_command(args: argparse.Namespace) -> None:
    try:
        report = join_provider_node(
            provider_config_path=Path(args.provider_config),
            subscriber_id=args.subscriber_id,
            home=Path(args.home),
            node_role=args.node_role,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))


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


def alpha_network_status_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_network_status(
            AlphaNetworkStatusConfig(
                primary_invite_path=Path(args.primary_invite),
                backup_invite_path=Path(args.backup_invite),
                report_path=Path(args.report),
                expected_primary_worker_id=args.expected_primary_worker_id,
                expected_backup_worker_id=args.expected_backup_worker_id,
                min_primary_live_workers=args.min_primary_live_workers,
                min_backup_live_workers=args.min_backup_live_workers,
                timeout_seconds=args.timeout_seconds,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_failover_smoke_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_failover_smoke(
            AlphaFailoverSmokeConfig(
                primary_invite_path=Path(args.primary_invite),
                backup_invite_path=Path(args.backup_invite),
                report_path=Path(args.report),
                jobs=args.jobs,
                min_live_workers=args.min_live_workers,
                min_accepted_results=args.min_accepted_results,
                min_verified_jobs=args.min_verified_jobs,
                expected_primary_worker_id=args.expected_primary_worker_id,
                expected_backup_worker_id=args.expected_backup_worker_id,
                min_expected_primary_results=args.min_expected_primary_results,
                min_expected_backup_results=args.min_expected_backup_results,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_reliability_pack_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_reliability_pack(
            AlphaReliabilityPackConfig(
                primary_invite_path=Path(args.primary_invite),
                backup_invite_path=Path(args.backup_invite),
                out_dir=Path(args.out),
                expected_primary_worker_id=args.expected_primary_worker_id,
                expected_backup_worker_id=args.expected_backup_worker_id,
                include_deterministic_smoke=args.include_deterministic_smoke,
                smoke_jobs=args.jobs,
                inference_jobs=args.inference_jobs,
                min_live_workers=args.min_live_workers,
                status_timeout_seconds=args.status_timeout_seconds,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_install_reliability_task_command(args: argparse.Namespace) -> None:
    try:
        report = install_reliability_task(
            ReliabilityTaskConfig(
                primary_invite_path=Path(args.primary_invite),
                backup_invite_path=Path(args.backup_invite),
                out_dir=Path(args.out),
                task_name=args.task_name,
                interval_minutes=args.interval_minutes,
                force=not args.no_force,
                expected_primary_worker_id=args.expected_primary_worker_id,
                expected_backup_worker_id=args.expected_backup_worker_id,
                include_deterministic_smoke=args.include_deterministic_smoke,
                jobs=args.jobs,
                inference_jobs=args.inference_jobs,
                min_live_workers=args.min_live_workers,
                status_timeout_seconds=args.status_timeout_seconds,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
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


def alpha_inference_proof_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_inference_proof(
            AlphaInferenceProofConfig(
                invite_path=Path(args.invite),
                report_path=Path(args.report),
                jobs=args.jobs,
                mode=args.mode,
                model=args.model,
                prompt=args.prompt,
                temperature=args.temperature,
                expected_worker_id=args.expected_worker_id,
                min_live_workers=args.min_live_workers,
                min_accepted_results=args.min_accepted_results,
                min_verified_jobs=args.min_verified_jobs,
                min_expected_worker_results=args.min_expected_worker_results,
                timeout_seconds=args.timeout_seconds,
                request_timeout_seconds=args.request_timeout_seconds,
                poll_interval=args.poll_interval,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_soak_command(args: argparse.Namespace) -> None:
    try:
        report = run_alpha_soak(
            AlphaSoakConfig(
                invite_path=Path(args.invite),
                report_path=Path(args.report),
                jobs_per_round=args.jobs_per_round,
                rounds=args.rounds,
                duration_seconds=args.duration_seconds,
                round_timeout_seconds=args.round_timeout_seconds,
                round_interval_seconds=args.round_interval_seconds,
                mode=args.mode,
                model=args.model,
                prompt=args.prompt,
                temperature=args.temperature,
                expected_worker_id=args.expected_worker_id,
                min_live_workers=args.min_live_workers,
                min_accepted_results_per_round=args.min_accepted_results_per_round,
                min_verified_jobs_per_round=args.min_verified_jobs_per_round,
                min_expected_worker_results_per_round=args.min_expected_worker_results_per_round,
                min_expected_worker_results_total=args.min_expected_worker_results_total,
                request_timeout_seconds=args.request_timeout_seconds,
                poll_interval=args.poll_interval,
                stop_on_failure=args.stop_on_failure,
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


def alpha_evidence_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    invite = Path(args.invite) if args.invite else home.parent / "alpha-invite.json"
    out_dir = Path(args.out) if args.out else home.parent / "alpha-evidence"
    watchdog_report = Path(args.watchdog_report) if args.watchdog_report else None
    try:
        report = run_alpha_evidence(
            AlphaEvidenceConfig(
                home=home,
                invite_path=invite,
                out_dir=out_dir,
                expected_worker_id=args.expected_worker_id,
                jobs=args.jobs,
                min_live_workers=args.min_live_workers,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
                status_timeout_seconds=args.status_timeout_seconds,
                watchdog_report_path=watchdog_report,
                operator_task_name=args.operator_task_name,
                query_operator_task=not args.no_task_query,
                include_inference_proof=args.include_inference_proof,
                inference_mode=args.inference_mode,
                inference_model=args.inference_model,
                inference_jobs=args.inference_jobs,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        raise SystemExit(1)


def alpha_ops_pack_command(args: argparse.Namespace) -> None:
    home = Path(args.home)
    invite = Path(args.invite) if args.invite else home.parent / "alpha-invite.json"
    out_dir = Path(args.out) if args.out else home.parent / "alpha-ops-pack"
    watchdog_report = Path(args.watchdog_report) if args.watchdog_report else None
    zip_path = Path(args.zip) if args.zip else None
    try:
        report = run_alpha_ops_pack(
            AlphaOpsPackConfig(
                home=home,
                invite_path=invite,
                out_dir=out_dir,
                expected_worker_id=args.expected_worker_id,
                jobs=args.jobs,
                min_live_workers=args.min_live_workers,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
                status_timeout_seconds=args.status_timeout_seconds,
                watchdog_report_path=watchdog_report,
                operator_task_name=args.operator_task_name,
                query_operator_task=not args.no_task_query,
                include_routing_evidence=args.include_routing_evidence,
                inference_mode=args.inference_mode,
                inference_model=args.inference_model,
                inference_jobs=args.inference_jobs,
                create_zip=not args.no_zip,
                zip_path=zip_path,
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


def run_proof_provider_edge(args: argparse.Namespace) -> None:
    config = ProviderEdgeProofConfig(
        provider_config_path=Path(args.provider_config),
        subscribers=args.subscribers,
        edge_workers=args.edge_workers,
        peer_workers=args.peer_workers,
        verifier_workers=args.verifier_workers,
        jobs=args.jobs,
        report_path=Path(args.report),
        timeout_seconds=args.timeout_seconds,
    )
    try:
        report = run_provider_edge_proof(config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "status": report["status"],
                "schema": report["schema"],
                "provider_id": report["provider_id"],
                "report": str(Path(args.report).expanduser().resolve()),
                "subscribers_created": report["subscribers_created"],
                "subscriber_nodes_live": report["subscriber_nodes_live"],
                "edge_workers_live": report["edge_workers_live"],
                "jobs_created": report["jobs_created"],
                "jobs_verified": report["jobs_verified"],
                "jobs_disputed": report["jobs_disputed"],
                "jobs_expired": report["jobs_expired"],
                "route_counts": report["route_counts"],
                "verification_rate": report["verification_rate"],
                "failure_reasons": report["failure_reasons"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not report["ok"]:
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


def run_node_refresh_capabilities_command(args: argparse.Namespace) -> None:
    try:
        report = refresh_node_capabilities(
            NodeCapabilityRefreshConfig(
                home=Path(args.home),
                invite_path=Path(args.invite) if args.invite else None,
                report_path=Path(args.report) if args.report else None,
                provider_config_path=Path(args.provider_config) if args.provider_config else None,
                provider_node_role=args.node_role,
                provider_subscriber_id=args.subscriber_id,
                restart_worker=args.restart_worker,
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
                ollama_timeout_seconds=args.ollama_timeout_seconds,
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
    try:
        coordinator_url, admission_token, invite_summary = _node_status_connection_from_args(args)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    health = None
    if not args.skip_health:
        health = _coordinator_health(coordinator_url=coordinator_url, admission_token=admission_token)
    processes = managed_processes_status(home=home)
    print(
        json.dumps(
            {
                "ok": all(process["alive"] for process in processes if process["managed"]),
                "home": str(home.expanduser().resolve()),
                "coordinator": coordinator_url,
                "invite": invite_summary,
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
        "--ollama-timeout-seconds",
        str(args.ollama_timeout_seconds),
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

    quickstart_parser = subcommands.add_parser(
        "quickstart",
        help="Start a local coordinator and worker, run one job, and print the result",
    )
    quickstart_parser.add_argument(
        "--home",
        default=".mesh/quickstart",
        help="Directory for the local quickstart coordinator, worker, database, and logs",
    )
    quickstart_parser.add_argument("--host", default="127.0.0.1", help="Coordinator host to bind")
    quickstart_parser.add_argument("--port", default=8766, type=int, help="Coordinator port to bind")
    quickstart_parser.add_argument(
        "--prompt",
        default="ChatP2P quickstart: echo this signed job.",
        help="Prompt for the echo job",
    )
    quickstart_parser.add_argument(
        "--timeout-seconds",
        default=45.0,
        type=float,
        help="Maximum time to wait for coordinator, worker, and result",
    )
    quickstart_parser.add_argument(
        "--poll-interval",
        default=0.25,
        type=float,
        help="Seconds between quickstart health/result checks",
    )
    quickstart_parser.add_argument(
        "--worker-interval",
        default=0.5,
        type=float,
        help="Seconds between worker polling attempts",
    )
    quickstart_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL advertised by the worker",
    )
    quickstart_parser.add_argument("--force", action="store_true", help="Restart managed quickstart processes")
    quickstart_parser.add_argument(
        "--stop-after-job",
        action="store_true",
        help="Stop quickstart coordinator and worker after the result is printed",
    )
    quickstart_parser.add_argument("--json", action="store_true", help="Print the full JSON quickstart report")
    quickstart_parser.set_defaults(func=run_quickstart_command)

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
    join_parser.add_argument(
        "--ollama-timeout-seconds",
        default=300.0,
        type=float,
        help="Seconds to wait for one local Ollama inference request",
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

    join_provider_parser = node_subcommands.add_parser(
        "join-provider",
        help="Create a provider-mode node identity and capability profile",
    )
    join_provider_parser.add_argument("--provider-config", required=True, help="Path to provider config JSON")
    join_provider_parser.add_argument("--subscriber-id", required=True, help="Subscriber ID from the provider config")
    join_provider_parser.add_argument("--home", default=".mesh", help="Directory for node identity and capabilities")
    join_provider_parser.add_argument(
        "--node-role",
        default="subscriber_gateway",
        choices=[
            "subscriber_gateway",
            "subscriber_device",
            "provider_edge_worker",
            "contributor_worker",
            "verifier",
        ],
        help="Provider-mode node role to advertise",
    )
    join_provider_parser.set_defaults(func=node_join_provider_command)

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
    up_parser.add_argument(
        "--ollama-timeout-seconds",
        default=300.0,
        type=float,
        help="Seconds to wait for one local Ollama inference request",
    )
    up_parser.add_argument("--worker-interval", default=5.0, type=float, help="Seconds between worker polling attempts")
    up_parser.add_argument(
        "--startup-timeout-seconds",
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
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
    status_parser.add_argument(
        "--invite",
        default=None,
        help="Alpha invite JSON to derive the coordinator URL and admission token",
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
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
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
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
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

    refresh_capabilities_parser = node_subcommands.add_parser(
        "refresh-capabilities",
        help="Re-benchmark this machine and optionally restart the managed worker",
    )
    refresh_capabilities_parser.add_argument("--home", default=".mesh", help="Directory for node identity and capabilities")
    refresh_capabilities_parser.add_argument(
        "--invite",
        default=None,
        help="Alpha invite path. Required when --restart-worker is used",
    )
    refresh_capabilities_parser.add_argument(
        "--report",
        default=None,
        help="Optional JSON report path",
    )
    refresh_capabilities_parser.add_argument(
        "--provider-config",
        default=None,
        help="Provider config JSON used to stamp provider-mode role metadata into capabilities",
    )
    refresh_capabilities_parser.add_argument(
        "--node-role",
        default=None,
        choices=[
            "subscriber_gateway",
            "subscriber_device",
            "provider_edge_worker",
            "contributor_worker",
            "verifier",
        ],
        help="Provider-mode role to advertise with refreshed capabilities",
    )
    refresh_capabilities_parser.add_argument(
        "--subscriber-id",
        default=None,
        help="Provider subscriber id for subscriber_gateway or subscriber_device roles",
    )
    refresh_capabilities_parser.add_argument(
        "--restart-worker",
        action="store_true",
        help="Restart the managed worker after saving refreshed capabilities",
    )
    refresh_capabilities_parser.add_argument(
        "--worker-interval",
        default=0.5,
        type=float,
        help="Worker loop interval when --restart-worker is used",
    )
    refresh_capabilities_parser.add_argument(
        "--startup-timeout-seconds",
        default=15.0,
        type=float,
        help="Seconds to wait for restarted worker registration",
    )
    refresh_capabilities_parser.add_argument(
        "--cpu-duration-seconds",
        default=0.25,
        type=float,
        help="Seconds to spend on the tiny CPU benchmark",
    )
    refresh_capabilities_parser.add_argument(
        "--ollama-base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help="Local Ollama base URL for model discovery",
    )
    refresh_capabilities_parser.set_defaults(func=run_node_refresh_capabilities_command)

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

    privacy_scan_parser = operator_subcommands.add_parser(
        "privacy-scan",
        help="Scan a public repo tree for committed secrets and private alpha identifiers",
    )
    privacy_scan_parser.add_argument("--root", default=".", help="Repository root to scan")
    privacy_scan_parser.add_argument("--report", default=None, help="Optional JSON report path")
    privacy_scan_parser.add_argument(
        "--include-provider-config-filenames",
        action="store_true",
        help="Also fail on tracked provider-config JSON filenames",
    )
    privacy_scan_parser.set_defaults(func=operator_privacy_scan_command)

    operator_console_parser = operator_subcommands.add_parser(
        "console",
        help="Write a static operator console report without starting jobs or processes",
    )
    operator_console_parser.add_argument("--repo", default=".", help="Public repository root to privacy-scan")
    operator_console_parser.add_argument("--home", default=".mesh", help="Local runtime home to inspect")
    operator_console_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    operator_console_parser.add_argument("--backup-invite", default=None, help="Optional backup alpha invite JSON")
    operator_console_parser.add_argument(
        "--reliability-dir",
        default=None,
        help="Optional reliability-pack directory containing reliability-summary.json",
    )
    operator_console_parser.add_argument("--out", required=True, help="Output directory for console artifacts")
    operator_console_parser.add_argument(
        "--partner-report",
        action="append",
        default=None,
        help="Optional partner/autopilot report JSON. Can be passed more than once",
    )
    operator_console_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID expected to be live",
    )
    operator_console_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID expected to be live",
    )
    operator_console_parser.add_argument(
        "--skip-network-checks",
        action="store_true",
        help="Skip coordinator health/snapshot HTTP checks and build an offline report",
    )
    operator_console_parser.add_argument(
        "--timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for coordinator health and snapshot checks",
    )
    operator_console_parser.add_argument(
        "--freshness-seconds",
        default=3600.0,
        type=float,
        help="Maximum age for reliability/autopilot reports before marking them stale",
    )
    operator_console_parser.add_argument(
        "--history-limit",
        default=20,
        type=int,
        help="Number of operator-console history entries to keep",
    )
    operator_console_parser.add_argument(
        "--stale-report-root",
        default=None,
        help="Root directory to scan for old report/proof artifacts. Defaults to HOME parent",
    )
    operator_console_parser.add_argument(
        "--stale-report-days",
        default=2.0,
        type=float,
        help="Report artifacts older than this many days are listed as cleanup candidates",
    )
    operator_console_parser.add_argument(
        "--stale-report-max-items",
        default=50,
        type=int,
        help="Maximum stale report candidates to include",
    )
    operator_console_parser.add_argument(
        "--daily-check-dir",
        default=None,
        help="Directory containing daily-check.json. Defaults to HOME parent/daily-check",
    )
    operator_console_parser.add_argument(
        "--daily-check-task-name",
        default=DEFAULT_DAILY_CHECK_TASK_NAME,
        help="Windows Scheduled Task name for the hourly daily check",
    )
    operator_console_parser.add_argument(
        "--skip-daily-check-task-query",
        action="store_true",
        help="Skip querying Windows Scheduled Tasks for the daily check task",
    )
    operator_console_parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    operator_console_parser.set_defaults(func=operator_console_command)

    operator_daily_check_parser = operator_subcommands.add_parser(
        "daily-check",
        help="Run the lightweight daily operator gate and write one status summary",
    )
    operator_daily_check_parser.add_argument("--repo", default=".", help="Public repository root to privacy-scan")
    operator_daily_check_parser.add_argument("--home", default=".mesh", help="Local runtime home to inspect")
    operator_daily_check_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    operator_daily_check_parser.add_argument("--backup-invite", default=None, help="Optional backup alpha invite JSON")
    operator_daily_check_parser.add_argument(
        "--reliability-dir",
        default=None,
        help="Optional reliability-pack directory containing reliability-summary.json",
    )
    operator_daily_check_parser.add_argument("--out", required=True, help="Output directory for daily-check artifacts")
    operator_daily_check_parser.add_argument(
        "--console-out",
        default=None,
        help="Operator Console output directory. Defaults to OUT/operator-console",
    )
    operator_daily_check_parser.add_argument(
        "--partner-report",
        action="append",
        default=None,
        help="Optional partner/autopilot report JSON. Can be passed more than once",
    )
    operator_daily_check_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID expected to be live",
    )
    operator_daily_check_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID expected to be live",
    )
    operator_daily_check_parser.add_argument(
        "--skip-network-checks",
        action="store_true",
        help="Skip coordinator health/snapshot HTTP checks and build an offline report",
    )
    operator_daily_check_parser.add_argument(
        "--refresh-reliability-pack",
        action="store_true",
        help="Also run reliability-pack before writing the daily summary",
    )
    operator_daily_check_parser.add_argument(
        "--include-deterministic-smoke",
        action="store_true",
        help="When refreshing reliability, also run deterministic smoke. Disabled by default.",
    )
    operator_daily_check_parser.add_argument(
        "--jobs",
        default=4,
        type=int,
        help="Deterministic smoke jobs per lane when --include-deterministic-smoke is used",
    )
    operator_daily_check_parser.add_argument(
        "--inference-jobs",
        default=4,
        type=int,
        help="Verified echo inference jobs per lane when refreshing reliability",
    )
    operator_daily_check_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required when refreshing reliability",
    )
    operator_daily_check_parser.add_argument(
        "--status-timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for coordinator health and snapshot checks",
    )
    operator_daily_check_parser.add_argument(
        "--timeout-seconds",
        default=90.0,
        type=float,
        help="Maximum time to wait for optional reliability refresh work",
    )
    operator_daily_check_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between optional reliability refresh polls",
    )
    operator_daily_check_parser.add_argument(
        "--freshness-seconds",
        default=3600.0,
        type=float,
        help="Maximum age for reliability/autopilot reports before marking them stale",
    )
    operator_daily_check_parser.add_argument(
        "--history-limit",
        default=20,
        type=int,
        help="Number of operator-console history entries to keep",
    )
    operator_daily_check_parser.add_argument(
        "--stale-report-root",
        default=None,
        help="Root directory to scan for old report/proof artifacts. Defaults to HOME parent",
    )
    operator_daily_check_parser.add_argument(
        "--stale-report-days",
        default=2.0,
        type=float,
        help="Report artifacts older than this many days are listed as cleanup candidates",
    )
    operator_daily_check_parser.add_argument(
        "--stale-report-max-items",
        default=50,
        type=int,
        help="Maximum stale report candidates to include",
    )
    operator_daily_check_parser.add_argument("--json", action="store_true", help="Print the full JSON report")
    operator_daily_check_parser.set_defaults(func=operator_daily_check_command)

    operator_action_queue_parser = operator_subcommands.add_parser(
        "action-queue",
        help="Build a ranked action queue from an operator daily-check report",
    )
    operator_action_queue_parser.add_argument(
        "--daily-report",
        required=True,
        help="Path to daily-check.json",
    )
    operator_action_queue_parser.add_argument(
        "--out",
        required=True,
        help="Output directory for action-queue.json and action-queue.md",
    )
    operator_action_queue_parser.add_argument("--json", action="store_true", help="Print the full JSON action queue")
    operator_action_queue_parser.set_defaults(func=operator_action_queue_command)

    operator_run_action_parser = operator_subcommands.add_parser(
        "run-action",
        help="Dry-run or execute an allowlisted suggested command from action-queue.json",
    )
    operator_run_action_parser.add_argument("--queue", required=True, help="Path to action-queue.json")
    operator_run_action_parser.add_argument(
        "--action",
        default=None,
        help="Action id to run. Defaults to the queue's next_action",
    )
    operator_run_action_parser.add_argument(
        "--command-index",
        default=0,
        type=int,
        help="Suggested command index to use for the selected action",
    )
    operator_run_action_parser.add_argument(
        "--out",
        default=None,
        help="Path for operator-action-run-report.json. Defaults next to the queue",
    )
    operator_run_action_parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for --execute. Defaults to the current directory",
    )
    operator_run_action_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the selected allowlisted command without executing it. This is the default",
    )
    operator_run_action_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run the selected allowlisted local operator command",
    )
    operator_run_action_parser.add_argument("--json", action="store_true", help="Print the full JSON run report")
    operator_run_action_parser.set_defaults(func=operator_run_action_command)

    operator_self_heal_parser = operator_subcommands.add_parser(
        "self-heal",
        help="Build a read-only self-heal report for local operator evidence and task issues",
    )
    operator_self_heal_parser.add_argument(
        "--console-report",
        required=True,
        help="Path to operator-console.json",
    )
    operator_self_heal_parser.add_argument(
        "--daily-report",
        required=True,
        help="Path to daily-check.json",
    )
    operator_self_heal_parser.add_argument(
        "--action-queue",
        required=True,
        help="Path to action-queue.json",
    )
    operator_self_heal_parser.add_argument("--out", required=True, help="Output directory for self-heal artifacts")
    operator_self_heal_parser.add_argument(
        "--freshness-seconds",
        default=3600.0,
        type=float,
        help="Maximum age for console/daily/action reports before marking them stale",
    )
    operator_self_heal_parser.add_argument("--json", action="store_true", help="Print the full JSON self-heal report")
    operator_self_heal_parser.set_defaults(func=operator_self_heal_command)

    operator_maintenance_parser = operator_subcommands.add_parser(
        "maintenance",
        help="Run the full local operator maintenance loop in one command",
    )
    operator_maintenance_parser.add_argument("--repo", default=".", help="Public repository root containing scripts and CLI modules")
    operator_maintenance_parser.add_argument(
        "--home",
        default=None,
        help="Mesh home path to pass through (defaults to <repo>\\.mesh)",
    )
    operator_maintenance_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    operator_maintenance_parser.add_argument("--backup-invite", default=None, help="Optional backup alpha invite JSON")
    operator_maintenance_parser.add_argument(
        "--out",
        required=True,
        help="Output root for operator maintenance artifacts",
    )
    operator_maintenance_parser.add_argument(
        "--reliability-dir",
        default=None,
        help="Optional reliability-pack directory containing reliability-summary.json",
    )
    operator_maintenance_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID expected to be live",
    )
    operator_maintenance_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID expected to be live",
    )
    operator_maintenance_parser.add_argument(
        "--skip-network-checks",
        action="store_true",
        help="Skip coordinator health/snapshot checks in the maintenance pass",
    )
    operator_maintenance_parser.add_argument(
        "--partner-report",
        action="append",
        default=None,
        help="Optional partner/autopilot report JSON. Can be passed more than once",
    )
    operator_maintenance_parser.add_argument(
        "--preview-top-action",
        action="store_true",
        help="Preview the top queued maintenance action only",
    )
    operator_maintenance_parser.add_argument(
        "--run-top-action",
        action="store_true",
        help="Run the top local operator action after maintenance (must pair with --allow-execute)",
    )
    operator_maintenance_parser.add_argument(
        "--allow-execute",
        action="store_true",
        help="Allow operator maintenance to execute the top action",
    )
    operator_maintenance_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full maintenance JSON report",
    )
    operator_maintenance_parser.set_defaults(func=operator_maintenance_command)

    operator_install_daily_check_task_parser = operator_subcommands.add_parser(
        "install-daily-check-task",
        help="Install a Windows Scheduled Task that periodically runs operator daily-check",
    )
    operator_install_daily_check_task_parser.add_argument("--repo", default=".", help="Public repository root to privacy-scan")
    operator_install_daily_check_task_parser.add_argument("--home", default=".mesh", help="Local runtime home to inspect")
    operator_install_daily_check_task_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    operator_install_daily_check_task_parser.add_argument("--backup-invite", default=None, help="Optional backup alpha invite JSON")
    operator_install_daily_check_task_parser.add_argument(
        "--reliability-dir",
        default=None,
        help="Optional reliability-pack directory containing reliability-summary.json",
    )
    operator_install_daily_check_task_parser.add_argument("--out", required=True, help="Output directory for daily-check artifacts")
    operator_install_daily_check_task_parser.add_argument(
        "--console-out",
        default=None,
        help="Operator Console output directory. Defaults to OUT/operator-console",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--task-name",
        default=DEFAULT_DAILY_CHECK_TASK_NAME,
        help="Windows Scheduled Task name",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--interval-minutes",
        default=60,
        type=int,
        help="Minutes between daily-check runs",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--partner-report",
        action="append",
        default=None,
        help="Optional partner/autopilot report JSON. Can be passed more than once",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID expected to be live",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID expected to be live",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--skip-network-checks",
        action="store_true",
        help="Skip coordinator health/snapshot HTTP checks and build an offline report",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--refresh-reliability-pack",
        action="store_true",
        help="Also run reliability-pack before writing the daily summary",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--include-deterministic-smoke",
        action="store_true",
        help="When refreshing reliability, also run deterministic smoke. Disabled by default.",
    )
    operator_install_daily_check_task_parser.add_argument("--jobs", default=4, type=int, help="Deterministic smoke jobs")
    operator_install_daily_check_task_parser.add_argument(
        "--inference-jobs",
        default=4,
        type=int,
        help="Verified echo inference jobs when refreshing reliability",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required when refreshing reliability",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--status-timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for coordinator health and snapshot checks",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--timeout-seconds",
        default=90.0,
        type=float,
        help="Maximum time to wait for optional reliability refresh work",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between optional reliability refresh polls",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--freshness-seconds",
        default=3600.0,
        type=float,
        help="Maximum age for reliability/autopilot reports before marking them stale",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--history-limit",
        default=20,
        type=int,
        help="Number of operator-console history entries to keep",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--stale-report-root",
        default=None,
        help="Root directory to scan for old report/proof artifacts. Defaults to HOME parent",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--stale-report-days",
        default=2.0,
        type=float,
        help="Report artifacts older than this many days are listed as cleanup candidates",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--stale-report-max-items",
        default=50,
        type=int,
        help="Maximum stale report candidates to include",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--work-dir",
        default=None,
        help="Working directory for the generated launcher. Defaults to the repo root",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--launcher",
        default=None,
        help="Path for generated .cmd launcher. Defaults to OUT/run/<task-name>.cmd",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--no-force",
        action="store_true",
        help="Do not replace an existing Scheduled Task of the same name",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--allow-startup-folder-fallback",
        action="store_true",
        help="If Scheduled Task creation is denied, install a per-user Startup folder launcher",
    )
    operator_install_daily_check_task_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the task plan without writing the launcher or creating a Scheduled Task",
    )
    operator_install_daily_check_task_parser.set_defaults(func=operator_install_daily_check_task_command)

    bootstrap_provider_parser = operator_subcommands.add_parser(
        "bootstrap-provider",
        help="Write an ISP-edge / broadband-bundle provider simulation config",
    )
    bootstrap_provider_parser.add_argument("--config", required=True, help="Path for provider config JSON")
    bootstrap_provider_parser.add_argument("--provider-name", required=True, help="Provider display name")
    bootstrap_provider_parser.add_argument("--region", required=True, help="Provider region label")
    bootstrap_provider_parser.add_argument("--provider-id", default=None, help="Optional stable provider id")
    bootstrap_provider_parser.add_argument("--force", action="store_true", help="Replace an existing config")
    bootstrap_provider_parser.set_defaults(func=bootstrap_provider_command)

    provider_ops_pack_parser = operator_subcommands.add_parser(
        "provider-ops-pack",
        help="Build provider-edge simulation evidence, handoff notes, and an optional zip",
    )
    provider_ops_pack_parser.add_argument("--provider-config", required=True, help="Path to provider config JSON")
    provider_ops_pack_parser.add_argument("--out", required=True, help="Provider ops pack output directory")
    provider_ops_pack_parser.add_argument("--subscribers", default=3, type=int, help="Subscribers to simulate")
    provider_ops_pack_parser.add_argument("--edge-workers", default=1, type=int, help="Provider edge workers to simulate")
    provider_ops_pack_parser.add_argument("--peer-workers", default=1, type=int, help="Trusted peer workers to simulate")
    provider_ops_pack_parser.add_argument("--verifier-workers", default=1, type=int, help="Verifier workers to simulate")
    provider_ops_pack_parser.add_argument("--jobs", default=25, type=int, help="Subscriber jobs to create")
    provider_ops_pack_parser.add_argument(
        "--timeout-seconds",
        default=60.0,
        type=float,
        help="Maximum provider proof runtime before marking the pack failed",
    )
    provider_ops_pack_parser.add_argument("--zip", default=None, help="Optional zip output path. Defaults to OUT.zip")
    provider_ops_pack_parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip zip creation and leave the ops pack as a folder only",
    )
    provider_ops_pack_parser.set_defaults(func=provider_ops_pack_command)

    provider_remote_proof_parser = operator_subcommands.add_parser(
        "provider-remote-proof",
        help="Run provider-shaped proof jobs on a live alpha coordinator",
    )
    provider_remote_proof_parser.add_argument("--invite", required=True, help="Path to alpha invite JSON")
    provider_remote_proof_parser.add_argument("--provider-config", required=True, help="Path to provider config JSON")
    provider_remote_proof_parser.add_argument("--expected-worker-id", default=None, help="Worker that should contribute")
    provider_remote_proof_parser.add_argument("--subscriber-id", default=None, help="Subscriber id to attach to proof jobs")
    provider_remote_proof_parser.add_argument("--jobs", default=10, type=int, help="Provider-shaped jobs to create")
    provider_remote_proof_parser.add_argument(
        "--min-live-workers",
        default=2,
        type=int,
        help="Minimum live workers required",
    )
    provider_remote_proof_parser.add_argument(
        "--min-accepted-results",
        default=None,
        type=int,
        help="Minimum accepted results. Defaults to jobs * 2",
    )
    provider_remote_proof_parser.add_argument(
        "--min-verified-jobs",
        default=None,
        type=int,
        help="Minimum verified proof jobs. Defaults to jobs",
    )
    provider_remote_proof_parser.add_argument(
        "--min-expected-worker-results",
        default=None,
        type=int,
        help="Minimum results from expected worker. Defaults to 1 when expected worker is set",
    )
    provider_remote_proof_parser.add_argument(
        "--timeout-seconds",
        default=120.0,
        type=float,
        help="Maximum time to wait for proof thresholds",
    )
    provider_remote_proof_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls",
    )
    provider_remote_proof_parser.add_argument("--report", required=True, help="Path for provider remote proof report")
    provider_remote_proof_parser.set_defaults(func=provider_remote_proof_command)

    provider_status_parser = operator_subcommands.add_parser(
        "provider-status",
        help="Show live ISP-edge / broadband-bundle status from a coordinator snapshot",
    )
    provider_status_parser.add_argument("--invite", required=True, help="Path to alpha invite JSON")
    provider_status_parser.add_argument("--provider-config", required=True, help="Path to provider config JSON")
    provider_status_parser.add_argument("--expected-worker-id", default=None, help="Worker that should be live")
    provider_status_parser.add_argument(
        "--timeout-seconds",
        default=10.0,
        type=float,
        help="HTTP timeout for health and snapshot checks",
    )
    provider_status_parser.add_argument("--report", default=None, help="Optional path for provider status JSON report")
    provider_status_parser.set_defaults(func=provider_status_command)

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

    alpha_evidence_parser = operator_subcommands.add_parser(
        "alpha-evidence",
        help="Build a redacted evidence pack with status, remote proof, watchdog, and task reports",
    )
    alpha_evidence_parser.add_argument("--home", required=True, help="Coordinator and primary worker home directory")
    alpha_evidence_parser.add_argument(
        "--invite",
        default=None,
        help="Path to alpha invite JSON. Defaults to HOME parent/alpha-invite.json",
    )
    alpha_evidence_parser.add_argument(
        "--out",
        default=None,
        help="Evidence pack directory. Defaults to HOME parent/alpha-evidence",
    )
    alpha_evidence_parser.add_argument(
        "--expected-worker-id",
        default=None,
        help="Worker node ID that should be live and return proof results",
    )
    alpha_evidence_parser.add_argument(
        "--jobs",
        default=25,
        type=int,
        help="Deterministic eval jobs to create for the remote proof",
    )
    alpha_evidence_parser.add_argument(
        "--min-live-workers",
        default=2,
        type=int,
        help="Minimum live workers required for status and remote proof pass",
    )
    alpha_evidence_parser.add_argument(
        "--timeout-seconds",
        default=300.0,
        type=float,
        help="Maximum time to wait for the remote proof",
    )
    alpha_evidence_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls during remote proof",
    )
    alpha_evidence_parser.add_argument(
        "--status-timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for the status health and snapshot checks",
    )
    alpha_evidence_parser.add_argument(
        "--watchdog-report",
        default=None,
        help="Existing watchdog report to copy. Defaults to HOME parent/node-watchdog-report.json",
    )
    alpha_evidence_parser.add_argument(
        "--operator-task-name",
        default=DEFAULT_OPERATOR_TASK_NAME,
        help="Windows Scheduled Task name to query for operator watchdog evidence",
    )
    alpha_evidence_parser.add_argument(
        "--no-task-query",
        action="store_true",
        help="Skip Windows Scheduled Task query",
    )
    alpha_evidence_parser.add_argument(
        "--include-inference-proof",
        action="store_true",
        help="Run an inference proof and include it as a redacted evidence sidecar",
    )
    alpha_evidence_parser.add_argument(
        "--inference-mode",
        choices=("echo", "auto", "ollama"),
        default="echo",
        help="Inference proof mode when --include-inference-proof is set",
    )
    alpha_evidence_parser.add_argument(
        "--inference-model",
        default=None,
        help="Ollama model to require for auto/ollama inference evidence",
    )
    alpha_evidence_parser.add_argument(
        "--inference-jobs",
        default=20,
        type=int,
        help="Inference jobs to create when --include-inference-proof is set",
    )
    alpha_evidence_parser.set_defaults(func=alpha_evidence_command)

    alpha_ops_pack_parser = operator_subcommands.add_parser(
        "alpha-ops-pack",
        help="Build a redacted operator pack with evidence, handoff notes, and an optional zip",
    )
    alpha_ops_pack_parser.add_argument("--home", required=True, help="Coordinator and primary worker home directory")
    alpha_ops_pack_parser.add_argument(
        "--invite",
        default=None,
        help="Path to alpha invite JSON. Defaults to HOME parent/alpha-invite.json",
    )
    alpha_ops_pack_parser.add_argument(
        "--out",
        default=None,
        help="Ops pack directory. Defaults to HOME parent/alpha-ops-pack",
    )
    alpha_ops_pack_parser.add_argument(
        "--expected-worker-id",
        default=None,
        help="Worker node ID that should be live and return proof results",
    )
    alpha_ops_pack_parser.add_argument(
        "--include-routing-evidence",
        action="store_true",
        help="Run echo/auto/Ollama inference proof and include routing evidence",
    )
    alpha_ops_pack_parser.add_argument(
        "--jobs",
        default=25,
        type=int,
        help="Deterministic eval jobs to create for the remote proof",
    )
    alpha_ops_pack_parser.add_argument(
        "--min-live-workers",
        default=2,
        type=int,
        help="Minimum live workers required for status and proofs",
    )
    alpha_ops_pack_parser.add_argument(
        "--timeout-seconds",
        default=300.0,
        type=float,
        help="Maximum time to wait for proof runs",
    )
    alpha_ops_pack_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls during proof runs",
    )
    alpha_ops_pack_parser.add_argument(
        "--status-timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for status health and snapshot checks",
    )
    alpha_ops_pack_parser.add_argument(
        "--watchdog-report",
        default=None,
        help="Existing watchdog report to copy. Defaults to HOME parent/node-watchdog-report.json",
    )
    alpha_ops_pack_parser.add_argument(
        "--operator-task-name",
        default=DEFAULT_OPERATOR_TASK_NAME,
        help="Windows Scheduled Task name to query for operator watchdog evidence",
    )
    alpha_ops_pack_parser.add_argument(
        "--no-task-query",
        action="store_true",
        help="Skip Windows Scheduled Task query",
    )
    alpha_ops_pack_parser.add_argument(
        "--inference-mode",
        choices=("echo", "auto", "ollama"),
        default="echo",
        help="Inference proof mode when --include-routing-evidence is set",
    )
    alpha_ops_pack_parser.add_argument(
        "--inference-model",
        default=None,
        help="Ollama model to require for auto/ollama routing evidence",
    )
    alpha_ops_pack_parser.add_argument(
        "--inference-jobs",
        default=20,
        type=int,
        help="Inference jobs to create when --include-routing-evidence is set",
    )
    alpha_ops_pack_parser.add_argument(
        "--zip",
        default=None,
        help="Optional zip output path. Defaults to OUT.zip",
    )
    alpha_ops_pack_parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip zip creation and leave the ops pack as a folder only",
    )
    alpha_ops_pack_parser.set_defaults(func=alpha_ops_pack_command)

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

    alpha_network_status_parser = operator_subcommands.add_parser(
        "network-status",
        help="Check primary and backup alpha coordinator lanes from the operator machine",
    )
    alpha_network_status_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    alpha_network_status_parser.add_argument("--backup-invite", required=True, help="Path to backup alpha invite JSON")
    alpha_network_status_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID that should be live",
    )
    alpha_network_status_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID that should be live",
    )
    alpha_network_status_parser.add_argument(
        "--min-primary-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required on the primary lane",
    )
    alpha_network_status_parser.add_argument(
        "--min-backup-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required on the backup lane",
    )
    alpha_network_status_parser.add_argument(
        "--timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for each coordinator snapshot check",
    )
    alpha_network_status_parser.add_argument("--report", required=True, help="Path for network status JSON report")
    alpha_network_status_parser.set_defaults(func=alpha_network_status_command)

    alpha_failover_smoke_parser = operator_subcommands.add_parser(
        "failover-smoke",
        help="Run deterministic smoke jobs on both primary and backup alpha lanes",
    )
    alpha_failover_smoke_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    alpha_failover_smoke_parser.add_argument("--backup-invite", required=True, help="Path to backup alpha invite JSON")
    alpha_failover_smoke_parser.add_argument("--jobs", default=4, type=int, help="Deterministic eval jobs per lane")
    alpha_failover_smoke_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required on each lane",
    )
    alpha_failover_smoke_parser.add_argument(
        "--min-accepted-results",
        default=None,
        type=int,
        help="Minimum accepted results required on each lane. Defaults to --jobs",
    )
    alpha_failover_smoke_parser.add_argument(
        "--min-verified-jobs",
        default=0,
        type=int,
        help="Minimum verified jobs required on each lane",
    )
    alpha_failover_smoke_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID that should return results",
    )
    alpha_failover_smoke_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID that should return results",
    )
    alpha_failover_smoke_parser.add_argument(
        "--min-expected-primary-results",
        default=0,
        type=int,
        help="Minimum primary-lane results required from the expected primary worker",
    )
    alpha_failover_smoke_parser.add_argument(
        "--min-expected-backup-results",
        default=0,
        type=int,
        help="Minimum backup-lane results required from the expected backup worker",
    )
    alpha_failover_smoke_parser.add_argument(
        "--timeout-seconds",
        default=90.0,
        type=float,
        help="Maximum time to wait for each lane's smoke thresholds",
    )
    alpha_failover_smoke_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls",
    )
    alpha_failover_smoke_parser.add_argument("--report", required=True, help="Path for combined failover smoke report")
    alpha_failover_smoke_parser.set_defaults(func=alpha_failover_smoke_command)

    alpha_reliability_pack_parser = operator_subcommands.add_parser(
        "reliability-pack",
        help="Run primary/backup network, smoke, inference, and redaction checks into one folder",
    )
    alpha_reliability_pack_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    alpha_reliability_pack_parser.add_argument("--backup-invite", required=True, help="Path to backup alpha invite JSON")
    alpha_reliability_pack_parser.add_argument("--out", required=True, help="Output directory for reliability artifacts")
    alpha_reliability_pack_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID that should be live and return verified echo results",
    )
    alpha_reliability_pack_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID that should be live and return verified echo results",
    )
    alpha_reliability_pack_parser.add_argument(
        "--include-deterministic-smoke",
        action="store_true",
        help="Also run deterministic failover smoke. Default reliability packs use verified echo proof only.",
    )
    alpha_reliability_pack_parser.add_argument(
        "--jobs",
        default=4,
        type=int,
        help="Deterministic smoke jobs per lane when --include-deterministic-smoke is used",
    )
    alpha_reliability_pack_parser.add_argument(
        "--inference-jobs",
        default=4,
        type=int,
        help="Verified echo inference jobs per lane",
    )
    alpha_reliability_pack_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required on each lane",
    )
    alpha_reliability_pack_parser.add_argument(
        "--status-timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for network status snapshots",
    )
    alpha_reliability_pack_parser.add_argument(
        "--timeout-seconds",
        default=90.0,
        type=float,
        help="Maximum time to wait for smoke and inference thresholds",
    )
    alpha_reliability_pack_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls",
    )
    alpha_reliability_pack_parser.set_defaults(func=alpha_reliability_pack_command)

    alpha_install_reliability_task_parser = operator_subcommands.add_parser(
        "install-reliability-task",
        help="Install a Windows Scheduled Task that periodically writes a reliability pack",
    )
    alpha_install_reliability_task_parser.add_argument("--primary-invite", required=True, help="Path to primary alpha invite JSON")
    alpha_install_reliability_task_parser.add_argument("--backup-invite", required=True, help="Path to backup alpha invite JSON")
    alpha_install_reliability_task_parser.add_argument("--out", required=True, help="Output directory for reliability artifacts")
    alpha_install_reliability_task_parser.add_argument(
        "--task-name",
        default=DEFAULT_RELIABILITY_TASK_NAME,
        help="Windows Scheduled Task name",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--interval-minutes",
        default=30,
        type=int,
        help="Minutes between reliability-pack runs",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--expected-primary-worker-id",
        default=None,
        help="Primary-lane worker ID that should be live and return verified echo results",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--expected-backup-worker-id",
        default=None,
        help="Backup-lane worker ID that should be live and return verified echo results",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--include-deterministic-smoke",
        action="store_true",
        help="Also run deterministic failover smoke in each scheduled pack. Disabled by default.",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--jobs",
        default=4,
        type=int,
        help="Deterministic smoke jobs per lane when --include-deterministic-smoke is used",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--inference-jobs",
        default=4,
        type=int,
        help="Verified echo inference jobs per lane",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required on each lane",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--status-timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for network status snapshots",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--timeout-seconds",
        default=90.0,
        type=float,
        help="Maximum time to wait for smoke and inference thresholds",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--work-dir",
        default=None,
        help="Working directory for the generated launcher. Defaults to the repo root",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--launcher",
        default=None,
        help="Path for generated .cmd launcher. Defaults to OUT/run/<task-name>.cmd",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--no-force",
        action="store_true",
        help="Do not replace an existing Scheduled Task of the same name",
    )
    alpha_install_reliability_task_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the task plan without writing the launcher or creating a Scheduled Task",
    )
    alpha_install_reliability_task_parser.set_defaults(func=alpha_install_reliability_task_command)

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

    alpha_inference_proof_parser = operator_subcommands.add_parser(
        "alpha-inference-proof",
        help="Create inference-style jobs and prove alpha workers can return signed results",
    )
    alpha_inference_proof_parser.add_argument("--invite", required=True, help="Path to alpha invite JSON")
    alpha_inference_proof_parser.add_argument(
        "--jobs",
        default=10,
        type=int,
        help="Inference proof jobs to create",
    )
    alpha_inference_proof_parser.add_argument(
        "--mode",
        choices=("echo", "ollama", "auto"),
        default="echo",
        help="Inference proof mode. Auto uses Ollama only when the requested model is advertised by a live node",
    )
    alpha_inference_proof_parser.add_argument(
        "--model",
        default=None,
        help="Ollama model name for --mode ollama or optional --mode auto upgrade",
    )
    alpha_inference_proof_parser.add_argument(
        "--prompt",
        default=DEFAULT_INFERENCE_PROOF_PROMPT,
        help="Base prompt for inference proof jobs",
    )
    alpha_inference_proof_parser.add_argument(
        "--temperature",
        default=None,
        type=float,
        help="Optional Ollama temperature",
    )
    alpha_inference_proof_parser.add_argument(
        "--expected-worker-id",
        default=None,
        help="Worker node ID that must be live and should return proof results",
    )
    alpha_inference_proof_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required for pass",
    )
    alpha_inference_proof_parser.add_argument(
        "--min-accepted-results",
        default=None,
        type=int,
        help="Minimum accepted results on proof-created jobs. Defaults to jobs",
    )
    alpha_inference_proof_parser.add_argument(
        "--min-verified-jobs",
        default=None,
        type=int,
        help="Minimum verified proof-created jobs. Defaults to jobs",
    )
    alpha_inference_proof_parser.add_argument(
        "--min-expected-worker-results",
        default=None,
        type=int,
        help="Minimum proof-created results from the expected worker. Defaults to 1 when expected worker is set",
    )
    alpha_inference_proof_parser.add_argument(
        "--timeout-seconds",
        default=120.0,
        type=float,
        help="Maximum time to wait for inference proof thresholds",
    )
    alpha_inference_proof_parser.add_argument(
        "--request-timeout-seconds",
        default=30.0,
        type=float,
        help="Seconds to wait for each coordinator HTTP request",
    )
    alpha_inference_proof_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls",
    )
    alpha_inference_proof_parser.add_argument("--report", required=True, help="Path for inference proof JSON report")
    alpha_inference_proof_parser.set_defaults(func=alpha_inference_proof_command)

    alpha_soak_parser = operator_subcommands.add_parser(
        "alpha-soak",
        help="Run repeated inference proof rounds and write a reliability soak report",
    )
    alpha_soak_parser.add_argument("--invite", required=True, help="Path to alpha invite JSON")
    alpha_soak_parser.add_argument(
        "--jobs-per-round",
        default=10,
        type=int,
        help="Inference proof jobs to create in each soak round",
    )
    alpha_soak_parser.add_argument(
        "--rounds",
        default=5,
        type=int,
        help="Maximum number of soak rounds to run",
    )
    alpha_soak_parser.add_argument(
        "--duration-seconds",
        default=None,
        type=float,
        help="Optional wall-clock cap for the soak run; at least one round is attempted",
    )
    alpha_soak_parser.add_argument(
        "--round-timeout-seconds",
        default=120.0,
        type=float,
        help="Maximum time to wait for each inference proof round",
    )
    alpha_soak_parser.add_argument(
        "--round-interval-seconds",
        default=5.0,
        type=float,
        help="Seconds to wait between completed rounds",
    )
    alpha_soak_parser.add_argument(
        "--mode",
        choices=("echo", "ollama", "auto"),
        default="echo",
        help="Inference proof mode to use for every soak round",
    )
    alpha_soak_parser.add_argument(
        "--model",
        default=None,
        help="Ollama model name for --mode ollama or optional --mode auto upgrade",
    )
    alpha_soak_parser.add_argument(
        "--prompt",
        default=DEFAULT_INFERENCE_PROOF_PROMPT,
        help="Base prompt for soak inference proof jobs",
    )
    alpha_soak_parser.add_argument(
        "--temperature",
        default=None,
        type=float,
        help="Optional Ollama temperature",
    )
    alpha_soak_parser.add_argument(
        "--expected-worker-id",
        default=None,
        help="Worker node ID that should remain live and return proof results",
    )
    alpha_soak_parser.add_argument(
        "--min-live-workers",
        default=1,
        type=int,
        help="Minimum live workers required in every round",
    )
    alpha_soak_parser.add_argument(
        "--min-accepted-results-per-round",
        default=None,
        type=int,
        help="Minimum accepted results required in each round. Defaults to jobs-per-round",
    )
    alpha_soak_parser.add_argument(
        "--min-verified-jobs-per-round",
        default=None,
        type=int,
        help="Minimum verified jobs required in each round. Defaults to jobs-per-round",
    )
    alpha_soak_parser.add_argument(
        "--min-expected-worker-results-per-round",
        default=None,
        type=int,
        help="Minimum results from the expected worker in each round. Defaults to 1 when expected worker is set",
    )
    alpha_soak_parser.add_argument(
        "--min-expected-worker-results-total",
        default=None,
        type=int,
        help=(
            "Minimum total results from the expected worker across the whole soak. "
            "When set, the default per-round expected-worker requirement becomes 0 unless explicitly provided"
        ),
    )
    alpha_soak_parser.add_argument(
        "--poll-interval",
        default=0.5,
        type=float,
        help="Seconds between coordinator snapshot polls inside each round",
    )
    alpha_soak_parser.add_argument(
        "--request-timeout-seconds",
        default=30.0,
        type=float,
        help="Seconds to wait for each coordinator HTTP request inside soak rounds",
    )
    alpha_soak_parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop after the first failed soak round instead of collecting more evidence",
    )
    alpha_soak_parser.add_argument("--report", required=True, help="Path for soak JSON report")
    alpha_soak_parser.set_defaults(func=alpha_soak_command)

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
    once_parser.add_argument(
        "--ollama-timeout-seconds",
        default=300.0,
        type=float,
        help="Seconds to wait for one local Ollama inference request",
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
    loop_parser.add_argument(
        "--ollama-timeout-seconds",
        default=300.0,
        type=float,
        help="Seconds to wait for one local Ollama inference request",
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

    provider_parser = subcommands.add_parser("provider", help="Provider simulation commands")
    provider_subcommands = provider_parser.add_subparsers(dest="provider_command", required=True)

    create_subscriber_parser = provider_subcommands.add_parser(
        "create-subscriber",
        help="Add a subscriber to a provider simulation config",
    )
    create_subscriber_parser.add_argument("--config", required=True, help="Path to provider config JSON")
    create_subscriber_parser.add_argument("--subscriber-id", required=True, help="Subscriber id to create")
    create_subscriber_parser.add_argument("--plan", required=True, help="Subscriber plan name")
    create_subscriber_parser.set_defaults(func=provider_create_subscriber_command)

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

    provider_edge_parser = proof_subcommands.add_parser(
        "provider-edge",
        help="Run an ISP-edge / broadband-bundle simulation proof",
    )
    provider_edge_parser.add_argument("--provider-config", required=True, help="Path to provider config JSON")
    provider_edge_parser.add_argument("--subscribers", default=3, type=int, help="Subscribers to simulate")
    provider_edge_parser.add_argument("--edge-workers", default=1, type=int, help="Provider edge workers to simulate")
    provider_edge_parser.add_argument("--peer-workers", default=1, type=int, help="Trusted peer workers to simulate")
    provider_edge_parser.add_argument("--verifier-workers", default=1, type=int, help="Verifier workers to simulate")
    provider_edge_parser.add_argument("--jobs", default=25, type=int, help="Subscriber jobs to create")
    provider_edge_parser.add_argument(
        "--report",
        default=".mesh/proof/provider-edge-report.json",
        help="Path for provider-edge JSON report",
    )
    provider_edge_parser.add_argument(
        "--timeout-seconds",
        default=60.0,
        type=float,
        help="Maximum proof runtime before marking the run failed",
    )
    provider_edge_parser.set_defaults(func=run_proof_provider_edge)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

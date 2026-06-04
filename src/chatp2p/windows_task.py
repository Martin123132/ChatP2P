"""Windows Scheduled Task helpers for keeping ChatP2P watchdogs alive."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WINDOWS_TASK_INSTALL_REPORT_SCHEMA = "chatp2p.windows-task-install-report.v1"
WINDOWS_TASK_UNINSTALL_REPORT_SCHEMA = "chatp2p.windows-task-uninstall-report.v1"
DEFAULT_TASK_NAME = "ChatP2P Watchdog"
DEFAULT_RELIABILITY_TASK_NAME = "ChatP2P Reliability Pack"
DEFAULT_DAILY_CHECK_TASK_NAME = "ChatP2P Daily Check"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 90.0
SUPPORTED_SCHEDULES = {"onlogon": "ONLOGON", "onstart": "ONSTART"}


@dataclass(frozen=True)
class WatchdogTaskConfig:
    home: Path
    invite_path: Path
    task_name: str = DEFAULT_TASK_NAME
    report_path: Path | None = None
    role: str = "worker"
    operator_config_path: Path | None = None
    schedule: str = "onlogon"
    force: bool = True
    startup_fallback: bool = False
    restart: bool = True
    checks: int = 0
    interval_seconds: float = 30.0
    coordinator_host: str = "0.0.0.0"
    coordinator_port: int | None = None
    lease_timeout_seconds: float = 30.0
    node_stale_seconds: float = 60.0
    worker_interval: float = 0.5
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS
    cpu_duration_seconds: float = 0.25
    ollama_base_url: str = "http://127.0.0.1:11434"
    python_executable: Path | None = None
    source_root: Path | None = None
    work_dir: Path | None = None
    launcher_path: Path | None = None


@dataclass(frozen=True)
class ReliabilityTaskConfig:
    primary_invite_path: Path
    backup_invite_path: Path
    out_dir: Path
    task_name: str = DEFAULT_RELIABILITY_TASK_NAME
    interval_minutes: int = 30
    force: bool = True
    startup_fallback: bool = False
    expected_primary_worker_id: str | None = None
    expected_backup_worker_id: str | None = None
    include_deterministic_smoke: bool = False
    jobs: int = 4
    inference_jobs: int = 4
    min_live_workers: int = 1
    status_timeout_seconds: float = 5.0
    timeout_seconds: float = 90.0
    poll_interval: float = 0.5
    python_executable: Path | None = None
    source_root: Path | None = None
    work_dir: Path | None = None
    launcher_path: Path | None = None


@dataclass(frozen=True)
class DailyCheckTaskConfig:
    repo: Path
    home: Path
    primary_invite_path: Path
    out_dir: Path
    task_name: str = DEFAULT_DAILY_CHECK_TASK_NAME
    interval_minutes: int = 60
    force: bool = True
    startup_fallback: bool = False
    backup_invite_path: Path | None = None
    reliability_dir: Path | None = None
    console_out_dir: Path | None = None
    partner_report_paths: tuple[Path, ...] = ()
    expected_primary_worker_id: str | None = None
    expected_backup_worker_id: str | None = None
    skip_network_checks: bool = False
    refresh_reliability_pack: bool = False
    include_deterministic_smoke: bool = False
    jobs: int = 4
    inference_jobs: int = 4
    min_live_workers: int = 1
    status_timeout_seconds: float = 5.0
    timeout_seconds: float = 90.0
    poll_interval: float = 0.5
    freshness_seconds: float = 3600.0
    history_limit: int = 20
    stale_report_root: Path | None = None
    stale_report_days: float = 2.0
    stale_report_max_items: int = 50
    python_executable: Path | None = None
    source_root: Path | None = None
    work_dir: Path | None = None
    launcher_path: Path | None = None


def install_watchdog_task(config: WatchdogTaskConfig, *, dry_run: bool = False) -> dict[str, Any]:
    plan = build_watchdog_task_plan(config)
    report = _task_report(
        schema=WINDOWS_TASK_INSTALL_REPORT_SCHEMA,
        task_name=config.task_name,
        dry_run=dry_run,
        plan=plan,
        command=None,
        returncode=None,
        stdout=None,
        stderr=None,
    )
    if dry_run:
        return report
    if not _is_windows():
        report.update({"ok": False, "status": "unsupported", "error": "Windows Scheduled Tasks require Windows"})
        return report

    launcher_path = Path(plan["launcher_path"])
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_path.write_text(plan["launcher"], encoding="utf-8", newline="\r\n")
    result = subprocess.run(plan["create_command"], capture_output=True, text=True, timeout=30)
    report = _task_report(
        schema=WINDOWS_TASK_INSTALL_REPORT_SCHEMA,
        task_name=config.task_name,
        dry_run=False,
        plan=plan,
        command=plan["create_command"],
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if result.returncode != 0 and config.startup_fallback and _is_access_denied(result.stderr):
        fallback = _install_startup_fallback(plan)
        report["fallback"] = fallback
        if fallback["ok"]:
            report["ok"] = True
            report["status"] = "pass"
            report["install_method"] = "startup-folder"
            report["errors"] = []
    return report


def install_reliability_task(config: ReliabilityTaskConfig, *, dry_run: bool = False) -> dict[str, Any]:
    plan = build_reliability_task_plan(config)
    report = _task_report(
        schema=WINDOWS_TASK_INSTALL_REPORT_SCHEMA,
        task_name=config.task_name,
        dry_run=dry_run,
        plan=plan,
        command=None,
        returncode=None,
        stdout=None,
        stderr=None,
    )
    if dry_run:
        return report
    if not _is_windows():
        report.update({"ok": False, "status": "unsupported", "error": "Windows Scheduled Tasks require Windows"})
        return report

    launcher_path = Path(plan["launcher_path"])
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_path.write_text(plan["launcher"], encoding="utf-8", newline="\r\n")
    result = subprocess.run(plan["create_command"], capture_output=True, text=True, timeout=30)
    report = _task_report(
        schema=WINDOWS_TASK_INSTALL_REPORT_SCHEMA,
        task_name=config.task_name,
        dry_run=False,
        plan=plan,
        command=plan["create_command"],
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if result.returncode != 0 and config.startup_fallback and _is_access_denied(result.stderr):
        fallback = _install_startup_fallback(plan)
        report["fallback"] = fallback
        if fallback["ok"]:
            report["ok"] = True
            report["status"] = "pass"
            report["install_method"] = "startup-folder"
            report["errors"] = []
    return report


def install_daily_check_task(config: DailyCheckTaskConfig, *, dry_run: bool = False) -> dict[str, Any]:
    plan = build_daily_check_task_plan(config)
    report = _task_report(
        schema=WINDOWS_TASK_INSTALL_REPORT_SCHEMA,
        task_name=config.task_name,
        dry_run=dry_run,
        plan=plan,
        command=None,
        returncode=None,
        stdout=None,
        stderr=None,
    )
    if dry_run:
        return report
    if not _is_windows():
        report.update({"ok": False, "status": "unsupported", "error": "Windows Scheduled Tasks require Windows"})
        return report

    launcher_path = Path(plan["launcher_path"])
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    launcher_path.write_text(plan["launcher"], encoding="utf-8", newline="\r\n")
    result = subprocess.run(plan["create_command"], capture_output=True, text=True, timeout=30)
    report = _task_report(
        schema=WINDOWS_TASK_INSTALL_REPORT_SCHEMA,
        task_name=config.task_name,
        dry_run=False,
        plan=plan,
        command=plan["create_command"],
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if result.returncode != 0 and config.startup_fallback and _is_access_denied(result.stderr):
        fallback = _install_startup_fallback(plan)
        report["fallback"] = fallback
        if fallback["ok"]:
            report["ok"] = True
            report["status"] = "pass"
            report["install_method"] = "startup-folder"
            report["errors"] = []
    return report


def uninstall_watchdog_task(
    *,
    task_name: str = DEFAULT_TASK_NAME,
    home: Path | None = None,
    launcher_path: Path | None = None,
    delete_launcher: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not task_name.strip():
        raise ValueError("--task-name cannot be blank")
    resolved_launcher = _resolve_launcher_path(home=home, task_name=task_name, launcher_path=launcher_path)
    delete_command = ["schtasks.exe", "/Delete", "/TN", task_name, "/F"]
    plan = {
        "task_name": task_name,
        "delete_command": delete_command,
        "launcher_path": str(resolved_launcher) if resolved_launcher else None,
        "delete_launcher": delete_launcher,
        "startup_launcher_path": str(_default_startup_launcher_path(task_name)),
    }
    report = _task_report(
        schema=WINDOWS_TASK_UNINSTALL_REPORT_SCHEMA,
        task_name=task_name,
        dry_run=dry_run,
        plan=plan,
        command=None,
        returncode=None,
        stdout=None,
        stderr=None,
    )
    if dry_run:
        return report
    if not _is_windows():
        report.update({"ok": False, "status": "unsupported", "error": "Windows Scheduled Tasks require Windows"})
        return report

    result = subprocess.run(delete_command, capture_output=True, text=True, timeout=30)
    launcher_deleted = False
    startup_launcher_deleted = False
    delete_errors: list[str] = []
    if delete_launcher and resolved_launcher is not None:
        try:
            resolved_launcher.unlink(missing_ok=True)
            launcher_deleted = True
        except OSError as exc:
            delete_errors.append(f"launcher delete failed: {type(exc).__name__}: {exc}")
    if delete_launcher:
        try:
            _default_startup_launcher_path(task_name).unlink(missing_ok=True)
            startup_launcher_deleted = True
        except OSError as exc:
            delete_errors.append(f"startup launcher delete failed: {type(exc).__name__}: {exc}")
    report = _task_report(
        schema=WINDOWS_TASK_UNINSTALL_REPORT_SCHEMA,
        task_name=task_name,
        dry_run=False,
        plan=plan,
        command=delete_command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    report["launcher_deleted"] = launcher_deleted
    report["startup_launcher_deleted"] = startup_launcher_deleted
    if delete_errors:
        report["ok"] = False
        report["status"] = "fail"
        report["errors"].extend(delete_errors)
    return report


def build_reliability_task_plan(config: ReliabilityTaskConfig) -> dict[str, Any]:
    _validate_reliability_task_config(config)
    primary_invite_path = config.primary_invite_path.expanduser().resolve()
    backup_invite_path = config.backup_invite_path.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    python_executable = (config.python_executable or Path(sys.executable)).expanduser().resolve()
    source_root = (config.source_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    work_dir = (config.work_dir or source_root.parent).expanduser().resolve()
    launcher_path = (config.launcher_path or out_dir / "run" / f"{_task_slug(config.task_name)}.cmd").expanduser().resolve()
    reliability_argv = _reliability_argv(
        config=config,
        primary_invite_path=primary_invite_path,
        backup_invite_path=backup_invite_path,
        out_dir=out_dir,
        python_executable=python_executable,
    )
    launcher = _launcher_contents(
        python_executable=python_executable,
        source_root=source_root,
        work_dir=work_dir,
        watchdog_argv=reliability_argv,
    )
    task_action = subprocess.list2cmdline([_cmd_executable(), "/c", str(launcher_path)])
    create_command = [
        "schtasks.exe",
        "/Create",
        "/TN",
        config.task_name,
        "/SC",
        "MINUTE",
        "/MO",
        str(config.interval_minutes),
        "/TR",
        task_action,
    ]
    if config.force:
        create_command.append("/F")
    return {
        "task_name": config.task_name,
        "schedule": "minute",
        "interval_minutes": config.interval_minutes,
        "primary_invite_path": str(primary_invite_path),
        "backup_invite_path": str(backup_invite_path),
        "out_dir": str(out_dir),
        "expected_primary_worker_id": config.expected_primary_worker_id,
        "expected_backup_worker_id": config.expected_backup_worker_id,
        "include_deterministic_smoke": config.include_deterministic_smoke,
        "launcher_path": str(launcher_path),
        "python_executable": str(python_executable),
        "source_root": str(source_root),
        "work_dir": str(work_dir),
        "reliability_argv": reliability_argv,
        "task_action": task_action,
        "create_command": create_command,
        "startup_launcher_path": str(_default_startup_launcher_path(config.task_name)),
        "startup_fallback": bool(config.startup_fallback),
        "launcher": launcher,
    }


def build_daily_check_task_plan(config: DailyCheckTaskConfig) -> dict[str, Any]:
    _validate_daily_check_task_config(config)
    repo = config.repo.expanduser().resolve()
    home = config.home.expanduser().resolve()
    primary_invite_path = config.primary_invite_path.expanduser().resolve()
    backup_invite_path = config.backup_invite_path.expanduser().resolve() if config.backup_invite_path else None
    reliability_dir = config.reliability_dir.expanduser().resolve() if config.reliability_dir else None
    out_dir = config.out_dir.expanduser().resolve()
    console_out_dir = config.console_out_dir.expanduser().resolve() if config.console_out_dir else None
    stale_report_root = config.stale_report_root.expanduser().resolve() if config.stale_report_root else None
    partner_report_paths = tuple(path.expanduser().resolve() for path in config.partner_report_paths)
    python_executable = (config.python_executable or Path(sys.executable)).expanduser().resolve()
    source_root = (config.source_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    work_dir = (config.work_dir or source_root.parent).expanduser().resolve()
    launcher_path = (config.launcher_path or out_dir / "run" / f"{_task_slug(config.task_name)}.cmd").expanduser().resolve()
    daily_check_argv = _daily_check_argv(
        config=config,
        repo=repo,
        home=home,
        primary_invite_path=primary_invite_path,
        backup_invite_path=backup_invite_path,
        reliability_dir=reliability_dir,
        out_dir=out_dir,
        console_out_dir=console_out_dir,
        partner_report_paths=partner_report_paths,
        stale_report_root=stale_report_root,
        python_executable=python_executable,
    )
    launcher = _launcher_contents(
        python_executable=python_executable,
        source_root=source_root,
        work_dir=work_dir,
        watchdog_argv=daily_check_argv,
    )
    task_action = subprocess.list2cmdline([_cmd_executable(), "/c", str(launcher_path)])
    create_command = [
        "schtasks.exe",
        "/Create",
        "/TN",
        config.task_name,
        "/SC",
        "MINUTE",
        "/MO",
        str(config.interval_minutes),
        "/TR",
        task_action,
    ]
    if config.force:
        create_command.append("/F")
    return {
        "task_name": config.task_name,
        "schedule": "minute",
        "interval_minutes": config.interval_minutes,
        "repo": str(repo),
        "home": str(home),
        "primary_invite_path": str(primary_invite_path),
        "backup_invite_path": str(backup_invite_path) if backup_invite_path else None,
        "reliability_dir": str(reliability_dir) if reliability_dir else None,
        "out_dir": str(out_dir),
        "console_out_dir": str(console_out_dir) if console_out_dir else None,
        "expected_primary_worker_id": config.expected_primary_worker_id,
        "expected_backup_worker_id": config.expected_backup_worker_id,
        "skip_network_checks": config.skip_network_checks,
        "refresh_reliability_pack": config.refresh_reliability_pack,
        "include_deterministic_smoke": config.include_deterministic_smoke,
        "launcher_path": str(launcher_path),
        "python_executable": str(python_executable),
        "source_root": str(source_root),
        "work_dir": str(work_dir),
        "daily_check_argv": daily_check_argv,
        "task_action": task_action,
        "create_command": create_command,
        "startup_launcher_path": str(_default_startup_launcher_path(config.task_name)),
        "startup_fallback": config.startup_fallback,
        "launcher": launcher,
    }


def build_watchdog_task_plan(config: WatchdogTaskConfig) -> dict[str, Any]:
    _validate_watchdog_task_config(config)
    home = config.home.expanduser().resolve()
    invite_path = config.invite_path.expanduser().resolve()
    report_path = config.report_path.expanduser().resolve() if config.report_path else home / "run" / "watchdog-task-report.json"
    operator_config_path = (
        config.operator_config_path.expanduser().resolve() if config.operator_config_path is not None else None
    )
    python_executable = (config.python_executable or Path(sys.executable)).expanduser().resolve()
    source_root = (config.source_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    work_dir = (config.work_dir or source_root.parent).expanduser().resolve()
    launcher_path = (config.launcher_path or _default_launcher_path(home, config.task_name)).expanduser().resolve()
    watchdog_argv = _watchdog_argv(
        config=config,
        home=home,
        invite_path=invite_path,
        report_path=report_path,
        operator_config_path=operator_config_path,
        python_executable=python_executable,
    )
    launcher = _launcher_contents(
        python_executable=python_executable,
        source_root=source_root,
        work_dir=work_dir,
        watchdog_argv=watchdog_argv,
    )
    task_action = subprocess.list2cmdline([_cmd_executable(), "/c", str(launcher_path)])
    create_command = [
        "schtasks.exe",
        "/Create",
        "/TN",
        config.task_name,
        "/SC",
        SUPPORTED_SCHEDULES[config.schedule.lower()],
        "/TR",
        task_action,
    ]
    if config.force:
        create_command.append("/F")
    return {
        "task_name": config.task_name,
        "schedule": config.schedule.lower(),
        "home": str(home),
        "invite_path": str(invite_path),
        "report_path": str(report_path),
        "operator_config_path": str(operator_config_path) if operator_config_path else None,
        "launcher_path": str(launcher_path),
        "python_executable": str(python_executable),
        "source_root": str(source_root),
        "work_dir": str(work_dir),
        "watchdog_argv": watchdog_argv,
        "task_action": task_action,
        "create_command": create_command,
        "startup_launcher_path": str(_default_startup_launcher_path(config.task_name)),
        "startup_fallback": config.startup_fallback,
        "launcher": launcher,
    }


def _validate_watchdog_task_config(config: WatchdogTaskConfig) -> None:
    if not config.task_name.strip():
        raise ValueError("--task-name cannot be blank")
    if config.role not in {"both", "coordinator", "worker"}:
        raise ValueError("--role must be both, coordinator, or worker")
    if config.schedule.lower() not in SUPPORTED_SCHEDULES:
        raise ValueError("--schedule must be onlogon or onstart")
    if config.role in {"both", "coordinator"} and config.operator_config_path is None:
        raise ValueError("--operator-config is required when installing a coordinator watchdog task")
    if config.checks < 0:
        raise ValueError("--checks cannot be negative")
    if config.interval_seconds <= 0:
        raise ValueError("--interval-seconds must be greater than 0")
    if config.worker_interval <= 0:
        raise ValueError("--worker-interval must be greater than 0")
    if config.startup_timeout_seconds <= 0:
        raise ValueError("--startup-timeout-seconds must be greater than 0")
    if config.cpu_duration_seconds < 0:
        raise ValueError("--cpu-duration-seconds cannot be negative")
    if config.coordinator_port is not None and not 1 <= config.coordinator_port <= 65535:
        raise ValueError("--coordinator-port must be between 1 and 65535")
    if config.lease_timeout_seconds <= 0:
        raise ValueError("--lease-timeout-seconds must be greater than 0")
    if config.node_stale_seconds <= 0:
        raise ValueError("--node-stale-seconds must be greater than 0")


def _validate_reliability_task_config(config: ReliabilityTaskConfig) -> None:
    if not config.task_name.strip():
        raise ValueError("--task-name cannot be blank")
    if config.interval_minutes < 1:
        raise ValueError("--interval-minutes must be at least 1")
    if config.include_deterministic_smoke and config.jobs < 1:
        raise ValueError("--jobs must be at least 1 when deterministic smoke is enabled")
    if config.inference_jobs < 1:
        raise ValueError("--inference-jobs must be at least 1")
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.status_timeout_seconds <= 0:
        raise ValueError("--status-timeout-seconds must be greater than 0")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.expected_primary_worker_id is not None and not config.expected_primary_worker_id.strip():
        raise ValueError("--expected-primary-worker-id cannot be blank")
    if config.expected_backup_worker_id is not None and not config.expected_backup_worker_id.strip():
        raise ValueError("--expected-backup-worker-id cannot be blank")


def _validate_daily_check_task_config(config: DailyCheckTaskConfig) -> None:
    if not config.task_name.strip():
        raise ValueError("--task-name cannot be blank")
    if config.interval_minutes < 1:
        raise ValueError("--interval-minutes must be at least 1")
    if config.refresh_reliability_pack and (config.backup_invite_path is None or config.reliability_dir is None):
        raise ValueError("--backup-invite and --reliability-dir are required with --refresh-reliability-pack")
    if config.include_deterministic_smoke and config.jobs < 1:
        raise ValueError("--jobs must be at least 1 when deterministic smoke is enabled")
    if config.inference_jobs < 1:
        raise ValueError("--inference-jobs must be at least 1")
    if config.min_live_workers < 0:
        raise ValueError("--min-live-workers cannot be negative")
    if config.status_timeout_seconds <= 0:
        raise ValueError("--status-timeout-seconds must be greater than 0")
    if config.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0")
    if config.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than 0")
    if config.freshness_seconds <= 0:
        raise ValueError("--freshness-seconds must be greater than 0")
    if config.history_limit < 1:
        raise ValueError("--history-limit must be at least 1")
    if config.stale_report_days <= 0:
        raise ValueError("--stale-report-days must be greater than 0")
    if config.stale_report_max_items < 0:
        raise ValueError("--stale-report-max-items cannot be negative")
    if config.expected_primary_worker_id is not None and not config.expected_primary_worker_id.strip():
        raise ValueError("--expected-primary-worker-id cannot be blank")
    if config.expected_backup_worker_id is not None and not config.expected_backup_worker_id.strip():
        raise ValueError("--expected-backup-worker-id cannot be blank")


def _watchdog_argv(
    *,
    config: WatchdogTaskConfig,
    home: Path,
    invite_path: Path,
    report_path: Path,
    operator_config_path: Path | None,
    python_executable: Path,
) -> list[str]:
    argv = [
        str(python_executable),
        "-m",
        "chatp2p.cli",
        "node",
        "watchdog",
        "--home",
        str(home),
        "--invite",
        str(invite_path),
        "--report",
        str(report_path),
        "--role",
        config.role,
        "--checks",
        str(config.checks),
        "--interval-seconds",
        str(config.interval_seconds),
        "--coordinator-host",
        config.coordinator_host,
        "--lease-timeout-seconds",
        str(config.lease_timeout_seconds),
        "--node-stale-seconds",
        str(config.node_stale_seconds),
        "--worker-interval",
        str(config.worker_interval),
        "--startup-timeout-seconds",
        str(config.startup_timeout_seconds),
        "--cpu-duration-seconds",
        str(config.cpu_duration_seconds),
        "--ollama-base-url",
        config.ollama_base_url,
    ]
    if operator_config_path is not None:
        argv.extend(["--operator-config", str(operator_config_path)])
    if config.coordinator_port is not None:
        argv.extend(["--coordinator-port", str(config.coordinator_port)])
    if not config.restart:
        argv.append("--no-restart")
    return argv


def _reliability_argv(
    *,
    config: ReliabilityTaskConfig,
    primary_invite_path: Path,
    backup_invite_path: Path,
    out_dir: Path,
    python_executable: Path,
) -> list[str]:
    argv = [
        str(python_executable),
        "-m",
        "chatp2p.cli",
        "operator",
        "reliability-pack",
        "--primary-invite",
        str(primary_invite_path),
        "--backup-invite",
        str(backup_invite_path),
        "--out",
        str(out_dir),
        "--jobs",
        str(config.jobs),
        "--inference-jobs",
        str(config.inference_jobs),
        "--min-live-workers",
        str(config.min_live_workers),
        "--status-timeout-seconds",
        str(config.status_timeout_seconds),
        "--timeout-seconds",
        str(config.timeout_seconds),
        "--poll-interval",
        str(config.poll_interval),
    ]
    if config.expected_primary_worker_id is not None:
        argv.extend(["--expected-primary-worker-id", config.expected_primary_worker_id])
    if config.expected_backup_worker_id is not None:
        argv.extend(["--expected-backup-worker-id", config.expected_backup_worker_id])
    if config.include_deterministic_smoke:
        argv.append("--include-deterministic-smoke")
    return argv


def _daily_check_argv(
    *,
    config: DailyCheckTaskConfig,
    repo: Path,
    home: Path,
    primary_invite_path: Path,
    backup_invite_path: Path | None,
    reliability_dir: Path | None,
    out_dir: Path,
    console_out_dir: Path | None,
    partner_report_paths: tuple[Path, ...],
    stale_report_root: Path | None,
    python_executable: Path,
) -> list[str]:
    argv = [
        str(python_executable),
        "-m",
        "chatp2p.cli",
        "operator",
        "daily-check",
        "--repo",
        str(repo),
        "--home",
        str(home),
        "--primary-invite",
        str(primary_invite_path),
        "--out",
        str(out_dir),
        "--jobs",
        str(config.jobs),
        "--inference-jobs",
        str(config.inference_jobs),
        "--min-live-workers",
        str(config.min_live_workers),
        "--status-timeout-seconds",
        str(config.status_timeout_seconds),
        "--timeout-seconds",
        str(config.timeout_seconds),
        "--poll-interval",
        str(config.poll_interval),
        "--freshness-seconds",
        str(config.freshness_seconds),
        "--history-limit",
        str(config.history_limit),
        "--stale-report-days",
        str(config.stale_report_days),
        "--stale-report-max-items",
        str(config.stale_report_max_items),
    ]
    if backup_invite_path is not None:
        argv.extend(["--backup-invite", str(backup_invite_path)])
    if reliability_dir is not None:
        argv.extend(["--reliability-dir", str(reliability_dir)])
    if console_out_dir is not None:
        argv.extend(["--console-out", str(console_out_dir)])
    for partner_report_path in partner_report_paths:
        argv.extend(["--partner-report", str(partner_report_path)])
    if config.expected_primary_worker_id is not None:
        argv.extend(["--expected-primary-worker-id", config.expected_primary_worker_id])
    if config.expected_backup_worker_id is not None:
        argv.extend(["--expected-backup-worker-id", config.expected_backup_worker_id])
    if stale_report_root is not None:
        argv.extend(["--stale-report-root", str(stale_report_root)])
    if config.skip_network_checks:
        argv.append("--skip-network-checks")
    if config.refresh_reliability_pack:
        argv.append("--refresh-reliability-pack")
    if config.include_deterministic_smoke:
        argv.append("--include-deterministic-smoke")
    return argv


def _launcher_contents(
    *,
    python_executable: Path,
    source_root: Path,
    work_dir: Path,
    watchdog_argv: list[str],
) -> str:
    command_line = subprocess.list2cmdline(watchdog_argv)
    return "\n".join(
        [
            "@echo off",
            "setlocal",
            f'cd /d "{work_dir}"',
            f'set "PYTHONPATH={source_root};%PYTHONPATH%"',
            command_line,
            "exit /b %ERRORLEVEL%",
            "",
        ]
    )


def _task_report(
    *,
    schema: str,
    task_name: str,
    dry_run: bool,
    plan: dict[str, Any],
    command: list[str] | None,
    returncode: int | None,
    stdout: str | None,
    stderr: str | None,
) -> dict[str, Any]:
    ok = returncode in {None, 0}
    return {
        "schema": schema,
        "ok": ok,
        "status": "pass" if ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "task_name": task_name,
        "plan": _public_plan(plan),
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "errors": [] if ok else ["schtasks.exe returned a non-zero exit code"],
    }


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    public = dict(plan)
    public.pop("launcher", None)
    return public


def _resolve_launcher_path(
    *,
    home: Path | None,
    task_name: str,
    launcher_path: Path | None,
) -> Path | None:
    if launcher_path is not None:
        return launcher_path.expanduser().resolve()
    if home is None:
        return None
    return _default_launcher_path(home.expanduser().resolve(), task_name)


def _default_launcher_path(home: Path, task_name: str) -> Path:
    return home / "run" / f"{_task_slug(task_name)}.cmd"


def _task_slug(task_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_name.strip()).strip("-").lower()
    return slug or "chatp2p-watchdog"


def _cmd_executable() -> str:
    return os.environ.get("ComSpec") or "cmd.exe"


def _is_windows() -> bool:
    return os.name == "nt"


def _is_access_denied(stderr: str | None) -> bool:
    return "access is denied" in (stderr or "").lower()


def _install_startup_fallback(plan: dict[str, Any]) -> dict[str, Any]:
    startup_launcher_path = Path(plan["startup_launcher_path"])
    startup_launcher_path.parent.mkdir(parents=True, exist_ok=True)
    startup_launcher_path.write_text(
        _startup_vbs_contents(Path(plan["launcher_path"])),
        encoding="utf-8",
        newline="\r\n",
    )
    return {
        "ok": True,
        "method": "startup-folder",
        "startup_launcher_path": str(startup_launcher_path),
        "message": "Scheduled Task creation was denied, so a per-user Startup folder launcher was installed.",
    }


def _startup_vbs_contents(launcher_path: Path) -> str:
    escaped = str(launcher_path).replace('"', '""')
    return "\n".join(
        [
            'Set shell = CreateObject("WScript.Shell")',
            f'shell.Run """{escaped}""", 0, False',
            "",
        ]
    )


def _default_startup_launcher_path(task_name: str) -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        startup_dir = (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
    else:
        startup_dir = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup_dir / f"{_task_slug(task_name)}.vbs"

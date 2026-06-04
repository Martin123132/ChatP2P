import builtins
import json
import sys
from pathlib import Path

import chatp2p.cli as cli_module
import pytest
from chatp2p.alpha import AlphaInvite, write_alpha_invite
from chatp2p.cli import _node_status_connection_from_args, build_parser
from chatp2p.node_runtime import (
    default_coordinator_url,
    managed_process_status,
    process_alive,
    redact_command_args,
    start_managed_process,
    stop_managed_process,
)


def test_managed_process_lifecycle(tmp_path):
    home = tmp_path / ".mesh"
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]

    result = start_managed_process(
        home=home,
        role="worker",
        argv=argv,
        coordinator_url="http://127.0.0.1:8765",
    )
    pid = result["state"]["pid"]

    try:
        assert result["status"] == "started"
        assert process_alive(pid)
        status = managed_process_status(home=home, role="worker")
        assert status["managed"] is True
        assert status["alive"] is True
        assert status["coordinator"] == "http://127.0.0.1:8765"

        duplicate = start_managed_process(
            home=home,
            role="worker",
            argv=argv,
            coordinator_url="http://127.0.0.1:8765",
        )
        assert duplicate["status"] == "already_running"
        assert duplicate["state"]["pid"] == pid
    finally:
        stopped = stop_managed_process(home=home, role="worker", timeout_seconds=5)

    assert stopped["status"] == "stopped"
    assert stopped["alive"] is False
    assert managed_process_status(home=home, role="worker")["managed"] is False


def test_managed_process_helpers_redact_and_derive_url():
    assert default_coordinator_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"
    assert default_coordinator_url("::1", 8765) == "http://[::1]:8765"
    assert redact_command_args(["worker", "--admission-token", "secret", "--interval", "5"]) == [
        "worker",
        "--admission-token",
        "<redacted>",
        "--interval",
        "5",
    ]
    assert redact_command_args(["worker", "--admission-token=secret"]) == [
        "worker",
        "--admission-token=<redacted>",
    ]


def test_node_managed_commands_parse(tmp_path):
    parser = build_parser()
    up_args = parser.parse_args(
        [
            "node",
            "up",
            "--home",
            str(tmp_path / ".mesh"),
            "--role",
            "worker",
            "--coordinator",
            "http://127.0.0.1:8765",
        ]
    )
    status_args = parser.parse_args(["node", "status", "--home", str(tmp_path / ".mesh")])
    watchdog_args = parser.parse_args(["node", "watchdog", "--home", str(tmp_path / ".mesh")])
    install_task_args = parser.parse_args(["node", "install-task", "--home", str(tmp_path / ".mesh")])
    uninstall_task_args = parser.parse_args(["node", "uninstall-task"])
    down_args = parser.parse_args(["node", "down", "--home", str(tmp_path / ".mesh")])

    assert up_args.func.__name__ == "run_node_up_command"
    assert status_args.func.__name__ == "run_node_status_command"
    assert watchdog_args.func.__name__ == "run_node_watchdog_command"
    assert install_task_args.func.__name__ == "run_node_install_task_command"
    assert uninstall_task_args.func.__name__ == "run_node_uninstall_task_command"
    assert down_args.func.__name__ == "run_node_down_command"


def test_operator_reliability_task_command_parse(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "install-reliability-task",
            "--primary-invite",
            str(tmp_path / "primary-invite.json"),
            "--backup-invite",
            str(tmp_path / "backup-invite.json"),
            "--out",
            str(tmp_path / "reliability-pack"),
            "--interval-minutes",
            "15",
            "--include-deterministic-smoke",
            "--dry-run",
        ]
    )

    assert args.func.__name__ == "alpha_install_reliability_task_command"
    assert args.interval_minutes == 15
    assert args.include_deterministic_smoke is True
    assert args.dry_run is True


def test_operator_daily_check_task_command_parse(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "install-daily-check-task",
            "--repo",
            str(tmp_path / "ChatP2P"),
            "--home",
            str(tmp_path / ".mesh"),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--backup-invite",
            str(tmp_path / "backup-invite.json"),
            "--reliability-dir",
            str(tmp_path / "reliability-pack"),
            "--out",
            str(tmp_path / "daily-check"),
            "--console-out",
            str(tmp_path / "operator-console"),
            "--interval-minutes",
            "45",
            "--allow-startup-folder-fallback",
            "--dry-run",
        ]
    )

    assert args.func.__name__ == "operator_install_daily_check_task_command"
    assert args.interval_minutes == 45
    assert args.allow_startup_folder_fallback is True
    assert args.dry_run is True


def test_operator_uninstall_daily_check_task_command_parse(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "uninstall-daily-check-task",
            "--home",
            str(tmp_path / ".mesh"),
            "--task-name",
            "ChatP2P Daily Check",
            "--launcher",
            str(tmp_path / "chatp2p-daily-check.cmd"),
            "--keep-launcher",
            "--dry-run",
        ]
    )

    assert args.func.__name__ == "operator_uninstall_daily_check_task_command"
    assert args.home == str(tmp_path / ".mesh")
    assert args.task_name == "ChatP2P Daily Check"
    assert args.launcher == str(tmp_path / "chatp2p-daily-check.cmd")
    assert args.keep_launcher is True
    assert args.dry_run is True


def test_operator_uninstall_reliability_task_command_parse(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "uninstall-reliability-task",
            "--home",
            str(tmp_path / ".mesh"),
            "--task-name",
            "ChatP2P Reliability Pack",
            "--launcher",
            str(tmp_path / "chatp2p-reliability-pack.cmd"),
            "--keep-launcher",
            "--dry-run",
        ]
    )

    assert args.func.__name__ == "operator_uninstall_reliability_task_command"
    assert args.home == str(tmp_path / ".mesh")
    assert args.task_name == "ChatP2P Reliability Pack"
    assert args.launcher == str(tmp_path / "chatp2p-reliability-pack.cmd")
    assert args.keep_launcher is True
    assert args.dry_run is True


def test_operator_uninstall_reliability_task_command_invokes_uninstall_watchdog(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "uninstall-reliability-task",
            "--home",
            str(tmp_path / ".mesh"),
            "--task-name",
            "ChatP2P Reliability Pack",
            "--launcher",
            str(tmp_path / "chatp2p-reliability-pack.cmd"),
            "--keep-launcher",
            "--dry-run",
        ]
    )

    captured = {}

    def fake_uninstall_watchdog_task(
        *,
        task_name: str,
        home: Path,
        launcher_path: Path | None,
        delete_launcher: bool,
        dry_run: bool,
    ) -> dict:
        captured["task_name"] = task_name
        captured["home"] = home
        captured["launcher_path"] = launcher_path
        captured["delete_launcher"] = delete_launcher
        captured["dry_run"] = dry_run
        return {
            "schema": "chatp2p.windows-task-uninstall-report.v1",
            "ok": True,
            "status": "pass",
            "dry_run": dry_run,
            "task_name": task_name,
            "plan": {},
            "command": None,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "errors": [],
        }

    monkeypatch.setattr(cli_module, "uninstall_watchdog_task", fake_uninstall_watchdog_task)
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: None)

    cli_module.operator_uninstall_reliability_task_command(args)

    assert captured["task_name"] == "ChatP2P Reliability Pack"
    assert captured["home"] == tmp_path / ".mesh"
    assert captured["launcher_path"] == tmp_path / "chatp2p-reliability-pack.cmd"
    assert captured["delete_launcher"] is False
    assert captured["dry_run"] is True


def test_operator_uninstall_reliability_task_command_raises_on_report_failure(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "uninstall-reliability-task",
            "--home",
            str(tmp_path / ".mesh"),
            "--task-name",
            "ChatP2P Reliability Pack",
        ]
    )

    def fake_uninstall_watchdog_task(
        *,
        task_name: str,
        home: Path,
        launcher_path: Path | None,
        delete_launcher: bool,
        dry_run: bool,
    ) -> dict:
        return {
            "schema": "chatp2p.windows-task-uninstall-report.v1",
            "ok": False,
            "status": "fail",
            "dry_run": dry_run,
            "task_name": task_name,
            "plan": {},
            "command": None,
            "returncode": 1,
            "stdout": "",
            "stderr": "nope",
            "errors": ["nope"],
        }

    monkeypatch.setattr(cli_module, "uninstall_watchdog_task", fake_uninstall_watchdog_task)
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit):
        cli_module.operator_uninstall_reliability_task_command(args)


def test_operator_uninstall_daily_check_task_command_invokes_uninstall_watchdog(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "uninstall-daily-check-task",
            "--home",
            str(tmp_path / ".mesh"),
            "--task-name",
            "ChatP2P Daily Check",
            "--launcher",
            str(tmp_path / "chatp2p-daily-check.cmd"),
            "--keep-launcher",
            "--dry-run",
        ]
    )

    captured = {}

    def fake_uninstall_watchdog_task(
        *,
        task_name: str,
        home: Path,
        launcher_path: Path | None,
        delete_launcher: bool,
        dry_run: bool,
    ) -> dict:
        captured["task_name"] = task_name
        captured["home"] = home
        captured["launcher_path"] = launcher_path
        captured["delete_launcher"] = delete_launcher
        captured["dry_run"] = dry_run
        return {
            "schema": "chatp2p.windows-task-uninstall-report.v1",
            "ok": True,
            "status": "pass",
            "dry_run": dry_run,
            "task_name": task_name,
            "plan": {},
            "command": None,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "errors": [],
        }

    monkeypatch.setattr(cli_module, "uninstall_watchdog_task", fake_uninstall_watchdog_task)
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: None)

    cli_module.operator_uninstall_daily_check_task_command(args)

    assert captured["task_name"] == "ChatP2P Daily Check"
    assert captured["home"] == tmp_path / ".mesh"
    assert captured["launcher_path"] == tmp_path / "chatp2p-daily-check.cmd"
    assert captured["delete_launcher"] is False
    assert captured["dry_run"] is True


def test_operator_uninstall_daily_check_task_command_raises_on_report_failure(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "uninstall-daily-check-task",
            "--home",
            str(tmp_path / ".mesh"),
            "--task-name",
            "ChatP2P Daily Check",
        ]
    )

    def fake_uninstall_watchdog_task(
        *,
        task_name: str,
        home: Path,
        launcher_path: Path | None,
        delete_launcher: bool,
        dry_run: bool,
    ) -> dict:
        return {
            "schema": "chatp2p.windows-task-uninstall-report.v1",
            "ok": False,
            "status": "fail",
            "dry_run": dry_run,
            "task_name": task_name,
            "plan": {},
            "command": None,
            "returncode": 1,
            "stdout": "",
            "stderr": "nope",
            "errors": ["nope"],
        }

    monkeypatch.setattr(cli_module, "uninstall_watchdog_task", fake_uninstall_watchdog_task)
    monkeypatch.setattr(builtins, "print", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit):
        cli_module.operator_uninstall_daily_check_task_command(args)


def test_operator_pause_command_parse(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "pause",
            "--home",
            str(tmp_path / ".mesh"),
            "--daily-task-name",
            "ChatP2P Daily Check",
            "--reliability-task-name",
            "ChatP2P Reliability Pack",
            "--daily-launcher",
            str(tmp_path / "chatp2p-daily-check.cmd"),
            "--reliability-launcher",
            str(tmp_path / "chatp2p-reliability-pack.cmd"),
            "--keep-launcher",
            "--dry-run",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_pause_command"
    assert args.home == str(tmp_path / ".mesh")
    assert args.daily_task_name == "ChatP2P Daily Check"
    assert args.reliability_task_name == "ChatP2P Reliability Pack"
    assert args.daily_launcher == str(tmp_path / "chatp2p-daily-check.cmd")
    assert args.reliability_launcher == str(tmp_path / "chatp2p-reliability-pack.cmd")
    assert args.keep_launcher is True
    assert args.dry_run is True
    assert args.json is True


def test_operator_pause_command_invokes_watchdog_uninstall_for_both_tasks(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "pause",
            "--home",
            str(tmp_path / ".mesh"),
            "--daily-task-name",
            "ChatP2P Daily Check",
            "--reliability-task-name",
            "ChatP2P Reliability Pack",
            "--daily-launcher",
            str(tmp_path / "chatp2p-daily-check.cmd"),
            "--reliability-launcher",
            str(tmp_path / "chatp2p-reliability-pack.cmd"),
            "--keep-launcher",
            "--dry-run",
        ]
    )

    calls = []

    def fake_uninstall_watchdog_task(
        *,
        task_name: str,
        home: Path,
        launcher_path: Path | None,
        delete_launcher: bool,
        dry_run: bool,
    ) -> dict:
        calls.append((task_name, home, launcher_path, delete_launcher, dry_run))
        return {
            "schema": "chatp2p.windows-task-uninstall-report.v1",
            "ok": True,
            "status": "pass",
            "dry_run": dry_run,
            "task_name": task_name,
            "plan": {},
            "command": None,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "errors": [],
        }

    monkeypatch.setattr(cli_module, "uninstall_watchdog_task", fake_uninstall_watchdog_task)
    monkeypatch.setattr(builtins, "print", lambda *_, **__: None)

    cli_module.operator_pause_command(args)

    assert len(calls) == 2
    assert calls[0][:4] == (
        "ChatP2P Daily Check",
        tmp_path / ".mesh",
        tmp_path / "chatp2p-daily-check.cmd",
        False,
    )
    assert calls[1][:4] == (
        "ChatP2P Reliability Pack",
        tmp_path / ".mesh",
        tmp_path / "chatp2p-reliability-pack.cmd",
        False,
    )
    assert calls[0][4] is True and calls[1][4] is True


def test_operator_pause_command_tolerates_missing_task_errors(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "pause",
            "--home",
            str(tmp_path / ".mesh"),
            "--json",
        ]
    )

    reported = []

    def fake_uninstall_watchdog_task(
        *,
        task_name: str,
        home: Path,
        launcher_path: Path | None,
        delete_launcher: bool,
        dry_run: bool,
    ) -> dict:
        reported.append(task_name)
        if task_name == "ChatP2P Daily Check":
            return {
                "schema": "chatp2p.windows-task-uninstall-report.v1",
                "ok": False,
                "status": "fail",
                "dry_run": dry_run,
                "task_name": task_name,
                "plan": {},
                "command": None,
                "returncode": 1,
                "stdout": "",
                "stderr": "The system cannot find the file specified.",
                "errors": ["The system cannot find the file specified."],
        }
        return {
            "schema": "chatp2p.windows-task-uninstall-report.v1",
            "ok": False,
            "status": "fail",
            "dry_run": dry_run,
            "task_name": task_name,
            "plan": {},
            "command": None,
            "returncode": 1,
            "stdout": "",
            "stderr": "The specified task name \"chatp2p-reliability-pack\" was not found.",
            "errors": ["The specified task name \"chatp2p-reliability-pack\" was not found."],
        }

    rendered = []

    def fake_print(*a, **k):
        if a:
            rendered.append(str(a[0]))

    monkeypatch.setattr(cli_module, "uninstall_watchdog_task", fake_uninstall_watchdog_task)
    monkeypatch.setattr(builtins, "print", fake_print)

    cli_module.operator_pause_command(args)

    assert set(reported) == {"ChatP2P Daily Check", "ChatP2P Reliability Pack"}
    report = json.loads("".join(rendered))
    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["ignore_missing"] is True
    assert report["steps"][0]["report"]["status"] == "warn"
    assert report["steps"][0]["report"]["ok"] is True
    assert any("already paused" in w for w in report["steps"][0]["report"]["warnings"])
    assert report["steps"][1]["report"]["status"] == "warn"
    assert report["steps"][1]["report"]["ok"] is True


def test_operator_pause_command_raises_on_non_missing_failure(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "pause",
            "--home",
            str(tmp_path / ".mesh"),
        ]
    )

    def fake_uninstall_watchdog_task(
        *,
        task_name: str,
        home: Path,
        launcher_path: Path | None,
        delete_launcher: bool,
        dry_run: bool,
    ) -> dict:
        return {
            "schema": "chatp2p.windows-task-uninstall-report.v1",
            "ok": False,
            "status": "fail",
            "dry_run": dry_run,
            "task_name": task_name,
            "plan": {},
            "command": None,
            "returncode": 1,
            "stdout": "",
            "stderr": "nope",
            "errors": ["nope"],
        }

    monkeypatch.setattr(cli_module, "uninstall_watchdog_task", fake_uninstall_watchdog_task)
    monkeypatch.setattr(builtins, "print", lambda *_, **__: None)

    with pytest.raises(SystemExit):
        cli_module.operator_pause_command(args)


def test_operator_resume_command_parse(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "resume",
            "--repo",
            str(tmp_path / "ChatP2P"),
            "--home",
            str(tmp_path / ".mesh"),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--backup-invite",
            str(tmp_path / "backup-alpha-invite.json"),
            "--out-root",
            str(tmp_path / "ChatP2PData"),
            "--daily-interval-minutes",
            "45",
            "--reliability-interval-minutes",
            "20",
            "--expected-primary-worker-id",
            "worker_PRIMARY",
            "--expected-backup-worker-id",
            "worker_BACKUP",
            "--skip-network-checks",
            "--allow-startup-folder-fallback",
            "--dry-run",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_resume_command"
    assert args.daily_interval_minutes == 45
    assert args.reliability_interval_minutes == 20
    assert args.allow_startup_folder_fallback is True
    assert args.skip_network_checks is True
    assert args.dry_run is True
    assert args.json is True


def test_operator_resume_installs_both_tasks_with_derived_dirs(monkeypatch, tmp_path):
    parser = build_parser()
    out_root = tmp_path / "ChatP2PData"
    args = parser.parse_args(
        [
            "operator",
            "resume",
            "--repo",
            str(tmp_path / "ChatP2P"),
            "--home",
            str(out_root / ".mesh"),
            "--primary-invite",
            str(out_root / "alpha-invite.json"),
            "--backup-invite",
            str(out_root / "backup-alpha-invite.json"),
            "--out-root",
            str(out_root),
            "--report",
            str(out_root / "operator-resume-report.json"),
            "--expected-primary-worker-id",
            "worker_PRIMARY",
            "--expected-backup-worker-id",
            "worker_BACKUP",
            "--skip-network-checks",
            "--allow-startup-folder-fallback",
            "--json",
        ]
    )

    captured: dict[str, object] = {}

    def fake_daily(config, *, dry_run: bool) -> dict:
        captured["daily_config"] = config
        captured["daily_dry_run"] = dry_run
        return {"ok": True, "status": "pass", "errors": [], "plan": {"task_name": config.task_name}}

    def fake_reliability(config, *, dry_run: bool) -> dict:
        captured["reliability_config"] = config
        captured["reliability_dry_run"] = dry_run
        return {"ok": True, "status": "pass", "errors": [], "plan": {"task_name": config.task_name}}

    rendered = []
    monkeypatch.setattr(cli_module, "install_daily_check_task", fake_daily)
    monkeypatch.setattr(cli_module, "install_reliability_task", fake_reliability)
    monkeypatch.setattr(builtins, "print", lambda *a, **k: rendered.append(str(a[0])) if a else None)

    cli_module.operator_resume_command(args)

    report = json.loads("".join(rendered))
    file_report = json.loads((out_root / "operator-resume-report.json").read_text(encoding="utf-8"))
    daily_config = captured["daily_config"]
    reliability_config = captured["reliability_config"]
    assert report["schema"] == "chatp2p.operator-resume-report.v1"
    assert file_report["schema"] == "chatp2p.operator-resume-report.v1"
    assert report["status"] == "pass"
    assert report["ok"] is True
    assert daily_config.out_dir == (out_root / "daily-check").resolve()
    assert daily_config.console_out_dir == (out_root / "operator-console").resolve()
    assert daily_config.reliability_dir == (out_root / "reliability-pack-live").resolve()
    assert daily_config.skip_network_checks is True
    assert daily_config.startup_fallback is True
    assert reliability_config.out_dir == (out_root / "reliability-pack-live").resolve()
    assert reliability_config.startup_fallback is True
    assert reliability_config.include_deterministic_smoke is False
    assert captured["daily_dry_run"] is False
    assert captured["reliability_dry_run"] is False
    assert "operator maintenance" in report["recommended_next_command"]
    assert "admission_token" not in json.dumps(report)
    assert "private_key" not in json.dumps(report)


def test_operator_resume_dry_run_and_skip_steps(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "resume",
            "--repo",
            str(tmp_path / "ChatP2P"),
            "--home",
            str(tmp_path / ".mesh"),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out-root",
            str(tmp_path / "ChatP2PData"),
            "--skip-reliability-task",
            "--dry-run",
            "--json",
        ]
    )

    captured = {}

    def fake_daily(config, *, dry_run: bool) -> dict:
        captured["daily_dry_run"] = dry_run
        return {"ok": True, "status": "pass", "errors": [], "plan": {}}

    def fail_reliability(*args, **kwargs):
        raise AssertionError("reliability installer should be skipped")

    rendered = []
    monkeypatch.setattr(cli_module, "install_daily_check_task", fake_daily)
    monkeypatch.setattr(cli_module, "install_reliability_task", fail_reliability)
    monkeypatch.setattr(builtins, "print", lambda *a, **k: rendered.append(str(a[0])) if a else None)

    cli_module.operator_resume_command(args)

    report = json.loads("".join(rendered))
    assert captured["daily_dry_run"] is True
    assert report["steps"][0]["name"] == "daily_check"
    assert report["steps"][1]["name"] == "reliability_pack"
    assert report["steps"][1]["status"] == "skipped"


def test_operator_resume_requires_backup_unless_reliability_skipped(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "resume",
            "--repo",
            str(tmp_path / "ChatP2P"),
            "--home",
            str(tmp_path / ".mesh"),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out-root",
            str(tmp_path / "ChatP2PData"),
        ]
    )

    with pytest.raises(SystemExit, match="backup-invite"):
        cli_module.operator_resume_command(args)


def test_operator_resume_raises_on_install_failure(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "resume",
            "--repo",
            str(tmp_path / "ChatP2P"),
            "--home",
            str(tmp_path / ".mesh"),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--backup-invite",
            str(tmp_path / "backup-alpha-invite.json"),
            "--out-root",
            str(tmp_path / "ChatP2PData"),
            "--json",
        ]
    )

    def fake_daily(config, *, dry_run: bool) -> dict:
        return {"ok": False, "status": "fail", "errors": ["daily failed"], "plan": {}}

    def fake_reliability(config, *, dry_run: bool) -> dict:
        return {"ok": True, "status": "pass", "errors": [], "plan": {}}

    rendered = []
    monkeypatch.setattr(cli_module, "install_daily_check_task", fake_daily)
    monkeypatch.setattr(cli_module, "install_reliability_task", fake_reliability)
    monkeypatch.setattr(builtins, "print", lambda *a, **k: rendered.append(str(a[0])) if a else None)

    with pytest.raises(SystemExit):
        cli_module.operator_resume_command(args)

    report = json.loads("".join(rendered))
    assert report["status"] == "fail"
    assert "daily failed" in report["errors"]


def test_node_status_can_derive_coordinator_from_invite(tmp_path):
    parser = build_parser()
    invite_path = tmp_path / "alpha-invite.json"
    invite = AlphaInvite.create(
        coordinator="http://100.64.10.20:8765",
        admission_token="alpha-token-123",
    )
    write_alpha_invite(invite_path, invite)

    args = parser.parse_args(
        [
            "node",
            "status",
            "--home",
            str(tmp_path / ".mesh"),
            "--invite",
            str(invite_path),
        ]
    )

    coordinator, admission_token, invite_summary = _node_status_connection_from_args(args)

    assert coordinator == "http://100.64.10.20:8765"
    assert admission_token == "alpha-token-123"
    assert invite_summary["coordinator"] == "http://100.64.10.20:8765"
    assert "admission_token" not in invite_summary


def test_node_status_explicit_coordinator_overrides_invite_url_but_reuses_token(tmp_path):
    parser = build_parser()
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(
            coordinator="http://100.64.10.20:8765",
            admission_token="alpha-token-123",
        ),
    )

    args = parser.parse_args(
        [
            "node",
            "status",
            "--home",
            str(tmp_path / ".mesh"),
            "--invite",
            str(invite_path),
            "--coordinator",
            "http://127.0.0.1:9999",
        ]
    )

    coordinator, admission_token, invite_summary = _node_status_connection_from_args(args)

    assert coordinator == "http://127.0.0.1:9999"
    assert admission_token == "alpha-token-123"
    assert invite_summary["coordinator"] == "http://100.64.10.20:8765"


def test_operator_alpha_status_command_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(["operator", "alpha-status", "--home", str(tmp_path / ".mesh")])

    assert args.func.__name__ == "alpha_status_command"


def test_operator_alpha_evidence_command_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "alpha-evidence",
            "--home",
            str(tmp_path / ".mesh"),
            "--expected-worker-id",
            "worker_test",
            "--jobs",
            "1",
            "--include-inference-proof",
            "--inference-mode",
            "auto",
            "--inference-model",
            "tiny-test-model",
            "--inference-jobs",
            "2",
        ]
    )

    assert args.func.__name__ == "alpha_evidence_command"
    assert args.include_inference_proof is True
    assert args.inference_mode == "auto"
    assert args.inference_model == "tiny-test-model"
    assert args.inference_jobs == 2


def test_node_refresh_capabilities_command_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "node",
            "refresh-capabilities",
            "--home",
            str(tmp_path / ".mesh"),
            "--invite",
            str(tmp_path / "alpha-invite.json"),
            "--restart-worker",
            "--report",
            str(tmp_path / "refresh.json"),
        ]
    )

    assert args.func.__name__ == "run_node_refresh_capabilities_command"
    assert args.restart_worker is True


def test_operator_alpha_inference_proof_command_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "alpha-inference-proof",
            "--invite",
            str(tmp_path / "alpha-invite.json"),
            "--mode",
            "auto",
            "--model",
            "tiny-test-model",
            "--jobs",
            "2",
            "--report",
            str(tmp_path / "alpha-inference-proof.json"),
        ]
    )

    assert args.func.__name__ == "alpha_inference_proof_command"

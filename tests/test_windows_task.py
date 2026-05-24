import json
from types import SimpleNamespace
from pathlib import Path

import pytest

import chatp2p.windows_task as windows_task
from chatp2p.windows_task import (
    DEFAULT_DAILY_CHECK_TASK_NAME,
    DEFAULT_TASK_NAME,
    DEFAULT_RELIABILITY_TASK_NAME,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    DailyCheckTaskConfig,
    ReliabilityTaskConfig,
    WatchdogTaskConfig,
    build_daily_check_task_plan,
    build_reliability_task_plan,
    build_watchdog_task_plan,
    install_daily_check_task,
    install_reliability_task,
    install_watchdog_task,
    uninstall_watchdog_task,
)


def test_watchdog_task_plan_builds_tokenless_launcher(tmp_path):
    home = tmp_path / ".mesh"
    invite = tmp_path / "alpha-invite.json"
    report = tmp_path / "watchdog-report.json"
    source_root = tmp_path / "ChatP2P" / "src"
    work_dir = tmp_path / "ChatP2P"
    python_executable = tmp_path / "python.exe"

    plan = build_watchdog_task_plan(
        WatchdogTaskConfig(
            home=home,
            invite_path=invite,
            report_path=report,
            task_name="ChatP2P Test Watchdog",
            source_root=source_root,
            work_dir=work_dir,
            python_executable=python_executable,
        )
    )

    assert plan["task_name"] == "ChatP2P Test Watchdog"
    assert plan["schedule"] == "onlogon"
    assert plan["launcher_path"].endswith("chatp2p-test-watchdog.cmd")
    assert plan["watchdog_argv"][:4] == [str(python_executable.resolve()), "-m", "chatp2p.cli", "node"]
    assert "--checks" in plan["watchdog_argv"]
    assert "0" in plan["watchdog_argv"]
    assert "--startup-timeout-seconds" in plan["watchdog_argv"]
    assert str(DEFAULT_STARTUP_TIMEOUT_SECONDS) in plan["watchdog_argv"]
    assert "alpha-invite.json" in plan["launcher"]
    assert str(source_root.resolve()) in plan["launcher"]
    assert "admission_token" not in json.dumps(plan)
    assert "secret" not in json.dumps(plan).lower()


def test_watchdog_task_requires_operator_config_for_coordinator(tmp_path):
    with pytest.raises(ValueError, match="operator-config"):
        build_watchdog_task_plan(
            WatchdogTaskConfig(
                home=tmp_path / ".mesh",
                invite_path=tmp_path / "alpha-invite.json",
                role="both",
            )
        )


def test_install_and_uninstall_task_dry_runs_do_not_touch_windows(tmp_path):
    install = install_watchdog_task(
        WatchdogTaskConfig(
            home=tmp_path / ".mesh",
            invite_path=tmp_path / "alpha-invite.json",
            python_executable=Path("python.exe"),
            source_root=tmp_path / "src",
            work_dir=tmp_path,
        ),
        dry_run=True,
    )
    uninstall = uninstall_watchdog_task(task_name=DEFAULT_TASK_NAME, home=tmp_path / ".mesh", dry_run=True)

    assert install["ok"] is True
    assert install["dry_run"] is True
    assert install["plan"]["create_command"][0] == "schtasks.exe"
    assert uninstall["ok"] is True
    assert uninstall["dry_run"] is True
    assert uninstall["plan"]["delete_command"] == ["schtasks.exe", "/Delete", "/TN", DEFAULT_TASK_NAME, "/F"]


def test_reliability_task_plan_builds_tokenless_launcher(tmp_path):
    source_root = tmp_path / "ChatP2P" / "src"
    work_dir = tmp_path / "ChatP2P"
    python_executable = tmp_path / "python.exe"

    plan = build_reliability_task_plan(
        ReliabilityTaskConfig(
            primary_invite_path=tmp_path / "primary-invite.json",
            backup_invite_path=tmp_path / "backup-invite.json",
            out_dir=tmp_path / "reliability-pack",
            task_name=DEFAULT_RELIABILITY_TASK_NAME,
            interval_minutes=15,
            expected_primary_worker_id="worker_primary",
            expected_backup_worker_id="worker_backup",
            python_executable=python_executable,
            source_root=source_root,
            work_dir=work_dir,
        )
    )

    assert plan["task_name"] == DEFAULT_RELIABILITY_TASK_NAME
    assert plan["schedule"] == "minute"
    assert plan["interval_minutes"] == 15
    assert plan["create_command"][:6] == [
        "schtasks.exe",
        "/Create",
        "/TN",
        DEFAULT_RELIABILITY_TASK_NAME,
        "/SC",
        "MINUTE",
    ]
    assert "/MO" in plan["create_command"]
    assert "15" in plan["create_command"]
    assert plan["launcher_path"].endswith("chatp2p-reliability-pack.cmd")
    assert plan["reliability_argv"][:5] == [
        str(python_executable.resolve()),
        "-m",
        "chatp2p.cli",
        "operator",
        "reliability-pack",
    ]
    assert "--expected-primary-worker-id" in plan["reliability_argv"]
    assert "--expected-backup-worker-id" in plan["reliability_argv"]
    assert "--include-deterministic-smoke" not in plan["reliability_argv"]
    assert str(source_root.resolve()) in plan["launcher"]
    assert "admission_token" not in json.dumps(plan)
    assert "secret" not in json.dumps(plan).lower()

    opt_in_plan = build_reliability_task_plan(
        ReliabilityTaskConfig(
            primary_invite_path=tmp_path / "primary-invite.json",
            backup_invite_path=tmp_path / "backup-invite.json",
            out_dir=tmp_path / "reliability-pack",
            include_deterministic_smoke=True,
            python_executable=python_executable,
            source_root=source_root,
            work_dir=work_dir,
        )
    )
    assert "--include-deterministic-smoke" in opt_in_plan["reliability_argv"]


def test_reliability_task_install_dry_run_does_not_touch_windows(tmp_path):
    install = install_reliability_task(
        ReliabilityTaskConfig(
            primary_invite_path=tmp_path / "primary-invite.json",
            backup_invite_path=tmp_path / "backup-invite.json",
            out_dir=tmp_path / "reliability-pack",
            python_executable=Path("python.exe"),
            source_root=tmp_path / "src",
            work_dir=tmp_path,
        ),
        dry_run=True,
    )

    assert install["ok"] is True
    assert install["dry_run"] is True
    assert install["plan"]["create_command"][0] == "schtasks.exe"
    assert install["plan"]["interval_minutes"] == 30


def test_daily_check_task_plan_builds_tokenless_launcher(tmp_path):
    source_root = tmp_path / "ChatP2P" / "src"
    work_dir = tmp_path / "ChatP2P"
    python_executable = tmp_path / "python.exe"

    plan = build_daily_check_task_plan(
        DailyCheckTaskConfig(
            repo=tmp_path / "ChatP2P",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "primary-invite.json",
            backup_invite_path=tmp_path / "backup-invite.json",
            reliability_dir=tmp_path / "reliability-pack",
            out_dir=tmp_path / "daily-check",
            console_out_dir=tmp_path / "operator-console",
            task_name=DEFAULT_DAILY_CHECK_TASK_NAME,
            interval_minutes=45,
            expected_primary_worker_id="worker_primary",
            expected_backup_worker_id="worker_backup",
            python_executable=python_executable,
            source_root=source_root,
            work_dir=work_dir,
        )
    )

    assert plan["task_name"] == DEFAULT_DAILY_CHECK_TASK_NAME
    assert plan["schedule"] == "minute"
    assert plan["interval_minutes"] == 45
    assert plan["create_command"][:6] == [
        "schtasks.exe",
        "/Create",
        "/TN",
        DEFAULT_DAILY_CHECK_TASK_NAME,
        "/SC",
        "MINUTE",
    ]
    assert plan["launcher_path"].endswith("chatp2p-daily-check.cmd")
    assert plan["daily_check_argv"][:5] == [
        str(python_executable.resolve()),
        "-m",
        "chatp2p.cli",
        "operator",
        "daily-check",
    ]
    assert "--refresh-reliability-pack" not in plan["daily_check_argv"]
    assert "--include-deterministic-smoke" not in plan["daily_check_argv"]
    assert "--expected-primary-worker-id" in plan["daily_check_argv"]
    assert "--expected-backup-worker-id" in plan["daily_check_argv"]
    assert str(source_root.resolve()) in plan["launcher"]
    assert "admission_token" not in json.dumps(plan)
    assert "secret" not in json.dumps(plan).lower()


def test_daily_check_task_can_opt_into_reliability_refresh(tmp_path):
    plan = build_daily_check_task_plan(
        DailyCheckTaskConfig(
            repo=tmp_path / "ChatP2P",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "primary-invite.json",
            backup_invite_path=tmp_path / "backup-invite.json",
            reliability_dir=tmp_path / "reliability-pack",
            out_dir=tmp_path / "daily-check",
            refresh_reliability_pack=True,
            include_deterministic_smoke=True,
        )
    )

    assert "--refresh-reliability-pack" in plan["daily_check_argv"]
    assert "--include-deterministic-smoke" in plan["daily_check_argv"]


def test_daily_check_task_refresh_requires_backup_and_reliability_dir(tmp_path):
    with pytest.raises(ValueError, match="backup-invite"):
        build_daily_check_task_plan(
            DailyCheckTaskConfig(
                repo=tmp_path / "ChatP2P",
                home=tmp_path / ".mesh",
                primary_invite_path=tmp_path / "primary-invite.json",
                out_dir=tmp_path / "daily-check",
                refresh_reliability_pack=True,
            )
        )


def test_daily_check_task_install_dry_run_does_not_touch_windows(tmp_path):
    install = install_daily_check_task(
        DailyCheckTaskConfig(
            repo=tmp_path / "ChatP2P",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "primary-invite.json",
            out_dir=tmp_path / "daily-check",
            python_executable=Path("python.exe"),
            source_root=tmp_path / "src",
            work_dir=tmp_path,
        ),
        dry_run=True,
    )

    assert install["ok"] is True
    assert install["dry_run"] is True
    assert install["plan"]["create_command"][0] == "schtasks.exe"
    assert install["plan"]["interval_minutes"] == 60


def test_daily_check_task_falls_back_to_startup_folder_on_access_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr(windows_task, "_is_windows", lambda: True)
    monkeypatch.setattr(
        windows_task.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Access is denied.\n"),
    )

    report = install_daily_check_task(
        DailyCheckTaskConfig(
            repo=tmp_path / "ChatP2P",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "alpha-invite.json",
            out_dir=tmp_path / "daily-check",
            task_name="ChatP2P Test Daily Check",
            python_executable=tmp_path / "python.exe",
            source_root=tmp_path / "src",
            work_dir=tmp_path,
            startup_fallback=True,
        )
    )

    startup_launcher = (
        tmp_path
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "chatp2p-test-daily-check.vbs"
    )
    main_launcher = tmp_path / "daily-check" / "run" / "chatp2p-test-daily-check.cmd"

    assert report["ok"] is True
    assert report["install_method"] == "startup-folder"
    assert main_launcher.exists()
    assert startup_launcher.exists()
    assert "operator daily-check" in main_launcher.read_text(encoding="utf-8")
    assert str(main_launcher) in startup_launcher.read_text(encoding="utf-8")


def test_install_task_falls_back_to_startup_folder_on_access_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr(windows_task, "_is_windows", lambda: True)
    monkeypatch.setattr(
        windows_task.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Access is denied.\n"),
    )

    report = install_watchdog_task(
        WatchdogTaskConfig(
            home=tmp_path / ".mesh",
            invite_path=tmp_path / "alpha-invite.json",
            task_name="ChatP2P Test Watchdog",
            python_executable=tmp_path / "python.exe",
            source_root=tmp_path / "src",
            work_dir=tmp_path,
            startup_fallback=True,
        )
    )

    startup_launcher = (
        tmp_path
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "chatp2p-test-watchdog.vbs"
    )
    main_launcher = tmp_path / ".mesh" / "run" / "chatp2p-test-watchdog.cmd"

    assert report["ok"] is True
    assert report["install_method"] == "startup-folder"
    assert main_launcher.exists()
    assert startup_launcher.exists()
    assert "alpha-invite.json" in main_launcher.read_text(encoding="utf-8")
    assert str(main_launcher) in startup_launcher.read_text(encoding="utf-8")

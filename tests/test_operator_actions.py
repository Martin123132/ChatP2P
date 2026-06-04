import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import sys

import pytest

from chatp2p.cli import build_parser
from chatp2p import cli as cli_module
from chatp2p.operator_actions import (
    build_operator_action_queue,
    format_operator_action_queue_markdown,
    run_operator_action,
    write_operator_action_queue,
)


def test_action_queue_blocks_on_privacy_findings(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="fail",
            can_continue=False,
            recommended_next_action="fix_public_privacy_findings",
            privacy_ok=False,
        )
    )

    assert queue["schema"] == "chatp2p.operator-action-queue.v1"
    assert queue["status"] == "fail"
    assert queue["next_action"]["action_id"] == "fix_public_privacy_findings"
    assert queue["next_action"]["partner_required"] is False
    assert queue["next_action"]["can_run_without_partner"] is True
    assert queue["next_action"]["suggested_commands"][0]["shell"] == "powershell"
    assert "operator privacy-scan" in queue["next_action"]["suggested_commands"][0]["command"]


def test_action_queue_allows_local_development_without_partner(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="continue_development",
            privacy_ok=True,
        )
    )

    assert queue["status"] == "pass"
    assert queue["can_continue_without_partner"] is True
    assert queue["next_action"]["action_id"] == "continue_development"
    assert queue["next_action"]["partner_required"] is False


def test_action_queue_sync_actions_are_local_and_allowlisted(tmp_path):
    wait_queue = build_operator_action_queue(
        _daily_report(
            status="warn",
            can_continue=True,
            recommended_next_action="wait_for_partner_autopull",
            privacy_ok=True,
        )
    )
    synced_queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="partner_synced_continue",
            privacy_ok=True,
        )
    )

    wait_action = wait_queue["next_action"]
    synced_action = synced_queue["next_action"]
    assert wait_action["partner_required"] is False
    assert wait_action["suggested_commands"][0]["argv"][2:5] == ["chatp2p.cli", "operator", "console"]
    assert synced_action["partner_required"] is False
    assert synced_action["suggested_commands"][0]["argv"][2:5] == ["chatp2p.cli", "operator", "privacy-scan"]


def test_action_queue_prioritizes_reliability_refresh_failure(tmp_path):
    report = _daily_report(
        status="fail",
        can_continue=True,
        recommended_next_action="repair_reliability_pack",
        privacy_ok=True,
    )
    report["steps"]["reliability_refresh"] = {
        "ok": False,
        "status": "fail",
        "message": "Refresh failed",
        "report_path": str(tmp_path / "daily-reliability-refresh.json"),
    }

    queue = build_operator_action_queue(report)

    assert queue["status"] == "fail"
    assert queue["next_action"]["action_id"] == "repair_reliability_pack"
    assert queue["next_action"]["severity"] == "blocker"
    assert "operator reliability-pack" in queue["next_action"]["suggested_commands"][0]["command"]


def test_action_queue_suggests_daily_check_command_for_daily_check_warning(tmp_path):
    report = _daily_report(
        status="warn",
        can_continue=True,
        recommended_next_action="continue_development",
        privacy_ok=True,
    )
    report["summary"]["warnings"] = ["daily check automation is stale"]

    queue = build_operator_action_queue(report)
    daily_action = next(
        action
        for action in queue["actions"]
        if action["action_id"].startswith("review_warning_daily_check_automation")
    )

    assert daily_action["partner_required"] is False
    assert "operator daily-check" in daily_action["suggested_commands"][0]["command"]
    assert "--console-out" in daily_action["suggested_commands"][0]["command"]


def test_action_queue_writes_json_and_markdown(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="continue_development",
            privacy_ok=True,
        )
    )

    artifacts = write_operator_action_queue(tmp_path / "actions", queue)
    markdown = format_operator_action_queue_markdown(queue)

    assert Path(artifacts["json"]).exists()
    assert Path(artifacts["markdown"]).exists()
    assert "continue_development" in markdown
    assert "Suggested Commands" in markdown


def test_run_action_dry_run_writes_report(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="continue_development",
            privacy_ok=True,
        )
    )
    queue_path = tmp_path / "action-queue.json"
    report_path = tmp_path / "operator-action-run-report.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")

    report = run_operator_action(
        queue,
        queue_path=queue_path,
        action_id="continue_development",
        dry_run=True,
        out_path=report_path,
    )

    assert report["schema"] == "chatp2p.operator-action-run-report.v1"
    assert report["status"] == "dry_run"
    assert report["ok"] is True
    assert report["execution"]["attempted"] is False
    assert "operator privacy-scan" in report["command"]["preview"]
    assert report_path.exists()


def test_run_action_refuses_unstructured_command(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="continue_development",
            privacy_ok=True,
        )
    )
    queue["actions"][0]["suggested_commands"][0].pop("argv")

    try:
        run_operator_action(queue, queue_path=tmp_path / "action-queue.json")
    except ValueError as exc:
        assert "structured argv" in str(exc)
    else:
        raise AssertionError("expected run_operator_action to reject command without argv")


def test_run_action_refuses_non_allowlisted_operator_command(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="continue_development",
            privacy_ok=True,
        )
    )
    queue["actions"][0]["suggested_commands"][0]["argv"] = [
        "python",
        "-m",
        "chatp2p.cli",
        "operator",
        "config",
    ]

    try:
        run_operator_action(queue, queue_path=tmp_path / "action-queue.json")
    except ValueError as exc:
        assert "not allowlisted" in str(exc)
    else:
        raise AssertionError("expected run_operator_action to reject non-allowlisted command")


def test_run_action_allows_action_queue_command(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="continue_development",
            privacy_ok=True,
        )
    )
    queue["actions"][0]["suggested_commands"][0] = {
        "label": "Rebuild action queue",
        "shell": "powershell",
        "command": "python -m chatp2p.cli operator action-queue --daily-report daily-check.json --out actions",
        "argv": [
            "python",
            "-m",
            "chatp2p.cli",
            "operator",
            "action-queue",
            "--daily-report",
            str(tmp_path / "daily-check.json"),
            "--out",
            str(tmp_path / "actions"),
        ],
    }

    report = run_operator_action(
        queue,
        queue_path=tmp_path / "action-queue.json",
        action_id=queue["actions"][0]["action_id"],
        dry_run=True,
    )

    assert report["status"] == "dry_run"
    assert "action-queue" in report["command"]["allowlist"]


def test_run_action_refuses_non_python_executable(tmp_path):
    queue = build_operator_action_queue(
        _daily_report(
            status="pass",
            can_continue=True,
            recommended_next_action="continue_development",
            privacy_ok=True,
        )
    )
    queue["actions"][0]["suggested_commands"][0]["argv"][0] = "cmd.exe"

    try:
        run_operator_action(queue, queue_path=tmp_path / "action-queue.json")
    except ValueError as exc:
        assert "Python executable" in str(exc)
    else:
        raise AssertionError("expected run_operator_action to reject non-Python executable")


def test_operator_action_queue_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "action-queue",
            "--daily-report",
            str(tmp_path / "daily-check.json"),
            "--out",
            str(tmp_path / "actions"),
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_action_queue_command"
    assert args.daily_report == str(tmp_path / "daily-check.json")
    assert args.out == str(tmp_path / "actions")
    assert args.json is True


def test_operator_run_action_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "run-action",
            "--queue",
            str(tmp_path / "action-queue.json"),
            "--action",
            "continue_development",
            "--command-index",
            "0",
            "--out",
            str(tmp_path / "operator-action-run-report.json"),
            "--dry-run",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_run_action_command"
    assert args.queue == str(tmp_path / "action-queue.json")
    assert args.action == "continue_development"
    assert args.command_index == 0
    assert args.dry_run is True
    assert args.execute is False
    assert args.json is True


def test_operator_maintenance_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(tmp_path),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--backup-invite",
            str(tmp_path / "backup-alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--home",
            str(tmp_path / ".mesh"),
            "--reliability-dir",
            str(tmp_path / "reliability"),
            "--skip-network-checks",
            "--expected-primary-worker-id",
            "worker_primary",
            "--expected-backup-worker-id",
            "worker_backup",
            "--partner-report",
            str(tmp_path / "partner-autopilot-report.json"),
            "--preview-top-action",
            "--run-top-action",
            "--allow-execute",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_maintenance_command"
    assert args.repo == str(tmp_path)
    assert args.primary_invite == str(tmp_path / "alpha-invite.json")
    assert args.out == str(tmp_path / "maintenance")
    assert args.home == str(tmp_path / ".mesh")
    assert args.skip_network_checks is True
    assert args.preview_top_action is True
    assert args.run_top_action is True
    assert args.allow_execute is True
    assert args.json is True


def test_operator_maintenance_command_invokes_powershell(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    maintenance_script = scripts / "operator-maintenance.ps1"
    maintenance_script.write_text("Write-Host \"maintenance\"", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(repo),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--preview-top-action",
            "--run-top-action",
            "--allow-execute",
            "--json",
        ]
    )

    captured = {}

    def fake_run(command, check=False, text=False, capture_output=False, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    cli_module.operator_maintenance_command(args)

    expected_script = str((repo / "scripts" / "operator-maintenance.ps1").resolve())
    assert captured["command"][0] == "powershell"
    assert expected_script in captured["command"]
    assert "-PrimaryInvite" in captured["command"]
    assert str(tmp_path / "alpha-invite.json") in captured["command"]
    assert "-PreviewTopAction" in captured["command"]
    assert "-RunTopAction" in captured["command"]
    assert "-AllowExecute" in captured["command"]
    assert "-Json" in captured["command"]


def test_operator_maintenance_script_writes_report_and_skips_partner_preview(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "operator-maintenance.ps1"
    fake_python = tmp_path / "fake-python.ps1"
    fake_log = tmp_path / "fake-python.log"
    out_root = tmp_path / "maintenance"
    fake_python.write_text(
        r'''
$ErrorActionPreference = "Stop"
$logPath = $env:CHATP2P_FAKE_PYTHON_LOG
Add-Content -LiteralPath $logPath -Value ($args -join " ")
$script:FakePythonArgs = $args

function Get-ArgAfter {
    param([string]$Name)
    for ($i = 0; $i -lt $script:FakePythonArgs.Count; $i++) {
        if ($script:FakePythonArgs[$i] -eq $Name -and ($i + 1) -lt $script:FakePythonArgs.Count) {
            return $script:FakePythonArgs[$i + 1]
        }
    }
    throw "missing argument: $Name"
}

$joined = $args -join " "
if ($joined -like "*operator console*") {
    $out = Get-ArgAfter "--out"
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    [ordered]@{
        summary = [ordered]@{
            can_continue_without_partner = $true
            recommended_next_action = "review_partner_required_action"
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $out "operator-console.json") -Encoding UTF8
    exit 0
}
if ($joined -like "*operator daily-check*") {
    $out = Get-ArgAfter "--out"
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    [ordered]@{
        summary = [ordered]@{
            can_continue_without_partner = $true
            recommended_next_action = "review_partner_required_action"
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $out "daily-check.json") -Encoding UTF8
    exit 0
}
if ($joined -like "*operator action-queue*") {
    $out = Get-ArgAfter "--out"
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    [ordered]@{
        next_action = [ordered]@{
            action_id = "partner_needed"
            partner_required = $true
            can_run_without_partner = $false
            suggested_commands = @([ordered]@{ argv = @("python") })
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $out "action-queue.json") -Encoding UTF8
    exit 0
}
if ($joined -like "*operator self-heal*") {
    $out = Get-ArgAfter "--out"
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    [ordered]@{
        summary = [ordered]@{
            repairable_issue_count = 0
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $out "operator-self-heal-report.json") -Encoding UTF8
    exit 0
}
if ($joined -like "*operator run-action*") {
    exit 99
}
exit 0
''',
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["CHATP2P_FAKE_PYTHON_LOG"] = str(fake_log)
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Root",
            str(repo),
            "-PrimaryInvite",
            str(tmp_path / "alpha-invite.json"),
            "-OutRoot",
            str(out_root),
            "-ReliabilityDir",
            str(tmp_path / "reliability-pack-live"),
            "-PreviewTopAction",
            "-Json",
            "-Python",
            str(fake_python),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "chatp2p.operator-maintenance-report.v1" in result.stdout
    report = json.loads((out_root / "operator-maintenance-report.json").read_text(encoding="utf-8-sig"))
    assert report["schema"] == "chatp2p.operator-maintenance-report.v1"
    assert report["summary"]["top_action_status"] == "not_local_executable"
    assert report["summary"]["top_action_partner_required"] is True
    assert len(report["steps"]) == 4
    assert {step["report_mode"] for step in report["steps"]} == {"report_only"}
    fake_calls = fake_log.read_text(encoding="utf-8")
    assert "operator daily-check" in fake_calls
    assert "--reliability-dir " + str(tmp_path / "reliability-pack-live") in fake_calls
    assert "operator run-action" not in fake_calls


def test_operator_maintenance_command_falls_back_to_python_when_script_missing(monkeypatch, tmp_path):
    parser = build_parser()
    repo = tmp_path / "repo"
    repo.mkdir()

    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(repo),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--preview-top-action",
            "--run-top-action",
            "--allow-execute",
        ]
    )

    captured_steps: list[list[str]] = []

    def fake_runner(command: list[str], *, label: str, cwd: Path) -> None:
        captured_steps.append(command.copy())

    def fake_read_json_file(path: Path, description: str = "JSON file"):
        path_text = str(path)
        if path_text.endswith("operator-console.json"):
            return {
                "summary": {"can_continue_without_partner": True, "recommended_next_action": "continue_development"}
            }
        if path_text.endswith("operator-self-heal-report.json"):
            return {"summary": {"repairable_issue_count": 0}}
        if path_text.endswith("action-queue.json"):
            return {
                "next_action": {
                    "action_id": "continue_development",
                    "partner_required": False,
                    "can_run_without_partner": True,
                    "suggested_commands": [{"argv": ["python"]}],
                }
            }
        raise ValueError(f"unexpected JSON read: {path_text}")

    monkeypatch.setattr(cli_module, "_run_operator_maintenance_command", fake_runner)
    monkeypatch.setattr(cli_module, "read_json_file", fake_read_json_file)

    cli_module.operator_maintenance_command(args)

    assert len(captured_steps) == 6
    assert Path(captured_steps[0][0]).name in {"python", "python.exe"} or captured_steps[0][0] == sys.executable
    assert captured_steps[0][0] == sys.executable
    assert captured_steps[0][2:5] == ["chatp2p.cli", "operator", "console"]
    assert captured_steps[1][2:5] == ["chatp2p.cli", "operator", "daily-check"]
    assert captured_steps[2][2:5] == ["chatp2p.cli", "operator", "action-queue"]
    assert captured_steps[3][2:5] == ["chatp2p.cli", "operator", "self-heal"]
    assert captured_steps[4][2:5] == ["chatp2p.cli", "operator", "run-action"]
    assert captured_steps[5][2:5] == ["chatp2p.cli", "operator", "run-action"]


def test_operator_maintenance_fallback_reports_json(monkeypatch, tmp_path, capsys):
    parser = build_parser()
    repo = tmp_path / "repo"
    repo.mkdir()

    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(repo),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--json",
        ]
    )

    captured_steps: list[list[str]] = []

    def fake_runner(command: list[str], *, label: str, cwd: Path) -> None:
        captured_steps.append(command.copy())

    def fake_read_json_file(path: Path, description: str = "JSON file"):
        path_text = str(path)
        if path_text.endswith("operator-console.json"):
            return {
                "summary": {"can_continue_without_partner": True, "recommended_next_action": "continue_development"},
                "artifacts": {"json": str(path)},
            }
        if path_text.endswith("operator-self-heal-report.json"):
            return {"summary": {"repairable_issue_count": 0}}
        if path_text.endswith("action-queue.json"):
            return {
                "next_action": {
                    "action_id": "continue_development",
                    "partner_required": False,
                    "can_run_without_partner": True,
                    "suggested_commands": [{"argv": ["python"]}],
                },
                "artifacts": {"json": str(path)},
            }
        raise ValueError(f"unexpected JSON read: {path_text}")

    monkeypatch.setattr(cli_module, "_run_operator_maintenance_command", fake_runner)
    monkeypatch.setattr(cli_module, "read_json_file", fake_read_json_file)

    cli_module.operator_maintenance_command(args)
    output = capsys.readouterr().out
    marker = output.find("{\n")
    assert marker >= 0
    report = json.loads(output[marker:])

    assert report["schema"] == "chatp2p.operator-maintenance-report.v1"
    assert report["status"] == "pass"
    assert report["summary"]["top_action_status"] == "safe_local"
    assert report["summary"]["recommended_next_action"] == "continue_development"
    assert report["summary"]["top_action"]["action_id"] == "continue_development"
    assert report["artifacts"]["maintenance_json"] == str((tmp_path / "maintenance" / "operator-maintenance-report.json").resolve())
    assert len(report["steps"]) == 4
    assert any(step["label"] == "operator console" for step in report["steps"])
    assert any(step["status"] == "pass" for step in report["steps"])
    assert {step["returncode"] for step in report["steps"]} == {0}
    assert len(captured_steps) == 4
    assert captured_steps[0][2:5] == ["chatp2p.cli", "operator", "console"]
    assert captured_steps[1][2:5] == ["chatp2p.cli", "operator", "daily-check"]
    assert captured_steps[2][2:5] == ["chatp2p.cli", "operator", "action-queue"]
    assert captured_steps[3][2:5] == ["chatp2p.cli", "operator", "self-heal"]


def test_operator_maintenance_fallback_treats_console_fail_as_report_only(monkeypatch, tmp_path, capsys):
    parser = build_parser()
    repo = tmp_path / "repo"
    repo.mkdir()

    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(repo),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--skip-network-checks",
            "--json",
        ]
    )

    captured_steps: list[list[str]] = []

    def fake_runner(command: list[str], *, label: str, cwd: Path) -> int:
        captured_steps.append(command.copy())
        if label in {"operator console", "operator daily-check"}:
            return 1
        return 0

    def fake_read_json_file(path: Path, description: str = "JSON file"):
        path_text = str(path)
        if path_text.endswith("operator-console.json"):
            return {
                "summary": {
                    "can_continue_without_partner": False,
                    "recommended_next_action": "fix_public_privacy_findings",
                    "status": "fail",
                },
                "artifacts": {"json": str(path)},
            }
        if path_text.endswith("operator-self-heal-report.json"):
            return {"summary": {"repairable_issue_count": 2}}
        if path_text.endswith("action-queue.json"):
            return {
                "next_action": {
                    "action_id": "fix_public_privacy_findings",
                    "partner_required": False,
                    "can_run_without_partner": True,
                    "suggested_commands": [{"argv": ["python"]}],
                }
            }
        raise ValueError(f"unexpected JSON read: {path_text}")

    monkeypatch.setattr(cli_module, "_run_operator_maintenance_command", fake_runner)
    monkeypatch.setattr(cli_module, "read_json_file", fake_read_json_file)

    cli_module.operator_maintenance_command(args)
    output = capsys.readouterr().out
    marker = output.find("{\n")
    assert marker >= 0
    report = json.loads(output[marker:])

    assert report["schema"] == "chatp2p.operator-maintenance-report.v1"
    assert report["status"] == "fail"
    assert len(report["steps"]) == 4
    console_step = next(step for step in report["steps"] if step["label"] == "operator console")
    daily_step = next(step for step in report["steps"] if step["label"] == "operator daily-check")
    action_queue_step = next(step for step in report["steps"] if step["label"] == "operator action-queue")
    assert console_step["returncode"] == 1
    assert console_step["status"] == "fail"
    assert console_step["report_mode"] == "report_only"
    assert "non-blocking report step" in console_step["error"]
    assert daily_step["returncode"] == 1
    assert daily_step["status"] == "fail"
    assert daily_step["report_mode"] == "report_only"
    assert "non-blocking report step" in daily_step["error"]
    assert action_queue_step["report_mode"] == "report_only"
    assert report["summary"]["top_action_status"] == "safe_local"


def test_operator_maintenance_skips_top_action_run_when_partner_required(monkeypatch, tmp_path):
    parser = build_parser()
    repo = tmp_path / "repo"
    repo.mkdir()

    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(repo),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--preview-top-action",
            "--run-top-action",
            "--allow-execute",
        ]
    )

    captured_steps: list[list[str]] = []

    def fake_runner(command: list[str], *, label: str, cwd: Path) -> None:
        captured_steps.append(command.copy())

    def fake_read_json_file(path: Path, description: str = "JSON file"):
        path_text = str(path)
        if path_text.endswith("operator-console.json"):
            return {
                "summary": {"can_continue_without_partner": True, "recommended_next_action": "continue_development"}
            }
        if path_text.endswith("operator-self-heal-report.json"):
            return {"summary": {"repairable_issue_count": 0}}
        if path_text.endswith("action-queue.json"):
            return {
                "next_action": {
                    "action_id": "continue_development",
                    "partner_required": True,
                    "can_run_without_partner": False,
                    "suggested_commands": [{"argv": ["python"]}],
                }
            }
        raise ValueError(f"unexpected JSON read: {path_text}")

    monkeypatch.setattr(cli_module, "_run_operator_maintenance_command", fake_runner)
    monkeypatch.setattr(cli_module, "read_json_file", fake_read_json_file)

    with pytest.raises(SystemExit):
        cli_module.operator_maintenance_command(args)

    assert len(captured_steps) == 4
    assert captured_steps[0][2:5] == ["chatp2p.cli", "operator", "console"]
    assert captured_steps[1][2:5] == ["chatp2p.cli", "operator", "daily-check"]
    assert captured_steps[2][2:5] == ["chatp2p.cli", "operator", "action-queue"]
    assert captured_steps[3][2:5] == ["chatp2p.cli", "operator", "self-heal"]


def test_operator_maintenance_skips_top_action_preview_when_no_local_action(monkeypatch, tmp_path):
    parser = build_parser()
    repo = tmp_path / "repo"
    repo.mkdir()

    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(repo),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--preview-top-action",
        ]
    )

    captured_steps: list[list[str]] = []

    def fake_runner(command: list[str], *, label: str, cwd: Path) -> None:
        captured_steps.append(command.copy())

    def fake_read_json_file(path: Path, description: str = "JSON file"):
        path_text = str(path)
        if path_text.endswith("operator-console.json"):
            return {
                "summary": {"can_continue_without_partner": True, "recommended_next_action": "continue_development"}
            }
        if path_text.endswith("operator-self-heal-report.json"):
            return {"summary": {"repairable_issue_count": 0}}
        if path_text.endswith("action-queue.json"):
            return {
                "next_action": {
                    "action_id": "continue_development",
                    "partner_required": True,
                    "can_run_without_partner": False,
                    "suggested_commands": [],
                }
            }
        raise ValueError(f"unexpected JSON read: {path_text}")

    monkeypatch.setattr(cli_module, "_run_operator_maintenance_command", fake_runner)
    monkeypatch.setattr(cli_module, "read_json_file", fake_read_json_file)

    cli_module.operator_maintenance_command(args)

    assert len(captured_steps) == 4
    assert captured_steps[0][2:5] == ["chatp2p.cli", "operator", "console"]
    assert captured_steps[1][2:5] == ["chatp2p.cli", "operator", "daily-check"]
    assert captured_steps[2][2:5] == ["chatp2p.cli", "operator", "action-queue"]
    assert captured_steps[3][2:5] == ["chatp2p.cli", "operator", "self-heal"]


def test_operator_maintenance_requires_execute_to_run_top_action(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "maintenance",
            "--repo",
            str(tmp_path),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--out",
            str(tmp_path / "maintenance"),
            "--run-top-action",
        ]
    )

    with pytest.raises(SystemExit):
        cli_module.operator_maintenance_command(args)


def _daily_report(
    *,
    status: str,
    can_continue: bool,
    recommended_next_action: str,
    privacy_ok: bool,
):
    return {
        "schema": "chatp2p.operator-daily-check-report.v1",
        "status": status,
        "generated_at": "2026-05-24T00:00:00+00:00",
        "config": {
            "repo": "D:\\Projects\\ChatP2P",
            "home": "D:\\ChatP2PData\\.mesh",
            "primary_invite_path": "D:\\ChatP2PData\\alpha-invite.json",
            "backup_invite_path": "D:\\ChatP2PData\\backup-alpha-invite-partner.json",
            "reliability_dir": "D:\\ChatP2PData\\reliability-pack-live",
            "out_dir": "D:\\ChatP2PData\\daily-check",
            "console_out_dir": "D:\\ChatP2PData\\operator-console",
            "expected_primary_worker_id": "worker_PRIMARY",
            "expected_backup_worker_id": "worker_BACKUP",
        },
        "summary": {
            "status": status,
            "can_continue_without_partner": can_continue,
            "recommended_next_action": recommended_next_action,
            "warnings": [],
            "errors": [] if status != "fail" else ["example failure"],
        },
        "steps": {
            "privacy_scan": {
                "ok": privacy_ok,
                "status": "pass" if privacy_ok else "fail",
                "finding_count": 0 if privacy_ok else 1,
                "report_path": "D:\\ChatP2PData\\daily-check\\daily-privacy-scan.json",
            },
            "reliability_refresh": {
                "ok": True,
                "status": "skipped",
                "message": "Reliability pack refresh was not requested.",
            },
            "operator_console": {
                "ok": status != "fail",
                "status": status,
                "message": recommended_next_action,
                "can_continue_without_partner": can_continue,
                "html": "D:\\ChatP2PData\\operator-console\\operator-console.html",
            },
        },
        "artifacts": {
            "json": "D:\\ChatP2PData\\daily-check\\daily-check.json",
            "markdown": "D:\\ChatP2PData\\daily-check\\daily-check.md",
            "operator_console_html": "D:\\ChatP2PData\\operator-console\\operator-console.html",
        },
    }

import json
from pathlib import Path

from chatp2p.cli import build_parser
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

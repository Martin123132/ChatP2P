from pathlib import Path

from chatp2p.cli import build_parser
from chatp2p.operator_actions import (
    build_operator_action_queue,
    format_operator_action_queue_markdown,
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

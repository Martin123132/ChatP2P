import json
from datetime import datetime, timezone
from pathlib import Path

from chatp2p.cli import build_parser
from chatp2p.operator_actions import build_operator_action_queue, write_operator_action_queue
from chatp2p.operator_self_heal import OperatorSelfHealConfig, run_operator_self_heal


def test_self_heal_reports_missing_daily_check_as_local_action(tmp_path):
    console = _console_report()
    console["daily_check_automation"]["report"] = {
        "status": "missing",
        "fresh": False,
        "path": str(tmp_path / "daily" / "daily-check.json"),
    }
    console_path, queue_path = _write_console_and_queue(tmp_path, console)

    report = run_operator_self_heal(
        OperatorSelfHealConfig(
            console_report_path=console_path,
            daily_report_path=tmp_path / "daily" / "daily-check.json",
            action_queue_path=queue_path,
            out_dir=tmp_path / "self-heal",
        )
    )

    assert report["schema"] == "chatp2p.operator-self-heal-report.v1"
    assert report["status"] == "warn"
    assert _issue_ids(report) >= {"daily_check_report_missing_or_stale"}
    assert "refresh_daily_check_report" in report["summary"]["selected_action_ids"]
    action = _action(report, "refresh_daily_check_report")
    assert action["partner_required"] is False
    assert "operator run-action" in action["run_action"]["dry_run_command"]
    assert "operator run-action" in action["run_action"]["execute_command"]


def test_self_heal_reports_stale_reliability_pack(tmp_path):
    console = _console_report()
    console["reliability"] = {
        "status": "pass",
        "exists": True,
        "fresh": False,
        "summary_path": str(tmp_path / "reliability" / "reliability-summary.json"),
    }
    console_path, queue_path = _write_console_and_queue(tmp_path, console)
    daily_path = _write_daily_report(tmp_path)

    report = run_operator_self_heal(
        OperatorSelfHealConfig(
            console_report_path=console_path,
            daily_report_path=daily_path,
            action_queue_path=queue_path,
            out_dir=tmp_path / "self-heal",
        )
    )

    assert "reliability_pack_stale" in _issue_ids(report)
    assert "refresh_reliability_pack" in report["summary"]["selected_action_ids"]


def test_self_heal_prioritizes_privacy_findings(tmp_path):
    console = _console_report()
    console["privacy_scan"] = {
        "ok": False,
        "status": "fail",
        "finding_count": 1,
        "findings": [{"pattern": "long_admission_token", "match": "<redacted>"}],
    }
    console_path, queue_path = _write_console_and_queue(tmp_path, console)
    daily_path = _write_daily_report(tmp_path, privacy_ok=False)

    report = run_operator_self_heal(
        OperatorSelfHealConfig(
            console_report_path=console_path,
            daily_report_path=daily_path,
            action_queue_path=queue_path,
            out_dir=tmp_path / "self-heal",
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["top_self_heal_action"] == "fix_public_privacy_findings"
    assert report["actions"][0]["partner_required"] is False


def test_self_heal_marks_all_v1_actions_partner_free(tmp_path):
    console = _console_report()
    console["daily_check_automation"]["report"]["status"] = "missing"
    console["daily_check_automation"]["report"]["fresh"] = False
    console["reliability"] = {"status": "missing", "exists": False, "fresh": False}
    console["action_runner"]["last_run"] = {"status": "missing", "path": str(tmp_path / "missing.json")}
    console_path, queue_path = _write_console_and_queue(tmp_path, console)

    report = run_operator_self_heal(
        OperatorSelfHealConfig(
            console_report_path=console_path,
            daily_report_path=tmp_path / "daily" / "daily-check.json",
            action_queue_path=queue_path,
            out_dir=tmp_path / "self-heal",
        )
    )

    assert report["actions"]
    assert all(action["partner_required"] is False for action in report["actions"])
    assert all(issue["partner_required"] is False for issue in report["issues"])


def test_self_heal_writes_reports_and_redacts_tokens(tmp_path):
    console = _console_report()
    console["config"]["admission_token"] = "S" * 32
    console_path, queue_path = _write_console_and_queue(tmp_path, console)
    daily_path = _write_daily_report(tmp_path)
    out_dir = tmp_path / "self-heal"

    report = run_operator_self_heal(
        OperatorSelfHealConfig(
            console_report_path=console_path,
            daily_report_path=daily_path,
            action_queue_path=queue_path,
            out_dir=out_dir,
        )
    )

    json_path = Path(report["artifacts"]["json"])
    markdown_path = Path(report["artifacts"]["markdown"])
    assert json_path.exists()
    assert markdown_path.exists()
    assert "S" * 32 not in json_path.read_text(encoding="utf-8")
    assert "S" * 32 not in markdown_path.read_text(encoding="utf-8")


def test_operator_self_heal_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "self-heal",
            "--console-report",
            str(tmp_path / "operator-console.json"),
            "--daily-report",
            str(tmp_path / "daily-check.json"),
            "--action-queue",
            str(tmp_path / "action-queue.json"),
            "--out",
            str(tmp_path / "self-heal"),
            "--freshness-seconds",
            "30",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_self_heal_command"
    assert args.console_report == str(tmp_path / "operator-console.json")
    assert args.daily_report == str(tmp_path / "daily-check.json")
    assert args.action_queue == str(tmp_path / "action-queue.json")
    assert args.out == str(tmp_path / "self-heal")
    assert args.freshness_seconds == 30
    assert args.json is True


def _write_console_and_queue(tmp_path, console):
    console_path = tmp_path / "operator-console" / "operator-console.json"
    console_path.parent.mkdir(parents=True, exist_ok=True)
    queue = build_operator_action_queue(console)
    console["action_queue"] = queue
    console["artifacts"]["action_queue_json"] = str(console_path.parent / "action-queue.json")
    console_path.write_text(json.dumps(console), encoding="utf-8")
    artifacts = write_operator_action_queue(console_path.parent, queue)
    return console_path, Path(artifacts["json"])


def _issue_ids(report):
    return {issue["issue_id"] for issue in report["issues"]}


def _action(report, action_id):
    return next(action for action in report["actions"] if action["action_id"] == action_id)


def _write_daily_report(tmp_path, *, privacy_ok=True):
    daily_path = tmp_path / "daily" / "daily-check.json"
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": "chatp2p.operator-daily-check-report.v1",
        "ok": privacy_ok,
        "status": "pass" if privacy_ok else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": _config(tmp_path),
        "summary": {
            "can_continue_without_partner": privacy_ok,
            "recommended_next_action": "continue_development" if privacy_ok else "fix_public_privacy_findings",
            "warnings": [],
            "errors": [] if privacy_ok else ["public privacy scan has findings"],
        },
        "steps": {
            "privacy_scan": {
                "ok": privacy_ok,
                "status": "pass" if privacy_ok else "fail",
                "finding_count": 0 if privacy_ok else 1,
            },
            "reliability_refresh": {"ok": True, "status": "skipped"},
            "operator_console": {"ok": True, "status": "pass"},
        },
        "artifacts": {
            "json": str(daily_path),
            "operator_console_json": str(tmp_path / "operator-console" / "operator-console.json"),
            "action_queue_json": str(tmp_path / "daily" / "action-queue.json"),
        },
    }
    daily_path.write_text(json.dumps(report), encoding="utf-8")
    return daily_path


def _console_report():
    tmp = Path("D:/ChatP2PData")
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema": "chatp2p.operator-console-report.v1",
        "ok": True,
        "status": "pass",
        "generated_at": generated_at,
        "config": _config(tmp),
        "summary": {
            "can_continue_without_partner": True,
            "recommended_next_action": "continue_development",
            "warnings": [],
            "errors": [],
        },
        "privacy_scan": {"ok": True, "status": "pass", "finding_count": 0, "findings": []},
        "reliability": {
            "status": "pass",
            "exists": True,
            "fresh": True,
            "can_continue_without_partner": True,
        },
        "daily_check_automation": {
            "status": "pass",
            "report": {
                "status": "pass",
                "fresh": True,
                "path": str(tmp / "daily-check" / "daily-check.json"),
            },
        },
        "action_runner": {
            "last_run": {
                "status": "pass",
                "fresh": True,
                "path": str(tmp / "operator-console" / "operator-action-run-report.json"),
            }
        },
        "artifacts": {
            "json": str(tmp / "operator-console" / "operator-console.json"),
            "markdown": str(tmp / "operator-console" / "operator-console.md"),
            "html": str(tmp / "operator-console" / "operator-console.html"),
            "action_queue_json": str(tmp / "operator-console" / "action-queue.json"),
        },
    }


def _config(root):
    return {
        "repo": str(root / "repo"),
        "home": str(root / ".mesh"),
        "primary_invite_path": str(root / "alpha-invite.json"),
        "backup_invite_path": str(root / "backup-alpha-invite.json"),
        "reliability_dir": str(root / "reliability-pack-live"),
        "out_dir": str(root / "operator-console"),
        "console_out_dir": str(root / "operator-console"),
        "daily_check_dir": str(root / "daily-check"),
        "expected_primary_worker_id": "worker_PRIMARY",
        "expected_backup_worker_id": "worker_BACKUP",
    }

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from chatp2p.alpha import AlphaInvite, write_alpha_invite
from chatp2p.cli import build_parser
from chatp2p.operator_console import OperatorConsoleConfig, run_operator_console


def test_operator_console_writes_static_reports_and_redacts_invite_tokens(tmp_path):
    repo = _clean_repo(tmp_path)
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    invite_path = private_dir / "primary-alpha-invite.json"
    token = "secret-token-1234567890"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:8765", admission_token=token),
    )
    reliability_dir = tmp_path / "reliability-pack"
    reliability_dir.mkdir()
    _write_reliability_summary(reliability_dir, can_continue=True)

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=tmp_path / "operator-console",
            skip_network_checks=True,
        )
    )

    assert report["schema"] == "chatp2p.operator-console-report.v1"
    assert report["status"] == "warn"
    assert report["summary"]["can_continue_without_partner"] is True
    assert report["summary"]["recommended_next_action"] == "continue_development"
    assert report["action_queue"]["next_action"]["action_id"] == "continue_development"
    assert report["action_queue"]["next_action"]["partner_required"] is False
    for key in ("json", "markdown", "html", "action_queue_json", "action_queue_markdown"):
        artifact = Path(report["artifacts"][key])
        assert artifact.exists()
        assert token not in artifact.read_text(encoding="utf-8")
    html_report = Path(report["artifacts"]["html"]).read_text(encoding="utf-8")
    assert "Action Queue" in html_report
    assert "continue_development" in html_report


def test_operator_console_privacy_failure_becomes_next_action(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    leaked_token = "S" * 32
    (repo / "README.md").write_text(f'{{"admission_token": "{leaked_token}"}}\n', encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    invite_path = private_dir / "primary-alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:8765", admission_token="secret-token-123456"),
    )
    reliability_dir = tmp_path / "reliability-pack"
    reliability_dir.mkdir()
    _write_reliability_summary(reliability_dir, can_continue=True)

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=tmp_path / "operator-console",
            skip_network_checks=True,
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "fix_public_privacy_findings"
    assert report["summary"]["can_continue_without_partner"] is False
    assert report["action_queue"]["next_action"]["action_id"] == "fix_public_privacy_findings"
    assert report["privacy_scan"]["findings"][0]["match"] == "<redacted>"
    assert leaked_token not in json.dumps(report)


def test_operator_console_can_continue_on_backup_lane(monkeypatch, tmp_path):
    repo = _clean_repo(tmp_path)
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    primary_invite = private_dir / "primary-alpha-invite.json"
    backup_invite = private_dir / "backup-alpha-invite.json"
    write_alpha_invite(
        primary_invite,
        AlphaInvite.create(coordinator="http://127.0.0.1:8765", admission_token="token-primary-1"),
    )
    write_alpha_invite(
        backup_invite,
        AlphaInvite.create(coordinator="http://127.0.0.1:8766", admission_token="token-backup-1"),
    )

    def fake_lane_status(**kwargs):
        label = kwargs["label"]
        ready = label == "backup"
        return {
            "label": label,
            "configured": True,
            "network_checked": True,
            "ready": ready,
            "status": "pass" if ready else "fail",
            "snapshot_summary": {"live_nodes": 1 if ready else 0, "disputed_jobs": 0},
            "errors": [] if ready else ["coordinator health unreachable"],
            "warnings": [],
        }

    monkeypatch.setattr("chatp2p.operator_console._lane_status", fake_lane_status)

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=primary_invite,
            backup_invite_path=backup_invite,
            out_dir=tmp_path / "operator-console",
        )
    )

    assert report["summary"]["primary_ready"] is False
    assert report["summary"]["backup_ready"] is True
    assert report["summary"]["can_continue_without_partner"] is True
    assert report["summary"]["recommended_next_action"] == "continue_on_backup_lane"
    assert report["action_queue"]["next_action"]["action_id"] == "continue_on_backup_lane"
    assert report["action_queue"]["next_action"]["partner_required"] is False


def test_operator_console_without_reliability_pack_still_gives_next_step(monkeypatch, tmp_path):
    repo = _clean_repo(tmp_path)
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    invite_path = private_dir / "primary-alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:8765", admission_token="secret-token-123456"),
    )

    monkeypatch.setattr(
        "chatp2p.operator_console._lane_status",
        lambda **kwargs: {
            "label": kwargs["label"],
            "configured": True,
            "network_checked": True,
            "ready": True,
            "status": "pass",
            "snapshot_summary": {"live_nodes": 1, "disputed_jobs": 0},
            "errors": [],
            "warnings": [],
        },
    )

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            out_dir=tmp_path / "operator-console",
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["can_continue_without_partner"] is True
    assert report["summary"]["recommended_next_action"] == "run_reliability_pack_when_ready"


def test_operator_console_keeps_history_and_reports_changes(monkeypatch, tmp_path):
    repo = _clean_repo(tmp_path)
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    invite_path = private_dir / "primary-alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:8765", admission_token="secret-token-123456"),
    )
    out_dir = tmp_path / "operator-console"

    monkeypatch.setattr(
        "chatp2p.operator_console._lane_status",
        lambda **kwargs: {
            "label": kwargs["label"],
            "configured": True,
            "network_checked": True,
            "ready": True,
            "status": "pass",
            "snapshot_summary": {"live_nodes": 1, "disputed_jobs": 0},
            "errors": [],
            "warnings": [],
        },
    )

    first = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            out_dir=out_dir,
        )
    )
    reliability_dir = tmp_path / "reliability-pack"
    reliability_dir.mkdir()
    _write_reliability_summary(reliability_dir, can_continue=True)
    second = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=out_dir,
        )
    )

    history_path = Path(second["artifacts"]["history"])
    history = json.loads(history_path.read_text(encoding="utf-8"))
    assert first["history"]["changes"] == ["first_console_run"]
    assert history["schema"] == "chatp2p.operator-console-history.v1"
    assert len(history["entries"]) == 2
    assert any("recommended_next_action" in change for change in second["history"]["changes"])


def test_operator_console_lists_stale_reports_without_deleting_them(tmp_path):
    repo = _clean_repo(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()
    stale_report = data_root / "alpha-soak-report-old.json"
    stale_report.write_text("{}", encoding="utf-8")
    old_time = time.time() - (5 * 24 * 60 * 60)
    os.utime(stale_report, (old_time, old_time))
    fresh_report = data_root / "alpha-smoke-report-fresh.json"
    fresh_report.write_text("{}", encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    invite_path = private_dir / "primary-alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:8765", admission_token="secret-token-123456"),
    )

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=data_root / ".mesh",
            primary_invite_path=invite_path,
            out_dir=tmp_path / "operator-console",
            skip_network_checks=True,
            stale_report_root=data_root,
            stale_report_days=2.0,
        )
    )

    assert report["stale_reports"]["candidate_count"] == 1
    assert report["stale_reports"]["candidates"][0]["relative_path"] == stale_report.name
    assert stale_report.exists()
    assert fresh_report.exists()
    cleanup_plan = Path(report["artifacts"]["cleanup_plan"])
    assert cleanup_plan.exists()
    assert "Move-Item" in cleanup_plan.read_text(encoding="utf-8")


def test_operator_console_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "console",
            "--repo",
            str(tmp_path / "repo"),
            "--home",
            str(tmp_path / ".mesh"),
            "--primary-invite",
            str(tmp_path / "primary-alpha-invite.json"),
            "--backup-invite",
            str(tmp_path / "backup-alpha-invite.json"),
            "--reliability-dir",
            str(tmp_path / "reliability-pack"),
            "--out",
            str(tmp_path / "operator-console"),
            "--expected-primary-worker-id",
            "worker_PRIMARY",
            "--expected-backup-worker-id",
            "worker_BACKUP",
            "--skip-network-checks",
            "--history-limit",
            "5",
            "--stale-report-root",
            str(tmp_path / "reports"),
            "--stale-report-days",
            "7",
            "--stale-report-max-items",
            "12",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_console_command"
    assert args.skip_network_checks is True
    assert args.expected_primary_worker_id == "worker_PRIMARY"
    assert args.history_limit == 5
    assert args.stale_report_days == 7
    assert args.stale_report_max_items == 12


def _clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("ChatP2P public docs use placeholders only.\n", encoding="utf-8")
    return repo


def _write_reliability_summary(path, *, can_continue):
    report = {
        "schema": "chatp2p.alpha-reliability-pack.v1",
        "ok": can_continue,
        "status": "pass" if can_continue else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "can_continue_without_partner": can_continue,
            "recommended_mode": "primary_only" if can_continue else "blocked",
            "primary_lane_ready": can_continue,
            "backup_lane_ready": False,
            "disputed_jobs": 0,
        },
        "criteria": {
            "token_redaction": {
                "passed": True,
            }
        },
    }
    (path / "reliability-summary.json").write_text(json.dumps(report), encoding="utf-8")

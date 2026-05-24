from pathlib import Path

from chatp2p.cli import build_parser
from chatp2p.operator_daily import OperatorDailyCheckConfig, run_operator_daily_check


def test_daily_check_writes_summary_and_uses_console(monkeypatch, tmp_path):
    monkeypatch.setattr("chatp2p.operator_daily.run_public_privacy_scan", _privacy(ok=True))
    monkeypatch.setattr("chatp2p.operator_daily.run_operator_console", _console(status="pass"))

    report = run_operator_daily_check(
        OperatorDailyCheckConfig(
            repo=tmp_path / "repo",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "alpha-invite.json",
            out_dir=tmp_path / "daily",
        )
    )

    assert report["schema"] == "chatp2p.operator-daily-check-report.v1"
    assert report["status"] == "pass"
    assert report["summary"]["can_continue_without_partner"] is True
    assert report["summary"]["recommended_next_action"] == "continue_development"
    assert report["steps"]["reliability_refresh"]["status"] == "skipped"
    assert report["action_queue"]["next_action"]["action_id"] == "continue_development"
    assert Path(report["artifacts"]["json"]).exists()
    assert Path(report["artifacts"]["markdown"]).exists()
    assert Path(report["artifacts"]["action_queue_json"]).exists()
    assert Path(report["artifacts"]["action_queue_markdown"]).exists()


def test_daily_check_fails_when_privacy_scan_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("chatp2p.operator_daily.run_public_privacy_scan", _privacy(ok=False))
    monkeypatch.setattr("chatp2p.operator_daily.run_operator_console", _console(status="pass"))

    report = run_operator_daily_check(
        OperatorDailyCheckConfig(
            repo=tmp_path / "repo",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "alpha-invite.json",
            out_dir=tmp_path / "daily",
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "fix_public_privacy_findings"
    assert report["summary"]["can_continue_without_partner"] is False
    assert report["action_queue"]["next_action"]["action_id"] == "fix_public_privacy_findings"


def test_daily_check_can_refresh_reliability_pack(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("chatp2p.operator_daily.run_public_privacy_scan", _privacy(ok=True))
    monkeypatch.setattr("chatp2p.operator_daily.run_operator_console", _console(status="pass"))

    def fake_reliability(config):
        calls.append(config)
        return {
            "ok": True,
            "status": "pass",
            "summary": {
                "can_continue_without_partner": True,
                "recommended_mode": "primary_and_backup_ready",
            },
            "artifacts": {
                "summary_json": str(config.out_dir / "reliability-summary.json"),
            },
        }

    monkeypatch.setattr("chatp2p.operator_daily.run_alpha_reliability_pack", fake_reliability)

    report = run_operator_daily_check(
        OperatorDailyCheckConfig(
            repo=tmp_path / "repo",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "alpha-invite.json",
            backup_invite_path=tmp_path / "backup-alpha-invite.json",
            reliability_dir=tmp_path / "reliability",
            out_dir=tmp_path / "daily",
            refresh_reliability_pack=True,
        )
    )

    assert calls
    assert report["status"] == "pass"
    assert report["steps"]["reliability_refresh"]["status"] == "pass"
    assert Path(report["steps"]["reliability_refresh"]["report_path"]).exists()


def test_daily_check_reliability_refresh_requires_backup_and_reliability_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("chatp2p.operator_daily.run_public_privacy_scan", _privacy(ok=True))
    monkeypatch.setattr("chatp2p.operator_daily.run_operator_console", _console(status="pass"))

    report = run_operator_daily_check(
        OperatorDailyCheckConfig(
            repo=tmp_path / "repo",
            home=tmp_path / ".mesh",
            primary_invite_path=tmp_path / "alpha-invite.json",
            out_dir=tmp_path / "daily",
            refresh_reliability_pack=True,
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "repair_reliability_pack"
    assert report["steps"]["reliability_refresh"]["status"] == "fail"


def test_daily_check_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "daily-check",
            "--repo",
            str(tmp_path / "repo"),
            "--home",
            str(tmp_path / ".mesh"),
            "--primary-invite",
            str(tmp_path / "alpha-invite.json"),
            "--backup-invite",
            str(tmp_path / "backup-alpha-invite.json"),
            "--reliability-dir",
            str(tmp_path / "reliability"),
            "--out",
            str(tmp_path / "daily"),
            "--console-out",
            str(tmp_path / "console"),
            "--refresh-reliability-pack",
            "--include-deterministic-smoke",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_daily_check_command"
    assert args.refresh_reliability_pack is True
    assert args.include_deterministic_smoke is True
    assert args.console_out == str(tmp_path / "console")


def _privacy(*, ok):
    def fake(config):
        report = {
            "schema": "chatp2p.public-privacy-scan.v1",
            "ok": ok,
            "status": "pass" if ok else "fail",
            "findings": [] if ok else [{"pattern": "long_admission_token", "match": "<redacted>"}],
        }
        if config.report_path is not None:
            config.report_path.parent.mkdir(parents=True, exist_ok=True)
            config.report_path.write_text("{}", encoding="utf-8")
            report["report_path"] = str(config.report_path)
        return report

    return fake


def _console(*, status):
    def fake(config):
        return {
            "schema": "chatp2p.operator-console-report.v1",
            "ok": status != "fail",
            "status": status,
            "summary": {
                "can_continue_without_partner": status != "fail",
                "recommended_next_action": "continue_development",
            },
            "artifacts": {
                "json": str(config.out_dir / "operator-console.json"),
                "markdown": str(config.out_dir / "operator-console.md"),
                "html": str(config.out_dir / "operator-console.html"),
            },
        }

    return fake

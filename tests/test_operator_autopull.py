import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chatp2p.cli import build_parser
from chatp2p.operator_autopull import OperatorAutopullHealthConfig, run_operator_autopull_health


def test_autopull_health_working_from_synced_sync_status(tmp_path):
    revision = "a" * 40
    sync_report = _write_sync_status(
        tmp_path,
        sync_state="synced",
        expected_revision=revision,
        nodes=[_node("worker_synced", revision, status="synced")],
    )

    report = run_operator_autopull_health(
        OperatorAutopullHealthConfig(
            repo=tmp_path,
            sync_status_report_path=sync_report,
            out_dir=tmp_path / "autopull-health",
        )
    )

    assert report["schema"] == "chatp2p.operator-autopull-health-report.v1"
    assert report["status"] == "pass"
    assert report["summary"]["autopull_state"] == "autopull_working"
    assert report["summary"]["recommended_next_action"] == "partner_synced_continue"
    assert report["summary"]["partner_required"] is False
    assert Path(report["artifacts"]["json"]).exists()
    assert Path(report["artifacts"]["markdown"]).exists()


def test_autopull_health_pending_when_sync_status_waits_for_autopull(tmp_path):
    revision = "b" * 40
    sync_report = _write_sync_status(
        tmp_path,
        sync_state="waiting_for_autopull",
        expected_revision=revision,
        nodes=[_node("worker_behind", "1" * 40, status="behind")],
    )

    report = run_operator_autopull_health(
        OperatorAutopullHealthConfig(
            repo=tmp_path,
            sync_status_report_path=sync_report,
            out_dir=tmp_path / "autopull-health",
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["autopull_state"] == "autopull_pending"
    assert report["summary"]["recommended_next_action"] == "wait_for_partner_autopull"
    assert report["summary"]["can_continue_without_partner"] is True


def test_autopull_health_stale_when_partner_report_is_stale(tmp_path):
    revision = "c" * 40
    sync_report = _write_sync_status(
        tmp_path,
        sync_state="waiting_for_autopull",
        expected_revision=revision,
        nodes=[_node("worker_behind", "2" * 40, status="behind")],
    )
    partner_report = _write_partner_report(tmp_path, finished_at=_iso_now(delta=timedelta(hours=-3)))

    report = run_operator_autopull_health(
        OperatorAutopullHealthConfig(
            repo=tmp_path,
            sync_status_report_path=sync_report,
            partner_report_paths=(partner_report,),
            out_dir=tmp_path / "autopull-health",
            freshness_seconds=60.0,
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["autopull_state"] == "autopull_stale"
    assert report["summary"]["recommended_next_action"] == "refresh_operator_console_or_wait"
    assert "stale" in " ".join(report["summary"]["warnings"])


def test_autopull_health_partner_offline_when_no_live_nodes(tmp_path):
    sync_report = _write_sync_status(
        tmp_path,
        sync_state="blocked",
        expected_revision="d" * 40,
        nodes=[],
        live_node_count=0,
        status="fail",
    )

    report = run_operator_autopull_health(
        OperatorAutopullHealthConfig(
            repo=tmp_path,
            sync_status_report_path=sync_report,
            out_dir=tmp_path / "autopull-health",
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["autopull_state"] == "partner_offline"
    assert report["summary"]["recommended_next_action"] == "continue_offline_or_wait_for_partner"
    assert report["summary"]["partner_required"] is False


def test_autopull_health_does_not_echo_sensitive_partner_config(tmp_path):
    token = "secret-token-" + "123456789"
    sync_report = _write_sync_status(
        tmp_path,
        sync_state="waiting_for_autopull",
        expected_revision="e" * 40,
        nodes=[_node("worker_behind", "3" * 40, status="behind")],
    )
    partner_report = _write_partner_report(
        tmp_path,
        extra={
            "config": {
                "admission_token": token,
                "private_repo_dir": "E:\\ChatP2P-private-version-autopilot",
            }
        },
    )

    report = run_operator_autopull_health(
        OperatorAutopullHealthConfig(
            repo=tmp_path,
            sync_status_report_path=sync_report,
            partner_report_paths=(partner_report,),
            out_dir=tmp_path / "autopull-health",
        )
    )
    serialized = json.dumps(report)

    assert report["status"] == "warn"
    assert token not in serialized
    assert "admission_token" not in serialized
    assert "private_repo_dir" not in serialized


def test_operator_autopull_health_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "autopull-health",
            "--repo",
            str(tmp_path),
            "--out",
            str(tmp_path / "autopull-health"),
            "--console-report",
            str(tmp_path / "operator-console.json"),
            "--sync-status-report",
            str(tmp_path / "sync-status.json"),
            "--partner-report",
            str(tmp_path / "partner-autopilot-report.json"),
            "--freshness-seconds",
            "120",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_autopull_health_command"
    assert args.repo == str(tmp_path)
    assert args.out == str(tmp_path / "autopull-health")
    assert args.console_report == str(tmp_path / "operator-console.json")
    assert args.sync_status_report == str(tmp_path / "sync-status.json")
    assert args.partner_report == [str(tmp_path / "partner-autopilot-report.json")]
    assert args.freshness_seconds == 120.0
    assert args.json is True


def _write_sync_status(
    tmp_path,
    *,
    sync_state,
    expected_revision,
    nodes,
    live_node_count=None,
    status="warn",
):
    path = tmp_path / "sync-status.json"
    live = len(nodes) if live_node_count is None else live_node_count
    report = {
        "schema": "chatp2p.operator-sync-status-report.v1",
        "ok": status != "fail",
        "status": status,
        "generated_at": _iso_now(),
        "summary": {
            "sync_state": sync_state,
            "expected_public_revision": expected_revision,
            "expected_public_revision_short": expected_revision[:12],
            "live_node_count": live,
            "synced_live_nodes": sum(1 for node in nodes if node["revision_status"] == "synced"),
            "behind_live_nodes": sum(1 for node in nodes if node["revision_status"] == "behind"),
            "unknown_live_nodes": sum(1 for node in nodes if node["revision_status"] == "unknown"),
            "dirty_live_nodes": sum(1 for node in nodes if node["revision_status"] == "dirty"),
            "recommended_next_action": "wait_for_partner_autopull",
            "can_continue_without_partner": True,
        },
        "nodes": nodes,
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _write_partner_report(tmp_path, *, finished_at=None, extra=None):
    path = tmp_path / "partner-autopilot-report.json"
    report = {
        "schema": "chatp2p.partner-autopilot-report.v1",
        "ok": True,
        "status": "pass",
        "started_at": _iso_now(),
        "finished_at": finished_at or _iso_now(),
        "steps": [{"name": "git_pull", "ok": True, "status": "pass"}],
        "errors": [],
        "warnings": [],
    }
    report.update(extra or {})
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _node(node_id, revision, *, status):
    return {
        "lane": "primary",
        "node_id": node_id,
        "revision_status": status,
        "source_revision": revision,
        "source_revision_short": revision[:12] if revision else None,
        "source_branch": "main" if revision else None,
        "source_dirty": status == "dirty",
        "chatp2p_version": "0.1.0",
        "collected_at": _iso_now(),
    }


def _iso_now(*, delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).isoformat()

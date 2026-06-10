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
    assert report["action_runner"]["next_action"]["status"] == "available"
    assert "operator run-action" in report["action_runner"]["next_action"]["dry_run_command"]
    assert report["self_heal"]["status"] == "missing"
    for key in ("json", "markdown", "html", "action_queue_json", "action_queue_markdown"):
        artifact = Path(report["artifacts"][key])
        assert artifact.exists()
        assert token not in artifact.read_text(encoding="utf-8")
    html_report = Path(report["artifacts"]["html"]).read_text(encoding="utf-8")
    assert "Action Queue" in html_report
    assert "Run Next Action" in html_report
    assert "Self-Heal" in html_report
    assert "operator self-heal" in html_report
    assert "operator run-action" in html_report
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


def test_operator_console_marks_live_node_revision_states(monkeypatch, tmp_path):
    repo = _clean_repo(tmp_path)
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
    expected_revision = "b" * 40

    monkeypatch.setattr(
        "chatp2p.operator_console.collect_software_metadata",
        lambda repo: _software_metadata(expected_revision, dirty=False),
    )
    monkeypatch.setattr(
        "chatp2p.operator_console._lane_status",
        lambda **kwargs: {
            "label": kwargs["label"],
            "configured": True,
            "network_checked": True,
            "ready": True,
            "status": "pass",
            "snapshot_summary": {"live_nodes": 4, "disputed_jobs": 0},
            "software_nodes": [
                _software_node("worker_synced", expected_revision),
                _software_node("worker_behind", "a" * 40),
                _software_node("worker_unknown", None),
                _software_node("worker_dirty", expected_revision, dirty=True),
            ],
            "errors": [],
            "warnings": [],
        },
    )

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=tmp_path / "operator-console",
        )
    )

    primary_sync = report["software"]["lanes"]["primary"]
    assert report["summary"]["recommended_next_action"] == "wait_for_partner_autopull"
    assert report["action_queue"]["next_action"]["action_id"] == "wait_for_partner_autopull"
    assert report["action_queue"]["next_action"]["partner_required"] is False
    assert primary_sync["synced_live_nodes"] == 1
    assert primary_sync["behind_live_nodes"] == 1
    assert primary_sync["unknown_live_nodes"] == 1
    assert primary_sync["dirty_live_nodes"] == 1
    assert "older or different public revision" in " ".join(report["summary"]["warnings"])
    assert "software revision metadata" in " ".join(report["summary"]["warnings"])
    assert "dirty source checkout" in " ".join(report["summary"]["warnings"])


def test_operator_console_recommends_synced_continue(monkeypatch, tmp_path):
    repo = _clean_repo(tmp_path)
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
    expected_revision = "c" * 40

    monkeypatch.setattr(
        "chatp2p.operator_console.collect_software_metadata",
        lambda repo: _software_metadata(expected_revision, dirty=False),
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
            "software_nodes": [_software_node("worker_synced", expected_revision)],
            "errors": [],
            "warnings": [],
        },
    )

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=tmp_path / "operator-console",
        )
    )

    assert report["software"]["all_live_nodes_synced"] is True
    assert report["summary"]["recommended_next_action"] == "partner_synced_continue"
    assert report["action_queue"]["next_action"]["action_id"] == "partner_synced_continue"


def test_operator_console_bounds_live_snapshot_payload(monkeypatch, tmp_path):
    repo = _clean_repo(tmp_path)
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    invite_path = private_dir / "primary-alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:8765", admission_token="secret-token-123456"),
    )

    nodes = [
        {
            "node_id": f"worker_{index:016x}",
            "node_role": "worker",
            "liveness_status": "live" if index < 2 else "offline",
            "public_key": "must-not-appear",
            "hardware": {"processor": "too much detail"},
            "supported_job_types": ["inference.echo.v1"],
            "software": {
                "source_revision": "d" * 40,
                "source_branch": "main",
                "source_dirty": False,
                "source_remote_url_redacted": "https://github.com/Martin123132/ChatP2P.git",
                "private_extra": "must-not-appear",
            },
        }
        for index in range(8)
    ]
    jobs = [
        {
            "job_id": f"job_{index}",
            "job_type": "inference.echo.v1",
            "status": "verified",
            "payload": {"prompt": "large private prompt must not appear"},
            "leases": [{"grant_hash": "must-not-appear"}],
            "routing": {"eligible_node_count": 1, "live_eligible_node_count": 1},
        }
        for index in range(12)
    ]
    results = [
        {
            "job_id": f"job_{index}",
            "job_type": "inference.echo.v1",
            "node_id": "worker_0000000000000000",
            "output": {"answer": "large model output must not appear"},
        }
        for index in range(12)
    ]

    def fake_client_call(call, *, url):
        if url.endswith("/api/snapshot"):
            return {
                "ok": True,
                "status": "pass",
                "url": url,
                "payload": {
                    "status": {
                        "known_nodes": len(nodes),
                        "live_nodes": 2,
                        "offline_nodes": 6,
                        "disputed_jobs": 0,
                        "jobs": len(jobs),
                        "verified_jobs": len(jobs),
                    },
                    "nodes": nodes,
                    "jobs": jobs,
                    "results": results,
                    "reputation": [{"node_id": "worker_0000000000000000"}],
                },
            }
        return {"ok": True, "status": "pass", "url": url, "payload": {"ok": True}}

    monkeypatch.setattr("chatp2p.operator_console._client_call", fake_client_call)

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            out_dir=tmp_path / "operator-console",
            query_daily_check_task=False,
        )
    )

    snapshot = report["lanes"]["primary"]["snapshot"]["payload"]
    assert report["lanes"]["primary"]["snapshot_summary"]["jobs"] == 12
    assert snapshot["counts"] == {"nodes": 8, "jobs": 12, "results": 12, "reputation": 1}
    assert len(snapshot["nodes"]) == 5
    assert len(snapshot["jobs"]) == 5
    assert len(snapshot["results"]) == 5
    assert snapshot["truncated"] == {"nodes": True, "jobs": True, "results": True, "reputation": True}

    serialized = json.dumps(report)
    assert "large private prompt must not appear" not in serialized
    assert "large model output must not appear" not in serialized
    assert "grant_hash" not in serialized
    assert "public_key" not in serialized
    assert "too much detail" not in serialized
    assert "must-not-appear" not in serialized
    assert snapshot["nodes"][0]["software"]["source_revision"] == "d" * 40


def test_operator_console_reports_daily_check_automation(monkeypatch, tmp_path):
    repo = _clean_repo(tmp_path)
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
    daily_dir = tmp_path / "daily-check"
    daily_dir.mkdir()
    _write_daily_check_report(daily_dir)

    monkeypatch.setattr(
        "chatp2p.operator_console._query_daily_check_task",
        lambda task_name, *, enabled: {
            "configured": True,
            "task_name": task_name,
            "queried": enabled,
            "status": "pass",
            "ok": True,
            "task_status": "Ready",
            "next_run_time": "24/05/2026 03:20:00",
            "last_run_time": "24/05/2026 02:20:00",
            "last_result": "0",
        },
    )

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=tmp_path / "operator-console",
            daily_check_dir=daily_dir,
            skip_network_checks=True,
        )
    )

    daily_check = report["daily_check_automation"]
    assert daily_check["status"] == "pass"
    assert daily_check["task"]["task_status"] == "Ready"
    assert daily_check["report"]["recommended_next_action"] == "continue_development"
    html_report = Path(report["artifacts"]["html"]).read_text(encoding="utf-8")
    assert "Scheduled Automation" in html_report
    assert "Daily check automation" in html_report


def test_operator_console_reports_model_release_status(tmp_path):
    repo = _clean_repo(tmp_path)
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
    status_path = tmp_path / "model-release-status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema": "chatp2p.model-release-status-report.v1",
                "ok": True,
                "status": "warn",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "model_id": "qwen2.5-7b-instruct",
                    "pipeline_stage": "runtime_check_needed",
                    "release_ready": False,
                    "blocked_gate_ids": ["runtime"],
                    "next_action_id": "runtime_check",
                    "recommended_next_action": "run_model_runtime_check",
                },
            }
        ),
        encoding="utf-8",
    )

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=tmp_path / "operator-console",
            model_release_status_path=status_path,
            skip_network_checks=True,
        )
    )

    assert report["model_release"]["status"] == "warn"
    assert report["model_release"]["pipeline_stage"] == "runtime_check_needed"
    assert report["summary"]["model_release_next_action"] == "run_model_runtime_check"
    html_report = Path(report["artifacts"]["html"]).read_text(encoding="utf-8")
    assert "Model Release" in html_report
    assert "runtime_check_needed" in html_report


def test_operator_console_reports_model_route_plan(tmp_path):
    repo = _clean_repo(tmp_path)
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
    route_plan_path = tmp_path / "model-route-plan.json"
    route_plan_path.write_text(
        json.dumps(
            {
                "schema": "chatp2p.model-route-plan-report.v1",
                "ok": True,
                "status": "pass",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "selected_model_id": "qwen2.5-7b-instruct",
                    "recommended_chat_model": "qwen2.5-7b-instruct",
                    "route_ready": True,
                    "network_checked": True,
                    "coordinator_reachable": True,
                    "live_model_capable_worker_count": 1,
                    "approved_model_count": 1,
                    "routeable_model_count": 1,
                    "recommended_next_action": "continue_chat_session_with_route_plan",
                },
            }
        ),
        encoding="utf-8",
    )

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=tmp_path / "operator-console",
            model_route_plan_path=route_plan_path,
            skip_network_checks=True,
        )
    )

    assert report["model_route_plan"]["status"] == "pass"
    assert report["model_route_plan"]["route_ready"] is True
    assert report["summary"]["model_route_plan_next_action"] == "continue_chat_session_with_route_plan"
    html_report = Path(report["artifacts"]["html"]).read_text(encoding="utf-8")
    assert "Model Route Plan" in html_report
    assert "qwen2.5-7b-instruct" in html_report


def test_operator_console_reports_last_action_run(tmp_path):
    repo = _clean_repo(tmp_path)
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
    out_dir = tmp_path / "operator-console"
    out_dir.mkdir()
    _write_action_run_report(out_dir)

    report = run_operator_console(
        OperatorConsoleConfig(
            repo=repo,
            home=tmp_path / ".mesh",
            primary_invite_path=invite_path,
            reliability_dir=reliability_dir,
            out_dir=out_dir,
            skip_network_checks=True,
        )
    )

    last_run = report["action_runner"]["last_run"]
    assert last_run["status"] == "pass"
    assert last_run["action_id"] == "continue_development"
    assert last_run["fresh"] is True
    html_report = Path(report["artifacts"]["html"]).read_text(encoding="utf-8")
    assert "Last run status" in html_report
    assert "operator-action-run-report.json" in html_report


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
            "--expected-public-revision",
            "abc123",
            "--skip-network-checks",
            "--history-limit",
            "5",
            "--stale-report-root",
            str(tmp_path / "reports"),
            "--stale-report-days",
            "7",
            "--stale-report-max-items",
            "12",
            "--daily-check-dir",
            str(tmp_path / "daily-check"),
            "--daily-check-task-name",
            "ChatP2P Daily Check Test",
            "--skip-daily-check-task-query",
            "--model-release-status",
            str(tmp_path / "model-release-status.json"),
            "--model-route-plan",
            str(tmp_path / "model-route-plan.json"),
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_console_command"
    assert args.skip_network_checks is True
    assert args.expected_primary_worker_id == "worker_PRIMARY"
    assert args.expected_public_revision == "abc123"
    assert args.history_limit == 5
    assert args.stale_report_days == 7
    assert args.stale_report_max_items == 12
    assert args.daily_check_dir == str(tmp_path / "daily-check")
    assert args.daily_check_task_name == "ChatP2P Daily Check Test"
    assert args.skip_daily_check_task_query is True
    assert args.model_release_status == str(tmp_path / "model-release-status.json")
    assert args.model_route_plan == str(tmp_path / "model-route-plan.json")


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


def _write_daily_check_report(path):
    report = {
        "schema": "chatp2p.operator-daily-check-report.v1",
        "ok": True,
        "status": "pass",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "can_continue_without_partner": True,
            "recommended_next_action": "continue_development",
            "warnings": [],
            "errors": [],
        },
        "action_queue": {
            "next_action": {
                "action_id": "continue_development",
            }
        },
    }
    (path / "daily-check.json").write_text(json.dumps(report), encoding="utf-8")


def _software_metadata(revision, *, dirty):
    return {
        "chatp2p_version": "0.1.0",
        "source_revision": revision,
        "source_branch": "main",
        "source_dirty": dirty,
        "source_remote_url_redacted": "https://github.com/Martin123132/ChatP2P.git",
        "source_status": "git",
        "collected_at": "2026-06-04T00:00:00+00:00",
    }


def _software_node(node_id, revision, *, dirty=False):
    software = _software_metadata(revision, dirty=dirty) if revision is not None else {}
    return {
        "node_id": node_id,
        "liveness_status": "live",
        "software": software,
    }


def _write_action_run_report(path):
    report = {
        "schema": "chatp2p.operator-action-run-report.v1",
        "ok": True,
        "status": "pass",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "action": {
            "action_id": "continue_development",
        },
        "command": {
            "label": "Recheck privacy before committing",
        },
        "execution": {
            "dry_run": False,
            "attempted": True,
            "returncode": 0,
        },
    }
    (path / "operator-action-run-report.json").write_text(json.dumps(report), encoding="utf-8")

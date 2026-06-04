import json
from pathlib import Path

import pytest

from chatp2p import cli as cli_module
from chatp2p.cli import build_parser
from chatp2p.operator_sync import OperatorSyncStatusConfig, run_operator_sync_status


def test_sync_status_synced_writes_reports(monkeypatch, tmp_path):
    revision = "a" * 40
    console_path = _write_console_report(
        tmp_path,
        expected_revision=revision,
        nodes=[_node("worker_synced", revision, status="synced")],
    )
    monkeypatch.setattr(cli_module, "run_operator_sync_status", run_operator_sync_status)
    _patch_local_metadata(monkeypatch, revision)

    report = run_operator_sync_status(
        OperatorSyncStatusConfig(
            repo=tmp_path,
            console_report_path=console_path,
            out_dir=tmp_path / "sync-status",
        )
    )

    assert report["schema"] == "chatp2p.operator-sync-status-report.v1"
    assert report["status"] == "pass"
    assert report["summary"]["sync_state"] == "synced"
    assert report["summary"]["recommended_next_action"] == "partner_synced_continue"
    assert Path(report["artifacts"]["json"]).exists()
    assert Path(report["artifacts"]["markdown"]).exists()


def test_sync_status_waiting_for_autopull_when_node_is_behind(monkeypatch, tmp_path):
    expected_revision = "b" * 40
    console_path = _write_console_report(
        tmp_path,
        expected_revision=expected_revision,
        nodes=[_node("worker_behind", "1" * 40, status="behind")],
    )
    _patch_local_metadata(monkeypatch, expected_revision)

    report = run_operator_sync_status(
        OperatorSyncStatusConfig(
            repo=tmp_path,
            console_report_path=console_path,
            out_dir=tmp_path / "sync-status",
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["sync_state"] == "waiting_for_autopull"
    assert report["summary"]["behind_live_nodes"] == 1
    assert report["summary"]["recommended_next_action"] == "wait_for_partner_autopull"


def test_sync_status_unknown_old_worker_for_missing_revision(monkeypatch, tmp_path):
    expected_revision = "c" * 40
    console_path = _write_console_report(
        tmp_path,
        expected_revision=expected_revision,
        nodes=[_node("worker_unknown", None, status="unknown")],
    )
    _patch_local_metadata(monkeypatch, expected_revision)

    report = run_operator_sync_status(
        OperatorSyncStatusConfig(
            repo=tmp_path,
            console_report_path=console_path,
            out_dir=tmp_path / "sync-status",
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["sync_state"] == "unknown_old_worker"
    assert report["summary"]["unknown_live_nodes"] == 1


def test_sync_status_local_dirty_is_warning_not_blocker(monkeypatch, tmp_path):
    expected_revision = "1" * 40
    console_path = _write_console_report(
        tmp_path,
        expected_revision=expected_revision,
        nodes=[_node("worker_synced", expected_revision, status="synced")],
    )
    _patch_local_metadata(monkeypatch, expected_revision, dirty=True)

    report = run_operator_sync_status(
        OperatorSyncStatusConfig(
            repo=tmp_path,
            console_report_path=console_path,
            out_dir=tmp_path / "sync-status",
            expected_public_revision=expected_revision,
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["sync_state"] == "synced"
    assert "local public repo checkout is dirty" in " ".join(report["summary"]["warnings"])


def test_sync_status_blocks_when_console_has_no_live_nodes(monkeypatch, tmp_path):
    revision = "d" * 40
    console_path = _write_console_report(tmp_path, expected_revision=revision, nodes=[])
    _patch_local_metadata(monkeypatch, revision)

    report = run_operator_sync_status(
        OperatorSyncStatusConfig(
            repo=tmp_path,
            console_report_path=console_path,
            out_dir=tmp_path / "sync-status",
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["sync_state"] == "blocked"
    assert "no live node revision metadata" in " ".join(report["summary"]["errors"])


def test_sync_status_report_does_not_echo_sensitive_console_fields(monkeypatch, tmp_path):
    revision = "e" * 40
    fake_token = "super-secret-" + "token-123456789"
    console_path = _write_console_report(
        tmp_path,
        expected_revision=revision,
        nodes=[_node("worker_synced", revision, status="synced")],
        extra_config={"admission_token": fake_token},
    )
    _patch_local_metadata(monkeypatch, revision)

    report = run_operator_sync_status(
        OperatorSyncStatusConfig(
            repo=tmp_path,
            console_report_path=console_path,
            out_dir=tmp_path / "sync-status",
        )
    )
    serialized = json.dumps(report)

    assert report["status"] == "pass"
    assert fake_token not in serialized
    assert "admission_token" not in serialized


def test_operator_sync_status_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "sync-status",
            "--repo",
            str(tmp_path),
            "--console-report",
            str(tmp_path / "operator-console.json"),
            "--out",
            str(tmp_path / "sync-status"),
            "--expected-public-revision",
            "abc123",
            "--autopull-stale-minutes",
            "15",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_sync_status_command"
    assert args.repo == str(tmp_path)
    assert args.console_report == str(tmp_path / "operator-console.json")
    assert args.out == str(tmp_path / "sync-status")
    assert args.expected_public_revision == "abc123"
    assert args.autopull_stale_minutes == 15.0
    assert args.json is True


def test_operator_sync_status_cli_exits_nonzero_on_blocked(monkeypatch, tmp_path, capsys):
    revision = "f" * 40
    console_path = _write_console_report(tmp_path, expected_revision=revision, nodes=[])
    _patch_local_metadata(monkeypatch, revision)
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "sync-status",
            "--repo",
            str(tmp_path),
            "--console-report",
            str(console_path),
            "--out",
            str(tmp_path / "sync-status"),
            "--json",
        ]
    )

    with pytest.raises(SystemExit) as exc:
        cli_module.operator_sync_status_command(args)

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "chatp2p.operator-sync-status-report.v1" in output


def _patch_local_metadata(monkeypatch, revision, *, dirty=False):
    monkeypatch.setattr(
        "chatp2p.operator_sync.collect_software_metadata",
        lambda repo: {
            "chatp2p_version": "0.1.0",
            "source_revision": revision,
            "source_branch": "main",
            "source_dirty": dirty,
            "source_remote_url_redacted": "https://github.com/Martin123132/ChatP2P.git",
            "source_status": "git",
            "collected_at": "2026-05-24T00:00:00+00:00",
        },
    )
    monkeypatch.setattr("chatp2p.operator_sync._local_tracking_revision", lambda repo: revision)


def _write_console_report(tmp_path, *, expected_revision, nodes, extra_config=None):
    console_path = tmp_path / "operator-console.json"
    config = {
        "repo": str(tmp_path),
        "out_dir": str(tmp_path / "operator-console"),
        "expected_public_revision": expected_revision,
    }
    config.update(extra_config or {})
    report = {
        "schema": "chatp2p.operator-console-report.v1",
        "status": "pass",
        "generated_at": "2026-05-24T00:00:00+00:00",
        "config": config,
        "software": {
            "status": "pass",
            "expected_public_revision": expected_revision,
            "live_node_count": len(nodes),
            "synced_live_nodes": sum(1 for node in nodes if node["revision_status"] == "synced"),
            "behind_live_nodes": sum(1 for node in nodes if node["revision_status"] == "behind"),
            "unknown_live_nodes": sum(1 for node in nodes if node["revision_status"] == "unknown"),
            "dirty_live_nodes": sum(1 for node in nodes if node["revision_status"] == "dirty"),
            "lanes": {
                "primary": {
                    "status": "pass",
                    "expected_public_revision": expected_revision,
                    "live_node_count": len(nodes),
                    "synced_live_nodes": sum(1 for node in nodes if node["revision_status"] == "synced"),
                    "behind_live_nodes": sum(1 for node in nodes if node["revision_status"] == "behind"),
                    "unknown_live_nodes": sum(1 for node in nodes if node["revision_status"] == "unknown"),
                    "dirty_live_nodes": sum(1 for node in nodes if node["revision_status"] == "dirty"),
                    "nodes": nodes,
                },
                "backup": {
                    "status": "unknown",
                    "expected_public_revision": expected_revision,
                    "live_node_count": 0,
                    "synced_live_nodes": 0,
                    "behind_live_nodes": 0,
                    "unknown_live_nodes": 0,
                    "dirty_live_nodes": 0,
                    "nodes": [],
                },
            },
        },
        "artifacts": {
            "json": str(console_path),
        },
    }
    console_path.write_text(json.dumps(report), encoding="utf-8")
    return console_path


def _node(node_id, revision, *, status):
    return {
        "node_id": node_id,
        "revision_status": status,
        "source_revision": revision,
        "source_revision_short": revision[:12] if revision else None,
        "source_branch": "main" if revision else None,
        "source_dirty": status == "dirty",
        "chatp2p_version": "0.1.0",
        "collected_at": "2026-05-24T00:00:00+00:00",
    }

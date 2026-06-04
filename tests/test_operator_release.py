import json

from chatp2p.cli import build_parser
from chatp2p.operator_release import OperatorReleaseCheckConfig, run_operator_release_check


def test_release_check_ready_to_push(monkeypatch, tmp_path):
    _patch_release_inputs(monkeypatch, revision="b" * 40, origin_revision="a" * 40, counts="0 1")

    report = run_operator_release_check(
        OperatorReleaseCheckConfig(
            repo=tmp_path,
            out_dir=tmp_path / "release-check",
        )
    )

    assert report["schema"] == "chatp2p.operator-release-check-report.v1"
    assert report["status"] == "pass"
    assert report["summary"]["publish_state"] == "ready_to_push"
    assert report["summary"]["can_push"] is True
    assert report["summary"]["recommended_next_action"] == "push_origin_main"
    assert (tmp_path / "release-check" / "release-check.json").exists()
    assert (tmp_path / "release-check" / "release-check.md").exists()


def test_release_check_already_published(monkeypatch, tmp_path):
    revision = "c" * 40
    _patch_release_inputs(monkeypatch, revision=revision, origin_revision=revision, counts="0 0")

    report = run_operator_release_check(
        OperatorReleaseCheckConfig(
            repo=tmp_path,
            out_dir=tmp_path / "release-check",
        )
    )

    assert report["status"] == "pass"
    assert report["summary"]["publish_state"] == "already_published"
    assert report["summary"]["recommended_next_action"] == "continue_development"


def test_release_check_blocks_dirty_worktree(monkeypatch, tmp_path):
    _patch_release_inputs(monkeypatch, revision="b" * 40, origin_revision="a" * 40, counts="0 1", dirty=True)

    report = run_operator_release_check(
        OperatorReleaseCheckConfig(
            repo=tmp_path,
            out_dir=tmp_path / "release-check",
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["publish_state"] == "blocked"
    assert report["summary"]["recommended_next_action"] == "commit_or_stash_changes"
    assert "uncommitted changes" in " ".join(report["summary"]["errors"])


def test_release_check_blocks_when_behind_origin(monkeypatch, tmp_path):
    _patch_release_inputs(monkeypatch, revision="a" * 40, origin_revision="b" * 40, counts="1 0")

    report = run_operator_release_check(
        OperatorReleaseCheckConfig(
            repo=tmp_path,
            out_dir=tmp_path / "release-check",
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "sync_with_origin_main"
    assert "behind origin/main" in " ".join(report["summary"]["errors"])


def test_release_check_blocks_privacy_findings(monkeypatch, tmp_path):
    _patch_release_inputs(
        monkeypatch,
        revision="b" * 40,
        origin_revision="a" * 40,
        counts="0 1",
        dirty=True,
        privacy_ok=False,
    )

    report = run_operator_release_check(
        OperatorReleaseCheckConfig(
            repo=tmp_path,
            out_dir=tmp_path / "release-check",
        )
    )

    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "fix_public_privacy_findings"
    assert report["privacy_scan"]["finding_count"] == 1
    assert "uncommitted changes" in " ".join(report["summary"]["errors"])


def test_release_check_warns_when_sync_status_waiting(monkeypatch, tmp_path):
    _patch_release_inputs(monkeypatch, revision="b" * 40, origin_revision="a" * 40, counts="0 1")
    sync_report = tmp_path / "sync-status.json"
    sync_report.write_text(
        json.dumps(
            {
                "schema": "chatp2p.operator-sync-status-report.v1",
                "status": "warn",
                "summary": {
                    "sync_state": "waiting_for_autopull",
                    "recommended_next_action": "wait_for_partner_autopull",
                },
            }
        ),
        encoding="utf-8",
    )

    report = run_operator_release_check(
        OperatorReleaseCheckConfig(
            repo=tmp_path,
            out_dir=tmp_path / "release-check",
            sync_status_report_path=sync_report,
        )
    )

    assert report["status"] == "warn"
    assert report["summary"]["publish_state"] == "ready_to_push"
    assert report["summary"]["can_push"] is True
    assert "revision sync is not confirmed" in " ".join(report["summary"]["warnings"])


def test_operator_release_check_cli_parses(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "operator",
            "release-check",
            "--repo",
            str(tmp_path),
            "--out",
            str(tmp_path / "release-check"),
            "--console-report",
            str(tmp_path / "operator-console.json"),
            "--sync-status-report",
            str(tmp_path / "sync-status.json"),
            "--allow-provider-config-filenames",
            "--json",
        ]
    )

    assert args.func.__name__ == "operator_release_check_command"
    assert args.repo == str(tmp_path)
    assert args.out == str(tmp_path / "release-check")
    assert args.console_report == str(tmp_path / "operator-console.json")
    assert args.sync_status_report == str(tmp_path / "sync-status.json")
    assert args.allow_provider_config_filenames is True
    assert args.json is True


def _patch_release_inputs(
    monkeypatch,
    *,
    revision,
    origin_revision,
    counts,
    dirty=False,
    privacy_ok=True,
):
    monkeypatch.setattr(
        "chatp2p.operator_release.collect_software_metadata",
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

    def fake_git_output(git, cwd, *args):
        if args == ("rev-parse", "--verify", "origin/main"):
            return origin_revision
        if args == ("rev-parse", "--verify", "origin/HEAD"):
            return origin_revision
        if args == ("rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return counts
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/Martin123132/ChatP2P.git"
        return None

    monkeypatch.setattr("chatp2p.operator_release._git_output", fake_git_output)
    monkeypatch.setattr("chatp2p.operator_release.shutil.which", lambda name: "git")
    monkeypatch.setattr(
        "chatp2p.operator_release.run_public_privacy_scan",
        lambda config: {
            "schema": "chatp2p.public-privacy-scan.v1",
            "ok": privacy_ok,
            "status": "pass" if privacy_ok else "fail",
            "findings": [] if privacy_ok else [{"pattern": "example", "match": "<redacted>"}],
            "scanned_files": 10,
        },
    )

import shutil
import subprocess

import pytest

from chatp2p.gremlinchat.config import ApprovedRepo, RunbookPolicy
from chatp2p.gremlinchat.runbooks import execute_runbook


def test_runbooks_reject_arbitrary_commands_and_unapproved_writes(tmp_path):
    policy = RunbookPolicy()

    arbitrary = execute_runbook("powershell Remove-Item", {}, policy=policy, home=tmp_path)
    assert arbitrary.accepted is False
    assert arbitrary.status == "rejected"
    assert "arbitrary" in arbitrary.summary

    unapproved_write = execute_runbook("repo.pull_ff_only", {"repo_path": str(tmp_path)}, policy=policy, home=tmp_path)
    assert unapproved_write.accepted is False
    assert "not enabled" in unapproved_write.summary


def test_repo_runbooks_are_path_locked(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    outside_path = tmp_path / "outside"
    outside_path.mkdir()
    policy = RunbookPolicy(approved_repos=[ApprovedRepo(name="repo", path=str(repo_path))])

    outside = execute_runbook(
        "repo.status",
        {"repo": "repo", "repo_path": str(outside_path)},
        policy=policy,
        home=tmp_path,
    )
    assert outside.accepted is False
    assert "escapes" in outside.summary

    unknown = execute_runbook("repo.status", {"repo_path": str(outside_path)}, policy=policy, home=tmp_path)
    assert unknown.accepted is False
    assert "not approved" in unknown.summary


def test_write_runbooks_require_repo_specific_approval(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    policy = RunbookPolicy(
        approved_repos=[ApprovedRepo(name="repo", path=str(repo_path), allow_pull_ff_only=False)],
        enabled_write_runbooks=["repo.pull_ff_only", "tests.run_allowlisted"],
        allowlisted_tests={"unit": ["python", "-c", "print('ok')"]},
    )

    pull = execute_runbook("repo.pull_ff_only", {"repo": "repo"}, policy=policy, home=tmp_path)
    assert pull.accepted is False
    assert "not enabled for repo" in pull.summary

    test = execute_runbook(
        "tests.run_allowlisted",
        {"repo": "repo", "test_name": "unit"},
        policy=policy,
        home=tmp_path,
    )
    assert test.accepted is False
    assert "not enabled for repo" in test.summary


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for repo runbook tests")
def test_repo_status_and_pull_policy_blocks_dirty_tree(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "-C", str(repo_path), "init"], check=True, capture_output=True, text=True)
    (repo_path / "work.txt").write_text("local change", encoding="utf-8")

    policy = RunbookPolicy(
        approved_repos=[ApprovedRepo(name="repo", path=str(repo_path), allow_pull_ff_only=True)],
        enabled_write_runbooks=["repo.pull_ff_only"],
    )

    status = execute_runbook("repo.status", {"repo": "repo"}, policy=policy, home=tmp_path)
    assert status.accepted is True
    assert status.output["repo_path"] == str(repo_path.resolve())
    assert status.output["clean"] is False

    pull = execute_runbook("repo.pull_ff_only", {"repo": "repo"}, policy=policy, home=tmp_path)
    assert pull.accepted is False
    assert "local changes" in pull.summary

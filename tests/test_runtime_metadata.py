import subprocess

import pytest

from chatp2p import runtime_metadata
from chatp2p.runtime_metadata import collect_software_metadata, redact_remote_url


def test_collect_software_metadata_handles_missing_git(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_metadata.shutil, "which", lambda command: None)

    report = collect_software_metadata(tmp_path)

    assert report["source_status"] == "git_unavailable"
    assert report["source_revision"] is None
    assert report["source_dirty"] is None
    assert report["source_remote_url_redacted"] is None
    assert report["collected_at"]


def test_collect_software_metadata_handles_non_git_directory(tmp_path):
    report = collect_software_metadata(tmp_path)

    assert report["source_status"] in {"not_git", "git_unavailable"}
    assert report["source_revision"] is None
    assert report["source_branch"] is None


def test_collect_software_metadata_from_git_checkout(tmp_path):
    if runtime_metadata.shutil.which("git") is None:
        pytest.skip("git is not available")
    _git(tmp_path, "init")
    (tmp_path / "README.md").write_text("ChatP2P\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _git(tmp_path, "remote", "add", "origin", "https://user:pass@github.com/Martin123132/ChatP2P.git")

    clean = collect_software_metadata(tmp_path)
    (tmp_path / "README.md").write_text("ChatP2P dirty\n", encoding="utf-8")
    dirty = collect_software_metadata(tmp_path)

    assert clean["source_status"] == "git"
    assert len(clean["source_revision"]) == 40
    assert clean["source_branch"] in {"main", "master"}
    assert clean["source_dirty"] is False
    assert clean["source_remote_url_redacted"] == "https://github.com/Martin123132/ChatP2P.git"
    assert dirty["source_dirty"] is True


def test_redact_remote_url_removes_credentials_and_local_paths():
    assert (
        redact_remote_url("https://user:pass@github.com/Martin123132/ChatP2P.git?unused=1")
        == "https://github.com/Martin123132/ChatP2P.git"
    )
    assert redact_remote_url("git@github.com:Martin123132/ChatP2P.git") == "github.com:Martin123132/ChatP2P.git"
    assert redact_remote_url("file:///C:/Users/example/private") == "<local-path-redacted>"
    assert redact_remote_url("C:\\Users\\example\\private") == "<local-path-redacted>"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)

"""Privacy-safe software/runtime metadata for node capability reports."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def collect_software_metadata(source_root: Path | None = None) -> dict[str, Any]:
    """Collect local package and git revision metadata without network access."""

    root = _resolve_source_root(source_root)
    git = shutil.which("git")
    revision = None
    branch = None
    dirty = None
    remote = None
    source_status = "not_git"

    if git is None:
        source_status = "git_unavailable"
    else:
        revision = _git_output(git, root, "rev-parse", "HEAD")
        if revision:
            source_status = "git"
            branch = _git_output(git, root, "rev-parse", "--abbrev-ref", "HEAD")
            dirty = bool(_git_output(git, root, "status", "--porcelain"))
            remote = redact_remote_url(_git_output(git, root, "remote", "get-url", "origin"))

    return {
        "chatp2p_version": _package_version(),
        "source_revision": revision,
        "source_branch": branch,
        "source_dirty": dirty,
        "source_remote_url_redacted": remote,
        "source_status": source_status,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def redact_remote_url(raw_url: str | None) -> str | None:
    """Return a remote URL safe enough for public reports."""

    if raw_url is None:
        return None
    value = raw_url.strip()
    if not value:
        return None

    # Local remotes can leak machine names or private drive layouts.
    if value.startswith((".", "/", "\\")) or ":" in Path(value).drive:
        return "<local-path-redacted>"
    lowered = value.lower()
    if lowered.startswith("file://"):
        return "<local-path-redacted>"

    # scp-like remotes: git@github.com:owner/repo.git
    if "@" in value and "://" not in value:
        _, _, remainder = value.partition("@")
        return remainder or "<redacted-remote>"

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-remote>"

    if not parsed.scheme or not parsed.netloc:
        return value

    hostname = parsed.hostname or parsed.netloc.rsplit("@", 1)[-1]
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def software_metadata_public_view(metadata_value: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the safe revision fields exposed in snapshots and reports."""

    metadata_value = metadata_value if isinstance(metadata_value, dict) else {}
    return {
        "chatp2p_version": metadata_value.get("chatp2p_version"),
        "source_revision": metadata_value.get("source_revision"),
        "source_branch": metadata_value.get("source_branch"),
        "source_dirty": metadata_value.get("source_dirty"),
        "source_remote_url_redacted": metadata_value.get("source_remote_url_redacted"),
        "source_status": metadata_value.get("source_status"),
        "collected_at": metadata_value.get("collected_at"),
    }


def _resolve_source_root(source_root: Path | None) -> Path:
    if source_root is not None:
        return source_root.expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _package_version() -> str | None:
    try:
        return metadata.version("chatp2p")
    except metadata.PackageNotFoundError:
        return None


def _git_output(git: str, cwd: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            [git, *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None

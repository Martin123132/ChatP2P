"""GremlinChat task report writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ensure_home
from .redaction import redact_value


def reports_dir(home: Path | None) -> Path:
    path = ensure_home(home) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_task_report(home: Path | None, report: dict[str, Any]) -> dict[str, str]:
    safe_report = redact_value(report)
    task_id = str(safe_report.get("task_id") or safe_report.get("result", {}).get("task_id") or "unknown")
    base = reports_dir(home) / task_id
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(safe_report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_markdown_report(safe_report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def _markdown_report(report: dict[str, Any]) -> str:
    result = report.get("result", {})
    task_id = report.get("task_id", result.get("task_id", "unknown"))
    runbook = report.get("runbook", result.get("runbook", "unknown"))
    status = result.get("status", report.get("status", "unknown"))
    summary = result.get("summary", report.get("summary", ""))
    accepted = result.get("accepted", report.get("accepted", ""))
    return "\n".join(
        [
            f"# GremlinChat Task Report: {task_id}",
            "",
            f"- Runbook: `{runbook}`",
            f"- Status: `{status}`",
            f"- Accepted: `{accepted}`",
            f"- Summary: {summary}",
            "",
            "## JSON",
            "",
            "```json",
            json.dumps(report, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


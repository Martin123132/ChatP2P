import re
from pathlib import Path

from chatp2p.cli import build_parser
from chatp2p.privacy import PrivacyScanConfig, run_public_privacy_scan


PUBLIC_DOC_PATHS = [Path("README.md"), *Path("docs").glob("*.md")]

FORBIDDEN_PUBLIC_DOC_PATTERNS = {
    "exact worker ids": re.compile(r"\bworker_[0-9a-f]{16}\b"),
    "private partner repo path": re.compile(r"E:\\ChatP2P-private-version(?:--main|-autopilot|-)?"),
    "partner-specific invite filename": re.compile(r"backup-alpha-invite-glyn", re.IGNORECASE),
    "partner name": re.compile(r"\bGlyn\b", re.IGNORECASE),
    "windows hostnames": re.compile(r"\bDESKTOP-[A-Z0-9]+\b"),
    "live tailnet addresses": re.compile(r"\b(?:100\.85\.112\.121|100\.86\.22\.29)\b"),
}


def test_public_docs_use_placeholders_for_private_alpha_details():
    leaks: list[str] = []
    for path in PUBLIC_DOC_PATHS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN_PUBLIC_DOC_PATTERNS.items():
            for match in pattern.finditer(text):
                leaks.append(f"{path}: {label}: {match.group(0)}")

    assert leaks == []


def test_privacy_scan_passes_clean_public_tree(tmp_path):
    (tmp_path / "README.md").write_text("Use --expected-worker-id worker_...\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "RUNBOOK.md").write_text("Use TAILSCALE_IP as a placeholder.\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fixture.py").write_text(
        'FORBIDDEN_PATTERN_EXAMPLE = "worker_87b5cefe53e67c6c"\n',
        encoding="utf-8",
    )

    report = run_public_privacy_scan(PrivacyScanConfig(root=tmp_path))

    assert report["schema"] == "chatp2p.public-privacy-scan.v1"
    assert report["status"] == "pass"
    assert report["findings"] == []


def test_privacy_scan_fails_on_public_doc_private_identifiers(tmp_path):
    (tmp_path / "README.md").write_text(
        "Expected partner worker worker_87b5cefe53e67c6c on host 100.85.112.121.\n",
        encoding="utf-8",
    )

    report = run_public_privacy_scan(PrivacyScanConfig(root=tmp_path))

    assert report["status"] == "fail"
    assert {finding["pattern"] for finding in report["findings"]} == {
        "exact_worker_id",
        "live_tailnet_address",
    }


def test_privacy_scan_redacts_credential_matches(tmp_path):
    token = "S" * 32
    (tmp_path / "notes.txt").write_text(f'{{"admission_token": "{token}"}}\n', encoding="utf-8")

    report = run_public_privacy_scan(PrivacyScanConfig(root=tmp_path))

    assert report["status"] == "fail"
    assert report["findings"][0]["pattern"] == "long_admission_token"
    assert report["findings"][0]["match"] == "<redacted>"
    assert token not in str(report)


def test_privacy_scan_fails_on_private_runtime_filenames(tmp_path):
    (tmp_path / "alpha-invite.json").write_text("{}", encoding="utf-8")

    report = run_public_privacy_scan(PrivacyScanConfig(root=tmp_path))

    assert report["status"] == "fail"
    assert report["findings"][0]["scope"] == "filename"


def test_privacy_scan_cli_parses(tmp_path):
    parser = build_parser()

    args = parser.parse_args(
        [
            "operator",
            "privacy-scan",
            "--root",
            str(tmp_path),
            "--report",
            str(tmp_path / "privacy.json"),
            "--include-provider-config-filenames",
        ]
    )

    assert args.func.__name__ == "operator_privacy_scan_command"
    assert args.include_provider_config_filenames is True

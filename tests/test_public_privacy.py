import re
from pathlib import Path


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

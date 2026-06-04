"""Operator-facing safety configuration for exposed coordinators."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jsonio import read_json_file


DEFAULT_ALLOWED_JOB_TYPES = (
    "eval.math.v1",
    "eval.deterministic.v1",
    "inference.echo.v1",
    "inference.ollama.v1",
    "inference.chat.v1",
)
DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_MAX_JOB_PAYLOAD_BYTES = 16 * 1024


@dataclass(frozen=True)
class OperatorConfig:
    public_alpha: bool = False
    admission_token: str | None = None
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_job_payload_bytes: int = DEFAULT_MAX_JOB_PAYLOAD_BYTES
    allowed_job_types: tuple[str, ...] = DEFAULT_ALLOWED_JOB_TYPES

    @classmethod
    def default(cls) -> "OperatorConfig":
        return cls()

    @classmethod
    def from_file(cls, path: Path) -> "OperatorConfig":
        data = read_json_file(path, description="operator config file")
        if not isinstance(data, dict):
            raise ValueError("operator config must be a JSON object")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperatorConfig":
        allowed_job_types = data.get("allowed_job_types", DEFAULT_ALLOWED_JOB_TYPES)
        if not isinstance(allowed_job_types, list | tuple) or not all(
            isinstance(job_type, str) and job_type.strip()
            for job_type in allowed_job_types
        ):
            raise ValueError("allowed_job_types must be a list of non-empty strings")

        config = cls(
            public_alpha=bool(data.get("public_alpha", False)),
            admission_token=_optional_string(data.get("admission_token")),
            max_request_bytes=int(data.get("max_request_bytes", DEFAULT_MAX_REQUEST_BYTES)),
            max_job_payload_bytes=int(
                data.get("max_job_payload_bytes", DEFAULT_MAX_JOB_PAYLOAD_BYTES)
            ),
            allowed_job_types=tuple(job_type.strip() for job_type in allowed_job_types),
        )
        config.validate()
        return config

    def with_overrides(
        self,
        *,
        public_alpha: bool | None = None,
        admission_token: str | None = None,
        max_request_bytes: int | None = None,
        max_job_payload_bytes: int | None = None,
        allowed_job_types: list[str] | None = None,
    ) -> "OperatorConfig":
        config = OperatorConfig(
            public_alpha=self.public_alpha if public_alpha is None else public_alpha,
            admission_token=self.admission_token if admission_token is None else _optional_string(admission_token),
            max_request_bytes=self.max_request_bytes if max_request_bytes is None else max_request_bytes,
            max_job_payload_bytes=(
                self.max_job_payload_bytes
                if max_job_payload_bytes is None
                else max_job_payload_bytes
            ),
            allowed_job_types=(
                self.allowed_job_types
                if allowed_job_types is None
                else tuple(job_type.strip() for job_type in allowed_job_types if job_type.strip())
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.public_alpha and not self.admission_token:
            raise ValueError("public_alpha requires an admission_token")
        if self.admission_token and len(self.admission_token) < 12:
            raise ValueError("admission_token must be at least 12 characters")
        if self.max_request_bytes < 1024:
            raise ValueError("max_request_bytes must be at least 1024")
        if self.max_job_payload_bytes < 128:
            raise ValueError("max_job_payload_bytes must be at least 128")
        if self.max_job_payload_bytes > self.max_request_bytes:
            raise ValueError("max_job_payload_bytes cannot exceed max_request_bytes")
        if not self.allowed_job_types:
            raise ValueError("allowed_job_types cannot be empty")

    def admission_required_for(self, path: str) -> bool:
        if not self.public_alpha:
            return False
        return path in {
            "/nodes/register",
            "/jobs",
            "/jobs/demo-math",
            "/jobs/demo-suite",
        }

    def token_matches(self, supplied_token: str | None) -> bool:
        if not self.admission_token:
            return False
        if supplied_token is None:
            return False
        return secrets.compare_digest(supplied_token, self.admission_token)

    def public_summary(self) -> dict[str, Any]:
        return {
            "public_alpha": self.public_alpha,
            "admission_token_required": self.public_alpha,
            "max_request_bytes": self.max_request_bytes,
            "max_job_payload_bytes": self.max_job_payload_bytes,
            "allowed_job_types": list(self.allowed_job_types),
        }

    def to_file_dict(self) -> dict[str, Any]:
        return {
            "public_alpha": self.public_alpha,
            "admission_token": self.admission_token,
            "max_request_bytes": self.max_request_bytes,
            "max_job_payload_bytes": self.max_job_payload_bytes,
            "allowed_job_types": list(self.allowed_job_types),
        }


def write_operator_config(path: Path, config: OperatorConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_file_dict(), indent=2, sort_keys=True), encoding="utf-8")


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("admission_token must be a string")
    stripped = value.strip()
    return stripped or None

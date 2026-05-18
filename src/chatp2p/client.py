"""HTTP client used by worker nodes."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .packets import JobPacket, JobResult, NodeRegistration


class CoordinatorClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            raw = response.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def snapshot(self) -> dict[str, Any]:
        return self._request("GET", "/api/snapshot")

    def nodes(self) -> dict[str, Any]:
        return self._request("GET", "/api/nodes")

    def jobs(self) -> dict[str, Any]:
        return self._request("GET", "/api/jobs")

    def results(self) -> dict[str, Any]:
        return self._request("GET", "/api/results")

    def register(self, registration: NodeRegistration) -> dict[str, Any]:
        return self._request("POST", "/nodes/register", registration.to_dict())

    def create_demo_math_job(self) -> JobPacket:
        response = self._request("POST", "/jobs/demo-math", {})
        return JobPacket.from_dict(response["job"])

    def create_demo_suite(self) -> list[JobPacket]:
        response = self._request("POST", "/jobs/demo-suite", {})
        return [JobPacket.from_dict(job) for job in response["jobs"]]

    def next_job(self, node_id: str) -> JobPacket | None:
        query = urlencode({"node_id": node_id})
        response = self._request("GET", f"/jobs/next?{query}")
        if response["job"] is None:
            return None
        return JobPacket.from_dict(response["job"])

    def submit_result(self, result: JobResult) -> dict[str, Any]:
        return self._request("POST", "/jobs/result", result.to_dict())

"""HTTP client used by worker nodes."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen

from .crypto import NodeIdentity
from .packets import JobLeaseAcknowledgement, JobLeaseRequest, JobPacket, JobResult, NodeHeartbeat, NodeRegistration


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

    def reputation(self) -> dict[str, Any]:
        return self._request("GET", "/api/reputation")

    def register(self, registration: NodeRegistration) -> dict[str, Any]:
        return self._request("POST", "/nodes/register", registration.to_dict())

    def heartbeat(self, node: NodeIdentity) -> dict[str, Any]:
        return self._request("POST", "/nodes/heartbeat", NodeHeartbeat.create(node=node).to_dict())

    def create_demo_math_job(self) -> JobPacket:
        response = self._request("POST", "/jobs/demo-math", {})
        return JobPacket.from_dict(response["job"])

    def create_demo_suite(self) -> list[JobPacket]:
        response = self._request("POST", "/jobs/demo-suite", {})
        return [JobPacket.from_dict(job) for job in response["jobs"]]

    def create_job(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        model_id: str | None = None,
        reward: int = 1,
        ttl_seconds: int = 300,
    ) -> JobPacket:
        request = {
            "job_type": job_type,
            "payload": payload,
            "reward": reward,
            "ttl_seconds": ttl_seconds,
        }
        if model_id is not None:
            request["model_id"] = model_id
        response = self._request("POST", "/jobs", request)
        return JobPacket.from_dict(response["job"])

    def next_job(self, node: NodeIdentity) -> JobPacket | None:
        response = self._request("POST", "/jobs/next", JobLeaseRequest.create(node=node).to_dict())
        if response["job"] is None:
            return None
        job = JobPacket.from_dict(response["job"])
        lease = response["lease"]
        acknowledgement = JobLeaseAcknowledgement.create(
            node=node,
            job_id=job.job_id,
            leased_at=lease["leased_at"],
            expires_at=lease["expires_at"],
        )
        ack_response = self.acknowledge_lease(acknowledgement)
        if not ack_response.get("accepted"):
            raise RuntimeError(f"lease acknowledgement rejected: {ack_response}")
        return job

    def acknowledge_lease(self, acknowledgement: JobLeaseAcknowledgement) -> dict[str, Any]:
        return self._request("POST", "/jobs/lease/ack", acknowledgement.to_dict())

    def submit_result(self, result: JobResult) -> dict[str, Any]:
        return self._request("POST", "/jobs/result", result.to_dict())

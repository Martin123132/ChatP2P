"""HTTP client used by worker nodes."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen

from .crypto import NodeIdentity
from .packets import (
    JobLeaseAcknowledgement,
    JobLeaseGrant,
    JobLeaseRequest,
    JobLeaseRenewal,
    JobPacket,
    JobResult,
    NodeHeartbeat,
    NodeRegistration,
)


class CoordinatorClient:
    def __init__(
        self,
        base_url: str,
        admission_token: str | None = None,
        timeout_seconds: float = 10.0,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        self.base_url = base_url.rstrip("/")
        self.admission_token = admission_token
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.admission_token:
            headers["X-ChatP2P-Admission-Token"] = self.admission_token
        if extra_headers:
            headers.update(extra_headers)
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
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

    def ledger(self) -> dict[str, Any]:
        return self._request("GET", "/api/ledger")

    def grant_requester_credits(
        self,
        *,
        credit_grant_token: str,
        account_id: str,
        credits: int,
        reason: str = "operator_credit_grant",
        transaction_id: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "account_id": account_id,
            "account_type": "requester",
            "delta": int(credits),
            "reason": reason,
        }
        if transaction_id is not None:
            request["transaction_id"] = transaction_id
        return self._request(
            "POST",
            "/operator/credits/grant",
            request,
            extra_headers={"X-ChatP2P-Credit-Grant-Token": credit_grant_token},
        )

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
        resource_requirements: dict[str, Any] | None = None,
        expected_output_schema: dict[str, Any] | None = None,
        verification_strategy: str | None = None,
        reward: int = 1,
        ttl_seconds: int = 300,
        requester_account_id: str | None = None,
        job_cost: int | None = None,
    ) -> JobPacket:
        request = {
            "job_type": job_type,
            "payload": payload,
            "reward": reward,
            "ttl_seconds": ttl_seconds,
        }
        if model_id is not None:
            request["model_id"] = model_id
        if resource_requirements is not None:
            request["resource_requirements"] = resource_requirements
        if expected_output_schema is not None:
            request["expected_output_schema"] = expected_output_schema
        if verification_strategy is not None:
            request["verification_strategy"] = verification_strategy
        if requester_account_id is not None:
            request["requester_account_id"] = requester_account_id
        if job_cost is not None:
            request["job_cost"] = job_cost
        response = self._request("POST", "/jobs", request)
        return JobPacket.from_dict(response["job"])

    def create_chat_job(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        reward: int = 1,
        ttl_seconds: int = 300,
        requester_account_id: str | None = None,
        job_cost: int | None = None,
    ) -> JobPacket:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return self.create_job(
            job_type="inference.chat.v1",
            payload=payload,
            model_id=model,
            reward=reward,
            ttl_seconds=ttl_seconds,
            requester_account_id=requester_account_id,
            job_cost=job_cost,
        )

    def next_job_with_lease(self, node: NodeIdentity) -> tuple[JobPacket, dict[str, Any]] | None:
        response = self._request("POST", "/jobs/next", JobLeaseRequest.create(node=node).to_dict())
        if response["job"] is None:
            return None
        job = JobPacket.from_dict(response["job"])
        lease = response["lease"]
        grant = JobLeaseGrant.from_dict(lease["grant"])
        if not grant.verify_signature():
            raise RuntimeError("lease grant signature rejected")
        if grant.grant_hash() != lease["grant_hash"]:
            raise RuntimeError("lease grant hash mismatch")
        acknowledgement = JobLeaseAcknowledgement.create(node=node, grant=grant)
        ack_response = self.acknowledge_lease(acknowledgement)
        if not ack_response.get("accepted"):
            raise RuntimeError(f"lease acknowledgement rejected: {ack_response}")
        return job, ack_response.get("lease") or lease

    def next_job(self, node: NodeIdentity) -> JobPacket | None:
        leased = self.next_job_with_lease(node)
        return leased[0] if leased is not None else None

    def acknowledge_lease(self, acknowledgement: JobLeaseAcknowledgement) -> dict[str, Any]:
        return self._request("POST", "/jobs/lease/ack", acknowledgement.to_dict())

    def renew_lease(self, renewal: JobLeaseRenewal) -> dict[str, Any]:
        return self._request("POST", "/jobs/lease/renew", renewal.to_dict())

    def submit_result(self, result: JobResult) -> dict[str, Any]:
        return self._request("POST", "/jobs/result", result.to_dict())

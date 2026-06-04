import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chatp2p.client import CoordinatorClient
from chatp2p.cli import _run_one_remote_job
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.operator_config import OperatorConfig
from chatp2p.packets import NodeHeartbeat, NodeRegistration
from chatp2p.worker import WorkerNode


class SlowWorkerNode(WorkerNode):
    def run_job(self, job):
        time.sleep(0.7)
        return super().run_job(job)


def test_http_worker_registers_leases_job_and_submits_result():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(identity=coordinator_identity)
    coordinator.create_deterministic_eval_jobs()
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        client = CoordinatorClient(f"http://{host}:{port}")
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(identity=worker_identity)

        registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
        register_response = client.register(registration)
        assert register_response["accepted"] is True

        job = client.next_job(worker_identity)
        assert job is not None
        assert job.job_type == "eval.deterministic.v1"

        result = worker.run_job(job)
        submit_response = client.submit_result(result)
        assert submit_response["accepted"] is True
        assert submit_response["credits"] == 1

        health = client.health()
        assert health["known_nodes"] == 1
        assert health["live_nodes"] == 1
        assert health["lease_timeout_seconds"] == 30.0
        assert health["pending_jobs"] == 1
        assert health["completed_jobs"] == 0
        assert client.heartbeat(worker_identity)["accepted"] is True

        snapshot = client.snapshot()
        assert len(snapshot["nodes"]) == 1
        assert len(snapshot["jobs"]) == 4
        assert len(snapshot["results"]) == 1
        assert len(snapshot["reputation"]) == 1
        assert snapshot["nodes"][0]["liveness_status"] == "live"
        assert snapshot["leasing_policy"]["flagged_order"] == ["tie_breaker"]
        assert snapshot["reputation"][0]["status"] == "new"
        leased_snapshot_job = next(item for item in snapshot["jobs"] if item["job_id"] == job.job_id)
        assert leased_snapshot_job["status"] == "pending"
        assert leased_snapshot_job["acknowledged_lease_count"] == 1
        assert leased_snapshot_job["leases"][0]["lease_id"].startswith("lease_")
        assert leased_snapshot_job["leases"][0]["grant_hash"]
        assert snapshot["credit_ledger"]["summary"]["entries"] == 1

        ledger = client.ledger()["credit_ledger"]
        assert ledger["summary"]["entries"] == 1
        assert ledger["recent_entries"][0]["reason"] == "worker_result_reward"

        with urlopen(f"http://{host}:{port}/dashboard", timeout=10) as response:
            dashboard = response.read().decode("utf-8")
        assert "ChatP2P Coordinator" in dashboard
        assert "Provider / ISP Edge" in dashboard
        assert "Recent Results" in dashboard
        with urlopen(f"http://{host}:{port}/api/provider", timeout=10) as response:
            provider = json.loads(response.read().decode("utf-8"))["provider"]
        assert provider["jobs_routed"]["provider_edge"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_worker_renews_short_lease_while_job_runs():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(identity=coordinator_identity, lease_timeout_seconds=0.5)
    coordinator.create_job(
        job_type="inference.echo.v1",
        payload={"prompt": "slow lease renewal proof"},
        ttl_seconds=10,
    )
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        client = CoordinatorClient(f"http://{host}:{port}")
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = SlowWorkerNode(identity=worker_identity)
        registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
        assert client.register(registration)["accepted"] is True

        result = _run_one_remote_job(client, worker)
        snapshot = client.snapshot()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["status"] == "submitted"
    assert result["result_accepted"] is True
    assert len(result["lease_renewals"]) >= 1
    assert result["lease_renewal_errors"] == []
    assert snapshot["status"]["expired_leases"] == 0
    assert snapshot["status"]["verified_jobs"] == 1


def test_http_producer_creates_job_after_coordinator_start():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(identity=coordinator_identity)
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        client = CoordinatorClient(f"http://{host}:{port}")
        created_job = client.create_job(
            job_type="eval.deterministic.v1",
            payload={
                "task": "arithmetic",
                "operation": "add",
                "operands": [7, 8],
                "expected": 15,
            },
        )
        assert created_job.verify_signature()

        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(identity=worker_identity)
        registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
        assert client.register(registration)["accepted"] is True

        leased_job = client.next_job(worker_identity)
        assert leased_job is not None
        result = worker.run_job(leased_job)
        assert result.output["answer"] == 15
        assert client.submit_result(result)["accepted"] is True

        snapshot = client.snapshot()
        assert snapshot["status"]["pending_jobs"] == 1
        assert snapshot["jobs"][0]["payload"]["operands"] == [7, 8]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_rejects_unsigned_liveness_and_legacy_job_pull():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(identity=coordinator_identity)
    coordinator.create_echo_inference_job("signed network only")
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        client = CoordinatorClient(f"http://{host}:{port}")
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(identity=worker_identity)
        registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
        assert client.register(registration)["accepted"] is True

        try:
            urlopen(f"http://{host}:{port}/jobs/next?node_id={worker_identity.node_id}", timeout=10)
        except HTTPError as exc:
            assert exc.code == 405
        else:
            raise AssertionError("Expected legacy unsigned job pull to be rejected")

        request = Request(
            f"http://{host}:{port}/nodes/heartbeat",
            data=json.dumps({"node_id": worker_identity.node_id}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(request, timeout=10)
        except HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("Expected unsigned heartbeat to be rejected")

        attacker_identity = NodeIdentity.generate(prefix="worker")
        heartbeat_data = NodeHeartbeat.create(node=attacker_identity).to_dict()
        heartbeat_data["node_id"] = worker_identity.node_id
        request = Request(
            f"http://{host}:{port}/nodes/heartbeat",
            data=json.dumps(heartbeat_data).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(request, timeout=10)
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Expected spoofed heartbeat to be rejected")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_public_alpha_requires_admission_token_for_registration_and_job_creation():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(identity=coordinator_identity)
    operator_config = OperatorConfig(public_alpha=True, admission_token="alpha-token-123")
    server = create_coordinator_http_server(
        coordinator,
        host="127.0.0.1",
        port=0,
        operator_config=operator_config,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(identity=worker_identity)
        registration = NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())

        try:
            CoordinatorClient(base_url).register(registration)
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Expected registration without admission token to fail")

        client = CoordinatorClient(base_url, admission_token="alpha-token-123")
        assert client.register(registration)["accepted"] is True

        job_payload = {
            "task": "arithmetic",
            "operation": "add",
            "operands": [1, 2],
            "expected": 3,
        }
        try:
            CoordinatorClient(base_url).create_job(
                job_type="eval.deterministic.v1",
                payload=job_payload,
            )
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Expected job creation without admission token to fail")

        job = client.create_job(job_type="eval.deterministic.v1", payload=job_payload)
        assert job.verify_signature()

        health = client.health()
        assert health["operator"]["public_alpha"] is True
        assert health["operator"]["admission_token_required"] is True
        assert "admission_token" not in health["operator"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_public_alpha_rejects_disallowed_and_oversized_jobs():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    coordinator = Coordinator(identity=coordinator_identity)
    operator_config = OperatorConfig(
        public_alpha=True,
        admission_token="alpha-token-123",
        max_job_payload_bytes=256,
        allowed_job_types=("eval.deterministic.v1",),
    )
    server = create_coordinator_http_server(
        coordinator,
        host="127.0.0.1",
        port=0,
        operator_config=operator_config,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        client = CoordinatorClient(f"http://{host}:{port}", admission_token="alpha-token-123")

        try:
            client.create_job(job_type="inference.echo.v1", payload={"prompt": "blocked"})
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("Expected disallowed job type to fail")

        try:
            client.create_job(
                job_type="eval.deterministic.v1",
                payload={
                    "task": "text",
                    "operation": "normalize_whitespace",
                    "value": "x" * 512,
                    "expected": "x" * 512,
                },
            )
        except HTTPError as exc:
            assert exc.code == 413
        else:
            raise AssertionError("Expected oversized job payload to fail")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

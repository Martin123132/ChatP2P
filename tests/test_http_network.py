import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chatp2p.client import CoordinatorClient
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.packets import NodeHeartbeat, NodeRegistration
from chatp2p.worker import WorkerNode


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

        with urlopen(f"http://{host}:{port}/dashboard", timeout=10) as response:
            dashboard = response.read().decode("utf-8")
        assert "ChatP2P Coordinator" in dashboard
        assert "Recent Results" in dashboard
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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

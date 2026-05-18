import threading
from urllib.request import urlopen

from chatp2p.client import CoordinatorClient
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.packets import NodeRegistration
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

        job = client.next_job(worker_identity.node_id)
        assert job is not None
        assert job.job_type == "eval.deterministic.v1"

        result = worker.run_job(job)
        submit_response = client.submit_result(result)
        assert submit_response["accepted"] is True
        assert submit_response["credits"] == 1

        health = client.health()
        assert health["known_nodes"] == 1
        assert health["completed_jobs"] == 1

        snapshot = client.snapshot()
        assert len(snapshot["nodes"]) == 1
        assert len(snapshot["jobs"]) == 4
        assert len(snapshot["results"]) == 1

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

        leased_job = client.next_job(worker_identity.node_id)
        assert leased_job is not None
        result = worker.run_job(leased_job)
        assert result.output["answer"] == 15
        assert client.submit_result(result)["accepted"] is True

        snapshot = client.snapshot()
        assert snapshot["status"]["completed_jobs"] == 1
        assert snapshot["jobs"][0]["payload"]["operands"] == [7, 8]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

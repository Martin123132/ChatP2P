from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.packets import NodeRegistration
from chatp2p.storage import SQLiteCoordinatorStore
from chatp2p.worker import WorkerNode


def test_sqlite_store_survives_coordinator_restart(tmp_path):
    db_path = tmp_path / "coordinator.sqlite3"
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)

    coordinator = Coordinator(
        identity=coordinator_identity,
        store=SQLiteCoordinatorStore(db_path),
    )
    assert coordinator.register_signed_node(
        NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
    )
    jobs = coordinator.create_deterministic_eval_jobs()
    assert len(jobs) == 4

    job = coordinator.lease_next_job(worker_identity.node_id)
    assert job is not None
    result = worker.run_job(job)
    assert coordinator.submit_result(result)

    restarted = Coordinator(
        identity=coordinator_identity,
        store=SQLiteCoordinatorStore(db_path),
    )

    assert worker_identity.node_id in restarted.known_nodes
    assert len(restarted.jobs) == 4
    assert restarted.credits[worker_identity.node_id] == 1
    assert restarted.status()["pending_jobs"] == 1
    assert restarted.status()["completed_jobs"] == 0


def test_sqlite_store_restores_active_lease_metadata(tmp_path):
    db_path = tmp_path / "coordinator.sqlite3"
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)

    coordinator = Coordinator(
        identity=coordinator_identity,
        store=SQLiteCoordinatorStore(db_path),
        lease_timeout_seconds=60,
    )
    assert coordinator.register_signed_node(
        NodeRegistration.create(node=worker_identity, capabilities=worker.capabilities())
    )
    job = coordinator.create_echo_inference_job("persist this lease")
    leased = coordinator.lease_next_job(worker_identity.node_id)
    assert leased is not None
    assert leased.job_id == job.job_id

    restarted = Coordinator(
        identity=coordinator_identity,
        store=SQLiteCoordinatorStore(db_path),
        lease_timeout_seconds=60,
    )
    summary = next(item for item in restarted.job_summaries() if item["job_id"] == job.job_id)

    assert summary["active_leases"] == [worker_identity.node_id]
    assert summary["leases"][0]["status"] == "active"
    assert summary["leases"][0]["expires_at"] is not None
    assert restarted.node_summaries()[0]["liveness_status"] == "live"

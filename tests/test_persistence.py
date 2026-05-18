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

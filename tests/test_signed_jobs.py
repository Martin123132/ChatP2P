from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.packets import NodeRegistration
from chatp2p.worker import WorkerNode


def test_worker_completes_signed_math_job():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    worker_identity = NodeIdentity.generate(prefix="worker")
    coordinator = Coordinator(identity=coordinator_identity)
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public())

    job = coordinator.create_math_eval_job()
    result = worker.run_job(job)

    assert job.verify_signature()
    assert job.verify_payload_hash()
    assert result.verify_signature()
    assert result.verify_output_hash()
    assert coordinator.submit_result(result)
    assert coordinator.credits[worker_identity.node_id] == 1
    assert result.output["passed"] is True


def test_worker_completes_deterministic_eval_suite():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    worker_identity = NodeIdentity.generate(prefix="worker")
    coordinator = Coordinator(identity=coordinator_identity)
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public())

    jobs = coordinator.create_deterministic_eval_jobs()

    for job in jobs:
        leased = coordinator.lease_next_job(worker_identity.node_id)
        assert leased is not None
        result = worker.run_job(leased)
        assert result.output["passed"] is True
        assert coordinator.submit_result(result)

    assert coordinator.credits[worker_identity.node_id] == len(jobs)


def test_coordinator_rejects_unknown_worker():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    worker_identity = NodeIdentity.generate(prefix="worker")
    coordinator = Coordinator(identity=coordinator_identity)
    worker = WorkerNode(identity=worker_identity)

    job = coordinator.create_math_eval_job()
    result = worker.run_job(job)

    assert coordinator.submit_result(result) is False


def test_coordinator_rejects_tampered_registration():
    worker_identity = NodeIdentity.generate(prefix="worker")
    registration_data = NodeRegistration.create(
        node=worker_identity,
        capabilities={"supported_job_types": ["eval.math.v1"]},
    ).to_dict()
    registration_data["capabilities"] = {"supported_job_types": ["admin.everything.v1"]}
    registration = NodeRegistration.from_dict(registration_data)

    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))

    assert coordinator.register_signed_node(registration) is False
    assert worker_identity.node_id not in coordinator.known_nodes


def test_coordinator_leases_only_supported_job_types():
    worker_identity = NodeIdentity.generate(prefix="worker")
    registration = NodeRegistration.create(
        node=worker_identity,
        capabilities={"supported_job_types": ["inference.echo.v1"]},
    )
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))

    assert coordinator.register_signed_node(registration)
    coordinator.create_deterministic_eval_jobs()

    assert coordinator.lease_next_job(worker_identity.node_id) is None


def test_coordinator_rejects_invalid_created_jobs():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))

    try:
        coordinator.create_job(
            job_type="eval.deterministic.v1",
            payload={
                "task": "arithmetic",
                "operation": "divide",
                "operands": [1, 0],
                "expected": 0,
            },
        )
    except ValueError as exc:
        assert "division by zero" in str(exc)
    else:
        raise AssertionError("Expected invalid arithmetic job to be rejected")

    try:
        coordinator.create_job(job_type="inference.echo.v1", payload={"prompt": ""})
    except ValueError as exc:
        assert "non-empty prompt" in str(exc)
    else:
        raise AssertionError("Expected empty echo prompt to be rejected")

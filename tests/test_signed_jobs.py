from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.packets import JobResult, NodeRegistration
from chatp2p.worker import WorkerNode


def test_worker_completes_signed_math_job():
    coordinator_identity = NodeIdentity.generate(prefix="coordinator")
    worker_identity = NodeIdentity.generate(prefix="worker")
    coordinator = Coordinator(identity=coordinator_identity)
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public())

    job = coordinator.create_math_eval_job()
    leased_job = coordinator.lease_next_job(worker_identity.node_id)
    assert leased_job is not None
    assert leased_job.job_id == job.job_id
    result = worker.run_job(leased_job)

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


def test_duplicate_verification_requires_matching_independent_results():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker_a_identity = NodeIdentity.generate(prefix="worker")
    worker_b_identity = NodeIdentity.generate(prefix="worker")
    worker_a = WorkerNode(identity=worker_a_identity)
    worker_b = WorkerNode(identity=worker_b_identity)
    coordinator.register_node(worker_a_identity.public(), capabilities=worker_a.capabilities())
    coordinator.register_node(worker_b_identity.public(), capabilities=worker_b.capabilities())
    job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "add",
            "operands": [2, 5],
            "expected": 7,
        },
    )

    leased_a = coordinator.lease_next_job(worker_a_identity.node_id)
    assert leased_a is not None
    assert leased_a.job_id == job.job_id
    assert coordinator.submit_result(worker_a.run_job(leased_a))
    assert coordinator.verification_summary(job.job_id)["status"] == "pending"

    leased_b = coordinator.lease_next_job(worker_b_identity.node_id)
    assert leased_b is not None
    assert leased_b.job_id == job.job_id
    assert coordinator.submit_result(worker_b.run_job(leased_b))

    summary = coordinator.verification_summary(job.job_id)
    assert summary["status"] == "verified"
    assert summary["required_results"] == 2
    assert coordinator.status()["verified_jobs"] == 1


def test_duplicate_verification_marks_disputed_after_tie_breaker_fails():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    identities = [NodeIdentity.generate(prefix="worker") for _ in range(3)]
    workers = [WorkerNode(identity=identity) for identity in identities]
    for worker in workers:
        coordinator.register_node(worker.identity.public(), capabilities=worker.capabilities())
    job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "add",
            "operands": [4, 6],
            "expected": 10,
        },
    )

    leased_a = coordinator.lease_next_job(identities[0].node_id)
    assert leased_a is not None
    assert coordinator.submit_result(workers[0].run_job(leased_a))

    leased_b = coordinator.lease_next_job(identities[1].node_id)
    assert leased_b is not None
    wrong_b = JobResult.create(
        node=identities[1],
        job=leased_b,
        output={"passed": False, "answer": 11, "expected": 10, "confidence": 1.0},
    )
    assert coordinator.submit_result(wrong_b)
    assert coordinator.verification_summary(job.job_id)["status"] == "pending"

    leased_c = coordinator.lease_next_job(identities[2].node_id)
    assert leased_c is not None
    wrong_c = JobResult.create(
        node=identities[2],
        job=leased_c,
        output={"passed": False, "answer": 12, "expected": 10, "confidence": 1.0},
    )
    assert coordinator.submit_result(wrong_c)

    summary = coordinator.verification_summary(job.job_id)
    assert summary["status"] == "disputed"
    assert coordinator.status()["disputed_jobs"] == 1

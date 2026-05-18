from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.packets import (
    JobLeaseAcknowledgement,
    JobLeaseGrant,
    JobLeaseRequest,
    JobResult,
    NodeHeartbeat,
    NodeRegistration,
)
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


def test_signed_heartbeat_rejects_spoofed_node_id():
    worker_identity = NodeIdentity.generate(prefix="worker")
    attacker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
    original_last_seen = coordinator.node_last_seen[worker_identity.node_id]

    heartbeat_data = NodeHeartbeat.create(node=attacker_identity).to_dict()
    heartbeat_data["node_id"] = worker_identity.node_id

    assert coordinator.record_signed_heartbeat(NodeHeartbeat.from_dict(heartbeat_data)) is False
    assert coordinator.node_last_seen[worker_identity.node_id] == original_last_seen


def test_signed_heartbeat_replay_and_stale_packets_are_rejected(monkeypatch):
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator = Coordinator(
        identity=NodeIdentity.generate(prefix="coordinator"),
        packet_max_age_seconds=10,
    )
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())

    heartbeat = NodeHeartbeat.create(node=worker_identity)

    assert coordinator.record_signed_heartbeat(heartbeat)
    assert coordinator.record_signed_heartbeat(heartbeat) is False

    monkeypatch.setattr("chatp2p.packets.time.time", lambda: 100.0)
    stale = NodeHeartbeat.create(node=worker_identity)
    monkeypatch.undo()

    assert coordinator.record_signed_heartbeat(stale) is False


def test_signed_lease_request_requires_acknowledgement():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker_identity = NodeIdentity.generate(prefix="worker")
    attacker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
    coordinator.create_echo_inference_job("signed lease flow")

    leased = coordinator.lease_next_signed_job(JobLeaseRequest.create(node=worker_identity))

    assert leased is not None
    job, lease = leased
    grant = JobLeaseGrant.from_dict(lease["grant"])
    attacker_ack = JobLeaseAcknowledgement.create(
        node=attacker_identity,
        grant=grant,
    )
    assert coordinator.acknowledge_lease(attacker_ack) is False

    acknowledgement = JobLeaseAcknowledgement.create(
        node=worker_identity,
        grant=grant,
    )

    assert coordinator.acknowledge_lease(acknowledgement)
    summary = coordinator.job_summaries()[0]
    assert summary["acknowledged_lease_count"] == 1
    assert summary["leases"][0]["acknowledged"] is True


def test_signed_lease_request_replay_is_rejected():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
    coordinator.create_echo_inference_job("replay request")
    request = JobLeaseRequest.create(node=worker_identity)

    assert coordinator.lease_next_signed_job(request) is not None

    assert coordinator.lease_next_signed_job(request) is None
    assert coordinator.signed_node_packet_rejection_reason(request) == "replayed packet"


def test_lease_acknowledgement_replay_and_tampered_grant_are_rejected():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
    coordinator.create_echo_inference_job("grant hash")
    leased = coordinator.lease_next_signed_job(JobLeaseRequest.create(node=worker_identity))
    assert leased is not None
    job, lease = leased
    grant = JobLeaseGrant.from_dict(lease["grant"])

    tampered_grant_data = grant.to_dict()
    tampered_grant_data["expires_at"] = tampered_grant_data["expires_at"] + 30
    tampered_ack = JobLeaseAcknowledgement.create(
        node=worker_identity,
        grant=JobLeaseGrant.from_dict(tampered_grant_data),
    )
    assert coordinator.acknowledge_lease(tampered_ack) is False

    acknowledgement = JobLeaseAcknowledgement.create(node=worker_identity, grant=grant)

    assert coordinator.acknowledge_lease(acknowledgement)
    assert coordinator.acknowledge_lease(acknowledgement) is False
    assert coordinator.signed_node_packet_rejection_reason(acknowledgement) == "replayed packet"


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
    reputation = coordinator.reputation_summaries()
    assert reputation[worker_a_identity.node_id]["score"] == 1
    assert reputation[worker_a_identity.node_id]["status"] == "ok"
    assert reputation[worker_b_identity.node_id]["verified_matches"] == 1


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
    reputation = coordinator.reputation_summaries()
    assert reputation[identities[0].node_id]["status"] == "flagged"
    assert reputation[identities[0].node_id]["disputed_results"] == 1
    assert reputation[identities[1].node_id]["score"] == -1
    assert reputation[identities[2].node_id]["score"] == -1


def test_verified_job_flags_mismatching_worker_reputation():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    identities = [NodeIdentity.generate(prefix="worker") for _ in range(3)]
    workers = [WorkerNode(identity=identity) for identity in identities]
    for worker in workers:
        coordinator.register_node(worker.identity.public(), capabilities=worker.capabilities())
    job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "multiply",
            "operands": [3, 4],
            "expected": 12,
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
        output={"passed": False, "answer": 13, "expected": 12, "confidence": 1.0},
    )
    assert coordinator.submit_result(wrong_b)

    leased_c = coordinator.lease_next_job(identities[2].node_id)
    assert leased_c is not None
    assert coordinator.submit_result(workers[2].run_job(leased_c))

    assert coordinator.verification_summary(job.job_id)["status"] == "verified"
    reputation = coordinator.reputation_summaries()
    assert reputation[identities[0].node_id]["status"] == "ok"
    assert reputation[identities[2].node_id]["verified_matches"] == 1
    assert reputation[identities[1].node_id]["mismatches"] == 1
    assert reputation[identities[1].node_id]["status"] == "flagged"


def test_flagged_worker_skips_ordinary_queued_jobs():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    identities = [NodeIdentity.generate(prefix="worker") for _ in range(3)]
    workers = [WorkerNode(identity=identity) for identity in identities]
    for worker in workers:
        coordinator.register_node(worker.identity.public(), capabilities=worker.capabilities())

    first_job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "add",
            "operands": [8, 1],
            "expected": 9,
        },
    )
    assert coordinator.submit_result(workers[0].run_job(coordinator.lease_next_job(identities[0].node_id)))
    leased_bad = coordinator.lease_next_job(identities[1].node_id)
    assert leased_bad is not None
    wrong = JobResult.create(
        node=identities[1],
        job=leased_bad,
        output={"passed": False, "answer": 10, "expected": 9, "confidence": 1.0},
    )
    assert coordinator.submit_result(wrong)
    assert coordinator.submit_result(workers[2].run_job(coordinator.lease_next_job(identities[2].node_id)))
    assert coordinator.verification_summary(first_job.job_id)["status"] == "verified"
    assert coordinator.reputation_summaries()[identities[1].node_id]["status"] == "flagged"

    coordinator.create_echo_inference_job("ordinary queued work")

    assert coordinator.lease_next_job(identities[1].node_id) is None


def test_reliable_worker_gets_pending_verification_before_queued_work():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    reliable_identity = NodeIdentity.generate(prefix="worker")
    helper_identity = NodeIdentity.generate(prefix="worker")
    reliable_worker = WorkerNode(identity=reliable_identity)
    helper_worker = WorkerNode(identity=helper_identity)
    coordinator.register_node(reliable_identity.public(), capabilities=reliable_worker.capabilities())
    coordinator.register_node(helper_identity.public(), capabilities=helper_worker.capabilities())

    reputation_job = coordinator.create_echo_inference_job("build initial score")
    leased_reputation = coordinator.lease_next_job(reliable_identity.node_id)
    assert leased_reputation is not None
    assert leased_reputation.job_id == reputation_job.job_id
    assert coordinator.submit_result(reliable_worker.run_job(leased_reputation))
    assert coordinator.reputation_summaries()[reliable_identity.node_id]["status"] == "ok"

    pending_job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "multiply",
            "operands": [5, 5],
            "expected": 25,
        },
    )
    leased_helper = coordinator.lease_next_job(helper_identity.node_id)
    assert leased_helper is not None
    assert leased_helper.job_id == pending_job.job_id
    assert coordinator.submit_result(helper_worker.run_job(leased_helper))
    coordinator.create_echo_inference_job("queued but less urgent")

    leased_reliable = coordinator.lease_next_job(reliable_identity.node_id)

    assert leased_reliable is not None
    assert leased_reliable.job_id == pending_job.job_id


def test_pending_job_does_not_overlease_beyond_needed_verification():
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
            "operands": [20, 22],
            "expected": 42,
        },
    )

    first_lease = coordinator.lease_next_job(identities[0].node_id)
    assert first_lease is not None
    assert first_lease.job_id == job.job_id
    assert coordinator.submit_result(workers[0].run_job(first_lease))

    second_lease = coordinator.lease_next_job(identities[1].node_id)
    assert second_lease is not None
    assert second_lease.job_id == job.job_id

    assert coordinator.lease_next_job(identities[2].node_id) is None

    assert coordinator.submit_result(workers[1].run_job(second_lease))
    assert coordinator.verification_summary(job.job_id)["status"] == "verified"


def test_flagged_worker_can_take_conflict_tie_breaker():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    identities = [NodeIdentity.generate(prefix="worker") for _ in range(6)]
    workers = [WorkerNode(identity=identity) for identity in identities]
    for worker in workers:
        coordinator.register_node(worker.identity.public(), capabilities=worker.capabilities())

    reputation_job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "add",
            "operands": [1, 2],
            "expected": 3,
        },
    )
    assert coordinator.submit_result(workers[0].run_job(coordinator.lease_next_job(identities[0].node_id)))
    bad_reputation_job = coordinator.lease_next_job(identities[1].node_id)
    assert bad_reputation_job is not None
    bad_reputation_result = JobResult.create(
        node=identities[1],
        job=bad_reputation_job,
        output={"passed": False, "answer": 4, "expected": 3, "confidence": 1.0},
    )
    assert coordinator.submit_result(bad_reputation_result)
    assert coordinator.submit_result(workers[2].run_job(coordinator.lease_next_job(identities[2].node_id)))
    assert coordinator.verification_summary(reputation_job.job_id)["status"] == "verified"
    assert coordinator.reputation_summaries()[identities[1].node_id]["status"] == "flagged"

    conflict_job = coordinator.create_job(
        job_type="eval.deterministic.v1",
        payload={
            "task": "arithmetic",
            "operation": "add",
            "operands": [10, 5],
            "expected": 15,
        },
    )
    assert coordinator.submit_result(workers[3].run_job(coordinator.lease_next_job(identities[3].node_id)))
    conflict_bad_job = coordinator.lease_next_job(identities[4].node_id)
    assert conflict_bad_job is not None
    conflict_bad_result = JobResult.create(
        node=identities[4],
        job=conflict_bad_job,
        output={"passed": False, "answer": 14, "expected": 15, "confidence": 1.0},
    )
    assert coordinator.submit_result(conflict_bad_result)

    tie_breaker = coordinator.lease_next_job(identities[1].node_id)

    assert tie_breaker is not None
    assert tie_breaker.job_id == conflict_job.job_id


def test_expired_lease_requeues_job_and_rejects_late_result():
    coordinator = Coordinator(
        identity=NodeIdentity.generate(prefix="coordinator"),
        lease_timeout_seconds=10,
    )
    first_identity = NodeIdentity.generate(prefix="worker")
    second_identity = NodeIdentity.generate(prefix="worker")
    first_worker = WorkerNode(identity=first_identity)
    second_worker = WorkerNode(identity=second_identity)
    coordinator.register_node(first_identity.public(), capabilities=first_worker.capabilities())
    coordinator.register_node(second_identity.public(), capabilities=second_worker.capabilities())
    job = coordinator.create_echo_inference_job("recover this if a worker disappears")

    first_lease = coordinator.lease_next_job(first_identity.node_id)
    assert first_lease is not None
    assert first_lease.job_id == job.job_id
    lease = coordinator.job_summaries()[0]["leases"][0]

    expired = coordinator.reap_expired_leases(now=lease["expires_at"] + 0.001)

    assert expired[0]["node_id"] == first_identity.node_id
    assert coordinator.submit_result(first_worker.run_job(first_lease)) is False
    assert coordinator.verification_summary(job.job_id)["status"] == "queued"
    assert coordinator.reputation_summaries()[first_identity.node_id]["timeouts"] == 1
    assert coordinator.reputation_summaries()[first_identity.node_id]["status"] == "watch"
    assert coordinator.lease_next_job(first_identity.node_id) is None

    second_lease = coordinator.lease_next_job(second_identity.node_id)
    assert second_lease is not None
    assert second_lease.job_id == job.job_id
    assert coordinator.submit_result(second_worker.run_job(second_lease))
    assert coordinator.verification_summary(job.job_id)["status"] == "verified"


def test_node_liveness_updates_when_worker_polls():
    coordinator = Coordinator(
        identity=NodeIdentity.generate(prefix="coordinator"),
        node_stale_seconds=10,
    )
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
    coordinator.create_echo_inference_job("liveness ping")

    coordinator.node_last_seen[worker_identity.node_id] -= 35
    assert coordinator.node_summaries()[0]["liveness_status"] == "offline"

    assert coordinator.lease_next_job(worker_identity.node_id) is not None

    summary = coordinator.node_summaries()[0]
    assert summary["liveness_status"] == "live"
    assert summary["active_leases"] == 1

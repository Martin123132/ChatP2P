import pytest

from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.packets import NodeRegistration
from chatp2p.storage import SQLiteCoordinatorStore
from chatp2p.worker import WorkerNode


def test_worker_reward_creates_credit_ledger_entry():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())

    job = coordinator.create_echo_inference_job("ledger reward")
    leased = coordinator.lease_next_job(worker_identity.node_id)
    assert leased is not None
    result = worker.run_job(leased)

    assert coordinator.submit_result(result)

    ledger = coordinator.credit_ledger_snapshot()
    assert coordinator.credits[worker_identity.node_id] == job.reward
    assert ledger["summary"]["entries"] == 1
    assert ledger["summary"]["positive_credits"] == job.reward
    assert ledger["summary"]["by_reason"]["worker_result_reward"]["net_delta"] == job.reward
    entry = ledger["recent_entries"][0]
    assert entry["schema"] == "chatp2p.credit-ledger-entry.v1"
    assert entry["account_id"] == worker_identity.node_id
    assert entry["delta"] == job.reward
    assert entry["balance_after"] == job.reward
    assert entry["job_id"] == job.job_id
    assert entry["node_id"] == worker_identity.node_id
    assert entry["metadata"]["job_type"] == "inference.echo.v1"


def test_duplicate_result_does_not_duplicate_ledger_entry():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(identity=worker_identity)
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
    coordinator.create_echo_inference_job("ledger duplicate guard")
    leased = coordinator.lease_next_job(worker_identity.node_id)
    assert leased is not None
    result = worker.run_job(leased)

    assert coordinator.submit_result(result)
    assert coordinator.submit_result(result) is False

    assert coordinator.credit_ledger_summary()["entries"] == 1
    assert coordinator.credits[worker_identity.node_id] == 1


def test_credit_debit_refuses_negative_balance():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    account_id = "user_demo_account"

    grant = coordinator.apply_credit_delta(
        account_id=account_id,
        account_type="user",
        delta=5,
        reason="operator_credit_grant",
    )
    assert grant.balance_after == 5

    with pytest.raises(ValueError, match="negative"):
        coordinator.apply_credit_delta(
            account_id=account_id,
            account_type="user",
            delta=-6,
            reason="job_request_spend",
        )

    spend = coordinator.apply_credit_delta(
        account_id=account_id,
        account_type="user",
        delta=-3,
        reason="job_request_spend",
    )
    assert spend.balance_after == 2
    assert coordinator.credits[account_id] == 2
    assert coordinator.credit_ledger_summary()["negative_credits"] == 3


def test_credit_ledger_persists_across_restart(tmp_path):
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
    coordinator.create_echo_inference_job("persist ledger")
    leased = coordinator.lease_next_job(worker_identity.node_id)
    assert leased is not None
    assert coordinator.submit_result(worker.run_job(leased))

    restarted = Coordinator(
        identity=coordinator_identity,
        store=SQLiteCoordinatorStore(db_path),
    )

    assert restarted.credits[worker_identity.node_id] == 1
    assert restarted.credit_ledger_summary()["entries"] == 1
    assert restarted.credit_ledger_entries()[0]["reason"] == "worker_result_reward"
    assert restarted.snapshot()["credit_ledger"]["summary"]["entries"] == 1

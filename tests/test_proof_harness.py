import json

from chatp2p.proof import SwarmProofConfig, run_swarm_proof


def test_swarm_proof_verifies_small_batch(tmp_path):
    report_path = tmp_path / "reports" / "small-swarm.json"

    report = run_swarm_proof(
        SwarmProofConfig(
            workers=4,
            jobs=8,
            work_dir=tmp_path / "proof",
            report_path=report_path,
            timeout_seconds=45,
            lease_timeout_seconds=5,
            poll_interval=0.05,
            worker_interval=0.02,
        )
    )

    assert report["passed"] is True
    assert report["workers_registered"] == 4
    assert report["jobs_created"] == 8
    assert report["verified_jobs"] == 8
    assert report["disputed_jobs"] == 0
    assert report["pending_jobs"] == 0
    assert report["queued_jobs"] == 0
    assert report["leased_jobs"] == 0
    assert report["accepted_results"] == 16
    assert report["worker_errors"] == []
    assert json.loads(report_path.read_text(encoding="utf-8"))["run_id"] == report["run_id"]


def test_swarm_proof_recovers_from_acknowledged_timeout_worker(tmp_path):
    report_path = tmp_path / "reports" / "lease-recovery.json"

    report = run_swarm_proof(
        SwarmProofConfig(
            workers=5,
            jobs=8,
            work_dir=tmp_path / "proof",
            report_path=report_path,
            timeout_seconds=45,
            lease_timeout_seconds=3,
            poll_interval=0.05,
            worker_interval=0.02,
            fault_timeout_workers=1,
        )
    )

    fault_workers = [
        summary
        for summary in report["worker_summaries"]
        if summary["role"] == "fault-timeout"
    ]

    assert report["passed"] is True
    assert report["verified_jobs"] == 8
    assert report["disputed_jobs"] == 0
    assert report["expired_leases"] >= 1
    assert len(fault_workers) == 1
    assert fault_workers[0]["leased_without_submit"] is True
    assert fault_workers[0]["leased_job_id"]

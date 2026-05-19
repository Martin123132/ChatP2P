import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chatp2p.proof import OllamaProofConfig, SwarmProofConfig, run_ollama_proof, run_swarm_proof


def _start_fake_ollama(models=None):
    models = ["tiny-test-model"] if models is None else models
    requests = []

    class FakeOllamaHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/tags":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"models": [{"name": model} for model in models]}).encode("utf-8")
            )

        def do_POST(self):
            if self.path != "/api/generate":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "model": body["model"],
                        "response": f"proof ok: {body['prompt'][:48]}",
                        "done": True,
                        "eval_count": 3,
                        "eval_duration": 1000,
                    }
                ).encode("utf-8")
            )

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}", requests


def _stop_fake_ollama(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


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
            timeout_seconds=90,
            lease_timeout_seconds=12,
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


def test_ollama_proof_runs_against_fake_ollama(tmp_path):
    server, thread, base_url, requests = _start_fake_ollama()
    report_path = tmp_path / "reports" / "ollama-proof.json"

    try:
        report = run_ollama_proof(
            OllamaProofConfig(
                workers=3,
                jobs=4,
                model="tiny-test-model",
                prompt="Say hello from the proof harness.",
                work_dir=tmp_path / "proof",
                report_path=report_path,
                timeout_seconds=90,
                lease_timeout_seconds=20,
                poll_interval=0.05,
                worker_interval=0.02,
                ollama_base_url=base_url,
                mismatched_workers=1,
            )
        )
    finally:
        _stop_fake_ollama(server, thread)

    mismatch_workers = [
        summary
        for summary in report["worker_summaries"]
        if summary["role"] == "ollama-mismatch"
    ]

    assert report["passed"] is True
    assert report["proof_kind"] == "ollama"
    assert report["workers_registered"] == 3
    assert report["jobs_created"] == 4
    assert report["verified_jobs"] == 4
    assert report["disputed_jobs"] == 0
    assert report["accepted_results"] == 4
    assert report["worker_errors"] == []
    assert report["ollama"]["model"] == "tiny-test-model"
    assert report["ollama"]["mismatched_workers"] == 1
    assert len(report["ollama_results"]) == 4
    assert len(requests) == 4
    assert len(mismatch_workers) == 1
    assert mismatch_workers[0]["completed_jobs"] == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["run_id"] == report["run_id"]


def test_ollama_proof_fails_fast_when_model_is_missing(tmp_path):
    server, thread, base_url, _requests = _start_fake_ollama(models=["other-model"])
    try:
        with pytest.raises(ValueError, match="does not advertise model"):
            run_ollama_proof(
                OllamaProofConfig(
                    workers=1,
                    jobs=1,
                    model="tiny-test-model",
                    work_dir=tmp_path / "proof",
                    report_path=tmp_path / "reports" / "missing-model.json",
                    ollama_base_url=base_url,
                )
            )
    finally:
        _stop_fake_ollama(server, thread)

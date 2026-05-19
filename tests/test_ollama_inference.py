import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chatp2p.benchmark import capabilities_from_benchmark
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.ollama import OllamaError, list_ollama_models
from chatp2p.worker import WorkerNode


def _ollama_capabilities(available=True, models=None):
    models = ["tiny-test-model"] if models is None and available else (models or [])
    return capabilities_from_benchmark(
        {
            "hardware": {
                "cpu_count": 8,
                "ram_total_mb": 16_000,
                "system": "TestOS",
            },
            "gpu": {
                "available": False,
                "provider": None,
                "devices": [],
                "total_vram_mb": None,
            },
            "benchmark": {"cpu_iterations_per_second": 10_000},
            "model_runtimes": {
                "ollama": {
                    "available": available,
                    "path": "/usr/bin/ollama" if available else None,
                    "models": models,
                }
            },
        }
    )


def _start_fake_ollama(response_body, status=200, models=None):
    models = ["tiny-test-model"] if models is None else models
    requests = []

    class FakeOllamaHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(json.loads(self.rfile.read(length).decode("utf-8")))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_body).encode("utf-8"))

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

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}", requests


def test_list_ollama_models_reads_tags_endpoint():
    server, thread, base_url, _requests = _start_fake_ollama(
        {"model": "tiny-test-model", "response": "ok", "done": True},
        models=["mistral:7b", "llama3.2:3b"],
    )
    try:
        assert list_ollama_models(base_url=base_url) == ["llama3.2:3b", "mistral:7b"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_worker_runs_ollama_job_against_fake_server():
    server, thread, base_url, requests = _start_fake_ollama(
        {
            "model": "tiny-test-model",
            "response": "A peer-to-peer AI network shares work across signed nodes.",
            "done": True,
            "eval_count": 9,
            "total_duration": 123,
        }
    )
    try:
        coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(
            identity=worker_identity,
            capability_profile=_ollama_capabilities(),
            ollama_base_url=base_url,
        )
        coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
        job = coordinator.create_ollama_inference_job(
            model="tiny-test-model",
            prompt="Explain the mesh",
            temperature=0.2,
        )

        assert job.resource_requirements["ollama_model"] == "tiny-test-model"

        leased = coordinator.lease_next_job(worker_identity.node_id)
        assert leased is not None
        assert leased.job_id == job.job_id

        result = worker.run_job(leased)

        assert result.output["answer"] == "A peer-to-peer AI network shares work across signed nodes."
        assert result.output["model"] == "tiny-test-model"
        assert result.output["confidence"] == 1.0
        assert result.output["ollama"]["eval_count"] == 9
        assert requests[0]["stream"] is False
        assert requests[0]["options"]["temperature"] == 0.2
        assert coordinator.submit_result(result)
        assert coordinator.credits[worker_identity.node_id] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ollama_job_requires_ollama_capable_worker():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    worker_identity = NodeIdentity.generate(prefix="worker")
    worker = WorkerNode(
        identity=worker_identity,
        capability_profile=_ollama_capabilities(available=False),
    )
    coordinator.register_node(worker_identity.public(), capabilities=worker.capabilities())
    coordinator.create_ollama_inference_job(model="tiny-test-model", prompt="hello")

    assert "inference.ollama.v1" not in worker.capabilities()["supported_job_types"]
    assert coordinator.lease_next_job(worker_identity.node_id) is None


def test_ollama_job_requires_requested_local_model():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    wrong_model_identity = NodeIdentity.generate(prefix="worker")
    right_model_identity = NodeIdentity.generate(prefix="worker")
    coordinator.register_node(
        wrong_model_identity.public(),
        capabilities=_ollama_capabilities(models=["mistral:7b"]),
    )
    coordinator.register_node(
        right_model_identity.public(),
        capabilities=_ollama_capabilities(models=["tiny-test-model"]),
    )
    coordinator.create_ollama_inference_job(model="tiny-test-model", prompt="hello")

    assert coordinator.lease_next_job(wrong_model_identity.node_id) is None
    leased = coordinator.lease_next_job(right_model_identity.node_id)
    assert leased is not None
    assert leased.payload["model"] == "tiny-test-model"


def test_ollama_payload_validation():
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))

    with pytest.raises(ValueError, match="model"):
        coordinator.create_job(job_type="inference.ollama.v1", payload={"model": "", "prompt": "hello"})
    with pytest.raises(ValueError, match="prompt"):
        coordinator.create_job(job_type="inference.ollama.v1", payload={"model": "tiny", "prompt": ""})
    with pytest.raises(ValueError, match="temperature"):
        coordinator.create_job(
            job_type="inference.ollama.v1",
            payload={"model": "tiny", "prompt": "hello", "temperature": 4},
        )


def test_worker_rejects_bad_ollama_response():
    server, thread, base_url, _requests = _start_fake_ollama({"done": True})
    try:
        coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
        worker_identity = NodeIdentity.generate(prefix="worker")
        worker = WorkerNode(
            identity=worker_identity,
            capability_profile=_ollama_capabilities(),
            ollama_base_url=base_url,
        )
        job = coordinator.create_ollama_inference_job(model="tiny-test-model", prompt="hello")

        with pytest.raises(OllamaError, match="response"):
            worker.run_job(job)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chatp2p.benchmark import CAPABILITY_PROFILE_NAME, capabilities_from_benchmark, save_node_benchmark
from chatp2p.crypto import NodeIdentity
from chatp2p.doctor import NodeDoctorConfig, run_node_doctor


def _benchmark_report(models=None):
    models = [] if models is None else models
    report = {
        "schema": "chatp2p.node-benchmark.v1",
        "hardware": {
            "machine": "test",
            "processor": "test",
            "python_version": "3.13",
            "system": "test",
            "system_version": "test",
            "cpu_count": 8,
            "ram_total_mb": 16_000,
            "disk_free_mb": 100_000,
        },
        "gpu": {"available": False, "provider": None, "devices": [], "total_vram_mb": None},
        "benchmark": {"cpu_iterations_per_second": 10_000},
        "model_runtimes": {
            "ollama": {
                "available": bool(models),
                "path": "/usr/bin/ollama" if models else None,
                "base_url": "http://127.0.0.1:11434",
                "models": models,
            }
        },
    }
    report["capability_tier"] = "gaming_laptop"
    report["capabilities"] = capabilities_from_benchmark(report)
    return report


def _save_benchmark(home, models=None):
    save_node_benchmark(_benchmark_report(models), home / CAPABILITY_PROFILE_NAME)


def _start_fake_ollama(models):
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

        def log_message(self, format, *args):
            return

    return _start_server(FakeOllamaHandler)


def _start_fake_coordinator():
    class FakeCoordinatorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "coordinator_id": "coordinator_test",
                        "known_nodes": 0,
                        "jobs": 0,
                        "verified_jobs": 0,
                    }
                ).encode("utf-8")
            )

        def log_message(self, format, *args):
            return

    return _start_server(FakeCoordinatorHandler)


def _start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _statuses(report):
    return {check["id"]: check["status"] for check in report["checks"]}


def test_node_doctor_ready_with_requested_model(tmp_path, monkeypatch):
    home = tmp_path / ".mesh"
    NodeIdentity.generate(prefix="worker").save(home / "worker.identity.json")
    _save_benchmark(home, models=["tiny-test-model"])
    monkeypatch.setattr("chatp2p.doctor.shutil.which", lambda command: "/usr/bin/ollama")
    ollama_server, ollama_thread, ollama_url = _start_fake_ollama(["tiny-test-model"])
    coordinator_server, coordinator_thread, coordinator_url = _start_fake_coordinator()

    try:
        report = run_node_doctor(
            NodeDoctorConfig(
                home=home,
                model="tiny-test-model",
                ollama_base_url=ollama_url,
                coordinator_url=coordinator_url,
            )
        )
    finally:
        _stop_server(coordinator_server, coordinator_thread)
        _stop_server(ollama_server, ollama_thread)

    statuses = _statuses(report)
    assert report["ok"] is True
    assert statuses["worker_identity"] == "pass"
    assert statuses["benchmark_profile"] == "pass"
    assert statuses["ollama_runtime_model"] == "pass"
    assert statuses["advertised_ollama_model"] == "pass"
    assert statuses["coordinator"] == "pass"


def test_node_doctor_reports_missing_setup_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr("chatp2p.doctor.shutil.which", lambda command: None)
    ollama_server, ollama_thread, ollama_url = _start_fake_ollama(["other-model"])

    try:
        report = run_node_doctor(
            NodeDoctorConfig(
                home=tmp_path / ".mesh",
                model="tiny-test-model",
                ollama_base_url=ollama_url,
                coordinator_url=None,
            )
        )
    finally:
        _stop_server(ollama_server, ollama_thread)

    statuses = _statuses(report)
    assert report["ok"] is False
    assert statuses["worker_identity"] == "warn"
    assert statuses["benchmark_profile"] == "fail"
    assert statuses["ollama_binary"] == "warn"
    assert statuses["ollama_runtime_model"] == "fail"
    assert statuses["advertised_ollama_model"] == "skip"
    assert statuses["coordinator"] == "skip"


def test_node_doctor_catches_model_missing_from_saved_capabilities(tmp_path, monkeypatch):
    home = tmp_path / ".mesh"
    NodeIdentity.generate(prefix="worker").save(home / "worker.identity.json")
    _save_benchmark(home, models=["other-model"])
    monkeypatch.setattr("chatp2p.doctor.shutil.which", lambda command: "/usr/bin/ollama")
    ollama_server, ollama_thread, ollama_url = _start_fake_ollama(["tiny-test-model"])

    try:
        report = run_node_doctor(
            NodeDoctorConfig(
                home=home,
                model="tiny-test-model",
                ollama_base_url=ollama_url,
                coordinator_url=None,
            )
        )
    finally:
        _stop_server(ollama_server, ollama_thread)

    statuses = _statuses(report)
    assert report["ok"] is False
    assert statuses["ollama_runtime_model"] == "pass"
    assert statuses["advertised_ollama_model"] == "fail"

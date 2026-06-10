import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chatp2p.cli import build_parser
from chatp2p.model_registry import default_model_registry
from chatp2p.model_runtime import (
    MODEL_RUNTIME_CHECK_REPORT_SCHEMA,
    ModelRuntimeCheckConfig,
    run_model_runtime_check,
)


def test_model_runtime_check_passes_when_ollama_model_is_present(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    server, thread, base_url, requests = _start_fake_ollama(
        {"model": "qwen2.5:7b-instruct", "response": "ok", "done": True},
        models=["qwen2.5:7b-instruct"],
    )
    try:
        report = run_model_runtime_check(
            ModelRuntimeCheckConfig(
                registry_path=registry_path,
                model_id="qwen2.5-7b-instruct",
                out_dir=tmp_path / "runtime",
                ollama_base_url=base_url,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert report["schema"] == MODEL_RUNTIME_CHECK_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["status"] == "pass"
    assert report["summary"]["runtime_verified"] is True
    assert report["summary"]["does_not_approve_model"] is True
    assert report["summary"]["registry_write"] is False
    assert report["summary"]["recommended_next_action"] == "attach_runtime_evidence_to_candidate_registry"
    assert report["runtime"]["ollama_model"] == "qwen2.5:7b-instruct"
    assert requests[0]["model"] == "qwen2.5:7b-instruct"
    assert (tmp_path / "runtime" / "model-runtime-check.json").exists()
    assert (tmp_path / "runtime" / "model-runtime-check.md").exists()


def test_model_runtime_check_warns_when_ollama_model_is_missing(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    server, thread, base_url, _requests = _start_fake_ollama(
        {"model": "other-model", "response": "ok", "done": True},
        models=["other-model"],
    )
    try:
        report = run_model_runtime_check(
            ModelRuntimeCheckConfig(
                registry_path=registry_path,
                model_id="qwen2.5-7b-instruct",
                out_dir=tmp_path / "runtime",
                ollama_base_url=base_url,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["runtime_verified"] is False
    assert report["summary"]["reachable"] is True
    assert report["summary"]["model_present"] is False
    assert report["summary"]["recommended_next_action"] == "pull_or_choose_ollama_model"


def test_model_runtime_check_warns_when_ollama_is_unreachable(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_runtime_check(
        ModelRuntimeCheckConfig(
            registry_path=registry_path,
            model_id="qwen2.5-7b-instruct",
            out_dir=tmp_path / "runtime",
            ollama_base_url="http://127.0.0.1:1",
            ollama_timeout_seconds=0.2,
        )
    )

    assert report["ok"] is True
    assert report["status"] == "warn"
    assert report["summary"]["reachable"] is False
    assert report["summary"]["recommended_next_action"] == "start_or_install_ollama"


def test_model_runtime_check_fails_for_missing_model_id(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)

    report = run_model_runtime_check(
        ModelRuntimeCheckConfig(
            registry_path=registry_path,
            model_id="missing-model",
            out_dir=tmp_path / "runtime",
        )
    )

    assert report["ok"] is False
    assert report["status"] == "fail"
    assert report["summary"]["recommended_next_action"] == "fix_runtime_check_errors"
    assert any("model_id not found in registry" in error for error in report["errors"])


def test_model_runtime_check_report_has_no_token_like_values(tmp_path):
    registry_path = _write_qwen_registry(tmp_path)
    server, thread, base_url, _requests = _start_fake_ollama(
        {"model": "qwen2.5:7b-instruct", "response": "ok", "done": True},
        models=["qwen2.5:7b-instruct"],
    )
    try:
        report = run_model_runtime_check(
            ModelRuntimeCheckConfig(
                registry_path=registry_path,
                model_id="qwen2.5-7b-instruct",
                out_dir=tmp_path / "runtime",
                ollama_base_url=base_url,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    serialized = json.dumps(report)
    assert "admission_token" not in serialized
    assert "alpha-token-" not in serialized
    assert "PRIVATE KEY" not in serialized
    assert "tskey-" not in serialized


def test_model_runtime_check_parser_accepts_flags():
    parser = build_parser()

    args = parser.parse_args(
        [
            "model",
            "runtime-check",
            "--registry",
            "D:\\ChatP2PData\\model-candidate-pack\\staging-model-registry.json",
            "--model-id",
            "qwen2.5-7b-instruct",
            "--runtime",
            "ollama",
            "--ollama-model",
            "qwen2.5:7b-instruct",
            "--out",
            "D:\\ChatP2PData\\model-runtime-check",
            "--ollama-base-url",
            "http://127.0.0.1:11434",
            "--ollama-timeout-seconds",
            "10",
            "--prompt",
            "Reply ok",
            "--expected-text",
            "ok",
            "--json",
        ]
    )

    assert args.func.__name__ == "model_runtime_check_command"
    assert args.command == "model"
    assert args.model_command == "runtime-check"
    assert args.model_id == "qwen2.5-7b-instruct"
    assert args.ollama_model == "qwen2.5:7b-instruct"
    assert args.json is True


def _write_qwen_registry(tmp_path):
    registry = default_model_registry()
    registry["models"].append(
        {
            "id": "qwen2.5-7b-instruct",
            "status": "candidate",
            "provider": "Qwen",
            "project": "Qwen2.5-7B-Instruct",
            "family": "base_chat_model",
            "variant": "Qwen2.5-7B-Instruct",
            "license": "Apache-2.0",
            "license_url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
            "source_url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
            "parameter_count_b": 7.61,
            "architecture": "transformer",
            "context_length_tokens": 131072,
            "domains": ["general", "coding", "maths"],
            "runtimes": [
                {"id": "ollama", "support_status": "candidate", "notes": "local smoke pending"},
                {"id": "llama.cpp", "support_status": "candidate", "notes": "quantization pending"},
            ],
            "hardware": {
                "min_ram_gb": 16,
                "min_vram_gb": 8,
                "recommended_capability_tier": "gaming_laptop",
            },
            "artifacts": {
                "manifest_sha256": "TBD",
                "weights_sha256": "TBD",
                "quantization": "TBD",
            },
            "eval_plan": {
                "required_evaluations": [
                    "domain_eval",
                    "regression_eval",
                    "safety_eval",
                    "license_review",
                    "local_smoke",
                ],
                "success_criteria": {
                    "minimum_domain_pass_rate": 0.7,
                    "no_known_license_blocker": True,
                    "local_chat_smoke_passes": True,
                },
                "completed_evaluations": [],
            },
            "governance": {
                "proposal_id": None,
                "review_status": "not_submitted",
                "rollback_plan": None,
                "approved_by": [],
            },
        }
    )
    registry_path = tmp_path / "model-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return registry_path


def _start_fake_ollama(response_body, status=200, models=None):
    models = ["qwen2.5:7b-instruct"] if models is None else models
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
            self.wfile.write(json.dumps({"models": [{"name": model} for model in models]}).encode("utf-8"))

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}", requests

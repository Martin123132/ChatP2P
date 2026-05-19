import json
import threading

from chatp2p.alpha import (
    ALPHA_INVITE_SCHEMA,
    AlphaInvite,
    AlphaJoinConfig,
    bootstrap_alpha,
    load_alpha_invite,
    run_alpha_join,
    write_alpha_invite,
)
from chatp2p.benchmark import CAPABILITY_PROFILE_NAME, capabilities_from_benchmark, save_node_benchmark
from chatp2p.client import CoordinatorClient
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.node_runtime import managed_process_status, stop_managed_process
from chatp2p.operator_config import OperatorConfig


def _benchmark_report():
    report = {
        "schema": "chatp2p.node-benchmark.v1",
        "hardware": {
            "machine": "test",
            "processor": "test",
            "python_version": "3.13",
            "system": "test",
            "system_version": "test",
            "cpu_count": 4,
            "ram_total_mb": 8_000,
            "disk_free_mb": 100_000,
        },
        "gpu": {"available": False, "provider": None, "devices": [], "total_vram_mb": None},
        "benchmark": {"cpu_iterations_per_second": 10_000},
        "model_runtimes": {
            "ollama": {
                "available": False,
                "path": None,
                "base_url": "http://127.0.0.1:11434",
                "models": [],
            },
            "llama_cpp": {"available": False, "path": None, "command": None},
            "vllm": {"available": False},
        },
    }
    report["capability_tier"] = "standard"
    report["capabilities"] = capabilities_from_benchmark(report)
    return report


def _save_benchmark(home):
    save_node_benchmark(_benchmark_report(), home / CAPABILITY_PROFILE_NAME)


def _start_public_alpha(token="alpha-token-123"):
    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    operator_config = OperatorConfig(public_alpha=True, admission_token=token)
    server = create_coordinator_http_server(
        coordinator,
        host="127.0.0.1",
        port=0,
        operator_config=operator_config,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_alpha_invite_round_trip_and_validation(tmp_path):
    invite_path = tmp_path / "alpha-invite.json"
    invite = AlphaInvite.create(
        coordinator="http://127.0.0.1:8765",
        admission_token="alpha-token-123",
        allowed_job_types=("eval.deterministic.v1",),
        notes="test invite",
    )

    write_alpha_invite(invite_path, invite)
    loaded = load_alpha_invite(invite_path)

    assert loaded.schema == ALPHA_INVITE_SCHEMA
    assert loaded.coordinator == "http://127.0.0.1:8765"
    assert loaded.admission_token == "alpha-token-123"
    assert loaded.allowed_job_types == ("eval.deterministic.v1",)
    assert "admission_token" not in loaded.public_summary()

    bad_invite_path = tmp_path / "bad-invite.json"
    bad_invite_path.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
    try:
        load_alpha_invite(bad_invite_path)
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("Expected malformed invite to fail validation")

    try:
        load_alpha_invite(tmp_path / "missing-invite.json")
    except FileNotFoundError as exc:
        assert "invite file not found" in str(exc)
    else:
        raise AssertionError("Expected missing invite to fail validation")


def test_bootstrap_alpha_generates_private_token_and_public_summaries(tmp_path):
    config_path = tmp_path / "operator-config.json"
    invite_path = tmp_path / "alpha-invite.json"

    report = bootstrap_alpha(
        config_path=config_path,
        invite_path=invite_path,
        coordinator_url="http://example.test:8765",
    )
    config = OperatorConfig.from_file(config_path)
    invite = load_alpha_invite(invite_path)

    assert config.public_alpha is True
    assert config.admission_token is not None
    assert len(config.admission_token) >= 32
    assert invite.admission_token == config.admission_token
    assert "admission_token" not in config.public_summary()
    assert config.admission_token not in json.dumps(report)


def test_node_join_starts_worker_and_registers_with_public_alpha_invite(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    home = tmp_path / ".mesh"
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator=coordinator_url, admission_token=token),
    )
    _save_benchmark(home)

    try:
        report = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        status = managed_process_status(home=home, role="worker")
        snapshot = CoordinatorClient(coordinator_url, admission_token=token).snapshot()
    finally:
        stop_managed_process(home=home, role="worker")
        _stop_server(server, thread)

    assert report["ok"] is True
    assert report["status"] == "joined"
    assert report["benchmark"]["status"] == "existing"
    assert status["alive"] is True
    assert any(node["node_id"] == report["worker_node_id"] for node in snapshot["nodes"])
    assert token not in json.dumps(report)
    assert token not in json.dumps(status)


def test_node_join_fails_and_cleans_up_when_invite_token_is_wrong(tmp_path):
    server, thread, coordinator_url = _start_public_alpha("correct-token-123")
    home = tmp_path / ".mesh"
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator=coordinator_url, admission_token="wrong-token-123"),
    )
    _save_benchmark(home)

    try:
        report = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=home,
                worker_interval=0.2,
                startup_timeout_seconds=5.0,
                force=True,
            )
        )
        status = managed_process_status(home=home, role="worker")
    finally:
        stop_managed_process(home=home, role="worker")
        _stop_server(server, thread)

    assert report["ok"] is False
    assert report["status"] == "needs_attention"
    assert status["managed"] is False


def test_node_join_reports_unreachable_coordinator_without_starting_worker(tmp_path):
    home = tmp_path / ".mesh"
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:1", admission_token="alpha-token-123"),
    )
    _save_benchmark(home)

    report = run_alpha_join(
        AlphaJoinConfig(
            invite_path=invite_path,
            home=home,
            startup_timeout_seconds=1.0,
            force=True,
        )
    )

    assert report["ok"] is False
    assert report["status"] == "coordinator_unreachable"
    assert managed_process_status(home=home, role="worker")["managed"] is False

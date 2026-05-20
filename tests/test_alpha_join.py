import json
import threading

import chatp2p.alpha as alpha_module
from chatp2p.alpha import (
    ALPHA_DRILL_REPORT_SCHEMA,
    ALPHA_EVIDENCE_PACK_SCHEMA,
    ALPHA_INVITE_SCHEMA,
    ALPHA_PREFLIGHT_REPORT_SCHEMA,
    ALPHA_REMOTE_PROOF_REPORT_SCHEMA,
    ALPHA_ROUTE_REPORT_SCHEMA,
    ALPHA_SMOKE_REPORT_SCHEMA,
    ALPHA_STATUS_REPORT_SCHEMA,
    AlphaDrillConfig,
    AlphaEvidenceConfig,
    AlphaInvite,
    AlphaJoinConfig,
    AlphaPreflightConfig,
    AlphaRemoteProofConfig,
    AlphaRouteConfig,
    AlphaSmokeConfig,
    AlphaStatusConfig,
    NODE_WATCHDOG_REPORT_SCHEMA,
    NodeWatchdogConfig,
    bootstrap_alpha,
    load_alpha_invite,
    run_alpha_drill,
    run_alpha_evidence,
    run_alpha_join,
    run_alpha_preflight,
    run_alpha_remote_proof,
    run_alpha_route,
    run_alpha_smoke,
    run_alpha_status,
    run_node_watchdog,
    write_alpha_invite,
    _invite_url_check,
)
from chatp2p.benchmark import CAPABILITY_PROFILE_NAME, capabilities_from_benchmark, save_node_benchmark
from chatp2p.client import CoordinatorClient
from chatp2p.coordinator import Coordinator
from chatp2p.crypto import NodeIdentity
from chatp2p.http_api import create_coordinator_http_server
from chatp2p.node_runtime import managed_process_status, stop_managed_process
from chatp2p.operator_config import OperatorConfig, write_operator_config


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


def test_alpha_preflight_passes_and_warns_for_local_invite_url(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    config_path = tmp_path / "operator-config.json"
    invite_path = tmp_path / "alpha-invite.json"
    report_path = tmp_path / "alpha-preflight-report.json"
    write_operator_config(config_path, OperatorConfig(public_alpha=True, admission_token=token))
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))

    try:
        report = run_alpha_preflight(
            AlphaPreflightConfig(
                config_path=config_path,
                invite_path=invite_path,
                home=tmp_path / ".mesh",
                report_path=report_path,
            )
        )
    finally:
        _stop_server(server, thread)

    checks = {check["id"]: check["status"] for check in report["checks"]}
    assert report["schema"] == ALPHA_PREFLIGHT_REPORT_SCHEMA
    assert report["ok"] is True
    assert checks["invite_token_matches_config"] == "pass"
    assert checks["coordinator_public_alpha"] == "pass"
    assert checks["invite_url_shareable"] == "warn"
    assert report_path.exists()
    assert token not in json.dumps(report)


def test_invite_url_check_warns_for_private_network_addresses():
    private_invite = AlphaInvite.create(
        coordinator="http://192.168.4.90:8765",
        admission_token="alpha-token-123",
    )
    local_name_invite = AlphaInvite.create(
        coordinator="http://chatp2p.local:8765",
        admission_token="alpha-token-123",
    )
    shared_network_invite = AlphaInvite.create(
        coordinator="http://100.64.10.20:8765",
        admission_token="alpha-token-123",
    )
    public_invite = AlphaInvite.create(
        coordinator="https://chatp2p.example.com",
        admission_token="alpha-token-123",
    )

    private_check = _invite_url_check(private_invite)
    local_name_check = _invite_url_check(local_name_invite)
    shared_network_check = _invite_url_check(shared_network_invite)
    public_check = _invite_url_check(public_invite)

    assert private_check["status"] == "warn"
    assert private_check["details"]["reachability"] == "private_network"
    assert "VPN" in private_check["message"]
    assert local_name_check["status"] == "warn"
    assert local_name_check["details"]["reachability"] == "local_name"
    assert shared_network_check["status"] == "warn"
    assert shared_network_check["details"]["reachability"] == "shared_network"
    assert public_check["status"] == "pass"
    assert public_check["details"]["reachability"] == "dns_name"


def test_alpha_route_reports_remote_readiness_without_exposing_token(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    invite_path = tmp_path / "alpha-invite.json"
    report_path = tmp_path / "alpha-route-report.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))

    try:
        report = run_alpha_route(
            AlphaRouteConfig(
                invite_path=invite_path,
                report_path=report_path,
                home=tmp_path / ".mesh",
                detect_tools=False,
            )
        )
    finally:
        _stop_server(server, thread)

    assert report["schema"] == ALPHA_ROUTE_REPORT_SCHEMA
    assert report["status"] == "warn"
    assert report["ok"] is True
    assert report["current_route"]["health"]["ok"] is True
    assert report["current_route"]["outside_ready"] is False
    assert report["current_route"]["reachability"]["kind"] == "local_only"
    assert report["tooling"] is None
    assert report_path.exists()
    assert token not in json.dumps(report)


def test_alpha_route_fails_when_invite_coordinator_is_unreachable(tmp_path):
    token = "alpha-token-123"
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://127.0.0.1:1", admission_token=token),
    )

    report = run_alpha_route(
        AlphaRouteConfig(
            invite_path=invite_path,
            report_path=tmp_path / "alpha-route-report.json",
            timeout_seconds=0.2,
            detect_tools=False,
        )
    )

    assert report["status"] == "fail"
    assert report["ok"] is False
    assert report["errors"] == ["current invite coordinator health is not reachable from this machine"]
    assert token not in json.dumps(report)


def test_alpha_route_marks_this_machine_tailscale_ip_as_ready(tmp_path, monkeypatch):
    token = "alpha-token-123"
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator="http://100.64.10.20:8765", admission_token=token),
    )

    monkeypatch.setattr(
        alpha_module,
        "_remote_route_tooling",
        lambda: {
            "tailscale": {
                "installed": True,
                "path": "tailscale",
                "source": "path",
                "ip4": {"ok": True, "stdout": "100.64.10.20"},
            },
            "cloudflared": {"installed": False, "path": None, "source": None},
        },
    )
    monkeypatch.setattr(
        alpha_module,
        "_coordinator_health",
        lambda invite, *, timeout_seconds: {"ok": True, "url": invite.coordinator, "payload": {}},
    )

    report = run_alpha_route(
        AlphaRouteConfig(
            invite_path=invite_path,
            report_path=tmp_path / "alpha-route-report.json",
        )
    )

    assert report["status"] == "pass"
    assert report["ok"] is True
    assert report["current_route"]["outside_ready"] is True
    assert report["current_route"]["reachability"]["kind"] == "tailnet_self"
    assert token not in json.dumps(report)


def test_alpha_preflight_fails_for_bad_config_wrong_token_and_public_alpha_disabled(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    bad_config_path = tmp_path / "bad-config.json"
    wrong_invite_path = tmp_path / "wrong-invite.json"
    bad_config_path.write_text("{not-json", encoding="utf-8")
    write_alpha_invite(
        wrong_invite_path,
        AlphaInvite.create(coordinator=coordinator_url, admission_token="wrong-token-123"),
    )

    try:
        bad_config_report = run_alpha_preflight(
            AlphaPreflightConfig(
                config_path=bad_config_path,
                invite_path=wrong_invite_path,
                home=tmp_path / ".mesh",
                report_path=tmp_path / "bad-config-report.json",
            )
        )
    finally:
        _stop_server(server, thread)

    checks = {check["id"]: check["status"] for check in bad_config_report["checks"]}
    assert bad_config_report["ok"] is False
    assert checks["operator_config"] == "fail"

    server, thread, coordinator_url = _start_public_alpha(token)
    config_path = tmp_path / "operator-config.json"
    write_operator_config(config_path, OperatorConfig(public_alpha=True, admission_token=token))
    try:
        wrong_token_report = run_alpha_preflight(
            AlphaPreflightConfig(
                config_path=config_path,
                invite_path=wrong_invite_path,
                home=tmp_path / ".mesh",
                report_path=tmp_path / "wrong-token-report.json",
            )
        )
    finally:
        _stop_server(server, thread)

    checks = {check["id"]: check["status"] for check in wrong_token_report["checks"]}
    assert wrong_token_report["ok"] is False
    assert checks["invite_token_matches_config"] == "fail"
    assert token not in json.dumps(wrong_token_report)
    assert "wrong-token-123" not in json.dumps(wrong_token_report)

    coordinator = Coordinator(identity=NodeIdentity.generate(prefix="coordinator"))
    server = create_coordinator_http_server(coordinator, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    write_alpha_invite(
        wrong_invite_path,
        AlphaInvite.create(coordinator=f"http://{host}:{port}", admission_token=token),
    )
    try:
        disabled_report = run_alpha_preflight(
            AlphaPreflightConfig(
                config_path=config_path,
                invite_path=wrong_invite_path,
                home=tmp_path / ".mesh",
                report_path=tmp_path / "disabled-report.json",
            )
        )
    finally:
        _stop_server(server, thread)

    checks = {check["id"]: check["status"] for check in disabled_report["checks"]}
    assert disabled_report["ok"] is False
    assert checks["coordinator_public_alpha"] == "fail"


def test_alpha_smoke_creates_jobs_and_observes_accepted_results(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    home = tmp_path / ".mesh"
    invite_path = tmp_path / "alpha-invite.json"
    report_path = tmp_path / "alpha-smoke-report.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))
    _save_benchmark(home)

    try:
        join_report = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        smoke_report = run_alpha_smoke(
            AlphaSmokeConfig(
                invite_path=invite_path,
                jobs=2,
                min_live_workers=1,
                min_accepted_results=1,
                min_verified_jobs=0,
                timeout_seconds=10.0,
                poll_interval=0.2,
                report_path=report_path,
            )
        )
    finally:
        stop_managed_process(home=home, role="worker")
        _stop_server(server, thread)

    assert join_report["ok"] is True
    assert smoke_report["schema"] == ALPHA_SMOKE_REPORT_SCHEMA
    assert smoke_report["ok"] is True
    assert smoke_report["criteria"]["live_workers"]["passed"] is True
    assert smoke_report["criteria"]["accepted_results"]["actual"] >= 1
    assert smoke_report["criteria"]["disputed_jobs"]["actual"] == 0
    assert report_path.exists()
    assert token not in json.dumps(smoke_report)


def test_alpha_remote_proof_waits_for_verified_jobs_and_expected_worker(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    operator_home = tmp_path / ".mesh-operator"
    partner_home = tmp_path / ".mesh-partner"
    invite_path = tmp_path / "alpha-invite.json"
    report_path = tmp_path / "alpha-remote-proof-report.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))
    _save_benchmark(operator_home)
    _save_benchmark(partner_home)

    try:
        operator_join = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=operator_home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        partner_join = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=partner_home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        proof_report = run_alpha_remote_proof(
            AlphaRemoteProofConfig(
                invite_path=invite_path,
                jobs=2,
                expected_worker_id=partner_join["worker_node_id"],
                min_live_workers=2,
                timeout_seconds=15.0,
                poll_interval=0.2,
                report_path=report_path,
            )
        )
    finally:
        stop_managed_process(home=operator_home, role="worker")
        stop_managed_process(home=partner_home, role="worker")
        _stop_server(server, thread)

    assert operator_join["ok"] is True
    assert partner_join["ok"] is True
    assert proof_report["schema"] == ALPHA_REMOTE_PROOF_REPORT_SCHEMA
    assert proof_report["ok"] is True
    assert proof_report["criteria"]["live_workers"]["actual"] >= 2
    assert proof_report["criteria"]["verified_jobs"]["actual"] == 2
    assert proof_report["criteria"]["all_created_jobs_terminal"]["passed"] is True
    assert proof_report["criteria"]["expected_worker_results"]["actual"] >= 1
    assert proof_report["criteria"]["disputed_jobs"]["actual"] == 0
    assert proof_report["criteria"]["expired_jobs"]["actual"] == 0
    assert proof_report["expected_worker"]["node_id"] == partner_join["worker_node_id"]
    assert proof_report["baseline"]["job_count"] == 0
    assert report_path.exists()
    assert token not in json.dumps(proof_report)


def test_alpha_status_reports_expected_worker_without_exposing_token(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    home = tmp_path / ".mesh"
    invite_path = tmp_path / "alpha-invite.json"
    report_path = tmp_path / "alpha-status-report.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))
    _save_benchmark(home)

    try:
        join_report = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        status_report = run_alpha_status(
            AlphaStatusConfig(
                home=home,
                invite_path=invite_path,
                expected_worker_id=join_report["worker_node_id"],
                min_live_workers=1,
                timeout_seconds=5.0,
                report_path=report_path,
            )
        )
    finally:
        stop_managed_process(home=home, role="worker")
        _stop_server(server, thread)

    assert join_report["ok"] is True
    assert status_report["schema"] == ALPHA_STATUS_REPORT_SCHEMA
    assert status_report["ok"] is True
    checks = {check["id"]: check["status"] for check in status_report["checks"]}
    assert checks["coordinator_health"] == "pass"
    assert checks["expected_worker_live"] == "pass"
    assert status_report["expected_worker"]["node_id"] == join_report["worker_node_id"]
    assert report_path.exists()
    assert token not in json.dumps(status_report)


def test_alpha_evidence_pack_runs_remote_proof_and_redacts_artifacts(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    operator_home = tmp_path / ".mesh-operator"
    partner_home = tmp_path / ".mesh-partner"
    invite_path = tmp_path / "alpha-invite.json"
    out_dir = tmp_path / "evidence"
    watchdog_report = tmp_path / "node-watchdog-report.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))
    _save_benchmark(operator_home)
    _save_benchmark(partner_home)
    watchdog_report.write_text(
        json.dumps({"schema": NODE_WATCHDOG_REPORT_SCHEMA, "admission_token": token}),
        encoding="utf-8",
    )

    try:
        operator_join = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=operator_home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        partner_join = run_alpha_join(
            AlphaJoinConfig(
                invite_path=invite_path,
                home=partner_home,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                force=True,
            )
        )
        report = run_alpha_evidence(
            AlphaEvidenceConfig(
                home=operator_home,
                invite_path=invite_path,
                out_dir=out_dir,
                expected_worker_id=partner_join["worker_node_id"],
                jobs=1,
                min_live_workers=2,
                timeout_seconds=15.0,
                poll_interval=0.2,
                status_timeout_seconds=5.0,
                watchdog_report_path=watchdog_report,
                query_operator_task=False,
            )
        )
    finally:
        stop_managed_process(home=operator_home, role="worker")
        stop_managed_process(home=partner_home, role="worker")
        _stop_server(server, thread)

    assert operator_join["ok"] is True
    assert partner_join["ok"] is True
    assert report["schema"] == ALPHA_EVIDENCE_PACK_SCHEMA
    assert report["ok"] is True
    assert report["remote_proof"]["criteria"]["verified_jobs"]["actual"] == 1
    assert report["alpha_status"]["expected_worker"]["node_id"] == partner_join["worker_node_id"]
    assert (out_dir / "alpha-status.json").exists()
    assert (out_dir / "alpha-remote-proof.json").exists()
    assert (out_dir / "node-watchdog-report.json").exists()
    assert (out_dir / "operator-watchdog-task.json").exists()
    assert (out_dir / "alpha-evidence-summary.md").exists()
    assert token not in json.dumps(report)
    assert token not in (out_dir / "node-watchdog-report.json").read_text(encoding="utf-8")


def test_node_watchdog_starts_missing_worker_from_invite(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    home = tmp_path / ".mesh"
    invite_path = tmp_path / "alpha-invite.json"
    report_path = tmp_path / "node-watchdog-report.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))
    _save_benchmark(home)

    try:
        report = run_node_watchdog(
            NodeWatchdogConfig(
                home=home,
                invite_path=invite_path,
                report_path=report_path,
                role="worker",
                checks=1,
                interval_seconds=0.2,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                cpu_duration_seconds=0.0,
            )
        )
        worker_status = managed_process_status(home=home, role="worker")
    finally:
        stop_managed_process(home=home, role="worker")
        _stop_server(server, thread)

    assert report["schema"] == NODE_WATCHDOG_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["iterations"][0]["actions"][0]["role"] == "worker"
    assert report["iterations"][0]["actions"][0]["join"]["ok"] is True
    assert worker_status["alive"] is True
    assert report_path.exists()
    assert token not in json.dumps(report)


def test_alpha_drill_starts_simulated_worker_and_verifies_jobs(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    home = tmp_path / ".mesh"
    invite_path = tmp_path / "alpha-invite.json"
    report_path = tmp_path / "alpha-drill-report.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))
    _save_benchmark(home)

    try:
        report = run_alpha_drill(
            AlphaDrillConfig(
                home=home,
                invite_path=invite_path,
                report_path=report_path,
                start_coordinator=False,
                run_preflight=False,
                simulated_workers=1,
                jobs=2,
                worker_interval=0.2,
                startup_timeout_seconds=10.0,
                timeout_seconds=15.0,
                poll_interval=0.2,
                cpu_duration_seconds=0.0,
                keep_simulated_workers=False,
            )
        )
    finally:
        stop_managed_process(home=home, role="worker")
        stop_managed_process(home=tmp_path / ".mesh-alpha-drill" / "worker-1", role="worker")
        _stop_server(server, thread)

    assert report["schema"] == ALPHA_DRILL_REPORT_SCHEMA
    assert report["ok"] is True
    assert report["smoke"]["criteria"]["live_workers"]["actual"] >= 2
    assert report["smoke"]["criteria"]["verified_jobs"]["actual"] == 2
    assert report["smoke"]["criteria"]["accepted_results"]["actual"] >= 4
    assert report["smoke"]["criteria"]["disputed_jobs"]["actual"] == 0
    assert report["simulated_workers"][0]["join"]["ok"] is True
    assert report["cleanup"][0]["stop"]["status"] in {"stopped", "not_managed"}
    assert report_path.exists()
    assert (tmp_path / "alpha-drill-report-smoke.json").exists()
    assert token not in json.dumps(report)


def test_alpha_smoke_fails_without_live_workers_and_with_wrong_token(tmp_path):
    token = "alpha-token-123"
    server, thread, coordinator_url = _start_public_alpha(token)
    invite_path = tmp_path / "alpha-invite.json"
    write_alpha_invite(invite_path, AlphaInvite.create(coordinator=coordinator_url, admission_token=token))

    try:
        no_worker_report = run_alpha_smoke(
            AlphaSmokeConfig(
                invite_path=invite_path,
                jobs=1,
                min_live_workers=1,
                min_accepted_results=1,
                timeout_seconds=1.0,
                poll_interval=0.2,
                report_path=tmp_path / "no-worker-report.json",
            )
        )
    finally:
        _stop_server(server, thread)

    assert no_worker_report["ok"] is False
    assert no_worker_report["criteria"]["live_workers"]["passed"] is False

    server, thread, coordinator_url = _start_public_alpha(token)
    write_alpha_invite(
        invite_path,
        AlphaInvite.create(coordinator=coordinator_url, admission_token="wrong-token-123"),
    )
    try:
        wrong_token_report = run_alpha_smoke(
            AlphaSmokeConfig(
                invite_path=invite_path,
                jobs=1,
                timeout_seconds=1.0,
                poll_interval=0.2,
                report_path=tmp_path / "wrong-token-report.json",
            )
        )
    finally:
        _stop_server(server, thread)

    assert wrong_token_report["ok"] is False
    assert wrong_token_report["created_jobs"] == []
    assert "wrong-token-123" not in json.dumps(wrong_token_report)
